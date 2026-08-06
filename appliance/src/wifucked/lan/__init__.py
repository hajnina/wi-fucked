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

from wifucked.config import LanConfig
from wifucked.policy import BEST_EFFORT, CRITICAL, ServiceProfile


@dataclass(frozen=True, slots=True)
class LanIdentity:
    """Derived once, from the Pi serial, and then immutable."""

    #: Broadcast SSID for "single" mode (ADR-020) — the one shipped by default.
    ssid: str
    #: Used only by the "two_bss"/"two_psk" modes (ADR-014, unverified).
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
    seed = hashlib.sha256(f"wifucked:{serial}".encode()).hexdigest()
    suffix = seed[:4].upper()

    # Locally administered, unicast: the top byte's low nibble is set to 2 so we
    # cannot collide with a real vendor OUI.
    octets = [0x02] + [int(seed[i : i + 2], 16) for i in range(4, 14, 2)]
    bssid = ":".join(f"{o:02x}" for o in octets)

    return LanIdentity(
        ssid=f"{config.ssid}-{suffix}",
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
    """Render hostapd.conf for the single-SSID, two-BSS, or two-PSK layout.

    "single" (ADR-020, the current default) is one plain BSS with one
    passphrase — the config with the fewest driver assumptions, chosen because
    the two-class layouts below depend on unverified multi-BSS / dynamic-VLAN
    driver behaviour (ADR-013, ADR-014; see docs/radio-spike.md). Everything
    above the LAN layer sees VLANs (or, in "single" mode, none), so the choice
    does not leak upwards.
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

    if mode == "single":
        return (
            base
            + f"""ssid={identity.ssid}
bssid={identity.bssid}
wpa_passphrase={identity.passphrase}
"""
        )

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


def lan_ifname_for_profile(
    profile: ServiceProfile, lan_mode: str, base_interface: str = "wlan0"
) -> str:
    """The kernel interface a profile's LAN traffic actually arrives on.

    Mirrors the layout ``hostapd_config()`` builds, so the two never drift
    apart.

    In ``"single"`` mode (ADR-020) there is no VLAN split: the only profile
    that exists there is ``BEST_EFFORT`` (``policy.profiles_for_lan_mode``),
    and its traffic arrives straight on ``base_interface`` with no
    subinterface at all.

    In the two-class modes, both set a static per-BSS ``vlan_id`` with no
    ``vlan_file``, and hostapd statically maps that to a
    ``<bss-iface>.<vlan-id>`` subinterface (confirmed against hostapd's own
    VLAN documentation —
    https://wireless.wiki.kernel.org/en/users/documentation/hostapd,
    https://gist.github.com/hkwi/8121425). The two modes differ only in which
    BSS carries which profile: ``two_bss`` gives each profile its own BSS
    (``wlan0`` / ``wlan0_1``); ``two_psk`` puts both on the single base BSS,
    distinguished by which PSK the client used rather than which radio BSS.
    Either way the VLAN suffix always matches ``profile.vlan``.

    Consulted by ``enforce/`` (to mark WAN-bound traffic by LAN origin) and
    ``demand/`` (to read per-class byte counters) so this mapping is defined
    once, here, rather than reconstructed independently in each caller.
    """
    if lan_mode == "single":
        return base_interface
    if lan_mode == "two_psk" or profile is BEST_EFFORT:
        bss = base_interface
    else:
        bss = f"{base_interface}_1"
    return f"{bss}.{profile.vlan}"


def wpa_psk_file(identity: LanIdentity) -> str:
    """Per-PSK VLAN assignment for the two-PSK fallback."""
    return (
        f"vlanid={CRITICAL.vlan} 00:00:00:00:00:00 {identity.critical_passphrase}\n"
        f"vlanid={BEST_EFFORT.vlan} 00:00:00:00:00:00 {identity.passphrase}\n"
    )


def networkd_unit_name(profile: ServiceProfile) -> str:
    """Filename for a profile's static-address unit under ``/etc/systemd/network/``.

    Numbered below dnsmasq/hostapd's implicit ordering concerns only in the
    sense that it sorts predictably next to a future second unit; systemd-networkd
    itself matches by interface name, not load order.
    """
    return f"10-wifucked-{profile.ssid_suffix}.network"


def networkd_config(
    config: LanConfig,
    profile: ServiceProfile,
    profiles: tuple[ServiceProfile, ...],
    lan_mode: str,
    base_interface: str = "wlan0",
) -> str:
    """Static gateway address for one profile's VLAN subinterface.

    hostapd creates ``wlan0.<vlan>`` / ``wlan0_1.<vlan>`` itself (static
    ``vlan_id`` per BSS); nothing else in this system assigns those
    interfaces an address, so without this a client gets a DHCP lease
    pointing at a gateway that does not exist. Matched by interface name
    (``[Match] Name=``) rather than started/ordered against hostapd, so it
    applies whenever the interface appears — independent of the daemon,
    same as hostapd and dnsmasq themselves (ADR-011).
    """
    index = profiles.index(profile)
    octets = config.address.split(".")
    third = int(octets[2]) + index
    address = f"{octets[0]}.{octets[1]}.{third}.1"
    ifname = lan_ifname_for_profile(profile, lan_mode, base_interface)
    return f"""# Generated at first boot. Static gateway address for the {profile.name}
# VLAN subinterface (ADR-011: independent of the daemon, matched by name).
[Match]
Name={ifname}

[Network]
Address={address}/{config.prefix}
DHCP=no
IPv6AcceptRA=no
LinkLocalAddressing=no
ConfigureWithoutCarrier=yes
"""


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
