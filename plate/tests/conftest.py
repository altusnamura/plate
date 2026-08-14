import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engine.library import load_library  # noqa: E402
from app.engine import targets as tg  # noqa: E402

TODAY = date(2026, 8, 14)


@pytest.fixture(scope="session")
def library():
    """The real bundled data set.

    Tested against the shipped data rather than fixtures on purpose: most of the
    failure modes in this app are data problems (a unit a food can't convert, a
    recipe whose servings are out by a factor of two), and a synthetic library
    would test the code while letting exactly those through.
    """
    return load_library(ROOT / "app" / "data")


@pytest.fixture(scope="session")
def normal_bp():
    return tg.summarise_bp({TODAY: 112.0}, {TODAY: 70.0}, today=TODAY)


@pytest.fixture(scope="session")
def high_bp():
    readings_sys = {TODAY.replace(day=d): 142.0 for d in range(1, 13)}
    readings_dia = {TODAY.replace(day=d): 92.0 for d in range(1, 13)}
    return tg.summarise_bp(readings_sys, readings_dia, today=TODAY)


@pytest.fixture(scope="session")
def targets(normal_bp):
    return tg.build_targets(kcal=2200, weight_lb=190, goal="lose", bp=normal_bp, sex="male")
