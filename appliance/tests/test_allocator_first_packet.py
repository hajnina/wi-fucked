"""Regression tests for backlog item 15: a first-time client could never get
routed at all.

`Allocator._build`'s NORMAL-pool share loop computed `want_bps` from
`demand[profile].down_bps` alone. `demand.CounterDemand`'s own docstring is
explicit that `down_bps` is the LAN interface's *transmit* delta (replies
going out to clients) and `up_bps` is the *receive* delta (requests coming in
from clients). A brand-new client's first packet — a SYN, a DNS query —
increments `up_bps`, never `down_bps`: it physically arrives regardless of
whether a route exists yet to forward it anywhere. But `enforce.render()`
only ever installs a route for a share whose `ceiling_bps > 0`, and that
ceiling came from `down_bps` alone — which can only become non-zero *after*
a reply has already made it back through a route that, by construction,
does not exist yet. Real deadlock: no route without demand, no demand
without a route.

These tests exercise `Allocator._build`/`decide()` directly, at the unit
level, since the bug is fully reproducible without any daemon/HAL wiring —
see `appliance/tests/scenarios/test_first_packet_routing.py` for the same
bug proven through the full control-loop -> enforced-kernel-state seam.
"""

from __future__ import annotations

from wifucked.allocator import Allocator
from wifucked.atomics.model import Atomic, Capacity, Health, Kind, Mode
from wifucked.clock import VirtualClock
from wifucked.demand import ClassDemand
from wifucked.telemetry import Telemetry

WIFI = "wifi:hotel"


def _normal_atomic(atomic_id: str = WIFI) -> Atomic:
    return Atomic(
        id=atomic_id,
        kind=Kind.WIFI,
        label="Hotel WiFi",
        mode=Mode.NORMAL,
        health=Health.GOOD,
        present=True,
        ifname=f"if-{atomic_id}",
        capacity=Capacity(down_bps=10_000_000, up_bps=3_000_000, confidence=1.0, measured_at=0.0),
    )


def _allocator(clock: VirtualClock) -> Allocator:
    return Allocator(clock, Telemetry(clock, None))


def test_first_packet_up_only_demand_still_gets_a_normal_ceiling():
    """The exact deadlock from item 15: down_bps=0, up_bps>0 (a fresh client's
    first request) must still produce a non-zero NORMAL ceiling so
    `enforce.render()` has something to install a route for.
    """
    clock = VirtualClock()
    allocator = _allocator(clock)
    atomic = _normal_atomic()

    demand = {
        "Stable_critical": ClassDemand("Stable_critical", down_bps=0, up_bps=200_000),
        "Stable_besteffort": ClassDemand("Stable_besteffort", down_bps=0, up_bps=0),
    }

    allocation = allocator.decide([atomic], demand)

    critical_share = next(
        s for s in allocation.shares if s.atomic_id == WIFI and s.profile_name == "Stable_critical"
    )
    assert critical_share.ceiling_bps > 0, (
        "a first packet (up_bps only, down_bps still zero) must open a NORMAL "
        "route — pre-fix this stayed 0 forever, per traffic-blockers.md item 15"
    )


def test_zero_demand_in_both_directions_still_yields_zero_ceiling():
    """Sanity: the fix must not hand out capacity to a profile with no demand
    at all in either direction — only up-only demand should change anything.
    """
    clock = VirtualClock()
    allocator = _allocator(clock)
    atomic = _normal_atomic()

    demand = {
        "Stable_critical": ClassDemand("Stable_critical", down_bps=0, up_bps=0),
        "Stable_besteffort": ClassDemand("Stable_besteffort", down_bps=0, up_bps=0),
    }

    allocation = allocator.decide([atomic], demand)

    assert all(s.ceiling_bps == 0 for s in allocation.shares if s.atomic_id == WIFI)


def test_up_only_demand_cannot_trigger_backup_activation():
    """ADR-006 money-safety guard, part 1: `_step_hysteresis` decides whether
    to arm/activate BACKUP from `_demand_for` (`down_bps` only, unmodified by
    this fix). Large up-only demand — the "many first packets" case — must
    not, by itself, create the deficit that arms BACKUP at all.
    """
    clock = VirtualClock()
    allocator = _allocator(clock)
    backup = Atomic(
        id="usbtether:phone",
        kind=Kind.USB_TETHER,
        label="Phone",
        mode=Mode.BACKUP,
        health=Health.GOOD,
        present=True,
        ifname="if-phone",
        capacity=Capacity(down_bps=20_000_000, up_bps=8_000_000, confidence=1.0, measured_at=0.0),
    )

    # No NORMAL pool at all, and a huge *up-only* critical demand — if the fix
    # had leaked into demand accounting used for the activation deficit, this
    # would arm BACKUP. down_bps stays 0 throughout, so it must not.
    demand = {
        "Stable_critical": ClassDemand("Stable_critical", down_bps=0, up_bps=5_000_000),
        "Stable_besteffort": ClassDemand("Stable_besteffort", down_bps=0, up_bps=0),
    }
    allocator.decide([backup], demand)
    clock.advance(121)  # past activation_dwell_s, in case it wrongly armed
    allocator.decide([backup], demand)

    assert allocator.backup_state != "active", (
        "up-only demand alone triggered BACKUP activation — ADR-006 money-safety broken"
    )


def test_backup_ceiling_still_gated_on_down_bps_alone_not_up_bps():
    """ADR-006 money-safety guard, part 2: once BACKUP is genuinely ACTIVE
    (via real down_bps deficit), its per-profile ceiling must still come
    from `down_bps` alone, unaffected by whatever `up_bps` happens to be —
    the fix is NORMAL-pool only (`_build`'s headroom loop), the BACKUP block
    is untouched.
    """
    clock = VirtualClock()
    allocator = _allocator(clock)
    backup = Atomic(
        id="usbtether:phone",
        kind=Kind.USB_TETHER,
        label="Phone",
        mode=Mode.BACKUP,
        health=Health.GOOD,
        present=True,
        ifname="if-phone",
        capacity=Capacity(down_bps=20_000_000, up_bps=8_000_000, confidence=1.0, measured_at=0.0),
    )

    # Real down_bps deficit (no NORMAL pool at all) forces genuine activation.
    # up_bps is deliberately much larger than down_bps so a leak of the
    # NORMAL-pool fix into this block would be visible as ceiling_bps tracking
    # up_bps instead of down_bps.
    demand = {
        "Stable_critical": ClassDemand("Stable_critical", down_bps=3_000_000, up_bps=9_000_000),
        "Stable_besteffort": ClassDemand("Stable_besteffort", down_bps=1_000_000, up_bps=6_000_000),
    }
    allocator.decide([backup], demand)
    clock.advance(121)  # clear activation_dwell_s
    allocation = allocator.decide([backup], demand)

    assert allocator.backup_state == "active"
    critical_share = next(
        s
        for s in allocation.shares
        if s.atomic_id == backup.id and s.profile_name == "Stable_critical"
    )
    assert critical_share.ceiling_bps == 3_000_000, (
        "BACKUP ceiling must track down_bps exactly, unaffected by up_bps — "
        f"got {critical_share.ceiling_bps}"
    )

    besteffort_share = next(
        s
        for s in allocation.shares
        if s.atomic_id == backup.id and s.profile_name == "Stable_besteffort"
    )
    assert besteffort_share.ceiling_bps == 0, "may_use_backup=False must still be honored"
