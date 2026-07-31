"""Real measurement on hardware — saturation detection and active probing.

The values fed to the allocator come from here. Two things matter most and are
guarded below: a link is only reported *saturated* when the kernel queue says it
was the bottleneck (ADR-003 — otherwise the capacity estimator learns nothing),
and a BACKUP link is *never* actively probed (ADR-003 — we do not spend the
user's backup data measuring it).

Everything here runs without hardware: the mock HAL supplies counters and queue
stats, and ``ping`` is replaced by a stubbed ``_run``.
"""

from __future__ import annotations

import wifucked.probe as probe_module
from wifucked.atomics.model import Atomic, Kind, Mode
from wifucked.clock import VirtualClock
from wifucked.hal.linux import _parse_qdisc_stats
from wifucked.hal.mock import build_mock_hal
from wifucked.probe import LinuxProber, PassiveProber, PingResult, _parse_ping

WLAN = "wlan0"


def _normal(ifname: str = WLAN) -> Atomic:
    return Atomic(
        id="hotel", kind=Kind.WIFI, label="Hotel", mode=Mode.NORMAL, ifname=ifname, present=True
    )


class TestPassiveSaturation:
    def test_first_observation_yields_no_sample(self):
        """Nothing to compare against yet — a rate needs two readings."""
        hal = build_mock_hal()
        prober = PassiveProber(hal, VirtualClock())
        assert prober.observe(_normal()) is None

    def test_no_ifname_yields_no_sample(self):
        hal = build_mock_hal()
        prober = PassiveProber(hal, VirtualClock())
        absent = Atomic(id="x", kind=Kind.WIFI, label="X", mode=Mode.NORMAL, ifname=None)
        assert prober.observe(absent) is None

    def test_backlog_marks_the_window_saturated(self):
        """A queue holding bytes means the link was the bottleneck."""
        hal, clock = build_mock_hal(), VirtualClock()
        prober = PassiveProber(hal, clock)
        atomic = _normal()

        prober.observe(atomic)  # seed
        hal.net.add_traffic(WLAN, rx=1_250_000, tx=125_000)
        hal.net.set_qdisc_stats(WLAN, backlog_bytes=1500, drops=0)
        clock.advance(1)

        obs = prober.observe(atomic)
        assert obs is not None
        assert obs.saturated is True
        assert obs.down_bps == 1_250_000 * 8  # rx is the download direction on WAN
        assert obs.up_bps == 125_000 * 8

    def test_rising_drops_mark_the_window_saturated(self):
        hal, clock = build_mock_hal(), VirtualClock()
        prober = PassiveProber(hal, clock)
        atomic = _normal()

        hal.net.set_qdisc_stats(WLAN, backlog_bytes=0, drops=5)
        prober.observe(atomic)  # seed with drops=5
        hal.net.set_qdisc_stats(WLAN, backlog_bytes=0, drops=9)
        hal.net.add_traffic(WLAN, rx=1000, tx=1000)
        clock.advance(1)

        obs = prober.observe(atomic)
        assert obs is not None and obs.saturated is True

    def test_idle_window_is_not_saturated(self):
        """An unused link is not a slow link — this is why fold() ignores it."""
        hal, clock = build_mock_hal(), VirtualClock()
        prober = PassiveProber(hal, clock)
        atomic = _normal()

        hal.net.set_qdisc_stats(WLAN, backlog_bytes=0, drops=0)
        prober.observe(atomic)
        hal.net.add_traffic(WLAN, rx=1000, tx=1000)
        clock.advance(1)

        obs = prober.observe(atomic)
        assert obs is not None and obs.saturated is False


_CAKE_QDISC = """qdisc cake 8001: root refcnt 2 bandwidth 20Mbit diffserv3
 Sent 5000 bytes 40 pkt (dropped 7, overlimits 3 requeues 0)
 backlog 15140b 10p requeues 0
"""

_IDLE_QDISC = """qdisc cake 8001: root refcnt 2 bandwidth 20Mbit
 Sent 5000 bytes 40 pkt (dropped 0, overlimits 0 requeues 0)
 backlog 0b 0p requeues 0
"""


class TestQdiscParsing:
    def test_reads_backlog_bytes_and_drops(self):
        assert _parse_qdisc_stats(_CAKE_QDISC) == (15140, 7)

    def test_idle_qdisc_is_zero(self):
        assert _parse_qdisc_stats(_IDLE_QDISC) == (0, 0)

    def test_si_suffix_on_backlog_is_scaled(self):
        assert _parse_qdisc_stats("backlog 15Kb 10p\n") == (15000, 0)

    def test_unrecognised_output_is_zero(self):
        assert _parse_qdisc_stats("nothing useful here") == (0, 0)


_GOOD_PING = """PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.
64 bytes from 1.1.1.1: icmp_seq=1 ttl=59 time=12.3 ms
64 bytes from 1.1.1.1: icmp_seq=2 ttl=59 time=13.1 ms
64 bytes from 1.1.1.1: icmp_seq=3 ttl=59 time=14.0 ms

--- 1.1.1.1 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 2003ms
rtt min/avg/max/mdev = 12.300/13.133/14.000/0.698 ms
"""

_TOTAL_LOSS_PING = """PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.

--- 8.8.8.8 ping statistics ---
3 packets transmitted, 0 received, 100% packet loss, time 2043ms
"""


class TestPingParsing:
    def test_good_run_yields_rtt_jitter_and_loss(self):
        result = _parse_ping(_GOOD_PING)
        assert result == PingResult(loss_pct=0.0, rtt_ms=13.133, jitter_ms=0.698)

    def test_total_loss_is_a_result_not_a_parse_failure(self):
        result = _parse_ping(_TOTAL_LOSS_PING)
        assert result == PingResult(loss_pct=100.0, rtt_ms=None, jitter_ms=None)

    def test_unrecognised_output_is_none(self):
        assert _parse_ping("this is not ping output") is None


class TestActiveProbing:
    def _prober_with_ping(self, monkeypatch, output: str) -> LinuxProber:
        calls: list[list[str]] = []

        def fake_run(argv, timeout=10.0):
            calls.append(argv)
            return output

        monkeypatch.setattr(probe_module, "_run", fake_run)
        prober = LinuxProber(build_mock_hal(), VirtualClock())
        prober._calls = calls  # type: ignore[attr-defined]
        return prober

    def _second_sample(self, prober: LinuxProber, atomic: Atomic):
        """Drive two ticks so passive has a delta and active probing runs."""
        prober.observe(atomic)
        prober._hal.net.add_traffic(atomic.ifname, rx=1000, tx=1000)  # type: ignore[arg-type]
        prober._clock.advance(1)  # type: ignore[attr-defined]
        return prober.observe(atomic)

    def test_normal_link_gets_rtt_jitter_loss_and_bloat(self, monkeypatch):
        prober = self._prober_with_ping(monkeypatch, _GOOD_PING)
        obs = self._second_sample(prober, _normal())

        assert obs is not None
        assert obs.rtt_ms == 13.133
        assert obs.jitter_ms == 0.698
        assert obs.loss_pct == 0.0
        assert obs.bloat_ms == 0.0  # first RTT sets the floor, so no added latency

    def test_backup_link_is_never_pinged(self, monkeypatch):
        def explode(argv, timeout=10.0):
            raise AssertionError("active probing must never run on a BACKUP link")

        monkeypatch.setattr(probe_module, "_run", explode)
        prober = LinuxProber(build_mock_hal(), VirtualClock())
        backup = Atomic(
            id="phone",
            kind=Kind.USB_TETHER,
            label="Phone",
            mode=Mode.BACKUP,
            ifname="usb0",
            present=True,
        )
        obs = self._second_sample(prober, backup)

        assert obs is not None
        assert obs.rtt_ms is None and obs.bloat_ms is None

    def test_bloat_is_latency_above_the_best_seen_rtt(self, monkeypatch):
        prober = self._prober_with_ping(monkeypatch, _GOOD_PING)
        atomic = _normal()
        self._second_sample(prober, atomic)  # floor := 13.133

        slow = _GOOD_PING.replace("12.300/13.133/14.000/0.698", "29.0/30.0/31.0/1.0")
        prober._calls.clear()  # type: ignore[attr-defined]

        def fake_run(argv, timeout=10.0):
            return slow

        monkeypatch.setattr(probe_module, "_run", fake_run)
        prober._hal.net.add_traffic(WLAN, rx=1000, tx=1000)
        prober._clock.advance(1)
        obs = prober.observe(atomic)

        assert obs is not None
        assert obs.rtt_ms == 30.0
        assert obs.bloat_ms == 30.0 - 13.133

    def test_falls_through_to_a_second_target_on_total_loss(self, monkeypatch):
        outputs = iter([_TOTAL_LOSS_PING, _GOOD_PING])

        def fake_run(argv, timeout=10.0):
            return next(outputs)

        monkeypatch.setattr(probe_module, "_run", fake_run)
        prober = LinuxProber(build_mock_hal(), VirtualClock())
        obs = self._second_sample(prober, _normal())

        assert obs is not None
        assert obs.rtt_ms == 13.133  # the live target's number, not the dead one's
