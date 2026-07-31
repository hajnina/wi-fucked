"""The scenario the product exists to survive.

A Pi in a moving van. Wi-Fi as NORMAL, a phone tether as BACKUP. The Wi-Fi
degrades, then vanishes, then returns. Critical traffic must stay usable,
best-effort must absorb the damage, and the phone must not carry a byte until
critical genuinely cannot be met.

This is the Phase 1 exit criterion, expressed as a test.
"""

from __future__ import annotations

from wifucked.atomics.model import Kind, Mode

from .conftest import assert_invariants

WIFI = "wifi:hotel"
PHONE = "usbtether:phone"


def _van(harness):
    harness.add_atomic(
        WIFI,
        label="Hotel WiFi",
        mode=Mode.NORMAL,
        capacity_bps=14_000_000,
        up_bps=3_000_000,
        rtt_ms=38,
    )
    harness.add_atomic(
        PHONE,
        kind=Kind.USB_TETHER,
        label="Martin's Phone",
        mode=Mode.BACKUP,
        capacity_bps=20_000_000,
        up_bps=8_000_000,
        rtt_ms=45,
    )
    harness.set_demand(critical_bps=2_000_000, besteffort_bps=9_000_000)
    return harness


def test_healthy_wifi_never_touches_backup(harness):
    _van(harness)
    harness.run_for(minutes=10)

    assert harness.bytes_on(PHONE) == 0
    assert harness.count_transitions() == 0
    assert_invariants(harness.timeline)


def test_moderate_degradation_is_tolerated_rather_than_paid_for(harness):
    """Demand exceeding NORMAL capacity is not, by itself, a reason to spend.

    Critical is still being met. Slower service is the correct outcome — this is
    the single most important behaviour in the product, and the easiest to
    regress by "improving" the allocator.
    """
    _van(harness)
    harness.set_capacity(WIFI, 8_000_000, rtt_ms=90)
    harness.set_demand(critical_bps=2_000_000, besteffort_bps=25_000_000)

    harness.run_for(minutes=15)

    assert harness.bytes_on(PHONE) == 0, "best-effort must never force paid connectivity"
    assert harness.served_bps("critical") >= 2_000_000
    assert_invariants(harness.timeline)


def test_severe_degradation_activates_backup_after_dwell(harness):
    """RTT 820 ms, 17% loss, capacity collapsed below critical demand."""
    _van(harness)
    harness.run_for(minutes=2)

    harness.set_capacity(WIFI, 1_400_000, up_bps=200_000, rtt_ms=820, loss_pct=17.0)
    harness.set_demand(critical_bps=3_100_000, besteffort_bps=20_000_000)

    # Inside the activation dwell nothing has been spent yet.
    harness.run_for(seconds=60)
    assert harness.bytes_on(PHONE) == 0, "must not activate before the dwell elapses"
    assert harness.daemon.allocator.backup_state == "arming"

    harness.run_for(minutes=3)
    assert harness.daemon.allocator.backup_state == "active"
    assert harness.bytes_on(PHONE) > 0
    assert_invariants(harness.timeline)


def test_best_effort_is_squeezed_before_critical(harness):
    _van(harness)
    harness.set_capacity(WIFI, 3_000_000, rtt_ms=250)
    harness.set_demand(critical_bps=2_000_000, besteffort_bps=30_000_000)

    harness.run_for(minutes=5)

    critical = harness.served_bps("critical")
    besteffort = harness.served_bps("besteffort")

    assert critical >= 2_000_000, "critical demand must be met while capacity allows"
    assert besteffort < 30_000_000, "best-effort must absorb the shortfall"
    assert_invariants(harness.timeline)


def test_wan_disappearing_is_a_normal_transition(harness):
    _van(harness)
    harness.run_for(minutes=2)

    harness.remove_atomic(WIFI)
    harness.set_demand(critical_bps=3_100_000, besteffort_bps=5_000_000)
    harness.run_for(minutes=5)

    assert harness.daemon.allocator.backup_state == "active"
    assert_invariants(harness.timeline)


def test_recovery_releases_backup_and_does_not_bounce(harness):
    _van(harness)
    harness.set_capacity(WIFI, 1_400_000, rtt_ms=820, loss_pct=17.0)
    harness.set_demand(critical_bps=3_100_000, besteffort_bps=20_000_000)
    harness.run_for(minutes=5)
    assert harness.daemon.allocator.backup_state == "active"

    harness.set_capacity(WIFI, 14_000_000, rtt_ms=40)
    harness.set_demand(critical_bps=2_000_000, besteffort_bps=9_000_000)

    # Recovery has its own dwell: one good sample must not release the backup.
    harness.run_for(minutes=2)
    assert harness.daemon.allocator.backup_state == "releasing"

    harness.run_for(minutes=5)
    assert harness.daemon.allocator.backup_state == "idle"
    assert harness.bytes_on(PHONE) == 0
    assert harness.count_transitions() == 2, "exactly one activation and one release"
    assert_invariants(harness.timeline)


def test_full_van_sequence_records_why(harness):
    """The machine must be able to explain the whole episode afterwards."""
    _van(harness)
    harness.run_for(minutes=1)
    harness.set_capacity(WIFI, 1_400_000, rtt_ms=820, loss_pct=17.0)
    harness.set_demand(critical_bps=3_100_000, besteffort_bps=20_000_000)
    harness.run_for(minutes=5)
    harness.remove_atomic(WIFI)
    harness.run_for(minutes=3)

    decisions = harness.daemon.telemetry.recent_decisions(50)
    actions = {d.action for d in decisions}
    assert "activate_backup" in actions

    activation = next(d for d in decisions if d.action == "activate_backup")
    assert activation.reason
    # A decision without its inputs cannot explain itself later (ADR-009).
    assert activation.inputs["critical_demand_bps"] > 0
    assert "activation_deficit_bps" in activation.thresholds

    assert_invariants(harness.timeline)
