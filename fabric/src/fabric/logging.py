"""Structured logging for the fabric server.

Every logger in this application is rooted at ``fabric``. Use :func:`get_logger`,
never ``logging.getLogger`` directly — mirrors the appliance's convention in
``appliance/src/wifucked/logging.py`` (see docs/sop/SOP-004), sized down for a
stateless container: fabric runs under gunicorn with no persistent disk worth
writing to, so stdout captured by the container runtime is the only sink that
matters. No file handler, no ``ResilientLogger`` — just a root handler that is
guaranteed to exist before gunicorn starts serving requests.
"""

from __future__ import annotations

import logging
import os
import sys

ROOT = "fabric"


def _configure_root() -> logging.Logger:
    root = logging.getLogger(ROOT)
    if root.handlers:
        return root

    root.setLevel(logging.DEBUG if os.getenv("FABRIC_DEBUG") else logging.INFO)
    root.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(console)

    return root


def get_logger(name: str) -> logging.Logger:
    """Return a logger rooted at ``fabric``.

    ``get_logger("peers")`` yields ``fabric.peers``. Passing an already-rooted
    name is idempotent.
    """
    _configure_root()
    if name == ROOT or name.startswith(f"{ROOT}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT}.{name}")
