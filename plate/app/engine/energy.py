"""Weight trend and calorie targets.

The central idea here is that we do not trust the Fitbit calorie number. Wrist
trackers estimate total daily expenditure from heart rate and step counts and are
commonly off by 10-25% in either direction for an individual — consistently off,
which is the useful part. So instead of using it as truth, we treat it as a prior
and correct it against the one measurement that cannot lie over time: the rate of
change of your own body mass.

Energy balance says that over any window,

    intake - expenditure = energy stored

and roughly 3500 kcal is stored per pound of body mass gained (see
KCAL_PER_LB for the caveats). Rearranged, if we know what you ate and we can see
your weight trend moving, we can solve for what you actually burned:

    TDEE = (sum of intake - 3500 * pounds gained) / days

That estimate is noisy when there is little data and sharp when there is a lot,
so :func:`calibrate` blends it against the Fitbit prior with a weight that grows
as logging coverage and window length grow. The result is a TDEE that starts out
equal to what your tracker says and converges on your actual metabolism over a
few weeks — and the ratio between them is itself worth showing you.

Daily weight readings are dominated by water, glycogen and gut contents; swings
of two or three pounds mean nothing. Everything here therefore works off an
exponentially weighted trend, never raw scale readings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence

# Energy density of body mass change. 3500 kcal/lb is the classic Wishnofsky
# figure for fat tissue. Real weight change mixes fat with lean tissue and
# glycogen-bound water, so the true figure drifts with deficit size and
# training; over multi-week windows on a moderate deficit it is close enough,
# and any systematic error lands in the calibration factor rather than in the
# target. Documented rather than tuned, so the number is auditable.
KCAL_PER_LB = 3500.0
KCAL_PER_KG = 7716.2
LB_PER_KG = 2.2046226218

# Trend smoothing. A 7-day half-life removes day-to-day water noise while still
# reacting inside a fortnight, which is the window people judge progress on.
TREND_HALF_LIFE_DAYS = 7.0

# Windows.
RATE_WINDOW_DAYS = 21       # regression window for lb/week
CALIBRATION_WINDOW_DAYS = 28
MIN_CALIBRATION_DAYS = 10   # below this, the observed TDEE is mostly noise
MIN_LOGGED_FRACTION = 0.6   # of days in the window needing an intake log

# Safety rails. These are floors on *planned intake*, not medical advice, and
# they exist so a mis-set goal or a bad weight reading cannot produce a
# dangerous target. Values follow the commonly cited minimum-intake guidance of
# 1200 kcal/day for women and 1500 for men for unsupervised dieting.
ABSOLUTE_FLOOR_KCAL = {"female": 1200.0, "male": 1500.0}
MAX_DEFICIT_FRACTION = 0.25   # never plan more than a 25% deficit
MAX_SURPLUS_FRACTION = 0.20
MAX_RATE_FRACTION_PER_WEEK = 0.01  # 1% of body mass per week

# How much of an unusually active day gets added back to that day's target.
# 1.0 holds the deficit exactly constant but fully inherits the tracker's noise
# on active calories; 0 ignores activity entirely. 0.75 splits the difference.
DEFAULT_ACTIVITY_PASSTHROUGH = 0.75


# --------------------------------------------------------------------------
# weight trend
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrendPoint:
    day: date
    raw_lb: float | None
    trend_lb: float


def ewma_trend(
    readings: Mapping[date, float],
    half_life_days: float = TREND_HALF_LIFE_DAYS,
) -> list[TrendPoint]:
    """Exponentially weighted weight trend, gap-aware.

    ``readings`` maps a day to a weight in pounds; days may be missing. The
    smoothing constant is scaled by the gap length so a week without weighing
    doesn't leave the trend anchored to stale data:
    ``alpha_effective = 1 - (1 - alpha) ** gap_days``.

    The returned series is dense — one point per day from first to last
    reading — because the rate regression and the UI chart both want that.
    """
    if not readings:
        return []

    alpha = 1.0 - math.pow(0.5, 1.0 / max(half_life_days, 0.5))
    days = sorted(readings)
    first, last = days[0], days[-1]

    out: list[TrendPoint] = []
    trend = readings[first]
    prev_day = first
    day = first
    while day <= last:
        raw = readings.get(day)
        if raw is not None:
            gap = max((day - prev_day).days, 1) if out else 1
            a_eff = 1.0 - math.pow(1.0 - alpha, gap)
            trend = trend + a_eff * (raw - trend)
            prev_day = day
        out.append(TrendPoint(day=day, raw_lb=raw, trend_lb=trend))
        day += timedelta(days=1)
    return out


def trend_rate_lb_per_week(
    trend: Sequence[TrendPoint],
    window_days: int = RATE_WINDOW_DAYS,
) -> float | None:
    """Least-squares slope of the trend line, in pounds per week.

    Regressing the *smoothed* series rather than raw readings is deliberate:
    the smoothing has already discarded the water noise that would otherwise
    dominate a short window, and the slope of an EWMA is a far more stable
    progress signal than the slope of a scatter of daily weights.
    """
    if len(trend) < 7:
        return None
    window = trend[-window_days:]
    n = len(window)
    if n < 7:
        return None

    x0 = window[0].day
    xs = [float((p.day - x0).days) for p in window]
    ys = [p.trend_lb for p in window]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return (sxy / sxx) * 7.0


def latest_trend_lb(trend: Sequence[TrendPoint]) -> float | None:
    return trend[-1].trend_lb if trend else None


# --------------------------------------------------------------------------
# expenditure priors
# --------------------------------------------------------------------------


def bmr_mifflin(weight_lb: float, height_in: float, age_years: float, sex: str) -> float:
    """Mifflin-St Jeor resting metabolic rate — the general-purpose default."""
    kg = weight_lb / LB_PER_KG
    cm = height_in * 2.54
    base = 10.0 * kg + 6.25 * cm - 5.0 * age_years
    return base + (5.0 if sex == "male" else -161.0)


def bmr_katch_mcardle(weight_lb: float, body_fat_pct: float) -> float:
    """Katch-McArdle, which beats Mifflin when body composition is known.

    Fitbit Aria and most smart scales report body fat, so prefer this whenever
    the number is present and plausible.
    """
    lean_kg = (weight_lb * (1.0 - body_fat_pct / 100.0)) / LB_PER_KG
    return 370.0 + 21.6 * lean_kg


def resting_rate(
    weight_lb: float,
    height_in: float,
    age_years: float,
    sex: str,
    body_fat_pct: float | None = None,
) -> tuple[float, str]:
    """Best available RMR estimate, plus which formula produced it."""
    if body_fat_pct is not None and 3.0 <= body_fat_pct <= 65.0:
        return bmr_katch_mcardle(weight_lb, body_fat_pct), "katch-mcardle"
    return bmr_mifflin(weight_lb, height_in, age_years, sex), "mifflin-st-jeor"


def activity_factor_from_steps(steps: float | None) -> float:
    """Physical activity level, used only when no tracker burn is available.

    Piecewise-linear between the conventional sedentary (1.2) and very active
    (1.9) multipliers, anchored at step counts that roughly correspond.
    """
    if steps is None:
        return 1.35
    anchors = ((2000.0, 1.20), (5000.0, 1.35), (8000.0, 1.50), (12000.0, 1.65), (18000.0, 1.85))
    if steps <= anchors[0][0]:
        return anchors[0][1]
    for (s0, f0), (s1, f1) in zip(anchors, anchors[1:]):
        if steps <= s1:
            t = (steps - s0) / (s1 - s0)
            return f0 + t * (f1 - f0)
    return anchors[-1][1]


# --------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Calibration:
    """The outcome of reconciling intake, tracker burn and weight change."""

    tdee: float                      # kcal/day, our best estimate
    prior_tdee: float                # what we'd have guessed without weight data
    observed_tdee: float | None      # solved from energy balance, if solvable
    confidence: float                # 0-1 weight given to the observed value
    tracker_bias: float | None       # observed / tracker burn; >1 = tracker reads low
    window_days: int
    logged_days: int
    source: str
    notes: tuple[str, ...] = ()

    @property
    def tracker_bias_pct(self) -> float | None:
        """Signed percentage error of the tracker, for display."""
        if self.tracker_bias is None:
            return None
        return (self.tracker_bias - 1.0) * 100.0


def calibrate(
    trend: Sequence[TrendPoint],
    intake_by_day: Mapping[date, float],
    tracker_burn_by_day: Mapping[date, float],
    fallback_tdee: float,
    window_days: int = CALIBRATION_WINDOW_DAYS,
    today: date | None = None,
) -> Calibration:
    """Estimate true TDEE by reconciling logged intake against weight change.

    The prior is the mean tracker burn over the window (or ``fallback_tdee``
    when the tracker has nothing to say). The observation solves energy balance
    for expenditure. Confidence in the observation rises with the number of
    logged days and the length of the window, and is halved when the weight
    trend barely moved — a flat trend makes the arithmetic very sensitive to
    small logging errors.
    """
    notes: list[str] = []
    today = today or date.today()
    start = today - timedelta(days=window_days)

    window_intake = {d: v for d, v in intake_by_day.items() if start <= d < today and v > 0}
    window_burn = {d: v for d, v in tracker_burn_by_day.items() if start <= d < today and v > 0}

    prior = (
        sum(window_burn.values()) / len(window_burn)
        if window_burn
        else fallback_tdee
    )
    if not window_burn:
        notes.append("No tracker expenditure in the window; prior came from the BMR formula.")

    # Weight change over the window, from the *slope* of the trend line rather
    # than the difference between its endpoints.
    #
    # This matters more than it looks. An EWMA lags the series it smooths, so at
    # the start of the window the trend is still catching up to reality and the
    # endpoint difference systematically understates how much weight actually
    # moved. Understating the change understates the deficit, which overstates
    # TDEE, which means the calibration reports the tracker as less wrong than it
    # is — biased toward doing nothing. A least-squares slope is immune: constant
    # lag shifts the line without changing its gradient.
    window_points = [p for p in trend if start <= p.day <= today]
    span_days = 0
    delta_lb: float | None = None
    if len(window_points) >= 2:
        span_days = (window_points[-1].day - window_points[0].day).days
        rate = trend_rate_lb_per_week(window_points, window_days=len(window_points))
        if span_days > 0 and rate is not None:
            delta_lb = rate * span_days / 7.0

    logged = len(window_intake)
    observed: float | None = None
    confidence = 0.0

    if delta_lb is None or span_days < MIN_CALIBRATION_DAYS:
        notes.append(
            f"Need about {MIN_CALIBRATION_DAYS} days of weight history to calibrate; "
            f"have {span_days}."
        )
    elif logged < max(MIN_CALIBRATION_DAYS, int(MIN_LOGGED_FRACTION * span_days)):
        notes.append(
            f"Only {logged} of the last {span_days} days have a food log, so the "
            "calorie target is still running off your tracker's estimate."
        )
    else:
        # Restrict to the days we can actually account for, and scale the
        # weight change to that same span so the two sides of the equation
        # describe the same period.
        covered = sorted(window_intake)
        cover_span = (covered[-1] - covered[0]).days + 1
        mean_intake = sum(window_intake.values()) / logged
        stored_per_day = (delta_lb * KCAL_PER_LB) / span_days
        observed = mean_intake - stored_per_day

        # Confidence: logging coverage x window maturity.
        coverage = min(1.0, logged / max(cover_span, 1))
        maturity = min(1.0, span_days / float(CALIBRATION_WINDOW_DAYS))
        confidence = max(0.0, min(0.9, coverage * maturity))

        # A trend that moved less than a pound over the window makes the
        # division above fragile; lean harder on the prior.
        if abs(delta_lb) < 1.0:
            confidence *= 0.5
            notes.append(
                "Weight trend is nearly flat, so the calibration is tentative."
            )

        # Reject physiologically absurd solutions rather than acting on them;
        # they almost always mean the food log or the scale data is wrong.
        if not 1000.0 <= observed <= 6000.0:
            notes.append(
                f"Energy-balance maths produced {observed:.0f} kcal/day, which is not "
                "credible — ignoring it. Check for missed food logs or a bad scale reading."
            )
            observed = None
            confidence = 0.0

    if observed is None:
        tdee = prior
        source = "tracker" if window_burn else "formula"
        bias = None
    else:
        tdee = confidence * observed + (1.0 - confidence) * prior
        source = "calibrated"
        tracker_mean = sum(window_burn.values()) / len(window_burn) if window_burn else None
        bias = (observed / tracker_mean) if tracker_mean else None
        if bias is not None and abs(bias - 1.0) > 0.08:
            direction = "low" if bias > 1 else "high"
            notes.append(
                f"Your tracker appears to read about {abs(bias - 1.0) * 100:.0f}% "
                f"{direction} for you; targets are corrected for that."
            )

    return Calibration(
        tdee=tdee,
        prior_tdee=prior,
        observed_tdee=observed,
        confidence=confidence,
        tracker_bias=bias,
        window_days=window_days,
        logged_days=logged,
        source=source,
        notes=tuple(notes),
    )


# --------------------------------------------------------------------------
# targets
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalorieTarget:
    """What to eat today, and the reasoning behind it."""

    kcal: float
    base_kcal: float             # the goal-derived target before today's activity
    tdee: float
    deficit_kcal: float          # negative when losing
    planned_rate_lb_per_week: float
    activity_adjustment: float   # kcal added for an unusually active day
    floor_applied: bool
    rate_capped: bool
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def weekly_kcal(self) -> float:
        return self.base_kcal * 7.0


def calorie_target(
    calibration: Calibration,
    weight_lb: float,
    sex: str,
    goal: str,
    target_rate_lb_per_week: float,
    resting_kcal: float,
    today_burn: float | None = None,
    typical_burn: float | None = None,
    activity_passthrough: float = DEFAULT_ACTIVITY_PASSTHROUGH,
) -> CalorieTarget:
    """Turn a TDEE estimate and a goal into today's calorie target.

    Applies, in order: the requested rate, a cap at 1% of body mass per week, a
    cap on deficit/surplus as a fraction of TDEE, a floor at resting metabolic
    rate, and an absolute floor. Then adds back part of today's excess activity
    so a long hike doesn't silently turn into a much steeper deficit.
    """
    notes: list[str] = []
    tdee = calibration.tdee

    rate = 0.0 if goal == "maintain" else float(target_rate_lb_per_week)
    if goal == "lose":
        rate = -abs(rate)
    elif goal == "gain":
        rate = abs(rate)

    rate_capped = False
    max_rate = MAX_RATE_FRACTION_PER_WEEK * weight_lb
    if abs(rate) > max_rate:
        rate = math.copysign(max_rate, rate)
        rate_capped = True
        notes.append(
            f"Rate capped at 1% of body mass — {abs(rate):.1f} lb/week — which is the "
            "fastest pace that reliably preserves muscle."
        )

    delta = rate * KCAL_PER_LB / 7.0

    max_deficit = MAX_DEFICIT_FRACTION * tdee
    max_surplus = MAX_SURPLUS_FRACTION * tdee
    if delta < -max_deficit:
        delta = -max_deficit
        rate_capped = True
        notes.append("Deficit capped at 25% of your estimated expenditure.")
    elif delta > max_surplus:
        delta = max_surplus
        rate_capped = True
        notes.append("Surplus capped at 20% of your estimated expenditure.")

    base = tdee + delta

    floor_applied = False
    absolute_floor = ABSOLUTE_FLOOR_KCAL.get(sex, 1400.0)
    floor = max(resting_kcal, absolute_floor)
    if base < floor:
        base = floor
        floor_applied = True
        notes.append(
            f"Target raised to {floor:.0f} kcal — your resting metabolic rate and the "
            "minimum safe intake for unsupervised dieting both sit above the number "
            "your goal rate implied."
        )

    adjustment = 0.0
    if today_burn is not None and typical_burn is not None and typical_burn > 0:
        excess = today_burn - typical_burn
        # Only add back genuine extra work. Quiet days keep the base target
        # rather than dropping further, because chasing a low reading downward
        # amplifies tracker noise into an unnecessarily harsh day.
        if excess > 75.0:
            adjustment = excess * activity_passthrough
            notes.append(
                f"You've burned about {excess:.0f} kcal more than a typical day, so "
                f"{adjustment:.0f} of that is added back."
            )

    return CalorieTarget(
        kcal=base + adjustment,
        base_kcal=base,
        tdee=tdee,
        deficit_kcal=delta,
        planned_rate_lb_per_week=rate,
        activity_adjustment=adjustment,
        floor_applied=floor_applied,
        rate_capped=rate_capped,
        notes=tuple(notes),
    )


def projected_goal_date(
    current_trend_lb: float,
    goal_weight_lb: float,
    rate_lb_per_week: float | None,
    today: date | None = None,
) -> date | None:
    """When the current *observed* rate would reach the goal weight.

    Uses the measured rate, not the planned one, because the honest answer to
    "when will I get there" is the one based on what is actually happening.
    """
    if rate_lb_per_week is None or abs(rate_lb_per_week) < 0.05:
        return None
    remaining = goal_weight_lb - current_trend_lb
    if remaining == 0:
        return today or date.today()
    if math.copysign(1, remaining) != math.copysign(1, rate_lb_per_week):
        return None  # moving away from the goal
    weeks = remaining / rate_lb_per_week
    if weeks <= 0 or weeks > 520:
        return None
    return (today or date.today()) + timedelta(days=round(weeks * 7))


def adherence(
    intake_by_day: Mapping[date, float],
    target_by_day: Mapping[date, float],
    days: int = 7,
    today: date | None = None,
    tolerance: float = 0.10,
) -> float | None:
    """Fraction of recent days whose intake landed within ``tolerance`` of target.

    Days with no log count against you, because an unlogged day is not evidence
    of a day on plan.
    """
    today = today or date.today()
    window = [today - timedelta(days=i) for i in range(1, days + 1)]
    scored = 0
    hits = 0
    for d in window:
        target = target_by_day.get(d)
        if not target:
            continue
        scored += 1
        got = intake_by_day.get(d, 0.0)
        if got and abs(got - target) / target <= tolerance:
            hits += 1
    if scored == 0:
        return None
    return hits / scored


def mean_of(values: Iterable[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None
