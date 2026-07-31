"""LAN identity and configuration generation.

The tests that matter here guard ADR-012: SSIDs and BSSID are derived once and
never change. A bug that re-derived on every boot would disconnect every client
device in the house, and would be nearly invisible in development where devices
are reflashed rather than rebooted.
"""

from __future__ import annotations

from wifucked.config import LanConfig
from wifucked.lan import (
    derive_identity,
    dnsmasq_config,
    hostapd_config,
    lan_ifname_for_profile,
    wpa_psk_file,
)
from wifucked.policy import BEST_EFFORT, CRITICAL, DEFAULT_PROFILES

SERIAL = "10000000deadbeef"


class TestIdentityDerivation:
    def test_is_deterministic(self):
        """The property everything else depends on."""
        first = derive_identity(SERIAL, LanConfig())
        second = derive_identity(SERIAL, LanConfig())
        assert first == second

    def test_differs_between_devices(self):
        """Neighbouring appliances must not collide."""
        a = derive_identity("10000000deadbeef", LanConfig())
        b = derive_identity("10000000cafebabe", LanConfig())
        assert a.critical_ssid != b.critical_ssid
        assert a.bssid != b.bssid
        assert a.passphrase != b.passphrase

    def test_bssid_is_locally_administered_unicast(self):
        """Must not collide with a real vendor OUI."""
        identity = derive_identity(SERIAL, LanConfig())
        first = int(identity.bssid.split(":")[0], 16)
        assert first & 0b10, "locally administered bit must be set"
        assert not first & 0b01, "must be unicast, not multicast"

    def test_the_two_ssids_are_distinct(self):
        identity = derive_identity(SERIAL, LanConfig())
        assert identity.critical_ssid != identity.besteffort_ssid

    def test_passphrases_differ_and_are_long_enough_for_wpa2(self):
        identity = derive_identity(SERIAL, LanConfig())
        assert identity.passphrase != identity.critical_passphrase
        for passphrase in (identity.passphrase, identity.critical_passphrase):
            assert 8 <= len(passphrase) <= 63

    def test_passphrase_avoids_characters_that_are_ambiguous_in_print(self):
        """It goes on a label a human retypes from."""
        identity = derive_identity(SERIAL, LanConfig())
        for character in "0O1lI":
            assert character not in identity.passphrase


class TestHostapdConfig:
    def test_two_bss_layout_declares_both_ssids(self):
        identity = derive_identity(SERIAL, LanConfig())
        rendered = hostapd_config(identity, 6, "two_bss")

        assert identity.besteffort_ssid in rendered
        assert identity.critical_ssid in rendered
        assert "bss=wlan0_1" in rendered
        assert f"vlan_id={CRITICAL.vlan}" in rendered
        assert f"vlan_id={BEST_EFFORT.vlan}" in rendered

    def test_two_psk_fallback_uses_one_ssid_and_a_psk_file(self):
        """ADR-014's sanctioned fallback when the driver refuses multi-BSS."""
        identity = derive_identity(SERIAL, LanConfig())
        rendered = hostapd_config(identity, 6, "two_psk")

        assert "bss=" not in rendered
        assert "wpa_psk_file=" in rendered
        assert "dynamic_vlan=1" in rendered

    def test_psk_file_maps_each_passphrase_to_a_vlan(self):
        identity = derive_identity(SERIAL, LanConfig())
        rendered = wpa_psk_file(identity)

        assert f"vlanid={CRITICAL.vlan}" in rendered
        assert f"vlanid={BEST_EFFORT.vlan}" in rendered
        assert identity.critical_passphrase in rendered
        assert identity.passphrase in rendered

    def test_channel_is_honoured(self):
        identity = derive_identity(SERIAL, LanConfig())
        assert "channel=11" in hostapd_config(identity, 11, "two_bss")


class TestLanIfnameForProfile:
    """Guards the mapping enforce/ and demand/ rely on to know which kernel
    interface carries which service class — must track hostapd_config()
    exactly, since a drift here would silently classify traffic wrong.
    """

    def test_two_bss_mode_gives_each_profile_its_own_bss_and_vlan_suffix(self):
        assert lan_ifname_for_profile(BEST_EFFORT, "two_bss") == "wlan0.20"
        assert lan_ifname_for_profile(CRITICAL, "two_bss") == "wlan0_1.10"

    def test_two_psk_mode_shares_one_bss_distinguished_by_vlan_suffix(self):
        assert lan_ifname_for_profile(BEST_EFFORT, "two_psk") == "wlan0.20"
        assert lan_ifname_for_profile(CRITICAL, "two_psk") == "wlan0.10"

    def test_honours_a_non_default_base_interface(self):
        assert lan_ifname_for_profile(BEST_EFFORT, "two_bss", "wlan2") == "wlan2.20"
        assert lan_ifname_for_profile(CRITICAL, "two_bss", "wlan2") == "wlan2_1.10"


class TestDnsmasqConfig:
    def test_serves_a_range_per_service_class(self):
        rendered = dnsmasq_config(LanConfig(), DEFAULT_PROFILES)
        assert rendered.count("dhcp-range=") == len(DEFAULT_PROFILES)

    def test_wildcard_dns_drives_the_captive_portal(self):
        config = LanConfig()
        rendered = dnsmasq_config(config, DEFAULT_PROFILES)
        assert f"address=/#/{config.address}" in rendered

    def test_classes_get_separate_subnets(self):
        rendered = dnsmasq_config(LanConfig(), DEFAULT_PROFILES)
        ranges = [line for line in rendered.splitlines() if "dhcp-range=" in line]
        subnets = {line.split(",")[1].rsplit(".", 1)[0] for line in ranges}
        assert len(subnets) == len(ranges), "each class needs its own subnet"
