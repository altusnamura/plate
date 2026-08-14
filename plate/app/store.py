"""SQLite persistence.

Lives at ``/data/plate.db``, which is the add-on's persistent volume, so it
survives restarts and updates.

This database is also the *durable* copy of your metric history. Home Assistant's
recorder purges detailed history after ten days by default, and long-term
statistics can be reset or lost with a database migration. Since the whole
calibration idea depends on months of weight and intake data, everything read
from HA gets written here once and read from here afterwards.

Access is plain synchronous ``sqlite3`` behind a lock. The queries are small
enough (single-digit milliseconds on a Pi) that pushing them to a thread pool
would cost more in complexity than it saves in latency, and SQLite's own locking
makes the write path safe for the single-process add-on this is.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per metric per day. This is the durable mirror of HA history.
CREATE TABLE IF NOT EXISTS metric_daily (
    day     TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   REAL NOT NULL,
    source  TEXT NOT NULL DEFAULT 'ha',
    updated TEXT NOT NULL,
    PRIMARY KEY (day, key)
);
CREATE INDEX IF NOT EXISTS idx_metric_key_day ON metric_daily (key, day);

-- Every eaten thing. `slot` links back to a planned meal when the entry came
-- from ticking one off; free-form entries leave it null.
CREATE TABLE IF NOT EXISTS intake (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    day       TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    source    TEXT NOT NULL,
    recipe_id TEXT,
    slot      TEXT,
    label     TEXT,
    servings  REAL NOT NULL DEFAULT 1,
    nutrients TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intake_day ON intake (day);
CREATE UNIQUE INDEX IF NOT EXISTS idx_intake_slot ON intake (slot) WHERE slot IS NOT NULL;

-- Saved menu plans, keyed by the week they start.
CREATE TABLE IF NOT EXISTS plan (
    start   TEXT PRIMARY KEY,
    created TEXT NOT NULL,
    seed    INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL
);

-- What the calorie target actually was on a given day, so adherence can be
-- scored against the number that was live at the time rather than today's.
CREATE TABLE IF NOT EXISTS target_history (
    day        TEXT PRIMARY KEY,
    kcal       REAL NOT NULL,
    tdee       REAL,
    confidence REAL,
    payload    TEXT
);

CREATE TABLE IF NOT EXISTS pantry (
    food_id TEXT PRIMARY KEY,
    grams   REAL NOT NULL,
    updated TEXT NOT NULL
);

-- Shopping list tick-boxes, per plan week and store.
CREATE TABLE IF NOT EXISTS shop_check (
    plan_start TEXT NOT NULL,
    store_id   TEXT NOT NULL,
    food_id    TEXT NOT NULL,
    checked    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (plan_start, store_id, food_id)
);

-- User settings that override add-on options at runtime, so the UI can change
-- things without an add-on restart.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Meals the user pinned; the planner treats these as immovable.
CREATE TABLE IF NOT EXISTS pinned (
    slot      TEXT PRIMARY KEY,
    recipe_id TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        current = int(self.get_meta("schema_version", "0"))
        if current == SCHEMA_VERSION:
            return
        # No migrations to run yet; the schema is created idempotently above.
        # When a future version needs one, branch on `current` here.
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        log.info("database schema at version %d", SCHEMA_VERSION)

    # ---- meta and settings ------------------------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        out: dict[str, Any] = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                out[r["key"]] = r["value"]
        return out

    def put_settings(self, values: Mapping[str, Any]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(k, json.dumps(v)) for k, v in values.items()],
            )
            self._conn.commit()

    # ---- metrics ----------------------------------------------------------

    def put_metrics(
        self,
        key: str,
        series: Mapping[date, float],
        source: str = "ha",
        protect_manual: bool = True,
    ) -> int:
        """Upsert a daily series. Returns rows written.

        ``protect_manual`` is what keeps a hand-typed reading from being silently
        replaced by an automatic sync. If you weighed yourself, typed it in, and
        later connected a scale integration that reports something different for
        that day, the number you typed wins — you were there, the integration is
        guessing at a day boundary. Passing ``source="manual"`` always writes.
        """
        if not series:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        rows = [(d.isoformat(), key, float(v), source, now) for d, v in series.items()]

        sql = (
            "INSERT INTO metric_daily(day,key,value,source,updated) VALUES(?,?,?,?,?) "
            "ON CONFLICT(day,key) DO UPDATE SET "
            "  value=excluded.value, source=excluded.source, updated=excluded.updated"
        )
        if protect_manual and source != "manual":
            sql += " WHERE metric_daily.source <> 'manual'"

        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()
        return len(rows)

    def delete_metric(self, day: date, key: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM metric_daily WHERE day=? AND key=?", (day.isoformat(), key)
            )
            self._conn.commit()
            return cur.rowcount

    def metric_rows(self, since: date | None = None) -> list[dict[str, Any]]:
        """Full daily metric records including source, for the review table."""
        sql = "SELECT day, key, value, source, updated FROM metric_daily"
        params: list[Any] = []
        if since:
            sql += " WHERE day >= ?"
            params.append(since.isoformat())
        sql += " ORDER BY day DESC, key"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def metrics(self, key: str, since: date | None = None) -> dict[date, float]:
        sql = "SELECT day, value FROM metric_daily WHERE key=?"
        params: list[Any] = [key]
        if since:
            sql += " AND day >= ?"
            params.append(since.isoformat())
        sql += " ORDER BY day"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return {date.fromisoformat(r["day"]): r["value"] for r in rows}

    def metric_keys(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, COUNT(*) AS n FROM metric_daily GROUP BY key"
            ).fetchall()
        return {r["key"]: r["n"] for r in rows}

    def latest_metric(self, key: str) -> tuple[date, float] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT day, value FROM metric_daily WHERE key=? ORDER BY day DESC LIMIT 1",
                (key,),
            ).fetchone()
        return (date.fromisoformat(row["day"]), row["value"]) if row else None

    # ---- intake -----------------------------------------------------------

    def log_intake(
        self,
        day: date,
        nutrients: Mapping[str, float],
        source: str = "manual",
        recipe_id: str | None = None,
        slot: str | None = None,
        label: str | None = None,
        servings: float = 1.0,
    ) -> int:
        """Record something eaten.

        ``slot`` is uniquely indexed, so ticking off the same planned meal twice
        updates rather than duplicating — which is what you want when someone taps
        the button again to correct the portion.
        """
        now = datetime.now().isoformat(timespec="seconds")
        payload = json.dumps({k: round(float(v), 3) for k, v in nutrients.items()})
        with self._lock:
            if slot:
                self._conn.execute("DELETE FROM intake WHERE slot=?", (slot,))
            cur = self._conn.execute(
                "INSERT INTO intake(day,logged_at,source,recipe_id,slot,label,servings,nutrients) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (day.isoformat(), now, source, recipe_id, slot, label, servings, payload),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def delete_intake(self, entry_id: int | None = None, slot: str | None = None) -> int:
        with self._lock:
            if entry_id is not None:
                cur = self._conn.execute("DELETE FROM intake WHERE id=?", (entry_id,))
            elif slot is not None:
                cur = self._conn.execute("DELETE FROM intake WHERE slot=?", (slot,))
            else:
                return 0
            self._conn.commit()
            return cur.rowcount

    def intake_entries(self, day: date) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM intake WHERE day=? ORDER BY logged_at", (day.isoformat(),)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["nutrients"] = json.loads(d["nutrients"])
            out.append(d)
        return out

    def intake_by_day(self, since: date | None = None) -> dict[date, float]:
        """Total kcal per day, which is what the calibration consumes."""
        sql = "SELECT day, nutrients FROM intake"
        params: list[Any] = []
        if since:
            sql += " WHERE day >= ?"
            params.append(since.isoformat())
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        totals: dict[date, float] = {}
        for r in rows:
            try:
                kcal = float(json.loads(r["nutrients"]).get("kcal", 0.0))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            d = date.fromisoformat(r["day"])
            totals[d] = totals.get(d, 0.0) + kcal
        return totals

    def intake_nutrients(self, day: date) -> dict[str, float]:
        out: dict[str, float] = {}
        for entry in self.intake_entries(day):
            for k, v in entry["nutrients"].items():
                out[k] = out.get(k, 0.0) + float(v)
        return out

    def logged_slots(self, days: Iterable[date]) -> set[str]:
        wanted = [d.isoformat() for d in days]
        if not wanted:
            return set()
        marks = ",".join("?" * len(wanted))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT slot FROM intake WHERE slot IS NOT NULL AND day IN ({marks})", wanted
            ).fetchall()
        return {r["slot"] for r in rows}

    # ---- targets ----------------------------------------------------------

    def put_target(
        self, day: date, kcal: float, tdee: float | None, confidence: float | None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO target_history(day,kcal,tdee,confidence,payload) VALUES(?,?,?,?,?) "
                "ON CONFLICT(day) DO UPDATE SET kcal=excluded.kcal, tdee=excluded.tdee, "
                "  confidence=excluded.confidence, payload=excluded.payload",
                (day.isoformat(), kcal, tdee, confidence, json.dumps(payload or {})),
            )
            self._conn.commit()

    def targets_by_day(self, since: date | None = None) -> dict[date, float]:
        sql = "SELECT day, kcal FROM target_history"
        params: list[Any] = []
        if since:
            sql += " WHERE day >= ?"
            params.append(since.isoformat())
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return {date.fromisoformat(r["day"]): r["kcal"] for r in rows}

    # ---- plans ------------------------------------------------------------

    def put_plan(self, start: date, seed: int, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO plan(start,created,seed,payload) VALUES(?,?,?,?) "
                "ON CONFLICT(start) DO UPDATE SET created=excluded.created, "
                "  seed=excluded.seed, payload=excluded.payload",
                (
                    start.isoformat(),
                    datetime.now().isoformat(timespec="seconds"),
                    seed,
                    json.dumps(payload),
                ),
            )
            self._conn.commit()

    def get_plan(self, start: date) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM plan WHERE start=?", (start.isoformat(),)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def plan_covering(self, day: date) -> dict[str, Any] | None:
        """The most recent plan whose window contains ``day``."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT start, payload FROM plan WHERE start <= ? ORDER BY start DESC LIMIT 4",
                (day.isoformat(),),
            ).fetchall()
        for r in rows:
            payload = json.loads(r["payload"])
            days = {d.get("day") for d in payload.get("days", [])}
            if day.isoformat() in days:
                return payload
        return None

    def prune_plans(self, keep_weeks: int = 12) -> int:
        cutoff = (date.today() - timedelta(weeks=keep_weeks)).isoformat()
        with self._lock:
            cur = self._conn.execute("DELETE FROM plan WHERE start < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    # ---- pantry -----------------------------------------------------------

    def pantry(self) -> dict[str, float]:
        with self._lock:
            rows = self._conn.execute("SELECT food_id, grams FROM pantry").fetchall()
        return {r["food_id"]: r["grams"] for r in rows}

    def set_pantry(self, food_id: str, grams: float) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            if grams <= 0:
                self._conn.execute("DELETE FROM pantry WHERE food_id=?", (food_id,))
            else:
                self._conn.execute(
                    "INSERT INTO pantry(food_id,grams,updated) VALUES(?,?,?) "
                    "ON CONFLICT(food_id) DO UPDATE SET grams=excluded.grams, "
                    "  updated=excluded.updated",
                    (food_id, grams, now),
                )
            self._conn.commit()

    # ---- shopping ticks ---------------------------------------------------

    def shop_checks(self, plan_start: date) -> dict[tuple[str, str], bool]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT store_id, food_id, checked FROM shop_check WHERE plan_start=?",
                (plan_start.isoformat(),),
            ).fetchall()
        return {(r["store_id"], r["food_id"]): bool(r["checked"]) for r in rows}

    def set_shop_check(self, plan_start: date, store_id: str, food_id: str, checked: bool) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO shop_check(plan_start,store_id,food_id,checked) VALUES(?,?,?,?) "
                "ON CONFLICT(plan_start,store_id,food_id) DO UPDATE SET checked=excluded.checked",
                (plan_start.isoformat(), store_id, food_id, int(checked)),
            )
            self._conn.commit()

    def clear_shop_checks(self, plan_start: date) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM shop_check WHERE plan_start=?", (plan_start.isoformat(),)
            )
            self._conn.commit()

    # ---- pins -------------------------------------------------------------

    def pins(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT slot, recipe_id FROM pinned").fetchall()
        return {r["slot"]: r["recipe_id"] for r in rows}

    def set_pin(self, slot: str, recipe_id: str | None) -> None:
        with self._lock:
            if recipe_id:
                self._conn.execute(
                    "INSERT INTO pinned(slot,recipe_id) VALUES(?,?) "
                    "ON CONFLICT(slot) DO UPDATE SET recipe_id=excluded.recipe_id",
                    (slot, recipe_id),
                )
            else:
                self._conn.execute("DELETE FROM pinned WHERE slot=?", (slot,))
            self._conn.commit()

    def clear_pins_before(self, day: date) -> int:
        """Pins are slot keys prefixed with a date, so old ones are dead weight."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM pinned WHERE slot < ?", (day.isoformat(),))
            self._conn.commit()
            return cur.rowcount

    # ---- stats ------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        with self._lock:
            counts = {
                t: self._conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                for t in ("metric_daily", "intake", "plan", "pantry", "target_history")
            }
        return {
            "path": str(self.path),
            "size_kb": round(self.path.stat().st_size / 1024, 1) if self.path.exists() else 0,
            "rows": counts,
            "metrics": self.metric_keys(),
        }
