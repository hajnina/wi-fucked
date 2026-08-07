"""Scenario coverage for ADR-023 — a LAN-out port's enforcement.

The DHCP-attempt/passive-listen pipeline itself is unit-tested directly
(`appliance/tests/test_lanout.py`, including the safety-critical "never
becomes a server on an ambiguous signal" cases). What this scenario proves is
the other half: once a port *is* `PortRole.LAN_OUT`, its traffic gets exactly
the same tunnel-routed treatment an AP LAN client's traffic gets (ADR-019) —
marked by its own `ifname`, riding the existing per-profile route rather than
needing a routing table of its own — and that it never re-enters the WAN pool
regardless of what `Mode` machinery might otherwise do with it.
"""

from __future__ import annotations

from wifucked.atomics.model import Kind, Mode, PortRole
from wifucked.policy import BEST_EFFORT

from .conftest import assert_invariants

WIFI = "wifi:hotel"
LAN_OUT_PORT = "usbeth:bareport"


def _classify_as_lan_out(harness, atomic_id: str) -> None:
    """Stand in for `LanOutClassifier` reaching this verdict.

    The pipeline that produces this outcome is unit-tested on its own
    (`test_lanout.py`) — this scenario is about what `enforce.render()` does
    with the result, not about re-proving the DHCP-attempt/passive-listen
    logic here too.
    """
    harness.add_atomic(atomic_id, kind=Kind.USB_ETHERNET, label="Bare port", mode=Mode.NORMAL)
    updated = harness.daemon.registry.set_role_and_mode(
        atomic_id, PortRole.LAN_OUT, Mode.UNUSED, reason="test_fixture"
    )
    assert updated is not None
    assert updated.ifname is not None


def test_lan_out_port_traffic_is_marked_and_routed_through_the_tunnel(harness):
    harness.add_atomic(WIFI, label="Hotel WiFi", mode=Mode.NORMAL, capacity_bps=10_000_000)
    harness.set_demand(critical_bps=1_000_000, besteffort_bps=500_000)
    _classify_as_lan_out(harness, LAN_OUT_PORT)

    harness.run_for(seconds=15)

    actual = harness.daemon.enforcer.actual()
    assert actual is not None

    lan_out_atomic = harness.daemon.registry.get(LAN_OUT_PORT)
    assert lan_out_atomic is not None
    assert lan_out_atomic.role is PortRole.LAN_OUT
    assert lan_out_atomic.ifname is not None

    # The port's own ifname is marked with BEST_EFFORT's vlan/fwmark — the
    # same class an undifferentiated AP LAN client gets under ADR-020's
    # default hotspot mode.
    assert (lan_out_atomic.ifname, BEST_EFFORT.vlan) in actual.lan_out_marks

    # That mark rides the tunnel the same way any other BEST_EFFORT route
    # does — no separate routing table, no ifname-specific route needed.
    tunnel_ifname = harness.daemon.tunnel.interface
    besteffort_routes = [r for r in actual.routes if r.fwmark == BEST_EFFORT.vlan]
    assert besteffort_routes, "expected a route for BEST_EFFORT once a NORMAL WAN atomic is serving"
    assert all(r.ifname == tunnel_ifname for r in besteffort_routes)

    assert_invariants(harness.timeline)


def test_lan_out_port_never_enters_the_wan_pool(harness):
    harness.add_atomic(WIFI, label="Hotel WiFi", mode=Mode.NORMAL, capacity_bps=10_000_000)
    harness.set_demand(critical_bps=1_000_000, besteffort_bps=500_000)
    _classify_as_lan_out(harness, LAN_OUT_PORT)

    harness.run_for(seconds=15)

    pool_ids = {a.id for a in harness.daemon.registry.normal_pool()}
    assert LAN_OUT_PORT not in pool_ids

    lan_out_atomic = harness.daemon.registry.get(LAN_OUT_PORT)
    assert lan_out_atomic is not None
    assert not lan_out_atomic.usable  # belt-and-suspenders guard on Atomic itself

    allocation = harness.daemon.allocation
    assert allocation is not None
    assert not any(s.atomic_id == LAN_OUT_PORT for s in allocation.shares)

    assert_invariants(harness.timeline)


def test_lan_out_port_disappearing_drops_its_mark_without_touching_ap_traffic(harness):
    """A LAN-out port unplugged mid-run must stop being marked (it's gone —
    nothing to mark traffic from), while the AP's own LAN routing is
    unaffected. Nothing here tears down existing kernel state (ADR-008); the
    port's mark simply stops being part of the newly rendered desired state.
    """
    harness.add_atomic(WIFI, label="Hotel WiFi", mode=Mode.NORMAL, capacity_bps=10_000_000)
    harness.set_demand(critical_bps=1_000_000, besteffort_bps=500_000)
    _classify_as_lan_out(harness, LAN_OUT_PORT)
    harness.run_for(seconds=15)

    before = harness.daemon.enforcer.actual()
    assert before is not None
    assert before.lan_out_marks

    harness.remove_atomic(LAN_OUT_PORT)
    harness.run_for(seconds=15)

    after = harness.daemon.enforcer.actual()
    assert after is not None
    assert after.lan_out_marks == ()

    # The Wi-Fi WAN atomic's own routing is untouched by the LAN-out port
    # disappearing.
    tunnel_ifname = harness.daemon.tunnel.interface
    assert any(r.ifname == tunnel_ifname for r in after.routes)

    assert_invariants(harness.timeline)
