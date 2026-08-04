"""Harness self-check.

Item 3's whole point is making the two product invariants *falsifiable*
through the enforcer/`MockAp` seams, rather than the harness quietly reading
back exactly what it just wrote in. These tests prove it: they deliberately
break the mock world with `Harness.drop_ap()` — a lever that didn't exist
before this change — and confirm `assert_invariants` actually raises.

Nothing here is a real scenario a device would hit; it exists purely so a
future change to the harness that silently stops checking anything gets
caught. See docs/backlog/traffic-blockers.md item 3.
"""

from __future__ import annotations

import pytest

from wifucked.atomics.model import Mode

from .conftest import assert_invariants

WIFI = "wifi:office"


def _steady_world(harness):
    harness.add_atomic(WIFI, mode=Mode.NORMAL, capacity_bps=10_000_000)
    harness.set_demand(critical_bps=1_000_000, besteffort_bps=1_000_000)
    harness.run_for(seconds=5)
    return harness


def test_ap_dying_is_caught_by_the_invariant(harness):
    _steady_world(harness)

    harness.drop_ap(running=False)
    harness.run_for(seconds=1)

    with pytest.raises(AssertionError, match="AP dropped"):
        assert_invariants(harness.timeline)


def test_ap_losing_clients_is_caught_by_the_invariant(harness):
    _steady_world(harness)

    harness.drop_ap(clients=0)
    harness.run_for(seconds=1)

    with pytest.raises(AssertionError, match="AP lost clients"):
        assert_invariants(harness.timeline)


def test_set_ap_clients_is_sugar_for_drop_ap(harness):
    _steady_world(harness)

    harness.set_ap_clients(0)
    harness.run_for(seconds=1)

    with pytest.raises(AssertionError, match="AP lost clients"):
        assert_invariants(harness.timeline)
