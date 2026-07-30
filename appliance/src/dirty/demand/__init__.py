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

from dirty.policy import ServiceProfile


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
    """Derives demand from per-VLAN byte counters.

    Placeholder for WS-B. Reading counters gives *served* throughput, which is a
    lower bound on demand — a saturated class wants more than it received, and
    the estimator has to infer how much from queue backlog. Getting that
    inference right is the interesting part of the work, and is not attempted
    here.
    """

    def __init__(self, profiles: tuple[ServiceProfile, ...]):
        self._profiles = profiles

    def sample(self) -> dict[str, ClassDemand]:
        return {p.name: ClassDemand(p.name, down_bps=0, up_bps=0) for p in self._profiles}
