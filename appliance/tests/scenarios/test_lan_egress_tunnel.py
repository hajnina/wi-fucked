"""Scenario coverage for ADR-019: LAN client egress is tunnel-owned.

Two things must be true, together:

1. A LAN client's traffic is enforced onto the tunnel interface (`wg0`), not
   a WAN atomic's own `ifname` — that's the fix itself.
2. That stays true, unchanged, across a WAN swap. The whole point of
   tunnel-owned egress is that a WAN swap changes which atomic *carries the
   tunnel* (`tunnel.bind_to`) without changing the shape of anything
   `enforce.render()` produces for LAN routing — no route flips its `ifname`
   when the active WAN changes underneath it.
"""

from __future__ import annotations

from wifucked.atomics.model import Mode
from wifucked.enforce import _table_for_atomic

from .conftest import assert_invariants

WIFI = "wifi:hotel"
USB = "usbeth:dock"


def test_lan_traffic_is_enforced_onto_the_tunnel_not_the_wan(harness):
    harness.add_atomic(WIFI, label="Hotel WiFi", mode=Mode.NORMAL, capacity_bps=10_000_000)
    harness.set_demand(critical_bps=1_000_000, besteffort_bps=500_000)
    harness.run_for(seconds=15)

    actual = harness.daemon.enforcer.actual()
    assert actual is not None
    assert actual.routes, "expected routes once a NORMAL atomic is serving demand"

    tunnel_ifname = harness.daemon.tunnel.interface
    wifi_ifname = harness.daemon.registry.get(WIFI).ifname
    assert (
        wifi_ifname != tunnel_ifname
    )  # sanity: they must actually differ for this to prove anything

    # Every route's next hop is the tunnel — never the WAN atomic's own ifname.
    assert all(r.ifname == tunnel_ifname for r in actual.routes)
    assert not any(r.ifname == wifi_ifname for r in actual.routes)

    # The tunnel itself is carried by the WAN atomic currently serving traffic
    # — that's `bind_to`'s job, a different mechanism from LAN routing.
    assert harness.daemon.tunnel.status().via_atomic_id == WIFI

    assert_invariants(harness.timeline)


def test_lan_routing_survives_a_wan_swap(harness):
    """Remove the active WAN atomic mid-test, add a different one, and confirm
    the LAN-to-tunnel hop itself never needs to change — only which atomic
    carries the tunnel does.
    """
    harness.add_atomic(WIFI, label="Hotel WiFi", mode=Mode.NORMAL, capacity_bps=10_000_000)
    harness.set_demand(critical_bps=1_000_000, besteffort_bps=500_000)
    harness.run_for(seconds=15)

    tunnel_ifname = harness.daemon.tunnel.interface
    before = harness.daemon.enforcer.actual()
    assert before is not None
    assert before.routes
    assert all(r.ifname == tunnel_ifname for r in before.routes)
    assert harness.daemon.tunnel.status().via_atomic_id == WIFI
    wifi_table = _table_for_atomic(WIFI)
    assert any(r.table == wifi_table for r in before.routes)

    # The WAN swap: the hotel Wi-Fi vanishes (a normal state transition, not
    # an error — ADR-002's atomic identity means this is not "wlan0 renamed",
    # it's "this atomic is gone"), a USB Ethernet dongle appears in its place.
    harness.remove_atomic(WIFI)
    harness.add_atomic(USB, label="USB dock", mode=Mode.NORMAL, capacity_bps=10_000_000)
    harness.run_for(seconds=15)

    after = harness.daemon.enforcer.actual()
    assert after is not None
    assert after.routes, "expected routes to still be enforced after the WAN swap"

    # The LAN-to-tunnel hop is unchanged — still the tunnel, still nothing
    # WAN-specific in what render() produced for routing.
    assert all(r.ifname == tunnel_ifname for r in after.routes)

    # What did change: which atomic is carrying the tunnel, and which table
    # is now live (the old WAN atomic's table is gone from the ruleset since
    # it no longer has an active share; nothing tears it down, ADR-008 — it's
    # simply absent from the newly rendered/reconciled desired state).
    assert harness.daemon.tunnel.status().via_atomic_id == USB
    usb_table = _table_for_atomic(USB)
    assert any(r.table == usb_table for r in after.routes)
    assert usb_table != wifi_table

    assert_invariants(harness.timeline)
