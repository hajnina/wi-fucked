"""Atomic identity — the abstraction most easily broken by well-meaning code.

If any of these fail, plug-and-play is broken: a phone that gets replugged
becomes a different connection, and its mode, learned capacity, and cost history
are all lost. See ADR-002.
"""

from __future__ import annotations

from dirty.atomics.identity import (
    is_locally_administered,
    mac_id,
    modem_id,
    usb_id,
    wifi_id,
)
from dirty.atomics.model import Kind


class TestWifiIdentity:
    def test_is_stable_across_calls(self):
        assert wifi_id("Hotel WiFi", "aa:bb:cc:11:22:33") == wifi_id(
            "Hotel WiFi", "aa:bb:cc:11:22:33"
        )

    def test_survives_roaming_between_access_points(self):
        """A network with several APs must stay one atomic.

        Only the BSSID's vendor prefix is keyed on, so roaming between APs of
        the same network does not fragment its learned history.
        """
        assert wifi_id("Campsite", "aa:bb:cc:11:22:33") == wifi_id("Campsite", "aa:bb:cc:99:88:77")

    def test_distinguishes_different_networks_sharing_an_ssid(self):
        """'BTWiFi' in two towns is two networks, not one."""
        assert wifi_id("BTWiFi", "aa:bb:cc:00:00:01") != wifi_id("BTWiFi", "dd:ee:ff:00:00:01")

    def test_handles_missing_bssid(self):
        assert wifi_id("Open Network").startswith("wifi:")

    def test_survives_awkward_ssids(self):
        for ssid in ("", "  ", "🥧 café", "a" * 200, "net/work:name"):
            assert wifi_id(ssid, "aa:bb:cc:11:22:33").startswith("wifi:")


class TestUsbIdentity:
    def test_serial_is_the_stable_key(self):
        """Same phone, different port, different enumeration order — one atomic."""
        first = usb_id(Kind.USB_TETHER, "05ac", "12a8", "PHONE001", "1-1")
        second = usb_id(Kind.USB_TETHER, "05ac", "12a8", "PHONE001", "2-4")
        assert first == second

    def test_two_identical_phones_are_two_atomics(self):
        assert usb_id(Kind.USB_TETHER, "05ac", "12a8", "PHONE001") != usb_id(
            Kind.USB_TETHER, "05ac", "12a8", "PHONE002"
        )

    def test_falls_back_to_port_path_without_a_serial(self):
        """The known weak point in ADR-002 — stable only per port, and that is
        documented rather than pretended away."""
        assert usb_id(Kind.USB_TETHER, "0bda", "8152", None, "1-1") == usb_id(
            Kind.USB_TETHER, "0bda", "8152", None, "1-1"
        )
        assert usb_id(Kind.USB_TETHER, "0bda", "8152", None, "1-1") != usb_id(
            Kind.USB_TETHER, "0bda", "8152", None, "1-2"
        )

    def test_tether_and_ethernet_are_distinct_namespaces(self):
        assert (
            usb_id(Kind.USB_TETHER, "0bda", "8152", "X").split(":")[0]
            != usb_id(Kind.USB_ETHERNET, "0bda", "8152", "X").split(":")[0]
        )


class TestOtherIdentities:
    def test_mac_identity_is_case_insensitive(self):
        assert mac_id(Kind.USB_ETHERNET, "AA:BB:CC:DD:EE:FF") == mac_id(
            Kind.USB_ETHERNET, "aa:bb:cc:dd:ee:ff"
        )

    def test_modem_identity_from_imei(self):
        assert modem_id("358240051111110").startswith("cellular:")


class TestRandomisedMacDetection:
    def test_detects_locally_administered(self):
        assert is_locally_administered("02:11:22:33:44:55")
        assert is_locally_administered("a6:11:22:33:44:55")

    def test_accepts_real_vendor_addresses(self):
        assert not is_locally_administered("b8:27:eb:11:22:33")  # Raspberry Pi

    def test_never_raises_on_rubbish(self):
        for value in ("", "not-a-mac", "zz:11:22"):
            assert is_locally_administered(value) is False


def test_no_identity_contains_an_interface_name():
    """The one thing identity must never encode.

    An `ifname` in an id would silently reattach state to the wrong connection
    the moment USB re-enumerates.
    """
    ids = [
        wifi_id("Hotel", "aa:bb:cc:11:22:33"),
        usb_id(Kind.USB_TETHER, "05ac", "12a8", "PHONE001", "1-1"),
        mac_id(Kind.USB_ETHERNET, "b8:27:eb:11:22:33"),
        modem_id("358240051111110"),
    ]
    for identity in ids:
        for ifname in ("wlan0", "wlan1", "usb0", "eth0", "ap0"):
            assert ifname not in identity
