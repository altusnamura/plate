"""Turn Home Assistant entity history into clean daily series.

Each metric needs a different aggregation and getting these wrong is the classic
way to produce confident nonsense:

* **Weight** — the *last* reading of the day. Stepping on the scale twice while
  fiddling with it shouldn't average a bad reading into the record.
* **Calories burned** — the *maximum*. Fitbit's calorie sensor is cumulative and
  resets at midnight, so the day's total is its high-water mark. Averaging it
  would report roughly half your expenditure, which would then flow straight into
  the TDEE calibration and quietly wreck it.
* **Steps** — the maximum, same reason.
* **Blood pressure, resting heart rate, body fat** — the mean, since these are
  point measurements where averaging genuinely reduces noise.
* **Sleep** — the maximum, as it's usually reported as a running total per night.

Everything read is written to SQLite, because HA's recorder purges detailed
history after ten days by default and this app needs months.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from .ha import HAClient, HAError, local_day
from .store import Store

log = logging.getLogger(__name__)

LB_PER_KG = 2.2046226218


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """How to fetch, aggregate and normalise one metric."""

    key: str                  # storage key in metric_daily
    option: str               # name under the add-on's `entities` option block
    agg: str                  # last | max | mean | min
    label: str
    unit: str
    # Converts (value, source_unit) to this metric's canonical unit.
    convert: Callable[[float, str | None], float] | None = None


def _weight_to_lb(value: float, unit: str | None) -> float:
    u = (unit or "").strip().lower()
    if u == "kg":
        return value * LB_PER_KG
    if u == "st":
        return value * 14.0
    if u == "g":
        return value / 1000.0 * LB_PER_KG
    return value  # already lb, or unlabelled and assumed lb


def _sleep_to_minutes(value: float, unit: str | None) -> float:
    u = (unit or "").strip().lower()
    if u in ("h", "hr", "hrs", "hour", "hours"):
        return value * 60.0
    if u in ("s", "sec", "seconds"):
        return value / 60.0
    return value


SPECS: tuple[MetricSpec, ...] = (
    MetricSpec("weight_lb", "weight", "last", "Weight", "lb", _weight_to_lb),
    MetricSpec("body_fat_pct", "body_fat", "mean", "Body fat", "%"),
    MetricSpec("calories_burned", "calories_burned", "max", "Calories burned", "kcal"),
    MetricSpec("steps", "steps", "max", "Steps", "steps"),
    MetricSpec("resting_hr", "resting_hr", "mean", "Resting heart rate", "bpm"),
    MetricSpec("sleep_minutes", "sleep_minutes", "max", "Sleep", "min", _sleep_to_minutes),
    MetricSpec("bp_systolic", "bp_systolic", "mean", "Systolic", "mmHg"),
    MetricSpec("bp_diastolic", "bp_diastolic", "mean", "Diastolic", "mmHg"),
)

BY_KEY = {s.key: s for s in SPECS}
BY_OPTION = {s.option: s for s in SPECS}


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------


def _aggregate(points: Sequence[tuple[datetime, float]], agg: str, tz_offset: int) -> dict[date, float]:
    """Collapse timestamped readings into one value per local day."""
    buckets: dict[date, list[tuple[datetime, float]]] = {}
    for ts, value in points:
        buckets.setdefault(local_day(ts, tz_offset), []).append((ts, value))

    out: dict[date, float] = {}
    for day, items in buckets.items():
        values = [v for _, v in items]
        if agg == "last":
            out[day] = max(items, key=lambda p: p[0])[1]
        elif agg == "max":
            out[day] = max(values)
        elif agg == "min":
            out[day] = min(values)
        else:
            out[day] = sum(values) / len(values)
    return out


def _from_statistics(buckets: Sequence[Mapping[str, Any]], agg: str) -> dict[date, float]:
    """Read a daily-period statistics response.

    Which fields the recorder kept depends on the sensor's ``state_class``:
    ``measurement`` yields mean/min/max, ``total``/``total_increasing`` yield
    sum/state. So each aggregation has an ordered preference list and falls
    through to whatever is actually present.
    """
    prefer = {
        "last": ("state", "max", "mean"),
        "max": ("max", "state", "sum", "mean"),
        "min": ("min", "mean", "state"),
        "mean": ("mean", "state", "max"),
    }[agg]

    out: dict[date, float] = {}
    for b in buckets:
        start = b.get("start")
        ts: datetime | None = None
        if isinstance(start, (int, float)):
            # Newer HA sends epoch milliseconds.
            ts = datetime.fromtimestamp(start / 1000.0, tz=timezone.utc)
        elif isinstance(start, str):
            try:
                ts = datetime.fromisoformat(start.replace("Z", "+00:00"))
            except ValueError:
                ts = None
        if ts is None:
            continue
        for field in prefer:
            v = b.get(field)
            if v is not None:
                try:
                    out[ts.date()] = float(v)
                except (TypeError, ValueError):
                    pass
                break
    return out


# --------------------------------------------------------------------------
# sync
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SyncReport:
    written: Mapping[str, int]
    skipped: Mapping[str, str]
    used_statistics: tuple[str, ...]
    used_history: tuple[str, ...]
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "written": dict(self.written),
            "skipped": dict(self.skipped),
            "used_statistics": list(self.used_statistics),
            "used_history": list(self.used_history),
            "errors": list(self.errors),
        }


async def sync_metrics(
    ha: HAClient,
    store: Store,
    entity_map: Mapping[str, str],
    days: int = 120,
    tz_offset_minutes: int = 0,
) -> SyncReport:
    """Pull configured entities from HA into the local database.

    Tries long-term statistics first (months of reach), then falls back to the
    recorder's detailed history (about ten days). Both paths write into the same
    daily series, and re-running is safe — days are upserted.
    """
    written: dict[str, int] = {}
    skipped: dict[str, str] = {}
    via_stats: list[str] = []
    via_history: list[str] = []
    errors: list[str] = []

    if not ha.configured:
        return SyncReport({}, {s.key: "Home Assistant not configured" for s in SPECS}, (), (), ())

    start = datetime.now(timezone.utc) - timedelta(days=days)

    configured = {
        spec: entity_map[spec.option]
        for spec in SPECS
        if entity_map.get(spec.option)
    }
    for spec in SPECS:
        if spec not in configured:
            skipped[spec.key] = "no entity configured"

    if not configured:
        return SyncReport(written, skipped, (), (), ())

    # One WebSocket round trip for every entity at once.
    stats: dict[str, list[dict[str, Any]]] = {}
    try:
        stats = await ha.statistics(list(configured.values()), start, period="day")
    except HAError as exc:
        errors.append(f"statistics: {exc}")

    for spec, entity_id in configured.items():
        info = await ha.state(entity_id)
        unit = info.unit if info else None

        series: dict[date, float] = {}
        buckets = stats.get(entity_id) or []
        if buckets:
            series = _from_statistics(buckets, spec.agg)
            if series:
                via_stats.append(spec.key)

        if not series:
            try:
                points = await ha.history(entity_id, start)
            except HAError as exc:
                errors.append(f"{spec.key}: {exc}")
                skipped[spec.key] = str(exc)
                continue
            if points:
                series = _aggregate(points, spec.agg, tz_offset_minutes)
                via_history.append(spec.key)

        if not series:
            skipped[spec.key] = f"no data returned for {entity_id}"
            continue

        if spec.convert:
            series = {d: spec.convert(v, unit) for d, v in series.items()}

        series = {d: v for d, v in series.items() if is_plausible(spec.key, v)}
        # protect_manual: a reading you typed by hand outranks whatever an
        # integration later reports for the same day.
        written[spec.key] = store.put_metrics(
            spec.key, series, source="ha", protect_manual=True
        )

    log.info(
        "metric sync: %s written, %d via statistics, %d via history",
        sum(written.values()), len(via_stats), len(via_history),
    )
    return SyncReport(written, skipped, tuple(via_stats), tuple(via_history), tuple(errors))


# Sanity ranges. A scale that reports 0 during a reboot, or a BP monitor that
# emits 255 on a failed read, would otherwise poison the trend for weeks — and
# unlike a wrong number in a chart, a bad weight silently corrupts the TDEE
# calibration where nobody would think to look for it.
#
# Also applied to hand-typed entries, where a slipped decimal point ("18.5" for a
# weight, "1320" for a systolic) is at least as likely as a sensor glitch.
PLAUSIBLE_RANGES: Mapping[str, tuple[float, float]] = {
    "weight_lb": (50.0, 700.0),
    "body_fat_pct": (2.0, 70.0),
    "calories_burned": (600.0, 10000.0),
    "steps": (0.0, 100000.0),
    "resting_hr": (30.0, 130.0),
    "sleep_minutes": (0.0, 1080.0),
    "bp_systolic": (60.0, 260.0),
    "bp_diastolic": (30.0, 180.0),
}


def is_plausible(key: str, value: float) -> bool:
    lo, hi = PLAUSIBLE_RANGES.get(key, (float("-inf"), float("inf")))
    return lo <= value <= hi


def range_hint(key: str) -> str:
    lo, hi = PLAUSIBLE_RANGES.get(key, (0.0, 0.0))
    return f"{lo:g}–{hi:g}"


# --------------------------------------------------------------------------
# reading back out
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    """Everything the rest of the app needs from the metric history."""

    weight: Mapping[date, float]
    body_fat: Mapping[date, float]
    burn: Mapping[date, float]
    steps: Mapping[date, float]
    resting_hr: Mapping[date, float]
    sleep: Mapping[date, float]
    bp_systolic: Mapping[date, float]
    bp_diastolic: Mapping[date, float]

    def latest(self, series: Mapping[date, float]) -> float | None:
        return series[max(series)] if series else None

    def coverage(self) -> dict[str, int]:
        return {
            "weight": len(self.weight),
            "body_fat": len(self.body_fat),
            "burn": len(self.burn),
            "steps": len(self.steps),
            "resting_hr": len(self.resting_hr),
            "sleep": len(self.sleep),
            "bp": len(self.bp_systolic),
        }


def load_snapshot(store: Store, days: int = 180) -> MetricSnapshot:
    since = date.today() - timedelta(days=days)
    return MetricSnapshot(
        weight=store.metrics("weight_lb", since),
        body_fat=store.metrics("body_fat_pct", since),
        burn=store.metrics("calories_burned", since),
        steps=store.metrics("steps", since),
        resting_hr=store.metrics("resting_hr", since),
        sleep=store.metrics("sleep_minutes", since),
        bp_systolic=store.metrics("bp_systolic", since),
        bp_diastolic=store.metrics("bp_diastolic", since),
    )


def recent_mean(series: Mapping[date, float], days: int = 14, today: date | None = None) -> float | None:
    today = today or date.today()
    cutoff = today - timedelta(days=days)
    vals = [v for d, v in series.items() if d >= cutoff]
    return sum(vals) / len(vals) if vals else None


def complete_days_only(
    series: Mapping[date, float], today: date | None = None
) -> dict[date, float]:
    """Drop today from a cumulative series.

    Today's Fitbit calorie total is a partial figure — at 2pm it reflects two
    thirds of a day — so including it in an average drags the estimate of a
    typical day downward. Anything that averages expenditure wants complete days
    only.
    """
    today = today or date.today()
    return {d: v for d, v in series.items() if d < today}
