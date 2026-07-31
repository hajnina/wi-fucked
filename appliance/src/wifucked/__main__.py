"""Entry point: ``python3 -m wifucked``.

The control loops run in a background thread and the API serves on the main
thread. Both are deliberately independent of hostapd and dnsmasq, which are
separate systemd units — if this process dies, the user keeps their network
(ADR-011, ADR-008).
"""

from __future__ import annotations

import os
import signal
import sys
import threading

from wifucked import __version__
from wifucked.api import create_app
from wifucked.config import load
from wifucked.daemon import Daemon
from wifucked.logging import get_logger

log = get_logger("main")


def _install_scenario(daemon: Daemon, name: str) -> None:
    """Drive the mock world through a scripted timeline.

    ``WIFUCKED_SCENARIO=moving_van`` reproduces the field conditions the product
    exists to survive, so the dashboard can be looked at without a van.
    """
    try:
        from wifucked.scenarios import install

        install(daemon, name)
    except (ImportError, KeyError) as exc:
        log.warning(
            "Scenario unavailable; running with static mock hardware",
            extra={
                "workflow": "scenario_init",
                "state": "skipped",
                "intent": "exercise the control loop against realistic conditions",
                "scenario": name,
                "reason": "scenario could not be loaded",
                "error": str(exc),
            },
        )


def main() -> int:
    config = load()
    persist = os.getenv("MOCK_HW") != "1"
    daemon = Daemon(config, persist=persist)

    scenario = os.getenv("WIFUCKED_SCENARIO")
    if scenario:
        _install_scenario(daemon, scenario)

    daemon.start()

    loops = threading.Thread(target=daemon.run_forever, name="wifucked-loops", daemon=True)
    loops.start()

    def shutdown(signum, _frame):
        log.info(
            "Signal received; stopping loops",
            extra={
                "workflow": "daemon_stop",
                "state": "started",
                "intent": "stop cleanly without touching the data plane",
                "signal": signum,
            },
        )
        daemon.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    app = create_app(daemon)
    log.info(
        "Dashboard listening",
        extra={
            "workflow": "api_start",
            "state": "completed",
            "intent": "let the user see what the appliance believes",
            "host": config.api_host,
            "port": config.api_port,
            "version": __version__,
        },
    )
    app.run(host=config.api_host, port=config.api_port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
