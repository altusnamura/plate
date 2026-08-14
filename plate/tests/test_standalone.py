"""The standalone path: no Home Assistant, measurements typed by hand.

The promise being tested is that hand entry is a *first-class* way to run this,
not a degraded one — the same trend smoothing, the same calibration, the same
DASH adjustments, just from sparser data. Plus the thing most likely to be got
wrong later: a hand-typed reading must never be silently replaced by a sync.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(tmp_path):
    """A fully isolated app instance with no Home Assistant reachable."""
    data = tmp_path / "data"
    config = tmp_path / "config"
    data.mkdir()
    config.mkdir()
    (data / "options.json").write_text('{"log_level": "warning"}', encoding="utf-8")

    saved = {k: os.environ.get(k) for k in
             ("PLATE_DATA_DIR", "PLATE_USER_DIR", "PLATE_OPTIONS_FILE",
              "HA_URL", "HA_TOKEN", "SUPERVISOR_TOKEN")}
    os.environ["PLATE_DATA_DIR"] = str(data)
    os.environ["PLATE_USER_DIR"] = str(config)
    os.environ["PLATE_OPTIONS_FILE"] = str(data / "options.json")
    for k in ("HA_URL", "HA_TOKEN", "SUPERVISOR_TOKEN"):
        os.environ.pop(k, None)

    # Imported late so it picks up the environment above.
    import app.main as main
    import importlib
    importlib.reload(main)

    with TestClient(main.app) as c:
        yield c

    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_app_runs_with_no_home_assistant(client):
    snap = client.get("/api/today").json()
    assert snap["standalone"] is True
    assert snap["mode"] == "standalone"
    # It still produces a usable plan and target from defaults.
    assert snap["energy"]["target_kcal"] > 1200
    assert snap["plan"]["today"]["meals"]


def test_setup_advice_does_not_mention_entities_when_standalone(client):
    """Telling someone with no HA to "point PLATE at your scale entity" is useless."""
    snap = client.get("/api/today").json()
    joined = " ".join(snap["needs_setup"]).lower()
    assert "entity" not in joined
    assert "measurement" in joined


def test_manual_weight_drives_the_trend(client):
    today = date.today()
    for i in range(28, 0, -2):          # a weigh-in every other day
        day = today - timedelta(days=i)
        client.post("/api/metrics", json={
            "day": day.isoformat(),
            "weight_lb": 210.0 - (28 - i) * 0.14,
        })

    snap = client.get("/api/today?force=true").json()
    trend = snap["trend"]
    assert trend["readings"] == 14
    assert 205 < trend["trend_lb"] < 211
    assert trend["rate_lb_per_week"] is not None
    assert trend["rate_lb_per_week"] < 0     # losing


def test_manual_blood_pressure_tightens_sodium(client):
    """The BP feature has to work without an Omron integration attached."""
    before = client.get("/api/today").json()
    assert before["targets"]["sodium_mg"]["ceiling"] == 2300

    today = date.today()
    for i in range(1, 9):
        client.post("/api/metrics", json={
            "day": (today - timedelta(days=i)).isoformat(),
            "bp_systolic": 144, "bp_diastolic": 92,
        })

    after = client.get("/api/today?force=true").json()
    assert after["bp"]["category"] == "stage_2"
    assert after["targets"]["sodium_mg"]["ceiling"] == 1500
    assert after["targets"]["potassium_mg"]["goal"] == 4700


def test_blood_pressure_requires_both_numbers(client):
    """Half a reading would be categorised as if it were whole."""
    r = client.post("/api/metrics", json={"bp_systolic": 130})
    assert r.status_code == 400
    assert "both numbers" in r.json()["detail"]

    snap = client.get("/api/today?force=true").json()
    assert snap["bp"]["systolic"] is None


def test_second_half_of_a_reading_is_accepted(client):
    """Entering systolic then diastolic separately should end up valid."""
    day = date.today().isoformat()
    client.post("/api/metrics", json={"day": day, "bp_systolic": 128, "bp_diastolic": 82})
    r = client.post("/api/metrics", json={"day": day, "bp_diastolic": 84})
    assert r.status_code == 200

    snap = client.get("/api/today?force=true").json()
    assert snap["bp"]["diastolic"] == 84


@pytest.mark.parametrize("field, value", [
    ("weight_lb", 18.5),        # slipped decimal
    ("weight_lb", 9000),
    ("bp_systolic", 1320),      # extra digit
    ("body_fat_pct", 140),
    ("resting_hr", 5),
])
def test_implausible_values_are_refused_with_a_useful_message(client, field, value):
    r = client.post("/api/metrics", json={field: value})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "plausible range" in detail and "decimal" in detail


def test_future_measurements_refused(client):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    r = client.post("/api/metrics", json={"day": tomorrow, "weight_lb": 200})
    assert r.status_code == 400


def test_empty_submission_refused(client):
    assert client.post("/api/metrics", json={}).status_code == 400


def test_manual_entries_survive_a_home_assistant_sync(client):
    """The rule that matters if HA gets connected later.

    You stood on the scale and typed the number. An integration reporting
    something different for that day is guessing at a day boundary; your value
    wins.
    """
    import app.main as main

    service = main.app.state.service
    day = date.today() - timedelta(days=3)

    client.post("/api/metrics", json={"day": day.isoformat(), "weight_lb": 199.5})

    # Simulate what a sync would write for the same day.
    written = service.store.put_metrics(
        "weight_lb", {day: 205.0}, source="ha", protect_manual=True
    )
    assert written == 1  # the statement ran...
    assert service.store.metrics("weight_lb")[day] == 199.5  # ...but changed nothing

    # A sync into a day you never touched still lands normally.
    fresh = day - timedelta(days=1)
    service.store.put_metrics("weight_lb", {fresh: 206.0}, source="ha", protect_manual=True)
    assert service.store.metrics("weight_lb")[fresh] == 206.0


def test_manual_entry_can_be_corrected(client):
    day = date.today().isoformat()
    client.post("/api/metrics", json={"day": day, "weight_lb": 201.0})
    client.post("/api/metrics", json={"day": day, "weight_lb": 202.5})

    rows = client.get("/api/metrics?days=7").json()
    entry = next(d for d in rows["days"] if d["day"] == day)
    assert entry["values"]["weight_lb"] == 202.5
    assert entry["sources"]["weight_lb"] == "manual"


def test_measurements_can_be_deleted(client):
    day = date.today().isoformat()
    client.post("/api/metrics", json={"day": day, "weight_lb": 201.0})
    assert client.post("/api/metrics/delete", json={"day": day, "key": "weight_lb"}).status_code == 200
    assert client.post("/api/metrics/delete", json={"day": day, "key": "weight_lb"}).status_code == 404


def test_calibration_engages_from_hand_entered_data(client):
    """The whole point: no wearable, and the engine still learns your metabolism.

    Feeds 40 days of weights consistent with a known TDEE plus matching food
    logs, then checks PLATE recovers roughly that expenditure — with no tracker
    data at all, which is the standalone user's situation exactly.
    """
    true_tdee = 2600.0
    eaten = 2100.0
    today = date.today()
    weight = 215.0

    for i in range(40, 0, -1):
        day = today - timedelta(days=i)
        weight += (eaten - true_tdee) / 3500.0
        client.post("/api/metrics", json={
            "day": day.isoformat(), "weight_lb": round(weight, 1),
        })
        client.post("/api/log", json={
            "day": day.isoformat(), "nutrients": {"kcal": eaten, "protein_g": 150},
            "label": "backfill",
        })

    snap = client.get("/api/today?force=true").json()
    energy = snap["energy"]
    assert energy["source"] == "calibrated"
    assert energy["observed_tdee"] == pytest.approx(true_tdee, abs=250)
    assert energy["confidence"] > 0.4
    # And with no tracker to compare against, it reports no bias rather than a
    # made-up one.
    assert energy["tracker_bias_pct"] is None


def test_settings_reports_standalone(client):
    settings = client.get("/api/settings").json()
    assert settings["standalone"] is True


def test_refresh_does_not_fail_without_home_assistant(client):
    r = client.post("/api/refresh")
    assert r.status_code == 200
    assert r.json()["sync"]["ok"] is False   # honest about what didn't happen
    assert r.json()["snapshot"]["energy"]["target_kcal"] > 0
