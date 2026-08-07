"""Runs the real control-plane daemon and dashboard for the AP E2E proof.

This is not a reimplementation of ``wifucked.__main__`` — it calls the exact
same ``Daemon`` class and ``create_app()`` factory production does, under
``MOCK_HW=1`` (the standard, required dev/test HAL seam, SOP-003), so the
control loops and telemetry never touch real ``tc``/``nft``/``hostapd_cli``
in this harness. hostapd and dnsmasq are started independently by the
orchestrator, exactly as ADR-011 requires on a real device.

The one deliberate deviation from ``__main__.main()``: the dashboard's bearer
token is fixed via ``--token`` instead of randomly generated, so the E2E
client can authenticate without scraping it out of a log line.
"""

from __future__ import annotations

import argparse
import os
import threading

from wifucked.api import create_app
from wifucked.config import load
from wifucked.daemon import Daemon
from wifucked.logging import get_logger

log = get_logger("e2e_daemon")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()

    if os.getenv("MOCK_HW") != "1":
        raise SystemExit("e2e_daemon.py requires MOCK_HW=1 (see module docstring)")

    config = load()
    daemon = Daemon(config, persist=False)
    daemon.start()

    loops = threading.Thread(target=daemon.run_forever, name="wifucked-loops", daemon=True)
    loops.start()

    app = create_app(daemon, api_token=args.token)
    log.info(
        "E2E dashboard listening",
        extra={
            "workflow": "api_start",
            "state": "completed",
            "intent": "serve the real dashboard for the AP E2E proof",
            "host": config.api_host,
            "port": config.api_port,
        },
    )
    app.run(host=config.api_host, port=config.api_port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
