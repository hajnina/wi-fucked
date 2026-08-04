"""Minimal systemd notify-protocol client.

`Type=notify` services are expected to send `READY=1` once they're up and,
if `WatchdogSec=` is set, `WATCHDOG=1` on some cadence shorter than that
timeout or systemd kills and restarts the unit.

We don't pull in the `sdnotify` PyPI package for this — the protocol is a
single datagram write to a Unix socket named by `$NOTIFY_SOCKET`. No socket
means no systemd (a laptop under `MOCK_HW=1`, a test, a plain `python3 -m
wifucked` run) and every call here is a silent no-op: never raise, never
block, never require systemd to exist.
"""

from __future__ import annotations

import os
import socket

from wifucked.logging import get_logger

log = get_logger("watchdog")


def sd_notify(message: str) -> None:
    """Send a systemd notify-protocol datagram, or do nothing.

    `message` is the raw payload, e.g. ``"READY=1"`` or ``"WATCHDOG=1"``.
    No-ops cleanly (no exception, no blocking) whenever `$NOTIFY_SOCKET`
    isn't set — that's the normal case everywhere except under a real
    systemd `Type=notify` unit.
    """
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return

    # systemd's convention: a leading "@" means an abstract-namespace socket,
    # represented on the wire as a leading NUL byte.
    if address.startswith("@"):
        address = "\0" + address[1:]

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.settimeout(1.0)
        sock.sendto(message.encode("utf-8"), address)
    except OSError as exc:
        # A wedged/missing notify socket must never take the daemon down —
        # the watchdog just goes unfed and systemd restarts us, which is the
        # documented fallback behaviour (ADR-008: fail to last-known-good).
        log.warning(
            "sd_notify failed",
            extra={
                "workflow": "watchdog_notify",
                "state": "failed",
                "intent": "keep systemd informed of liveness",
                "message": message,
                "reason": str(exc),
            },
        )
    finally:
        sock.close()
