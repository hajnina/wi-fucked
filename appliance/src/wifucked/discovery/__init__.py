"""Discovery — turn physical connectivity into atomics.

The user should never have to think about ``wlan0``, ``usb0``, or ``eth0``.
Discovery translates whatever the hardware reports into human-readable atomics
with stable identities (ADR-002).

Discovery reports what is *present*. It never decides what to use — that is the
user's choice, expressed as a mode, and a newly discovered connection is always
UNUSED until they say otherwise.
"""

from __future__ import annotations

from wifucked.atomics.identity import mac_id, usb_id, wifi_id
from wifucked.atomics.model import Atomic, Health, Kind, Mode
from wifucked.clock import Clock
from wifucked.hal import Hal
from wifucked.logging import get_logger

log = get_logger("discovery")

#: How often the Wi-Fi radio is allowed to actively scan, independent of the
#: medium loop's ~10s cadence. On the Zero 2W's single shared radio an active
#: scan typically has to leave the AP's serving channel briefly, which cuts
#: against ADR-011's "AP is the anchor" guarantee. This is the conservative,
#: clearly-safe mitigation (a slower minimum interval); whether to also skip
#: scanning entirely while the AP has associated clients is a bigger decision
#: left for its own follow-up (see backlog item 10 and PR discussion).
DEFAULT_WIFI_SCAN_MIN_INTERVAL_S = 120.0


def discover(hal: Hal) -> list[Atomic]:
    """One unthrottled discovery sweep. Never raises: a broken source must not
    stop the rest.

    Used directly by tests and anywhere that wants a synchronous snapshot. The
    running daemon should go through `Discoverer` instead, which gates the
    Wi-Fi scan to a slower cadence (see module docstring above).
    """
    return _sweep(hal, wifi=_discover_wifi)


class Discoverer:
    """Stateful discovery: throttles the Wi-Fi scan, leaves USB unthrottled.

    USB enumeration is cheap and local (no radio, no off-channel risk), so it
    still runs on every sweep. The Wi-Fi scan is the one that can disturb the
    AP, so it is gated by wall-clock time rather than loop cadence — moving it
    to the slow loop alone would not be enough if the slow loop's own interval
    ever changes.
    """

    def __init__(
        self,
        clock: Clock,
        wifi_scan_min_interval_s: float = DEFAULT_WIFI_SCAN_MIN_INTERVAL_S,
    ):
        self._clock = clock
        self._wifi_scan_min_interval_s = wifi_scan_min_interval_s
        self._next_wifi_scan = 0.0
        self._last_wifi_atomics: list[Atomic] = []

    def discover(self, hal: Hal) -> list[Atomic]:
        return _sweep(hal, wifi=self._discover_wifi_throttled)

    def _discover_wifi_throttled(self, hal: Hal) -> list[Atomic]:
        now = self._clock.now()
        if now < self._next_wifi_scan and self._last_wifi_atomics:
            return self._last_wifi_atomics

        atomics = _discover_wifi(hal)
        self._last_wifi_atomics = atomics
        self._next_wifi_scan = now + self._wifi_scan_min_interval_s
        log.info(
            "Wi-Fi scan completed",
            extra={
                "workflow": "wan_discovery",
                "state": "completed",
                "intent": "enumerate visible Wi-Fi networks without disturbing the AP",
                "source": "wifi",
                "networks_seen": len(atomics),
                "next_scan_in_s": self._wifi_scan_min_interval_s,
            },
        )
        return atomics


def _sweep(hal: Hal, *, wifi) -> list[Atomic]:
    found: list[Atomic] = []
    for name, source in (
        ("wifi", wifi),
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
        # SSID alone is not enough: two different access points can broadcast
        # the same SSID (e.g. two "hotel-wifi" networks), and BSSID is the
        # only thing that actually tells them apart (ADR-002). Comparing both
        # avoids two distinct networks both claiming to be "connected" and
        # colliding on the same ifname.
        connected = link is not None and link.ssid == network.ssid and link.bssid == network.bssid
        atomics.append(
            Atomic(
                id=wifi_id(network.ssid, network.bssid),
                kind=Kind.WIFI,
                label=network.ssid,
                mode=Mode.UNUSED,
                health=Health.GOOD if connected else Health.UNKNOWN,
                ifname=link.ifname if connected else None,
                present=True,
                ever_connected=connected,
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
                ever_connected=carrier,
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
