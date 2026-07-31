"""Demand — what the users are actually trying to do.

Demand is not the same as available bandwidth. A 100 Mbps link with 3 Mbps of
demand needs no management at all; a 20 Mbps link with 80 Mbps of demand is
capacity-constrained and needs all of it. The allocator cannot tell those apart
without this.

Measured per service class and separately per direction, because upload
saturation is what destroys download performance and latency — the failure this
product exists to manage.

WS-B owns this module. Phase 0 ships the interface and a fake that scenario
tests drive directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from wifucked.clock import Clock
from wifucked.hal.base import Hal
from wifucked.lan import lan_ifname_for_profile
from wifucked.logging import get_logger
from wifucked.policy import ServiceProfile

log = get_logger("demand")


@dataclass(frozen=True, slots=True)
class ClassDemand:
    profile_name: str
    down_bps: int
    up_bps: int
    #: True when the class is being held back — offered load exceeded what it
    #: was allowed. Distinguishes "wants little" from "wants more but is capped".
    constrained: bool = False

    @property
    def total_bps(self) -> int:
        return self.down_bps + self.up_bps


class DemandEstimator(Protocol):
    def sample(self) -> dict[str, ClassDemand]:
        """Current demand keyed by service profile name."""


class StaticDemand(DemandEstimator):
    """A demand estimator the tests set by hand."""

    def __init__(self, profiles: tuple[ServiceProfile, ...]):
        self._demand = {p.name: ClassDemand(p.name, down_bps=0, up_bps=0) for p in profiles}

    def set(
        self,
        profile_name: str,
        down_bps: int,
        up_bps: int = 0,
        constrained: bool = False,
    ) -> None:
        self._demand[profile_name] = ClassDemand(profile_name, down_bps, up_bps, constrained)

    def sample(self) -> dict[str, ClassDemand]:
        return dict(self._demand)


class CounterDemand(DemandEstimator):
    """Derives demand from each service class's per-VLAN byte counters.

    Each profile's LAN traffic arrives on a VLAN subinterface whose name is
    resolved by :func:`wifucked.lan.lan_ifname_for_profile`, so this reads the same
    interface hostapd hands the class and never reconstructs the mapping itself.
    Demand is the rate of change of those counters between samples — the same
    delta-over-elapsed arithmetic ``PassiveProber`` uses on the WAN side.

    Directions are mirrored relative to the WAN prober: on a LAN interface,
    bytes the interface *transmits* go out to clients (their download) and bytes
    it *receives* come in from clients (their upload). So ``down_bps`` is the tx
    delta and ``up_bps`` is the rx delta.

    Served throughput is only a lower bound on true demand — a class held at its
    ceiling wanted more than it got — and separating "wants little" from "wants
    more but is capped" needs queue-depth inference the original docstring called
    out as deliberately hard. That inference is not attempted, so ``constrained``
    is always False here; the allocator treats served rate as observed demand,
    which is safe (it never *over*-states what a class is asking for).
    """

    def __init__(
        self,
        profiles: tuple[ServiceProfile, ...],
        hal: Hal,
        clock: Clock,
        lan_mode: str = "two_bss",
        base_interface: str = "wlan0",
    ):
        self._profiles = profiles
        self._hal = hal
        self._clock = clock
        self._lan_mode = lan_mode
        self._base_interface = base_interface
        #: profile name -> (monotonic_s, rx_bytes, tx_bytes)
        self._last: dict[str, tuple[float, int, int]] = {}

    def sample(self) -> dict[str, ClassDemand]:
        now = self._clock.now()
        result: dict[str, ClassDemand] = {}
        for profile in self._profiles:
            ifname = lan_ifname_for_profile(profile, self._lan_mode, self._base_interface)
            rx, tx = self._hal.net.counters(ifname)
            previous = self._last.get(profile.name)
            self._last[profile.name] = (now, rx, tx)
            if previous is None:
                result[profile.name] = ClassDemand(profile.name, down_bps=0, up_bps=0)
                continue
            then, prev_rx, prev_tx = previous
            elapsed = now - then
            if elapsed <= 0:
                result[profile.name] = ClassDemand(profile.name, down_bps=0, up_bps=0)
                continue
            result[profile.name] = ClassDemand(
                profile_name=profile.name,
                down_bps=int(max(0, tx - prev_tx) * 8 / elapsed),
                up_bps=int(max(0, rx - prev_rx) * 8 / elapsed),
            )
        log.debug(
            "Sampled per-class demand from LAN counters",
            extra={
                "workflow": "demand_sample",
                "state": "completed",
                "intent": "give the allocator observed per-class load",
                "lan_mode": self._lan_mode,
                "demand_bps": {name: d.total_bps for name, d in result.items()},
            },
        )
        return result
