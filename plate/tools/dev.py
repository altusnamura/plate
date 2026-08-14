"""Run PLATE locally, outside Home Assistant.

The add-on normally gets its paths from run.sh and its data from the Supervisor.
This sets sane local equivalents so you can develop against a real browser:

    python plate/tools/dev.py            # http://localhost:8099
    python plate/tools/dev.py --port 8100

Data lands in ``.devdata/`` and user overlays in ``.devconfig/``, both beside the
repo and both safe to delete. To talk to a real Home Assistant, export ``HA_URL``
and ``HA_TOKEN`` (a long-lived access token) before running — without them the app
starts fine and simply reports that no metrics are configured.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "plate"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()

    data = REPO / ".devdata"
    user = REPO / ".devconfig"
    data.mkdir(exist_ok=True)
    user.mkdir(exist_ok=True)

    options = data / "options.json"
    if not options.exists():
        options.write_text('{"log_level": "info"}', encoding="utf-8")

    os.environ.setdefault("PLATE_DATA_DIR", str(data))
    os.environ.setdefault("PLATE_USER_DIR", str(user))
    os.environ.setdefault("PLATE_OPTIONS_FILE", str(options))

    sys.path.insert(0, str(PKG))
    import uvicorn

    print(f"PLATE dev server on http://{args.host}:{args.port}")
    print(f"  data:   {data}")
    print(f"  config: {user}")
    if not (os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN")):
        print("  note:   no HA_TOKEN set, so no metrics will sync")

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        app_dir=str(PKG),
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
