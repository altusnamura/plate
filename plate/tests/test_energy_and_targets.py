"""The adaptive energy model and the guideline-derived nutrient targets.

The most valuable test in this file is
:func:`test_calibration_recovers_a_known_tdee`: it fabricates a subject whose
true expenditure is known and whose tracker lies by a fixed amount, then checks
that the energy-balance reconciliation finds the truth. If that ever breaks,
every calorie target the app produces is wrong, and nothing else in the app would
tell you.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from app.engine import energy
from app.engine import targets as tg

TODAY = date(2026, 8, 14)


def synth(true_tdee=2650.0, eaten=2150.0, tracker_bias=1.12, days=60, start_lb=210.0, seed=5):
    """A subject with a known metabolism and a consistently wrong tracker."""
    rng = random.Random(seed)
    weight, intake, burn = {}, {}, {}
    w = start_lb
    for i in range(days, 0, -1):
        day = TODAY - timedelta(days=i)
        w += (eaten - true_tdee) / energy.KCAL_PER_LB
        weight[day] = w + rng.gauss(0, 1.0)          # daily water noise
        intake[day] = eaten + rng.gauss(0, 150)
        burn[day] = true_tdee * tracker_bias + rng.gauss(0, 200)
    return weight, intake, burn


# --------------------------------------------------------------------------
# weight trend
# --------------------------------------------------------------------------


def test_trend_smooths_out_a_single_bad_reading():
    """One 6 lb water spike must barely move the trend."""
    base = {TODAY - timedelta(days=i): 200.0 for i in range(30, 0, -1)}
    spiked = dict(base)
    spiked[TODAY - timedelta(days=5)] = 206.0

    clean = energy.latest_trend_lb(energy.ewma_trend(base))
    noisy = energy.latest_trend_lb(energy.ewma_trend(spiked))
    assert abs(noisy - clean) < 1.0


def test_trend_is_gap_aware():
    """A week without weighing must not leave the trend anchored to stale data."""
    readings = {TODAY - timedelta(days=30): 200.0, TODAY - timedelta(days=1): 190.0}
    trend = energy.ewma_trend(readings)
    # With gap-scaled alpha the trend should have moved most of the way.
    assert energy.latest_trend_lb(trend) < 193.0


def test_rate_matches_a_known_slope():
    weight = {
        TODAY - timedelta(days=i): 200.0 - (30 - i) * (1.0 / 7.0)
        for i in range(30, 0, -1)
    }
    rate = energy.trend_rate_lb_per_week(energy.ewma_trend(weight))
    assert rate == pytest.approx(-1.0, abs=0.2)


def test_rate_needs_a_minimum_history():
    assert energy.trend_rate_lb_per_week(energy.ewma_trend({TODAY: 200.0})) is None


# --------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tracker_bias", [1.20, 1.12, 1.00, 0.90])
def test_calibration_recovers_a_known_tdee(tracker_bias):
    """The core promise: find true expenditure from weight change, not the watch.

    Averaged over several noise seeds rather than asserted on one, because the
    recovered value is a statistic over noisy daily weights and food logs. A
    single-sample assertion here would be testing one lucky random draw.
    """
    true_tdee = 2650.0
    recovered, biases = [], []
    for seed in (5, 17, 33, 71, 104):
        weight, intake, burn = synth(true_tdee=true_tdee, tracker_bias=tracker_bias, seed=seed)
        cal = energy.calibrate(
            energy.ewma_trend(weight), intake, burn, fallback_tdee=2400.0, today=TODAY
        )
        assert cal.source == "calibrated"
        assert cal.observed_tdee is not None
        recovered.append(cal.observed_tdee)
        biases.append(cal.tracker_bias_pct)

    mean_tdee = sum(recovered) / len(recovered)
    assert mean_tdee == pytest.approx(true_tdee, rel=0.04), recovered
    # Every individual estimate should at least be in the right ballpark.
    assert all(abs(v - true_tdee) < 250 for v in recovered), recovered

    # And the reported tracker error should track the real one.
    true_bias_pct = (1.0 / tracker_bias - 1.0) * 100
    assert sum(biases) / len(biases) == pytest.approx(true_bias_pct, abs=3.0), biases


def test_calibration_falls_back_without_enough_logs():
    weight, _, burn = synth()
    trend = energy.ewma_trend(weight)
    sparse = {TODAY - timedelta(days=i): 2100.0 for i in (2, 9, 20)}
    cal = energy.calibrate(trend, sparse, burn, fallback_tdee=2400.0, today=TODAY)

    assert cal.source == "tracker"
    assert cal.observed_tdee is None
    assert cal.confidence == 0.0
    assert any("food log" in n for n in cal.notes)


def test_calibration_rejects_absurd_solutions():
    """A corrupt weight series must not yield a 9000 kcal target."""
    weight = {TODAY - timedelta(days=i): 210.0 for i in range(40, 0, -1)}
    weight[TODAY - timedelta(days=1)] = 150.0   # bad scale reading
    intake = {TODAY - timedelta(days=i): 2200.0 for i in range(40, 0, -1)}
    burn = {TODAY - timedelta(days=i): 2600.0 for i in range(40, 0, -1)}

    cal = energy.calibrate(energy.ewma_trend(weight), intake, burn,
                           fallback_tdee=2600.0, today=TODAY)
    assert 1000 <= cal.tdee <= 6000
    assert cal.observed_tdee is None or 1000 <= cal.observed_tdee <= 6000


def test_flat_trend_lowers_confidence():
    weight = {TODAY - timedelta(days=i): 200.0 for i in range(40, 0, -1)}
    intake = {TODAY - timedelta(days=i): 2400.0 for i in range(40, 0, -1)}
    burn = {TODAY - timedelta(days=i): 2400.0 for i in range(40, 0, -1)}
    cal = energy.calibrate(energy.ewma_trend(weight), intake, burn,
                           fallback_tdee=2400.0, today=TODAY)
    assert cal.confidence <= 0.5


# --------------------------------------------------------------------------
# resting rate and targets
# --------------------------------------------------------------------------


def test_katch_mcardle_used_when_body_fat_known():
    _, formula = energy.resting_rate(190, 70, 45, "male", body_fat_pct=22.0)
    assert formula == "katch-mcardle"
    _, formula2 = energy.resting_rate(190, 70, 45, "male")
    assert formula2 == "mifflin-st-jeor"


def test_implausible_body_fat_falls_back_to_mifflin():
    _, formula = energy.resting_rate(190, 70, 45, "male", body_fat_pct=95.0)
    assert formula == "mifflin-st-jeor"


def _calibration(tdee: float) -> energy.Calibration:
    return energy.Calibration(
        tdee=tdee, prior_tdee=tdee, observed_tdee=tdee, confidence=0.8,
        tracker_bias=1.0, window_days=28, logged_days=25, source="calibrated",
    )


def test_deficit_matches_the_requested_rate():
    target = energy.calorie_target(
        _calibration(2600), weight_lb=200, sex="male", goal="lose",
        target_rate_lb_per_week=-1.0, resting_kcal=1800,
    )
    assert target.base_kcal == pytest.approx(2600 - 500, abs=5)


def test_rate_capped_at_one_percent_of_body_mass():
    target = energy.calorie_target(
        _calibration(2600), weight_lb=150, sex="male", goal="lose",
        target_rate_lb_per_week=-2.5, resting_kcal=1600,
    )
    assert target.rate_capped
    assert abs(target.planned_rate_lb_per_week) <= 1.5 + 1e-6


def test_target_never_goes_below_resting_rate():
    """An aggressive goal on a small person must not produce a starvation target.

    Asserts the outcome rather than which guard produced it — here the 25%
    deficit cap happens to bite before the resting-rate floor does, and either
    is a correct way to arrive at a safe number.
    """
    target = energy.calorie_target(
        _calibration(1900), weight_lb=120, sex="female", goal="lose",
        target_rate_lb_per_week=-2.0, resting_kcal=1400,
    )
    assert target.base_kcal >= 1400
    assert target.rate_capped or target.floor_applied


def test_resting_floor_binds_when_the_deficit_cap_is_not_enough():
    """A high resting rate relative to TDEE must raise the target explicitly."""
    target = energy.calorie_target(
        _calibration(1900), weight_lb=150, sex="female", goal="lose",
        target_rate_lb_per_week=-1.5, resting_kcal=1650,
    )
    assert target.floor_applied
    assert target.base_kcal >= 1650
    assert any("resting metabolic rate" in n for n in target.notes)


def test_absolute_floor_respected_for_men():
    target = energy.calorie_target(
        _calibration(1700), weight_lb=140, sex="male", goal="lose",
        target_rate_lb_per_week=-2.0, resting_kcal=1200,
    )
    assert target.base_kcal >= energy.ABSOLUTE_FLOOR_KCAL["male"]


def test_active_day_adds_calories_back():
    target = energy.calorie_target(
        _calibration(2600), weight_lb=200, sex="male", goal="lose",
        target_rate_lb_per_week=-1.0, resting_kcal=1800,
        today_burn=3400, typical_burn=2600, activity_passthrough=0.75,
    )
    assert target.activity_adjustment == pytest.approx(600, abs=1)
    assert target.kcal > target.base_kcal


def test_quiet_day_does_not_cut_calories_further():
    """Chasing a low tracker reading downward just amplifies its noise."""
    target = energy.calorie_target(
        _calibration(2600), weight_lb=200, sex="male", goal="lose",
        target_rate_lb_per_week=-1.0, resting_kcal=1800,
        today_burn=2100, typical_burn=2600,
    )
    assert target.activity_adjustment == 0.0
    assert target.kcal == target.base_kcal


# --------------------------------------------------------------------------
# blood pressure
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sys_, dia, expected", [
    (112, 70, tg.BPCategory.NORMAL),
    (124, 76, tg.BPCategory.ELEVATED),
    (132, 78, tg.BPCategory.STAGE_1),
    (118, 84, tg.BPCategory.STAGE_1),     # diastolic alone decides
    (145, 88, tg.BPCategory.STAGE_2),
    (185, 100, tg.BPCategory.CRISIS),
])
def test_bp_categories_follow_acc_aha(sys_, dia, expected):
    assert tg.classify_bp_values(sys_, dia) is expected


def test_bp_unknown_without_readings():
    status = tg.summarise_bp({}, {}, today=TODAY)
    assert status.category is tg.BPCategory.UNKNOWN
    assert not status.known


def test_crisis_readings_advise_seeing_a_doctor():
    status = tg.summarise_bp(
        {TODAY - timedelta(days=i): 190.0 for i in range(6)},
        {TODAY - timedelta(days=i): 125.0 for i in range(6)},
        today=TODAY,
    )
    assert status.category is tg.BPCategory.CRISIS
    assert any("doctor" in a for a in status.advice)


def test_elevated_bp_tightens_sodium_and_raises_potassium(normal_bp, high_bp):
    calm = tg.build_targets(2200, 190, "lose", normal_bp, sex="male")
    tense = tg.build_targets(2200, 190, "lose", high_bp, sex="male")

    assert calm.targets["sodium_mg"].ceiling == 2300
    assert tense.targets["sodium_mg"].ceiling == 1500
    assert tense.targets["potassium_mg"].goal > calm.targets["potassium_mg"].goal
    assert tense.targets["satfat_g"].ceiling < calm.targets["satfat_g"].ceiling


def test_elevated_bp_warns_about_potassium_and_medication(high_bp):
    t = tg.build_targets(2200, 190, "lose", high_bp, sex="male")
    joined = " ".join(t.disclaimers)
    assert "kidney" in joined and "doctor" in joined


def test_targets_always_carry_a_disclaimer(targets):
    assert targets.disclaimers
    assert "not medical advice" in " ".join(targets.disclaimers)


def test_protein_capped_at_forty_percent_of_energy():
    """A heavy person on a small budget must not be told to eat 300 g of protein."""
    t = tg.build_targets(kcal=1500, weight_lb=280, goal="lose",
                         bp=tg.summarise_bp({}, {}, today=TODAY), sex="male")
    protein_kcal = t.targets["protein_g"].goal * 4
    assert protein_kcal <= 1500 * 0.40 + 1
    assert any("40%" in n for n in t.notes)


def test_fibre_scales_with_energy():
    small = tg.build_targets(1600, 150, "lose", tg.summarise_bp({}, {}, today=TODAY))
    large = tg.build_targets(3000, 150, "gain", tg.summarise_bp({}, {}, today=TODAY))
    assert large.targets["fiber_g"].goal > small.targets["fiber_g"].goal
    assert small.targets["fiber_g"].goal == pytest.approx(14 * 1.6, abs=0.1)


def test_dash_score_rewards_hitting_the_pattern():
    on_pattern = {
        "vegetables": 4.5, "fruit": 4.5, "grains": 7, "dairy_lowfat": 2.5,
        "lean_protein": 4, "nuts_legumes": 0.7, "fats_oils": 2.5, "sweets": 0.3,
    }
    score = tg.dash_score(on_pattern, 2000).score
    assert score > 95

    poor = tg.dash_score({"grains": 10, "sweets": 4}, 2000)
    assert poor.score < 40
    assert "vegetables" in poor.shortfalls
