"""The orchestrator: everything the API layer needs, in one place.

Holds the loaded library, the database, the HA client and the config, and knows
the order things have to happen in — metrics before trend, trend before
calibration, calibration before targets, targets before a plan, plan before a
shopping list.

One design rule worth stating: :meth:`Service.snapshot` is the single source of
truth for every number the user or Home Assistant sees. The UI renders it, the
MQTT/REST publisher publishes it, and nothing else recomputes a calorie target
independently. That is what keeps the sensor in your dashboard and the ring in
the app from disagreeing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

from . import metrics as mx
from . import publish as pub
from .config import Config, build_config, load_options
from .engine import energy
from .engine import targets as tg
from .engine.library import DataError, Library, load_library
from .engine.models import Meal
from .engine.nutrients import CORE, Nutrients
from .engine.planner import MenuPlan, PlanRequest, plan_menu
from .engine.shopping import ShoppingList, build_shopping_list
from .ha import HAClient, discover_entities
from .store import Store

log = logging.getLogger(__name__)

# How long a computed snapshot is reused. Short enough to feel live, long enough
# that a phone polling every few seconds doesn't re-run the trend maths.
SNAPSHOT_TTL_SECONDS = 20.0


@dataclass
class Health:
    """What's working and what isn't, for the Settings screen."""

    ha_reachable: bool = False
    ha_mode: str = "unknown"
    entities_configured: int = 0
    metric_days: Mapping[str, int] | None = None
    library_warnings: tuple[str, ...] = ()
    last_sync: str | None = None
    last_publish: str | None = None
    mqtt_connected: bool = False
    mqtt_error: str | None = None
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ha_reachable": self.ha_reachable,
            "ha_mode": self.ha_mode,
            "entities_configured": self.entities_configured,
            "metric_days": dict(self.metric_days or {}),
            "library_warnings": list(self.library_warnings),
            "last_sync": self.last_sync,
            "last_publish": self.last_publish,
            "mqtt_connected": self.mqtt_connected,
            "mqtt_error": self.mqtt_error,
            "errors": list(self.errors),
        }


class Service:
    def __init__(self) -> None:
        self.config: Config = build_config()
        self.store: Store | None = None
        self.library: Library | None = None
        self.ha: HAClient | None = None
        self.mqtt: pub.MqttPublisher | None = None
        self.health = Health()
        self._snapshot: dict[str, Any] | None = None
        self._snapshot_at: datetime | None = None
        self._lock = asyncio.Lock()

    # ---- lifecycle --------------------------------------------------------

    def start(self) -> None:
        options = load_options()
        # The database has to exist before config can be fully composed, because
        # the settings table is the highest-priority layer. So: bootstrap config
        # from options only, open the database, then recompose.
        bootstrap = build_config(options)
        self.store = Store(bootstrap.db_path)
        self.config = build_config(options, self.store.get_settings())

        try:
            self.library = load_library(self.config.builtin_data_dir, self.config.user_dir)
            self.health.library_warnings = self.library.warnings
        except DataError as exc:
            # A broken user overlay must not brick the app. Retry with bundled
            # data only and surface the reason loudly.
            log.error("data error with user overlay: %s", exc)
            self.library = load_library(self.config.builtin_data_dir, None)
            self.health.library_warnings = (
                f"Your files in {self.config.user_dir} were ignored: {exc}",
            ) + self.library.warnings

        self.ha = HAClient()
        self.health.ha_mode = self.ha.mode
        self.health.entities_configured = len(self.config.entities.as_map())

        if self.config.mqtt.enabled:
            self.mqtt = pub.MqttPublisher(
                self.config.mqtt.host, self.config.mqtt.port,
                self.config.mqtt.username, self.config.mqtt.password,
            )

        self._seed_user_dir()
        log.info(
            "PLATE ready: %d foods, %d recipes, HA mode %s",
            len(self.library.foods), len(self.library.recipes), self.ha.mode,
        )

    async def stop(self) -> None:
        if self.ha:
            await self.ha.close()
        if self.mqtt:
            self.mqtt.disconnect()
        if self.store:
            self.store.close()

    def _seed_user_dir(self) -> None:
        """Create the user data folders with a README, once.

        Without this, the "drop your own YAML in /config" story requires the user
        to guess the directory layout.
        """
        base = self.config.user_dir
        try:
            for sub in ("foods", "recipes", "stores"):
                (base / sub).mkdir(parents=True, exist_ok=True)
            readme = base / "README.md"
            if not readme.exists():
                readme.write_text(_USER_README, encoding="utf-8")
        except OSError as exc:
            log.warning("could not prepare %s: %s", base, exc)

    def reload_config(self) -> None:
        assert self.store is not None
        self.config = build_config(load_options(), self.store.get_settings())
        self.health.entities_configured = len(self.config.entities.as_map())
        self._invalidate()

    def reload_library(self) -> tuple[bool, str]:
        try:
            self.library = load_library(self.config.builtin_data_dir, self.config.user_dir)
        except DataError as exc:
            return False, str(exc)
        self.health.library_warnings = self.library.warnings
        self._invalidate()
        return True, f"{len(self.library.foods)} foods, {len(self.library.recipes)} recipes"

    def _invalidate(self) -> None:
        self._snapshot = None
        self._snapshot_at = None

    # ---- metric sync ------------------------------------------------------

    async def sync(self, days: int = 120) -> dict[str, Any]:
        assert self.ha and self.store
        self.health.ha_reachable = await self.ha.ping()
        if not self.health.ha_reachable:
            return {"ok": False, "error": "Home Assistant is not reachable"}

        report = await mx.sync_metrics(
            self.ha, self.store, self.config.entities.as_map(),
            days=days, tz_offset_minutes=self.config.tz_offset_minutes,
        )
        self.health.last_sync = datetime.now().isoformat(timespec="seconds")
        self.health.metric_days = self.store.metric_keys()
        self._invalidate()
        return {"ok": True, **report.as_dict()}

    async def discover(self) -> dict[str, list[dict[str, Any]]]:
        assert self.ha
        try:
            entities = await self.ha.states()
        except Exception as exc:
            log.warning("discovery failed: %s", exc)
            return {}
        found = discover_entities(entities)
        return {
            metric: [
                {
                    "entity_id": e.entity_id,
                    "name": e.friendly_name,
                    "state": e.state,
                    "unit": e.unit,
                    "device_class": e.device_class,
                }
                for e in candidates[:8]
            ]
            for metric, candidates in found.items()
        }

    # ---- the snapshot -----------------------------------------------------

    async def snapshot(self, today: date | None = None, force: bool = False) -> dict[str, Any]:
        today = today or date.today()
        async with self._lock:
            fresh = (
                self._snapshot is not None
                and self._snapshot_at is not None
                and (datetime.now() - self._snapshot_at).total_seconds() < SNAPSHOT_TTL_SECONDS
                and self._snapshot.get("day") == today.isoformat()
            )
            if fresh and not force:
                return self._snapshot  # type: ignore[return-value]
            snap = self._compute_snapshot(today)
            self._snapshot = snap
            self._snapshot_at = datetime.now()
            return snap

    def _compute_snapshot(self, today: date) -> dict[str, Any]:
        assert self.store is not None and self.library is not None
        cfg = self.config
        p = cfg.profile
        snap_metrics = mx.load_snapshot(self.store)

        # ---- weight -----------------------------------------------------
        trend_points = energy.ewma_trend(snap_metrics.weight)
        trend_lb = energy.latest_trend_lb(trend_points)
        rate = energy.trend_rate_lb_per_week(trend_points)
        needs_setup: list[str] = []

        # Standalone means no Home Assistant is reachable, so advice about
        # picking entities is useless — the user needs to be told to type a
        # number in instead.
        standalone = not (self.ha and self.ha.configured)

        if trend_lb is None:
            trend_lb = p.assumed_weight_lb
            needs_setup.append(
                "No weight history yet, so everything below is based on the assumed "
                f"weight of {p.assumed_weight_lb:.0f} lb. "
                + ("Tap Add measurement to enter one." if standalone
                   else "Point PLATE at your scale entity in Settings.")
            )
        body_fat = mx.recent_mean(snap_metrics.body_fat, days=21, today=today)

        # ---- expenditure -------------------------------------------------
        rmr, formula = energy.resting_rate(
            trend_lb, p.height_in, p.age(today), p.sex, body_fat_pct=body_fat
        )
        complete_burn = mx.complete_days_only(snap_metrics.burn, today)
        typical_burn = mx.recent_mean(complete_burn, days=21, today=today)
        fallback_tdee = typical_burn or rmr * energy.activity_factor_from_steps(
            mx.recent_mean(snap_metrics.steps, days=21, today=today)
        )

        intake_by_day = self.store.intake_by_day(since=today - timedelta(days=90))
        calibration = energy.calibrate(
            trend_points, intake_by_day, complete_burn,
            fallback_tdee=fallback_tdee, today=today,
        )
        if not snap_metrics.burn:
            needs_setup.append(
                "No tracker expenditure data, so targets use a formula estimate. "
                + ("That's fine — once there are a few weeks of weights and food "
                   "logs, the calibration corrects the formula against your actual "
                   "rate of change anyway."
                   if standalone
                   else "Configure a Fitbit calories entity to improve it.")
            )

        target = energy.calorie_target(
            calibration,
            weight_lb=trend_lb,
            sex=p.sex,
            goal=p.goal,
            target_rate_lb_per_week=p.target_rate_lb_per_week,
            resting_kcal=rmr,
            today_burn=snap_metrics.burn.get(today),
            typical_burn=typical_burn,
            activity_passthrough=p.activity_passthrough,
        )

        # ---- blood pressure and nutrient targets -------------------------
        bp = tg.summarise_bp(
            snap_metrics.bp_systolic, snap_metrics.bp_diastolic, window_days=14, today=today
        )
        nutrition = tg.build_targets(
            kcal=target.base_kcal, weight_lb=trend_lb, goal=p.goal, bp=bp,
            body_fat_pct=body_fat, sex=p.sex,
        )

        self.store.put_target(
            today, target.kcal, calibration.tdee, calibration.confidence,
            {"deficit": target.deficit_kcal, "source": calibration.source},
        )

        # ---- plan --------------------------------------------------------
        plan_payload = self.store.plan_covering(today)
        if plan_payload is None:
            plan = self.build_plan(week_start(today), nutrition, target.base_kcal)
            plan_payload = plan.as_dict()

        today_plan = next(
            (d for d in plan_payload.get("days", []) if d.get("day") == today.isoformat()),
            None,
        )

        # ---- intake ------------------------------------------------------
        eaten_raw = self.store.intake_nutrients(today)
        eaten = {k: round(eaten_raw.get(k, 0.0), 1) for k in CORE}
        logged_slots = self.store.logged_slots([today])

        remaining = max(0.0, target.kcal - eaten.get("kcal", 0.0))
        adherence = energy.adherence(
            intake_by_day, self.store.targets_by_day(since=today - timedelta(days=30)),
            days=7, today=today,
        )

        # ---- shopping ----------------------------------------------------
        plan_start = date.fromisoformat(plan_payload["start"])
        checks = self.store.shop_checks(plan_start)
        outstanding = None
        shopping_total = None
        try:
            sl = self.shopping_list(plan_payload)
            outstanding = sum(
                1
                for s in sl.stores
                for line in s.lines
                if not checks.get((s.store.id, line.food_id), False)
            )
            shopping_total = sl.total_estimate
        except Exception as exc:  # a shopping failure must not blank the dashboard
            log.warning("shopping list failed: %s", exc)

        return {
            "day": today.isoformat(),
            "generated": datetime.now().isoformat(timespec="seconds"),
            "needs_setup": needs_setup,
            # Lets the UI hide the entity pickers and lead with manual entry when
            # there's no Home Assistant behind this.
            "mode": "standalone" if standalone else self.ha.mode,
            "standalone": standalone,
            "profile": {
                "goal": p.goal,
                "goal_weight_lb": p.goal_weight_lb,
                "target_rate_lb_per_week": p.target_rate_lb_per_week,
                "age": p.age(today),
                "sex": p.sex,
            },
            "trend": {
                "trend_lb": round(trend_lb, 1),
                "raw_lb": snap_metrics.latest(snap_metrics.weight),
                "rate_lb_per_week": round(rate, 2) if rate is not None else None,
                "body_fat_pct": round(body_fat, 1) if body_fat else None,
                "readings": len(snap_metrics.weight),
                "goal_date": _iso(
                    energy.projected_goal_date(trend_lb, p.goal_weight_lb, rate, today)
                ),
                "series": [
                    {"day": pt.day.isoformat(), "raw": pt.raw_lb, "trend": round(pt.trend_lb, 2)}
                    for pt in trend_points[-120:]
                ],
            },
            "energy": {
                "target_kcal": round(target.kcal),
                "base_kcal": round(target.base_kcal),
                "tdee": round(calibration.tdee),
                "prior_tdee": round(calibration.prior_tdee),
                "observed_tdee": round(calibration.observed_tdee) if calibration.observed_tdee else None,
                "resting_kcal": round(rmr),
                "resting_formula": formula,
                "deficit_kcal": round(target.deficit_kcal),
                "activity_adjustment": round(target.activity_adjustment),
                "planned_rate": round(target.planned_rate_lb_per_week, 2),
                "confidence": round(calibration.confidence, 2),
                "source": calibration.source,
                "tracker_bias_pct": (
                    round(calibration.tracker_bias_pct, 1)
                    if calibration.tracker_bias_pct is not None else None
                ),
                "logged_days": calibration.logged_days,
                "notes": list(calibration.notes) + list(target.notes),
                "floor_applied": target.floor_applied,
            },
            "bp": {
                "systolic": round(bp.systolic) if bp.systolic else None,
                "diastolic": round(bp.diastolic) if bp.diastolic else None,
                "category": bp.category.value,
                "category_label": bp.category.label,
                "severity": bp.category.severity,
                "readings": bp.readings,
                "trend_systolic": round(bp.trend_systolic, 2) if bp.trend_systolic else None,
                "advice": list(bp.advice),
            },
            "targets": nutrition.evaluate(Nutrients.from_mapping(eaten_raw)) if eaten_raw
                       else nutrition.evaluate(Nutrients.zero()),
            "target_meta": {
                "protein_basis": nutrition.protein_basis,
                "notes": list(nutrition.notes),
                "disclaimers": list(nutrition.disclaimers),
                "rationale": {k: t.rationale for k, t in nutrition.targets.items()},
            },
            "eaten": eaten,
            "remaining_kcal": round(remaining),
            "adherence": adherence,
            "plan": {
                "start": plan_payload["start"],
                "today": today_plan,
                "dash_score": (today_plan or {}).get("dash_score"),
                "issues": (plan_payload.get("diagnostics") or {}).get("issues", []),
                "next_meal": _next_meal(today_plan, logged_slots),
                "logged_slots": sorted(logged_slots),
            },
            "shopping": {
                "outstanding": outstanding,
                "total_estimate": shopping_total,
                "plan_start": plan_payload["start"],
            },
            "coverage": snap_metrics.coverage(),
        }

    # ---- planning ---------------------------------------------------------

    def build_plan(
        self,
        start: date,
        nutrition: tg.NutritionTargets | None = None,
        kcal: float | None = None,
        seed: int | None = None,
        days: int | None = None,
    ) -> MenuPlan:
        """Generate and persist a plan for the week beginning ``start``."""
        assert self.library is not None and self.store is not None
        cfg = self.config

        if nutrition is None or kcal is None:
            # Called directly (e.g. from the API's regenerate button) so derive
            # targets from the current snapshot inputs.
            snap = self._compute_targets_only(start)
            nutrition, kcal = snap

        n_days = days or cfg.diet.plan_days
        day_list = [start + timedelta(days=i) for i in range(n_days)]

        meals: list[Meal] = [Meal.BREAKFAST, Meal.LUNCH, Meal.DINNER][: cfg.diet.meals_per_day]
        if cfg.diet.meals_per_day >= 3 and Meal.DINNER not in meals:
            meals.append(Meal.DINNER)

        pins = {
            slot: rid for slot, rid in self.store.pins().items()
            if slot.split(":", 1)[0] in {d.isoformat() for d in day_list}
        }

        req = PlanRequest(
            start=start,
            days=n_days,
            kcal_by_day={d: kcal for d in day_list},
            targets=nutrition,
            meals=tuple(meals),
            snacks_per_day=cfg.diet.snacks_per_day,
            vegetarian_lunch_ratio=cfg.diet.vegetarian_lunch_ratio,
            vegetarian_dinner_ratio=cfg.diet.vegetarian_dinner_ratio,
            max_weekday_prep_min=cfg.diet.max_weekday_prep_min,
            exclude_foods=cfg.diet.excluded,
            locked=pins,
            seed=seed if seed is not None else _seed_for(start),
            iterations=cfg.diet.planner_iterations,
        )
        plan = plan_menu(self.library, req)
        self.store.put_plan(start, req.seed, plan.as_dict())
        self.store.clear_shop_checks(start)
        self._invalidate()
        log.info("planned week of %s (cost %.1f, seed %d)", start, plan.cost, req.seed)
        return plan

    def _compute_targets_only(self, today: date) -> tuple[tg.NutritionTargets, float]:
        """Targets without the full snapshot, to avoid recursion when planning."""
        assert self.store is not None
        cfg = self.config
        p = cfg.profile
        m = mx.load_snapshot(self.store)

        trend_points = energy.ewma_trend(m.weight)
        weight = energy.latest_trend_lb(trend_points) or p.assumed_weight_lb
        body_fat = mx.recent_mean(m.body_fat, days=21, today=today)
        rmr, _ = energy.resting_rate(weight, p.height_in, p.age(today), p.sex, body_fat)
        complete_burn = mx.complete_days_only(m.burn, today)
        typical = mx.recent_mean(complete_burn, days=21, today=today)
        fallback = typical or rmr * energy.activity_factor_from_steps(
            mx.recent_mean(m.steps, days=21, today=today)
        )
        cal = energy.calibrate(
            trend_points, self.store.intake_by_day(since=today - timedelta(days=90)),
            complete_burn, fallback_tdee=fallback, today=today,
        )
        target = energy.calorie_target(
            cal, weight_lb=weight, sex=p.sex, goal=p.goal,
            target_rate_lb_per_week=p.target_rate_lb_per_week, resting_kcal=rmr,
        )
        bp = tg.summarise_bp(m.bp_systolic, m.bp_diastolic, today=today)
        nutrition = tg.build_targets(
            kcal=target.base_kcal, weight_lb=weight, goal=p.goal, bp=bp,
            body_fat_pct=body_fat, sex=p.sex,
        )
        return nutrition, target.base_kcal

    def plan_for(self, start: date) -> dict[str, Any]:
        assert self.store is not None
        existing = self.store.get_plan(start)
        if existing:
            return existing
        return self.build_plan(start).as_dict()

    # ---- shopping ---------------------------------------------------------

    def shopping_list(self, plan_payload: Mapping[str, Any]) -> ShoppingList:
        assert self.library is not None and self.store is not None
        grams: dict[str, float] = {}
        used_by: dict[str, list[str]] = {}

        # Rebuild the ingredient totals from the stored plan. Cooked quantities,
        # not eaten ones — a four-serving recipe uses four servings of onion even
        # if only three get eaten.
        for batch in plan_payload.get("batches") or []:
            recipe = self.library.recipes.get(batch["recipe_id"])
            if recipe is None:
                continue
            for fid, g in recipe.scaled_grams(batch["servings_cooked"]).items():
                grams[fid] = grams.get(fid, 0.0) + g
                used_by.setdefault(fid, [])
                if recipe.title not in used_by[fid]:
                    used_by[fid].append(recipe.title)

        # Batches only lists multi-slot cooks, so single-slot meals need adding.
        batched_slots = {
            slot for b in (plan_payload.get("batches") or []) for slot in b.get("feeds_slots", [])
        }
        for day in plan_payload.get("days") or []:
            for meal in day.get("meals") or []:
                if meal["slot"] in batched_slots:
                    continue
                recipe = self.library.recipes.get((meal.get("recipe") or {}).get("id"))
                if recipe is None:
                    continue
                cooked = _cook_yield(recipe.servings, meal.get("servings", 1.0))
                for fid, g in recipe.scaled_grams(cooked).items():
                    grams[fid] = grams.get(fid, 0.0) + g
                    used_by.setdefault(fid, [])
                    if recipe.title not in used_by[fid]:
                        used_by[fid].append(recipe.title)

        return build_shopping_list(
            self.library,
            grams,
            enabled_stores=list(self.config.stores.enabled),
            pantry=self.store.pantry(),
            delivery_partner=self.config.stores.delivery_partner,
            used_by=used_by,
        )

    # ---- publishing -------------------------------------------------------

    async def publish(self, snapshot: Mapping[str, Any] | None = None) -> dict[str, Any]:
        assert self.ha is not None
        snap = dict(snapshot or await self.snapshot())
        readings = pub.build_readings(snap)

        result: dict[str, Any] = {"readings": len(readings)}
        if self.ha.configured:
            result["rest"] = await pub.publish_rest(self.ha, readings)
        if self.mqtt is not None:
            result["mqtt"] = self.mqtt.publish(readings)
            self.health.mqtt_connected = self.mqtt.connected
            self.health.mqtt_error = self.mqtt.last_error
        self.health.last_publish = datetime.now().isoformat(timespec="seconds")
        return result

    # ---- background loop --------------------------------------------------

    async def background_loop(self, interval_seconds: int = 900) -> None:
        """Sync, replan when the week turns, and publish, forever.

        Exceptions are caught and logged rather than allowed to kill the task,
        because a transient HA outage should not silently stop all updates for the
        rest of the container's life.
        """
        await asyncio.sleep(5)  # let the server bind first
        while True:
            try:
                if self.config.entities.any_configured:
                    await self.sync(days=120)
                snap = await self.snapshot(force=True)
                await self.publish(snap)
                self._housekeeping()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("background cycle failed; will retry")
            await asyncio.sleep(interval_seconds)

    def _housekeeping(self) -> None:
        assert self.store is not None
        today = date.today()
        # Roll the plan forward once the current week is spent.
        if self.store.plan_covering(today) is None:
            try:
                self.build_plan(week_start(today))
            except Exception:
                log.exception("could not auto-generate this week's plan")
        if today.day == 1:
            self.store.prune_plans()
            self.store.clear_pins_before(today - timedelta(days=14))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def week_start(day: date) -> date:
    """Monday of the week containing ``day``.

    Weeks start Monday because shopping and batch cooking usually happen at the
    weekend for the week ahead.
    """
    return day - timedelta(days=day.weekday())


def _seed_for(start: date) -> int:
    """Stable per-week seed, so re-requesting a week returns the same plan."""
    return start.toordinal()


def _cook_yield(recipe_servings: float, needed: float) -> float:
    from .engine.planner import cook_yield
    return cook_yield(recipe_servings, needed)


def _next_meal(
    today_plan: Mapping[str, Any] | None, logged: set[str]
) -> dict[str, Any] | None:
    """The next unlogged meal today, in meal order."""
    if not today_plan:
        return None
    order = {"breakfast": 0, "lunch": 1, "snack": 2, "dinner": 3}
    candidates = sorted(
        (m for m in today_plan.get("meals", []) if m["slot"] not in logged),
        key=lambda m: order.get(m.get("meal", ""), 9),
    )
    if not candidates:
        return None
    m = candidates[0]
    recipe = m.get("recipe") or {}
    return {
        "slot": m["slot"],
        "meal": m.get("meal"),
        "title": recipe.get("title"),
        "recipe_id": recipe.get("id"),
        "kcal": m.get("kcal"),
        "protein_g": m.get("protein_g"),
        "servings": m.get("servings"),
        "vegetarian": m.get("vegetarian"),
        "leftover_from": m.get("leftover_from"),
        "active_min": m.get("active_min"),
    }


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


_USER_README = """# PLATE user data

Anything you put here overrides or extends the data bundled with the add-on, and
survives add-on updates. Files are matched by `id`: a food or recipe here with the
same id as a built-in one replaces it entirely.

    foods/      *.yaml  - food records (per-100g nutrition, units, aisle)
    recipes/    *.yaml  - recipes (ingredients reference food ids)
    stores/     *.yaml  - store definitions and product/price mappings

Files beginning with `_` are ignored, so `_notes.yaml` is a safe scratch file.

The two highest-value edits you can make:

1. **Prices.** Every price in `stores/` is an estimate typed in by hand. No store
   in this app has an API. Correct them as you shop and the cost estimates become
   genuinely useful.
2. **Aisle order.** Each store's `aisle_order` decides the order your shopping
   list is sorted into. Match it to the actual layout of the store you go to and
   the list becomes a single walk instead of a scavenger hunt.

After editing, hit *Reload data* on the Settings screen. Errors are reported
there — a broken file is ignored rather than taking the app down, so if your
changes seem to have no effect, look there first.
"""
