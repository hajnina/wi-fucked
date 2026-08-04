"""Regression tests for backlog item 6.

Three bugs in `allocator/__init__.py`:

1. `_step_hysteresis` never left ACTIVE when the backup atomic vanished, and
   the RELEASING -> ACTIVE bounce-back skipped `activation_dwell_s` entirely.
2. `_build` emitted two conflicting `Share` entries per profile when the
   backup atomic was also the primary (empty NORMAL pool).
3. `_record` reported `action=no_connectivity` while a healthy BACKUP was
   ARMING, even though there was nothing wrong with connectivity.

See docs/backlog/traffic-blockers.md item 6.
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
    return harness


def test_active_leaves_state_when_backup_atomic_vanishes(harness):
    """Bug 1a: ACTIVE only ever checked `recovered`.

    If the BACKUP atomic disappears entirely while NORMAL is still in
    deficit, `recovered` is never true (NORMAL capacity did not improve) so
    the state machine was stuck in ACTIVE forever, with no backup atomic to
    actually be active on.
    """
    _van(harness)
    harness.set_capacity(WIFI, 1_400_000, up_bps=200_000, rtt_ms=820, loss_pct=17.0)
    harness.set_demand(critical_bps=3_100_000, besteffort_bps=20_000_000)

    harness.run_for(minutes=3)
    assert harness.daemon.allocator.backup_state == "active"

    # The backup vanishes; NORMAL is still nowhere near enough. `recovered`
    # stays false the whole time.
    harness.remove_atomic(PHONE)
    harness.run_for(minutes=1)

    assert harness.daemon.allocator.backup_state != "active", (
        "backup_state got stuck in ACTIVE after the backup atomic vanished"
    )
    assert harness.bytes_on(PHONE) == 0
    assert_invariants(harness.timeline)


def test_releasing_reactivation_honors_activation_dwell(harness):
    """Bug 1b: RELEASING -> ACTIVE skipped the activation dwell entirely.

    Once backup is RELEASING (recovered, but waiting out the recovery dwell)
    a single bad sample must not flip straight back to ACTIVE with zero
    dwell — it has to arm again, same as any other activation.
    """
    _van(harness)
    harness.set_capacity(WIFI, 1_400_000, up_bps=200_000, rtt_ms=820, loss_pct=17.0)
    harness.set_demand(critical_bps=3_100_000, besteffort_bps=20_000_000)
    harness.run_for(minutes=3)
    assert harness.daemon.allocator.backup_state == "active"

    # Recover, but not long enough to fully release (recovery_dwell_s=300).
    harness.set_capacity(WIFI, 14_000_000, rtt_ms=40)
    harness.set_demand(critical_bps=2_000_000, besteffort_bps=9_000_000)
    harness.run_for(minutes=2)
    assert harness.daemon.allocator.backup_state == "releasing"

    # Flip back into deficit before the recovery dwell completes.
    harness.set_capacity(WIFI, 1_400_000, up_bps=200_000, rtt_ms=820, loss_pct=17.0)
    harness.set_demand(critical_bps=3_100_000, besteffort_bps=20_000_000)
    harness.run_for(seconds=1)

    assert harness.daemon.allocator.backup_state != "active", (
        "RELEASING bounced straight back to ACTIVE without honoring activation_dwell_s"
    )
    assert harness.bytes_on(PHONE) == 0, (
        "backup carried traffic despite bypassing the activation dwell on re-arm"
    )

    # Confirm it does *not* re-activate before the fresh dwell elapses...
    harness.run_for(seconds=100)
    assert harness.daemon.allocator.backup_state != "active"

    # ...but does once it does.
    harness.run_for(minutes=2)
    assert harness.daemon.allocator.backup_state == "active"
    assert_invariants(harness.timeline)


def test_backup_as_primary_emits_one_share_per_profile(harness):
    """Bug 2: empty NORMAL pool, BACKUP promoted to primary.

    `_build` used to run both the headroom-based primary path and the
    backup-active path for the same atomic, emitting two conflicting Share
    entries per profile. There should be exactly one.
    """
    harness.add_atomic(
        PHONE,
        kind=Kind.USB_TETHER,
        label="Martin's Phone",
        mode=Mode.BACKUP,
        capacity_bps=20_000_000,
        up_bps=8_000_000,
        rtt_ms=45,
    )
    harness.set_demand(critical_bps=3_100_000, besteffort_bps=5_000_000)

    harness.run_for(minutes=3)
    assert harness.daemon.allocator.backup_state == "active"

    allocation = harness.daemon.allocation
    assert allocation is not None
    assert allocation.primary_id == PHONE

    phone_shares = [s for s in allocation.shares if s.atomic_id == PHONE]
    profile_names = [s.profile_name for s in phone_shares]
    assert len(profile_names) == len(set(profile_names)), (
        f"duplicate Share entries for {PHONE}: {phone_shares}"
    )
    # One profile per service profile (critical, besteffort), no duplicates.
    assert len(phone_shares) == 2

    critical_share = next(s for s in phone_shares if s.profile_name == "Stable_critical")
    assert critical_share.ceiling_bps >= 3_100_000
    besteffort_share = next(s for s in phone_shares if s.profile_name == "Stable_besteffort")
    assert besteffort_share.ceiling_bps == 0, "best-effort must not spend BACKUP money"

    assert_invariants(harness.timeline)


def test_arming_decision_record_does_not_claim_no_connectivity(harness):
    """Bug 3: ARMING with a healthy BACKUP falsely reported no_connectivity.

    `primary_id` is only `None` during ARMING when the NORMAL pool is empty
    (no `_best(pool)` candidate, and `primary` only falls back to `backup`
    once `backup_active`, which is false during ARMING) — so the pool must
    be empty here to actually exercise the bug. With a degraded-but-present
    NORMAL atomic, `primary_id` stays non-None throughout ARMING and the
    buggy branch is never reached.
    """
    harness.add_atomic(
        PHONE,
        kind=Kind.USB_TETHER,
        label="Martin's Phone",
        mode=Mode.BACKUP,
        capacity_bps=20_000_000,
        up_bps=8_000_000,
        rtt_ms=45,
    )
    harness.set_demand(critical_bps=3_100_000, besteffort_bps=20_000_000)

    # Inside the activation dwell: still ARMING, not yet ACTIVE.
    harness.run_for(seconds=60)
    assert harness.daemon.allocator.backup_state == "arming"
    assert harness.daemon.allocation is not None
    assert harness.daemon.allocation.primary_id is None

    decisions = harness.daemon.telemetry.recent_decisions(50)
    assert decisions, "no decision records were written"

    no_connectivity = [d for d in decisions if d.action == "no_connectivity"]
    assert not no_connectivity, (
        f"decision record falsely claimed no_connectivity while BACKUP was "
        f"arming: {no_connectivity}"
    )

    arming_decisions = [d for d in decisions if d.inputs.get("backup_state") == "arming"]
    assert arming_decisions, "expected at least one decision recorded while ARMING"
    for d in arming_decisions:
        assert d.action != "no_connectivity"

    assert_invariants(harness.timeline)
