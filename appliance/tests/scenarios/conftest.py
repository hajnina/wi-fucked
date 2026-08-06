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
from wifucked.config import Config, LanConfig
from wifucked.daemon import Daemon
from wifucked.demand import StaticDemand
from wifucked.enforce import DesiredState
from wifucked.hal import build_hal
from wifucked.hal.base import ApStatus
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

    def __init__(self, *, lan_mode: str = "two_bss") -> None:
        self.clock = VirtualClock()
        self.hal = build_hal(force_mock=True)
        # Empty world by default: scenarios declare exactly what exists, so a
        # test never silently depends on the mock's default furniture.
        self.hal.usb.attached = []
        self.hal.wifi.networks = []
        self.hal.wifi.link = None

        self.prober = ScriptedProber()
        self.demand = StaticDemand(DEFAULT_PROFILES)

        # Most scenarios exercise the two-class allocator/enforcement logic
        # (ADR-006, ADR-009) — that logic is unaffected by which LAN broadcast
        # mode ships by default (ADR-020: `Config()` alone now defaults to
        # "single", one undifferentiated class), so the default fixture asks
        # for "two_bss" explicitly, giving `daemon.profiles == DEFAULT_PROFILES`
        # as these scenarios were written to assert. `test_single_hotspot.py`
        # passes `lan_mode="single"` to exercise the interim default itself.
        self.daemon = Daemon(
            Config(lan=LanConfig(lan_mode=lan_mode)),
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

    def drop_ap(self, *, running: bool | None = None, clients: int | None = None) -> None:
        """Perturb the mock AP directly, bypassing every code path that would
        normally move it.

        `MockAp` is otherwise only ever read via `.status()` — nothing in a
        scenario can make the AP fail. This is the lever for it: pass
        `running=False` to simulate the AP process dying, or `clients=<n>` to
        simulate associated clients dropping (e.g. a botched channel move),
        or both. Whatever isn't passed is left as it currently is.

        This exists so the "AP never drops" invariant (`assert_invariants`) is
        actually falsifiable — see the harness self-check in
        `test_harness_self_check.py`.
        """
        state = self.hal.ap.state
        self.hal.ap.state = ApStatus(
            running=state.running if running is None else running,
            channel=state.channel,
            ssids=state.ssids,
            associated_clients=state.associated_clients if clients is None else clients,
        )

    def set_ap_clients(self, n: int) -> None:
        """Sugar over `drop_ap(clients=n)` for the common case of a client count change."""
        self.drop_ap(clients=n)

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
        """Snapshot the world as *enforced*, not as merely decided.

        `daemon.allocation` is the allocator's intent; it is not the seam this
        harness is meant to be testing. What actually matters for the two
        product invariants is what `daemon.enforcer` — `MockEnforcer` under
        `MOCK_HW` — has committed via `reconcile()`. Reading `enforcer.actual()`
        here means a bug that stops `render()`/`reconcile()` from carrying the
        allocator's decision through to the kernel-state seam shows up as a
        failing scenario, which is the whole point of item 3.
        """
        ap = self.hal.ap.status()
        actual = self.daemon.enforcer.actual()
        self.timeline.append(
            Frame(
                at_s=self.clock.now(),
                ap_running=ap.running,
                ap_clients=ap.associated_clients,
                backup_bytes=self._backup_bytes(actual),
                backup_state=str(self.daemon.allocator.backup_state),
                served=self._served_from_actual(actual),
            )
        )

    def _served_from_actual(self, actual: DesiredState | None) -> dict[str, int]:
        """Per-profile capacity currently provisioned, read off the enforced state.

        `DesiredState` doesn't carry per-profile ceilings (that's an allocator
        concept, lost by the time `render()` turns it into routes + shaping) —
        it carries fwmark->ifname routes and per-ifname shaping. We reconstruct
        a profile-keyed view by resolving each route's fwmark back to the
        profile it belongs to (fwmark == vlan, see `render()`) and reporting
        the shaping ceiling of the ifname it's currently routed to.
        """
        if actual is None:
            return {}
        shaping_by_ifname = {s.ifname: s.down_bps for s in actual.shaping}
        profile_by_vlan = {p.vlan: p.name for p in DEFAULT_PROFILES}
        served: dict[str, int] = {}
        for route in actual.routes:
            name = profile_by_vlan.get(route.fwmark)
            if name is None:
                continue
            served[name] = shaping_by_ifname.get(route.ifname, 0)
        return served

    def _backup_bytes(self, actual: DesiredState | None) -> int:
        """Whether a BACKUP atomic is currently carrying an enforced rule.

        Design decision (see PR body): `MockEnforcer.bytes_on(ifname)` is a
        running total accumulated across every reconcile that ever changed the
        desired state over the *whole* test run — it answers "how much has
        ever been applied", not "is BACKUP carrying traffic right now". The
        invariant this feeds (`assert_invariants`'s BACKUP-carries-zero-bytes
        check) needs a genuinely per-tick value: a frame is a leak only if
        BACKUP is *currently* enforced while `backup_state != "active"`. A
        cumulative counter can't answer that — it never goes back down, so
        once BACKUP had ever been active, every later frame would show a
        false leak; and a naive delta between ticks would read 0 on every tick
        where `reconcile()` short-circuited an unchanged state, which is most
        ticks (`reconcile()` only appends when the key changes), silently
        masking a real leak that persists unchanged across many ticks.
        Instead we look at what's *currently* committed —
        `enforcer.actual()` — and report the shaping ceiling for any BACKUP
        atomic whose interface is present there right now. That is exactly
        "BACKUP has an enforced rule at this instant", which is what the
        invariant is actually asking, and it self-corrects the tick after the
        allocator quiesces BACKUP and the enforcer's next reconcile drops it.

        Liveness budget is excluded deliberately — it is accounted separately
        and is not "carrying traffic" (ADR-006), and it never reaches
        `render()`/`DesiredState` in the first place.
        """
        if actual is None:
            return 0
        backup_ifnames = {a.ifname for a in self.daemon.registry.backups() if a.ifname}
        if not backup_ifnames:
            return 0
        return sum(s.down_bps + s.up_bps for s in actual.shaping if s.ifname in backup_ifnames)

    # -- assertions -----------------------------------------------------------

    def bytes_on(self, atomic_id: str) -> int:
        """The allocator's current ceiling for one atomic, summed across profiles.

        Deliberately reads `daemon.allocation.shares` (the allocator's decision),
        not the enforcer — `Allocation.shares` carries per-profile ceilings that
        `DesiredState` doesn't (see `_served_from_actual`'s docstring), and
        scenarios asserting "the phone carried nothing" want to know what was
        *decided*, at the same granularity a scenario declared demand in. The
        enforced-state seam is what `Frame.backup_bytes`/`assert_invariants`
        check instead — this helper and that invariant are deliberately looking
        at two different layers of the same pipeline.
        """
        allocation = self.daemon.allocation
        if allocation is None:
            return 0
        return sum(s.ceiling_bps for s in allocation.shares if s.atomic_id == atomic_id)

    def served_bps(self, profile_key: str) -> int:
        """The allocator's current ceiling for one profile ("critical"/"besteffort"),
        summed across whichever atomic(s) it's routed to. See `bytes_on` for why
        this reads the allocation rather than the enforced state.
        """
        allocation = self.daemon.allocation
        if allocation is None:
            return 0
        name = "Stable_critical" if profile_key == "critical" else "Stable_besteffort"
        return sum(s.ceiling_bps for s in allocation.shares if s.profile_name == name)

    def count_transitions(self) -> int:
        """How many times BACKUP has actually activated or released this run."""
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
