"""Push PLATE's numbers back into Home Assistant as entities.

Two transports, and the difference matters:

**REST** (``POST /api/states/sensor.plate_x``) works with zero configuration but
creates entities that no integration owns. Home Assistant forgets them on
restart, and they never appear in long-term statistics or the energy dashboard.
Fine for a dashboard card you look at now; useless for a history graph. This
module therefore re-publishes on a timer so the gap after a restart is minutes,
not forever.

**MQTT discovery** requires a broker but produces real, durable entities with
proper device grouping, units and state classes — so they get recorded, graphed
and can drive automations reliably. If you already run Mosquitto, turn it on.

Both run at once when configured, deliberately: the REST copy keeps working if
the broker is down, and the entity ids differ (``sensor.plate_*`` in both cases,
but MQTT claims them first and REST then updates the same ids harmlessly).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .ha import HAClient

log = logging.getLogger(__name__)

DEVICE = {
    "identifiers": ["plate_menu_planner"],
    "name": "PLATE",
    "manufacturer": "PLATE",
    "model": "Adaptive menu planner",
}

DISCOVERY_PREFIX = "homeassistant"
STATE_PREFIX = "plate"


@dataclass(frozen=True, slots=True)
class Reading:
    """One value to publish."""

    slug: str                      # becomes sensor.plate_<slug>
    name: str
    value: float | str | None
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = "measurement"
    icon: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    domain: str = "sensor"

    @property
    def entity_id(self) -> str:
        return f"{self.domain}.plate_{self.slug}"

    @property
    def unique_id(self) -> str:
        return f"plate_{self.slug}"

    @property
    def state_topic(self) -> str:
        return f"{STATE_PREFIX}/{self.domain}/{self.slug}/state"

    @property
    def attrs_topic(self) -> str:
        return f"{STATE_PREFIX}/{self.domain}/{self.slug}/attributes"

    @property
    def config_topic(self) -> str:
        return f"{DISCOVERY_PREFIX}/{self.domain}/plate/{self.slug}/config"

    def state_str(self) -> str:
        if self.value is None:
            return "unknown"
        if isinstance(self.value, float):
            return f"{self.value:.2f}".rstrip("0").rstrip(".")
        return str(self.value)

    def discovery_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "unique_id": self.unique_id,
            "object_id": self.unique_id,
            "state_topic": self.state_topic,
            "json_attributes_topic": self.attrs_topic,
            "device": DEVICE,
            "availability_topic": f"{STATE_PREFIX}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        if self.unit:
            payload["unit_of_measurement"] = self.unit
        if self.device_class:
            payload["device_class"] = self.device_class
        if self.state_class and self.domain == "sensor":
            payload["state_class"] = self.state_class
        if self.icon:
            payload["icon"] = self.icon
        if self.domain == "binary_sensor":
            payload["payload_on"] = "on"
            payload["payload_off"] = "off"
            payload.pop("state_class", None)
        return payload


# --------------------------------------------------------------------------
# building the reading set
# --------------------------------------------------------------------------


def build_readings(snapshot: Mapping[str, Any]) -> list[Reading]:
    """Translate a ``/api/today`` style payload into publishable readings.

    Takes the already-computed dashboard dict rather than recomputing anything,
    so what Home Assistant reports and what the app shows cannot drift apart.
    """
    t = snapshot.get("targets") or {}
    eaten = snapshot.get("eaten") or {}
    energy = snapshot.get("energy") or {}
    trend = snapshot.get("trend") or {}
    bp = snapshot.get("bp") or {}
    plan = snapshot.get("plan") or {}
    shopping = snapshot.get("shopping") or {}

    target_kcal = _num(energy.get("target_kcal"))
    eaten_kcal = _num(eaten.get("kcal")) or 0.0
    protein_target = _num((t.get("protein_g") or {}).get("goal"))
    protein_eaten = _num(eaten.get("protein_g")) or 0.0

    readings: list[Reading] = [
        Reading("calorie_target", "Calorie target", target_kcal, "kcal",
                icon="mdi:target", state_class="measurement"),
        Reading("calories_eaten", "Calories eaten", round(eaten_kcal), "kcal",
                icon="mdi:silverware-fork-knife"),
        Reading("calories_remaining", "Calories remaining",
                round(target_kcal - eaten_kcal) if target_kcal else None, "kcal",
                icon="mdi:fire"),
        Reading("protein_remaining", "Protein remaining",
                round(max(0.0, protein_target - protein_eaten)) if protein_target else None,
                "g", icon="mdi:food-steak"),
        Reading("protein_eaten", "Protein eaten", round(protein_eaten), "g",
                icon="mdi:food-drumstick"),
        Reading("sodium_today", "Sodium today", round(_num(eaten.get("sodium_mg")) or 0), "mg",
                icon="mdi:shaker-outline"),
        Reading("fiber_today", "Fibre today", round(_num(eaten.get("fiber_g")) or 0), "g",
                icon="mdi:leaf"),
        Reading("potassium_today", "Potassium today",
                round(_num(eaten.get("potassium_mg")) or 0), "mg", icon="mdi:atom-variant"),
        Reading("weight_trend", "Weight trend", _round(trend.get("trend_lb"), 1), "lb",
                device_class="weight", icon="mdi:scale-bathroom"),
        Reading("weekly_rate", "Weekly rate", _round(trend.get("rate_lb_per_week"), 2), "lb",
                icon="mdi:trending-down"),
        Reading("tdee", "Calibrated TDEE", _round(energy.get("tdee"), 0), "kcal",
                icon="mdi:calculator-variant"),
        Reading("tracker_bias", "Tracker bias", _round(energy.get("tracker_bias_pct"), 1), "%",
                icon="mdi:watch-variant"),
        Reading("calibration_confidence", "Calibration confidence",
                _round((energy.get("confidence") or 0) * 100, 0), "%",
                icon="mdi:check-decagram"),
        Reading("adherence_7d", "Adherence (7 day)",
                _round((snapshot.get("adherence") or 0) * 100, 0) if snapshot.get("adherence") is not None else None,
                "%", icon="mdi:calendar-check"),
        Reading("dash_score", "DASH score today", _round(plan.get("dash_score"), 0), None,
                icon="mdi:heart-pulse"),
    ]

    if bp.get("systolic") is not None:
        readings += [
            Reading("bp_systolic_avg", "Blood pressure systolic (avg)",
                    _round(bp.get("systolic"), 0), "mmHg", icon="mdi:heart-pulse"),
            Reading("bp_diastolic_avg", "Blood pressure diastolic (avg)",
                    _round(bp.get("diastolic"), 0), "mmHg", icon="mdi:heart-pulse"),
            Reading("bp_category", "Blood pressure category", bp.get("category_label"),
                    state_class=None, icon="mdi:heart-pulse"),
        ]

    next_meal = plan.get("next_meal") or {}
    readings.append(
        Reading(
            "next_meal", "Next meal",
            next_meal.get("title") or "nothing planned",
            state_class=None,
            icon="mdi:silverware-variant",
            attributes={
                "meal": next_meal.get("meal"),
                "kcal": next_meal.get("kcal"),
                "protein_g": next_meal.get("protein_g"),
                "servings": next_meal.get("servings"),
                "vegetarian": next_meal.get("vegetarian"),
                "leftovers": next_meal.get("leftover_from"),
                "active_min": next_meal.get("active_min"),
            },
        )
    )

    readings.append(
        Reading(
            "shopping_items", "Shopping items outstanding",
            shopping.get("outstanding"), None, icon="mdi:cart-outline",
        )
    )
    readings.append(
        Reading(
            "shopping_needed", "Shopping needed",
            "on" if (shopping.get("outstanding") or 0) > 0 else "off",
            domain="binary_sensor", state_class=None, icon="mdi:cart-arrow-down",
        )
    )

    # Attach the plan's caveats to the target sensor so an automation can react
    # to "the plan couldn't hit its constraints" without scraping the UI.
    issues = (plan.get("issues") or [])[:6]
    if issues:
        readings[0] = Reading(
            "calorie_target", "Calorie target", target_kcal, "kcal",
            icon="mdi:target", attributes={"plan_issues": issues},
        )

    return readings


# --------------------------------------------------------------------------
# REST publishing
# --------------------------------------------------------------------------


async def publish_rest(ha: HAClient, readings: Sequence[Reading]) -> dict[str, int]:
    ok = fail = 0
    for r in readings:
        attrs: dict[str, Any] = {
            "friendly_name": f"PLATE {r.name}",
            **{k: v for k, v in r.attributes.items() if v is not None},
        }
        if r.unit:
            attrs["unit_of_measurement"] = r.unit
        if r.device_class:
            attrs["device_class"] = r.device_class
        if r.state_class and r.domain == "sensor":
            attrs["state_class"] = r.state_class
        if r.icon:
            attrs["icon"] = r.icon

        if await ha.set_state(r.entity_id, r.state_str(), attrs):
            ok += 1
        else:
            fail += 1
    log.debug("published %d states to HA (%d failed)", ok, fail)
    return {"published": ok, "failed": fail}


# --------------------------------------------------------------------------
# MQTT publishing
# --------------------------------------------------------------------------


class MqttPublisher:
    """Optional durable transport via MQTT discovery.

    Connects lazily and tolerates the broker being absent — a missing broker
    should degrade the integration, not take the app down.
    """

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str = "",
        password: str = "",
        client_id: str = "plate",
    ) -> None:
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.client_id = client_id
        self._client: Any = None
        self._announced: set[str] = set()
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None and getattr(self._client, "is_connected", lambda: False)()

    def connect(self) -> bool:
        if self.connected:
            return True
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.last_error = "paho-mqtt not installed"
            return False
        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id
            )
            if self.username:
                client.username_pw_set(self.username, self.password)
            # Last will, so entities go unavailable if the add-on dies rather
            # than sitting there showing a stale calorie target forever.
            client.will_set(f"{STATE_PREFIX}/status", "offline", retain=True)
            client.connect(self.host, self.port, keepalive=60)
            client.loop_start()
            client.publish(f"{STATE_PREFIX}/status", "online", retain=True)
            self._client = client
            self.last_error = None
            log.info("MQTT connected to %s:%d", self.host, self.port)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("MQTT connect failed: %s", exc)
            self._client = None
            return False

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.publish(f"{STATE_PREFIX}/status", "offline", retain=True)
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

    def publish(self, readings: Sequence[Reading]) -> dict[str, Any]:
        if not self.connect():
            return {"published": 0, "error": self.last_error}
        published = 0
        for r in readings:
            try:
                # Discovery config is retained and only sent once per process;
                # re-sending on every cycle is legal but spams the broker's log.
                if r.unique_id not in self._announced:
                    self._client.publish(
                        r.config_topic, json.dumps(r.discovery_payload()), retain=True
                    )
                    self._announced.add(r.unique_id)
                self._client.publish(r.state_topic, r.state_str(), retain=True)
                if r.attributes:
                    self._client.publish(
                        r.attrs_topic,
                        json.dumps({k: v for k, v in r.attributes.items() if v is not None}),
                        retain=True,
                    )
                published += 1
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("MQTT publish failed for %s: %s", r.slug, exc)
        return {"published": published, "error": self.last_error}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _round(v: Any, places: int) -> float | None:
    n = _num(v)
    if n is None:
        return None
    return round(n, places) if places else round(n)
