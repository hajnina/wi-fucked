"""Regression coverage for `enforce.render()` — item 4 of `traffic-blockers.md`,
plus item 5 / ADR-019's tunnel-owned egress.

Four bugs lived in `render()`/`_apply_cake`: a single shared routing table for
every atomic instead of one per atomic, routes emitted for zero-ceiling
shares, `allocation.quiesced` never actively consulted, and CAKE shaping
egress from `down_bps` instead of `up_bps`. This scenario drives an
allocation with an active BACKUP alongside a NORMAL atomic and asserts on the
enforced state — `daemon.enforcer.actual()` — which is exactly the seam
these bugs lived on.

Since ADR-019, every route's `ifname` is the tunnel interface, not the WAN
atomic's own `ifname` (see `test_lan_egress_tunnel.py` for that behaviour
directly) — so "independent per atomic" now means independent *tables*, and
tests here identify an atomic's routes by its table (`_table_for_atomic`)
rather than by `ifname`.
"""

from __future__ import annotations

from wifucked.atomics.model import Kind, Mode
from wifucked.enforce import _table_for_atomic
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

    # ADR-019: every route's next hop is the tunnel, never a WAN atomic's own
    # ifname — that's the whole point of tunnel-owned egress surviving a WAN
    # swap without `render()`'s output changing shape.
    tunnel_ifname = harness.daemon.tunnel.interface
    assert all(r.ifname == tunnel_ifname for r in actual.routes), (
        f"a route pointed somewhere other than the tunnel ({tunnel_ifname}) — got {actual.routes}"
    )

    wifi_table = _table_for_atomic(WIFI)
    phone_table = _table_for_atomic(PHONE)
    tables_present = {r.table for r in actual.routes}
    assert wifi_table in tables_present
    assert phone_table in tables_present
    assert wifi_table != phone_table, (
        "NORMAL and BACKUP atomics hashed to the same table — "
        f"got wifi={wifi_table} phone={phone_table}"
    )

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

    phone_table = _table_for_atomic(PHONE)
    besteffort_routes_to_phone = [
        r
        for r in actual.routes
        if r.table == phone_table and profile_by_vlan.get(r.fwmark) == "Stable_besteffort"
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
    phone_table = _table_for_atomic(PHONE)
    if actual is not None:
        # Routes all share the tunnel's ifname (ADR-019), so a quiesced
        # atomic's absence shows up as its table never appearing, not as its
        # ifname never appearing. Shaping is still keyed by the atomic's own
        # physical ifname, unaffected by ADR-019.
        assert not any(r.table == phone_table for r in actual.routes)
        assert not any(s.ifname == phone_ifname for s in actual.shaping)

    assert_invariants(harness.timeline)
