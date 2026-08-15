"""Configuration, layered.

Three sources, later winning over earlier:

1. Defaults in this file, so the app runs with nothing configured.
2. ``/data/options.json``, written by Home Assistant from the add-on's options.
3. The ``settings`` table in SQLite, written by the app's own Settings screen.

Layer 3 exists because editing add-on options restarts the container. Changing
your goal weight or picking a different Fitbit entity should not require a
restart, so the UI writes to the database and those values shadow the add-on
options. The add-on options remain the place for things you set once (MQTT
credentials, API keys) and the UI owns the things you actually adjust.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger(__name__)

# Settings-table keys the UI is allowed to override, mapped to their block.
OVERRIDABLE = ("profile", "entities", "diet", "stores")


@dataclass(frozen=True, slots=True)
class Profile:
    sex: str = "male"
    birth_year: int = 1980
    height_in: float = 70.0
    goal: str = "lose"
    target_rate_lb_per_week: float = -1.0
    goal_weight_lb: float = 175.0
    # Used only until real scale data arrives, so the app has something to show.
    assumed_weight_lb: float = 190.0
    activity_passthrough: float = 0.75

    def age(self, today: date | None = None) -> float:
        today = today or date.today()
        return max(14.0, today.year - self.birth_year)


@dataclass(frozen=True, slots=True)
class Entities:
    weight: str = ""
    body_fat: str = ""
    calories_burned: str = ""
    steps: str = ""
    resting_hr: str = ""
    sleep_minutes: str = ""
    bp_systolic: str = ""
    bp_diastolic: str = ""

    def as_map(self) -> dict[str, str]:
        return {
            k: v for k, v in {
                "weight": self.weight,
                "body_fat": self.body_fat,
                "calories_burned": self.calories_burned,
                "steps": self.steps,
                "resting_hr": self.resting_hr,
                "sleep_minutes": self.sleep_minutes,
                "bp_systolic": self.bp_systolic,
                "bp_diastolic": self.bp_diastolic,
            }.items() if v
        }

    @property
    def any_configured(self) -> bool:
        return bool(self.as_map())


@dataclass(frozen=True, slots=True)
class Diet:
    vegetarian_lunch_ratio: float = 0.85
    vegetarian_dinner_ratio: float = 0.4
    exclude_foods: tuple[str, ...] = ()
    dislikes: tuple[str, ...] = ()
    max_weekday_prep_min: int = 35
    meals_per_day: int = 3
    snacks_per_day: int = 1
    plan_days: int = 7
    planner_iterations: int = 4000

    @property
    def excluded(self) -> frozenset[str]:
        """Foods to keep out, from both the explicit list and the dislikes."""
        return frozenset(list(self.exclude_foods) + list(self.dislikes))


@dataclass(frozen=True, slots=True)
class Stores:
    enabled: tuple[str, ...] = ("trader-joes", "safeway", "whole-foods")
    delivery_partner: str = "instacart"


@dataclass(frozen=True, slots=True)
class Mealie:
    url: str = ""
    token: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)


@dataclass(frozen=True, slots=True)
class Mqtt:
    enabled: bool = False
    host: str = "core-mosquitto"
    port: int = 1883
    username: str = ""
    password: str = ""


@dataclass(frozen=True, slots=True)
class Config:
    profile: Profile = field(default_factory=Profile)
    entities: Entities = field(default_factory=Entities)
    diet: Diet = field(default_factory=Diet)
    stores: Stores = field(default_factory=Stores)
    mealie: Mealie = field(default_factory=Mealie)
    mqtt: Mqtt = field(default_factory=Mqtt)
    usda_api_key: str = ""
    units: str = "imperial"
    log_level: str = "info"
    # Minutes east of UTC. HA reports its own zone, but the add-on container is
    # usually UTC, and day boundaries matter for daily metric aggregation.
    tz_offset_minutes: int = 0

    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("PLATE_DATA_DIR", "/data")))
    user_dir: Path = field(default_factory=lambda: Path(os.environ.get("PLATE_USER_DIR", "/config")))
    builtin_data_dir: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / "data"
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "plate.db"

    def public_dict(self) -> dict[str, Any]:
        """Serialisable view with secrets removed.

        Tokens and passwords are reported as booleans so the UI can show
        "configured" without ever receiving the value.
        """
        return {
            "profile": _asdict(self.profile),
            "entities": _asdict(self.entities),
            "diet": {**_asdict(self.diet), "exclude_foods": list(self.diet.exclude_foods),
                     "dislikes": list(self.diet.dislikes)},
            "stores": {"enabled": list(self.stores.enabled),
                       "delivery_partner": self.stores.delivery_partner},
            "mealie": {"url": self.mealie.url, "token_set": bool(self.mealie.token)},
            "mqtt": {"enabled": self.mqtt.enabled, "host": self.mqtt.host,
                     "port": self.mqtt.port, "password_set": bool(self.mqtt.password)},
            "usda": {"api_key_set": bool(self.usda_api_key)},
            "units": self.units,
            "tz_offset_minutes": self.tz_offset_minutes,
        }


def _asdict(obj: Any) -> dict[str, Any]:
    return {f: getattr(obj, f) for f in obj.__dataclass_fields__}  # type: ignore[attr-defined]


def _merge(block: Any, overrides: Mapping[str, Any] | None) -> Any:
    """Return a copy of a frozen dataclass with recognised keys replaced.

    Unknown keys are ignored rather than raising: the settings table can outlive
    a field being renamed, and a stale key shouldn't stop the app booting.
    """
    if not overrides:
        return block
    fields = block.__dataclass_fields__  # type: ignore[attr-defined]
    clean: dict[str, Any] = {}
    for key, value in overrides.items():
        if key not in fields or value is None:
            continue
        current = getattr(block, key)
        try:
            if isinstance(current, tuple):
                clean[key] = tuple(value)
            elif isinstance(current, bool):
                clean[key] = bool(value)
            elif isinstance(current, int) and not isinstance(current, bool):
                clean[key] = int(value)
            elif isinstance(current, float):
                clean[key] = float(value)
            else:
                clean[key] = type(current)(value) if current is not None else value
        except (TypeError, ValueError):
            log.warning("ignoring unusable config value %s=%r", key, value)
    return replace(block, **clean) if clean else block


def load_options(path: Path | str | None = None) -> dict[str, Any]:
    """Read the add-on options file, tolerating its absence."""
    p = Path(path or os.environ.get("PLATE_OPTIONS_FILE", "/data/options.json"))
    if not p.is_file():
        log.info("no options file at %s; using defaults", p)
        return {}
    try:
        # utf-8-sig, not utf-8: Notepad and PowerShell's Out-File both prepend a
        # byte-order mark, and a strict utf-8 read then fails on character zero.
        # The failure is caught below and the file silently ignored, which is a
        # miserable thing to debug — you edit your options, nothing changes, and
        # the only clue is one line in the log. utf-8-sig reads both forms.
        with p.open("r", encoding="utf-8-sig") as fh:
            return json.load(fh) or {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        log.warning(
            "could not read %s (%s) — falling back to defaults, so any options "
            "set in that file are being ignored", p, exc
        )
        return {}


def build_config(
    options: Mapping[str, Any] | None = None,
    db_settings: Mapping[str, Any] | None = None,
) -> Config:
    """Compose the three layers into one Config."""
    options = options or {}
    db_settings = db_settings or {}

    def block(name: str) -> dict[str, Any]:
        merged = dict(options.get(name) or {})
        merged.update(db_settings.get(name) or {})
        return merged

    cfg = Config(
        profile=_merge(Profile(), block("profile")),
        entities=_merge(Entities(), block("entities")),
        diet=_merge(Diet(), block("diet")),
        stores=_merge(Stores(), block("stores")),
        mealie=_merge(Mealie(), options.get("mealie") or {}),
        mqtt=_merge(Mqtt(), options.get("mqtt") or {}),
        usda_api_key=str((options.get("usda") or {}).get("api_key") or ""),
        units=str(options.get("units") or "imperial"),
        log_level=str(options.get("log_level") or os.environ.get("PLATE_LOG_LEVEL") or "info"),
        tz_offset_minutes=int(db_settings.get("tz_offset_minutes") or 0),
    )

    # A goal rate whose sign contradicts the goal is a config mistake that would
    # otherwise produce a deficit while the user believes they're bulking.
    p = cfg.profile
    rate = p.target_rate_lb_per_week
    if p.goal == "lose" and rate > 0:
        cfg = replace(cfg, profile=replace(p, target_rate_lb_per_week=-rate))
    elif p.goal == "gain" and rate < 0:
        cfg = replace(cfg, profile=replace(p, target_rate_lb_per_week=-rate))
    elif p.goal == "maintain" and rate != 0:
        cfg = replace(cfg, profile=replace(p, target_rate_lb_per_week=0.0))

    return cfg


LOG_LEVELS = {
    "trace": logging.DEBUG, "debug": logging.DEBUG, "info": logging.INFO,
    "notice": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=LOG_LEVELS.get(level.lower(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
