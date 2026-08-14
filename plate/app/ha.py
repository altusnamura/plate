"""Home Assistant client.

Running as an add-on with ``homeassistant_api: true`` gets us a proxy at
``http://supervisor/core/api`` and a ``SUPERVISOR_TOKEN`` in the environment, so
there is no host, port or long-lived token to configure. If those are absent the
client falls back to ``HA_URL``/``HA_TOKEN``, which is what makes the same code
runnable outside the add-on for development.

**Why this file bothers with WebSockets.** The obvious way to read weight history
is the REST history endpoint, and that is a trap: the recorder's default
``purge_keep_days`` is 10, so REST history simply cannot see far enough back to
calibrate a TDEE (which wants 28 days) or fit a weight trend. Long-term
statistics are kept indefinitely for any sensor with a ``state_class``, but they
are only reachable over the WebSocket API. So this client speaks both: statistics
first, REST history as a fallback for whatever the recorder still holds, and the
local SQLite cache accumulates everything so the problem shrinks over time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

import httpx

log = logging.getLogger(__name__)

SUPERVISOR_CORE = "http://supervisor/core"
SUPERVISOR_WS = "ws://supervisor/core/websocket"


class HAError(Exception):
    """Home Assistant was unreachable or refused the request."""


@dataclass(frozen=True, slots=True)
class EntityInfo:
    entity_id: str
    state: str
    unit: str | None
    device_class: str | None
    state_class: str | None
    friendly_name: str
    last_changed: str | None = None

    @property
    def numeric(self) -> float | None:
        try:
            return float(self.state)
        except (TypeError, ValueError):
            return None


class HAClient:
    """Thin async wrapper over the bits of the HA API this app needs."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        ws_url: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
        if supervisor_token:
            self.base_url = (base_url or SUPERVISOR_CORE).rstrip("/")
            self.token = supervisor_token
            self.ws_url = ws_url or SUPERVISOR_WS
            self.mode = "supervisor"
        else:
            url = (base_url or os.environ.get("HA_URL") or "http://homeassistant:8123").rstrip("/")
            self.base_url = url
            self.token = token or os.environ.get("HA_TOKEN") or ""
            scheme = "wss" if url.startswith("https") else "ws"
            host = url.split("://", 1)[-1]
            self.ws_url = ws_url or f"{scheme}://{host}/api/websocket"
            self.mode = "token"

        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    # ---- plumbing ---------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"{self.base_url}/api",
                headers=self._headers(),
                timeout=self._timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, **params: Any) -> Any:
        client = await self._http()
        try:
            r = await client.get(path, params={k: v for k, v in params.items() if v is not None})
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            raise HAError(f"GET {path} failed: {exc}") from exc

    # ---- states -----------------------------------------------------------

    async def ping(self) -> bool:
        try:
            await self._get("/")
            return True
        except HAError:
            return False

    async def states(self) -> list[EntityInfo]:
        raw = await self._get("/states")
        out: list[EntityInfo] = []
        for item in raw or []:
            attrs = item.get("attributes") or {}
            out.append(
                EntityInfo(
                    entity_id=item.get("entity_id", ""),
                    state=item.get("state", ""),
                    unit=attrs.get("unit_of_measurement"),
                    device_class=attrs.get("device_class"),
                    state_class=attrs.get("state_class"),
                    friendly_name=attrs.get("friendly_name") or item.get("entity_id", ""),
                    last_changed=item.get("last_changed"),
                )
            )
        return out

    async def state(self, entity_id: str) -> EntityInfo | None:
        try:
            item = await self._get(f"/states/{entity_id}")
        except HAError:
            return None
        if not item:
            return None
        attrs = item.get("attributes") or {}
        return EntityInfo(
            entity_id=item.get("entity_id", entity_id),
            state=item.get("state", ""),
            unit=attrs.get("unit_of_measurement"),
            device_class=attrs.get("device_class"),
            state_class=attrs.get("state_class"),
            friendly_name=attrs.get("friendly_name") or entity_id,
            last_changed=item.get("last_changed"),
        )

    async def set_state(
        self,
        entity_id: str,
        state: str | float,
        attributes: Mapping[str, Any] | None = None,
    ) -> bool:
        """Write a state into HA.

        Caveat worth knowing: states created this way are not backed by an
        integration, so they vanish when Home Assistant restarts until this app
        writes them again. That is why :mod:`app.publish` re-pushes on a timer and
        why MQTT discovery is offered as the durable alternative.
        """
        client = await self._http()
        body: dict[str, Any] = {"state": str(state)}
        if attributes:
            body["attributes"] = dict(attributes)
        try:
            r = await client.post(f"/states/{entity_id}", json=body)
            r.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.warning("could not write %s: %s", entity_id, exc)
            return False

    # ---- history (recent, recorder-limited) -------------------------------

    async def history(
        self,
        entity_id: str,
        start: datetime,
        end: datetime | None = None,
    ) -> list[tuple[datetime, float]]:
        """Raw state changes for one entity, as (timestamp, value) pairs.

        Only reaches back as far as ``recorder.purge_keep_days``, which defaults
        to 10. Use :meth:`statistics` for anything older.
        """
        path = f"/history/period/{start.astimezone(timezone.utc).isoformat()}"
        raw = await self._get(
            path,
            filter_entity_id=entity_id,
            end_time=end.astimezone(timezone.utc).isoformat() if end else None,
            minimal_response="",
            significant_changes_only="",
        )
        out: list[tuple[datetime, float]] = []
        for series in raw or []:
            for point in series:
                value = _to_float(point.get("state"))
                ts = _parse_ts(point.get("last_changed") or point.get("last_updated"))
                if value is not None and ts is not None:
                    out.append((ts, value))
        out.sort()
        return out

    # ---- long-term statistics (the good stuff) ----------------------------

    async def statistics(
        self,
        entity_ids: Sequence[str],
        start: datetime,
        end: datetime | None = None,
        period: str = "day",
    ) -> dict[str, list[dict[str, Any]]]:
        """Long-term statistics for ``entity_ids``, one bucket per ``period``.

        Returns entity_id -> list of buckets, each with ``start``, and whichever
        of ``mean``/``min``/``max``/``sum``/``state`` the recorder kept. Empty
        when the WebSocket API is unavailable, so callers must degrade to
        :meth:`history`.
        """
        try:
            import websockets
        except ImportError:  # pragma: no cover - dependency is declared
            log.warning("websockets not installed; long-term statistics unavailable")
            return {}

        payload = {
            "type": "recorder/statistics_during_period",
            "start_time": start.astimezone(timezone.utc).isoformat(),
            "statistic_ids": list(entity_ids),
            "period": period,
        }
        if end:
            payload["end_time"] = end.astimezone(timezone.utc).isoformat()

        try:
            async with websockets.connect(self.ws_url, max_size=8 * 1024 * 1024) as ws:
                # Handshake: auth_required -> auth -> auth_ok, then commands.
                hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if hello.get("type") == "auth_required":
                    await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
                    ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    if ack.get("type") != "auth_ok":
                        raise HAError(f"WebSocket auth rejected: {ack}")

                payload["id"] = 1
                await ws.send(json.dumps(payload))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                    if msg.get("id") != 1:
                        continue
                    if not msg.get("success", False):
                        raise HAError(f"statistics request failed: {msg.get('error')}")
                    return msg.get("result") or {}
        except HAError:
            raise
        except Exception as exc:  # network, timeout, protocol
            log.warning("long-term statistics unavailable (%s); falling back to history", exc)
            return {}


# --------------------------------------------------------------------------
# entity discovery
# --------------------------------------------------------------------------

# Ordered candidate rules. Each is (metric key, predicate). First match per
# metric wins, and higher-confidence rules come first, so an entity with the
# right device_class beats one that merely has a suggestive name.
_WEIGHT_WORDS = ("weight", "gewicht", "peso", "masse")
_BODYFAT_WORDS = ("body_fat", "bodyfat", "fat_percentage", "fat_pct")
_CAL_WORDS = ("calories", "calorie", "energy_burned", "active_energy", "kcal")
_STEP_WORDS = ("steps", "step_count", "pedometer")
_RHR_WORDS = ("resting_heart", "resting_hr", "rhr")
_SLEEP_WORDS = ("sleep_minutes", "sleep_duration", "time_asleep", "asleep")
_SYS_WORDS = ("systolic", "bp_sys", "blood_pressure_sys")
_DIA_WORDS = ("diastolic", "bp_dia", "blood_pressure_dia")


def _name_of(e: EntityInfo) -> str:
    return f"{e.entity_id} {e.friendly_name}".lower().replace(" ", "_")


def discover_entities(entities: Iterable[EntityInfo]) -> dict[str, list[EntityInfo]]:
    """Suggest entities for each metric, best guess first.

    Returns every plausible candidate rather than picking one, because guessing
    silently is how you end up calibrating a metabolism against the cat's litter
    tray sensor. The Settings screen shows these as a dropdown for the user to
    confirm.
    """
    pool = [e for e in entities if e.entity_id.startswith(("sensor.", "number."))]

    def find(words: Sequence[str], units: Sequence[str] = (), dev_class: str | None = None):
        strong: list[EntityInfo] = []
        weak: list[EntityInfo] = []
        for e in pool:
            name = _name_of(e)
            if not any(w in name for w in words):
                continue
            if e.numeric is None:
                continue
            unit_ok = not units or (e.unit or "").lower() in units
            class_ok = dev_class is None or e.device_class == dev_class
            (strong if (unit_ok and class_ok) else weak).append(e)
        return strong + weak

    return {
        "weight": find(_WEIGHT_WORDS, units=("lb", "lbs", "kg", "st"), dev_class="weight"),
        "body_fat": find(_BODYFAT_WORDS, units=("%",)),
        "calories_burned": find(_CAL_WORDS, units=("kcal", "cal")),
        "steps": find(_STEP_WORDS),
        "resting_hr": find(_RHR_WORDS, units=("bpm",)),
        "sleep_minutes": find(_SLEEP_WORDS, units=("min", "minutes", "h", "hours")),
        "bp_systolic": find(_SYS_WORDS, units=("mmhg",)),
        "bp_diastolic": find(_DIA_WORDS, units=("mmhg",)),
    }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _to_float(v: Any) -> float | None:
    if v in (None, "", "unknown", "unavailable", "none"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        # HA emits ISO-8601 with a 'Z' or an offset; fromisoformat handles both
        # on 3.11+, but normalise Z anyway for older behaviour.
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def local_day(ts: datetime, tz_offset_minutes: int = 0) -> date:
    """Calendar day a timestamp belongs to, in the user's local zone.

    Days matter here — a weight taken at 23:50 belongs to that day, not the next
    one in UTC — so the offset is applied before the date is taken.
    """
    return (ts.astimezone(timezone.utc) + timedelta(minutes=tz_offset_minutes)).date()
