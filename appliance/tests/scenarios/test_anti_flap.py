"""Hysteresis.

WAN quality oscillates on a scale of seconds. Without asymmetric thresholds and
dwell times, the appliance would activate and release a metered connection
repeatedly — expensive, and visibly broken.

Any change to activation or recovery thresholds needs a test here
(docs/sop/SOP-003).
"""

from __future__ import annotations

from dirty.atomics.model import Kind, Mode

from .conftest import assert_invariants

WIFI = "wifi:flappy"
PHONE = "usbtether:phone"


def _pair(harness):
    harness.add_atomic(WIFI, label="Flappy WiFi", mode=Mode.NORMAL, capacity_bps=9_000_000)
    harness.add_atomic(
        PHONE,
        kind=Kind.USB_TETHER,
        label="Phone",
        mode=Mode.BACKUP,
        capacity_bps=20_000_000,
    )
    harness.set_demand(critical_bps=3_000_000, besteffort_bps=8_000_000)
    return harness


def test_oscillating_capacity_does_not_flap_backup(harness):
    """Capacity swinging every 20 s must not produce a matching swing in spend."""
    _pair(harness)

    for _ in range(20):
        harness.set_capacity(WIFI, 1_000_000, rtt_ms=700, loss_pct=12.0)
        harness.run_for(seconds=20)
        harness.set_capacity(WIFI, 9_000_000, rtt_ms=40)
        harness.run_for(seconds=20)

    # 20 cycles of oscillation, each shorter than the activation dwell. If the
    # controller tracked the input it would have transitioned dozens of times.
    assert harness.count_transitions() <= 2, (
        f"backup transitioned {harness.count_transitions()} times under oscillating "
        f"input — hysteresis is not holding"
    )
    assert_invariants(harness.timeline)


def test_brief_degradation_never_activates(harness):
    """A dip shorter than the activation dwell costs the user nothing."""
    _pair(harness)
    harness.run_for(minutes=1)

    harness.set_capacity(WIFI, 500_000, rtt_ms=900, loss_pct=25.0)
    harness.run_for(seconds=90)  # under the 120 s activation dwell
    harness.set_capacity(WIFI, 9_000_000, rtt_ms=40)
    harness.run_for(minutes=5)

    assert harness.count_transitions() == 0
    assert harness.bytes_on(PHONE) == 0
    assert_invariants(harness.timeline)


def test_recovery_dwell_is_longer_than_activation_dwell(harness):
    """Asymmetry is the point: releasing must be more reluctant than activating.

    Releasing early puts the user straight back into the degradation that caused
    the activation, so recovery is deliberately slower.
    """
    thresholds = harness.daemon.config.thresholds
    assert thresholds.recovery_dwell_s > thresholds.activation_dwell_s
    assert thresholds.recovery_margin_bps > thresholds.activation_deficit_bps


def test_backup_absent_means_no_transitions(harness):
    """With nothing to fail over to, the controller must not thrash trying."""
    harness.add_atomic(WIFI, mode=Mode.NORMAL, capacity_bps=500_000)
    harness.set_demand(critical_bps=5_000_000, besteffort_bps=5_000_000)

    harness.run_for(minutes=20)

    assert harness.count_transitions() == 0
    assert_invariants(harness.timeline)
