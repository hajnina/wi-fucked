"""Real hardware, via Linux userspace tools.

WS-A owns this module. Phase 0 ships the parts that are read-only and cheap to
verify (sysfs, USB enumeration, LED, system facts); the radio control paths are
deliberately unimplemented until the capability spike reports, because their
correct shape depends on what the driver actually does.

See docs/radio-spike.md — do not guess here.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
from pathlib import Path
from typing import ClassVar

from wifucked.hal.base import (
    ApHal,
    ApStatus,
    Hal,
    LedHal,
    NetHal,
    RadioCapabilities,
    ScannedNetwork,
    StationLink,
    SystemFacts,
    SystemHal,
    UsbDevice,
    UsbHal,
    WifiHal,
)
from wifucked.logging import get_logger

log = get_logger("hal.linux")

_SYS_NET = Path("/sys/class/net")
_SYS_USB = Path("/sys/bus/usb/devices")
_LED = Path("/sys/class/leds/ACT")


def _run(argv: list[str], timeout: float = 10.0) -> str | None:
    """Run a command, returning stdout or None. Never raises."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning(
            "Command failed to execute",
            extra={
                "workflow": "hal_command",
                "state": "failed",
                "intent": " ".join(argv[:2]),
                "argv": argv,
                "reason": "could not spawn process",
                "error": str(exc),
            },
        )
        return None
    if done.returncode != 0:
        log.warning(
            "Command returned non-zero",
            extra={
                "workflow": "hal_command",
                "state": "failed",
                "intent": " ".join(argv[:2]),
                "argv": argv,
                "returncode": done.returncode,
                "reason": (done.stderr or "").strip()[:200],
            },
        )
        return None
    return done.stdout


class LinuxWifi(WifiHal):
    def __init__(self, ifname: str = "wlan0"):
        self.ifname = ifname

    def scan(self) -> list[ScannedNetwork]:
        out = _run(["nmcli", "-t", "-f", "SSID,BSSID,CHAN,SIGNAL,SECURITY", "dev", "wifi"])
        if not out:
            return []
        networks: list[ScannedNetwork] = []
        for line in out.splitlines():
            # nmcli escapes the colons inside a BSSID as '\:'
            fields = line.replace("\\:", "\x00").split(":")
            if len(fields) < 5:
                continue
            ssid, bssid, chan, signal, security = (f.replace("\x00", ":") for f in fields[:5])
            if not ssid:
                continue
            try:
                networks.append(
                    ScannedNetwork(
                        ssid=ssid,
                        bssid=bssid,
                        channel=int(chan),
                        # nmcli reports 0-100 quality; map to a rough dBm.
                        signal_dbm=int(signal) // 2 - 100,
                        secured=bool(security.strip()),
                    )
                )
            except ValueError:
                continue
        return networks

    def station_link(self) -> StationLink | None:
        out = _run(["iw", "dev", self.ifname, "link"])
        if not out or "Not connected" in out:
            return None
        ssid = bssid = None
        channel = 0
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Connected to"):
                bssid = line.split()[2]
            elif line.startswith("SSID:"):
                ssid = line.split(":", 1)[1].strip()
            elif line.startswith("freq:"):
                channel = _freq_to_channel(int(line.split(":", 1)[1].strip()))
        if not ssid or not bssid:
            return None
        return StationLink(ssid, bssid, channel, self.ifname)

    def connect_station(self, ssid: str, passphrase: str | None) -> bool:
        argv = ["nmcli", "dev", "wifi", "connect", ssid]
        if passphrase:
            argv += ["password", passphrase]
        return _run(argv, timeout=45.0) is not None

    def disconnect_station(self) -> None:
        _run(["nmcli", "dev", "disconnect", self.ifname])

    def capabilities(self) -> RadioCapabilities:
        """Probe what the radio can do.

        Parsing 'valid interface combinations' properly is spike work — until
        then this reports the conservative answer rather than a hopeful one, so
        nothing downstream assumes a capability that was never measured.
        """
        out = _run(["iw", "phy"]) or ""
        return RadioCapabilities(
            ap_sta_concurrent="{ managed, AP }" in out or "AP, managed" in out,
            multi_bss="#{ AP } <= 2" in out or "AP } <= 2" in out,
            csa=False,  # unverified; see docs/radio-spike.md Q3
            max_ap_interfaces=1,
        )


#: ``tc -s`` prints a backlog figure with an optional SI-ish suffix, e.g.
#: ``backlog 0b 0p`` or ``backlog 15Kb 10p``. Read the root qdisc's line only —
#: it is the first block ``tc`` emits, before any class children.
_BACKLOG_RE = re.compile(r"backlog\s+(\d+)([KMG]?)b")
_DROPPED_RE = re.compile(r"dropped\s+(\d+)")
_SI_MULTIPLIER = {"": 1, "K": 1000, "M": 1_000_000, "G": 1_000_000_000}


def _parse_qdisc_stats(out: str) -> tuple[int, int]:
    """Extract ``(backlog_bytes, dropped_packets)`` from ``tc -s qdisc`` output.

    Takes the first match of each — the root qdisc is emitted first, and its
    aggregate counters are the ones that describe the interface as a whole.
    """
    backlog_bytes = 0
    backlog_m = _BACKLOG_RE.search(out)
    if backlog_m:
        backlog_bytes = int(backlog_m.group(1)) * _SI_MULTIPLIER[backlog_m.group(2)]
    drops = 0
    drops_m = _DROPPED_RE.search(out)
    if drops_m:
        drops = int(drops_m.group(1))
    return backlog_bytes, drops


def _freq_to_channel(mhz: int) -> int:
    if 2412 <= mhz <= 2472:
        return (mhz - 2412) // 5 + 1
    if mhz == 2484:
        return 14
    return 0


class LinuxAp(ApHal):
    def status(self) -> ApStatus:
        out = _run(["hostapd_cli", "status"])
        if not out:
            return ApStatus(running=False)
        values: dict[str, str] = {}
        for line in out.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        stations = _run(["hostapd_cli", "list_sta"]) or ""
        return ApStatus(
            running=values.get("state") == "ENABLED",
            channel=int(values["channel"]) if values.get("channel", "").isdigit() else None,
            ssids=tuple(v for k, v in values.items() if k.startswith("ssid")),
            associated_clients=len([s for s in stations.splitlines() if s.strip()]),
        )

    def channel_switch(self, channel: int) -> bool:
        freq = 2407 + channel * 5 if channel != 14 else 2484
        before = self.status()
        ok = _run(["hostapd_cli", "chan_switch", "5", str(freq)]) is not None
        after = self.status()
        log.info(
            "AP channel switch requested",
            extra={
                "workflow": "csa_move",
                "state": "completed" if ok else "failed",
                "intent": "keep AP and station on one channel in SHARED profile",
                "channel_from": before.channel,
                "channel_to": channel,
                "clients_before": before.associated_clients,
                "clients_after": after.associated_clients,
                "reason": None if ok else "hostapd_cli refused the switch",
            },
        )
        return ok


class LinuxUsb(UsbHal):
    def devices(self) -> list[UsbDevice]:
        found: list[UsbDevice] = []
        if not _SYS_USB.exists():
            return found
        for entry in sorted(_SYS_USB.iterdir()):
            vendor = _read(entry / "idVendor")
            product = _read(entry / "idProduct")
            if not vendor or not product:
                continue
            ifname, is_tether = _net_interface_for(entry)
            found.append(
                UsbDevice(
                    vendor=vendor,
                    product=product,
                    serial=_read(entry / "serial"),
                    port_path=entry.name,
                    ifname=ifname,
                    is_tether=is_tether,
                    description=_read(entry / "product") or "USB device",
                )
            )
        return found


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


#: RNDIS is what Android/Windows Mobile tethering presents: a vendor-specific
#: "Wireless Controller" interface class rather than a CDC Ethernet subclass.
#: This is how the kernel's own rndis_host driver decides what to bind to.
_RNDIS_CLASS = "e0"
_RNDIS_SUBCLASS = "01"
_RNDIS_PROTOCOL = "03"

#: CDC control-interface subclasses that mean "this is an Ethernet adapter",
#: per the USB CDC spec: Ethernet Networking Control Model (ECM), Ethernet
#: Emulation Model (EEM), Network Control Model (NCM). iPhone tethering also
#: uses NCM, which is why NCM alone cannot fully distinguish phone from
#: dongle — see the fallback note below.
_CDC_CLASS = "02"
_CDC_ETHERNET_SUBCLASSES = {"06", "0c", "0d"}


def _classify_interface(iface_dir: Path) -> bool | None:
    """Best-effort tether-vs-adapter call from the USB interface descriptor.

    Returns True (tether), False (Ethernet adapter), or None when the
    descriptor doesn't match a known pattern, in which case the caller must
    fall back rather than guess silently.
    """
    usb_class = _read(iface_dir / "bInterfaceClass")
    subclass = _read(iface_dir / "bInterfaceSubClass")
    protocol = _read(iface_dir / "bInterfaceProtocol")
    if usb_class == _RNDIS_CLASS and subclass == _RNDIS_SUBCLASS and protocol == _RNDIS_PROTOCOL:
        return True
    if usb_class == _CDC_CLASS and subclass in _CDC_ETHERNET_SUBCLASSES:
        return False
    return None


def _net_interface_for(usb_entry: Path) -> tuple[str | None, bool]:
    """Find the network interface a USB device exposes, if any, and classify it.

    Classification reads the USB interface descriptor (class/subclass/protocol)
    rather than assuming every USB netdev is a phone. RNDIS — what Android and
    Windows Mobile tethering present — is a distinct, vendor-specific interface
    class from CDC Ethernet (ECM/EEM/NCM), which is what USB Ethernet dongles
    use. iPhone tethering also uses NCM, so a bare NCM interface is genuinely
    ambiguous between "iPhone tether" and "USB Ethernet dongle"; that case (and
    any interface class this function has not seen before) falls back to
    treating it as a tether, since a false "backup, ask before use" costs
    nothing while silently treating a phone as a free wired uplink would spend
    someone's data plan.
    """
    for candidate in usb_entry.rglob("net/*"):
        if candidate.is_dir():
            # sysfs lays this out as <interface-dir>/net/<ifname>, so the
            # descriptor files live two levels up from the netdev, not one.
            iface_dir = candidate.parent.parent
            classified = _classify_interface(iface_dir)
            if classified is None:
                log.info(
                    "USB network interface has an unrecognised descriptor; defaulting to tether",
                    extra={
                        "workflow": "usb_classification",
                        "state": "completed",
                        "intent": "distinguish phone tethering from a USB Ethernet adapter",
                        "ifname": candidate.name,
                        "usb_class": _read(iface_dir / "bInterfaceClass"),
                        "usb_subclass": _read(iface_dir / "bInterfaceSubClass"),
                        "reason": "no known RNDIS/CDC-Ethernet pattern matched; "
                        "defaulting to the safer (metered) assumption",
                    },
                )
                classified = True
            return candidate.name, classified
    return None, False


class LinuxNet(NetHal):
    def interfaces(self) -> dict[str, bool]:
        result: dict[str, bool] = {}
        if not _SYS_NET.exists():
            return result
        for entry in _SYS_NET.iterdir():
            result[entry.name] = _read(entry / "carrier") == "1"
        return result

    def counters(self, ifname: str) -> tuple[int, int]:
        base = _SYS_NET / ifname / "statistics"
        rx = _read(base / "rx_bytes") or "0"
        tx = _read(base / "tx_bytes") or "0"
        try:
            return int(rx), int(tx)
        except ValueError:
            return 0, 0

    def qdisc_stats(self, ifname: str) -> tuple[int, int]:
        """Root-qdisc backlog and drop count from ``tc -s qdisc show``.

        This shells out to ``tc``, which SOP-002 reserves for ``enforce/``. That
        rule is about *mutating* kernel state — installing and reconciling
        qdiscs/rules — which must flow through one module. This is a read-only
        query of a statistic, in the same family as the ``iw``/``ethtool``/sysfs
        reads this HAL already performs, so it belongs here rather than being a
        stray ``subprocess`` call inside ``probe/``.
        """
        out = _run(["tc", "-s", "qdisc", "show", "dev", ifname])
        if not out:
            return 0, 0
        return _parse_qdisc_stats(out)

    def mac(self, ifname: str) -> str | None:
        # Prefer the permanent address: a randomised MAC is not stable identity.
        out = _run(["ethtool", "-P", ifname])
        if out and ":" in out:
            return out.strip().split()[-1]
        return _read(_SYS_NET / ifname / "address")


class LinuxLed(LedHal):
    """The onboard ACT LED — the only status channel on a headless device."""

    _TIMINGS: ClassVar[dict[str, tuple[int, int]]] = {
        "solid": (1, 0),
        "slow": (900, 900),
        "fast": (150, 150),
        "sos": (100, 400),
        "off": (0, 1),
    }

    def __init__(self) -> None:
        self._current: str | None = None
        self._available = _LED.exists()
        if self._available:
            self._write("trigger", "timer")

    def set_pattern(self, pattern: str) -> None:
        if pattern == self._current or not self._available:
            return
        on, off = self._TIMINGS.get(pattern, self._TIMINGS["off"])
        self._write("delay_on", str(on))
        self._write("delay_off", str(off))
        self._current = pattern

    def _write(self, name: str, value: str) -> None:
        try:
            (_LED / name).write_text(value)
        except OSError:
            self._available = False


class LinuxSystem(SystemHal):
    def facts(self) -> SystemFacts:
        serial = "unknown"
        model = "unknown"
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("Serial"):
                    serial = line.split(":", 1)[1].strip()
                elif line.startswith("Model"):
                    model = line.split(":", 1)[1].strip()
        except OSError:
            pass

        throttled = 0
        out = _run(["vcgencmd", "get_throttled"])
        if out and "=" in out:
            try:
                throttled = int(out.split("=", 1)[1].strip(), 16)
            except ValueError:
                throttled = 0

        uptime = 0.0
        with contextlib.suppress(OSError, ValueError, IndexError):
            uptime = float(Path("/proc/uptime").read_text().split()[0])

        return SystemFacts(serial=serial, model=model, throttled=throttled, uptime_s=uptime)


def build_linux_hal() -> Hal:
    log.info(
        "Using real hardware",
        extra={
            "workflow": "hal_init",
            "state": "completed",
            "intent": "drive the radio, USB and LED on a Raspberry Pi",
            "mocked": False,
        },
    )
    return Hal(
        wifi=LinuxWifi(),
        ap=LinuxAp(),
        usb=LinuxUsb(),
        net=LinuxNet(),
        led=LinuxLed(),
        system=LinuxSystem(),
        mocked=False,
        notes={"csa": "unverified until the radio spike reports — docs/radio-spike.md"},
    )
