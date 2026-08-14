"""Validate the YAML data set without booting the whole app.

Run this after editing any food, recipe or product file:

    python plate/tools/validate.py

It loads the library exactly as the add-on does, so a clean run here means the
add-on will start. Hard errors exit non-zero; warnings are printed and tolerated,
because they flag things worth a look rather than things that are broken.

Pass ``--recipes`` to also dump a per-recipe nutrition table, which is the
quickest way to spot a serving count that's out by a factor of two.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engine.library import DataError, load_library  # noqa: E402
from app.engine.models import Meal  # noqa: E402
from app.engine.nutrients import CORE  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recipes", action="store_true", help="print per-recipe nutrition")
    ap.add_argument("--user-dir", type=Path, default=None, help="overlay directory")
    args = ap.parse_args()

    data_dir = ROOT / "app" / "data"
    try:
        lib = load_library(data_dir, args.user_dir)
    except DataError as exc:
        print(f"DATA ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        f"OK  {len(lib.foods)} foods, {len(lib.recipes)} recipes, "
        f"{len(lib.stores)} stores, {len(lib.products)} products"
    )

    for meal in Meal:
        pool = lib.recipes_for(meal)
        veg = sum(1 for r in pool if r.vegetarian)
        print(f"    {meal.value:<10} {len(pool):>3} recipes ({veg} vegetarian)")

    unmapped = 0
    for fid in lib.foods:
        if not lib.products_for(fid, list(lib.stores)) and not lib.foods[fid].pantry_staple:
            unmapped += 1
    print(f"    {unmapped} non-staple foods have no store product")

    if lib.warnings:
        print(f"\n{len(lib.warnings)} warning(s):")
        for w in lib.warnings:
            print(f"  - {w}")

    if args.recipes:
        print(f"\n{'recipe':<38}{'kcal':>6}{'P':>6}{'F':>6}{'C':>6}{'fib':>6}{'Na':>7}{'K':>7}  veg")
        for r in sorted(lib.recipes.values(), key=lambda x: x.id):
            n = r.nutrition
            print(
                f"{r.id[:37]:<38}{n.kcal:>6.0f}{n.get('protein_g'):>6.0f}"
                f"{n.get('fat_g'):>6.0f}{n.get('carb_g'):>6.0f}{n.get('fiber_g'):>6.0f}"
                f"{n.get('sodium_mg'):>7.0f}{n.get('potassium_mg'):>7.0f}"
                f"  {'V' if r.vegetarian else '-'}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
