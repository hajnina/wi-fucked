"""LAN-out DHCP-server fallback (ADR-023) — unit coverage for the
DHCP-attempt -> passive-listen -> DHCP-server pipeline.

This is the safety-critical part of the whole feature (ADR-022's Decision
section): a wrong "become a DHCP server" answer puts a second, competing
DHCP server on a network this device doesn't own. Every "did it correctly
refuse to become a server" case here is deliberately exercised, including
the ambiguous/partial-signal case, not just the clean heard/not-heard split.
"""

from __future__ import annotations

import time

from wifucked.atomics.model import Atomic, Health, Kind, Mode, PortRole
from wifucked.hal.base import DhcpLease
from wifucked.hal.mock import build_mock_hal
from wifucked.lanout import CANDIDATE_KINDS, LanOutClassifier, subnet_third_octet


def _wired_atomic(atomic_id: str, ifname: str, *, present: bool = True) -> Atomic:
    return Atomic(
        id=atomic_id,
        kind=Kind.USB_ETHERNET,
        label="USB Ethernet",
        mode=Mode.NORMAL,  # ADR-022's discovery default
        health=Health.GOOD if present else Health.DOWN,
        ifname=ifname if present else None,
        present=present,
    )


def _run_and_collect(classifier: LanOutClassifier, hal, atomics, *, timeout_s: float = 5.0):
    """Poll `consider()` until every in-flight pipeline has finished.

    The pipeline genuinely runs on a background thread (see the module
    docstring in `wifucked.lanout` for why), so a single `consider()` call
    right after kicking one off will usually see nothing done yet — this
    mirrors how `daemon.py`'s medium loop calls it every tick until an
    outcome appears.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        outcomes = classifier.consider(hal, atomics)
        if outcomes:
            return outcomes
        if not classifier._in_flight:
            return []
        time.sleep(0.01)
    raise AssertionError("classification pipeline did not finish within the test timeout")


def test_dhcp_lease_obtained_classifies_as_wan_and_leaves_mode_normal():
    hal = build_mock_hal()
    hal.dhcp.leases["eth1"] = DhcpLease(ip="192.168.1.50", gateway="192.168.1.1")
    atomic = _wired_atomic("usbeth:dock1", "eth1")
    classifier = LanOutClassifier(dhcp_client_timeout_s=0.01, passive_listen_timeout_s=0.01)

    outcomes = _run_and_collect(classifier, hal, [atomic])

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.role is PortRole.WAN
    assert outcome.mode is Mode.NORMAL
    assert outcome.reason == "dhcp_client_lease_obtained"
    # The passive-listen guard must never even run once a lease was obtained.
    assert not any(call[0] == "passive_listen_for_foreign_server" for call in hal.dhcp.calls)
    assert hal.dhcp.servers_started == []


def test_no_lease_but_foreign_server_heard_never_becomes_a_server():
    hal = build_mock_hal()
    hal.dhcp.leases["eth1"] = None
    hal.dhcp.foreign_heard["eth1"] = True
    atomic = _wired_atomic("usbeth:dock1", "eth1")
    classifier = LanOutClassifier(dhcp_client_timeout_s=0.01, passive_listen_timeout_s=0.01)

    outcomes = _run_and_collect(classifier, hal, [atomic])

    outcome = outcomes[0]
    assert outcome.role is PortRole.WAN  # unchanged — never became LAN_OUT
    assert outcome.mode is Mode.UNUSED  # not a working WAN either
    assert outcome.reason == "foreign_dhcp_server_heard_or_undetermined"
    assert hal.dhcp.servers_started == []


def test_ambiguous_or_undetermined_signal_never_becomes_a_server():
    """The mock's default (nothing configured) is exactly the ambiguous case:
    no lease, and `foreign_heard` was never explicitly set to False. This is
    the case ADR-022's Decision section is most explicit about: a partial or
    undetermined signal must be treated as "something might be there," not
    as license to proceed.
    """
    hal = build_mock_hal()
    hal.dhcp.leases["eth1"] = None
    # hal.dhcp.foreign_heard deliberately left empty — MockDhcp defaults to
    # True (conservative) for exactly this reason.
    atomic = _wired_atomic("usbeth:dock1", "eth1")
    classifier = LanOutClassifier(dhcp_client_timeout_s=0.01, passive_listen_timeout_s=0.01)

    outcomes = _run_and_collect(classifier, hal, [atomic])

    outcome = outcomes[0]
    assert outcome.role is PortRole.WAN
    assert outcome.mode is Mode.UNUSED
    assert hal.dhcp.servers_started == []


def test_no_lease_and_quiet_segment_becomes_lan_out():
    hal = build_mock_hal()
    hal.dhcp.leases["eth1"] = None
    hal.dhcp.foreign_heard["eth1"] = False  # explicitly quiet
    atomic = _wired_atomic("usbeth:dock1", "eth1")
    classifier = LanOutClassifier(
        dhcp_client_timeout_s=0.01,
        passive_listen_timeout_s=0.01,
        gateway_prefix="10.44",
        base_third_octet=0,
    )

    outcomes = _run_and_collect(classifier, hal, [atomic])

    outcome = outcomes[0]
    assert outcome.role is PortRole.LAN_OUT
    assert outcome.mode is Mode.UNUSED  # excluded from the WAN pool
    assert outcome.reason == "became_dhcp_server"
    assert len(hal.dhcp.servers_started) == 1
    started_ifname, third_octet, gateway = hal.dhcp.servers_started[0]
    assert started_ifname == "eth1"
    assert gateway == f"10.44.{third_octet}.1"


def test_start_server_failure_does_not_claim_lan_out():
    hal = build_mock_hal()
    hal.dhcp.leases["eth1"] = None
    hal.dhcp.foreign_heard["eth1"] = False
    hal.dhcp.server_start_ok["eth1"] = False
    atomic = _wired_atomic("usbeth:dock1", "eth1")
    classifier = LanOutClassifier(dhcp_client_timeout_s=0.01, passive_listen_timeout_s=0.01)

    outcomes = _run_and_collect(classifier, hal, [atomic])

    outcome = outcomes[0]
    assert outcome.role is PortRole.WAN  # never claims a role it didn't earn
    assert outcome.mode is Mode.UNUSED
    assert outcome.reason == "dhcp_server_start_failed"


def test_usb_tether_is_never_a_lan_out_candidate():
    """Phone tethering already carries connectivity via the modem's own NAT —
    there is no "bare downstream port" reading for it (see `wifucked.lanout`'s
    CANDIDATE_KINDS docstring).
    """
    hal = build_mock_hal()
    tether = Atomic(
        id="usbtether:phone1",
        kind=Kind.USB_TETHER,
        label="Phone",
        mode=Mode.NORMAL,
        health=Health.GOOD,
        ifname="usb0",
        present=True,
    )
    classifier = LanOutClassifier(dhcp_client_timeout_s=0.01, passive_listen_timeout_s=0.01)

    classifier.consider(hal, [tether])
    time.sleep(0.05)
    outcomes = classifier.consider(hal, [tether])

    assert outcomes == []
    assert hal.dhcp.calls == []
    assert Kind.USB_TETHER not in CANDIDATE_KINDS


def test_absent_atomic_is_never_a_candidate():
    hal = build_mock_hal()
    atomic = _wired_atomic("usbeth:dock1", "eth1", present=False)
    classifier = LanOutClassifier(dhcp_client_timeout_s=0.01, passive_listen_timeout_s=0.01)

    classifier.consider(hal, [atomic])
    time.sleep(0.05)
    outcomes = classifier.consider(hal, [atomic])

    assert outcomes == []
    assert hal.dhcp.calls == []


def test_already_lan_out_port_is_not_reclassified_every_tick():
    """Once a role is decided, a still-present port must not be re-run on
    every subsequent medium-loop tick — the pipeline costs real wall-clock
    time (bounded timeouts, twice) and there is nothing to re-decide while
    the port hasn't changed state.
    """
    hal = build_mock_hal()
    hal.dhcp.leases["eth1"] = None
    hal.dhcp.foreign_heard["eth1"] = False
    atomic = _wired_atomic("usbeth:dock1", "eth1")
    classifier = LanOutClassifier(dhcp_client_timeout_s=0.01, passive_listen_timeout_s=0.01)

    first = _run_and_collect(classifier, hal, [atomic])
    assert len(first) == 1

    calls_after_first = len(hal.dhcp.calls)
    for _ in range(5):
        outcomes = classifier.consider(hal, [atomic])
        assert outcomes == []
    assert len(hal.dhcp.calls) == calls_after_first  # no new pipeline runs


def test_replug_clears_the_prior_verdict_and_reclassifies():
    """The port goes absent (unplugged) then present again (replugged,
    possibly into a different network) — it must be reconsidered, not stuck
    on its first answer forever.
    """
    hal = build_mock_hal()
    hal.dhcp.leases["eth1"] = None
    hal.dhcp.foreign_heard["eth1"] = False
    atomic_id = "usbeth:dock1"
    classifier = LanOutClassifier(dhcp_client_timeout_s=0.01, passive_listen_timeout_s=0.01)

    present = _wired_atomic(atomic_id, "eth1", present=True)
    first = _run_and_collect(classifier, hal, [present])
    assert first[0].role is PortRole.LAN_OUT

    absent = _wired_atomic(atomic_id, "eth1", present=False)
    classifier.consider(hal, [absent])

    # Replugged, this time with a lease available (a different network).
    hal.dhcp.leases["eth1"] = DhcpLease(ip="192.168.9.5")
    present_again = _wired_atomic(atomic_id, "eth1", present=True)
    second = _run_and_collect(classifier, hal, [present_again])
    assert second[0].role is PortRole.WAN
    assert second[0].mode is Mode.NORMAL


def test_subnet_third_octet_is_deterministic_and_avoids_the_ap_subnet_range():
    a = subnet_third_octet("usbeth:dock1", base_third_octet=0)
    b = subnet_third_octet("usbeth:dock1", base_third_octet=0)
    other = subnet_third_octet("usbeth:dock2", base_third_octet=0)

    assert a == b  # same atomic id, same answer every time — nothing to persist
    assert a != other  # different ports don't collide (not guaranteed for all
    # inputs by a hash, but true for these two fixed ids — a regression here
    # would mean the hash degenerated, worth catching)
    assert a >= 50  # clear of the AP's own profile subnet(s), which use 0/1
