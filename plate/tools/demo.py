"""End-to-end dry run with synthetic health data.

Exercises the whole chain — weight trend, TDEE calibration, targets, the planner,
the shopping list — without Home Assistant, a container or a browser. Useful for
seeing what the app will actually produce, and the fastest way to check that a
change to the cost weights improved anything:

    python plate/tools/demo.py
    python plate/tools/demo.py --bp elevated --goal lose --rate -1.5
    python plate/tools/demo.py --seed 7 --iterations 12000

The synthetic subject loses weight at a rate that implies a TDEE about 12% below
what their tracker claims, so the calibration has something real to find.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engine import energy, targets as tg  # noqa: E402
from app.engine.library import load_library  # noqa: E402
from app.engine.models import Meal  # noqa: E402
from app.engine.planner import PlanRequest, plan_menu  # noqa: E402
from app.engine.shopping import build_shopping_list  # noqa: E402

TODAY = date(2026, 8, 14)


def synth_metrics(days: int = 60, tracker_bias: float = 1.12, seed: int = 3):
    """Fabricate a plausible 60 days of scale, tracker and cuff readings.

    ``tracker_bias`` is how much the wrist tracker over-reports: at 1.12 it
    claims 12% more burn than the weight trend can justify, which is squarely in
    the range these devices actually miss by.
    """
    rng = random.Random(seed)
    weight, intake, burn, sys_bp, dia_bp = {}, {}, {}, {}, {}

    true_tdee = 2650.0
    eaten = 2150.0            # ~500 kcal/day deficit -> about 1 lb/week
    w = 208.0

    for i in range(days, 0, -1):
        day = TODAY - timedelta(days=i)
        # Real physiology: trend follows energy balance, plus water noise.
        w += (eaten - true_tdee) / energy.KCAL_PER_LB
        weight[day] = round(w + rng.gauss(0, 1.1), 1)
        if rng.random() > 0.12:                      # some days go unlogged
            intake[day] = round(eaten + rng.gauss(0, 180))
        burn[day] = round(true_tdee * tracker_bias + rng.gauss(0, 220))
        if i % 2 == 0:
            sys_bp[day] = round(rng.gauss(134, 6))
            dia_bp[day] = round(rng.gauss(84, 4))
    return weight, intake, burn, sys_bp, dia_bp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--goal", default="lose", choices=["lose", "maintain", "gain"])
    ap.add_argument("--rate", type=float, default=-1.0, help="lb per week")
    ap.add_argument("--bp", default="real", choices=["real", "normal", "elevated"])
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--iterations", type=int, default=4000)
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    lib = load_library(ROOT / "app" / "data")
    weight, intake, burn, sys_bp, dia_bp = synth_metrics()

    # ---- weight trend --------------------------------------------------
    trend = energy.ewma_trend(weight)
    rate = energy.trend_rate_lb_per_week(trend)
    current = energy.latest_trend_lb(trend)
    print("=" * 78)
    print("METRICS")
    print("=" * 78)
    print(f"  scale readings      {len(weight)} over {args.days and 60} days")
    print(f"  raw weight today    {weight[max(weight)]:.1f} lb")
    print(f"  trend weight        {current:.1f} lb  (EWMA, 7-day half life)")
    print(f"  observed rate       {rate:+.2f} lb/week")

    # ---- calibration ---------------------------------------------------
    rmr, formula = energy.resting_rate(current, 70.0, 46.0, "male", body_fat_pct=24.0)
    cal = energy.calibrate(
        trend, intake, burn,
        fallback_tdee=rmr * 1.45,
        today=TODAY,
    )
    print()
    print(f"  resting rate        {rmr:.0f} kcal ({formula})")
    print(f"  tracker says        {cal.prior_tdee:.0f} kcal/day")
    print(f"  energy balance says {cal.observed_tdee:.0f} kcal/day"
          if cal.observed_tdee else "  energy balance      not solvable yet")
    print(f"  calibrated TDEE     {cal.tdee:.0f} kcal/day  "
          f"(confidence {cal.confidence:.0%}, source {cal.source})")
    if cal.tracker_bias_pct is not None:
        print(f"  tracker bias        {cal.tracker_bias_pct:+.0f}%")
    for note in cal.notes:
        print(f"    - {note}")

    # ---- blood pressure and targets ------------------------------------
    if args.bp == "normal":
        bp = tg.summarise_bp({TODAY: 114.0}, {TODAY: 72.0}, today=TODAY)
    elif args.bp == "elevated":
        bp = tg.summarise_bp({TODAY: 142.0}, {TODAY: 91.0}, today=TODAY)
    else:
        bp = tg.summarise_bp(sys_bp, dia_bp, today=TODAY)

    ct = energy.calorie_target(
        cal, weight_lb=current, sex="male", goal=args.goal,
        target_rate_lb_per_week=args.rate, resting_kcal=rmr,
        today_burn=burn.get(TODAY - timedelta(days=1)),
        typical_burn=cal.prior_tdee,
    )
    nt = tg.build_targets(
        kcal=ct.base_kcal, weight_lb=current, goal=args.goal, bp=bp,
        body_fat_pct=24.0, sex="male",
    )

    print()
    print("=" * 78)
    print("TARGETS")
    print("=" * 78)
    print(f"  blood pressure      {bp.systolic:.0f}/{bp.diastolic:.0f} — {bp.category.label}"
          f" ({bp.readings} readings)")
    print(f"  calorie target      {ct.base_kcal:.0f} kcal/day "
          f"(deficit {ct.deficit_kcal:+.0f}, planned {ct.planned_rate_lb_per_week:+.2f} lb/wk)")
    if ct.activity_adjustment:
        print(f"  today's adjustment  {ct.activity_adjustment:+.0f} kcal for extra activity")
    for name, t in nt.targets.items():
        bits = []
        if t.goal is not None:
            bits.append(f"goal {t.goal:.0f}")
        if t.floor is not None:
            bits.append(f"min {t.floor:.0f}")
        if t.ceiling is not None:
            bits.append(f"max {t.ceiling:.0f}")
        print(f"    {name:<14} {', '.join(bits):<34} {t.unit}")
    for note in nt.notes:
        print(f"    ! {note}")

    goal_date = energy.projected_goal_date(current, 175.0, rate, today=TODAY)
    if goal_date:
        print(f"  175 lb projected    {goal_date.isoformat()} at the current rate")

    # ---- plan ----------------------------------------------------------
    days = [TODAY + timedelta(days=i) for i in range(args.days)]
    req = PlanRequest(
        start=TODAY,
        days=args.days,
        kcal_by_day={d: ct.base_kcal for d in days},
        targets=nt,
        meals=(Meal.BREAKFAST, Meal.LUNCH, Meal.DINNER),
        snacks_per_day=1,
        seed=args.seed,
        iterations=args.iterations,
    )

    t0 = time.perf_counter()
    plan = plan_menu(lib, req)
    elapsed = time.perf_counter() - t0

    print()
    print("=" * 78)
    print(f"MENU  (cost {plan.cost:.1f}, planned in {elapsed:.2f}s)")
    print("=" * 78)
    for d in plan.days:
        flag = "" if abs(d.nutrition.kcal - d.kcal_target) / d.kcal_target < 0.06 else "  <-- off"
        print(f"\n{d.day.strftime('%a %d %b')}   {d.nutrition.kcal:.0f}/{d.kcal_target:.0f} kcal   "
              f"P{d.nutrition.get('protein_g'):.0f} F{d.nutrition.get('fiber_g'):.0f} "
              f"Na{d.nutrition.get('sodium_mg'):.0f} K{d.nutrition.get('potassium_mg'):.0f}   "
              f"DASH {d.dash.score:.0f}   {d.active_min} min active{flag}")
        for m in d.meals:
            veg = "v" if m.recipe.vegetarian else " "
            left = f"  (leftovers from {m.leftover_from.strftime('%a')})" if m.leftover_from else ""
            print(f"    {m.slot.meal.value:<9} {veg} {m.recipe.title[:40]:<42}"
                  f"{m.servings:>4.2f}x {m.nutrition.kcal:>5.0f} kcal{left}")

    print()
    print("DIAGNOSTICS")
    for k, v in plan.diagnostics.items():
        if k == "issues":
            continue
        print(f"  {k:<22} {v}")
    for issue in plan.diagnostics["issues"]:
        print(f"  ! {issue}")

    # ---- shopping ------------------------------------------------------
    used_by: dict[str, list[str]] = {}
    for meal in plan.all_meals():
        for fid in meal.recipe.food_ids:
            used_by.setdefault(fid, [])
            if meal.recipe.title not in used_by[fid]:
                used_by[fid].append(meal.recipe.title)

    sl = build_shopping_list(
        lib,
        plan.grams_by_food,
        enabled_stores=["trader-joes", "safeway", "whole-foods"],
        pantry={},
        used_by=used_by,
        today=TODAY,
    )

    print()
    print("=" * 78)
    print(f"SHOPPING LIST   estimated ${sl.total_estimate:.2f}"
          if sl.total_estimate else "SHOPPING LIST")
    print("=" * 78)
    for store in sl.stores:
        sub = f"${store.subtotal:.2f}" if store.subtotal is not None else "no prices"
        print(f"\n{store.store.name.upper()}   {len(store.lines)} items   {sub}")
        for group in store.as_dict()["aisles"]:
            print(f"  {group['label']}")
            for line in group["lines"]:
                cost = f"${line['est_cost']:.2f}" if line["est_cost"] is not None else "     "
                print(f"    [ ] {line['name'][:30]:<32}{line['quantity_text']:<20}{cost}")
    if sl.unmatched:
        print(f"\nNO STORE MAPPING ({len(sl.unmatched)})")
        for line in sl.unmatched:
            print(f"    [ ] {line.name:<32}{line.quantity_text}")
    print()
    for note in sl.notes:
        print(f"  note: {note}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
