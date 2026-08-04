"""Regression coverage for `enforce.render()` — item 4 of `traffic-blockers.md`.

Four bugs lived in `render()`/`_apply_cake`: a single shared routing table for
every atomic instead of one per atomic, routes emitted for zero-ceiling
shares, `allocation.quiesced` never actively consulted, and CAKE shaping
egress from `down_bps` instead of `up_bps`. This scenario drives an
allocation with an active BACKUP alongside a NORMAL atomic and asserts on the
enforced state — `daemon.enforcer.actual()` — which is exactly the seam
these bugs lived on.
"""

from __future__ import annotations

from wifucked.atomics.model import Kind, Mode
from wifucked.policy import DEFAULT_PROFILES

from .conftest import assert_invariants

WIFI = "wifi:hotel"
PHONE = "usbtether:phone"


def _forced_backup(harness):
    """A NORMAL atomic too weak to meet critical demand, plus a healthy BACKUP.

    Deliberately harsh and sustained so the allocator's activation dwell
    (120 s) is cleared and BACKUP goes genuinely ACTIVE, not just ARMING —
    the routing/shaping bugs this test targets only show up once BACKUP is
    actually carrying shares.
    """
    harness.add_atomic(WIFI, label="Hotel WiFi", mode=Mode.NORMAL, capacity_bps=500_000)
    harness.add_atomic(
        PHONE,
        kind=Kind.USB_TETHER,
        label="Phone",
        mode=Mode.BACKUP,
        capacity_bps=20_000_000,
        up_bps=8_000_000,
    )
    harness.set_demand(critical_bps=3_000_000, besteffort_bps=2_000_000)
    return harness


def test_normal_and_backup_get_independent_routing_tables(harness):
    """Bug 1: `render()` reused one constant table for every atomic.

    Once BACKUP is active, NORMAL and BACKUP must each carry their own,
    distinct policy-routing table — the whole point of `ip rule` steering
    marked traffic to a *specific* atomic's default route. A single shared
    table would mean both classes' traffic ends up governed by whichever
    route was installed last, defeating per-atomic routing entirely.
    """
    _forced_backup(harness)
    harness.run_for(seconds=150)  # clears the 120 s activation dwell

    assert harness.daemon.allocator.backup_state == "active"

    actual = harness.daemon.enforcer.actual()
    assert actual is not None
    assert actual.routes, "expected at least one installed route once BACKUP is active"

    wifi_ifname = harness.daemon.registry.get(WIFI).ifname
    phone_ifname = harness.daemon.registry.get(PHONE).ifname

    tables_by_ifname = {r.ifname: r.table for r in actual.routes}
    assert wifi_ifname in tables_by_ifname
    assert phone_ifname in tables_by_ifname
    assert tables_by_ifname[wifi_ifname] != tables_by_ifname[phone_ifname], (
        f"NORMAL and BACKUP atomics were routed through the same table — got {tables_by_ifname}"
    )

    # Every route for a given ifname must agree on that ifname's table across
    # the whole ruleset (multiple profiles can route to the same atomic).
    for ifname, table in tables_by_ifname.items():
        assert all(r.table == table for r in actual.routes if r.ifname == ifname)

    assert_invariants(harness.timeline)


def test_zero_ceiling_shares_produce_no_route(harness):
    """Bug 2: a `Share` with `ceiling_bps == 0` ("not routed here") must not
    still get a `RouteRule` emitted for it.

    Best-effort traffic is never permitted onto BACKUP
    (`ServiceProfile.may_use_backup = False`), so once BACKUP is active the
    allocator emits a zero-ceiling best-effort share for it. That share must
    not produce a route pointing best-effort traffic at the phone.
    """
    _forced_backup(harness)
    harness.run_for(seconds=150)

    assert harness.daemon.allocator.backup_state == "active"

    allocation = harness.daemon.allocation
    assert allocation is not None
    zero_shares = [s for s in allocation.shares if s.ceiling_bps == 0]
    assert zero_shares, "expected the allocator to emit at least one zero-ceiling share here"

    actual = harness.daemon.enforcer.actual()
    assert actual is not None
    profile_by_vlan = {p.vlan: p.name for p in DEFAULT_PROFILES}

    phone_ifname = harness.daemon.registry.get(PHONE).ifname
    besteffort_routes_to_phone = [
        r
        for r in actual.routes
        if r.ifname == phone_ifname and profile_by_vlan.get(r.fwmark) == "Stable_besteffort"
    ]
    assert not besteffort_routes_to_phone, (
        "best-effort traffic was routed to BACKUP despite a zero-ceiling share — "
        f"got {besteffort_routes_to_phone}"
    )

    assert_invariants(harness.timeline)


def test_quiesced_atomic_never_appears_in_routes(harness):
    """Bug 3: `allocation.quiesced` atomics must never surface as a route.

    Before BACKUP activates, the phone is quiesced (`backup_active=False`
    means `_build` appends it to `quiesced` and emits no shares for it at
    all). Confirm the enforced state agrees: no route or shaping entry
    references the phone's interface while it is quiesced.
    """
    _forced_backup(harness)
    harness.run_for(seconds=5)  # nowhere near the activation dwell

    allocation = harness.daemon.allocation
    assert allocation is not None
    assert PHONE in allocation.quiesced
    assert not any(s.atomic_id == PHONE for s in allocation.shares)

    actual = harness.daemon.enforcer.actual()
    phone_ifname = harness.daemon.registry.get(PHONE).ifname
    if actual is not None:
        assert not any(r.ifname == phone_ifname for r in actual.routes)
        assert not any(s.ifname == phone_ifname for s in actual.shaping)

    assert_invariants(harness.timeline)
