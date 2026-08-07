"""Polls the real dashboard's `/api/state` for the duration of the WAN-chaos
window and writes every snapshot to a JSON array.

Run from inside the guest, against the real `wifucked.service` (real
`Daemon`, real HAL, no `MOCK_HW`) while the host degrades the two WAN taps
(`appliance/tests/qemu/chaos_wan.sh`). This is the raw data
`aggregate_report.py` turns into the link-health/switch-timeline graphs — a
plain list of what the real allocator believed at each point in time, read
back over HTTP exactly as a human watching the dashboard would see it.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from base64 import b64encode
from pathlib import Path


def _get(url: str, token: str) -> dict | None:
    if not url.startswith("http://"):
        raise ValueError(f"refusing non-http URL: {url!r}")
    req = urllib.request.Request(url)  # noqa: S310 — scheme checked above
    req.add_header("Authorization", "Basic " + b64encode(f"wifucked:{token}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 — scheme checked above
            return json.loads(resp.read())
    except Exception:  # a missed poll is a gap in the graph, not a fatal error
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", required=True, help="dashboard base URL, e.g. http://10.44.0.1:8080"
    )
    parser.add_argument("--token", required=True)
    parser.add_argument("--duration-s", type=float, required=True)
    parser.add_argument("--interval-s", type=float, default=3.0)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    snapshots: list[dict] = []
    started = time.monotonic()
    while time.monotonic() - started < args.duration_s:
        t = round(time.monotonic() - started, 1)
        state = _get(args.url.rstrip("/") + "/api/state", args.token)
        snapshots.append({"t": t, "state": state})
        time.sleep(args.interval_s)

    decisions = _get(args.url.rstrip("/") + "/api/decisions?limit=500", args.token) or []

    args.out.write_text(json.dumps({"snapshots": snapshots, "decisions": decisions}, indent=2))
    print(f"wrote {len(snapshots)} snapshots and {len(decisions)} decisions to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
