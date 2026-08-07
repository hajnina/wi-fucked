"""Real hardware, via Linux userspace tools.

WS-A owns this module. Phase 0 ships the parts that are read-only and cheap to
verify (sysfs, USB enumeration, LED, system facts); the radio control paths are
deliberately unimplemented until the capability spike reports, because their
correct shape depends on what the driver actually does.

See docs/radio-spike.md — do not guess here.
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import ClassVar

from wifucked.hal.base import (
    ApHal,
    ApStatus,
    DhcpHal,
    DhcpLease,
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

#: tmpfs runtime dir (setup_rpi.sh creates /run/wifucked already, ADR-010) —
#: wpa_supplicant's config/control socket/pidfile for the Wi-Fi-as-WAN station
#: link live here, never on the SD card.
_WPA_RUN_DIR = Path("/run/wifucked")
#: How long to wait for association after starting wpa_supplicant before
#: giving up. Chosen to be generous for a slow AP handshake without blocking
#: the caller indefinitely; not measured against real hardware timing.
_STATION_ASSOC_TIMEOUT_S = 20.0


def _wpa_conf_path(ifname: str) -> Path:
    return _WPA_RUN_DIR / f"wpa_supplicant-{ifname}.conf"


def _wpa_pid_path(ifname: str) -> Path:
    return _WPA_RUN_DIR / f"wpa_supplicant-{ifname}.pid"


def _escape_wpa_string(value: str) -> str:
    """Escape a value for a double-quoted wpa_supplicant config string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _wpa_supplicant_conf(ssid: str, passphrase: str | None) -> str:
    """Build a minimal single-network wpa_supplicant config.

    Written directly with Python (not shelled through a template) so an SSID
    or passphrase containing shell metacharacters can never reach a shell —
    only the config file's own quoting rules apply, and those are escaped
    above.
    """
    lines = [f'ssid="{_escape_wpa_string(ssid)}"']
    if passphrase:
        lines.append("key_mgmt=WPA-PSK")
        lines.append(f'psk="{_escape_wpa_string(passphrase)}"')
    else:
        lines.append("key_mgmt=NONE")
    network_block = "\n    ".join(lines)
    return (
        f"ctrl_interface={_WPA_RUN_DIR}/wpa_supplicant\n"
        "update_config=0\n"
        f"network={{\n    {network_block}\n}}\n"
    )


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
        """Scan for networks via ``iw`` directly.

        ``setup_rpi.sh`` marks ``wlan0*`` unmanaged by NetworkManager so
        hostapd can own the radio (ADR-011) — which also means `nmcli` cannot
        drive this interface, so this shells out to ``iw`` the same way
        ``station_link()`` already does. ``iw scan`` typically needs
        `CAP_NET_ADMIN` (this daemon runs as root, same assumption every other
        privileged call in this module makes) and can fail outright while the
        interface is doing AP+STA concurrent duty (radio-spike.md Q1) — an
        empty/failed scan degrades to "no networks found" rather than raising,
        consistent with `_run()`'s never-raises contract.
        """
        out = _run(["iw", "dev", self.ifname, "scan"], timeout=30.0)
        if not out:
            return []
        return _parse_scan_dump(out)

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
        """Join a Wi-Fi network as a station, driving ``wpa_supplicant`` directly.

        `nmcli` cannot be used here for the same reason `scan()` cannot: this
        interface is `unmanaged-devices` for NetworkManager (setup_rpi.sh) so
        hostapd can own the radio undisturbed. There is no simpler `iw`-only
        path for authenticated association — `iw` can join open networks but
        does not speak WPA/WPA2 handshakes, so this writes a scoped
        `wpa_supplicant` config to the tmpfs runtime dir (`/run/wifucked`,
        already created for SD-card-write avoidance, ADR-010), starts a
        detached `wpa_supplicant` bound to this interface only, waits for
        association, then leases an address with `dhclient`.

        JUDGMENT CALL — flagged for hardware review (see PR body and
        docs/active-tests.md): whether `wpa_supplicant`/`dhclient` can run
        against `wlan0` at all *while hostapd also holds wlan0* for the AP
        (SHARED profile, AP+STA concurrency) is exactly the thing
        docs/radio-spike.md Q1 has not yet measured. This method is written
        to be correct for "some interface running station mode against this
        driver," not confirmed for "concurrently with hostapd on this chip."
        """
        start = time.monotonic()
        self.disconnect_station()
        try:
            _WPA_RUN_DIR.mkdir(parents=True, exist_ok=True)
            _wpa_conf_path(self.ifname).write_text(_wpa_supplicant_conf(ssid, passphrase))
        except OSError as exc:
            log.warning(
                "Could not write wpa_supplicant config",
                extra={
                    "workflow": "wan_wifi_connect",
                    "state": "failed",
                    "intent": "join a Wi-Fi network as the WAN station link",
                    "ifname": self.ifname,
                    "ssid": ssid,
                    "reason": "could not write runtime config",
                    "error": str(exc),
                },
            )
            return False

        started = (
            _run(
                [
                    "wpa_supplicant",
                    "-B",
                    "-D",
                    "nl80211",
                    "-i",
                    self.ifname,
                    "-c",
                    str(_wpa_conf_path(self.ifname)),
                    "-P",
                    str(_wpa_pid_path(self.ifname)),
                ],
                timeout=15.0,
            )
            is not None
        )
        associated = started and self._wait_for_association(ssid, timeout=_STATION_ASSOC_TIMEOUT_S)
        leased = associated and (
            _run(["dhclient", "-1", "-timeout", "20", self.ifname], timeout=25.0) is not None
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        if not leased:
            log.warning(
                "Wi-Fi station connect did not complete",
                extra={
                    "workflow": "wan_wifi_connect",
                    "state": "failed",
                    "intent": "join a Wi-Fi network as the WAN station link",
                    "ifname": self.ifname,
                    "ssid": ssid,
                    "wpa_supplicant_started": started,
                    "associated": bool(associated),
                    "duration_ms": duration_ms,
                    "reason": "wpa_supplicant failed to start"
                    if not started
                    else "did not associate within timeout"
                    if not associated
                    else "dhclient did not obtain a lease",
                },
            )
            self.disconnect_station()
            return False
        log.info(
            "Wi-Fi station connected",
            extra={
                "workflow": "wan_wifi_connect",
                "state": "completed",
                "intent": "join a Wi-Fi network as the WAN station link",
                "ifname": self.ifname,
                "ssid": ssid,
                "duration_ms": duration_ms,
            },
        )
        return True

    def _wait_for_association(self, ssid: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            link = self.station_link()
            if link and link.ssid == ssid:
                return True
            time.sleep(1.0)
        return False

    def disconnect_station(self) -> None:
        pid_text = _read(_wpa_pid_path(self.ifname))
        if pid_text and pid_text.isdigit():
            _run(["kill", pid_text])
        else:
            # No pidfile (nothing we started is running, or it raced/crashed
            # before writing one) — best-effort cleanup by matching this
            # interface's own wpa_supplicant invocation. Scoped to "-i
            # <ifname>" so it cannot touch a supplicant instance on another
            # interface.
            _run(["pkill", "-f", f"wpa_supplicant -B -D nl80211 -i {self.ifname} "])
        with contextlib.suppress(FileNotFoundError, OSError):
            _wpa_pid_path(self.ifname).unlink()
        _run(["ip", "addr", "flush", "dev", self.ifname])

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


#: ``iw dev <ifname> scan`` prints one block per BSS, starting with a line
#: like ``BSS aa:bb:cc:dd:ee:ff(on wlan0)`` (sometimes with a trailing
#: ``-- associated`` marker), followed by indented ``key: value`` lines for
#: that BSS until the next ``BSS `` line or end of output.
_SCAN_BSS_RE = re.compile(r"^BSS\s+([0-9a-fA-F:]{17})")


def _parse_scan_dump(out: str) -> list[ScannedNetwork]:
    """Parse ``iw dev <ifname> scan`` output into ``ScannedNetwork`` entries.

    Pure string parsing, unit-tested against a synthetic fixture built from
    ``iw``'s documented output format — see docs/active-tests.md, this does
    not stand in for having run a real scan on the actual chip.
    """
    networks: list[ScannedNetwork] = []
    bssid: str | None = None
    ssid: str | None = None
    freq = 0
    signal_dbm = 0
    secured = False

    def flush() -> None:
        if bssid and ssid:
            networks.append(
                ScannedNetwork(
                    ssid=ssid,
                    bssid=bssid.lower(),
                    channel=_freq_to_channel(freq),
                    signal_dbm=signal_dbm,
                    secured=secured,
                )
            )

    for raw_line in out.splitlines():
        line = raw_line.strip()
        bss_match = _SCAN_BSS_RE.match(line)
        if bss_match:
            flush()
            bssid = bss_match.group(1)
            ssid, freq, signal_dbm, secured = None, 0, 0, False
            continue
        if bssid is None:
            continue
        if line.startswith("freq:"):
            with contextlib.suppress(ValueError, IndexError):
                freq = int(line.split(":", 1)[1].strip().split()[0])
        elif line.startswith("signal:"):
            with contextlib.suppress(ValueError, IndexError):
                # e.g. "signal: -52.00 dBm"
                signal_dbm = int(float(line.split(":", 1)[1].strip().split()[0]))
        elif line.startswith("SSID:"):
            # Hidden networks emit "SSID: " with nothing after the colon;
            # leave ssid as None (falsy) so flush() drops them, same as the
            # old nmcli-based parser did.
            ssid = line.split(":", 1)[1].strip() or None
        elif line.startswith(("RSN:", "WPA:")):
            secured = True
    flush()
    return networks


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


_DNSMASQ_DROPIN_DIR = Path("/etc/dnsmasq.d")


def _safe_ifname(ifname: str) -> str:
    """Filesystem-safe form of an interface name, for a per-port config filename.

    `ifname` is a *current fact*, never identity (ADR-002) — this filename is
    regenerated on every classification run, not read back as persisted state,
    so re-deriving it from a possibly-different `ifname` after re-enumeration
    is exactly the intended behaviour, not a bug.
    """
    return re.sub(r"[^A-Za-z0-9_.-]", "_", ifname)


class LinuxDhcp(DhcpHal):
    """Drives ``dhclient`` (client attempt), ``tcpdump`` (passive listen), and
    a ``dnsmasq`` drop-in (server mode) directly — see ADR-023 for why each
    step exists and why the passive-listen guard fails safe.

    JUDGMENT CALL, flagged for hardware review (see PR body and
    docs/active-tests.md): the exact tool invocations here (reading the
    lease back via ``ip addr``/``ip route`` rather than parsing dhclient's
    own lease-file format, matching ``tcpdump -v``'s decoded text for a
    DHCP reply, a dnsmasq drop-in plus ``systemctl reload``) are written to
    be correct for a Debian-family Linux userspace, not confirmed against
    real hardware — no test in this repo can prove real DHCP wire behaviour
    (SOP-003's real-kernel-proof tier is the only thing that could).
    """

    def attempt_client_lease(self, ifname: str, timeout_s: float) -> DhcpLease | None:
        started = time.monotonic()
        pidfile = f"/run/wifucked-dhclient-{_safe_ifname(ifname)}.pid"
        leasefile = f"/run/wifucked-dhclient-{_safe_ifname(ifname)}.leases"
        ok = (
            _run(
                [
                    "dhclient",
                    "-1",
                    "-timeout",
                    str(max(1, int(timeout_s))),
                    "-pf",
                    pidfile,
                    "-lf",
                    leasefile,
                    ifname,
                ],
                timeout=timeout_s + 5.0,
            )
            is not None
        )
        lease = self._read_lease(ifname) if ok else None
        duration_ms = int((time.monotonic() - started) * 1000)
        log.info(
            "DHCP client lease attempt",
            extra={
                "workflow": "lan_out_dhcp_client_attempt",
                "state": "completed" if lease else "failed",
                "intent": "find out whether an upstream network exists on this port",
                "ifname": ifname,
                "timeout_s": timeout_s,
                "duration_ms": duration_ms,
                "lease_obtained": lease is not None,
                "reason": None if lease else "dhclient did not obtain a usable lease in time",
            },
        )
        return lease

    def _read_lease(self, ifname: str) -> DhcpLease | None:
        out = _run(["ip", "-j", "addr", "show", "dev", ifname])
        ip_addr: str | None = None
        if out:
            try:
                for entry in json.loads(out):
                    for addr in entry.get("addr_info", []):
                        if addr.get("family") == "inet":
                            ip_addr = addr.get("local")
            except json.JSONDecodeError:
                ip_addr = None
        if not ip_addr:
            return None
        gateway = None
        route_out = _run(["ip", "-j", "route", "show", "dev", ifname, "default"])
        if route_out:
            try:
                routes = json.loads(route_out)
                if routes:
                    gateway = routes[0].get("gateway")
            except json.JSONDecodeError:
                gateway = None
        return DhcpLease(ip=ip_addr, gateway=gateway)

    def passive_listen_for_foreign_server(self, ifname: str, timeout_s: float) -> bool:
        started = time.monotonic()
        try:
            done = subprocess.run(
                [
                    "timeout",
                    str(max(1, int(timeout_s))),
                    "tcpdump",
                    "-i",
                    ifname,
                    "-n",
                    "-l",
                    "-v",
                    "udp",
                    "and",
                    "(port",
                    "67",
                    "or",
                    "port",
                    "68)",
                ],
                capture_output=True,
                text=True,
                timeout=timeout_s + 10.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning(
                "Could not run passive DHCP listen; assuming a server may be present",
                extra={
                    "workflow": "lan_out_passive_listen",
                    "state": "failed",
                    "intent": "never put a second DHCP server on a network this device doesn't own",
                    "ifname": ifname,
                    "timeout_s": timeout_s,
                    "reason": "tcpdump could not be run",
                    "error": str(exc),
                },
            )
            # Fail safe: an unverifiable segment is treated the same as a
            # segment where something answered. See ADR-022's Decision
            # section and ADR-023 — a false "become a WAN source" costs
            # nothing, a false "become a DHCP server" is actively harmful.
            return True

        # `tcpdump -v` decodes a BOOTP/DHCP reply as "BOOTP/DHCP, Reply" — a
        # request from a client instead reads "BOOTP/DHCP, Request". Any
        # reply seen on this segment came from something other than us (we
        # are not running a server yet), so it is exactly the foreign-server
        # signal this guard exists to catch. Matching decoded text rather
        # than a stricter packet parse is a deliberate, documented judgement
        # call — see the class docstring — and errs toward over-matching
        # (ambiguous output reads as "heard something"), the safe direction.
        heard = "BOOTP/DHCP, Reply" in (done.stdout + done.stderr)
        duration_ms = int((time.monotonic() - started) * 1000)
        log.info(
            "Passive DHCP listen completed",
            extra={
                "workflow": "lan_out_passive_listen",
                "state": "completed",
                "intent": "confirm nothing already serves DHCP here before becoming a server",
                "ifname": ifname,
                "timeout_s": timeout_s,
                "duration_ms": duration_ms,
                "foreign_server_heard": heard,
            },
        )
        return heard

    def start_server(self, ifname: str, subnet_third_octet: int, gateway: str) -> bool:
        started = time.monotonic()
        subnet = ".".join(gateway.split(".")[:3])
        conf_path = _DNSMASQ_DROPIN_DIR / f"wifucked-lanout-{_safe_ifname(ifname)}.conf"
        conf = (
            "# Generated by wifucked's LAN-out DHCP-server fallback (ADR-023).\n"
            "# ifname is a current fact about this atomic, not its identity (ADR-002) —\n"
            "# this file is regenerated on every classification, never read back as state.\n"
            f"interface={ifname}\n"
            "bind-dynamic\n"
            f"dhcp-range={subnet}.50,{subnet}.200,255.255.255.0,12h\n"
            f"dhcp-option=3,{gateway}\n"
            f"dhcp-option=6,{gateway}\n"
        )
        try:
            _DNSMASQ_DROPIN_DIR.mkdir(parents=True, exist_ok=True)
            conf_path.write_text(conf)
        except OSError as exc:
            log.error(
                "Could not write dnsmasq drop-in for LAN-out port",
                extra={
                    "workflow": "lan_out_server_start",
                    "state": "failed",
                    "intent": "hand out stabilized internet on a bare, quiet wired port",
                    "ifname": ifname,
                    "reason": "could not write config file",
                    "error": str(exc),
                },
            )
            return False

        # Tolerate "File exists" the same idempotent way enforce/ does for
        # `ip rule add` — a re-classification of an already-serving port
        # (e.g. after a daemon restart) must not treat the address already
        # being there as a failure.
        _run(["ip", "addr", "add", f"{gateway}/24", "dev", ifname])
        reloaded = _run(["systemctl", "reload", "dnsmasq.service"]) is not None
        duration_ms = int((time.monotonic() - started) * 1000)
        log.info(
            "LAN-out DHCP server start requested",
            extra={
                "workflow": "lan_out_server_start",
                "state": "completed" if reloaded else "failed",
                "intent": "hand out the same stabilized internet LAN clients get through the AP",
                "ifname": ifname,
                "gateway": gateway,
                "subnet_third_octet": subnet_third_octet,
                "duration_ms": duration_ms,
                "reason": None if reloaded else "dnsmasq reload failed; config file was written",
            },
        )
        return reloaded


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
        dhcp=LinuxDhcp(),
        mocked=False,
        notes={"csa": "unverified until the radio spike reports — docs/radio-spike.md"},
    )
