"""Load and validate the YAML data set.

Two layers: the bundled data that ships with the add-on, then the user's own
files in ``/config`` overlaid on top by id. That means you can correct one of my
nutrition estimates or replace a recipe without forking the add-on, and your
edits survive updates.

Validation is strict and happens once at startup. A recipe referencing a food
that doesn't exist, or a quantity in a unit the food can't convert, is a hard
error — those mistakes are invisible in the UI but corrupt every calorie
downstream. Softer problems (a food whose stated calories disagree with its
macros) become warnings surfaced on the Insight screen.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .models import AISLE_ORDER, Food, Ingredient, Library, Meal, Product, Recipe, Store
from .nutrients import CORE, Nutrients
from .units import UnitError

log = logging.getLogger(__name__)

# A food's stated kcal should roughly match 4/4/9 over its macros. Fibre,
# sugar alcohols and rounding make exact agreement impossible, so this is a
# generous band that still catches transposed digits.
ATWATER_TOLERANCE = 0.22
ATWATER_FLOOR_KCAL = 25.0


class DataError(Exception):
    """The data set is internally inconsistent and cannot be used."""


def _read_yaml_files(*dirs: Path) -> list[tuple[Path, Any]]:
    out: list[tuple[Path, Any]] = []
    for d in dirs:
        if not d or not d.is_dir():
            continue
        for path in sorted(d.rglob("*.y*ml")):
            if path.name.startswith("_"):
                continue
            try:
                with path.open("r", encoding="utf-8") as fh:
                    doc = yaml.safe_load(fh)
            except yaml.YAMLError as exc:
                raise DataError(f"{path}: invalid YAML: {exc}") from exc
            if doc is not None:
                out.append((path, doc))
    return out


def _as_records(doc: Any, key: str, path: Path) -> list[Mapping[str, Any]]:
    """Accept either a bare list or a mapping with a named list under ``key``.

    Raises if the file contains nothing recognisable, which catches typos in the
    top-level key.
    """
    records = _records_under(doc, key)
    if records is None:
        raise DataError(f"{path}: expected a list of {key} or a mapping with '{key}'")
    return records


def _records_under(doc: Any, key: str) -> list[Mapping[str, Any]] | None:
    """Records for ``key``, or ``None`` if this document doesn't hold any.

    Stores and products share a directory (and may share a file), so the loader
    makes two passes over it and each pass has to tolerate documents that hold
    only the other kind.
    """
    if isinstance(doc, list):
        return [r for r in doc if isinstance(r, Mapping)]
    if isinstance(doc, Mapping):
        if isinstance(doc.get(key), list):
            return [r for r in doc[key] if isinstance(r, Mapping)]
        # A single record per file, the natural shape for recipes.
        if "id" in doc and key != "products":
            return [doc]
        # A recognised document that simply has no records of this kind.
        if any(k in doc for k in ("foods", "recipes", "stores", "products")):
            return []
    return None


def _is_product_record(rec: Mapping[str, Any]) -> bool:
    """Products are distinguishable by carrying a package size and a food id."""
    return "package_qty" in rec or "food_id" in rec or "package_unit" in rec


# --------------------------------------------------------------------------
# foods
# --------------------------------------------------------------------------


def _load_food(rec: Mapping[str, Any], path: Path, warnings: list[str]) -> Food:
    fid = rec.get("id")
    if not fid:
        raise DataError(f"{path}: a food record has no 'id'")

    raw_nutr = rec.get("per_100g") or rec.get("nutrition") or {}
    if not isinstance(raw_nutr, Mapping):
        raise DataError(f"{path}: food '{fid}' has a non-mapping per_100g")

    missing = [n for n in CORE if raw_nutr.get(n) is None]
    if missing:
        raise DataError(
            f"{path}: food '{fid}' is missing required nutrients: {', '.join(missing)}. "
            "Core nutrients cannot be estimated at runtime; add them or delete the food."
        )

    nutr = Nutrients.from_mapping(raw_nutr)
    tags = frozenset(str(t) for t in (rec.get("tags") or ()))

    # Some foods legitimately break the 4/4/9 reconstruction: cocoa's
    # unavailable carbohydrate, baking powder's mineral salts, the ethanol in
    # vanilla extract. Those declare `atwater_exempt` rather than have us either
    # fudge the data or live with a permanent false alarm.
    stated = nutr.kcal
    if stated >= ATWATER_FLOOR_KCAL and "atwater_exempt" not in tags:
        implied = nutr.kcal_from_macros
        if implied > 0 and abs(implied - stated) / stated > ATWATER_TOLERANCE:
            warnings.append(
                f"food '{fid}': stated {stated:.0f} kcal/100g but macros imply "
                f"{implied:.0f} — check the record ({path.name})"
            )

    aisle = str(rec.get("aisle", "other"))
    if aisle not in AISLE_ORDER:
        warnings.append(f"food '{fid}': unknown aisle '{aisle}', treating as 'other'")
        aisle = "other"

    unit_g = {str(k): float(v) for k, v in (rec.get("unit_g") or {}).items()}
    vegan = bool(rec.get("vegan", False))
    # Vegan implies vegetarian; a record claiming otherwise is a typo.
    vegetarian = bool(rec.get("vegetarian", True)) or vegan

    return Food(
        id=str(fid),
        name=str(rec.get("name", fid)),
        aisle=aisle,
        per_100g=nutr,
        g_per_ml=_opt_float(rec.get("g_per_ml")),
        unit_g=unit_g,
        display_unit=_opt_str(rec.get("display_unit")),
        vegetarian=vegetarian,
        vegan=vegan,
        tags=tags,
        aliases=tuple(str(a) for a in (rec.get("aliases") or ())),
        dash_group=_opt_str(rec.get("dash_group")),
        dash_serving_g=_opt_float(rec.get("dash_serving_g")),
        pantry_staple=bool(rec.get("pantry_staple", False)),
        keeps_days=int(rec.get("keeps_days", 4)),
        notes=str(rec.get("notes", "")),
    )


# --------------------------------------------------------------------------
# recipes
# --------------------------------------------------------------------------


def _load_recipe(
    rec: Mapping[str, Any], path: Path, foods: Mapping[str, Food], warnings: list[str]
) -> Recipe:
    rid = rec.get("id")
    if not rid:
        raise DataError(f"{path}: a recipe record has no 'id'")

    raw_meals = rec.get("meals") or rec.get("meal") or []
    if isinstance(raw_meals, str):
        raw_meals = [raw_meals]
    try:
        meals = tuple(Meal(str(m).lower()) for m in raw_meals)
    except ValueError as exc:
        raise DataError(f"{path}: recipe '{rid}' has an unknown meal: {exc}") from exc
    if not meals:
        raise DataError(f"{path}: recipe '{rid}' lists no meals")

    raw_ings = rec.get("ingredients") or []
    if not raw_ings:
        raise DataError(f"{path}: recipe '{rid}' has no ingredients")

    ings: list[Ingredient] = []
    for entry in raw_ings:
        if not isinstance(entry, Mapping):
            raise DataError(f"{path}: recipe '{rid}' has a malformed ingredient: {entry!r}")
        food_id = str(entry.get("item") or entry.get("food") or entry.get("food_id") or "")
        if food_id not in foods:
            raise DataError(
                f"{path}: recipe '{rid}' references unknown food '{food_id}'. "
                "Add it to a foods YAML file or fix the id."
            )
        qty = _opt_float(entry.get("qty"))
        unit = _opt_str(entry.get("unit"))
        if qty is None or unit is None:
            raise DataError(
                f"{path}: recipe '{rid}' ingredient '{food_id}' needs both qty and unit"
            )
        ing = Ingredient(
            food_id=food_id,
            qty=qty,
            unit=unit,
            prep=str(entry.get("prep", "")),
            optional=bool(entry.get("optional", False)),
        )
        # Convert now so a bad unit is a startup error, not a 500 at plan time.
        try:
            ing.grams(foods)
        except UnitError as exc:
            raise DataError(f"{path}: recipe '{rid}': {exc}") from exc
        ings.append(ing)

    servings = float(rec.get("servings", 2))
    if servings <= 0:
        raise DataError(f"{path}: recipe '{rid}' has servings <= 0")

    recipe = Recipe(
        id=str(rid),
        title=str(rec.get("title", rid)),
        meals=meals,
        ingredients=tuple(ings),
        servings=servings,
        prep_min=int(rec.get("prep_min", 10)),
        cook_min=int(rec.get("cook_min", 0)),
        cuisine=str(rec.get("cuisine", "other")),
        tags=frozenset(str(t) for t in (rec.get("tags") or ())),
        steps=tuple(str(s) for s in (rec.get("steps") or ())),
        notes=str(rec.get("notes", "")),
        keeps_days=int(rec.get("keeps_days", 3)),
        packable=bool(rec.get("packable", True)),
        source=str(rec.get("source", "builtin")),
        source_url=str(rec.get("source_url", "")),
        _foods=foods,
    )

    kcal = recipe.nutrition.kcal
    if not 80 <= kcal <= 1600:
        warnings.append(
            f"recipe '{rid}': {kcal:.0f} kcal per serving looks implausible — "
            f"check servings ({servings:g}) and quantities ({path.name})"
        )

    # A recipe tagged vegetarian that isn't, is a data bug worth surfacing:
    # the lunch rule depends on the derived value, not the tag.
    if "vegetarian" in recipe.tags and not recipe.vegetarian:
        offenders = ", ".join(
            fid for fid in sorted(recipe.food_ids) if not foods[fid].vegetarian
        )
        warnings.append(
            f"recipe '{rid}' is tagged vegetarian but contains: {offenders}. "
            "The derived value wins."
        )

    return recipe


# --------------------------------------------------------------------------
# stores and products
# --------------------------------------------------------------------------


def _load_store(rec: Mapping[str, Any], path: Path) -> Store:
    sid = rec.get("id")
    if not sid:
        raise DataError(f"{path}: a store record has no 'id'")
    order = tuple(str(a) for a in (rec.get("aisle_order") or AISLE_ORDER))
    return Store(
        id=str(sid),
        name=str(rec.get("name", sid)),
        short=str(rec.get("short", str(sid)[:3].upper())),
        aisle_order=order,
        search_url=str(rec.get("search_url", "")),
        instacart_url=str(rec.get("instacart_url", "")),
        amazon_url=str(rec.get("amazon_url", "")),
        delivers=bool(rec.get("delivers", True)),
        notes=str(rec.get("notes", "")),
    )


def _load_product(
    rec: Mapping[str, Any],
    path: Path,
    foods: Mapping[str, Food],
    stores: Mapping[str, Store],
    warnings: list[str],
) -> Product | None:
    food_id = str(rec.get("food_id") or rec.get("item") or "")
    store_id = str(rec.get("store_id") or rec.get("store") or "")
    if food_id not in foods:
        warnings.append(f"{path.name}: product references unknown food '{food_id}', skipped")
        return None
    if store_id not in stores:
        warnings.append(f"{path.name}: product references unknown store '{store_id}', skipped")
        return None

    product = Product(
        store_id=store_id,
        food_id=food_id,
        name=str(rec.get("name", foods[food_id].name)),
        package_qty=float(rec.get("package_qty", 1)),
        package_unit=str(rec.get("package_unit", "ea")),
        price_usd=_opt_float(rec.get("price_usd")),
        aisle=_opt_str(rec.get("aisle")),
        by_weight=bool(rec.get("by_weight", False)),
        organic=bool(rec.get("organic", False)),
        notes=str(rec.get("notes", "")),
        price_updated=str(rec.get("price_updated", "")),
    )
    try:
        grams = product.package_grams(foods[food_id])
    except UnitError as exc:
        warnings.append(f"{path.name}: product '{product.name}': {exc}, skipped")
        return None
    if grams <= 0:
        warnings.append(f"{path.name}: product '{product.name}' has zero package weight, skipped")
        return None
    return product


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def load_library(builtin_dir: Path, user_dir: Path | None = None) -> Library:
    """Load bundled data, overlay the user's, validate, return.

    Later records win on id collision, and ``user_dir`` is read after
    ``builtin_dir``, which is what makes user overrides work.
    """
    warnings: list[str] = []

    def dirs(sub: str) -> list[Path]:
        out = [builtin_dir / sub]
        if user_dir:
            out.append(user_dir / sub)
        return out

    foods: dict[str, Food] = {}
    for path, doc in _read_yaml_files(*dirs("foods")):
        for rec in _as_records(doc, "foods", path):
            food = _load_food(rec, path, warnings)
            if food.id in foods:
                log.debug("food %s overridden by %s", food.id, path)
            foods[food.id] = food
    if not foods:
        raise DataError(f"no foods found under {builtin_dir / 'foods'}")

    # Stores and products share the `stores/` directory, so read it once and
    # split the records by shape. Stores must land first: a product referencing
    # an unknown store is dropped with a warning.
    store_docs = _read_yaml_files(*dirs("stores"))

    stores: dict[str, Store] = {}
    for path, doc in store_docs:
        for rec in _records_under(doc, "stores") or ():
            if _is_product_record(rec):
                continue
            store = _load_store(rec, path)
            stores[store.id] = store

    products: list[Product] = []
    seen_products: set[tuple[str, str, str]] = set()
    for path, doc in store_docs:
        for rec in _records_under(doc, "products") or ():
            if not _is_product_record(rec):
                continue
            product = _load_product(rec, path, foods, stores, warnings)
            if product is None:
                continue
            key = (product.store_id, product.food_id, product.name)
            if key in seen_products:
                continue
            seen_products.add(key)
            products.append(product)

    recipes: dict[str, Recipe] = {}
    for path, doc in _read_yaml_files(*dirs("recipes")):
        for rec in _as_records(doc, "recipes", path):
            recipe = _load_recipe(rec, path, foods, warnings)
            recipes[recipe.id] = recipe
    if not recipes:
        raise DataError(f"no recipes found under {builtin_dir / 'recipes'}")

    _warn_unpurchasable(recipes, foods, stores, products, warnings)

    log.info(
        "library: %d foods, %d recipes, %d stores, %d products, %d warnings",
        len(foods), len(recipes), len(stores), len(products), len(warnings),
    )
    for w in warnings:
        log.warning("data: %s", w)

    return Library(
        foods=foods,
        recipes=recipes,
        stores=stores,
        products=products,
        warnings=tuple(warnings),
    )


def _warn_unpurchasable(
    recipes: Mapping[str, Recipe],
    foods: Mapping[str, Food],
    stores: Mapping[str, Store],
    products: Iterable[Product],
    warnings: list[str],
) -> None:
    """Flag foods a recipe needs that no configured store stocks.

    Not fatal — the shopping list falls back to a generic line item — but it is
    the most common reason a list looks incomplete, so it belongs in the UI.
    """
    stocked = {p.food_id for p in products}
    needed: set[str] = set()
    for r in recipes.values():
        needed |= r.food_ids
    orphans = sorted(
        fid for fid in needed - stocked if not foods[fid].pantry_staple
    )
    if orphans:
        preview = ", ".join(orphans[:8])
        more = f" (+{len(orphans) - 8} more)" if len(orphans) > 8 else ""
        warnings.append(
            f"{len(orphans)} recipe ingredients have no store product mapping, so they "
            f"appear on lists without a price: {preview}{more}"
        )


def _opt_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    return float(v)


def _opt_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
