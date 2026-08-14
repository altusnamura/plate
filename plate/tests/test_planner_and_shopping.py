"""Planner constraints and shopping list construction.

These are behavioural tests against the real recipe library, so they assert the
*constraints the user was promised* rather than a specific menu. A plan is a
trade-off between conflicting goals and there is no single right answer, but
"most lunches are vegetarian", "nothing is eaten twice in one day" and "snacks
aren't scaled to 700 kcal" are promises regardless of which recipes get picked.
"""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from app.engine import targets as tg
from app.engine.models import Meal
from app.engine.planner import (
    MAX_BATCH_SLOTS,
    MAX_FREE_APPEARANCES,
    PlanRequest,
    _max_servings,
    cook_yield,
    plan_menu,
)
from app.engine.shopping import build_shopping_list

START = date(2026, 8, 17)  # a Monday


def make_request(targets, **kwargs):
    days = kwargs.pop("days", 7)
    kcal = kwargs.pop("kcal", 2200.0)
    return PlanRequest(
        start=START,
        days=days,
        kcal_by_day={START + timedelta(days=i): kcal for i in range(days)},
        targets=targets,
        snacks_per_day=1,
        seed=kwargs.pop("seed", 11),
        iterations=kwargs.pop("iterations", 2500),
        **kwargs,
    )


@pytest.fixture(scope="module")
def plan(library, normal_bp):
    targets = tg.build_targets(2200, 190, "lose", normal_bp, sex="male")
    return plan_menu(library, make_request(targets))


# --------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------


def test_plan_fills_every_slot(plan):
    assert len(plan.days) == 7
    for day in plan.days:
        assert len(day.meals) == 4          # breakfast, lunch, dinner, one snack
        assert all(m.recipe is not None for m in day.meals)


def test_plan_is_deterministic_for_a_seed(library, normal_bp):
    targets = tg.build_targets(2200, 190, "lose", normal_bp, sex="male")
    a = plan_menu(library, make_request(targets, seed=42))
    b = plan_menu(library, make_request(targets, seed=42))
    assert [m.recipe.id for m in a.all_meals()] == [m.recipe.id for m in b.all_meals()]


def test_different_seeds_give_different_weeks(library, normal_bp):
    targets = tg.build_targets(2200, 190, "lose", normal_bp, sex="male")
    a = plan_menu(library, make_request(targets, seed=1))
    b = plan_menu(library, make_request(targets, seed=999))
    assert [m.recipe.id for m in a.all_meals()] != [m.recipe.id for m in b.all_meals()]


# --------------------------------------------------------------------------
# the promises
# --------------------------------------------------------------------------


def test_most_lunches_are_vegetarian(plan):
    """The headline requirement: lunches are mostly meat-free."""
    lunches = [m for m in plan.all_meals() if m.slot.meal is Meal.LUNCH]
    veg = sum(1 for m in lunches if m.recipe.vegetarian)
    assert veg / len(lunches) >= 0.70, f"only {veg}/{len(lunches)} vegetarian"


def test_calorie_targets_are_roughly_met(plan):
    for day in plan.days:
        off = abs(day.nutrition.kcal - day.kcal_target) / day.kcal_target
        assert off < 0.15, f"{day.day} is {off:.0%} off target"


def test_no_recipe_appears_twice_in_one_day(plan):
    for day in plan.days:
        ids = [m.recipe.id for m in day.meals]
        assert len(ids) == len(set(ids)), f"{day.day} repeats {ids}"


def test_no_recipe_dominates_the_week(plan):
    counts: dict[str, int] = {}
    for meal in plan.all_meals():
        counts[meal.recipe.id] = counts.get(meal.recipe.id, 0) + 1
    worst = max(counts.values())
    # The cost function allows MAX_FREE_APPEARANCES for free and charges beyond
    # it; a small overshoot is a legitimate trade, five of one dish is not.
    assert worst <= MAX_FREE_APPEARANCES + 1, counts


def test_a_batch_never_feeds_too_many_sittings(plan):
    for batch in plan.batches:
        assert len(batch.slot_keys) <= MAX_BATCH_SLOTS


def test_leftovers_respect_the_keep_window(plan, library):
    for batch in plan.batches:
        recipe = library.recipes[batch.recipe_id]
        days = [date.fromisoformat(k.split(":", 1)[0]) for k in batch.slot_keys]
        assert max(days) - min(days) <= timedelta(days=recipe.keeps_days)


def test_make_fresh_recipes_are_never_carried_over(plan, library):
    """A leftover protein shake is not a thing."""
    for batch in plan.batches:
        if library.recipes[batch.recipe_id].keeps_days == 0:
            days = {k.split(":", 1)[0] for k in batch.slot_keys}
            assert len(days) == 1, f"{batch.recipe_id} spread across {days}"


def test_snacks_are_not_scaled_into_meals(plan):
    for meal in plan.all_meals():
        assert meal.servings <= _max_servings(meal.slot.meal) + 1e-9
    snacks = [m for m in plan.all_meals() if m.slot.meal is Meal.SNACK]
    assert all(m.nutrition.kcal < 620 for m in snacks)


def test_weeknight_cooking_stays_near_budget(plan):
    """The prep budget is a strong preference, not a hard constraint.

    Measured behaviour: with the shipped weights, mean weeknight active cooking
    lands around the 35 minute budget, but individual evenings go over when the
    nutrition targets need a dish that takes longer. Asserting a hard per-day cap
    would be asserting something the planner never promised — and could only be
    satisfied by making the week blander. So this checks the average holds and
    that no single evening blows out.
    """
    weeknights = [d for d in plan.days if d.day.weekday() < 5]
    mean_active = sum(d.active_min for d in weeknights) / len(weeknights)
    worst = max(d.active_min for d in weeknights)
    assert mean_active <= 35 * 1.25, [(d.day.isoformat(), d.active_min) for d in weeknights]
    assert worst <= 35 * 2.2, f"one weeknight needs {worst} min of cooking"


def test_excluded_foods_never_appear(library, normal_bp):
    targets = tg.build_targets(2200, 190, "lose", normal_bp, sex="male")
    banned = frozenset({"tofu-extra-firm", "tempeh", "salmon-fillet"})
    plan = plan_menu(library, make_request(targets, exclude_foods=banned))
    for meal in plan.all_meals():
        assert not (banned & meal.recipe.food_ids), meal.recipe.id


def test_pinned_meals_are_kept(library, normal_bp):
    targets = tg.build_targets(2200, 190, "lose", normal_bp, sex="male")
    slot = f"{START.isoformat()}:dinner:0"
    plan = plan_menu(library, make_request(targets, locked={slot: "red-lentil-dal"}))
    pinned = next(m for m in plan.all_meals() if m.slot.key == slot)
    assert pinned.recipe.id == "red-lentil-dal"


def test_elevated_bp_produces_a_lower_sodium_week(library, normal_bp, high_bp):
    """The BP integration has to change the menu, not just the displayed limit."""
    calm = plan_menu(
        library, make_request(tg.build_targets(2200, 190, "lose", normal_bp, sex="male"))
    )
    tense = plan_menu(
        library, make_request(tg.build_targets(2200, 190, "lose", high_bp, sex="male"))
    )
    calm_na = sum(d.nutrition.get("sodium_mg") for d in calm.days) / 7
    tense_na = sum(d.nutrition.get("sodium_mg") for d in tense.days) / 7
    assert tense_na < calm_na, f"{tense_na:.0f} vs {calm_na:.0f} mg/day"


def test_empty_pool_raises_a_useful_error(library, normal_bp):
    targets = tg.build_targets(2200, 190, "lose", normal_bp, sex="male")
    everything = frozenset(library.foods)
    with pytest.raises(ValueError, match="No recipes available"):
        plan_menu(library, make_request(targets, exclude_foods=everything))


# --------------------------------------------------------------------------
# batching arithmetic
# --------------------------------------------------------------------------


@pytest.mark.parametrize("yields, needed, expected", [
    (4.0, 1.0, 2.0),     # can't cook less than half a recipe
    (4.0, 2.0, 2.0),
    (4.0, 2.5, 4.0),     # rounds up to a whole recipe
    (2.0, 3.0, 3.0),     # one and a half batches
    (2.0, 5.0, 5.0),     # two and a half batches is makeable
    (2.0, 7.0, 8.0),     # beyond the largest multiple, round up to whole ones
])
def test_cook_yield_rounds_to_makeable_amounts(yields, needed, expected):
    assert cook_yield(yields, needed) == pytest.approx(expected)


# --------------------------------------------------------------------------
# shopping
# --------------------------------------------------------------------------


def test_shopping_rounds_up_to_whole_packages(library):
    """Needing 180 g of a 150 g tub means buying two, not 1.2."""
    grams = {"feta": 180.0}
    sl = build_shopping_list(library, grams, ["trader-joes"], today=date(2026, 8, 14))
    lines = [l for s in sl.stores for l in s.lines]
    assert len(lines) == 1
    assert lines[0].packages >= 1
    assert lines[0].grams_purchased >= 180.0


def test_pantry_is_deducted_before_buying(library):
    grams = {"olive-oil": 200.0}
    with_pantry = build_shopping_list(
        library, grams, ["trader-joes"], pantry={"olive-oil": 500.0},
        include_pantry_staples=True, today=date(2026, 8, 14),
    )
    assert with_pantry.pantry_used.get("olive-oil") == pytest.approx(200.0)
    assert not any(s.lines for s in with_pantry.stores)


def test_pantry_staples_are_hidden_by_default(library):
    sl = build_shopping_list(library, {"cumin-ground": 20.0}, ["trader-joes"])
    assert not any(s.lines for s in sl.stores)


def test_negligible_quantities_are_dropped(library):
    sl = build_shopping_list(library, {"feta": 0.5}, ["trader-joes"])
    assert not any(s.lines for s in sl.stores)


def test_lines_are_grouped_and_sorted_by_aisle(library):
    grams = {"feta": 200.0, "spinach-baby": 300.0, "chicken-breast": 500.0}
    sl = build_shopping_list(library, grams, ["trader-joes"], today=date(2026, 8, 14))
    store = sl.stores[0]
    ranks = [store.store.aisle_rank(a) for a in
             [g["aisle"] for g in store.as_dict()["aisles"]]]
    assert ranks == sorted(ranks)


def test_order_links_carry_only_a_search_term(library):
    # Carrots, because Safeway is the only store with an Instacart handoff and
    # it has to be a food Safeway actually stocks in the catalogue.
    sl = build_shopping_list(
        library, {"carrot": 500.0, "potato-russet": 800.0}, ["safeway"],
        delivery_partner="instacart", today=date(2026, 8, 14),
    )
    line = next(l for s in sl.stores for l in s.lines if l.food_id == "carrot")
    href = line.links.get("order", "")
    assert href.startswith("https://www.instacart.com/store/safeway/s?")

    # The real property is that the link carries a search term and nothing else:
    # exactly one query parameter, holding the product name. Substring-matching
    # the whole URL for words like "cart" would just find "instacart".
    parsed = urlparse(href)
    params = parse_qs(parsed.query)
    assert list(params) == ["k"], params
    assert "carrot" in params["k"][0].lower()
    for forbidden in ("token", "session", "auth", "user", "password"):
        assert forbidden not in parsed.query.lower()


def test_single_item_trips_are_folded_away(library):
    """One jar at a second store isn't worth a separate stop."""
    grams = {
        "spinach-baby": 300.0, "feta": 200.0, "tomato-cherry": 400.0,
        "cucumber-persian": 300.0, "tahini": 100.0,
    }
    sl = build_shopping_list(
        library, grams, ["trader-joes", "whole-foods"], today=date(2026, 8, 14)
    )
    for store in sl.stores:
        assert len(store.lines) >= 2, f"{store.store.id} has a one-item trip"


def test_cost_estimate_is_flagged_as_an_estimate(library):
    sl = build_shopping_list(library, {"feta": 200.0}, ["trader-joes"], today=date(2026, 8, 14))
    joined = " ".join(sl.notes).lower()
    assert "estimate" in joined
    assert "not live" in joined or "do not build a cart" in joined


def test_unmapped_foods_still_appear_without_a_price(library):
    """Missing from the catalogue must mean 'no price', not 'silently absent'."""
    orphan = next(
        (f.id for f in library.foods.values()
         if not f.pantry_staple and not library.products_for(f.id, list(library.stores))),
        None,
    )
    if orphan is None:
        pytest.skip("every non-staple food is mapped, which is the desired state")
    sl = build_shopping_list(library, {orphan: 200.0}, ["trader-joes"])
    assert any(l.food_id == orphan for l in sl.unmatched)
