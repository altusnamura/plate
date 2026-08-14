"""Nutrient targets derived from your metrics and published guidelines.

Everything in this module is traceable to a public recommendation rather than
invented, because a target you cannot audit is a target you cannot trust. The
sources, all current as of writing:

* Protein — International Society of Sports Nutrition position stand and the
  Helms et al. review on preserving lean mass in a deficit: 1.6-2.4 g/kg body
  mass, higher end while dieting.
* Fat — Dietary Guidelines for Americans (DGA) 2020-2025, 20-35% of energy,
  with a floor around 0.5-0.6 g/kg for hormone and fat-soluble vitamin needs.
* Fibre — Institute of Medicine adequate intake, 14 g per 1000 kcal.
* Sodium — DGA chronic disease risk reduction limit of 2300 mg/day; the American
  Heart Association's preferred 1500 mg/day for adults with elevated blood
  pressure.
* Potassium — DGA adequate intake, 3400 mg/day for men and 2600 for women; the
  DASH trials ran nearer 4700 mg.
* Saturated fat — DGA under 10% of energy; AHA under 6% for those managing blood
  pressure or lipids.
* Added sugar — DGA under 10% of energy.
* Blood pressure categories — 2017 ACC/AHA guideline thresholds.

This is nutrition planning software reading a home monitor, not a clinician. It
adjusts targets within published population guidance; it does not diagnose
anything and it does not know your medications, kidney function or history. The
potassium target in particular is one to raise with a doctor before chasing,
because potassium restriction rather than potassium loading is correct for some
kidney conditions and for people on certain blood-pressure drugs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Mapping, Sequence

from .nutrients import Nutrients

LB_PER_KG = 2.2046226218


# --------------------------------------------------------------------------
# blood pressure
# --------------------------------------------------------------------------


class BPCategory(str, Enum):
    UNKNOWN = "unknown"
    NORMAL = "normal"
    ELEVATED = "elevated"
    STAGE_1 = "stage_1"
    STAGE_2 = "stage_2"
    CRISIS = "crisis"

    @property
    def label(self) -> str:
        return {
            "unknown": "No readings yet",
            "normal": "Normal",
            "elevated": "Elevated",
            "stage_1": "Stage 1 hypertension range",
            "stage_2": "Stage 2 hypertension range",
            "crisis": "Hypertensive crisis range",
        }[self.value]

    @property
    def severity(self) -> int:
        return {
            "unknown": 0, "normal": 0, "elevated": 1,
            "stage_1": 2, "stage_2": 3, "crisis": 4,
        }[self.value]


@dataclass(frozen=True, slots=True)
class BPStatus:
    category: BPCategory
    systolic: float | None
    diastolic: float | None
    readings: int
    window_days: int
    trend_systolic: float | None = None   # change per week over the window
    advice: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        return self.category is not BPCategory.UNKNOWN


def classify_bp_values(systolic: float, diastolic: float) -> BPCategory:
    """2017 ACC/AHA categories. The higher of the two numbers decides."""
    if systolic > 180 or diastolic > 120:
        return BPCategory.CRISIS
    if systolic >= 140 or diastolic >= 90:
        return BPCategory.STAGE_2
    if systolic >= 130 or diastolic >= 80:
        return BPCategory.STAGE_1
    if systolic >= 120:
        return BPCategory.ELEVATED
    return BPCategory.NORMAL


def summarise_bp(
    systolic_by_day: Mapping[date, float],
    diastolic_by_day: Mapping[date, float],
    window_days: int = 14,
    today: date | None = None,
) -> BPStatus:
    """Average recent readings and categorise.

    Averaging matters: a single cuff reading has enough measurement error and
    white-coat effect that categorising on one number is meaningless. The ACC/AHA
    guideline itself asks for an average of readings across separate occasions.
    """
    today = today or date.today()
    start = today - timedelta(days=window_days)
    sys_vals = [v for d, v in systolic_by_day.items() if start <= d <= today]
    dia_vals = [v for d, v in diastolic_by_day.items() if start <= d <= today]

    if not sys_vals or not dia_vals:
        return BPStatus(
            category=BPCategory.UNKNOWN,
            systolic=None,
            diastolic=None,
            readings=0,
            window_days=window_days,
            advice=(
                "Connect the Omron integration and take a few readings — sodium and "
                "potassium targets stay at the general population defaults until then.",
            ),
        )

    sys_mean = sum(sys_vals) / len(sys_vals)
    dia_mean = sum(dia_vals) / len(dia_vals)
    category = classify_bp_values(sys_mean, dia_mean)

    trend = _weekly_slope(
        {d: v for d, v in systolic_by_day.items() if start - timedelta(days=window_days) <= d <= today}
    )

    advice: list[str] = []
    if len(sys_vals) < 4:
        advice.append(
            f"Only {len(sys_vals)} reading(s) in the last {window_days} days. "
            "Categories are based on averages, so this one is provisional."
        )
    if category is BPCategory.CRISIS:
        advice.append(
            "These averages are in the hypertensive crisis range. That is a matter for "
            "a doctor now, not a menu plan — please get it checked."
        )
    elif category.severity >= 2:
        advice.append(
            "Averages are in a hypertension range. Diet changes here follow the DASH "
            "pattern, which has good trial evidence, but this is worth a conversation "
            "with your doctor rather than something to manage alone."
        )
    if trend is not None and trend > 1.0:
        advice.append(f"Systolic trend is rising about {trend:.1f} mmHg/week.")
    elif trend is not None and trend < -1.0:
        advice.append(f"Systolic trend is falling about {abs(trend):.1f} mmHg/week.")

    return BPStatus(
        category=category,
        systolic=sys_mean,
        diastolic=dia_mean,
        readings=len(sys_vals),
        window_days=window_days,
        trend_systolic=trend,
        advice=tuple(advice),
    )


def _weekly_slope(series: Mapping[date, float]) -> float | None:
    if len(series) < 5:
        return None
    days = sorted(series)
    x0 = days[0]
    xs = [float((d - x0).days) for d in days]
    ys = [series[d] for d in days]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return (sxy / sxx) * 7.0


# --------------------------------------------------------------------------
# targets
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    """One nutrient goal.

    ``floor``/``ceiling`` are the hard edges the planner is penalised for
    crossing; ``goal`` is what it aims at. Any of the three may be ``None``.
    """

    nutrient: str
    goal: float | None = None
    floor: float | None = None
    ceiling: float | None = None
    unit: str = ""
    rationale: str = ""

    def status(self, amount: float) -> str:
        if self.ceiling is not None and amount > self.ceiling:
            return "over"
        if self.floor is not None and amount < self.floor:
            return "under"
        return "ok"

    def fraction(self, amount: float) -> float:
        """Progress toward the goal (or ceiling), clamped for progress bars."""
        ref = self.goal or self.floor or self.ceiling
        if not ref:
            return 0.0
        return max(0.0, min(2.0, amount / ref))


@dataclass(frozen=True, slots=True)
class NutritionTargets:
    kcal: float
    targets: Mapping[str, Target]
    bp: BPStatus
    protein_basis: str
    notes: tuple[str, ...] = field(default_factory=tuple)
    disclaimers: tuple[str, ...] = field(default_factory=tuple)

    def get(self, nutrient: str) -> Target | None:
        return self.targets.get(nutrient)

    def evaluate(self, n: Nutrients) -> dict[str, dict[str, object]]:
        """Compare an actual day (or plan) against every target."""
        out: dict[str, dict[str, object]] = {}
        for name, t in self.targets.items():
            amount = n.get(name)
            coverage = n.coverage(name)
            out[name] = {
                "amount": round(amount, 1),
                "goal": t.goal,
                "floor": t.floor,
                "ceiling": t.ceiling,
                "unit": t.unit,
                "status": t.status(amount) if coverage >= 0.7 else "unknown",
                "fraction": round(t.fraction(amount), 3),
                "coverage": round(coverage, 2),
                "rationale": t.rationale,
            }
        return out


def build_targets(
    kcal: float,
    weight_lb: float,
    goal: str,
    bp: BPStatus,
    body_fat_pct: float | None = None,
    sex: str = "male",
) -> NutritionTargets:
    """Assemble the full nutrient target set for a day of ``kcal`` calories."""
    notes: list[str] = []
    kg = weight_lb / LB_PER_KG
    lean_kg = kg * (1.0 - body_fat_pct / 100.0) if body_fat_pct else None

    # --- protein ---------------------------------------------------------
    # Higher while dieting: a deficit is when lean mass is at risk, and protein
    # plus resistance training is what protects it.
    # These sit at the well-supported middle of the ISSN range rather than its
    # athlete-cutting top end. The higher figures (2.2-2.4 g/kg body mass, or
    # 2.6-3.1 g/kg lean) come from studies of already-lean resistance-trained
    # subjects in a deficit; applied to an average body composition on a moderate
    # calorie budget they demand about 30% of energy from protein, which crowds
    # out the fibre and potassium this app is also trying to hit. Raise them in
    # the add-on options if you are lean and lifting hard.
    if goal == "lose":
        per_kg, per_lean = 1.8, 2.3
    elif goal == "gain":
        per_kg, per_lean = 1.7, 2.2
    else:
        per_kg, per_lean = 1.6, 2.1

    if lean_kg:
        protein_goal = per_lean * lean_kg
        basis = f"{per_lean:.1f} g per kg of lean mass ({lean_kg:.1f} kg)"
    else:
        protein_goal = per_kg * kg
        basis = f"{per_kg:.1f} g per kg of body mass ({kg:.1f} kg)"

    # Protein above ~40% of calories crowds out everything else on a small
    # calorie budget, so cap it there and say so.
    protein_cap = 0.40 * kcal / 4.0
    if protein_goal > protein_cap:
        notes.append(
            f"Protein goal trimmed from {protein_goal:.0f} g to {protein_cap:.0f} g so it "
            "stays under 40% of your calories."
        )
        protein_goal = protein_cap

    # --- fat -------------------------------------------------------------
    fat_goal = 0.30 * kcal / 9.0
    fat_floor = max(0.55 * kg, 0.20 * kcal / 9.0)

    # --- carbohydrate: the remainder -------------------------------------
    carb_kcal = kcal - protein_goal * 4.0 - fat_goal * 9.0
    carb_goal = max(50.0, carb_kcal / 4.0)

    # --- blood-pressure-sensitive limits ---------------------------------
    severity = bp.category.severity
    if severity >= 1:
        sodium_ceiling = 1500.0
        sodium_reason = (
            f"AHA's 1500 mg/day preference, applied because your readings average "
            f"{bp.systolic:.0f}/{bp.diastolic:.0f} ({bp.category.label})."
        )
        satfat_pct = 0.06
        satfat_reason = "AHA's under-6%-of-energy figure for blood pressure and lipid management."
        potassium_goal = 4700.0
        potassium_reason = "The DASH trial intake, which is where the blood pressure benefit was measured."
    else:
        sodium_ceiling = 2300.0
        sodium_reason = "DGA 2020-2025 chronic disease risk reduction limit for adults."
        satfat_pct = 0.10
        satfat_reason = "DGA limit of under 10% of energy."
        potassium_goal = 3400.0 if sex == "male" else 2600.0
        potassium_reason = "DGA adequate intake for adults."

    if severity >= 1:
        notes.append(
            "Sodium and saturated fat limits are tightened and potassium raised because "
            "your blood pressure average is above the normal range. The menu shifts "
            "toward the DASH pattern to match."
        )

    # A hard sodium floor: sweating through training on a genuinely low-sodium
    # diet is its own problem, and 1500 mg is already the AHA's aggressive figure.
    sodium_floor = 1200.0

    t: dict[str, Target] = {
        "kcal": Target(
            "kcal", goal=kcal, floor=kcal * 0.92, ceiling=kcal * 1.08, unit="kcal",
            rationale="Your calibrated expenditure adjusted for your goal rate.",
        ),
        "protein_g": Target(
            "protein_g", goal=protein_goal, floor=protein_goal * 0.9, unit="g",
            rationale=f"ISSN range for preserving lean mass: {basis}.",
        ),
        "fat_g": Target(
            "fat_g", goal=fat_goal, floor=fat_floor, unit="g",
            rationale="30% of energy, with a floor of 0.55 g/kg for hormone and vitamin needs.",
        ),
        "carb_g": Target(
            "carb_g", goal=carb_goal, unit="g",
            rationale="Whatever energy is left once protein and fat are set.",
        ),
        "fiber_g": Target(
            "fiber_g", goal=14.0 * kcal / 1000.0, floor=12.0 * kcal / 1000.0, unit="g",
            rationale="IOM adequate intake of 14 g per 1000 kcal.",
        ),
        "sodium_mg": Target(
            "sodium_mg", goal=sodium_ceiling * 0.85, floor=sodium_floor,
            ceiling=sodium_ceiling, unit="mg", rationale=sodium_reason,
        ),
        "potassium_mg": Target(
            "potassium_mg", goal=potassium_goal, floor=potassium_goal * 0.8, unit="mg",
            rationale=potassium_reason,
        ),
        "satfat_g": Target(
            "satfat_g", goal=satfat_pct * kcal / 9.0 * 0.8,
            ceiling=satfat_pct * kcal / 9.0, unit="g", rationale=satfat_reason,
        ),
        "sugar_g": Target(
            "sugar_g", ceiling=0.10 * kcal / 4.0, unit="g",
            rationale="DGA limit of under 10% of energy from added sugar. This counts all "
                      "sugars, including fruit, so treat it as a loose upper guide.",
        ),
    }

    disclaimers = [
        "These targets come from population-level published guidelines, adjusted for your "
        "metrics. They are not medical advice and this app knows nothing about your "
        "medications, kidney function or medical history.",
    ]
    if severity >= 1:
        disclaimers.append(
            "The raised potassium target is worth checking with a doctor first: with some "
            "kidney conditions, and on ACE inhibitors, ARBs or potassium-sparing diuretics, "
            "the correct advice is the opposite."
        )

    return NutritionTargets(
        kcal=kcal,
        targets=t,
        bp=bp,
        protein_basis=basis,
        notes=tuple(notes),
        disclaimers=tuple(disclaimers),
    )


# --------------------------------------------------------------------------
# DASH pattern scoring
# --------------------------------------------------------------------------

# Servings per 2000 kcal from the NIH DASH eating plan. Foods declare their
# group and serving size in the food database (dash_group / dash_serving_g).
DASH_SERVINGS_PER_2000 = {
    "vegetables": (4.0, 5.0),
    "fruit": (4.0, 5.0),
    "grains": (6.0, 8.0),
    "dairy_lowfat": (2.0, 3.0),
    "lean_protein": (0.0, 6.0),
    "nuts_legumes": (0.6, 0.8),   # 4-5 per week, expressed daily
    "fats_oils": (2.0, 3.0),
    "sweets": (0.0, 0.7),         # 5 or fewer per week
}


@dataclass(frozen=True, slots=True)
class DashScore:
    """How closely a day or a week matches the DASH pattern, 0-100."""

    score: float
    per_group: Mapping[str, dict[str, float]]
    shortfalls: tuple[str, ...]
    excesses: tuple[str, ...]


def dash_score(
    servings: Mapping[str, float],
    kcal: float,
) -> DashScore:
    """Score DASH adherence from counted servings, scaled to actual energy intake."""
    if kcal <= 0:
        return DashScore(0.0, {}, (), ())
    scale = kcal / 2000.0

    per_group: dict[str, dict[str, float]] = {}
    shortfalls: list[str] = []
    excesses: list[str] = []
    points = 0.0

    for group, (lo, hi) in DASH_SERVINGS_PER_2000.items():
        lo_s, hi_s = lo * scale, hi * scale
        got = servings.get(group, 0.0)
        if got < lo_s:
            frac = got / lo_s if lo_s > 0 else 1.0
            shortfalls.append(group)
        elif got > hi_s:
            # Overshoot is penalised more gently than shortfall for the food
            # groups where more is broadly harmless.
            over = (got - hi_s) / max(hi_s, 0.5)
            frac = max(0.0, 1.0 - (over * (1.0 if group in ("sweets", "lean_protein") else 0.4)))
            excesses.append(group)
        else:
            frac = 1.0
        per_group[group] = {
            "servings": round(got, 2),
            "min": round(lo_s, 2),
            "max": round(hi_s, 2),
            "fraction": round(min(1.0, frac), 3),
        }
        points += min(1.0, max(0.0, frac))

    return DashScore(
        score=100.0 * points / len(DASH_SERVINGS_PER_2000),
        per_group=per_group,
        shortfalls=tuple(shortfalls),
        excesses=tuple(excesses),
    )


def count_dash_servings(
    grams_by_food: Mapping[str, float],
    foods: Mapping[str, object],
) -> dict[str, float]:
    """Turn grams of each food into DASH servings by group.

    Foods without a ``dash_group`` simply don't contribute, which is why the
    score is reported alongside a coverage figure in the UI.
    """
    out: dict[str, float] = {}
    for food_id, grams in grams_by_food.items():
        food = foods.get(food_id)
        if food is None:
            continue
        group = getattr(food, "dash_group", None) or _group_from_tags(food)
        if not group:
            continue
        serving_g = getattr(food, "dash_serving_g", None) or _default_serving_g(group)
        if not serving_g:
            continue
        out[group] = out.get(group, 0.0) + grams / serving_g
    return out


def _group_from_tags(food: object) -> str | None:
    tags = getattr(food, "tags", frozenset())
    for tag in tags:
        if tag.startswith("dash:"):
            return tag.split(":", 1)[1]
    # Fall back to the aisle, which is right often enough to be useful.
    aisle = getattr(food, "aisle", "")
    return {
        "produce": None,      # ambiguous between vegetables and fruit
        "legumes": "nuts_legumes",
        "nuts_seeds": "nuts_legumes",
        "grains": "grains",
        "oils_vinegar": "fats_oils",
        "meat": "lean_protein",
        "seafood": "lean_protein",
    }.get(aisle)


def _default_serving_g(group: str) -> float | None:
    # NIH DASH serving sizes, converted to representative gram weights.
    return {
        "vegetables": 80.0,
        "fruit": 120.0,
        "grains": 45.0,
        "dairy_lowfat": 245.0,
        "lean_protein": 85.0,
        "nuts_legumes": 45.0,
        "fats_oils": 5.0,
        "sweets": 25.0,
    }.get(group)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def round_to(v: float, step: float) -> float:
    return math.floor(v / step + 0.5) * step
