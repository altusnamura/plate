"""Units, nutrient arithmetic and the integrity of the bundled data set."""

from __future__ import annotations

import pytest

from app.engine import units
from app.engine.models import Meal
from app.engine.nutrients import CORE, Nutrients
from app.engine.units import UnitError


# --------------------------------------------------------------------------
# units
# --------------------------------------------------------------------------


def test_mass_units_convert_for_any_food(library):
    olive = library.food("olive-oil")
    assert units.to_grams(1, "lb", olive) == pytest.approx(453.59237)
    assert units.to_grams(2, "oz", olive) == pytest.approx(56.699, rel=1e-3)


def test_tablespoon_of_oil_is_not_15_grams(library):
    """Oil is less dense than water; assuming 15 g/tbsp overstates it by 11%.

    Over a week of cooking that is a few hundred calories of phantom deficit,
    which is exactly the kind of error the calibration would then chase.
    """
    olive = library.food("olive-oil")
    assert units.to_grams(1, "tbsp", olive) == pytest.approx(13.5)


def test_volume_needs_density_or_explicit_weight(library):
    """A food with neither must refuse rather than silently guess."""
    salt = library.food("salt-kosher")
    assert salt.g_per_ml is None
    with pytest.raises(UnitError, match="g_per_ml"):
        units.to_grams(1, "cup", salt)


def test_unknown_count_unit_names_what_is_missing(library):
    broccoli = library.food("broccoli")
    with pytest.raises(UnitError) as exc:
        units.to_grams(1, "punnet", broccoli)
    assert "unit_g" in str(exc.value)
    assert "punnet" in str(exc.value)


def test_count_units_use_declared_weights(library):
    eggs = library.food("eggs")
    assert units.to_grams(3, "ea", eggs) == pytest.approx(150.0)
    garlic = library.food("garlic")
    assert units.to_grams(4, "clove", garlic) == pytest.approx(12.0)


def test_humanise_prefers_fractions(library):
    lemon = library.food("lemon")
    assert units.humanise(42.0, lemon) == "½"
    assert units.humanise(84.0, lemon) == "1"


# --------------------------------------------------------------------------
# nutrients
# --------------------------------------------------------------------------


def test_addition_sums_core_nutrients():
    a = Nutrients.from_mapping({n: 1.0 for n in CORE})
    b = Nutrients.from_mapping({n: 2.0 for n in CORE})
    total = a + b
    assert total.get("protein_g") == pytest.approx(3.0)


def test_coverage_tracks_which_components_knew_a_nutrient():
    """Half the calories from a food that knows magnesium gives 50% coverage."""
    known = Nutrients.from_mapping({**{n: 0.0 for n in CORE}, "kcal": 100.0, "magnesium_mg": 40.0})
    unknown = Nutrients.from_mapping({**{n: 0.0 for n in CORE}, "kcal": 100.0})
    total = known + unknown
    assert total.get("magnesium_mg") == pytest.approx(40.0)
    assert total.coverage("magnesium_mg") == pytest.approx(0.5)
    # Below the threshold the number is withheld rather than shown as a shortfall.
    assert total.known("magnesium_mg", min_coverage=0.7) is None
    assert total.known("magnesium_mg", min_coverage=0.4) == pytest.approx(40.0)


def test_core_nutrients_are_always_fully_covered():
    n = Nutrients.from_mapping({x: 1.0 for x in CORE})
    assert all(n.coverage(x) == 1.0 for x in CORE)


def test_fiber_is_billed_at_two_kcal_per_gram():
    """Naive 4/4/9 overstates leafy greens badly; the loader's check depends on this."""
    spinach = Nutrients.from_mapping(
        {"kcal": 23, "protein_g": 2.9, "fat_g": 0.4, "satfat_g": 0.06,
         "carb_g": 3.6, "fiber_g": 2.2, "sugar_g": 0.4, "sodium_mg": 79, "potassium_mg": 558}
    )
    assert spinach.kcal_from_macros == pytest.approx(25.2, abs=0.5)


def test_scaling_is_linear():
    n = Nutrients.from_mapping({**{x: 0.0 for x in CORE}, "kcal": 100.0, "protein_g": 10.0})
    assert n.scaled(2.5).get("protein_g") == pytest.approx(25.0)


# --------------------------------------------------------------------------
# the shipped data
# --------------------------------------------------------------------------


def test_library_loads_without_warnings(library):
    """Warnings are for the user; in CI they mean the shipped data drifted."""
    unexpected = [w for w in library.warnings if "no store product mapping" not in w]
    assert unexpected == [], "\n".join(unexpected)


def test_every_recipe_ingredient_resolves(library):
    for recipe in library.recipes.values():
        for food_id in recipe.food_ids:
            assert food_id in library.foods, f"{recipe.id} -> {food_id}"


def test_recipe_calories_are_plausible(library):
    for recipe in library.recipes.values():
        kcal = recipe.nutrition.kcal
        assert 90 <= kcal <= 1200, f"{recipe.id} is {kcal:.0f} kcal per serving"


def test_vegetarian_is_derived_not_declared(library):
    """A recipe cannot claim to be vegetarian while containing meat."""
    for recipe in library.recipes.values():
        if recipe.vegetarian:
            assert all(library.foods[f].vegetarian for f in recipe.food_ids), recipe.id


def test_parmesan_is_not_vegetarian(library):
    """Animal rennet. The lunch rule depends on getting this right."""
    assert library.food("parmesan").vegetarian is False


def test_enough_vegetarian_lunches_to_satisfy_the_default_ratio(library):
    """The default asks for 85% of lunches meat-free; the pool must support it.

    Seven lunches a week at 85% needs six vegetarian ones from a pool with enough
    variety that they aren't all the same dish.
    """
    veg = [r for r in library.recipes_for(Meal.LUNCH) if r.vegetarian]
    assert len(veg) >= 10


def test_every_meal_has_a_usable_pool(library):
    for meal in Meal:
        assert len(library.recipes_for(meal)) >= 5, meal


def test_high_protein_vegetarian_lunches_exist(library):
    """The hard part of a vegetarian lunch is protein, not variety.

    Without a decent number of 25g+ options the planner cannot hit both the
    protein floor and the vegetarian ratio, and will quietly sacrifice one.
    """
    good = [
        r for r in library.recipes_for(Meal.LUNCH)
        if r.vegetarian and r.nutrition.get("protein_g") >= 25
    ]
    assert len(good) >= 8, f"only {len(good)} vegetarian lunches clear 25 g protein"


def test_no_orphan_products(library):
    for product in library.products:
        assert product.food_id in library.foods
        assert product.store_id in library.stores


def test_product_packages_convert_to_grams(library):
    for product in library.products:
        food = library.foods[product.food_id]
        assert product.package_grams(food) > 0, f"{product.store_id}/{product.name}"


def test_dry_grains_are_recorded_dry(library):
    """A cooked-basis rice record would understate calories by roughly 3x."""
    assert library.food("rice-brown").per_100g.kcal > 300
    assert library.food("oats-rolled").per_100g.kcal > 300
    assert library.food("lentils-red").per_100g.kcal > 300
