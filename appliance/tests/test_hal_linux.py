"""USB tether/Ethernet-adapter classification from real sysfs descriptors.

Discovery used to treat any USB-attached netdev as a phone tether — correct
enough to ship, but it meant a USB Ethernet dongle was silently classified
the same as a phone, and a scenario deciding whether to spend someone's data
plan had no way to know which one it was actually looking at. This reads the
USB interface class/subclass/protocol instead, which is how the kernel itself
decides whether to bind rndis_host (tether) or cdc_ether/cdc_ncm (adapter).
"""

from __future__ import annotations

from pathlib import Path

from wifucked.hal.linux import _classify_interface, _net_interface_for, _parse_scan_dump


def _write_descriptor(iface_dir: Path, usb_class: str, subclass: str, protocol: str = "00") -> None:
    iface_dir.mkdir(parents=True, exist_ok=True)
    (iface_dir / "bInterfaceClass").write_text(usb_class)
    (iface_dir / "bInterfaceSubClass").write_text(subclass)
    (iface_dir / "bInterfaceProtocol").write_text(protocol)


class TestClassifyInterface:
    def test_rndis_is_a_tether(self, tmp_path: Path):
        _write_descriptor(tmp_path, usb_class="e0", subclass="01", protocol="03")
        assert _classify_interface(tmp_path) is True

    def test_cdc_ecm_is_an_adapter(self, tmp_path: Path):
        _write_descriptor(tmp_path, usb_class="02", subclass="06")
        assert _classify_interface(tmp_path) is False

    def test_cdc_ncm_is_an_adapter(self, tmp_path: Path):
        _write_descriptor(tmp_path, usb_class="02", subclass="0d")
        assert _classify_interface(tmp_path) is False

    def test_unknown_descriptor_is_ambiguous(self, tmp_path: Path):
        _write_descriptor(tmp_path, usb_class="ff", subclass="ff")
        assert _classify_interface(tmp_path) is None

    def test_missing_descriptor_files_are_ambiguous_not_a_crash(self, tmp_path: Path):
        assert _classify_interface(tmp_path) is None


class TestNetInterfaceFor:
    def test_finds_rndis_tether(self, tmp_path: Path):
        iface = tmp_path / "1-1:1.0"
        _write_descriptor(iface, usb_class="e0", subclass="01", protocol="03")
        (iface / "net" / "usb0").mkdir(parents=True)

        ifname, is_tether = _net_interface_for(tmp_path)

        assert ifname == "usb0"
        assert is_tether is True

    def test_finds_cdc_ethernet_adapter(self, tmp_path: Path):
        iface = tmp_path / "1-2:1.0"
        _write_descriptor(iface, usb_class="02", subclass="06")
        (iface / "net" / "usb1").mkdir(parents=True)

        ifname, is_tether = _net_interface_for(tmp_path)

        assert ifname == "usb1"
        assert is_tether is False

    def test_unrecognised_descriptor_defaults_to_tether(self, tmp_path: Path):
        """Ambiguous (e.g. bare NCM shared by iPhone tethering and some
        dongles, or a class this function has never seen) must fail toward
        treating it as metered, not toward spending a stranger's data."""
        iface = tmp_path / "1-3:1.0"
        _write_descriptor(iface, usb_class="ff", subclass="ff")
        (iface / "net" / "usb2").mkdir(parents=True)

        ifname, is_tether = _net_interface_for(tmp_path)

        assert ifname == "usb2"
        assert is_tether is True

    def test_no_net_directory_means_not_a_network_device(self, tmp_path: Path):
        (tmp_path / "idVendor").write_text("0bda")

        ifname, is_tether = _net_interface_for(tmp_path)

        assert ifname is None
        assert is_tether is False


# ``iw dev <ifname> scan`` output, as documented by ``iw``'s own scan.c
# formatting and widely-observed real dumps. This is a hand-built fixture,
# not a capture from a running Pi Zero 2W — see docs/active-tests.md, the
# parser logic is what's confirmed here, not driver behaviour.
_SAMPLE_SCAN_DUMP = """\
BSS aa:bb:cc:11:22:33(on wlan0) -- associated
\tTSF: 123456789 usec (1d, 10:17:36)
\tfreq: 2437
\tbeacon interval: 100 TUs
\tcapability: ESS Privacy ShortSlotTime (0x0411)
\tsignal: -52.00 dBm
\tlast seen: 120 ms ago
\tSSID: Hotel WiFi
\tSupported rates: 1.0* 2.0* 5.5* 11.0* 18.0 24.0 36.0 54.0
\tRSN:\t * Version: 1
\t\t * Group cipher: CCMP
\t\t * Pairwise ciphers: CCMP
\t\t * Authentication suites: PSK
\t\t * Capabilities: (0x0000)
BSS dd:ee:ff:44:55:66(on wlan0)
\tTSF: 987654321 usec (2d, 03:02:11)
\tfreq: 2462
\tbeacon interval: 100 TUs
\tcapability: ESS ShortSlotTime (0x0401)
\tsignal: -71.00 dBm
\tlast seen: 340 ms ago
\tSSID: Campsite
\tSupported rates: 1.0* 2.0* 5.5* 11.0* 18.0 24.0 36.0 54.0
BSS 11:22:33:44:55:66(on wlan0)
\tfreq: 2412
\tsignal: -80.00 dBm
\tlast seen: 500 ms ago
\tSSID:\x20
"""


class TestParseScanDump:
    def test_parses_multiple_bss_blocks(self):
        networks = _parse_scan_dump(_SAMPLE_SCAN_DUMP)

        by_ssid = {n.ssid: n for n in networks}
        assert set(by_ssid) == {"Hotel WiFi", "Campsite"}

    def test_secured_network_has_rsn_block(self):
        networks = _parse_scan_dump(_SAMPLE_SCAN_DUMP)
        hotel = next(n for n in networks if n.ssid == "Hotel WiFi")

        assert hotel.bssid == "aa:bb:cc:11:22:33"
        assert hotel.channel == 6
        assert hotel.signal_dbm == -52
        assert hotel.secured is True

    def test_open_network_without_rsn_or_wpa_is_unsecured(self):
        networks = _parse_scan_dump(_SAMPLE_SCAN_DUMP)
        campsite = next(n for n in networks if n.ssid == "Campsite")

        assert campsite.bssid == "dd:ee:ff:44:55:66"
        assert campsite.channel == 11
        assert campsite.signal_dbm == -71
        assert campsite.secured is False

    def test_hidden_ssid_is_dropped(self):
        networks = _parse_scan_dump(_SAMPLE_SCAN_DUMP)

        assert all(n.bssid != "11:22:33:44:55:66" for n in networks)

    def test_empty_output_returns_no_networks(self):
        assert _parse_scan_dump("") == []

    def test_no_bss_lines_returns_no_networks(self):
        assert _parse_scan_dump("command line reported nothing useful\n") == []
