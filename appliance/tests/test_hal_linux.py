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

from wifucked.hal.linux import _classify_interface, _net_interface_for


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
