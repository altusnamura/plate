"""Nutrient vectors with honest handling of missing data.

Every number in this app is eventually a sum over food records, and those records
are uneven: kcal and protein are known for everything, magnesium is known for
maybe half the database until someone runs the USDA importer. Silently treating
an unknown as zero would make a magnesium target look badly missed when really
we just don't know, so a ``Nutrients`` value tracks *which* nutrients its
components failed to specify, weighted by the kcal those components contributed.
That lets the UI say "1180 mg potassium across 82% of today's intake" instead of
quietly lying.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping

# Nutrients we require every food record to specify. The loader rejects a food
# that is missing any of these, which is what keeps calorie and macro math from
# needing coverage caveats.
CORE: tuple[str, ...] = (
    "kcal",
    "protein_g",
    "fat_g",
    "satfat_g",
    "carb_g",
    "fiber_g",
    "sugar_g",
    "sodium_mg",
    "potassium_mg",
)

# Nice to have. Absent unless hand-entered or backfilled from FoodData Central.
OPTIONAL: tuple[str, ...] = (
    "calcium_mg",
    "magnesium_mg",
    "iron_mg",
    "vitamin_c_mg",
    "omega3_g",
)

ALL: tuple[str, ...] = CORE + OPTIONAL

# Display metadata: label, unit, decimal places.
LABELS: Mapping[str, tuple[str, str, int]] = {
    "kcal": ("Calories", "kcal", 0),
    "protein_g": ("Protein", "g", 0),
    "fat_g": ("Fat", "g", 0),
    "satfat_g": ("Saturated fat", "g", 1),
    "carb_g": ("Carbs", "g", 0),
    "fiber_g": ("Fiber", "g", 0),
    "sugar_g": ("Sugar", "g", 0),
    "sodium_mg": ("Sodium", "mg", 0),
    "potassium_mg": ("Potassium", "mg", 0),
    "calcium_mg": ("Calcium", "mg", 0),
    "magnesium_mg": ("Magnesium", "mg", 0),
    "iron_mg": ("Iron", "mg", 1),
    "vitamin_c_mg": ("Vitamin C", "mg", 0),
    "omega3_g": ("Omega-3", "g", 2),
}

KCAL_PER_G = {"protein_g": 4.0, "carb_g": 4.0, "fat_g": 9.0}

# Fibre is counted inside carbohydrate on a US label but yields roughly 2 kcal/g,
# not 4. Ignoring that makes the Atwater cross-check fire on every leafy green:
# spinach declares 23 kcal/100 g where naive 4/4/9 predicts 30.
KCAL_PER_G_FIBER = 2.0


@dataclass(frozen=True, slots=True)
class Nutrients:
    """An additive bundle of nutrient amounts.

    ``values`` holds only nutrients this bundle actually knows. ``missing_kcal``
    maps a nutrient name to the number of kcal contributed by components that did
    not specify it, so coverage is recoverable after any number of additions.
    """

    values: Mapping[str, float] = field(default_factory=dict)
    missing_kcal: Mapping[str, float] = field(default_factory=dict)

    # ---- construction -----------------------------------------------------

    @classmethod
    def zero(cls) -> "Nutrients":
        return cls(values={n: 0.0 for n in CORE}, missing_kcal={})

    @classmethod
    def from_mapping(cls, raw: Mapping[str, float | None]) -> "Nutrients":
        """Build from a food record's per-100g block.

        Unspecified optional nutrients become coverage debt against this
        record's own kcal.
        """
        vals: dict[str, float] = {}
        for name in ALL:
            v = raw.get(name)
            if v is not None:
                vals[name] = float(v)
        kcal = vals.get("kcal", 0.0)
        missing = {n: kcal for n in OPTIONAL if n not in vals}
        return cls(values=vals, missing_kcal=missing)

    # ---- arithmetic -------------------------------------------------------

    def scaled(self, factor: float) -> "Nutrients":
        return Nutrients(
            values={k: v * factor for k, v in self.values.items()},
            missing_kcal={k: v * factor for k, v in self.missing_kcal.items()},
        )

    def __add__(self, other: "Nutrients") -> "Nutrients":
        vals = dict(self.values)
        for k, v in other.values.items():
            vals[k] = vals.get(k, 0.0) + v
        miss = dict(self.missing_kcal)
        for k, v in other.missing_kcal.items():
            miss[k] = miss.get(k, 0.0) + v
        # A nutrient one side knows and the other does not is still partial:
        # the unknown side already recorded its own kcal as debt, so nothing
        # more to do here.
        return Nutrients(values=vals, missing_kcal=miss)

    __radd__ = __add__

    @classmethod
    def total(cls, parts: Iterable["Nutrients"]) -> "Nutrients":
        acc = cls.zero()
        for p in parts:
            acc = acc + p
        return acc

    # ---- access -----------------------------------------------------------

    def get(self, name: str, default: float = 0.0) -> float:
        """Amount of ``name``, treating unknown components as zero.

        Use with :meth:`coverage` when the number is shown to a human.
        """
        return float(self.values.get(name, default))

    def __getitem__(self, name: str) -> float:
        return self.get(name)

    @property
    def kcal(self) -> float:
        return self.get("kcal")

    def coverage(self, name: str) -> float:
        """Fraction of this bundle's kcal from components that knew ``name``.

        1.0 means fully known; 0.0 means nothing in here specified it. Core
        nutrients are always 1.0 because the loader enforces them.
        """
        if name in CORE:
            return 1.0
        kcal = self.kcal
        if kcal <= 0:
            return 1.0 if name in self.values else 0.0
        missing = self.missing_kcal.get(name, 0.0)
        return max(0.0, min(1.0, 1.0 - missing / kcal))

    def known(self, name: str, min_coverage: float = 0.7) -> float | None:
        """``get`` but ``None`` when too much of the intake is unaccounted for."""
        if self.coverage(name) < min_coverage:
            return None
        return self.get(name)

    # ---- derived ----------------------------------------------------------

    @property
    def kcal_from_macros(self) -> float:
        """Atwater reconstruction, used to sanity-check the food database.

        Fibre is billed at 2 kcal/g and deducted from carbohydrate, since US
        labels report it inside the carb figure.
        """
        fiber = min(self.get("fiber_g"), self.get("carb_g"))
        net_carb = self.get("carb_g") - fiber
        return (
            self.get("protein_g") * KCAL_PER_G["protein_g"]
            + self.get("fat_g") * KCAL_PER_G["fat_g"]
            + net_carb * KCAL_PER_G["carb_g"]
            + fiber * KCAL_PER_G_FIBER
        )

    def macro_split(self) -> dict[str, float]:
        """Protein/fat/carb share of calories, normalised to sum to 1."""
        parts = {m: self.get(m) * f for m, f in KCAL_PER_G.items()}
        total = sum(parts.values())
        if total <= 0:
            return {m: 0.0 for m in KCAL_PER_G}
        return {m.removesuffix("_g"): v / total for m, v in parts.items()}

    def per_1000_kcal(self, name: str) -> float:
        """Nutrient density, the fair way to compare sodium across diet sizes."""
        kcal = self.kcal
        if kcal <= 0:
            return 0.0
        return self.get(name) * 1000.0 / kcal

    def as_dict(self, include_coverage: bool = False) -> dict[str, object]:
        out: dict[str, object] = {n: round(self.get(n), 3) for n in ALL if n in self.values}
        if include_coverage:
            out["_coverage"] = {
                n: round(self.coverage(n), 3) for n in OPTIONAL if n in self.values
            }
        return out

    def without(self, *names: str) -> "Nutrients":
        drop = set(names)
        return replace(
            self,
            values={k: v for k, v in self.values.items() if k not in drop},
        )
