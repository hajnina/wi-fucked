"""The control loops.

Three cadences over one state store:

* **fast** (~1 s) — liveness, failover, hysteresis timers, enforcement reconciliation
* **medium** (~10 s) — capacity re-estimation, demand, allocation decisions
* **slow** (~5 min) — telemetry flush, rollups, learning

Splitting them matters. Failover must be fast, but re-deciding allocation every
second would flap, and learning is expensive enough that it must not sit in the
hot path.

The daemon programs the network. It is never in the path of it — if this process
dies, kernel rules persist and the user keeps working (ADR-008).
"""

from __future__ import annotations

import threading

from dirty.allocator import Allocation, Allocator, BackupState
from dirty.atomics import Health, Mode, Registry
from dirty.clock import Clock, RealClock
from dirty.config import Config, release_info
from dirty.demand import DemandEstimator, StaticDemand
from dirty.discovery import discover
from dirty.enforce import Enforcer, MockEnforcer, render
from dirty.hal import Hal, build_hal
from dirty.logging import get_logger
from dirty.policy import DEFAULT_PROFILES
from dirty.probe import Prober, ScriptedProber, fold, quality_of
from dirty.radio import RadioManager, RadioState
from dirty.telemetry import Telemetry
from dirty.tunnel import MockTunnel, Tunnel

log = get_logger("daemon")


class Daemon:
    def __init__(
        self,
        config: Config,
        *,
        hal: Hal | None = None,
        clock: Clock | None = None,
        telemetry: Telemetry | None = None,
        enforcer: Enforcer | None = None,
        prober: Prober | None = None,
        demand: DemandEstimator | None = None,
        tunnel: Tunnel | None = None,
        persist: bool = True,
    ):
        self.config = config
        self.clock = clock or RealClock()
        self.hal = hal or build_hal()
        self.release = release_info()

        self.telemetry = telemetry or Telemetry(
            self.clock, config.telemetry_path if persist else None
        )
        self.registry = Registry(self.clock, config.registry_path if persist else None)
        self.allocator = Allocator(self.clock, self.telemetry, config.thresholds)
        self.enforcer = enforcer or MockEnforcer()
        self.prober = prober or ScriptedProber()
        self.demand = demand or StaticDemand(DEFAULT_PROFILES)
        self.radio = RadioManager(self.hal, self.telemetry)
        self.tunnel = tunnel or MockTunnel(self.release.get("DIRTY_FABRIC_MIN", "0.0.0"))

        self.allocation: Allocation | None = None
        self.radio_state: RadioState | None = None

        self._stop = threading.Event()
        self._next = {"fast": 0.0, "medium": 0.0, "slow": 0.0}

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        log.info(
            "Daemon starting",
            extra={
                "workflow": "daemon_start",
                "state": "started",
                "intent": "begin managing connectivity",
                "version": self.release.get("DIRTY_VERSION", "unknown"),
                "channel": self.release.get("DIRTY_CHANNEL", "unknown"),
                "mocked_hardware": self.hal.mocked,
            },
        )
        # Discover before the first decision so we never allocate against an
        # empty world and briefly conclude there is no connectivity.
        self.discover_once()

    def stop(self) -> None:
        """Stop the loops.

        Note what this does *not* do: it does not remove qdiscs, flush nftables,
        or drop routes. Kernel state outlives this process deliberately, so that
        a crash or an update degrades adaptivity rather than causing an outage
        (ADR-008). Do not add teardown here.
        """
        self._stop.set()
        self.telemetry.close()
        log.info(
            "Daemon stopping; kernel state left in place",
            extra={
                "workflow": "daemon_stop",
                "state": "completed",
                "intent": "keep the user's network working without us",
            },
        )

    def run_forever(self) -> None:
        self.start()
        while not self._stop.is_set():
            self.tick()
            self.clock.sleep(self.config.loops.fast_s)

    # -- loops ----------------------------------------------------------------

    def tick(self) -> None:
        """One scheduler pass. Runs whichever loops are due."""
        now = self.clock.now()
        if now >= self._next["fast"]:
            self._next["fast"] = now + self.config.loops.fast_s
            self._fast_loop()
        if now >= self._next["medium"]:
            self._next["medium"] = now + self.config.loops.medium_s
            self._medium_loop()
        if now >= self._next["slow"]:
            self._next["slow"] = now + self.config.loops.slow_s
            self._slow_loop()

    def _fast_loop(self) -> None:
        atomics = self.registry.all()

        self.radio_state = self.radio.observe(atomics)
        if self.radio_state.channel_conflict:
            self.radio.align(self.radio_state)

        if self.allocation is not None:
            by_id = {a.id: a for a in atomics}
            self.enforcer.reconcile(render(self.allocation, by_id))
            self._bind_tunnel(by_id)

        self._update_led()

    def _medium_loop(self) -> None:
        self.discover_once()
        self._measure()

        atomics = self.registry.all()
        demand = self.demand.sample()
        self.allocation = self.allocator.decide(atomics, demand)

        self._spend_liveness_budget()

    def _slow_loop(self) -> None:
        self.telemetry.tick()
        self.registry.persist()

        facts = self.hal.system.facts()
        if facts.throttled:
            # A marginal power supply explains a large share of "it's flaky"
            # reports, and the device is in a position to say so itself.
            log.warning(
                "SoC reports throttling",
                extra={
                    "workflow": "health_check",
                    "state": "completed",
                    "intent": "surface undervoltage before the user blames the software",
                    "throttled_flags": hex(facts.throttled),
                    "reason": "undervoltage or thermal limiting detected",
                },
            )

    # -- steps ----------------------------------------------------------------

    def discover_once(self) -> None:
        self.registry.observe(discover(self.hal))

    def _measure(self) -> None:
        now = self.clock.now()
        for atomic in self.registry.present():
            observation = self.prober.observe(atomic)
            if observation is None:
                continue

            self.registry.update_capacity(atomic.id, fold(atomic.capacity, observation, now))
            updated = self.registry.get(atomic.id)
            if updated is not None:
                updated.quality = quality_of(observation)
                self.registry.update_health(atomic.id, self._health_of(observation))

            self.telemetry.record_sample(
                atomic.id,
                observation.down_bps,
                observation.up_bps,
                observation.rtt_ms,
                observation.loss_pct,
            )

    def _health_of(self, observation) -> Health:
        t = self.config.thresholds
        if observation.rtt_ms is not None and observation.rtt_ms > t.degraded_rtt_ms:
            return Health.DEGRADED
        if observation.loss_pct is not None and observation.loss_pct > t.degraded_loss_pct:
            return Health.DEGRADED
        return Health.GOOD

    def _spend_liveness_budget(self) -> None:
        """Probe BACKUP links occasionally so failover works when needed.

        Small, bounded, and accounted — the user sees these bytes separately
        from activation data, because "zero bytes at rest" carries an asterisk
        and hiding it would be the real problem (ADR-006).
        """
        for atomic in self.registry.backups():
            if not self.allocator.due_for_liveness(atomic):
                continue
            spent = self.config.thresholds.liveness_bytes
            self.registry.add_cost(atomic.id, liveness=spent)
            log.info(
                "Spent BACKUP liveness budget",
                extra={
                    "workflow": "backup_liveness",
                    "state": "completed",
                    "intent": "know the backup works before we need it",
                    "atomic_id": atomic.id,
                    "label": atomic.label,
                    "bytes": spent,
                    "interval_s": self.config.thresholds.liveness_interval_s,
                },
            )

    def _bind_tunnel(self, by_id: dict) -> None:
        if self.allocation is None or self.allocation.primary_id is None:
            return
        primary = by_id.get(self.allocation.primary_id)
        if primary is not None and primary.ifname:
            self.tunnel.bind_to(primary.id, primary.ifname)

    def _update_led(self) -> None:
        """The only status channel on a headless device."""
        pool = self.registry.normal_pool()
        spending = self.allocator.backup_state is BackupState.ACTIVE
        degraded = any(a.health is Health.DEGRADED for a in pool)

        if not pool and not self.registry.backups():
            pattern = "fast"
        elif spending or degraded:
            pattern = "slow"
        else:
            pattern = "solid"
        self.hal.led.set_pattern(pattern)

    # -- introspection --------------------------------------------------------

    def state_snapshot(self) -> dict:
        atomics = self.registry.all()
        return {
            "version": self.release.get("DIRTY_VERSION", "0.0.0-dev"),
            "channel": self.release.get("DIRTY_CHANNEL", "development"),
            "mocked_hardware": self.hal.mocked,
            "radio": {
                "profile": str(self.radio_state.profile) if self.radio_state else None,
                "ap_channel": self.radio_state.ap_channel if self.radio_state else None,
                "station_channel": (self.radio_state.station_channel if self.radio_state else None),
                "ap_running": self.radio_state.ap_running if self.radio_state else False,
                "clients": self.radio_state.clients if self.radio_state else 0,
                "csa_available": not self.radio.csa_unavailable,
            },
            "tunnel": _tunnel_dict(self.tunnel),
            "backup_state": str(self.allocator.backup_state),
            "allocation": {
                "primary_id": self.allocation.primary_id if self.allocation else None,
                "backup_active": self.allocation.backup_active if self.allocation else False,
            },
            "counts": {
                "normal": sum(1 for a in atomics if a.mode is Mode.NORMAL),
                "backup": sum(1 for a in atomics if a.mode is Mode.BACKUP),
                "unused": sum(1 for a in atomics if a.mode is Mode.UNUSED),
                "present": sum(1 for a in atomics if a.present),
            },
            "atomics": [a.to_dict() for a in atomics],
        }


def _tunnel_dict(tunnel: Tunnel) -> dict:
    status = tunnel.status()
    return {
        "state": str(status.state),
        "server": status.server.name if status.server else None,
        "via_atomic_id": status.via_atomic_id,
        "rtt_ms": status.server.rtt_ms if status.server else None,
    }
