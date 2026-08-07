"""Scenario coverage for backlog item 15: a first-time client could never get
routed at all.

Every earlier scenario/e2e proof of ADR-019 egress hand-seeded demand with
`harness.set_demand()`, which sets both `down_bps` and `up_bps` non-zero from
tick one — exactly what hides this bug. This scenario instead drives the
harness's underlying `StaticDemand` estimator directly so it can model the
one moment that actually broke: a fresh atomic, a fresh allocator tick, and a
client whose only observed traffic so far is a request that has arrived
(`up_bps`) with no reply yet (`down_bps` still 0) — because no route has ever
existed to carry one back.

Found by `appliance/tests/e2e/`'s real fabric/tunnel proof (PR #48, stage
`18_tunnel_download_survives_chaos`): `route_rules=0` on every single
`enforce_reconcile` log line for a full 150s real-traffic run, and a real
`curl` unable to even open a TCP connection through the real tunnel. This
scenario is the `MOCK_HW=1` regression coverage for the same mechanism —
`appliance/tests/e2e/` remains the authority on whether it is fixed against
real kernel routing.
"""

from __future__ import annotations

from wifucked.atomics.model import Mode
from wifucked.enforce import _table_for_atomic

from .conftest import assert_invariants

WIFI = "wifi:hotel"


def test_up_only_first_packet_demand_gets_a_route(harness):
    """The deadlock itself: down_bps=0, up_bps>0 must still produce an
    enforced route — pre-fix, `ceiling_bps` came from `down_bps` alone, so
    this stayed unrouted forever (`route_rules=0` on every tick).
    """
    harness.add_atomic(WIFI, label="Hotel WiFi", mode=Mode.NORMAL, capacity_bps=10_000_000)

    # A client's very first request: it has arrived (up_bps, LAN rx) but no
    # reply has gone back yet (down_bps, LAN tx, still 0 — there was never a
    # route to carry one). Bypass `harness.set_demand()` deliberately: it
    # always seeds both directions together, which is exactly what every
    # earlier proof did and exactly what hid this bug.
    harness.demand.set("Stable_critical", down_bps=0, up_bps=300_000)
    harness.demand.set("Stable_besteffort", down_bps=0, up_bps=0)

    harness.run_for(seconds=5)

    actual = harness.daemon.enforcer.actual()
    assert actual is not None, "expected an enforced route for up-only first-packet demand"
    assert actual.routes, (
        "no route was ever installed for a client's first packet — the exact "
        "deadlock from traffic-blockers.md item 15 (route_rules stayed 0)"
    )

    wifi_table = _table_for_atomic(WIFI)
    assert any(r.table == wifi_table for r in actual.routes)

    assert_invariants(harness.timeline)


def test_up_only_demand_never_forces_capacity_onto_backup(harness):
    """The other half of the fix's contract: a first packet may open a NORMAL
    route for free, but must never, by itself, force capacity onto a metered
    BACKUP atomic with no measured down_bps demand to justify it (ADR-006).

    A weak NORMAL atomic plus a healthy BACKUP, with only up-only demand —
    never enough sustained *down_bps* deficit to justify activation, since
    down_bps stays 0 throughout. BACKUP must stay untouched.
    """
    harness.add_atomic(WIFI, label="Hotel WiFi", mode=Mode.NORMAL, capacity_bps=500_000)
    harness.add_atomic(
        "usbtether:phone",
        mode=Mode.BACKUP,
        capacity_bps=20_000_000,
        up_bps=8_000_000,
    )
    harness.demand.set("Stable_critical", down_bps=0, up_bps=300_000)
    harness.demand.set("Stable_besteffort", down_bps=0, up_bps=0)

    harness.run_for(minutes=3)  # comfortably past the 120s activation dwell

    assert harness.bytes_on("usbtether:phone") == 0, (
        "up-only demand must not be sufficient, by itself, to activate a metered BACKUP connection"
    )
    assert_invariants(harness.timeline)
