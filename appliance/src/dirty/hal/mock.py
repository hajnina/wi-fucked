"""Mock hardware.

Presents a small, plausible world so the whole control loop runs on a laptop:
two Wi-Fi networks in range and a phone on USB. Scenario tests drive this
directly rather than going through discovery.
"""

from __future__ import annotations

from dirty.hal.base import (
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
from dirty.logging import get_logger

log = get_logger("hal.mock")


class MockWifi(WifiHal):
    def __init__(self) -> None:
        self.networks = [
            ScannedNetwork("Hotel WiFi", "aa:bb:cc:11:22:33", 6, -52),
            ScannedNetwork("Campsite", "dd:ee:ff:44:55:66", 11, -71),
        ]
        self.link: StationLink | None = StationLink("Hotel WiFi", "aa:bb:cc:11:22:33", 6, "wlan0")
        self.caps = RadioCapabilities(
            ap_sta_concurrent=True, multi_bss=True, csa=True, max_ap_interfaces=2
        )

    def scan(self) -> list[ScannedNetwork]:
        return list(self.networks)

    def station_link(self) -> StationLink | None:
        return self.link

    def connect_station(self, ssid: str, passphrase: str | None) -> bool:
        match = next((n for n in self.networks if n.ssid == ssid), None)
        if match is None:
            return False
        self.link = StationLink(match.ssid, match.bssid, match.channel, "wlan0")
        return True

    def disconnect_station(self) -> None:
        self.link = None

    def capabilities(self) -> RadioCapabilities:
        return self.caps


class MockAp(ApHal):
    def __init__(self) -> None:
        self.state = ApStatus(
            running=True,
            channel=6,
            ssids=("Stable_critical", "Stable_besteffort"),
            associated_clients=3,
        )
        #: Scenario tests assert on this — the AP must never drop, so every
        #: move is recorded with the client counts either side of it.
        self.switches: list[tuple[int, int, int, int]] = []
        self.refuse_csa = False

    def status(self) -> ApStatus:
        return self.state

    def channel_switch(self, channel: int) -> bool:
        if self.refuse_csa:
            return False
        before = self.state.channel or 0
        clients = self.state.associated_clients
        self.state = ApStatus(
            running=True,
            channel=channel,
            ssids=self.state.ssids,
            associated_clients=clients,
        )
        self.switches.append((before, channel, clients, clients))
        return True


class MockUsb(UsbHal):
    def __init__(self) -> None:
        self.attached = [
            UsbDevice(
                vendor="05ac",
                product="12a8",
                serial="MOCKPHONE001",
                port_path="1-1",
                ifname="usb0",
                is_tether=True,
                description="Martin's Phone",
            )
        ]

    def devices(self) -> list[UsbDevice]:
        return list(self.attached)


class MockNet(NetHal):
    def __init__(self) -> None:
        self.links = {"wlan0": True, "usb0": True, "ap0": True}
        self._counters: dict[str, tuple[int, int]] = {}
        self._qdisc: dict[str, tuple[int, int]] = {}

    def interfaces(self) -> dict[str, bool]:
        return dict(self.links)

    def counters(self, ifname: str) -> tuple[int, int]:
        return self._counters.get(ifname, (0, 0))

    def add_traffic(self, ifname: str, rx: int, tx: int) -> None:
        have_rx, have_tx = self._counters.get(ifname, (0, 0))
        self._counters[ifname] = (have_rx + rx, have_tx + tx)

    def qdisc_stats(self, ifname: str) -> tuple[int, int]:
        return self._qdisc.get(ifname, (0, 0))

    def set_qdisc_stats(self, ifname: str, backlog_bytes: int, drops: int) -> None:
        """Let a test say the kernel queue is backed up or dropping."""
        self._qdisc[ifname] = (backlog_bytes, drops)

    def mac(self, ifname: str) -> str | None:
        return {"wlan0": "b8:27:eb:00:00:01", "usb0": "b8:27:eb:00:00:02"}.get(ifname)


class MockLed(LedHal):
    def __init__(self) -> None:
        self.pattern = "off"
        self.history: list[str] = []

    def set_pattern(self, pattern: str) -> None:
        if pattern != self.pattern:
            self.pattern = pattern
            self.history.append(pattern)


class MockSystem(SystemHal):
    def __init__(self) -> None:
        self.state = SystemFacts(serial="10000000deadbeef", model="Raspberry Pi Zero 2 W (mock)")

    def facts(self) -> SystemFacts:
        return self.state


def build_mock_hal() -> Hal:
    log.info(
        "Using mock hardware",
        extra={
            "workflow": "hal_init",
            "state": "completed",
            "intent": "run the full control loop without a Raspberry Pi",
            "mocked": True,
        },
    )
    return Hal(
        wifi=MockWifi(),
        ap=MockAp(),
        usb=MockUsb(),
        net=MockNet(),
        led=MockLed(),
        system=MockSystem(),
        mocked=True,
    )
