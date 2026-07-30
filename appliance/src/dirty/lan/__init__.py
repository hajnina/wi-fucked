"""LAN side — the networks that never go away.

This module *generates configuration* for hostapd and dnsmasq. It does not start,
stop, or restart them: they are independent systemd units and the AP is the
anchor (ADR-011). The daemon may ask hostapd to change channel via CSA; anything
that would bounce the AP is out of bounds.

SSIDs and BSSID are generated once at first boot by the provisioning script and
never change (ADR-012). The functions here are used by that script and by tests;
the running daemon reads them, it does not rewrite them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from dirty.config import LanConfig
from dirty.policy import BEST_EFFORT, CRITICAL, ServiceProfile


@dataclass(frozen=True, slots=True)
class LanIdentity:
    """Derived once, from the Pi serial, and then immutable."""

    critical_ssid: str
    besteffort_ssid: str
    bssid: str
    passphrase: str
    #: Second passphrase, used only in the two-PSK fallback (ADR-014).
    critical_passphrase: str


def derive_identity(serial: str, config: LanConfig) -> LanIdentity:
    """Derive stable LAN identity from the device serial.

    Must be called exactly once per device, at first boot. Calling it again
    yields the same answer — which is the point: a bug that re-derives on every
    boot must not change what clients see.
    """
    seed = hashlib.sha256(f"dirty:{serial}".encode()).hexdigest()
    suffix = seed[:4].upper()

    # Locally administered, unicast: the top byte's low nibble is set to 2 so we
    # cannot collide with a real vendor OUI.
    octets = [0x02] + [int(seed[i : i + 2], 16) for i in range(4, 14, 2)]
    bssid = ":".join(f"{o:02x}" for o in octets)

    return LanIdentity(
        critical_ssid=f"{config.critical_ssid}-{suffix}",
        besteffort_ssid=f"{config.besteffort_ssid}-{suffix}",
        bssid=bssid,
        passphrase=_passphrase(seed, "besteffort"),
        critical_passphrase=_passphrase(seed, "critical"),
    )


def _passphrase(seed: str, salt: str) -> str:
    """A readable 16-character passphrase with real entropy behind it.

    Printed on the device label, so it avoids characters that are ambiguous in
    print — no O/0, no l/1/I.
    """
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    digest = hashlib.sha256(f"{seed}:{salt}".encode()).digest()
    return "".join(alphabet[b % len(alphabet)] for b in digest[:16])


def hostapd_config(identity: LanIdentity, channel: int, mode: str, interface: str = "wlan0") -> str:
    """Render hostapd.conf for the two-BSS or two-PSK layout.

    Which one is chosen depends on what the driver actually supports, probed at
    first boot (ADR-014). Everything above the LAN layer sees VLANs, so the
    choice does not leak upwards.
    """
    base = f"""# Generated at first boot. SSID and BSSID are immutable (ADR-012).
interface={interface}
driver=nl80211
country_code=GB
hw_mode=g
channel={channel}
ieee80211n=1
wmm_enabled=1
auth_algs=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
"""

    if mode == "two_psk":
        return (
            base
            + f"""ssid={identity.besteffort_ssid}
bssid={identity.bssid}
wpa_psk_file=/etc/hostapd/wpa_psk
vlan_file=/etc/hostapd/hostapd.vlan
dynamic_vlan=1
"""
        )

    return (
        base
        + f"""ssid={identity.besteffort_ssid}
bssid={identity.bssid}
wpa_passphrase={identity.passphrase}
vlan_id={BEST_EFFORT.vlan}

bss={interface}_1
ssid={identity.critical_ssid}
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase={identity.critical_passphrase}
vlan_id={CRITICAL.vlan}
"""
    )


def wpa_psk_file(identity: LanIdentity) -> str:
    """Per-PSK VLAN assignment for the two-PSK fallback."""
    return (
        f"vlanid={CRITICAL.vlan} 00:00:00:00:00:00 {identity.critical_passphrase}\n"
        f"vlanid={BEST_EFFORT.vlan} 00:00:00:00:00:00 {identity.passphrase}\n"
    )


def dnsmasq_config(config: LanConfig, profiles: tuple[ServiceProfile, ...]) -> str:
    """DHCP and DNS for each service VLAN, plus captive-portal wildcard DNS."""
    octets = config.address.split(".")
    lines = [
        "# Generated at first boot.",
        "bind-interfaces",
        "domain-needed",
        "bogus-priv",
        # Wildcard: any hostname resolves to us, so the captive portal appears.
        f"address=/#/{config.address}",
        "no-resolv",
        "server=1.1.1.1",
        "server=8.8.8.8",
    ]
    for index, profile in enumerate(profiles):
        third = int(octets[2]) + index
        subnet = f"{octets[0]}.{octets[1]}.{third}"
        lines.append(
            f"dhcp-range=set:{profile.ssid_suffix},{subnet}.50,{subnet}.200,255.255.255.0,12h"
        )
        lines.append(f"dhcp-option=tag:{profile.ssid_suffix},3,{subnet}.1")
    return "\n".join(lines) + "\n"
