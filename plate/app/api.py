"""REST API.

Every path is relative to wherever Ingress mounts the app, so nothing here emits
an absolute URL and the frontend only ever calls ``./api/...``. Home Assistant
handles authentication in front of Ingress, which is why there are no tokens or
login routes in this file — but it also means anyone who can reach this port
directly bypasses that, so the container only publishes it to the Supervisor.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .engine.models import Meal
from .engine.nutrients import CORE
from .importers import (
    MealieImporter,
    UsdaBackfill,
    build_index,
    write_food_overlay,
    write_recipe_overlay,
)
from .service import Service, week_start

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def svc(request: Request) -> Service:
    service: Service | None = getattr(request.app.state, "service", None)
    if service is None or service.library is None or service.store is None:
        raise HTTPException(503, "PLATE is still starting up")
    return service


def _parse_day(value: str | None, default: date | None = None) -> date:
    if not value:
        return default or date.today()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"'{value}' is not an ISO date (YYYY-MM-DD)")


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------


@router.get("/today")
async def today(request: Request, day: str | None = None, force: bool = False):
    service = svc(request)
    return await service.snapshot(_parse_day(day), force=force)


@router.post("/refresh")
async def refresh(request: Request):
    """Pull fresh metrics from HA, recompute, republish."""
    service = svc(request)
    sync = await service.sync()
    snapshot = await service.snapshot(force=True)
    published = await service.publish(snapshot)
    return {"sync": sync, "published": published, "snapshot": snapshot}


@router.get("/health")
async def health(request: Request):
    service = svc(request)
    assert service.store is not None
    service.health.metric_days = service.store.metric_keys()
    if service.ha:
        service.health.ha_reachable = await service.ha.ping()
    return {
        "health": service.health.as_dict(),
        "database": service.store.summary(),
        "library": {
            "foods": len(service.library.foods) if service.library else 0,
            "recipes": len(service.library.recipes) if service.library else 0,
            "products": len(service.library.products) if service.library else 0,
            "stores": sorted(service.library.stores) if service.library else [],
        },
        "config": service.config.public_dict(),
    }


# --------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------


@router.get("/week")
async def week(request: Request, start: str | None = None):
    service = svc(request)
    begin = week_start(_parse_day(start))
    return service.plan_for(begin)


class RegenerateBody(BaseModel):
    start: str | None = None
    # A different seed gives a genuinely different week; the same seed reproduces
    # the previous one exactly, which is what makes "shuffle" and "undo" possible.
    seed: int | None = None
    keep_pins: bool = True


@router.post("/week/regenerate")
async def regenerate(request: Request, body: RegenerateBody):
    service = svc(request)
    assert service.store is not None
    begin = week_start(_parse_day(body.start))
    if not body.keep_pins:
        for slot in list(service.store.pins()):
            if slot.startswith(begin.isoformat()[:4]):
                service.store.set_pin(slot, None)
    seed = body.seed if body.seed is not None else begin.toordinal() + _bump(service, begin)
    plan = service.build_plan(begin, seed=seed)
    return plan.as_dict()


def _bump(service: Service, begin: date) -> int:
    """Advance the seed so consecutive shuffles keep producing new weeks."""
    assert service.store is not None
    key = f"shuffle:{begin.isoformat()}"
    n = int(service.store.get_meta(key, "0")) + 1
    service.store.set_meta(key, str(n))
    return n * 1013


class PinBody(BaseModel):
    slot: str
    recipe_id: str | None = None
    regenerate: bool = True


@router.post("/week/pin")
async def pin(request: Request, body: PinBody):
    """Fix (or release) one meal, then replan around it."""
    service = svc(request)
    assert service.store is not None and service.library is not None
    if body.recipe_id and body.recipe_id not in service.library.recipes:
        raise HTTPException(404, f"no recipe '{body.recipe_id}'")
    service.store.set_pin(body.slot, body.recipe_id)

    if not body.regenerate:
        return {"ok": True, "pins": service.store.pins()}
    try:
        begin = week_start(date.fromisoformat(body.slot.split(":", 1)[0]))
    except (ValueError, IndexError):
        raise HTTPException(400, f"slot '{body.slot}' is malformed")
    plan = service.build_plan(begin)
    return {"ok": True, "pins": service.store.pins(), "plan": plan.as_dict()}


# --------------------------------------------------------------------------
# recipes
# --------------------------------------------------------------------------


@router.get("/recipes")
async def recipes(
    request: Request,
    meal: str | None = None,
    q: str | None = None,
    vegetarian: bool | None = None,
    max_min: int | None = None,
    limit: int = Query(200, ge=1, le=500),
):
    service = svc(request)
    assert service.library is not None
    items = list(service.library.recipes.values())

    if meal:
        try:
            want = Meal(meal.lower())
        except ValueError:
            raise HTTPException(400, f"'{meal}' is not a meal")
        items = [r for r in items if r.suits(want)]
    if vegetarian is not None:
        items = [r for r in items if r.vegetarian is vegetarian]
    if max_min is not None:
        items = [r for r in items if r.total_min <= max_min]
    if q:
        needle = q.lower()
        items = [
            r for r in items
            if needle in r.title.lower()
            or needle in r.cuisine
            or any(needle in t for t in r.tags)
        ]

    items.sort(key=lambda r: r.title)
    return {"count": len(items), "recipes": [r.summary() for r in items[:limit]]}


@router.get("/recipes/{recipe_id}")
async def recipe_detail(request: Request, recipe_id: str):
    service = svc(request)
    assert service.library is not None
    recipe = service.library.recipes.get(recipe_id)
    if recipe is None:
        raise HTTPException(404, f"no recipe '{recipe_id}'")

    from .engine.units import humanise

    ingredients = []
    for ing in recipe.ingredients:
        food = service.library.foods[ing.food_id]
        grams = ing.grams(service.library.foods)
        ingredients.append({
            "food_id": food.id,
            "name": food.name,
            "qty": ing.qty,
            "unit": ing.unit,
            "grams": round(grams, 1),
            "display": f"{ing.qty:g} {ing.unit}" if ing.unit != "ea" else f"{ing.qty:g}",
            "weight": humanise(grams, food),
            "prep": ing.prep,
            "optional": ing.optional,
            "aisle": food.aisle,
            "vegetarian": food.vegetarian,
        })

    return {
        **recipe.summary(),
        "ingredients": ingredients,
        "steps": list(recipe.steps),
        "notes": recipe.notes,
        "nutrition_full": recipe.nutrition.as_dict(include_coverage=True),
    }


# --------------------------------------------------------------------------
# logging what you ate
# --------------------------------------------------------------------------


class LogBody(BaseModel):
    day: str | None = None
    slot: str | None = None
    recipe_id: str | None = None
    servings: float = Field(1.0, gt=0, le=10)
    label: str | None = None
    # Free-form entry for anything not in the library.
    nutrients: dict[str, float] | None = None


@router.post("/log")
async def log_intake(request: Request, body: LogBody):
    """Record a meal as eaten.

    Three ways in: a planned slot (the common case, one tap), an arbitrary recipe
    with a serving count, or raw nutrient numbers for a restaurant meal. Logging
    a slot twice replaces the earlier entry rather than double-counting.
    """
    service = svc(request)
    assert service.store is not None and service.library is not None
    day = _parse_day(body.day)

    nutrients: dict[str, float]
    recipe_id = body.recipe_id
    label = body.label

    if body.slot:
        plan = service.store.plan_covering(date.fromisoformat(body.slot.split(":", 1)[0]))
        if plan is None:
            raise HTTPException(404, "no saved plan contains that slot")
        meal = next(
            (m for d in plan["days"] for m in d["meals"] if m["slot"] == body.slot), None
        )
        if meal is None:
            raise HTTPException(404, f"slot '{body.slot}' is not in the plan")
        recipe = service.library.recipes.get(meal["recipe"]["id"])
        if recipe is None:
            raise HTTPException(404, "that slot's recipe is no longer in the library")
        servings = body.servings if body.servings != 1.0 else meal.get("servings", 1.0)
        nutrients = {k: recipe.nutrition.get(k) * servings for k in CORE}
        recipe_id = recipe.id
        label = label or recipe.title
        day = date.fromisoformat(meal["day"])
    elif body.recipe_id:
        recipe = service.library.recipes.get(body.recipe_id)
        if recipe is None:
            raise HTTPException(404, f"no recipe '{body.recipe_id}'")
        nutrients = {k: recipe.nutrition.get(k) * body.servings for k in CORE}
        label = label or recipe.title
        servings = body.servings
    elif body.nutrients:
        nutrients = {k: float(v) for k, v in body.nutrients.items() if k in CORE}
        if "kcal" not in nutrients:
            raise HTTPException(400, "a free-form entry needs at least 'kcal'")
        servings = body.servings
        label = label or "Manual entry"
    else:
        raise HTTPException(400, "provide one of: slot, recipe_id, or nutrients")

    entry_id = service.store.log_intake(
        day, nutrients, source="app", recipe_id=recipe_id,
        slot=body.slot, label=label, servings=servings,
    )
    service._invalidate()
    snapshot = await service.snapshot(force=True)
    await service.publish(snapshot)
    return {"ok": True, "id": entry_id, "snapshot": snapshot}


@router.get("/log")
async def list_intake(request: Request, day: str | None = None):
    service = svc(request)
    assert service.store is not None
    d = _parse_day(day)
    return {
        "day": d.isoformat(),
        "entries": service.store.intake_entries(d),
        "totals": {k: round(v, 1) for k, v in service.store.intake_nutrients(d).items()},
    }


class UnlogBody(BaseModel):
    id: int | None = None
    slot: str | None = None


@router.post("/log/delete")
async def delete_intake(request: Request, body: UnlogBody):
    service = svc(request)
    assert service.store is not None
    removed = service.store.delete_intake(entry_id=body.id, slot=body.slot)
    if not removed:
        raise HTTPException(404, "nothing matched")
    service._invalidate()
    return {"ok": True, "removed": removed, "snapshot": await service.snapshot(force=True)}


# --------------------------------------------------------------------------
# shopping
# --------------------------------------------------------------------------


@router.get("/shopping")
async def shopping(request: Request, start: str | None = None):
    service = svc(request)
    assert service.store is not None
    begin = week_start(_parse_day(start))
    plan = service.plan_for(begin)
    sl = service.shopping_list(plan)
    checks = service.store.shop_checks(begin)

    payload = sl.as_dict()
    for store in payload["stores"]:
        for aisle in store["aisles"]:
            for line in aisle["lines"]:
                line["checked"] = checks.get((store["store_id"], line["food_id"]), False)
    payload["plan_start"] = begin.isoformat()
    payload["pantry"] = service.store.pantry()
    return payload


class CheckBody(BaseModel):
    plan_start: str
    store_id: str
    food_id: str
    checked: bool = True


@router.post("/shopping/check")
async def shopping_check(request: Request, body: CheckBody):
    service = svc(request)
    assert service.store is not None
    service.store.set_shop_check(
        _parse_day(body.plan_start), body.store_id, body.food_id, body.checked
    )
    return {"ok": True}


class PantryBody(BaseModel):
    food_id: str
    grams: float = Field(0.0, ge=0)


@router.post("/pantry")
async def set_pantry(request: Request, body: PantryBody):
    """Tell PLATE you already have something, so it drops off the list."""
    service = svc(request)
    assert service.store is not None and service.library is not None
    if body.food_id not in service.library.foods:
        raise HTTPException(404, f"no food '{body.food_id}'")
    service.store.set_pantry(body.food_id, body.grams)
    service._invalidate()
    return {"ok": True, "pantry": service.store.pantry()}


# --------------------------------------------------------------------------
# insight
# --------------------------------------------------------------------------


@router.get("/insight")
async def insight(request: Request, days: int = Query(90, ge=14, le=365)):
    """Longer-range history for the charts."""
    service = svc(request)
    assert service.store is not None
    snap = await service.snapshot()
    since = date.today() - timedelta(days=days)

    intake = service.store.intake_by_day(since)
    targets = service.store.targets_by_day(since)
    return {
        "snapshot": snap,
        "series": {
            "weight": snap["trend"]["series"],
            "intake": [{"day": d.isoformat(), "kcal": round(v)} for d, v in sorted(intake.items())],
            "target": [{"day": d.isoformat(), "kcal": round(v)} for d, v in sorted(targets.items())],
            "burn": [
                {"day": d.isoformat(), "kcal": round(v)}
                for d, v in sorted(service.store.metrics("calories_burned", since).items())
            ],
            "bp": [
                {"day": d.isoformat(), "systolic": round(v),
                 "diastolic": round(service.store.metrics("bp_diastolic", since).get(d, 0))}
                for d, v in sorted(service.store.metrics("bp_systolic", since).items())
            ],
            "steps": [
                {"day": d.isoformat(), "steps": round(v)}
                for d, v in sorted(service.store.metrics("steps", since).items())
            ],
        },
        "library_warnings": list(service.health.library_warnings),
    }


# --------------------------------------------------------------------------
# settings and data
# --------------------------------------------------------------------------


@router.get("/settings")
async def get_settings(request: Request):
    service = svc(request)
    assert service.store is not None and service.library is not None
    return {
        "config": service.config.public_dict(),
        "overrides": service.store.get_settings(),
        "stores": [
            {"id": s.id, "name": s.name, "short": s.short,
             "delivers": s.delivers, "notes": s.notes}
            for s in service.library.stores.values()
        ],
        "metrics": service.store.metric_keys(),
    }


class SettingsBody(BaseModel):
    profile: dict[str, Any] | None = None
    entities: dict[str, str] | None = None
    diet: dict[str, Any] | None = None
    stores: dict[str, Any] | None = None
    tz_offset_minutes: int | None = None


@router.put("/settings")
async def put_settings(request: Request, body: SettingsBody):
    """Persist UI-editable settings, which shadow the add-on options."""
    service = svc(request)
    assert service.store is not None
    payload = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not payload:
        raise HTTPException(400, "nothing to save")
    service.store.put_settings(payload)
    service.reload_config()
    return {"ok": True, "config": service.config.public_dict()}


@router.get("/entities/discover")
async def entities_discover(request: Request):
    """Suggest Fitbit/scale/blood-pressure entities to point PLATE at."""
    service = svc(request)
    return {"candidates": await service.discover(), "current": service.config.entities.as_map()}


@router.post("/data/reload")
async def data_reload(request: Request):
    service = svc(request)
    ok, message = service.reload_library()
    if not ok:
        raise HTTPException(400, message)
    return {
        "ok": True, "message": message,
        "warnings": list(service.health.library_warnings),
    }


# --------------------------------------------------------------------------
# imports
# --------------------------------------------------------------------------


class MealieBody(BaseModel):
    limit: int = Field(60, ge=1, le=300)
    # Preview by default; nothing is written until the user confirms.
    write: bool = False


@router.post("/import/mealie")
async def import_mealie(request: Request, body: MealieBody):
    service = svc(request)
    assert service.library is not None
    cfg = service.config.mealie
    if not cfg.configured:
        raise HTTPException(400, "Set the Mealie URL and token in the add-on options first")

    importer = MealieImporter(cfg.url, cfg.token)
    ok, message = await importer.probe()
    if not ok:
        raise HTTPException(502, message)

    slugs = await importer.list_slugs(body.limit)
    raw = await importer.fetch(slugs)
    index = build_index(service.library)
    converted = [importer.convert(r, service.library, index) for r in raw]

    written = None
    if body.write:
        path = write_recipe_overlay(converted, service.config.user_dir)
        written = str(path)
        service.reload_library()

    importable = [c for c in converted if c.importable]
    return {
        "server": message,
        "fetched": len(converted),
        "importable": len(importable),
        "written_to": written,
        "recipes": [c.as_dict() for c in converted],
        "hint": (
            "Recipes with unmatched ingredients were skipped rather than imported "
            "with food missing. Add aliases to your own foods YAML, or add the "
            "missing foods, then re-run."
        ),
    }


class UsdaBody(BaseModel):
    food_ids: list[str] | None = None
    limit: int = Field(40, ge=1, le=200)
    include_core: bool = False
    write: bool = True


@router.post("/import/usda")
async def import_usda(request: Request, body: UsdaBody):
    """Backfill micronutrients from FoodData Central."""
    service = svc(request)
    assert service.library is not None
    if not service.config.usda_api_key:
        raise HTTPException(400, "Set a FoodData Central API key in the add-on options first")

    if body.food_ids:
        foods = [service.library.foods[f] for f in body.food_ids if f in service.library.foods]
    else:
        # Default to the foods that are missing the most, most-used first.
        foods = [
            f for f in service.library.foods.values()
            if any(f.per_100g.coverage(n) < 1.0 for n in ("calcium_mg", "magnesium_mg", "iron_mg"))
        ][: body.limit]
    if not foods:
        return {"ok": True, "message": "nothing needed backfilling", "results": []}

    backfiller = UsdaBackfill(service.config.usda_api_key)
    try:
        results = await backfiller.backfill(foods, include_core=body.include_core)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))

    written = None
    if body.write:
        path = write_food_overlay(results, service.library, service.config.user_dir)
        written = str(path)
        service.reload_library()

    return {
        "ok": True,
        "requested": len(foods),
        "matched": sum(1 for r in results if r.nutrients),
        "written_to": written,
        "results": [r.as_dict() for r in results],
        "warning": (
            "Matches are made by fuzzy name search against FoodData Central. Check the "
            "`matched` description on each result before trusting it."
        ),
    }
