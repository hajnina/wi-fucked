"""Entry point: ``python3 -m wifucked``.

The control loops run in a background thread and the API serves on the main
thread. Both are deliberately independent of hostapd and dnsmasq, which are
separate systemd units — if this process dies, the user keeps their network
(ADR-011, ADR-008).
"""

from __future__ import annotations

import errno
import os
import signal
import sys
import threading
import time

from wifucked import __version__
from wifucked.api import create_app
from wifucked.config import load, load_or_create_api_token
from wifucked.daemon import Daemon
from wifucked.logging import get_logger

log = get_logger("main")

#: How long to keep retrying a bind failure before giving up and letting
#: systemd's own Restart=always take over (wifucked.service, RestartSec=5).
#: Bounded rather than infinite so a permanently-unbindable address (wrong
#: config.api_host, not a transient race) still surfaces as a restart loop
#: instead of hanging silently forever.
_BIND_RETRY_TIMEOUT_S = 120
_BIND_RETRY_INTERVAL_S = 2


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


def _serve_with_retry(app, host: str, port: int, *, sleep=time.sleep, deadline=None) -> None:
    """Bind and serve, retrying a not-yet-available address instead of crashing.

    ``config.api_host`` is hardcoded to the LAN gateway address (e.g.
    ``10.44.0.1``, never ``0.0.0.0`` — see ``config.py``), which is assigned to
    the AP interface by ``systemd-networkd`` from a generated ``.network`` unit
    (ADR-011: independent of this daemon, no ordering guarantee against it).
    ``wifucked.service`` starting before that address lands raises
    ``OSError: [Errno 99] Cannot assign requested address`` from Flask's
    ``werkzeug`` bind — previously unhandled, which killed this process
    (including the already-running control-loop thread, `daemon=True`) and,
    under ``Restart=always``/``RestartSec=5``, produced a tight crash loop that
    never got far enough to log anything about the AP itself. Confirmed as this
    exact failure in a real CI run of ``appliance/tests/e2e`` before this fix
    existed (see that test's own sequencing comment).

    Retrying here — rather than only fixing systemd unit ordering — matches
    ADR-007 (reconciliation, not a one-shot command) and ADR-008 (fail to
    last-known-good): a slow interface bring-up should cost adaptivity while it
    resolves, not repeatedly discard the whole process's state.
    """
    deadline = deadline if deadline is not None else time.monotonic() + _BIND_RETRY_TIMEOUT_S
    attempt = 0
    while True:
        attempt += 1
        try:
            app.run(host=host, port=port, threaded=True)
            return
        except OSError as exc:
            if exc.errno != errno.EADDRNOTAVAIL or time.monotonic() >= deadline:
                log.error(
                    "Could not bind dashboard; giving up",
                    extra={
                        "workflow": "api_start",
                        "state": "failed",
                        "intent": "let the user see what the appliance believes",
                        "host": host,
                        "port": port,
                        "attempt": attempt,
                        "reason": "address permanently unbindable"
                        if exc.errno != errno.EADDRNOTAVAIL
                        else "gave up after retry timeout",
                        "error": str(exc),
                    },
                    exc_info=True,
                )
                raise
            log.warning(
                "Dashboard bind address not yet available; retrying",
                extra={
                    "workflow": "api_start",
                    "state": "processing",
                    "intent": "wait for systemd-networkd to land the gateway address first",
                    "host": host,
                    "port": port,
                    "attempt": attempt,
                    "reason": "address not yet assigned to any local interface",
                    "error": str(exc),
                },
            )
            sleep(_BIND_RETRY_INTERVAL_S)


def main() -> int:
    config = load()
    persist = os.getenv("MOCK_HW") != "1"
    daemon = Daemon(config, persist=persist)

    scenario = os.getenv("WIFUCKED_SCENARIO")
    if scenario:
        _install_scenario(daemon, scenario)

    # daemon.run_forever() (below) calls start() itself as its first action;
    # an explicit call here was a redundant duplicate (visible in the field as
    # two "Daemon starting" log lines per process lifetime, and a second,
    # wasted discover_once()/fabric-attach attempt).
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

    api_token = load_or_create_api_token(config, persist=persist)
    app = create_app(daemon, api_token=api_token)
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
    _serve_with_retry(app, config.api_host, config.api_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
