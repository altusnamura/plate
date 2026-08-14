"""Boot the app in-process and hit every endpoint.

Faster and more deterministic than starting uvicorn and curling it, and it runs
the real lifespan (library load, database creation, background task) so a startup
regression shows up here rather than in the add-on log.

    python plate/tools/smoke.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

GETS = [
    "/healthz",
    "/api/health",
    "/api/today",
    "/api/week",
    "/api/shopping",
    "/api/insight?days=30",
    "/api/settings",
    "/api/recipes?meal=lunch&vegetarian=true",
    "/api/recipes/chickpea-shawarma-bowl",
    "/api/log",
    "/api/entities/discover",
    "/",
    "/css/app.css",
    "/js/app.js",
    "/manifest.webmanifest",
]

POSTS = [
    ("/api/week/regenerate", {}),
    ("/api/pantry", {"food_id": "olive-oil", "grams": 500}),
]


def main() -> int:
    failures = 0
    with TestClient(app) as client:
        for path in GETS:
            r = client.get(path)
            flag = "ok " if r.status_code < 400 else "FAIL"
            print(f"{flag} {r.status_code}  GET  {path}")
            if r.status_code >= 400:
                failures += 1
                print("      " + r.text[:300])

        for path, body in POSTS:
            r = client.post(path, json=body)
            flag = "ok " if r.status_code < 400 else "FAIL"
            print(f"{flag} {r.status_code}  POST {path}")
            if r.status_code >= 400:
                failures += 1
                print("      " + r.text[:300])

        # Log a meal planned for TODAY, then confirm it moves today's totals.
        # Using any old slot would log against that slot's own day and leave
        # today at zero, which looks like success and proves nothing.
        today = client.get("/api/today").json()
        before = today["eaten"]["kcal"]
        meals = ((today.get("plan") or {}).get("today") or {}).get("meals") or []
        if not meals:
            print("FAIL      no meals planned for today; cannot test logging")
            failures += 1
        else:
            slot = meals[0]["slot"]
            expected = meals[0]["kcal"]
            r = client.post("/api/log", json={"slot": slot})
            ok = r.status_code < 400
            print(f"{'ok ' if ok else 'FAIL'} {r.status_code}  POST /api/log ({slot})")
            if not ok:
                failures += 1
                print("      " + r.text[:300])
            else:
                after = r.json()["snapshot"]["eaten"]["kcal"]
                moved = after - before
                good = abs(moved - expected) <= max(2.0, expected * 0.02)
                print(
                    f"{'ok ' if good else 'FAIL'}      today's intake {before:.0f} -> {after:.0f} "
                    f"kcal (meal was {expected:.0f})"
                )
                if not good:
                    failures += 1
                client.post("/api/log/delete", json={"slot": slot})

        snap = client.get("/api/today").json()
        print("\n--- snapshot shape ---")
        print(json.dumps(
            {
                "target_kcal": snap["energy"]["target_kcal"],
                "tdee": snap["energy"]["tdee"],
                "source": snap["energy"]["source"],
                "trend_lb": snap["trend"]["trend_lb"],
                "needs_setup": snap["needs_setup"],
                "next_meal": (snap["plan"]["next_meal"] or {}).get("title"),
                "shopping_outstanding": snap["shopping"]["outstanding"],
            },
            indent=2,
        ))

    print(f"\n{'PASS' if not failures else str(failures) + ' FAILURES'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
