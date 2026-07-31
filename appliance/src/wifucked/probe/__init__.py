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

import re
import subprocess
from dataclasses import dataclass, replace
from typing import Protocol

from wifucked.atomics.model import Atomic, Capacity, Mode, Quality
from wifucked.clock import Clock
from wifucked.hal.base import Hal
from wifucked.logging import get_logger

log = get_logger("probe")

#: An estimate loses all confidence after this long without corroboration. A
#: stale number reported as current is worse than admitting we don't know.
CONFIDENCE_HALF_LIFE_S = 1800.0

#: Where active RTT probes are sent. The same anycast/public resolvers this repo
#: already trusts as DNS upstreams (see wifucked.lan.dnsmasq_config), so this adds
#: no new external dependency. Tried in order; the first that answers wins.
PROBE_TARGETS: tuple[str, ...] = ("1.1.1.1", "8.8.8.8")

#: How many echo requests one active probe sends. Three is enough for ``ping``
#: to report a meaningful ``mdev`` (our jitter figure) without being chatty.
PROBE_COUNT = 3


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

    Reads byte counters between ticks to get achieved throughput, and reads the
    interface's root-qdisc backlog and drop count to decide whether the link was
    actually saturated during the window. Saturation is the load-bearing signal:
    ``fold()`` raises a capacity estimate *only* on a saturated observation
    (ADR-003), so a link that is never reported saturated can never teach the
    allocator anything — which is exactly the bug this replaces.

    A window is saturated if the queue held bytes at read time, or if the drop
    counter advanced since the previous read. Either means the kernel had more
    to send than the link could carry, so the throughput seen is a real floor on
    capacity rather than an artefact of light use.
    """

    def __init__(self, hal: Hal, clock: Clock):
        self._hal = hal
        self._clock = clock
        #: atomic_id -> (monotonic_s, rx_bytes, tx_bytes, dropped_packets)
        self._last: dict[str, tuple[float, int, int, int]] = {}

    def observe(self, atomic: Atomic) -> Observation | None:
        # ifname is a volatile fact read at the point of use, never persisted as
        # identity (ADR-002); state is keyed on atomic.id throughout.
        ifname = atomic.ifname
        if not ifname:
            return None
        now = self._clock.now()
        rx, tx = self._hal.net.counters(ifname)
        backlog_bytes, drops = self._hal.net.qdisc_stats(ifname)
        previous = self._last.get(atomic.id)
        self._last[atomic.id] = (now, rx, tx, drops)
        if previous is None:
            return None

        then, prev_rx, prev_tx, prev_drops = previous
        elapsed = now - then
        if elapsed <= 0:
            return None

        saturated = backlog_bytes > 0 or drops > prev_drops
        observation = Observation(
            atomic_id=atomic.id,
            down_bps=int(max(0, rx - prev_rx) * 8 / elapsed),
            up_bps=int(max(0, tx - prev_tx) * 8 / elapsed),
            saturated=saturated,
        )
        if saturated:
            log.debug(
                "Observed a saturated window",
                extra={
                    "workflow": "passive_probe",
                    "state": "completed",
                    "intent": "raise the capacity estimate from throughput under load",
                    "atomic_id": atomic.id,
                    "down_bps": observation.down_bps,
                    "up_bps": observation.up_bps,
                    "backlog_bytes": backlog_bytes,
                    "drops_delta": drops - prev_drops,
                    "duration_ms": int(elapsed * 1000),
                },
            )
        return observation


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


@dataclass(frozen=True, slots=True)
class PingResult:
    loss_pct: float
    #: None when every request was lost — there is no RTT to report.
    rtt_ms: float | None = None
    #: ``ping``'s own ``mdev``: the standard deviation of the round-trip times.
    jitter_ms: float | None = None


#: iputils ``ping`` summary lines we read: the loss percentage, and the
#: ``min/avg/max/mdev`` block. Verified against iputils output, which prints
#: e.g. ``0% packet loss`` and ``rtt min/avg/max/mdev = 1.2/1.3/1.4/0.1 ms``.
_LOSS_RE = re.compile(r"([\d.]+)% packet loss")
_RTT_RE = re.compile(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms")


def _parse_ping(out: str) -> PingResult | None:
    """Pull loss, mean RTT, and jitter out of a ``ping`` run's summary.

    Returns None only when the output has no loss line at all — i.e. it is not
    recognisable ``ping`` output. A 100%-loss run is a valid result (the link is
    down), not a parse failure.
    """
    loss_m = _LOSS_RE.search(out)
    if loss_m is None:
        return None
    loss_pct = float(loss_m.group(1))
    rtt_m = _RTT_RE.search(out)
    if rtt_m is None:
        return PingResult(loss_pct=loss_pct)
    _min, avg, _max, mdev = (float(group) for group in rtt_m.groups())
    return PingResult(loss_pct=loss_pct, rtt_ms=avg, jitter_ms=mdev)


def _run(argv: list[str], timeout: float = 10.0) -> str | None:
    """Run a command, returning stdout or None. Never raises.

    Mirrors ``wifucked.hal.linux._run`` but tolerates ``ping``'s exit code 1:
    ``ping`` returns 1 when some or all replies were lost yet still prints the
    statistics block we parse, so a lossy result must not be discarded as an
    error. Only a spawn failure, a timeout, or exit code >= 2 yields None.
    """
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning(
            "Active probe command failed to execute",
            extra={
                "workflow": "active_probe",
                "state": "failed",
                "intent": "measure RTT, jitter and loss on a NORMAL link",
                "argv": argv,
                "reason": "could not spawn or complete the process",
                "error": str(exc),
            },
        )
        return None
    if done.returncode >= 2:
        log.warning(
            "Active probe command returned an error",
            extra={
                "workflow": "active_probe",
                "state": "failed",
                "intent": "measure RTT, jitter and loss on a NORMAL link",
                "argv": argv,
                "returncode": done.returncode,
                "reason": (done.stderr or "").strip()[:200],
            },
        )
        return None
    return done.stdout


class LinuxProber(PassiveProber):
    """Real measurement on hardware.

    Passive throughput and saturation from the base class, plus active
    RTT/jitter/loss/bufferbloat on NORMAL links only. Active probing is gated by
    :func:`may_probe_actively` — a BACKUP link is never pinged, because a product
    that promises not to spend the user's backup data cannot spend it measuring
    itself (ADR-003).

    The bufferbloat figure is latency-under-load: current mean RTT minus the
    best RTT ever seen on this atomic. The baseline is tracked per atomic.id and
    only ratchets downward, so a genuinely faster sample tightens it but a slow
    one under load does not inflate it.
    """

    def __init__(self, hal: Hal, clock: Clock):
        super().__init__(hal, clock)
        #: atomic_id -> lowest mean RTT ever observed, the bufferbloat baseline.
        self._rtt_floor: dict[str, float] = {}

    def observe(self, atomic: Atomic) -> Observation | None:
        passive = super().observe(atomic)
        if passive is None:
            return None
        if not may_probe_actively(atomic):
            return passive
        ifname = atomic.ifname
        if not ifname:
            return passive

        result = self._active_probe(atomic.id, ifname)
        if result is None:
            return passive

        bloat_ms = self._fold_rtt_floor(atomic.id, result.rtt_ms)
        return replace(
            passive,
            rtt_ms=result.rtt_ms,
            jitter_ms=result.jitter_ms,
            loss_pct=result.loss_pct,
            bloat_ms=bloat_ms,
        )

    def _active_probe(self, atomic_id: str, ifname: str) -> PingResult | None:
        """Ping public targets over one interface until one gives an RTT.

        Binds to the interface directly (``ping -I <ifname>``) rather than the
        routing table, so this needs no coordination with policy routing. Falls
        through the targets so one dead resolver does not mark a live link down;
        keeps a loss-only result as the fallback if none answer.
        """
        fallback: PingResult | None = None
        for target in PROBE_TARGETS:
            argv = ["ping", "-c", str(PROBE_COUNT), "-W", "1", "-I", ifname, target]
            out = _run(argv)
            if out is None:
                continue
            parsed = _parse_ping(out)
            if parsed is None:
                continue
            if parsed.rtt_ms is not None:
                log.debug(
                    "Active probe completed",
                    extra={
                        "workflow": "active_probe",
                        "state": "completed",
                        "intent": "measure RTT, jitter and loss on a NORMAL link",
                        "atomic_id": atomic_id,
                        "target": target,
                        "rtt_ms": parsed.rtt_ms,
                        "jitter_ms": parsed.jitter_ms,
                        "loss_pct": parsed.loss_pct,
                    },
                )
                return parsed
            fallback = parsed
        if fallback is not None:
            log.warning(
                "Active probe saw total loss on every target",
                extra={
                    "workflow": "active_probe",
                    "state": "failed",
                    "intent": "measure RTT, jitter and loss on a NORMAL link",
                    "atomic_id": atomic_id,
                    "targets": list(PROBE_TARGETS),
                    "loss_pct": fallback.loss_pct,
                    "reason": "no target answered — link present but not passing probes",
                },
            )
        return fallback

    def _fold_rtt_floor(self, atomic_id: str, rtt_ms: float | None) -> float | None:
        """Update the per-atomic RTT floor and return latency added under load."""
        if rtt_ms is None:
            return None
        floor = min(self._rtt_floor.get(atomic_id, rtt_ms), rtt_ms)
        self._rtt_floor[atomic_id] = floor
        return max(0.0, rtt_ms - floor)
