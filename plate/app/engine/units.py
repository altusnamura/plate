"""Unit conversion to grams, the canonical quantity in this app.

Mass units convert unconditionally. Volume needs the food's density and count
units ("1 can", "2 cloves") need a per-item weight, so those conversions are
food-specific and fail loudly when the food record doesn't supply what's needed.
Failing loudly matters: a silent fallback would put "3 cups of lentils" through
as 3 grams and the whole week's calorie math would be wrong in a way nobody
would notice by looking at the menu.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:  # avoid a circular import at runtime
    from .models import Food


class UnitError(ValueError):
    """A quantity could not be converted for this particular food."""


MASS_TO_G: Mapping[str, float] = {
    "g": 1.0,
    "gram": 1.0,
    "grams": 1.0,
    "kg": 1000.0,
    "oz": 28.349523125,
    "lb": 453.59237,
    "lbs": 453.59237,
}

VOLUME_TO_ML: Mapping[str, float] = {
    "ml": 1.0,
    "l": 1000.0,
    "tsp": 4.92892159375,
    "tbsp": 14.78676478125,
    "cup": 236.5882365,
    "fl_oz": 29.5735295625,
    "pint": 473.176473,
    "quart": 946.352946,
}

# Vague-but-real cooking amounts. Deliberately small so they never move the
# calorie total much; they exist so recipes can read like recipes.
PINCH_G = 0.36
DASH_G = 0.72

_COUNT_ALIASES: Mapping[str, str] = {
    "each": "ea",
    "ea": "ea",
    "item": "ea",
    "whole": "ea",
    "piece": "ea",
}


def normalise(unit: str) -> str:
    u = unit.strip().lower().replace(" ", "_")
    u = u.removesuffix("es") if u.endswith(("boxes", "bunches")) else u
    return _COUNT_ALIASES.get(u, u)


def is_mass(unit: str) -> bool:
    return normalise(unit) in MASS_TO_G


def to_grams(qty: float, unit: str, food: "Food") -> float:
    """Convert ``qty unit`` of ``food`` into grams.

    Raises :class:`UnitError` when the food lacks the density or per-item weight
    the conversion needs.
    """
    u = normalise(unit)

    if u in MASS_TO_G:
        return qty * MASS_TO_G[u]

    if u == "pinch":
        return qty * PINCH_G
    if u == "dash":
        return qty * DASH_G

    if u in VOLUME_TO_ML:
        ml = qty * VOLUME_TO_ML[u]
        # A food may state a direct per-cup weight, which is more accurate than
        # density for anything that packs (flour, chopped greens, grated cheese).
        direct = food.unit_g.get(u)
        if direct is not None:
            return qty * direct
        if food.g_per_ml is None:
            raise UnitError(
                f"food '{food.id}' needs 'g_per_ml' (or unit_g.{u}) to use "
                f"volume unit '{unit}'"
            )
        return ml * food.g_per_ml

    # Everything else is a count unit the food must define: ea, can, clove,
    # slice, bunch, fillet, tortilla...
    per = food.unit_g.get(u)
    if per is None:
        known = ", ".join(sorted(food.unit_g)) or "none"
        raise UnitError(
            f"food '{food.id}' has no weight for unit '{unit}' "
            f"(defined: {known}); add it under unit_g"
        )
    return qty * per


def from_grams(grams: float, unit: str, food: "Food") -> float:
    """Inverse of :func:`to_grams`, for rendering purchase quantities."""
    one = to_grams(1.0, unit, food)
    if one <= 0:
        raise UnitError(f"unit '{unit}' has zero weight for food '{food.id}'")
    return grams / one


def humanise(grams: float, food: "Food") -> str:
    """Render grams in whatever unit a person would actually say.

    Prefers the food's ``display_unit``, falls back to a count unit, then to a
    plain weight in the caller's least-surprising units.
    """
    unit = food.display_unit
    if unit:
        try:
            n = from_grams(grams, unit, food)
        except UnitError:
            unit = None
        else:
            label = unit if unit not in ("ea",) else ""
            n_txt = _fmt_count(n)
            return f"{n_txt} {label}".strip() or n_txt

    if grams >= 1000:
        return f"{grams / 1000:.2f} kg".replace(".00", "")
    if grams >= 100:
        return f"{grams:.0f} g"
    return f"{grams:.4g} g"


def _fmt_count(n: float) -> str:
    """Halves and quarters read better than decimals on a shopping list."""
    for denom, glyph in ((2, "½"), (4, "¼"), (3, "⅓")):
        scaled = n * denom
        if abs(scaled - round(scaled)) < 0.02:
            whole, rem = divmod(int(round(scaled)), denom)
            if rem == 0:
                return str(whole)
            frac = glyph if rem == 1 else f"{rem}/{denom}"
            return f"{whole}{frac}" if whole else frac
    return f"{n:.2f}".rstrip("0").rstrip(".")
