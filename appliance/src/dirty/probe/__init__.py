"""Probing and capacity estimation.

Capacity is observed, not configured (ADR-003). The default path is passive:
watch throughput during natural saturation, and watch latency rise under load to
find the bufferbloat knee. Active probing is opt-in, NORMAL-only, and **never**
runs on a BACKUP atomic — a product whose promise is "we won't spend your backup
data" cannot spend backup data measuring itself.

WS-B owns this module. Phase 0 ships the interface, the confidence-decay model
(which the allocator depends on being honest), and a fake for scenario tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from dirty.atomics.model import Atomic, Capacity, Mode, Quality
from dirty.clock import Clock
from dirty.logging import get_logger

log = get_logger("probe")

#: An estimate loses all confidence after this long without corroboration. A
#: stale number reported as current is worse than admitting we don't know.
CONFIDENCE_HALF_LIFE_S = 1800.0


@dataclass(frozen=True, slots=True)
class Observation:
    atomic_id: str
    down_bps: int
    up_bps: int
    rtt_ms: float | None = None
    jitter_ms: float | None = None
    loss_pct: float | None = None
    bloat_ms: float | None = None
    #: True when the link was actually busy — an idle link tells us nothing
    #: about its capacity, only that it wasn't being used.
    saturated: bool = False


class Prober(Protocol):
    def observe(self, atomic: Atomic) -> Observation | None:
        """One observation, or None if nothing could be measured."""


def decay_confidence(capacity: Capacity, now: float) -> float:
    """Exponential decay of an estimate's confidence with age."""
    if capacity.measured_at is None or not capacity.known:
        return 0.0
    age = max(0.0, now - capacity.measured_at)
    return capacity.confidence * (0.5 ** (age / CONFIDENCE_HALF_LIFE_S))


def fold(capacity: Capacity, observation: Observation, now: float) -> Capacity:
    """Fold an observation into a capacity estimate.

    Only a *saturated* observation raises the estimate — an idle link's low
    throughput is not evidence of low capacity, and treating it as such would
    make a good connection look bad the moment nobody used it.
    """
    decayed = decay_confidence(capacity, now)

    if not observation.saturated:
        return Capacity(
            down_bps=capacity.down_bps,
            up_bps=capacity.up_bps,
            confidence=decayed,
            measured_at=capacity.measured_at,
        )

    # EWMA, weighted towards the new sample when we had little confidence before.
    weight = 0.4 if decayed > 0.5 else 0.7
    down = int(capacity.down_bps * (1 - weight) + observation.down_bps * weight)
    up = int(capacity.up_bps * (1 - weight) + observation.up_bps * weight)
    return Capacity(
        down_bps=down,
        up_bps=up,
        confidence=min(1.0, decayed + 0.3),
        measured_at=now,
    )


def quality_of(observation: Observation) -> Quality:
    return Quality(
        rtt_ms=observation.rtt_ms,
        jitter_ms=observation.jitter_ms,
        loss_pct=observation.loss_pct,
        bloat_ms=observation.bloat_ms,
    )


class PassiveProber(Prober):
    """Passive estimation from interface counters.

    Placeholder for WS-B. The real implementation reads byte counters between
    ticks to get achieved throughput, correlates with queue backlog from CAKE to
    decide whether the link was saturated, and tracks latency-under-load to find
    the knee. Only the counter arithmetic is here; saturation detection is the
    interesting part and is left to WS-B.
    """

    def __init__(self, hal, clock: Clock):
        self._hal = hal
        self._clock = clock
        self._last: dict[str, tuple[float, int, int]] = {}

    def observe(self, atomic: Atomic) -> Observation | None:
        if not atomic.ifname:
            return None
        now = self._clock.now()
        rx, tx = self._hal.net.counters(atomic.ifname)
        previous = self._last.get(atomic.id)
        self._last[atomic.id] = (now, rx, tx)
        if previous is None:
            return None

        then, prev_rx, prev_tx = previous
        elapsed = now - then
        if elapsed <= 0:
            return None

        return Observation(
            atomic_id=atomic.id,
            down_bps=int(max(0, rx - prev_rx) * 8 / elapsed),
            up_bps=int(max(0, tx - prev_tx) * 8 / elapsed),
            saturated=False,  # WS-B: derive from queue backlog
        )


class ScriptedProber(Prober):
    """A prober the scenario harness drives directly."""

    def __init__(self) -> None:
        self._observations: dict[str, Observation] = {}

    def set(self, observation: Observation) -> None:
        self._observations[observation.atomic_id] = observation

    def observe(self, atomic: Atomic) -> Observation | None:
        return self._observations.get(atomic.id)


def may_probe_actively(atomic: Atomic) -> bool:
    """Active probing is NORMAL-only. This is a hard rule, not a preference."""
    if atomic.mode is not Mode.NORMAL:
        log.debug(
            "Refusing active probe on a non-NORMAL connection",
            extra={
                "workflow": "active_probe",
                "state": "skipped",
                "intent": "avoid spending metered or unpermitted bandwidth",
                "atomic_id": atomic.id,
                "mode": str(atomic.mode),
                "reason": "active probing is permitted on NORMAL atomics only",
            },
        )
        return False
    return True
