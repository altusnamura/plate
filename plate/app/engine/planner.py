"""The menu planner.

The problem is a constrained assignment: fill roughly 28 meal slots a week from
a pool of recipes so that each day lands near its calorie target, protein clears
its floor, sodium stays under its ceiling, most lunches are vegetarian, nothing
repeats too soon, weeknight cooking stays short, and the shopping list doesn't
sprawl across forty perishable ingredients you'll throw half of away.

Those goals conflict, so there is no "correct" plan — only better and worse
trade-offs. Two approaches were available. An integer program would find the
true optimum for the linear parts, but the interesting constraints here are not
linear (batch cooking creates a variable that depends on which days share a
recipe; ingredient overlap is a set-cover reward) and an infeasible model gives
the user nothing at all. So this uses a **greedy seed followed by local search**:
it always returns a plan, it degrades gracefully when the recipe pool is too
small to satisfy everything, and the cost function is readable enough that when a
plan looks odd you can see which term caused it.

Batch cooking falls out of the assignment rather than being planned separately.
If the same recipe lands on Tuesday dinner and Wednesday lunch, ``_find_batches``
merges them into one cook on Tuesday — so prep time is only charged once, which
is what makes the search prefer sensible leftover patterns on its own.

Performance note: the cost function runs thousands of times per plan and Home
Assistant frequently lives on a Raspberry Pi, so anything derivable from a
recipe alone is precomputed once into a :class:`_Stat`. The hot loop then adds
six floats per slot instead of merging nutrient dictionaries, which is the
difference between a replan feeling instant and feeling broken.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence

from .models import Library, Meal, Recipe
from .nutrients import Nutrients
from .targets import NutritionTargets, count_dash_servings, dash_score

log = logging.getLogger(__name__)

# Portion sizes the planner may use. Quantised because "1.37 servings" is not
# something you can plate, and because it keeps shopping quantities sane.
SERVING_STEPS: tuple[float, ...] = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)

# Most servings worth cooking in one go. Beyond this you're eating the same
# thing for five days and the plan stops being pleasant.
MAX_BATCH_SERVINGS = 6.0

# ...and however many servings it is, it may not feed more than this many meal
# slots. Without the slot cap, one batch could legally cover five sittings inside
# its keep window, and because batched slots are exempt from the "don't repeat
# this within two days" penalty, the search would happily do exactly that.
MAX_BATCH_SLOTS = 3

# Times one recipe may appear across the whole plan before it starts costing.
# This penalty applies regardless of batching, which is the backstop that keeps
# a cheap-to-shop-for dish from taking over the week.
MAX_FREE_APPEARANCES = 3

# Ceiling on portion scaling per meal type. A 2x dinner is a big dinner; a 2x
# snack is not a snack. Applied in both the portion fitter and the search's
# serving-nudge move.
_MAX_SERVINGS_BY_MEAL = {
    Meal.BREAKFAST: 1.5,
    Meal.LUNCH: 1.75,
    Meal.DINNER: 1.75,
    Meal.SNACK: 1.25,
}


def _max_servings(meal: Meal) -> float:
    return _MAX_SERVINGS_BY_MEAL.get(meal, 1.75)

# Recipe yields are fixed, so a batch cooks in these multiples of the stated
# serving count.
COOK_MULTIPLES: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)

# The nutrients the cost function judges. All are core, so they are always
# present on every recipe and need no coverage guards in the hot loop.
_JUDGED = ("kcal", "protein_g", "fiber_g", "potassium_mg", "sodium_mg", "satfat_g")


@dataclass(frozen=True, slots=True)
class Weights:
    """Cost weights. Each term is normalised so these are comparable.

    A weight of 100 on ``kcal`` means a day 10% off its calorie target costs
    1.0 point, which is the reference scale for everything else.
    """

    kcal: float = 100.0
    protein: float = 70.0
    # High, because the DASH reward pulls the other way: potassium-rich whole
    # foods and a 1500 mg sodium ceiling are compatible, but only if the search
    # is pushed to find the combinations that manage both.
    sodium: float = 72.0
    satfat: float = 25.0
    fiber: float = 22.0
    potassium: float = 16.0
    vegetarian: float = 60.0
    repeat_soon: float = 30.0
    repeat_week: float = 14.0
    # Appearances beyond MAX_FREE_APPEARANCES, batched or not.
    repeat_total: float = 24.0
    # Same dish twice in one day, which reads as a mistake even when the macros work.
    same_day_repeat: float = 34.0
    cuisine_adjacent: float = 7.0
    # Measured: raising this from 26 to 60 pulls mean weeknight cooking from
    # ~41 min to ~33 against a 35 min budget. Pushing on to 90 buys almost
    # nothing further (the term saturates against the nutrition constraints) at
    # the cost of a blander, more batch-heavy week — so the budget behaves as a
    # strong preference, not a hard cap, and some evenings will still run over.
    prep_time: float = 60.0
    unpackable_lunch: float = 12.0
    # Deliberately small. This term scales with the *total* number of distinct
    # perishable ingredients — around 40-70 for a week — so a weight in the low
    # single digits silently dominates every nutrition term put together and the
    # search collapses the menu onto three recipes to save shopping. It is a
    # tie-breaker between otherwise-equal plans, nothing more.
    shopping_breadth: float = 0.35
    dash: float = 4.5
    waste: float = 14.0
    monotony: float = 12.0


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """Everything the planner needs that isn't the recipe library."""

    start: date
    days: int
    # Per-day calorie target. Future days use the activity-free base target;
    # today may differ because it accounts for what you've actually burned.
    kcal_by_day: Mapping[date, float]
    targets: NutritionTargets
    meals: tuple[Meal, ...] = (Meal.BREAKFAST, Meal.LUNCH, Meal.DINNER)
    snacks_per_day: int = 1
    vegetarian_lunch_ratio: float = 0.85
    vegetarian_dinner_ratio: float = 0.4
    max_weekday_prep_min: int = 35
    exclude_foods: frozenset[str] = frozenset()
    exclude_recipes: frozenset[str] = frozenset()
    # slot key -> recipe_id the user has pinned; the search will not move these.
    locked: Mapping[str, str] = field(default_factory=dict)
    seed: int = 0
    iterations: int = 4000
    weights: Weights = Weights()

    def day_list(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(self.days)]


@dataclass(frozen=True, slots=True)
class Slot:
    day: date
    meal: Meal
    index: int = 0  # distinguishes multiple snacks on one day

    @property
    def key(self) -> str:
        return f"{self.day.isoformat()}:{self.meal.value}:{self.index}"

    @property
    def is_weekday(self) -> bool:
        return self.day.weekday() < 5


@dataclass(frozen=True, slots=True)
class Batch:
    """One cooking session that feeds one or more slots."""

    recipe_id: str
    cook_day: date
    slot_keys: tuple[str, ...]
    servings_eaten: float
    servings_cooked: float

    @property
    def is_batch(self) -> bool:
        return len(self.slot_keys) > 1

    @property
    def waste(self) -> float:
        return max(0.0, self.servings_cooked - self.servings_eaten)


@dataclass(slots=True)
class Assignment:
    """A mutable candidate solution: slot key -> (recipe id, servings)."""

    recipes: dict[str, str]
    servings: dict[str, float]

    def copy(self) -> "Assignment":
        return Assignment(dict(self.recipes), dict(self.servings))


# --------------------------------------------------------------------------
# precomputed per-recipe facts
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Stat:
    """Everything the cost function needs about one recipe, computed once."""

    recipe_id: str
    # Per-serving amounts of the judged nutrients, in _JUDGED order.
    nutrients: tuple[float, ...]
    # Per-serving DASH servings by group.
    dash: Mapping[str, float]
    # Non-pantry ingredients, i.e. things that land on a shopping list.
    perishables: frozenset[str]
    vegetarian: bool
    packable: bool
    cuisine: str
    total_min: int
    keeps_days: int
    meals: frozenset[Meal]
    yield_servings: float


@dataclass(frozen=True, slots=True)
class _Ctx:
    """Immutable context shared by every cost evaluation in one plan run."""

    library: Library
    req: PlanRequest
    stats: Mapping[str, _Stat]
    slots: tuple[Slot, ...]
    slots_by_day: Mapping[date, tuple[Slot, ...]]
    # Target references pulled out of NutritionTargets once, in _JUDGED order.
    floors: tuple[float | None, ...]
    ceilings: tuple[float | None, ...]

    def stat(self, slot_key: str, a: Assignment) -> _Stat | None:
        rid = a.recipes.get(slot_key)
        return self.stats.get(rid) if rid else None


def _build_stats(library: Library) -> dict[str, _Stat]:
    out: dict[str, _Stat] = {}
    for rid, recipe in library.recipes.items():
        n = recipe.nutrition
        per_serving_grams = recipe.scaled_grams(1.0)
        dash = count_dash_servings(per_serving_grams, library.foods)
        out[rid] = _Stat(
            recipe_id=rid,
            nutrients=tuple(n.get(k) for k in _JUDGED),
            dash=dash,
            perishables=frozenset(
                fid for fid in recipe.food_ids if not library.foods[fid].pantry_staple
            ),
            vegetarian=recipe.vegetarian,
            packable=recipe.packable,
            cuisine=recipe.cuisine,
            total_min=recipe.total_min,
            keeps_days=recipe.keeps_days,
            meals=frozenset(recipe.meals),
            yield_servings=recipe.servings,
        )
    return out


def _build_ctx(library: Library, req: PlanRequest, slots: Sequence[Slot]) -> _Ctx:
    by_day: dict[date, list[Slot]] = {}
    for s in slots:
        by_day.setdefault(s.day, []).append(s)

    floors: list[float | None] = []
    ceilings: list[float | None] = []
    for nutrient in _JUDGED:
        t = req.targets.get(nutrient)
        if t is None:
            floors.append(None)
            ceilings.append(None)
        else:
            floors.append(t.floor or t.goal)
            ceilings.append(t.ceiling)

    return _Ctx(
        library=library,
        req=req,
        stats=_build_stats(library),
        slots=tuple(slots),
        slots_by_day={d: tuple(v) for d, v in by_day.items()},
        floors=tuple(floors),
        ceilings=tuple(ceilings),
    )


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedMeal:
    slot: Slot
    recipe: Recipe
    servings: float
    nutrition: Nutrients
    # Set when this slot eats something cooked on an earlier day.
    leftover_from: date | None = None
    # Number of distinct days the cook session this slot belongs to feeds.
    feeds_days: int = 1

    def as_dict(self) -> dict[str, object]:
        n = self.nutrition
        return {
            "slot": self.slot.key,
            "day": self.slot.day.isoformat(),
            "meal": self.slot.meal.value,
            "recipe": self.recipe.summary(),
            "servings": self.servings,
            "kcal": round(n.kcal),
            "protein_g": round(n.get("protein_g")),
            "sodium_mg": round(n.get("sodium_mg")),
            "fiber_g": round(n.get("fiber_g")),
            "vegetarian": self.recipe.vegetarian,
            "leftover_from": self.leftover_from.isoformat() if self.leftover_from else None,
            "feeds_days": self.feeds_days,
            "active_min": 0 if self.leftover_from else self.recipe.total_min,
        }


@dataclass(frozen=True, slots=True)
class DayPlan:
    day: date
    meals: tuple[PlannedMeal, ...]
    nutrition: Nutrients
    kcal_target: float
    evaluation: Mapping[str, dict[str, object]]
    dash: object
    active_min: int

    @property
    def vegetarian_meals(self) -> int:
        return sum(1 for m in self.meals if m.recipe.vegetarian)

    def as_dict(self) -> dict[str, object]:
        return {
            "day": self.day.isoformat(),
            "weekday": self.day.strftime("%a"),
            "meals": [m.as_dict() for m in self.meals],
            "kcal": round(self.nutrition.kcal),
            "kcal_target": round(self.kcal_target),
            "active_min": self.active_min,
            "nutrition": self.nutrition.as_dict(include_coverage=True),
            "evaluation": dict(self.evaluation),
            "dash_score": round(getattr(self.dash, "score", 0.0), 1),
            "vegetarian_meals": self.vegetarian_meals,
        }


@dataclass(frozen=True, slots=True)
class MenuPlan:
    start: date
    days: tuple[DayPlan, ...]
    batches: tuple[Batch, ...]
    grams_by_food: Mapping[str, float]
    cost: float
    diagnostics: Mapping[str, object]
    seed: int

    def all_meals(self) -> Iterable[PlannedMeal]:
        for d in self.days:
            yield from d.meals

    def as_dict(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "days": [d.as_dict() for d in self.days],
            "batches": [
                {
                    "recipe_id": b.recipe_id,
                    "cook_day": b.cook_day.isoformat(),
                    "servings_cooked": b.servings_cooked,
                    "servings_eaten": b.servings_eaten,
                    "feeds_slots": list(b.slot_keys),
                }
                for b in self.batches
                if b.is_batch
            ],
            "cost": round(self.cost, 2),
            "diagnostics": dict(self.diagnostics),
            "seed": self.seed,
        }


# --------------------------------------------------------------------------
# slots and pools
# --------------------------------------------------------------------------


def _slots(req: PlanRequest) -> list[Slot]:
    out: list[Slot] = []
    for day in req.day_list():
        for meal in req.meals:
            out.append(Slot(day=day, meal=meal))
        for i in range(req.snacks_per_day):
            out.append(Slot(day=day, meal=Meal.SNACK, index=i))
    return out


def _eligible(library: Library, req: PlanRequest, meal: Meal) -> list[Recipe]:
    """Recipes usable for ``meal`` after hard filters.

    Exclusions are hard filters rather than cost terms on purpose: a disliked
    food should never appear because the search happened to find it cheap.
    """
    out: list[Recipe] = []
    for r in library.recipes.values():
        if r.id in req.exclude_recipes:
            continue
        if not r.suits(meal):
            continue
        if req.exclude_foods & r.food_ids:
            continue
        out.append(r)
    return out


def _meal_rank(meal: Meal) -> int:
    return {Meal.BREAKFAST: 0, Meal.LUNCH: 1, Meal.SNACK: 2, Meal.DINNER: 3}[meal]


def _is_weekday(day: date) -> bool:
    return day.weekday() < 5


# --------------------------------------------------------------------------
# batching
# --------------------------------------------------------------------------


def cook_yield(recipe_servings: float, needed: float) -> float:
    """Servings actually produced when you need ``needed`` portions.

    You can halve a recipe or make one-and-a-half batches, but you cannot make
    2.3 servings of something that yields 4 at a time.
    """
    if needed <= 0:
        return 0.0
    ratio = needed / recipe_servings
    for mult in COOK_MULTIPLES:
        if ratio <= mult + 1e-9:
            return mult * recipe_servings
    return math.ceil(ratio) * recipe_servings


def _find_batches(ctx: _Ctx, a: Assignment) -> dict[str, Batch]:
    """Group repeat appearances of a recipe into single cooking sessions.

    Slots eating the same recipe merge when they fall within the recipe's
    ``keeps_days`` of the cook day and the combined portions stay under
    :data:`MAX_BATCH_SERVINGS`. Returns slot key -> the batch feeding it.
    """
    by_recipe: dict[str, list[Slot]] = {}
    for slot in ctx.slots:
        rid = a.recipes.get(slot.key)
        if rid:
            by_recipe.setdefault(rid, []).append(slot)

    out: dict[str, Batch] = {}
    for rid, group in by_recipe.items():
        stat = ctx.stats[rid]
        group.sort(key=lambda s: (s.day, _meal_rank(s.meal)))
        run: list[Slot] = []
        eaten = 0.0
        for slot in group:
            portion = a.servings.get(slot.key, 1.0)
            within_keep = bool(run) and (slot.day - run[0].day).days <= stat.keeps_days
            fits = eaten + portion <= MAX_BATCH_SERVINGS and len(run) < MAX_BATCH_SLOTS
            if run and within_keep and fits:
                run.append(slot)
                eaten += portion
            else:
                if run:
                    _emit_batch(out, stat, run, eaten)
                run = [slot]
                eaten = portion
        if run:
            _emit_batch(out, stat, run, eaten)
    return out


def _emit_batch(out: dict[str, Batch], stat: _Stat, run: list[Slot], eaten: float) -> None:
    batch = Batch(
        recipe_id=stat.recipe_id,
        cook_day=run[0].day,
        slot_keys=tuple(s.key for s in run),
        servings_eaten=eaten,
        servings_cooked=cook_yield(stat.yield_servings, eaten),
    )
    for s in run:
        out[s.key] = batch


# --------------------------------------------------------------------------
# cost
# --------------------------------------------------------------------------


def _cost(ctx: _Ctx, a: Assignment, breakdown: dict[str, float] | None = None) -> float:
    w = ctx.req.weights
    req = ctx.req
    parts: dict[str, float] = {}
    total = 0.0

    def add(name: str, value: float) -> None:
        nonlocal total
        if value:
            total += value
            parts[name] = parts.get(name, 0.0) + value

    batches = _find_batches(ctx, a)

    # ---- per-day nutrition, prep time and DASH ---------------------------
    for day, day_slots in ctx.slots_by_day.items():
        sums = [0.0] * len(_JUDGED)
        dash_servings: dict[str, float] = {}
        active = 0

        for slot in day_slots:
            stat = ctx.stat(slot.key, a)
            if stat is None:
                continue
            portion = a.servings.get(slot.key, 1.0)
            for i, v in enumerate(stat.nutrients):
                sums[i] += v * portion
            for group, n in stat.dash.items():
                dash_servings[group] = dash_servings.get(group, 0.0) + n * portion

            batch = batches.get(slot.key)
            if batch is not None and batch.slot_keys[0] == slot.key:
                active += stat.total_min

        kcal = sums[0]
        target_kcal = req.kcal_by_day.get(day, req.targets.kcal)
        if target_kcal > 0:
            rel = (kcal - target_kcal) / target_kcal
            add("kcal", w.kcal * rel * rel)

        for i, name in enumerate(_JUDGED):
            if name == "kcal":
                continue
            weight = getattr(w, _WEIGHT_FOR[name])
            floor = ctx.floors[i]
            ceiling = ctx.ceilings[i]
            if ceiling is not None and ceiling > 0:
                over = max(0.0, sums[i] - ceiling) / ceiling
                add(name, weight * over * over)
            elif floor is not None and floor > 0:
                short = max(0.0, floor - sums[i]) / floor
                add(name, weight * short * short)

        cap = req.max_weekday_prep_min if _is_weekday(day) else req.max_weekday_prep_min * 2
        if cap > 0 and active > cap:
            over = (active - cap) / cap
            add("prep_time", w.prep_time * over * over)

        # DASH alignment is a reward, scaled to the same order as the penalties.
        score = dash_score(dash_servings, kcal).score
        add("dash", -w.dash * score / 100.0)

    # ---- vegetarian ratios ------------------------------------------------
    add("vegetarian", w.vegetarian * _veg_penalty(ctx, a, Meal.LUNCH, req.vegetarian_lunch_ratio))
    add(
        "vegetarian",
        w.vegetarian * 0.6 * _veg_penalty(ctx, a, Meal.DINNER, req.vegetarian_dinner_ratio),
    )

    # ---- variety ----------------------------------------------------------
    appearances: dict[str, list[Slot]] = {}
    for s in ctx.slots:
        rid = a.recipes.get(s.key)
        if rid:
            appearances.setdefault(rid, []).append(s)

    for rid, group in appearances.items():
        group.sort(key=lambda s: (s.day, _meal_rank(s.meal)))

        # Repeats are fine when they're leftovers from one cook; eating the same
        # dish from two separate cooking sessions in a week is monotony.
        cook_days = {batches[s.key].cook_day for s in group if s.key in batches}
        if len(cook_days) > 1:
            add("repeat_week", w.repeat_week * (len(cook_days) - 1))
        for x, y in zip(group, group[1:]):
            if batches.get(x.key) is not batches.get(y.key):
                gap = (y.day - x.day).days
                if gap <= 2:
                    add("repeat_soon", w.repeat_soon * (3 - gap) / 3.0)

        # Total-appearance cap, which applies even inside a single batch. The
        # penalties above all exempt same-batch repeats, so without this a big
        # cook could cover half the week for free.
        excess = len(group) - MAX_FREE_APPEARANCES
        if excess > 0:
            add("repeat_total", w.repeat_total * excess ** 1.5)

        # The same dish twice in one day.
        per_day: dict[date, int] = {}
        for s in group:
            per_day[s.day] = per_day.get(s.day, 0) + 1
        for n in per_day.values():
            if n > 1:
                add("same_day_repeat", w.same_day_repeat * (n - 1))

    # Cuisine variety across dinners, so the week doesn't read as five nights of
    # the same thing wearing different hats.
    dinners = sorted(
        (s for s in ctx.slots if s.meal is Meal.DINNER and a.recipes.get(s.key)),
        key=lambda s: s.day,
    )
    for x, y in zip(dinners, dinners[1:]):
        sx, sy = ctx.stats[a.recipes[x.key]], ctx.stats[a.recipes[y.key]]
        if sx.cuisine == sy.cuisine and sx.recipe_id != sy.recipe_id:
            add("cuisine_adjacent", w.cuisine_adjacent)

    distinct = len(set(a.recipes.values()))
    ideal = max(6, int(len(ctx.slots) * 0.45))
    if distinct < ideal:
        add("monotony", w.monotony * (ideal - distinct))

    # ---- practicality -----------------------------------------------------
    for s in ctx.slots:
        if s.meal is Meal.LUNCH and s.is_weekday:
            stat = ctx.stat(s.key, a)
            if stat is not None and not stat.packable:
                add("unpackable_lunch", w.unpackable_lunch)

    # ---- shopping ---------------------------------------------------------
    # Every distinct perishable ingredient is another thing to buy and possibly
    # waste, so breadth is penalised. Pantry staples are free.
    needed: set[str] = set()
    for rid in set(a.recipes.values()):
        needed |= ctx.stats[rid].perishables
    add("shopping_breadth", w.shopping_breadth * len(needed))

    # Cooked-but-uneaten portions, since recipe yields are fixed.
    for batch in set(batches.values()):
        if batch.waste > 0.05:
            add("waste", w.waste * batch.waste)

    if breakdown is not None:
        breakdown.clear()
        breakdown.update({k: round(v, 2) for k, v in parts.items()})
    return total


_WEIGHT_FOR = {
    "protein_g": "protein",
    "fiber_g": "fiber",
    "potassium_mg": "potassium",
    "sodium_mg": "sodium",
    "satfat_g": "satfat",
}


def _veg_penalty(ctx: _Ctx, a: Assignment, meal: Meal, ratio: float) -> float:
    """Squared shortfall against the requested vegetarian share of ``meal``.

    Normalised by meal count so missing the lunch ratio by one meal costs the
    same whatever the plan length.
    """
    total = 0
    veg = 0
    for s in ctx.slots:
        if s.meal is not meal:
            continue
        stat = ctx.stat(s.key, a)
        if stat is None:
            continue
        total += 1
        if stat.vegetarian:
            veg += 1
    if total == 0:
        return 0.0
    short = max(0.0, ratio * total - veg) / total
    return short * short


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------


def plan_menu(library: Library, req: PlanRequest) -> MenuPlan:
    """Produce a menu plan. Deterministic for a given ``req.seed``."""
    rng = random.Random(req.seed)
    slots = _slots(req)
    ctx = _build_ctx(library, req, slots)

    pools = {meal: _eligible(library, req, meal) for meal in {s.meal for s in slots}}
    empty = sorted(m.value for m, p in pools.items() if not p)
    if empty:
        raise ValueError(
            "No recipes available for: " + ", ".join(empty)
            + ". Loosen the exclusions or add recipes for those meals."
        )

    assignment = _greedy_seed(ctx, pools, rng)
    seed_cost = _cost(ctx, assignment)
    assignment = _local_search(ctx, pools, rng, assignment)
    final_cost = _cost(ctx, assignment)
    log.debug("planner: cost %.1f -> %.1f (%d iterations)", seed_cost, final_cost, req.iterations)

    return _materialise(ctx, assignment)


def _greedy_seed(
    ctx: _Ctx, pools: Mapping[Meal, Sequence[Recipe]], rng: random.Random
) -> Assignment:
    """Fill slots in order, each time taking the locally cheapest option.

    Greedy alone produces a mediocre plan — it cannot see that Tuesday's choice
    makes Thursday awkward — but it lands in a sane basin, and the local search
    that follows does the real work from there.
    """
    a = Assignment(recipes={}, servings={})
    for slot in ctx.slots:
        locked = ctx.req.locked.get(slot.key)
        if locked and locked in ctx.library.recipes:
            a.recipes[slot.key] = locked
            a.servings[slot.key] = 1.0
            continue

        pool = list(pools[slot.meal])
        rng.shuffle(pool)
        # Only score a sample: exhaustively scoring every candidate at every slot
        # is wasted effort when the local search revisits all of it anyway.
        sample = pool[: min(len(pool), 10)]
        best, best_cost = sample[0].id, float("inf")
        for recipe in sample:
            a.recipes[slot.key] = recipe.id
            a.servings[slot.key] = 1.0
            c = _cost(ctx, a)
            if c < best_cost:
                best, best_cost = recipe.id, c
        a.recipes[slot.key] = best
        a.servings[slot.key] = 1.0

    _fit_portions(ctx, a)
    return a


def _local_search(
    ctx: _Ctx,
    pools: Mapping[Meal, Sequence[Recipe]],
    rng: random.Random,
    start: Assignment,
) -> Assignment:
    """Hill-climb with occasional uphill acceptance to escape local minima.

    Temperature decays linearly to zero, so the tail of the run is pure
    hill-climbing and the returned plan is a local optimum of the cost function.
    """
    movable = [s for s in ctx.slots if s.key not in ctx.req.locked]
    if not movable:
        return start

    current = start.copy()
    current_cost = _cost(ctx, current)
    best, best_cost = current.copy(), current_cost

    iterations = max(200, ctx.req.iterations)
    for i in range(iterations):
        temperature = 2.5 * (1.0 - i / iterations)
        candidate = current.copy()

        roll = rng.random()
        if roll < 0.55:
            _move_swap_recipe(candidate, movable, pools, rng)
        elif roll < 0.75:
            _move_swap_slots(candidate, movable, rng)
        elif roll < 0.90:
            _move_extend_batch(ctx, candidate, movable, rng)
        else:
            _move_nudge_servings(candidate, movable, rng)

        _fit_portions(ctx, candidate)
        cand_cost = _cost(ctx, candidate)

        delta = cand_cost - current_cost
        if delta < 0 or (temperature > 1e-6 and rng.random() < math.exp(-delta / temperature)):
            current, current_cost = candidate, cand_cost
            if cand_cost < best_cost:
                best, best_cost = candidate.copy(), cand_cost

    return best


def _move_swap_recipe(
    a: Assignment,
    movable: Sequence[Slot],
    pools: Mapping[Meal, Sequence[Recipe]],
    rng: random.Random,
) -> None:
    slot = rng.choice(movable)
    pool = pools.get(slot.meal)
    if pool:
        a.recipes[slot.key] = rng.choice(pool).id


def _move_swap_slots(a: Assignment, movable: Sequence[Slot], rng: random.Random) -> None:
    """Exchange two same-meal slots, which shifts a dish to a different day."""
    groups: dict[Meal, list[Slot]] = {}
    for s in movable:
        groups.setdefault(s.meal, []).append(s)
    candidates = [g for g in groups.values() if len(g) >= 2]
    if not candidates:
        return
    s1, s2 = rng.sample(rng.choice(candidates), 2)
    a.recipes[s1.key], a.recipes[s2.key] = a.recipes[s2.key], a.recipes[s1.key]
    a.servings[s1.key], a.servings[s2.key] = a.servings[s2.key], a.servings[s1.key]


def _move_extend_batch(
    ctx: _Ctx, a: Assignment, movable: Sequence[Slot], rng: random.Random
) -> None:
    """Copy a dish onto a nearby slot, proposing a leftovers pattern.

    This is the move that discovers "cook it Tuesday, eat it again Wednesday
    lunch". The batch detector then charges prep time only once, so if the
    nutrition still works the search keeps it.
    """
    slot = rng.choice(movable)
    rid = a.recipes.get(slot.key)
    if not rid:
        return
    stat = ctx.stats[rid]
    nearby = [
        s for s in movable
        if s.key != slot.key
        and s.meal in stat.meals
        and 0 < (s.day - slot.day).days <= stat.keeps_days
    ]
    if nearby:
        a.recipes[rng.choice(nearby).key] = rid


def _move_nudge_servings(a: Assignment, movable: Sequence[Slot], rng: random.Random) -> None:
    slot = rng.choice(movable)
    current = a.servings.get(slot.key, 1.0)
    idx = min(range(len(SERVING_STEPS)), key=lambda i: abs(SERVING_STEPS[i] - current))
    idx = max(0, min(len(SERVING_STEPS) - 1, idx + rng.choice((-1, 1))))
    a.servings[slot.key] = min(SERVING_STEPS[idx], _max_servings(slot.meal))


def _fit_portions(ctx: _Ctx, a: Assignment) -> None:
    """Scale each day's portions toward its calorie target.

    Recipe choice sets the *character* of a day; portion size is how it lands on
    the number. Scaling proportionally then quantising to :data:`SERVING_STEPS`
    gets within a few percent without the search having to stumble onto the right
    combination of portions by itself. The correction is capped, because a day
    40% off has the wrong recipes and portions shouldn't paper over that.
    """
    for day, day_slots in ctx.slots_by_day.items():
        target = ctx.req.kcal_by_day.get(day, ctx.req.targets.kcal)
        if target <= 0:
            continue
        current = 0.0
        for s in day_slots:
            stat = ctx.stat(s.key, a)
            if stat is not None:
                current += stat.nutrients[0] * a.servings.get(s.key, 1.0)
        if current <= 0:
            continue
        factor = max(0.7, min(1.4, target / current))
        for s in day_slots:
            if s.key in ctx.req.locked or s.key not in a.recipes:
                continue
            want = a.servings.get(s.key, 1.0) * factor
            cap = _max_servings(s.meal)
            allowed = [v for v in SERVING_STEPS if v <= cap] or [SERVING_STEPS[0]]
            a.servings[s.key] = min(allowed, key=lambda v: abs(v - want))


# --------------------------------------------------------------------------
# materialisation
# --------------------------------------------------------------------------


def _materialise(ctx: _Ctx, a: Assignment) -> MenuPlan:
    """Turn the winning assignment into the structure the API returns."""
    library, req = ctx.library, ctx.req
    batches = _find_batches(ctx, a)
    breakdown: dict[str, float] = {}
    total_cost = _cost(ctx, a, breakdown)

    days: list[DayPlan] = []
    for day in req.day_list():
        day_slots = sorted(
            ctx.slots_by_day.get(day, ()), key=lambda s: (_meal_rank(s.meal), s.index)
        )
        planned: list[PlannedMeal] = []
        nutrition = Nutrients.zero()
        active = 0
        day_grams: dict[str, float] = {}

        for slot in day_slots:
            rid = a.recipes.get(slot.key)
            if not rid:
                continue
            recipe = library.recipes[rid]
            servings = a.servings.get(slot.key, 1.0)
            n = recipe.nutrition.scaled(servings)
            nutrition = nutrition + n

            batch = batches.get(slot.key)
            cooks_here = batch is not None and batch.slot_keys[0] == slot.key
            if cooks_here:
                active += recipe.total_min

            feeds_days = 1
            if batch is not None:
                feeds_days = len({k.split(":", 1)[0] for k in batch.slot_keys})

            planned.append(
                PlannedMeal(
                    slot=slot,
                    recipe=recipe,
                    servings=servings,
                    nutrition=n,
                    leftover_from=None if cooks_here else (batch.cook_day if batch else None),
                    feeds_days=feeds_days,
                )
            )
            for fid, g in recipe.scaled_grams(servings).items():
                day_grams[fid] = day_grams.get(fid, 0.0) + g

        days.append(
            DayPlan(
                day=day,
                meals=tuple(planned),
                nutrition=nutrition,
                kcal_target=req.kcal_by_day.get(day, req.targets.kcal),
                evaluation=req.targets.evaluate(nutrition),
                dash=dash_score(count_dash_servings(day_grams, library.foods), nutrition.kcal),
                active_min=active,
            )
        )

    # Shopping quantities follow what is actually *cooked*, not what is eaten,
    # because a recipe yielding four servings uses four servings of onion even
    # if the plan only eats three.
    grams_total: dict[str, float] = {}
    for batch in set(batches.values()):
        recipe = library.recipes[batch.recipe_id]
        for fid, g in recipe.scaled_grams(batch.servings_cooked).items():
            grams_total[fid] = grams_total.get(fid, 0.0) + g

    return MenuPlan(
        start=req.start,
        days=tuple(days),
        batches=tuple(sorted(set(batches.values()), key=lambda b: (b.cook_day, b.recipe_id))),
        grams_by_food=grams_total,
        cost=total_cost,
        diagnostics=_diagnose(ctx, a, days, breakdown),
        seed=req.seed,
    )


def _diagnose(
    ctx: _Ctx,
    a: Assignment,
    days: Sequence[DayPlan],
    breakdown: Mapping[str, float],
) -> dict[str, object]:
    """Plain-language account of where the plan compromised.

    The cost breakdown says which terms hurt; this translates the ones a person
    would want to know about into sentences, so an odd-looking week explains
    itself instead of looking like a bug.
    """
    req = ctx.req

    def veg_count(meal: Meal) -> tuple[int, int]:
        total = veg = 0
        for s in ctx.slots:
            if s.meal is not meal:
                continue
            stat = ctx.stat(s.key, a)
            if stat is None:
                continue
            total += 1
            veg += 1 if stat.vegetarian else 0
        return veg, total

    veg_lunch, n_lunch = veg_count(Meal.LUNCH)
    veg_dinner, n_dinner = veg_count(Meal.DINNER)

    issues: list[str] = []
    for d in days:
        if d.kcal_target:
            off = (d.nutrition.kcal - d.kcal_target) / d.kcal_target
            if abs(off) > 0.10:
                issues.append(
                    f"{d.day.strftime('%a')} lands {off * 100:+.0f}% off its calorie target "
                    f"({d.nutrition.kcal:.0f} vs {d.kcal_target:.0f})."
                )
        for nutrient, label in (("sodium_mg", "Sodium"), ("satfat_g", "Saturated fat")):
            ev = d.evaluation.get(nutrient, {})
            if ev.get("status") == "over":
                issues.append(
                    f"{d.day.strftime('%a')}: {label} {ev['amount']:.0f} {ev['unit']} is over the "
                    f"{ev['ceiling']:.0f} {ev['unit']} limit."
                )
        for nutrient, label in (("protein_g", "Protein"), ("fiber_g", "Fibre")):
            ev = d.evaluation.get(nutrient, {})
            if ev.get("status") == "under" and ev.get("floor"):
                issues.append(
                    f"{d.day.strftime('%a')}: {label} {ev['amount']:.0f} {ev['unit']} is short of "
                    f"{ev['floor']:.0f} {ev['unit']}."
                )

    if n_lunch and veg_lunch / n_lunch < req.vegetarian_lunch_ratio - 1e-6:
        issues.append(
            f"Only {veg_lunch} of {n_lunch} lunches are vegetarian, against a target of "
            f"{req.vegetarian_lunch_ratio:.0%}. This usually means the vegetarian lunch recipes "
            "could not clear the protein floor — adding a few high-protein ones fixes it."
        )

    return {
        "cost_breakdown": dict(sorted(breakdown.items(), key=lambda kv: -abs(kv[1]))),
        "vegetarian_lunches": f"{veg_lunch}/{n_lunch}",
        "vegetarian_dinners": f"{veg_dinner}/{n_dinner}",
        "distinct_recipes": len(set(a.recipes.values())),
        "total_active_min": sum(d.active_min for d in days),
        "mean_dash_score": round(
            sum(getattr(d.dash, "score", 0.0) for d in days) / max(len(days), 1), 1
        ),
        "issues": issues,
    }
