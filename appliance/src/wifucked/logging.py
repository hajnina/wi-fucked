"""Resilient structured logging for the wifucked daemon.

Every logger in this application is rooted at ``wifucked``. Use :func:`get_logger`,
never ``logging.getLogger`` directly — see docs/sop/SOP-004.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

ROOT = "wifucked"

# /tmp is the deliberate last resort: on an unprovisioned or read-only root
# it is the only writable path, and losing logs entirely is worse.
_LOG_FILE_CANDIDATES = ("/var/log/wifucked.log", "/tmp/wifucked.log")  # noqa: S108

#: Keys that ``logging`` reserves on LogRecord. Passing any of these in ``extra``
#: raises KeyError, which ResilientLogger absorbs rather than crashing the daemon.
_RESERVED = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }
)  # fmt: skip


def _log_file_path() -> str:
    for candidate in _LOG_FILE_CANDIDATES:
        parent = os.path.dirname(candidate)
        if os.access(parent, os.W_OK):
            return candidate
    return _LOG_FILE_CANDIDATES[-1]


class ResilientLogger(logging.Logger):
    """A logger that will not take the network down over a bad log call.

    A malformed ``extra`` payload — most often a key that collides with a
    reserved LogRecord attribute — raises KeyError inside ``makeRecord``. On an
    appliance that is the difference between a noisy log line and an outage, so
    we degrade to a stringified payload instead of propagating.
    """

    def makeRecord(
        self,
        name,
        level,
        fn,
        lno,
        msg,
        args,
        exc_info,
        func=None,
        extra=None,
        sinfo=None,
    ):
        try:
            return super().makeRecord(name, level, fn, lno, msg, args, exc_info, func, extra, sinfo)
        except Exception as exc:
            fallback = f"{msg} | [resilient-logger: unusable extra {extra!r} ({exc})]"
            return super().makeRecord(
                name, level, fn, lno, fallback, args, exc_info, func, None, sinfo
            )


logging.setLoggerClass(ResilientLogger)


class _ExtraFormatter(logging.Formatter):
    """Renders the structured ``extra`` payload after the message.

    Keeps human-readable console output while preserving the fields that make a
    log line diagnosable in the field.
    """

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        }
        if not extras:
            return base
        rendered = " ".join(f"{k}={v!r}" for k, v in sorted(extras.items()))
        return f"{base} | {rendered}"


def _configure_root() -> logging.Logger:
    root = logging.getLogger(ROOT)
    if root.handlers:
        return root

    root.setLevel(logging.DEBUG if os.getenv("WIFUCKED_DEBUG") else logging.INFO)
    root.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_ExtraFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(console)

    # File handler is best-effort: a read-only or full filesystem must not stop
    # the daemon from starting.
    try:
        handler = logging.handlers.RotatingFileHandler(
            _log_file_path(), maxBytes=4 * 1024 * 1024, backupCount=3
        )
        handler.setFormatter(_ExtraFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(handler)
    except OSError as exc:
        root.warning(
            "File logging unavailable; continuing with console only",
            extra={
                "workflow": "logging_init",
                "state": "failed",
                "intent": "persist logs across reboot for field diagnosis",
                "reason": "could not open rotating log file",
                "error": str(exc),
            },
        )

    return root


def get_logger(name: str) -> logging.Logger:
    """Return a resilient logger rooted at ``wifucked``.

    ``get_logger("allocator")`` yields ``wifucked.allocator``. Passing an
    already-rooted name is idempotent.
    """
    _configure_root()
    if name == ROOT or name.startswith(f"{ROOT}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT}.{name}")
