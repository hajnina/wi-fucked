"""Scenario harness.

Drives the control loop through a scripted timeline against mock hardware, on a
virtual clock, so a 30-minute hysteresis window takes no real time at all.

Every scenario asserts the two invariants via :func:`assert_invariants`. They
are the product promises, and a change that breaks either is a release blocker
regardless of what else it improves. See docs/sop/SOP-003.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from wifucked.atomics.model import Atomic, Health, Kind, Mode
from wifucked.clock import VirtualClock
from wifucked.config import Config
from wifucked.daemon import Daemon
from wifucked.demand import StaticDemand
from wifucked.hal import build_hal
from wifucked.policy import DEFAULT_PROFILES
from wifucked.probe import Observation, ScriptedProber
from wifucked.telemetry import Telemetry


@dataclass
class Frame:
    """One observation of the world, captured after a tick."""

    at_s: float
    ap_running: bool
    ap_clients: int
    backup_bytes: int
    backup_state: str
    served: dict[str, int] = field(default_factory=dict)


class Harness:
    """A daemon plus the levers a scenario needs to move the world."""

    def __init__(self) -> None:
        self.clock = VirtualClock()
        self.hal = build_hal(force_mock=True)
        # Empty world by default: scenarios declare exactly what exists, so a
        # test never silently depends on the mock's default furniture.
        self.hal.usb.attached = []
        self.hal.wifi.networks = []
        self.hal.wifi.link = None

        self.prober = ScriptedProber()
        self.demand = StaticDemand(DEFAULT_PROFILES)

        self.daemon = Daemon(
            Config(),
            hal=self.hal,
            clock=self.clock,
            telemetry=Telemetry(self.clock, None),
            prober=self.prober,
            demand=self.demand,
            persist=False,
        )
        self.timeline: list[Frame] = []
        self._injected: dict[str, Atomic] = {}

        # The harness is the world. Scenarios declare what exists; the daemon's
        # own discovery would otherwise sweep the injected atomics away on the
        # next medium loop, since the mock HAL has been emptied above.
        self.daemon.discover_once = self._sweep  # type: ignore[method-assign]
        self.daemon.start()

    def _sweep(self) -> None:
        self.daemon.registry.observe(list(self._injected.values()))

    # -- world ----------------------------------------------------------------

    def add_atomic(
        self,
        atomic_id: str,
        *,
        kind: Kind = Kind.WIFI,
        label: str | None = None,
        mode: Mode = Mode.NORMAL,
        capacity_bps: int = 10_000_000,
        up_bps: int = 2_000_000,
        rtt_ms: float = 40.0,
        loss_pct: float = 0.0,
    ) -> str:
        """Inject an atomic directly, bypassing discovery.

        Scenarios are about control behaviour, not about whether nmcli parses.
        Discovery has its own tests.
        """
        atomic = Atomic(
            id=atomic_id,
            kind=kind,
            label=label or atomic_id,
            mode=mode,
            health=Health.GOOD,
            ifname=f"if-{atomic_id[:8]}",
            present=True,
        )
        self._injected[atomic_id] = atomic
        self._sweep()
        self.daemon.registry.set_mode(atomic_id, mode)
        self.set_capacity(atomic_id, capacity_bps, up_bps, rtt_ms, loss_pct)
        return atomic_id

    def remove_atomic(self, atomic_id: str) -> None:
        """The WAN vanishes — a normal state transition, not an error."""
        self._injected.pop(atomic_id, None)
        self._sweep()

    def set_capacity(
        self,
        atomic_id: str,
        down_bps: int,
        up_bps: int = 2_000_000,
        rtt_ms: float = 40.0,
        loss_pct: float = 0.0,
    ) -> None:
        self.prober.set(
            Observation(
                atomic_id=atomic_id,
                down_bps=down_bps,
                up_bps=up_bps,
                rtt_ms=rtt_ms,
                loss_pct=loss_pct,
                saturated=True,
            )
        )

    def set_demand(self, *, critical_bps: int = 0, besteffort_bps: int = 0) -> None:
        self.demand.set("Stable_critical", down_bps=critical_bps, up_bps=critical_bps // 4)
        self.demand.set("Stable_besteffort", down_bps=besteffort_bps, up_bps=besteffort_bps // 8)

    # -- running --------------------------------------------------------------

    def run_for(self, *, seconds: float = 0, minutes: float = 0) -> None:
        """Advance the virtual clock. Instant — never sleeps."""
        total = int(seconds + minutes * 60)
        for _ in range(total):
            self.daemon.tick()
            self.clock.advance(1)
            self._capture()

    def _capture(self) -> None:
        ap = self.hal.ap.status()
        allocation = self.daemon.allocation
        served = {s.profile_name: s.ceiling_bps for s in (allocation.shares if allocation else ())}
        self.timeline.append(
            Frame(
                at_s=self.clock.now(),
                ap_running=ap.running,
                ap_clients=ap.associated_clients,
                backup_bytes=self._backup_bytes(),
                backup_state=str(self.daemon.allocator.backup_state),
                served=served,
            )
        )

    def _backup_bytes(self) -> int:
        """Traffic the allocator permitted onto a BACKUP atomic.

        Liveness budget is excluded deliberately — it is accounted separately
        and is not "carrying traffic" (ADR-006).
        """
        allocation = self.daemon.allocation
        if allocation is None or not allocation.backup_active:
            return 0
        backup_ids = {a.id for a in self.daemon.registry.backups()}
        return sum(
            share.ceiling_bps for share in allocation.shares if share.atomic_id in backup_ids
        )

    # -- assertions -----------------------------------------------------------

    def bytes_on(self, atomic_id: str) -> int:
        allocation = self.daemon.allocation
        if allocation is None:
            return 0
        return sum(s.ceiling_bps for s in allocation.shares if s.atomic_id == atomic_id)

    def served_bps(self, profile_key: str) -> int:
        allocation = self.daemon.allocation
        if allocation is None:
            return 0
        name = "Stable_critical" if profile_key == "critical" else "Stable_besteffort"
        return sum(s.ceiling_bps for s in allocation.shares if s.profile_name == name)

    def count_transitions(self) -> int:
        return self.daemon.allocator.transitions


def assert_invariants(timeline: list[Frame]) -> None:
    """The two product promises. Every scenario asserts these.

    1. The AP never drops — not across WAN churn, profile switches, or daemon
       restarts. Clients that were associated stay associated.
    2. BACKUP carries zero bytes unless the allocator declared it active, which
       it may only do when critical demand cannot otherwise be met.
    """
    assert timeline, "scenario produced no frames — did it run?"

    dropped = [f for f in timeline if not f.ap_running]
    assert not dropped, (
        f"AP dropped at t={dropped[0].at_s}s. The Stable SSIDs must never go away — see ADR-011."
    )

    peak = max(f.ap_clients for f in timeline)
    lost = [f for f in timeline if f.ap_clients < peak]
    assert not lost, (
        f"AP lost clients at t={lost[0].at_s}s ({lost[0].ap_clients} of {peak}). "
        f"Clients must survive channel moves via CSA — see ADR-013."
    )

    leaked = [f for f in timeline if f.backup_bytes > 0 and f.backup_state != "active"]
    assert not leaked, (
        f"BACKUP carried {leaked[0].backup_bytes} bps at t={leaked[0].at_s}s while "
        f"state was {leaked[0].backup_state!r}. BACKUP is paid insurance — "
        f"see ADR-006."
    )


@pytest.fixture
def harness() -> Harness:
    return Harness()
