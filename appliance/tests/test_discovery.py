"""Discovery — turning mock hardware into atomics with the right Kind.

Covers the specific thing the appliance is for: telling a phone tether apart
from a USB Ethernet adapter, and a Wi-Fi network apart from another one, all
without ever touching a kernel interface name as identity (ADR-002).
"""

from __future__ import annotations

from wifucked.atomics.model import Health, Kind, Mode
from wifucked.clock import VirtualClock
from wifucked.discovery import (
    DEFAULT_WIFI_SCAN_MIN_INTERVAL_S,
    Discoverer,
    discover,
    ethernet_atomic,
)
from wifucked.hal.base import ScannedNetwork
from wifucked.hal.mock import build_mock_hal


def test_discovers_the_default_mock_world():
    hal = build_mock_hal()

    atomics = discover(hal)

    kinds = {a.kind for a in atomics}
    assert Kind.WIFI in kinds
    assert Kind.USB_TETHER in kinds


def test_distinguishes_usb_ethernet_adapter_from_tether():
    hal = build_mock_hal()
    hal.usb.attached.append(hal.usb.spare_ethernet_adapter)

    atomics = discover(hal)

    by_kind = {a.kind: a for a in atomics if a.kind in (Kind.USB_TETHER, Kind.USB_ETHERNET)}
    assert by_kind[Kind.USB_TETHER].attributes["transport"] == "USB tethering"
    assert by_kind[Kind.USB_ETHERNET].attributes["transport"] == "USB Ethernet"

    assert by_kind[Kind.USB_TETHER].id != by_kind[Kind.USB_ETHERNET].id


def test_non_usb_ethernet_is_not_mislabelled_as_usb():
    """`ethernet_atomic` builds a non-USB Ethernet atomic (backlog item 13).

    It must not be labelled `Kind.USB_ETHERNET` — that kind is explicitly for
    the USB-attached adapter case, which `_discover_usb` already covers.
    """
    hal = build_mock_hal()

    atomic = ethernet_atomic(hal, "wlan0")

    assert atomic is not None
    assert atomic.kind is Kind.ETHERNET
    assert atomic.kind is not Kind.USB_ETHERNET


def test_ethernet_atomic_returns_none_without_a_mac():
    hal = build_mock_hal()

    assert ethernet_atomic(hal, "no-such-interface") is None


def test_scanned_wifi_networks_are_unused_until_joined():
    """A merely-scanned network isn't even connected yet; it can't be "main"."""
    hal = build_mock_hal()

    atomics = discover(hal)

    assert all(a.mode is Mode.UNUSED for a in atomics if a.kind is Kind.WIFI)


def test_physically_connected_usb_devices_default_to_main():
    """ADR-022: a plugged-in connection enables itself; no dashboard visit needed."""
    hal = build_mock_hal()

    atomics = discover(hal)

    usb_atomics = [a for a in atomics if a.kind in (Kind.USB_TETHER, Kind.USB_ETHERNET)]
    assert usb_atomics, "mock HAL should report at least one attached USB device"
    assert all(a.mode is Mode.NORMAL for a in usb_atomics)


def test_connected_wifi_network_is_marked_good():
    hal = build_mock_hal()

    atomics = discover(hal)

    connected = [a for a in atomics if a.kind is Kind.WIFI and a.attributes["connected"] == "yes"]
    assert len(connected) == 1
    assert connected[0].health is Health.GOOD
    assert connected[0].ifname == "wlan0"


def test_usb_device_without_carrier_is_down():
    hal = build_mock_hal()
    hal.net.links["usb0"] = False

    atomics = discover(hal)

    tether = next(a for a in atomics if a.kind is Kind.USB_TETHER)
    assert tether.health is Health.DOWN


def test_connected_matching_requires_bssid_not_just_ssid():
    """Two different APs sharing an SSID (ADR-002) must not both read connected.

    Bug 2: matching only on SSID would mark both "Hotel WiFi" networks as
    connected once the station link associated with one of them.
    """
    hal = build_mock_hal()
    # A second, distinct access point broadcasting the same SSID as the one
    # the station is actually associated with.
    hal.wifi.networks.append(ScannedNetwork("Hotel WiFi", "ff:ff:ff:11:22:33", 1, -80))

    atomics = discover(hal)

    same_ssid = [a for a in atomics if a.kind is Kind.WIFI and a.attributes["ssid"] == "Hotel WiFi"]
    assert len(same_ssid) == 2
    connected = [a for a in same_ssid if a.attributes["connected"] == "yes"]
    assert len(connected) == 1, "only the actually-associated BSSID may read connected"
    assert connected[0].id != same_ssid[0].id or connected[0] is same_ssid[0]
    not_connected = [a for a in same_ssid if a.attributes["connected"] == "no"]
    assert len(not_connected) == 1
    assert not_connected[0].health is Health.UNKNOWN
    assert not_connected[0].ifname is None


class TestDiscovererScanCadence:
    """Fix 3: gate the AP-radio-sharing Wi-Fi scan to a slow, clock-based cadence."""

    def test_reuses_last_scan_within_the_minimum_interval(self):
        hal = build_mock_hal()
        clock = VirtualClock()
        discoverer = Discoverer(clock, wifi_scan_min_interval_s=60.0)

        first = discoverer.discover(hal)

        # Change what the radio would report; a throttled scan must not see it.
        hal.wifi.networks.append(ScannedNetwork("New Network", "11:22:33:44:55:66", 3, -40))
        clock.advance(30.0)
        second = discoverer.discover(hal)

        wifi_first = {a.id for a in first if a.kind is Kind.WIFI}
        wifi_second = {a.id for a in second if a.kind is Kind.WIFI}
        assert wifi_second == wifi_first, "scan must not re-run before the minimum interval"

    def test_scans_again_once_the_minimum_interval_elapses(self):
        hal = build_mock_hal()
        clock = VirtualClock()
        discoverer = Discoverer(clock, wifi_scan_min_interval_s=60.0)

        discoverer.discover(hal)
        hal.wifi.networks.append(ScannedNetwork("New Network", "11:22:33:44:55:66", 3, -40))
        clock.advance(61.0)
        atomics = discoverer.discover(hal)

        assert any(a.label == "New Network" for a in atomics if a.kind is Kind.WIFI)

    def test_usb_discovery_is_never_throttled(self):
        """Only the radio scan is gated; USB enumeration has no off-channel risk."""
        hal = build_mock_hal()
        clock = VirtualClock()
        discoverer = Discoverer(clock, wifi_scan_min_interval_s=1000.0)

        discoverer.discover(hal)
        hal.usb.attached.append(hal.usb.spare_ethernet_adapter)
        clock.advance(1.0)
        atomics = discoverer.discover(hal)

        assert any(a.kind is Kind.USB_ETHERNET for a in atomics)

    def test_default_min_interval_is_slower_than_the_medium_loop(self):
        """Backlog item 10: the ~10s medium loop must not drive off-channel scans."""
        assert DEFAULT_WIFI_SCAN_MIN_INTERVAL_S > 10.0
