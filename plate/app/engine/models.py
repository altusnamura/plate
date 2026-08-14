"""Foods, recipes, stores and products.

Design decision worth knowing: recipe nutrition is *never* stored, it is always
computed from the ingredient list. Hand-written per-recipe macros drift out of
sync the moment someone edits an ingredient, and they make portion scaling a lie
(scale a recipe to 1.3x and stored macros no longer describe the plate). The
cost is that every ingredient has to resolve to a food record, which the loader
enforces at startup.

Vegetarian/vegan status is derived the same way, from the ingredients, so a
recipe cannot claim to be vegetarian while containing anchovies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from functools import cached_property
from typing import Mapping, Sequence

from . import units
from .nutrients import CORE, Nutrients


class Meal(str, Enum):
    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SNACK = "snack"

    @property
    def label(self) -> str:
        return self.value.capitalize()


# Aisle categories, ordered the way you actually walk a US grocery store:
# perimeter first (produce, then the back wall), centre aisles last. Shopping
# lists sort by this so you don't backtrack.
AISLE_ORDER: tuple[str, ...] = (
    "produce",
    "bakery",
    "deli",
    "meat",
    "seafood",
    "dairy",
    "eggs",
    "refrigerated",
    "frozen",
    "grains",
    "legumes",
    "canned",
    "pasta_sauce",
    "condiments",
    "oils_vinegar",
    "spices",
    "nuts_seeds",
    "baking",
    "beverages",
    "other",
)


@dataclass(frozen=True, slots=True)
class Food:
    """One ingredient, with nutrition expressed per 100 g."""

    id: str
    name: str
    aisle: str = "other"
    per_100g: Nutrients = field(default_factory=Nutrients.zero)
    # Density, needed for cups/ml of anything pourable.
    g_per_ml: float | None = None
    # Weights for count units and for volumes that pack unpredictably.
    unit_g: Mapping[str, float] = field(default_factory=dict)
    display_unit: str | None = None
    vegetarian: bool = True
    vegan: bool = False
    # Free-form: b12_source, heme_iron, high_sodium, omega3, staple, dash_bonus
    tags: frozenset[str] = frozenset()
    aliases: tuple[str, ...] = ()
    # DASH food group and the gram weight of one DASH serving. Set on the food
    # rather than inferred from the aisle, because "produce" splits between
    # vegetables and fruit and guessing gets that wrong half the time.
    dash_group: str | None = None
    dash_serving_g: float | None = None
    # Things you are assumed to own; excluded from shopping lists unless the
    # user marks them out of stock.
    pantry_staple: bool = False
    # Days a cooked portion keeps in the fridge, for batch-cook planning.
    keeps_days: int = 4
    notes: str = ""

    def nutrients_for(self, grams: float) -> Nutrients:
        return self.per_100g.scaled(grams / 100.0)

    def to_grams(self, qty: float, unit: str) -> float:
        return units.to_grams(qty, unit, self)


@dataclass(frozen=True, slots=True)
class Ingredient:
    """A quantity of a food inside a recipe."""

    food_id: str
    qty: float
    unit: str
    # Cosmetic instruction that does not change the food: "diced", "divided".
    prep: str = ""
    # True for garnishes and "to taste" items, which the planner is allowed to
    # drop from a shopping list if the food is a pantry staple.
    optional: bool = False

    def grams(self, foods: Mapping[str, Food]) -> float:
        return foods[self.food_id].to_grams(self.qty, self.unit)


@dataclass
class Recipe:
    """A dish. Nutrition is derived; see module docstring.

    Deliberately *not* ``slots=True``: the derived values below are
    ``cached_property``, which needs an instance ``__dict__`` to memoise into.
    There are only a few dozen recipes, so the memory that slots would save is
    irrelevant next to recomputing every ingredient sum on each access — and the
    planner reads ``nutrition`` thousands of times per plan.
    """

    id: str
    title: str
    meals: tuple[Meal, ...]
    ingredients: tuple[Ingredient, ...]
    servings: float = 2.0
    prep_min: int = 10
    cook_min: int = 0
    cuisine: str = "other"
    tags: frozenset[str] = frozenset()
    steps: tuple[str, ...] = ()
    notes: str = ""
    # How many days the cooked dish keeps, which caps batch-cooking reuse.
    keeps_days: int = 3
    # Survives being carried to work at room temperature / reheats acceptably.
    packable: bool = True
    source: str = "builtin"
    source_url: str = ""

    # Injected by the library so a Recipe is self-sufficient downstream.
    _foods: Mapping[str, Food] = field(default_factory=dict, repr=False)

    @property
    def total_min(self) -> int:
        return self.prep_min + self.cook_min

    @cached_property
    def ingredient_grams(self) -> dict[str, float]:
        """food_id -> grams for the whole recipe (all servings)."""
        out: dict[str, float] = {}
        for ing in self.ingredients:
            out[ing.food_id] = out.get(ing.food_id, 0.0) + ing.grams(self._foods)
        return out

    @cached_property
    def nutrition_total(self) -> Nutrients:
        return Nutrients.total(
            self._foods[fid].nutrients_for(g) for fid, g in self.ingredient_grams.items()
        )

    @cached_property
    def nutrition(self) -> Nutrients:
        """Per serving, which is what every planner decision uses."""
        return self.nutrition_total.scaled(1.0 / max(self.servings, 0.01))

    @cached_property
    def vegetarian(self) -> bool:
        return all(self._foods[fid].vegetarian for fid in self.ingredient_grams)

    @cached_property
    def vegan(self) -> bool:
        return all(self._foods[fid].vegan for fid in self.ingredient_grams)

    @cached_property
    def food_ids(self) -> frozenset[str]:
        return frozenset(self.ingredient_grams)

    @cached_property
    def aisles(self) -> frozenset[str]:
        return frozenset(self._foods[fid].aisle for fid in self.ingredient_grams)

    def scaled_grams(self, servings: float) -> dict[str, float]:
        """Ingredient grams needed to produce ``servings`` portions."""
        factor = servings / max(self.servings, 0.01)
        return {fid: g * factor for fid, g in self.ingredient_grams.items()}

    def suits(self, meal: Meal) -> bool:
        return meal in self.meals

    def summary(self) -> dict[str, object]:
        n = self.nutrition
        return {
            "id": self.id,
            "title": self.title,
            "meals": [m.value for m in self.meals],
            "servings": self.servings,
            "prep_min": self.prep_min,
            "cook_min": self.cook_min,
            "total_min": self.total_min,
            "cuisine": self.cuisine,
            "tags": sorted(self.tags),
            "vegetarian": self.vegetarian,
            "vegan": self.vegan,
            "packable": self.packable,
            "keeps_days": self.keeps_days,
            "per_serving": {k: round(n.get(k), 1) for k in CORE},
            "source": self.source,
            "source_url": self.source_url,
        }


@dataclass(frozen=True, slots=True)
class Store:
    """A physical store, plus how to hand a search off to a delivery app."""

    id: str
    name: str
    short: str
    # Aisle ids in the order you encounter them in this chain's layout.
    aisle_order: tuple[str, ...] = AISLE_ORDER
    # Templates take a single {q} placeholder, url-encoded by the caller.
    search_url: str = ""
    instacart_url: str = ""
    amazon_url: str = ""
    delivers: bool = True
    notes: str = ""

    def aisle_rank(self, aisle: str) -> int:
        try:
            return self.aisle_order.index(aisle)
        except ValueError:
            return len(self.aisle_order)


@dataclass(frozen=True, slots=True)
class Product:
    """A specific package of a food at a specific store.

    ``price_usd`` is a user-maintained estimate. Nothing in this app fetches
    live prices; see docs/GROCERY.md for why.
    """

    store_id: str
    food_id: str
    name: str
    package_qty: float
    package_unit: str
    price_usd: float | None = None
    # Overrides the food's aisle when a chain shelves it somewhere odd.
    aisle: str | None = None
    # Sold loose, so you can buy a fractional package (produce by weight).
    by_weight: bool = False
    organic: bool = False
    notes: str = ""
    price_updated: str = ""

    def package_grams(self, food: Food) -> float:
        return food.to_grams(self.package_qty, self.package_unit)

    def unit_price_per_100g(self, food: Food) -> float | None:
        if self.price_usd is None:
            return None
        g = self.package_grams(food)
        if g <= 0:
            return None
        return self.price_usd * 100.0 / g


@dataclass(frozen=True, slots=True)
class Library:
    """Everything loaded from YAML, resolved and validated."""

    foods: Mapping[str, Food]
    recipes: Mapping[str, Recipe]
    stores: Mapping[str, Store]
    products: Sequence[Product]
    warnings: tuple[str, ...] = ()

    def recipes_for(self, meal: Meal) -> list[Recipe]:
        return [r for r in self.recipes.values() if r.suits(meal)]

    def products_for(self, food_id: str, store_ids: Sequence[str]) -> list[Product]:
        allowed = set(store_ids)
        return [p for p in self.products if p.food_id == food_id and p.store_id in allowed]

    def food(self, food_id: str) -> Food:
        return self.foods[food_id]
