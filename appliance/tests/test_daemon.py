"""Daemon wiring, the API surface, and the safety properties.

The tests here guard the rules that are easiest to break by accident: never
tearing down kernel state, never letting the AP depend on the daemon, and never
probing a BACKUP link.
"""

from __future__ import annotations

import dataclasses

import pytest

from dirty.atomics.model import Atomic, Capacity, Kind, Mode
from dirty.capacity import HistoricalHint, blend
from dirty.clock import VirtualClock
from dirty.config import Config, load
from dirty.daemon import Daemon
from dirty.demand import CounterDemand, StaticDemand
from dirty.enforce import LinuxEnforcer, MockEnforcer
from dirty.hal import build_hal
from dirty.probe import (
    CONFIDENCE_HALF_LIFE_S,
    LinuxProber,
    Observation,
    ScriptedProber,
    decay_confidence,
    fold,
    may_probe_actively,
)
from dirty.telemetry import Telemetry
from dirty.tunnel import MockTunnel, WireGuardTunnel, fabric_compatible, version_tuple


@pytest.fixture
def daemon() -> Daemon:
    clock = VirtualClock()
    return Daemon(
        Config(),
        hal=build_hal(force_mock=True),
        clock=clock,
        telemetry=Telemetry(clock, None),
        persist=False,
    )


class TestDaemonLifecycle:
    def test_starts_and_discovers(self, daemon):
        daemon.start()
        assert daemon.registry.all(), "the mock world should yield atomics"

    def test_ticks_without_raising(self, daemon):
        daemon.start()
        for _ in range(30):
            daemon.tick()
            daemon.clock.advance(1)

    def test_stop_does_not_tear_down_the_data_plane(self, daemon):
        """ADR-008: a control-plane crash must not become an outage.

        If someone adds a cleanup path, this fails — which is the point.
        """
        daemon.start()
        daemon.tick()
        before = daemon.enforcer.actual()

        daemon.stop()

        assert daemon.enforcer.actual() == before, (
            "stopping the daemon must leave kernel state in place"
        )

    def test_snapshot_is_serialisable(self, daemon):
        import json

        daemon.start()
        daemon.tick()
        json.dumps(daemon.state_snapshot())


class TestRealHardwareSelection:
    """`self.hal.mocked` is the single switch between the fake and real worlds.

    Every workstream shipped a real implementation (LinuxEnforcer, LinuxProber,
    CounterDemand, WireGuardTunnel) that was previously dead code — nothing
    constructed them. These tests guard that the daemon actually reaches for
    them once there is real hardware to back them, and keeps using the fakes
    under MOCK_HW=1 exactly as before.
    """

    def _real_hal(self):
        # A Hal whose backends are the harmless mocks, but flagged unmocked —
        # exercises the daemon's *selection* logic without needing a real Pi or
        # letting any real subprocess call happen.
        return dataclasses.replace(build_hal(force_mock=True), mocked=False)

    def test_mocked_hal_selects_the_fakes(self):
        clock = VirtualClock()
        daemon = Daemon(
            Config(),
            hal=build_hal(force_mock=True),
            clock=clock,
            telemetry=Telemetry(clock, None),
            persist=False,
        )
        assert isinstance(daemon.enforcer, MockEnforcer)
        assert isinstance(daemon.prober, ScriptedProber)
        assert isinstance(daemon.demand, StaticDemand)
        assert isinstance(daemon.tunnel, MockTunnel)

    def test_unmocked_hal_selects_the_real_implementations(self):
        clock = VirtualClock()
        daemon = Daemon(
            Config(),
            hal=self._real_hal(),
            clock=clock,
            telemetry=Telemetry(clock, None),
            persist=False,
        )
        assert isinstance(daemon.enforcer, LinuxEnforcer)
        assert isinstance(daemon.prober, LinuxProber)
        assert isinstance(daemon.demand, CounterDemand)
        assert isinstance(daemon.tunnel, WireGuardTunnel)

    def test_explicit_override_always_wins_over_the_hal_switch(self):
        """A caller-supplied instance is never second-guessed, mocked HAL or not."""
        clock = VirtualClock()
        explicit = MockEnforcer()
        daemon = Daemon(
            Config(),
            hal=self._real_hal(),
            clock=clock,
            telemetry=Telemetry(clock, None),
            persist=False,
            enforcer=explicit,
        )
        assert daemon.enforcer is explicit

    def test_no_fabric_server_configured_skips_attach_without_raising(self):
        clock = VirtualClock()
        daemon = Daemon(
            Config(),
            hal=self._real_hal(),
            clock=clock,
            telemetry=Telemetry(clock, None),
            persist=False,
        )
        daemon.start()  # config.fabric.servers is empty by default
        assert daemon.tunnel.status().server is None

    def test_attach_is_tried_at_most_once_per_process(self, monkeypatch):
        clock = VirtualClock()
        config = Config()
        config.fabric.servers = ["https://fabric.invalid"]
        daemon = Daemon(
            config,
            hal=self._real_hal(),
            clock=clock,
            telemetry=Telemetry(clock, None),
            persist=False,
        )
        calls = []
        monkeypatch.setattr(daemon.tunnel, "attach", lambda *a, **k: calls.append(1) or False)
        daemon.start()
        daemon.start()
        assert len(calls) == 1


class TestApNeverDepends:
    def test_ap_stays_up_across_a_daemon_restart(self, daemon):
        """The AP is the anchor (ADR-011)."""
        daemon.start()
        daemon.tick()
        assert daemon.hal.ap.status().running

        daemon.stop()
        assert daemon.hal.ap.status().running, "the AP must not stop with the daemon"

    def test_led_reports_no_wan_when_nothing_is_permitted(self, daemon):
        daemon.start()
        daemon.tick()
        assert daemon.hal.led.pattern in ("fast", "solid", "slow")


class TestProbeSafety:
    def test_never_actively_probes_a_backup_link(self):
        """ADR-003. Spending metered data to measure it defeats the point."""
        backup = Atomic(id="b", kind=Kind.USB_TETHER, label="Phone", mode=Mode.BACKUP)
        assert may_probe_actively(backup) is False

    def test_never_actively_probes_an_unused_link(self):
        unused = Atomic(id="u", kind=Kind.WIFI, label="Random", mode=Mode.UNUSED)
        assert may_probe_actively(unused) is False

    def test_permits_active_probing_of_normal_links(self):
        normal = Atomic(id="n", kind=Kind.WIFI, label="Hotel", mode=Mode.NORMAL)
        assert may_probe_actively(normal) is True


class TestCapacityEstimation:
    def test_idle_observation_does_not_raise_the_estimate(self):
        """An unused link is not a slow link."""
        capacity = Capacity(10_000_000, 2_000_000, 0.9, measured_at=0)
        observation = Observation("a", down_bps=100, up_bps=50, saturated=False)

        folded = fold(capacity, observation, now=1)
        assert folded.down_bps == 10_000_000

    def test_saturated_observation_moves_the_estimate(self):
        capacity = Capacity(10_000_000, 2_000_000, 0.9, measured_at=0)
        observation = Observation("a", down_bps=2_000_000, up_bps=500_000, saturated=True)

        folded = fold(capacity, observation, now=1)
        assert folded.down_bps < 10_000_000

    def test_confidence_decays_with_age(self):
        capacity = Capacity(10_000_000, 2_000_000, 1.0, measured_at=0)

        assert decay_confidence(capacity, 0) == pytest.approx(1.0)
        assert decay_confidence(capacity, CONFIDENCE_HALF_LIFE_S) == pytest.approx(0.5)
        assert decay_confidence(capacity, CONFIDENCE_HALF_LIFE_S * 4) < 0.1

    def test_unmeasured_capacity_has_no_confidence(self):
        assert decay_confidence(Capacity(), 100) == 0.0
        assert Capacity().known is False


class TestHistoricalHints:
    def test_a_measurement_always_beats_a_recollection(self):
        """Learned information is evidence, never truth. Current state wins."""
        measured = Capacity(2_000_000, 500_000, confidence=0.9, measured_at=0)
        hint = HistoricalHint("a", down_bps=40_000_000, up_bps=10_000_000, confidence=1.0)

        assert blend(measured, hint).down_bps == 2_000_000

    def test_a_hint_fills_a_gap_but_is_capped_below_a_measurement(self):
        hint = HistoricalHint("a", down_bps=40_000_000, up_bps=10_000_000, confidence=1.0)

        blended = blend(Capacity(), hint)
        assert blended.down_bps == 40_000_000
        assert blended.confidence <= 0.4, (
            "the allocator must be able to tell a recollection from an observation"
        )


class TestFabricCompatibility:
    def test_accepts_an_equal_or_newer_fabric(self):
        assert fabric_compatible("1.2.0", "1.2.0")
        assert fabric_compatible("1.3.0", "1.2.0")
        assert fabric_compatible("2.0.0", "1.2.0")

    def test_rejects_an_older_fabric(self):
        assert not fabric_compatible("1.1.9", "1.2.0")

    def test_unknown_version_fails_closed(self):
        """A clear refusal beats a tunnel that half-works."""
        assert not fabric_compatible(None, "1.2.0")
        assert not fabric_compatible("", "1.2.0")

    def test_prerelease_metadata_is_ignored_for_comparison(self):
        assert version_tuple("1.5.0-pr42.abc1234") == (1, 5, 0)
        assert version_tuple("1.5.0+build.7") == (1, 5, 0)

    def test_unparseable_version_is_treated_as_ancient(self):
        assert version_tuple("garbage") == (0, 0, 0)


class TestConfig:
    def test_defaults_produce_a_working_appliance(self):
        """First boot has no config, and neither does a factory reset."""
        config = Config()
        assert config.lan.critical_ssid
        assert config.lan.besteffort_ssid
        assert config.thresholds.activation_dwell_s > 0

    def test_missing_file_falls_back_to_defaults(self, tmp_path):
        config = load(tmp_path / "absent.json")
        assert config.lan.critical_ssid == "Stable_critical"

    def test_corrupt_file_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("{ not json")

        config = load(path)
        assert config.lan.critical_ssid == "Stable_critical"

    def test_partial_file_merges_over_defaults(self, tmp_path):
        import json

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"api_port": 9090}))

        config = load(path)
        assert config.api_port == 9090
        assert config.lan.address == "10.44.0.1"


class TestApi:
    @pytest.fixture
    def client(self, daemon):
        from dirty.api import create_app

        daemon.start()
        daemon.tick()
        app = create_app(daemon)
        app.config.update(TESTING=True)
        return app.test_client()

    def test_state_endpoint(self, client):
        response = client.get("/api/state")
        assert response.status_code == 200
        assert "atomics" in response.get_json()

    def test_health_endpoint_is_cheap_and_complete(self, client):
        """The OTA watchdog depends on this. It must not hang or 500."""
        payload = client.get("/api/health").get_json()
        assert payload["ok"] is True
        assert "version" in payload
        assert "ap_running" in payload

    def test_setting_a_mode(self, client, daemon):
        atomic_id = daemon.registry.all()[0].id

        response = client.post(f"/api/atomics/{atomic_id}/mode", json={"mode": "normal"})

        assert response.status_code == 200
        assert daemon.registry.get(atomic_id).mode is Mode.NORMAL

    def test_rejects_an_unknown_mode(self, client, daemon):
        atomic_id = daemon.registry.all()[0].id
        response = client.post(f"/api/atomics/{atomic_id}/mode", json={"mode": "turbo"})
        assert response.status_code == 400

    def test_unknown_atomic_is_404(self, client):
        response = client.post("/api/atomics/nope/mode", json={"mode": "normal"})
        assert response.status_code == 404

    def test_dashboard_renders(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert b"DIRTY" in response.data

    def test_diagnostics_bundle_carries_no_credentials(self, client):
        """Safe to attach to an issue — keep it that way when extending it."""
        import io
        import tarfile

        response = client.get("/api/diagnostics/bundle")
        assert response.status_code == 200

        with tarfile.open(fileobj=io.BytesIO(response.data), mode="r:gz") as archive:
            blob = b"".join(archive.extractfile(m).read() for m in archive.getmembers()).lower()

        for marker in (b"passphrase", b"private key", b"psk", b"password"):
            assert marker not in blob
