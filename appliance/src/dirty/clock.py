"""Injectable clock.

Scenario tests drive hysteresis windows measured in minutes. They must not take
minutes to run, so nothing in the daemon calls ``time.sleep`` or ``time.time``
directly — everything takes a :class:`Clock`. See docs/sop/SOP-003.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Monotonic seconds. Only differences are meaningful."""

    def wall(self) -> float:
        """Unix timestamp, for anything a human will read."""

    def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class VirtualClock:
    """A clock the tests move by hand. ``sleep`` advances instead of blocking."""

    def __init__(self, start: float = 0.0, wall_start: float = 1_780_000_000.0):
        self._now = start
        self._wall_start = wall_start

    def now(self) -> float:
        return self._now

    def wall(self) -> float:
        return self._wall_start + self._now

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("time does not go backwards")
        self._now += seconds
