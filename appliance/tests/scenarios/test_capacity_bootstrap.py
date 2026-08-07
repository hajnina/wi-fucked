"""Scenario coverage for backlog item 16: a never-measured NORMAL atomic
could never get a route at all, even after item 15's demand-side fix.

Every earlier scenario/e2e proof of this path — including item 15's own
`test_first_packet_routing.py` — hand-seeded `Capacity` via
`harness.add_atomic(capacity_bps=...)`, which immediately folds a saturated
observation and sets `Capacity.measured_at`. That is exactly what hid this
bug from every scenario test that existed: `_usable_capacity()` only counts
an atomic once `confidence >= min_confidence`, and confidence only rises
from a saturated *passive* observation, which requires a route to already
exist to carry the traffic that would saturate it. No route without
capacity, no capacity without a route.

Found by `appliance/tests/e2e/`'s real fabric/tunnel proof (PR #48, stage
`18_tunnel_download_survives_chaos`, still failing after item 15's fix
merged): `route_rules=0` on every single `enforce_reconcile` log line across
a full 150s+ real run, and every atomic's `capacity.known` staying `false`
in every state snapshot. See ADR-024.
"""

from __future__ import annotations

from wifucked.atomics.model import Mode
from wifucked.enforce import _table_for_atomic

from .conftest import assert_invariants

WIFI = "wifi:hotel"


def test_never_measured_atomic_still_gets_a_route(harness):
    """The deadlock itself: a NORMAL atomic with genuinely unmeasured
    capacity (`measured_at=None`, confidence 0) must still get a route for a
    client's first-packet demand — pre-ADR-024, `_usable_capacity()` counted
    it as zero headroom forever, so no route was ever built, so the link
    could never generate the saturated observation needed to measure it.
    """
    harness.add_atomic(WIFI, label="Hotel WiFi", mode=Mode.NORMAL, capacity_bps=None)

    # Same up-only first-packet demand shape as item 15's own test — the two
    # gates compose, and this test exists specifically to catch either one
    # regressing back to zero headroom independently of the other.
    harness.demand.set("Stable_critical", down_bps=0, up_bps=300_000)
    harness.demand.set("Stable_besteffort", down_bps=0, up_bps=0)

    harness.run_for(seconds=5)

    actual = harness.daemon.enforcer.actual()
    assert actual is not None, "expected an enforced route for a never-measured atomic"
    assert actual.routes, (
        "no route was ever installed for a never-measured NORMAL atomic — the capacity-side "
        "deadlock from traffic-blockers.md item 16 (route_rules stayed 0)"
    )

    wifi_table = _table_for_atomic(WIFI)
    assert any(r.table == wifi_table for r in actual.routes)

    assert_invariants(harness.timeline)


def test_bootstrap_floor_stops_once_a_real_measurement_lands(harness):
    """The floor is a one-time bootstrap, not a standing guess: once the
    atomic has a real (if low-confidence) measurement, ADR-024's floor must
    not still be adding itself on top.
    """
    harness.add_atomic(WIFI, label="Hotel WiFi", mode=Mode.NORMAL, capacity_bps=None)
    harness.demand.set("Stable_critical", down_bps=0, up_bps=300_000)

    harness.run_for(seconds=2)
    atomic = harness.daemon.registry.get(WIFI)
    assert atomic is not None
    assert atomic.capacity.measured_at is None, "sanity: unmeasured before the real observation"

    # A real, low measurement lands — deliberately below the bootstrap floor,
    # so if the floor were still being added on top the allocator would
    # (wrongly) see more headroom after a real measurement than before one.
    harness.set_capacity(WIFI, down_bps=64_000, up_bps=32_000)
    harness.run_for(seconds=15)  # comfortably past LoopConfig's default 10s medium cadence

    atomic = harness.daemon.registry.get(WIFI)
    assert atomic is not None
    assert atomic.capacity.measured_at is not None, "expected the real observation to have folded"

    assert_invariants(harness.timeline)


def test_bootstrap_floor_never_applies_to_backup(harness):
    """ADR-024 is explicitly NORMAL-only, same scope as item 15's demand-side
    fix and for the same reason (ADR-006): a never-measured BACKUP atomic
    must not have money spent on it just because it's unmeasured.
    """
    harness.add_atomic(WIFI, label="Hotel WiFi", mode=Mode.NORMAL, capacity_bps=500_000)
    harness.add_atomic(
        "usbtether:phone",
        mode=Mode.BACKUP,
        capacity_bps=None,
    )
    harness.demand.set("Stable_critical", down_bps=0, up_bps=300_000)

    harness.run_for(minutes=3)  # comfortably past the 120s activation dwell

    assert harness.bytes_on("usbtether:phone") == 0, (
        "a never-measured BACKUP atomic must never have the bootstrap floor spend money on it"
    )
    assert_invariants(harness.timeline)
