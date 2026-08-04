"""The registry — remembering connections across disappearance and reboot."""

from __future__ import annotations

import json

from wifucked.atomics import Atomic, Capacity, Health, Kind, Mode, Registry
from wifucked.clock import VirtualClock


def _atomic(atomic_id: str = "wifi:hotel", **kwargs) -> Atomic:
    defaults = {
        "kind": Kind.WIFI,
        "label": "Hotel WiFi",
        "present": True,
        "ifname": "wlan0",
    }
    return Atomic(id=atomic_id, **{**defaults, **kwargs})


class TestObservation:
    def test_discovers_new_connections(self):
        registry = Registry(VirtualClock())
        registry.observe([_atomic()])

        assert len(registry.all()) == 1
        assert registry.get("wifi:hotel").mode is Mode.UNUSED, "discovery must not imply permission"

    def test_marks_vanished_connections_absent_without_forgetting_them(self):
        registry = Registry(VirtualClock())
        registry.observe([_atomic()])
        registry.set_mode("wifi:hotel", Mode.NORMAL)

        registry.observe([])

        atomic = registry.get("wifi:hotel")
        assert atomic is not None, "a vanished connection must not be forgotten"
        assert not atomic.present
        assert atomic.health is Health.ABSENT
        assert atomic.ifname is None, "a stale ifname is a lie"
        assert atomic.mode is Mode.NORMAL, "the user's choice must survive"

    def test_restores_policy_when_a_connection_returns(self):
        """The plug-and-play promise: unplug, replug, no reconfiguration."""
        registry = Registry(VirtualClock())
        registry.observe([_atomic()])
        registry.set_mode("wifi:hotel", Mode.BACKUP)
        registry.update_capacity("wifi:hotel", Capacity(9_000_000, 2_000_000, 0.8, 0))
        registry.observe([])

        registry.observe([_atomic(ifname="wlan1")])  # renumbered on return

        atomic = registry.get("wifi:hotel")
        assert atomic.present
        assert atomic.mode is Mode.BACKUP
        assert atomic.capacity.down_bps == 9_000_000, "learned history must survive"
        assert atomic.ifname == "wlan1", "ifname is refreshed, not remembered"

    def test_discovery_never_resets_a_user_choice(self):
        registry = Registry(VirtualClock())
        registry.observe([_atomic()])
        registry.set_mode("wifi:hotel", Mode.NORMAL)

        # Discovery always reports UNUSED for what it finds.
        registry.observe([_atomic(mode=Mode.UNUSED)])

        assert registry.get("wifi:hotel").mode is Mode.NORMAL


class TestPools:
    def test_normal_pool_excludes_absent_and_unused(self):
        registry = Registry(VirtualClock())
        registry.observe(
            [
                _atomic("a", label="A"),
                _atomic("b", label="B"),
                _atomic("c", label="C"),
            ]
        )
        registry.set_mode("a", Mode.NORMAL)
        registry.set_mode("b", Mode.BACKUP)
        registry.update_health("a", Health.GOOD)

        assert [x.id for x in registry.normal_pool()] == ["a"]
        assert [x.id for x in registry.backups()] == ["b"]

    def test_degraded_connections_stay_in_the_pool(self):
        """Degraded is usable. Removing it would drop the user to nothing."""
        registry = Registry(VirtualClock())
        registry.observe([_atomic()])
        registry.set_mode("wifi:hotel", Mode.NORMAL)
        registry.update_health("wifi:hotel", Health.DEGRADED)

        assert len(registry.normal_pool()) == 1


class TestPersistence:
    def test_round_trips_remembered_fields(self, tmp_path):
        path = tmp_path / "atomics.json"
        registry = Registry(VirtualClock(), path)
        registry.observe([_atomic()])
        registry.set_mode("wifi:hotel", Mode.BACKUP)
        registry.add_cost("wifi:hotel", consumed=1024, liveness=300)
        registry.persist()

        restored = Registry(VirtualClock(), path)
        atomic = restored.get("wifi:hotel")

        assert atomic.mode is Mode.BACKUP
        assert atomic.cost.consumed_bytes == 1024
        assert atomic.cost.liveness_bytes == 300

    def test_does_not_persist_volatile_state(self):
        """Presence and ifname are rediscovered; storing them would be a lie."""
        clock = VirtualClock()
        registry = Registry(clock)
        registry.observe([_atomic()])
        registry.persist()  # no path — must not raise

    def test_restored_connections_start_absent(self, tmp_path):
        path = tmp_path / "atomics.json"
        registry = Registry(VirtualClock(), path)
        registry.observe([_atomic()])
        registry.set_mode("wifi:hotel", Mode.NORMAL)

        restored = Registry(VirtualClock(), path)
        assert not restored.get("wifi:hotel").present

    def test_survives_a_corrupt_state_file(self, tmp_path):
        path = tmp_path / "atomics.json"
        path.write_text("{ this is not json")

        registry = Registry(VirtualClock(), path)
        assert registry.all() == [], "a corrupt file must not stop the daemon booting"

    def test_skips_unreadable_entries_but_keeps_the_rest(self, tmp_path):
        path = tmp_path / "atomics.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "atomics": {
                        "good": {"kind": "wifi", "label": "Good", "mode": "normal"},
                        "bad": {"kind": "not-a-kind", "label": "Bad"},
                    },
                }
            )
        )

        registry = Registry(VirtualClock(), path)
        assert [a.id for a in registry.all()] == ["good"]


def test_unknown_atomic_mode_change_is_survivable():
    registry = Registry(VirtualClock())
    assert registry.set_mode("nope", Mode.NORMAL) is None


class TestBoundedPersistence:
    """Bug 1: every scanned SSID must not become a permanent persisted row.

    Only atomics the user classified (mode != UNUSED) or that were genuinely
    connected to at least once (``ever_connected``) earn a place in the
    persisted file (ADR-010: bounded by construction, not by a cleanup job).
    """

    def test_never_connected_unclassified_atomics_are_not_persisted(self, tmp_path):
        path = tmp_path / "atomics.json"
        clock = VirtualClock()
        registry = Registry(clock, path)

        # Simulate many scanned-but-never-joined SSIDs arriving over a long
        # uptime, one medium-loop tick at a time.
        for i in range(500):
            registry.observe(
                [_atomic(f"wifi:seen-{i}", label=f"Network {i}", ever_connected=False)]
            )
            clock.advance(10.0)
            registry.persist()

        assert len(registry.all()) == 500, "in-memory state still tracks everything present"

        payload = json.loads(path.read_text())
        assert payload["atomics"] == {}, "unclassified, never-connected SSIDs must not be written"

    def test_ever_connected_atomic_is_persisted_even_if_still_unclassified(self, tmp_path):
        path = tmp_path / "atomics.json"
        registry = Registry(VirtualClock(), path)
        registry.observe([_atomic("wifi:home", ever_connected=True)])

        registry.persist()

        payload = json.loads(path.read_text())
        assert "wifi:home" in payload["atomics"]
        assert registry.get("wifi:home").mode is Mode.UNUSED, "connection was never classified"

    def test_ever_connected_stays_true_once_the_link_drops(self, tmp_path):
        """A radio going out of range must not un-persist a previously-used network."""
        path = tmp_path / "atomics.json"
        registry = Registry(VirtualClock(), path)
        registry.observe([_atomic("wifi:home", ever_connected=True)])
        registry.observe([_atomic("wifi:home", ever_connected=False)])  # scan sees it, not joined

        registry.persist()

        payload = json.loads(path.read_text())
        assert "wifi:home" in payload["atomics"]

    def test_bound_survives_a_restart(self, tmp_path):
        path = tmp_path / "atomics.json"
        registry = Registry(VirtualClock(), path)
        registry.observe(
            [
                _atomic("wifi:home", ever_connected=True),
                _atomic("wifi:never-joined", ever_connected=False),
            ]
        )
        registry.persist()

        restored = Registry(VirtualClock(), path)
        assert restored.get("wifi:home") is not None
        assert restored.get("wifi:never-joined") is None
        # And the bound keeps holding after a reboot, not just before one.
        restored.persist()
        payload = json.loads(path.read_text())
        assert list(payload["atomics"]) == ["wifi:home"]
