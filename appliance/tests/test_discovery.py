"""Discovery — turning mock hardware into atomics with the right Kind.

Covers the specific thing the appliance is for: telling a phone tether apart
from a USB Ethernet adapter, and a Wi-Fi network apart from another one, all
without ever touching a kernel interface name as identity (ADR-002).
"""

from __future__ import annotations

from wifucked.atomics.model import Health, Kind, Mode
from wifucked.discovery import discover
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


def test_discovered_connections_are_unused_until_classified():
    """Discovery reports presence; it never grants permission on its own."""
    hal = build_mock_hal()

    atomics = discover(hal)

    assert all(a.mode is Mode.UNUSED for a in atomics)


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
