"""Discovery — turn physical connectivity into atomics.

The user should never have to think about ``wlan0``, ``usb0``, or ``eth0``.
Discovery translates whatever the hardware reports into human-readable atomics
with stable identities (ADR-002).

Discovery reports what is *present*. It never decides what to use — that is the
user's choice, expressed as a mode, and a newly discovered connection is always
UNUSED until they say otherwise.
"""

from __future__ import annotations

from dirty.atomics.identity import mac_id, usb_id, wifi_id
from dirty.atomics.model import Atomic, Health, Kind, Mode
from dirty.hal import Hal
from dirty.logging import get_logger

log = get_logger("discovery")


def discover(hal: Hal) -> list[Atomic]:
    """One discovery sweep. Never raises: a broken source must not stop the rest."""
    found: list[Atomic] = []
    for name, source in (
        ("wifi", _discover_wifi),
        ("usb", _discover_usb),
    ):
        try:
            found.extend(source(hal))
        except Exception as exc:
            log.error(
                "Discovery source failed; continuing with the others",
                extra={
                    "workflow": "wan_discovery",
                    "state": "failed",
                    "intent": "enumerate available connections",
                    "source": name,
                    "reason": "unhandled error in discovery source",
                    "error": str(exc),
                },
                exc_info=True,
            )
    return found


def _discover_wifi(hal: Hal) -> list[Atomic]:
    link = hal.wifi.station_link()
    atomics: list[Atomic] = []

    for network in hal.wifi.scan():
        connected = link is not None and link.ssid == network.ssid
        atomics.append(
            Atomic(
                id=wifi_id(network.ssid, network.bssid),
                kind=Kind.WIFI,
                label=network.ssid,
                mode=Mode.UNUSED,
                health=Health.GOOD if connected else Health.UNKNOWN,
                ifname=link.ifname if connected else None,
                present=True,
                attributes={
                    "ssid": network.ssid,
                    "channel": str(network.channel),
                    "signal_dbm": str(network.signal_dbm),
                    "secured": "yes" if network.secured else "no",
                    "connected": "yes" if connected else "no",
                },
            )
        )
    return atomics


def _discover_usb(hal: Hal) -> list[Atomic]:
    atomics: list[Atomic] = []
    for device in hal.usb.devices():
        if not device.ifname:
            continue

        kind = Kind.USB_TETHER if device.is_tether else Kind.USB_ETHERNET
        carrier = hal.net.interfaces().get(device.ifname, False)
        atomics.append(
            Atomic(
                id=usb_id(kind, device.vendor, device.product, device.serial, device.port_path),
                kind=kind,
                label=device.description or "USB connection",
                mode=Mode.UNUSED,
                health=Health.GOOD if carrier else Health.DOWN,
                ifname=device.ifname,
                present=True,
                attributes={
                    "transport": "USB tethering" if device.is_tether else "USB Ethernet",
                    "vendor": device.vendor,
                    "product": device.product,
                    "serial_known": "yes" if device.serial else "no",
                    "port": device.port_path,
                },
            )
        )
    return atomics


def ethernet_atomic(hal: Hal, ifname: str) -> Atomic | None:
    """Build an atomic for a non-USB Ethernet interface, if one exists.

    The Pi Zero 2W has no onboard Ethernet, so this only fires on hardware that
    is not the base BOM. Kept because the model is N connections, not one.
    """
    mac = hal.net.mac(ifname)
    if not mac:
        return None
    return Atomic(
        id=mac_id(Kind.USB_ETHERNET, mac),
        kind=Kind.USB_ETHERNET,
        label=f"Ethernet ({ifname})",
        present=hal.net.interfaces().get(ifname, False),
        ifname=ifname,
        attributes={"transport": "Ethernet", "mac": mac},
    )
