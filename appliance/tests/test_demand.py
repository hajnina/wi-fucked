"""Per-class demand derived from LAN VLAN counters.

Guards the two things a reader is most likely to get wrong: which interface each
service class is read from (it must match the hostapd layout via
``lan_ifname_for_profile``), and the direction mapping — on a LAN interface the
bytes it *transmits* are the clients' download, the mirror of the WAN side.

Runs without hardware: the mock HAL supplies the byte counters.
"""

from __future__ import annotations

from dirty.clock import VirtualClock
from dirty.demand import CounterDemand
from dirty.lan import lan_ifname_for_profile
from dirty.policy import BEST_EFFORT, CRITICAL, DEFAULT_PROFILES


def _counter_demand(hal, clock, lan_mode="two_bss") -> CounterDemand:
    return CounterDemand(DEFAULT_PROFILES, hal=hal, clock=clock, lan_mode=lan_mode)


class TestCounterDemand:
    def test_first_sample_reports_zero_for_every_class(self):
        """A rate needs two readings; the first only seeds the baseline."""
        from dirty.hal.mock import build_mock_hal

        demand = _counter_demand(build_mock_hal(), VirtualClock())
        sample = demand.sample()
        assert set(sample) == {CRITICAL.name, BEST_EFFORT.name}
        assert all(d.total_bps == 0 for d in sample.values())

    def test_rate_is_bytes_delta_over_elapsed_time(self):
        from dirty.hal.mock import build_mock_hal

        hal, clock = build_mock_hal(), VirtualClock()
        demand = _counter_demand(hal, clock)
        crit_if = lan_ifname_for_profile(CRITICAL, "two_bss")  # wlan0_1.10

        demand.sample()  # seed
        # tx is the clients' download, rx is their upload (LAN mirrors the WAN).
        hal.net.add_traffic(crit_if, rx=125_000, tx=1_250_000)
        clock.advance(1)

        sample = demand.sample()
        assert sample[CRITICAL.name].down_bps == 1_250_000 * 8
        assert sample[CRITICAL.name].up_bps == 125_000 * 8
        assert sample[BEST_EFFORT.name].total_bps == 0  # untouched interface

    def test_classes_read_from_their_own_interfaces(self):
        from dirty.hal.mock import build_mock_hal

        hal, clock = build_mock_hal(), VirtualClock()
        demand = _counter_demand(hal, clock)

        demand.sample()
        hal.net.add_traffic(lan_ifname_for_profile(BEST_EFFORT, "two_bss"), rx=0, tx=250_000)
        clock.advance(1)

        sample = demand.sample()
        assert sample[BEST_EFFORT.name].down_bps == 250_000 * 8
        assert sample[CRITICAL.name].down_bps == 0

    def test_two_psk_mode_reads_the_shared_bss_interface(self):
        from dirty.hal.mock import build_mock_hal

        hal, clock = build_mock_hal(), VirtualClock()
        demand = _counter_demand(hal, clock, lan_mode="two_psk")
        crit_if = lan_ifname_for_profile(CRITICAL, "two_psk")  # wlan0.10
        assert crit_if == "wlan0.10"

        demand.sample()
        hal.net.add_traffic(crit_if, rx=0, tx=100_000)
        clock.advance(1)

        assert demand.sample()[CRITICAL.name].down_bps == 100_000 * 8

    def test_constrained_is_left_unset(self):
        """Inferring 'capped' from served rate is deliberately not attempted."""
        from dirty.hal.mock import build_mock_hal

        hal, clock = build_mock_hal(), VirtualClock()
        demand = _counter_demand(hal, clock)
        demand.sample()
        hal.net.add_traffic(lan_ifname_for_profile(CRITICAL, "two_bss"), rx=1000, tx=1000)
        clock.advance(1)
        assert demand.sample()[CRITICAL.name].constrained is False
