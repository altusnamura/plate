"""Run PLATE as a standalone app.

    python run.py                 # http://localhost:8099, this machine only
    python run.py --lan           # also reachable from your phone on the wifi
    python run.py --port 9000

No Home Assistant required. Weight and blood pressure are entered by hand in the
app, and everything else — the trend smoothing, the TDEE calibration, the DASH
adjustments, the planner — works exactly as it does with sensors attached, just
from sparser data.

Data lives in ``data/`` beside this file and your own recipe/price overrides in
``config/``. Both are plain files you can back up by copying the folder.

To connect a Home Assistant later, set two environment variables before running
and the app picks them up on the next start:

    HA_URL=http://homeassistant.local:8123
    HA_TOKEN=<a long-lived access token from your HA profile page>

Nothing you typed by hand is lost when you do — manual measurements always win
over a sync for the same day.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PKG = ROOT / "plate"


def local_ip() -> str | None:
    """Best guess at this machine's LAN address, for the phone-friendly URL.

    Connecting a UDP socket to an outside address doesn't send anything; it just
    makes the OS pick the interface it would route through, which is a more
    reliable answer than resolving the hostname.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.4)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Run PLATE standalone.")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument(
        "--lan",
        action="store_true",
        help="listen on all interfaces so other devices on your network can reach it",
    )
    ap.add_argument("--host", default=None, help="override the bind address entirely")
    ap.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--config-dir", type=Path, default=ROOT / "config")
    args = ap.parse_args()

    host = args.host or ("0.0.0.0" if args.lan else "127.0.0.1")

    args.data_dir.mkdir(parents=True, exist_ok=True)
    args.config_dir.mkdir(parents=True, exist_ok=True)
    options = args.data_dir / "options.json"
    if not options.exists():
        options.write_text('{"log_level": "info"}\n', encoding="utf-8")

    os.environ.setdefault("PLATE_DATA_DIR", str(args.data_dir))
    os.environ.setdefault("PLATE_USER_DIR", str(args.config_dir))
    os.environ.setdefault("PLATE_OPTIONS_FILE", str(options))

    sys.path.insert(0, str(PKG))
    try:
        import uvicorn
    except ImportError:
        print(
            "Missing dependencies. Install them with:\n"
            f"    {sys.executable} -m pip install -r plate/requirements.txt",
            file=sys.stderr,
        )
        return 1

    print()
    print("  PLATE")
    print(f"    on this machine   http://localhost:{args.port}")
    if args.lan:
        ip = local_ip()
        if ip:
            print(f"    on your phone     http://{ip}:{args.port}")
        else:
            print("    on your phone     (could not determine this machine's LAN address)")
    else:
        print("    phone access      off — start with --lan to enable it")
    print(f"    data              {args.data_dir}")
    print(f"    your overrides    {args.config_dir}")
    if os.environ.get("HA_TOKEN"):
        print(f"    home assistant    {os.environ.get('HA_URL', 'configured')}")
    else:
        print("    home assistant    not connected (enter measurements in the app)")
    print()
    print("  Ctrl+C to stop.")
    print()

    if args.lan:
        # Worth saying plainly: this binds to every interface with no
        # authentication in front of it, because the add-on deployment relies on
        # Home Assistant's Ingress for that. Fine on a home network, not on a
        # public or shared one.
        print("  Note: --lan serves to your whole network with no password.")
        print("        Fine at home; don't do it on public or shared wifi.")
        print()

    # Python block-buffers stdout when it isn't a terminal, so without this the
    # banner appears after uvicorn's log lines — or not at all if the output is
    # being piped somewhere that only reads the first few lines.
    sys.stdout.flush()

    uvicorn.run(
        "app.main:app",
        host=host,
        port=args.port,
        reload=args.reload,
        app_dir=str(PKG),
        log_level="info",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
