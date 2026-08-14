"""Turn a menu plan into per-store shopping lists.

Three things happen here that a naive "sum the ingredients" list gets wrong.

**Packages, not grams.** A recipe wants 180 g of feta; the store sells it in a
150 g tub. You buy two. Ignoring that understates cost by a third and leaves you
short at the counter, so every quantity is rounded up to whole packages except
for genuinely loose goods (produce by weight, bulk bins), which round to a
sensible increment instead.

**Store assignment.** Each ingredient goes to whichever enabled store gives the
best price per gram, with a tie-break on store preference order and an override
on the food record. Then single-item trips get folded away: if the whole Safeway
list is one jar of tahini that Whole Foods also stocks, it moves rather than
sending you across town for it.

**What this is not.** No prices are fetched. There is no Trader Joe's API, Whole
Foods is behind Amazon's authentication, and the unofficial Safeway endpoints are
both brittle and against their terms. Every price here is an estimate you
maintain in YAML, shown with the date it was last touched so a stale number looks
stale. The "order" links are plain search handoffs into Instacart, Amazon Fresh
or the retailer's own site — they carry a search term and nothing else, they do
not log in, and they do not build a cart. See docs/GROCERY.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Mapping, Sequence
from urllib.parse import quote_plus

from .models import Food, Library, Product, Store
from .units import humanise

# Loose goods round to these increments rather than to whole packages.
BY_WEIGHT_STEP_G = 25.0

# Below this, don't bother listing an ingredient separately — it's a pinch of
# something you either have or won't miss.
NEGLIGIBLE_G = 2.0

# A store trip is worth making for more than this many items; below it, we try
# to move the items elsewhere.
MIN_ITEMS_PER_TRIP = 2


@dataclass(frozen=True, slots=True)
class Line:
    """One thing to buy at one store."""

    food_id: str
    name: str
    aisle: str
    grams_needed: float
    grams_purchased: float
    packages: float
    package_text: str
    quantity_text: str
    est_cost: float | None
    price_stale_days: int | None
    product_name: str | None
    by_weight: bool
    links: Mapping[str, str] = field(default_factory=dict)
    # Recipes that drove this line, so the list explains itself.
    used_by: tuple[str, ...] = ()
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "food_id": self.food_id,
            "name": self.name,
            "aisle": self.aisle,
            "grams_needed": round(self.grams_needed, 1),
            "grams_purchased": round(self.grams_purchased, 1),
            "packages": self.packages,
            "package_text": self.package_text,
            "quantity_text": self.quantity_text,
            "est_cost": round(self.est_cost, 2) if self.est_cost is not None else None,
            "price_stale_days": self.price_stale_days,
            "product_name": self.product_name,
            "by_weight": self.by_weight,
            "links": dict(self.links),
            "used_by": list(self.used_by),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class StoreList:
    store: Store
    lines: tuple[Line, ...]
    subtotal: float | None
    priced_lines: int
    unpriced_lines: int

    def as_dict(self) -> dict[str, object]:
        by_aisle: dict[str, list[dict[str, object]]] = {}
        for line in self.lines:
            by_aisle.setdefault(line.aisle, []).append(line.as_dict())
        return {
            "store_id": self.store.id,
            "store_name": self.store.name,
            "store_short": self.store.short,
            "delivers": self.store.delivers,
            "notes": self.store.notes,
            "subtotal": round(self.subtotal, 2) if self.subtotal is not None else None,
            "priced_lines": self.priced_lines,
            "unpriced_lines": self.unpriced_lines,
            "item_count": len(self.lines),
            "aisles": [
                {"aisle": aisle, "label": _aisle_label(aisle), "lines": lines}
                for aisle, lines in sorted(
                    by_aisle.items(), key=lambda kv: self.store.aisle_rank(kv[0])
                )
            ],
        }


@dataclass(frozen=True, slots=True)
class ShoppingList:
    stores: tuple[StoreList, ...]
    unmatched: tuple[Line, ...]
    pantry_used: Mapping[str, float]
    total_estimate: float | None
    coverage: float
    generated: datetime
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "stores": [s.as_dict() for s in self.stores],
            "unmatched": [l.as_dict() for l in self.unmatched],
            "pantry_used": {k: round(v, 1) for k, v in self.pantry_used.items()},
            "total_estimate": (
                round(self.total_estimate, 2) if self.total_estimate is not None else None
            ),
            "price_coverage": round(self.coverage, 2),
            "generated": self.generated.isoformat(timespec="seconds"),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------
# store choice
# --------------------------------------------------------------------------


def _choose_product(
    food: Food,
    candidates: Sequence[Product],
    store_preference: Sequence[str],
) -> Product | None:
    """Pick the package to buy: cheapest per gram, then store preference.

    Foods can force a store with a ``preferred_store:<id>`` tag — for the cases
    where price isn't the point and you just want the Trader Joe's version.
    """
    if not candidates:
        return None

    forced = next(
        (t.split(":", 1)[1] for t in food.tags if t.startswith("preferred_store:")), None
    )
    if forced:
        preferred = [p for p in candidates if p.store_id == forced]
        if preferred:
            candidates = preferred

    def rank(p: Product) -> tuple[float, int]:
        unit = p.unit_price_per_100g(food)
        try:
            pref = store_preference.index(p.store_id)
        except ValueError:
            pref = len(store_preference)
        # Unpriced products sort last on price but are still usable.
        return (unit if unit is not None else float("inf"), pref)

    return min(candidates, key=rank)


def _packages_for(food: Food, product: Product, grams: float) -> tuple[float, float]:
    """Package count and grams actually purchased."""
    package_g = product.package_grams(food)
    if package_g <= 0:
        return 1.0, grams
    if product.by_weight:
        # Loose goods: buy roughly what's needed, rounded to a workable amount.
        purchased = max(BY_WEIGHT_STEP_G, math.ceil(grams / BY_WEIGHT_STEP_G) * BY_WEIGHT_STEP_G)
        return round(purchased / package_g, 2), purchased
    count = max(1, math.ceil(grams / package_g - 1e-6))
    return float(count), count * package_g


def _links(store: Store, food: Food, product: Product | None, partner: str) -> dict[str, str]:
    """Search handoffs. Query terms only — no cart, no credentials, no session."""
    term = product.name if product else food.name
    q = quote_plus(term)
    out: dict[str, str] = {}
    if store.search_url:
        out["store"] = store.search_url.replace("{q}", q)
    if partner == "instacart" and store.instacart_url:
        out["order"] = store.instacart_url.replace("{q}", q)
    elif partner == "amazon-fresh" and store.amazon_url:
        out["order"] = store.amazon_url.replace("{q}", q)
    return out


def _stale_days(product: Product | None, today: date) -> int | None:
    if product is None or not product.price_updated:
        return None
    try:
        when = date.fromisoformat(product.price_updated)
    except ValueError:
        return None
    return max(0, (today - when).days)


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------


def build_shopping_list(
    library: Library,
    grams_by_food: Mapping[str, float],
    enabled_stores: Sequence[str],
    pantry: Mapping[str, float] | None = None,
    delivery_partner: str = "instacart",
    include_pantry_staples: bool = False,
    used_by: Mapping[str, Sequence[str]] | None = None,
    today: date | None = None,
) -> ShoppingList:
    """Build per-store lists from the total grams a plan needs.

    ``pantry`` maps food id to grams already on hand and is deducted first.
    ``used_by`` maps food id to recipe titles, purely so each line can say what
    it's for.
    """
    today = today or date.today()
    pantry = dict(pantry or {})
    used_by = used_by or {}
    notes: list[str] = []

    stores = [library.stores[s] for s in enabled_stores if s in library.stores]
    if not stores:
        notes.append("No stores are enabled, so nothing could be assigned.")
        return ShoppingList((), (), {}, None, 0.0, datetime.now(), tuple(notes))

    preference = [s.id for s in stores]
    by_store: dict[str, list[Line]] = {s.id: [] for s in stores}
    unmatched: list[Line] = []
    pantry_used: dict[str, float] = {}

    for food_id, grams in sorted(grams_by_food.items()):
        if grams <= NEGLIGIBLE_G:
            continue
        food = library.foods.get(food_id)
        if food is None:
            continue
        if food.pantry_staple and not include_pantry_staples:
            continue

        # Deduct what's already in the kitchen.
        on_hand = pantry.get(food_id, 0.0)
        if on_hand > 0:
            take = min(on_hand, grams)
            pantry_used[food_id] = take
            pantry[food_id] = on_hand - take
            grams -= take
            if grams <= NEGLIGIBLE_G:
                continue

        candidates = library.products_for(food_id, preference)
        product = _choose_product(food, candidates, preference)

        if product is None:
            # No mapping: still list it, without a price or a store.
            unmatched.append(
                Line(
                    food_id=food_id,
                    name=food.name,
                    aisle=food.aisle,
                    grams_needed=grams,
                    grams_purchased=grams,
                    packages=1.0,
                    package_text="",
                    quantity_text=humanise(grams, food),
                    est_cost=None,
                    price_stale_days=None,
                    product_name=None,
                    by_weight=False,
                    links={},
                    used_by=tuple(used_by.get(food_id, ())),
                    note="No store product mapped yet — add one to get a price.",
                )
            )
            continue

        store = library.stores[product.store_id]
        packages, purchased = _packages_for(food, product, grams)
        cost = product.price_usd * packages if product.price_usd is not None else None

        by_store[store.id].append(
            Line(
                food_id=food_id,
                name=food.name,
                aisle=product.aisle or food.aisle,
                grams_needed=grams,
                grams_purchased=purchased,
                packages=packages,
                package_text=f"{product.package_qty:g} {product.package_unit}",
                quantity_text=_quantity_text(food, product, packages, purchased),
                est_cost=cost,
                price_stale_days=_stale_days(product, today),
                product_name=product.name,
                by_weight=product.by_weight,
                links=_links(store, food, product, delivery_partner),
                used_by=tuple(used_by.get(food_id, ())),
            )
        )

    by_store = _consolidate_trips(by_store, library, preference, delivery_partner)

    store_lists: list[StoreList] = []
    grand_total = 0.0
    priced = unpriced = 0
    for store in stores:
        lines = by_store.get(store.id, [])
        if not lines:
            continue
        lines.sort(key=lambda l: (store.aisle_rank(l.aisle), l.name))
        sub = sum(l.est_cost for l in lines if l.est_cost is not None)
        n_priced = sum(1 for l in lines if l.est_cost is not None)
        n_unpriced = len(lines) - n_priced
        priced += n_priced
        unpriced += n_unpriced
        grand_total += sub
        store_lists.append(
            StoreList(
                store=store,
                lines=tuple(lines),
                subtotal=sub if n_priced else None,
                priced_lines=n_priced,
                unpriced_lines=n_unpriced,
            )
        )

    total_lines = priced + unpriced + len(unmatched)
    coverage = priced / total_lines if total_lines else 0.0

    if unpriced or unmatched:
        notes.append(
            f"{unpriced + len(unmatched)} of {total_lines} items have no price on file, so the "
            "total is a floor rather than an estimate."
        )
    stale = [
        l for sl in store_lists for l in sl.lines
        if l.price_stale_days is not None and l.price_stale_days > 120
    ]
    if stale:
        notes.append(
            f"{len(stale)} price(s) are over four months old. Correct them in "
            "/config/stores/ as you shop and the estimates get better."
        )
    notes.append(
        "Prices are your own estimates, not live store data. Order links hand a search "
        "term to the retailer's app; they do not build a cart."
    )

    return ShoppingList(
        stores=tuple(store_lists),
        unmatched=tuple(unmatched),
        pantry_used=pantry_used,
        total_estimate=grand_total if priced else None,
        coverage=coverage,
        generated=datetime.now(),
        notes=tuple(notes),
    )


def _quantity_text(food: Food, product: Product, packages: float, purchased: float) -> str:
    """How the line reads on the list: "2 × 150 g tub" or "0.9 lb"."""
    if product.by_weight:
        return humanise(purchased, food)
    if packages == 1:
        return f"1 × {product.package_qty:g} {product.package_unit}"
    return f"{packages:g} × {product.package_qty:g} {product.package_unit}"


def _consolidate_trips(
    by_store: dict[str, list[Line]],
    library: Library,
    preference: Sequence[str],
    delivery_partner: str,
) -> dict[str, list[Line]]:
    """Fold away trips too small to be worth making.

    Saving forty cents is not worth a separate stop, so a store holding fewer
    than :data:`MIN_ITEMS_PER_TRIP` lines gives them up to whichever remaining
    store also stocks them. Relocations are worked out in full before anything
    is written, and are only committed when *every* item on the doomed list found
    a home — a partial move would leave you visiting the store anyway, for less.
    """
    order = sorted(preference, key=lambda sid: len(by_store.get(sid, [])))
    for sid in order:
        lines = by_store.get(sid, [])
        if not lines or len(lines) >= MIN_ITEMS_PER_TRIP:
            continue
        targets = [t for t in preference if t != sid and by_store.get(t)]
        if not targets:
            continue

        # Plan the whole move first: target store id -> replacement line.
        planned: list[tuple[str, Line]] = []
        for line in lines:
            food = library.foods[line.food_id]
            relocated = None
            for target in sorted(targets, key=lambda t: -len(by_store[t])):
                alt = _choose_product(food, library.products_for(line.food_id, [target]), [target])
                if alt is None:
                    continue
                packages, purchased = _packages_for(food, alt, line.grams_needed)
                relocated = (
                    target,
                    Line(
                        food_id=line.food_id,
                        name=line.name,
                        aisle=alt.aisle or food.aisle,
                        grams_needed=line.grams_needed,
                        grams_purchased=purchased,
                        packages=packages,
                        package_text=f"{alt.package_qty:g} {alt.package_unit}",
                        quantity_text=_quantity_text(food, alt, packages, purchased),
                        est_cost=alt.price_usd * packages if alt.price_usd is not None else None,
                        price_stale_days=_stale_days(alt, date.today()),
                        product_name=alt.name,
                        by_weight=alt.by_weight,
                        links=_links(library.stores[target], food, alt, delivery_partner),
                        used_by=line.used_by,
                        note=f"Moved from {library.stores[sid].short} to save a stop.",
                    ),
                )
                break
            if relocated is None:
                break  # this trip cannot be collapsed; abandon the whole idea
            planned.append(relocated)

        if len(planned) == len(lines):
            for target, new_line in planned:
                by_store[target].append(new_line)
            by_store[sid] = []
    return by_store


def used_by_index(plan_recipes: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """Invert recipe -> food ids into food id -> recipe titles."""
    out: dict[str, list[str]] = {}
    for title, food_ids in plan_recipes.items():
        for fid in food_ids:
            out.setdefault(fid, []).append(title)
    return out


_AISLE_LABELS = {
    "produce": "Produce",
    "bakery": "Bakery",
    "deli": "Deli",
    "meat": "Meat",
    "seafood": "Seafood",
    "dairy": "Dairy",
    "eggs": "Eggs",
    "refrigerated": "Refrigerated",
    "frozen": "Frozen",
    "grains": "Grains & rice",
    "legumes": "Beans & legumes",
    "canned": "Canned goods",
    "pasta_sauce": "Pasta & sauce",
    "condiments": "Condiments",
    "oils_vinegar": "Oils & vinegar",
    "spices": "Spices",
    "nuts_seeds": "Nuts & seeds",
    "baking": "Baking",
    "beverages": "Beverages",
    "other": "Other",
}


def _aisle_label(aisle: str) -> str:
    return _AISLE_LABELS.get(aisle, aisle.replace("_", " ").title())
