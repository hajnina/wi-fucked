"""ADR-020: the interim default — one hotspot, one class, no VLAN split.

`Config()` alone now yields `lan_mode="single"`, which collapses the LAN to a
single `BEST_EFFORT` profile (`policy.profiles_for_lan_mode`). The one thing
that must never happen here is `BACKUP` activating for undifferentiated
traffic — `BEST_EFFORT.may_use_backup` is `False` specifically so collapsing
the two classes onto one SSID can't accidentally spend the user's money.
"""

from __future__ import annotations

import pytest

from wifucked.atomics.model import Kind, Mode
from wifucked.lan import lan_ifname_for_profile
from wifucked.policy import BEST_EFFORT, CRITICAL

from .conftest import Harness, assert_invariants

WIFI = "wifi:hotel"
PHONE = "usbtether:phone"


@pytest.fixture
def harness_single() -> Harness:
    return Harness(lan_mode="single")


def test_default_config_exposes_only_best_effort():
    harness = Harness(lan_mode="single")
    assert harness.daemon.profiles == (BEST_EFFORT,)


def test_backup_never_activates_no_matter_how_starved_the_single_class_is(harness_single):
    """Even demand set on the "critical" key must not reach BACKUP here.

    There is no critical channel in single-hotspot mode — `set_demand`'s
    critical_bps has nothing to attach to once `daemon.profiles` is just
    `(BEST_EFFORT,)`, and `BEST_EFFORT.may_use_backup` is False regardless.
    """
    harness = harness_single
    harness.add_atomic(WIFI, label="Hotel WiFi", mode=Mode.NORMAL, capacity_bps=200_000)
    harness.add_atomic(
        PHONE,
        kind=Kind.USB_TETHER,
        label="Phone",
        mode=Mode.BACKUP,
        capacity_bps=20_000_000,
        up_bps=8_000_000,
    )
    harness.set_demand(critical_bps=5_000_000, besteffort_bps=5_000_000)
    harness.run_for(minutes=10)

    assert harness.daemon.allocator.backup_state == "idle"
    assert_invariants(harness.timeline)


def test_lan_ifname_has_no_vlan_split_in_single_mode():
    """Both would-be classes resolve to the same physical interface."""
    assert lan_ifname_for_profile(BEST_EFFORT, "single") == "wlan0"
    assert lan_ifname_for_profile(CRITICAL, "single") == "wlan0"
