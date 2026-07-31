"""Stable identity derivation for atomics.

An atomic's identity must survive interface renumbering, USB re-enumeration,
DHCP changes, and the connection vanishing for a month. It therefore derives
from properties of the *connection*, never from a kernel interface name — see
ADR-002.

Every function here is pure and total: given the same observation it returns the
same id, and it never raises. An identity that occasionally fails to derive
would silently split one connection into two.
"""

from __future__ import annotations

import hashlib
import re

from wifucked.atomics.model import Kind

_UNSAFE = re.compile(r"[^a-z0-9]+")


def _slug(value: str, limit: int = 24) -> str:
    slug = _UNSAFE.sub("-", value.strip().lower()).strip("-")
    return slug[:limit] or "unnamed"


def _digest(*parts: str) -> str:
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8", "replace")).hexdigest()[:10]


def wifi_id(ssid: str, bssid: str | None = None) -> str:
    """Identity for a Wi-Fi network.

    Keyed on SSID plus the BSSID's OUI (vendor) prefix rather than the full
    BSSID. A network with several access points roams between BSSIDs — treating
    each as a separate atomic would fragment its learned history — while the OUI
    still distinguishes two different networks that happen to share an SSID.
    """
    oui = ""
    if bssid:
        octets = bssid.lower().replace("-", ":").split(":")
        if len(octets) >= 3:
            oui = ":".join(octets[:3])
    return f"wifi:{_slug(ssid)}:{_digest(ssid, oui)}"


def usb_id(
    kind: Kind,
    vendor: str,
    product: str,
    serial: str | None = None,
    port_path: str | None = None,
) -> str:
    """Identity for a USB-attached connection (tether or Ethernet adapter).

    Serial is the stable key. Devices without one fall back to the physical port
    path, which is stable only while the user keeps using the same port — the
    known weak point called out in ADR-002.
    """
    prefix = "usbtether" if kind is Kind.USB_TETHER else "usbeth"
    if serial:
        return f"{prefix}:{_digest(vendor, product, serial)}"
    return f"{prefix}:{_digest(vendor, product, port_path or 'unknown-port')}"


def mac_id(kind: Kind, mac: str) -> str:
    """Identity for anything best identified by a permanent MAC address.

    Callers must pass the *permanent* address. A randomised or locally
    administered MAC changes and would fragment identity; detecting that is the
    caller's job because only it knows where the address came from.
    """
    return f"{_slug(str(kind))}:{_digest(mac.lower())}"


def modem_id(imei: str) -> str:
    return f"cellular:{_digest(imei)}"


def is_locally_administered(mac: str) -> bool:
    """True for randomised / locally administered MACs, which are not stable."""
    try:
        first = int(mac.split(":")[0], 16)
    except (ValueError, IndexError):
        return False
    return bool(first & 0b10)
