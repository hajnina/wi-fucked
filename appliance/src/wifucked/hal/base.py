"""Hardware abstraction interfaces.

Everything that touches a radio, netlink, USB enumeration, the LED, or
privileged system state goes through here so that ``MOCK_HW=1`` can replace it.
That is the primary development path, not a testing convenience — a module that
can only run on a Pi cannot be iterated on. See docs/sop/SOP-002.

When you add a capability here, add it to the mock in the same commit. A mock
that lags the interface silently disables testing for everyone else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ScannedNetwork:
    ssid: str
    bssid: str
    channel: int
    signal_dbm: int
    secured: bool = True


@dataclass(frozen=True, slots=True)
class StationLink:
    """The radio's current station (WAN-side Wi-Fi) association."""

    ssid: str
    bssid: str
    channel: int
    ifname: str


@dataclass(frozen=True, slots=True)
class UsbDevice:
    vendor: str
    product: str
    serial: str | None
    port_path: str
    #: Set when the device presents a network interface (tether, USB Ethernet).
    ifname: str | None = None
    #: True for RNDIS/NCM/CDC-ECM style tethering interfaces.
    is_tether: bool = False
    description: str = ""


@dataclass(frozen=True, slots=True)
class ApStatus:
    running: bool
    channel: int | None = None
    ssids: tuple[str, ...] = ()
    associated_clients: int = 0


@dataclass(frozen=True, slots=True)
class RadioCapabilities:
    """What the radio can actually do.

    Probed at first boot rather than assumed — ADR-013 and ADR-014 rest on
    driver behaviour that varies, and the Phase 0 spike exists to measure it.
    """

    ap_sta_concurrent: bool = False
    multi_bss: bool = False
    csa: bool = False
    max_ap_interfaces: int = 1


@dataclass(frozen=True, slots=True)
class SystemFacts:
    serial: str
    model: str
    #: SoC throttling flags. Non-zero means undervoltage or thermal limiting,
    #: which explains a large share of "the appliance is flaky" reports.
    throttled: int = 0
    uptime_s: float = 0.0


class WifiHal(Protocol):
    def scan(self) -> list[ScannedNetwork]: ...
    def station_link(self) -> StationLink | None: ...
    def connect_station(self, ssid: str, passphrase: str | None) -> bool: ...
    def disconnect_station(self) -> None: ...
    def capabilities(self) -> RadioCapabilities: ...


class ApHal(Protocol):
    def status(self) -> ApStatus: ...
    def channel_switch(self, channel: int) -> bool:
        """Move the AP via CSA. Returns False if the driver refused.

        Never restarts hostapd — the AP is the anchor and does not go down for a
        channel change (ADR-011).
        """


class UsbHal(Protocol):
    def devices(self) -> list[UsbDevice]: ...


@dataclass(frozen=True, slots=True)
class DhcpLease:
    """A lease obtained by a bounded DHCP client attempt on one interface."""

    ip: str
    gateway: str | None = None
    lease_seconds: int = 0


class DhcpHal(Protocol):
    """The DHCP-attempt -> passive-listen -> DHCP-server pipeline (ADR-023).

    One port, one pipeline, independently of every other port — see the
    module docstring in ``wifucked.lanout``, which owns sequencing these
    three calls. Each call is bounded by its own ``timeout_s`` and never
    raises; a HAL that cannot determine an answer degrades to the
    conservative outcome at the call site (see ``lanout``'s docstring for
    why "conservative" means "assume a real DHCP server might be there").
    """

    def attempt_client_lease(self, ifname: str, timeout_s: float) -> DhcpLease | None:
        """Try to get a DHCP lease as a client on this interface.

        A lease means an upstream network exists here — the same conclusion
        that already drives USB Ethernet/tether WAN discovery.
        """

    def passive_listen_for_foreign_server(self, ifname: str, timeout_s: float) -> bool:
        """Listen (never transmit) for existing DHCP server traffic on this
        segment — a DHCPOFFER or DHCPACK from something that is not us.

        Returns ``True`` whenever a foreign server was heard *or* the answer
        could not be determined (a failed capture, a missing tool). A false
        "nothing here" is what puts a second, competing DHCP server on a
        network this device doesn't own (ADR-022's Decision section) — this
        call is only ever allowed to be wrong in the safe direction.
        """

    def start_server(self, ifname: str, subnet_third_octet: int, gateway: str) -> bool:
        """Switch this port into DHCP-server mode, handing out ``gateway``'s
        /24 starting at .50. Returns whether it was actually applied.

        Never called unless both of the above already ran and came back
        negative for this interface, this call/pipeline invocation.
        """


class NetHal(Protocol):
    def interfaces(self) -> dict[str, bool]:
        """Interface name to carrier state."""

    def counters(self, ifname: str) -> tuple[int, int]:
        """``(rx_bytes, tx_bytes)`` for an interface."""

    def qdisc_stats(self, ifname: str) -> tuple[int, int]:
        """``(backlog_bytes, dropped_packets)`` for the interface's root qdisc.

        A read-only view of what the kernel queue is doing right now — used by
        ``probe/`` to tell whether a link was actually saturated during the last
        sample window (ADR-003: capacity is observed under load, not
        configured). A nonzero backlog or a rising drop count means the link
        was the bottleneck, so the throughput seen is a real capacity signal.
        """

    def mac(self, ifname: str) -> str | None:
        """Permanent MAC where obtainable, else the current one."""


class LedHal(Protocol):
    def set_pattern(self, pattern: str) -> None:
        """One of: solid, slow, fast, sos, off."""


class SystemHal(Protocol):
    def facts(self) -> SystemFacts: ...


@dataclass
class Hal:
    wifi: WifiHal
    ap: ApHal
    usb: UsbHal
    net: NetHal
    led: LedHal
    system: SystemHal
    dhcp: DhcpHal
    mocked: bool = False
    notes: dict[str, str] = field(default_factory=dict)
