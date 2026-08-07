"""The control loops.

Three cadences over one state store, plus one standalone cadence:

* **fast** (~1 s) — liveness, failover, hysteresis timers, enforcement reconciliation
* **medium** (~10 s) — capacity re-estimation, demand, allocation decisions
* **slow** (~5 min) — registry persistence, rollups, learning
* **telemetry** (~60 s, matches `Telemetry.flush_interval_s`) — decision/event/
  sample flush to sqlite. Scheduled independently of the three loops above
  because none of their cadences match the documented flush interval — see
  `tick()` and docs/backlog/traffic-blockers.md item 12.

Splitting them matters. Failover must be fast, but re-deciding allocation every
second would flap, and learning is expensive enough that it must not sit in the
hot path.

The daemon programs the network. It is never in the path of it — if this process
dies, kernel rules persist and the user keeps working (ADR-008).
"""

from __future__ import annotations

import threading

from wifucked.allocator import Allocation, Allocator, BackupState
from wifucked.atomics import Health, Mode, PortRole, Registry
from wifucked.clock import Clock, RealClock
from wifucked.config import Config, release_info
from wifucked.demand import CounterDemand, DemandEstimator, StaticDemand
from wifucked.discovery import Discoverer
from wifucked.enforce import Enforcer, LinuxEnforcer, MockEnforcer, render
from wifucked.hal import Hal, build_hal
from wifucked.lanout import LanOutClassifier
from wifucked.logging import get_logger
from wifucked.policy import profiles_for_lan_mode
from wifucked.probe import LinuxProber, Prober, ScriptedProber, fold, quality_of
from wifucked.radio import RadioManager, RadioState
from wifucked.telemetry import Telemetry
from wifucked.tunnel import MockTunnel, Tunnel, WireGuardTunnel
from wifucked.watchdog import sd_notify

log = get_logger("daemon")

#: Fallback base if `LanConfig.address` is unparseable — matches `LanConfig`'s
#: own default, so a malformed hand-edited config still boots (SOP-002:
#: "the device must boot and serve its SSIDs with no configuration file at
#: all" — a bad `lan.address` must degrade, not crash daemon construction).
_DEFAULT_LAN_OUT_GATEWAY_PREFIX = "10.44"
_DEFAULT_LAN_OUT_BASE_THIRD_OCTET = 0


def _lan_out_subnet_base(address: str) -> tuple[str, int]:
    """Derive the LAN-out subnet base from `LanConfig.address`, degrading to
    a safe default rather than raising on a malformed hand-edited config.
    """
    octets = address.split(".")
    if len(octets) != 4:
        log.warning(
            "Could not derive LAN-out subnet base from lan.address; using default",
            extra={
                "workflow": "daemon_init",
                "state": "failed",
                "intent": "pick a subnet range for wired ports that switch into DHCP-server mode",
                "address": address,
                "reason": "lan.address is not a dotted-quad",
                "fallback_prefix": _DEFAULT_LAN_OUT_GATEWAY_PREFIX,
            },
        )
        return _DEFAULT_LAN_OUT_GATEWAY_PREFIX, _DEFAULT_LAN_OUT_BASE_THIRD_OCTET
    try:
        third_octet = int(octets[2])
    except ValueError:
        log.warning(
            "Could not derive LAN-out subnet base from lan.address; using default",
            extra={
                "workflow": "daemon_init",
                "state": "failed",
                "intent": "pick a subnet range for wired ports that switch into DHCP-server mode",
                "address": address,
                "reason": "third octet is not numeric",
                "fallback_prefix": _DEFAULT_LAN_OUT_GATEWAY_PREFIX,
            },
        )
        return _DEFAULT_LAN_OUT_GATEWAY_PREFIX, _DEFAULT_LAN_OUT_BASE_THIRD_OCTET
    return f"{octets[0]}.{octets[1]}", third_octet


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
        #: Which service profiles actually exist for the configured LAN layout
        #: (ADR-020) — `"single"` (the current default) has no VLAN split, so
        #: only `BEST_EFFORT` is real. Threaded into every module that
        #: classifies or accounts LAN traffic, rather than each assuming
        #: `DEFAULT_PROFILES`.
        self.profiles = profiles_for_lan_mode(config.lan.lan_mode)
        self.allocator = Allocator(
            self.clock, self.telemetry, config.thresholds, profiles=self.profiles
        )
        self.discoverer = Discoverer(
            self.clock,
            wifi_scan_min_interval_s=config.loops.wifi_scan_min_interval_s,
            include_wifi_wan=config.lan.wan_uses_wifi,
        )
        #: DHCP-attempt/passive-listen/DHCP-server-fallback pipeline for
        #: wired ports (ADR-023, implementing ADR-022's Decision). None when
        #: disabled by config — every wired port then simply stays whatever
        #: ADR-022's discovery default already set it to (Mode.NORMAL), with
        #: no automatic reclassification to LAN_OUT ever happening.
        gateway_prefix, base_third_octet = _lan_out_subnet_base(config.lan.address)
        self.lan_out_classifier = (
            LanOutClassifier(
                dhcp_client_timeout_s=config.lan_out.dhcp_client_timeout_s,
                passive_listen_timeout_s=config.lan_out.passive_listen_timeout_s,
                gateway_prefix=gateway_prefix,
                base_third_octet=base_third_octet,
            )
            if config.lan_out.enabled
            else None
        )

        # Real, hardware-backed implementations when there is real hardware to
        # back them; the scripted/mock fakes otherwise. `self.hal.mocked` is the
        # single source of truth for which world this process is in — build_hal()
        # already resolved MOCK_HW into it, so nothing here re-reads the env var.
        real_hw = not self.hal.mocked
        self.enforcer = enforcer or (
            LinuxEnforcer(lan_mode=config.lan.lan_mode, profiles=self.profiles)
            if real_hw
            else MockEnforcer()
        )
        self.prober = prober or (
            LinuxProber(
                self.hal,
                self.clock,
                active_probe_budget_s=config.loops.probe_budget_s,
                active_probe_timeout_s=config.loops.probe_timeout_s,
            )
            if real_hw
            else ScriptedProber()
        )
        self.demand = demand or (
            CounterDemand(
                self.profiles, hal=self.hal, clock=self.clock, lan_mode=config.lan.lan_mode
            )
            if real_hw
            else StaticDemand(self.profiles)
        )
        self.radio = RadioManager(self.hal, self.telemetry)
        fabric_min = self.release.get("WIFUCKED_FABRIC_MIN", "0.0.0")
        self.tunnel = tunnel or (
            WireGuardTunnel(fabric_min, interface=config.fabric.interface)
            if real_hw
            else MockTunnel(fabric_min)
        )
        #: Set once `attach()` has been tried, so start() only ever tries the
        #: one-shot fabric registration a single time per process lifetime.
        self._fabric_attach_tried = False

        self.allocation: Allocation | None = None
        self.radio_state: RadioState | None = None

        self._stop = threading.Event()
        # "telemetry" is its own scheduler entry, not tied to fast/medium/slow
        # (see tick()'s docstring below and docs/backlog/traffic-blockers.md
        # item 12) — none of the three loop cadences match the documented
        # `flush_interval_s`, so telemetry gets a cadence of its own.
        self._next = {"fast": 0.0, "medium": 0.0, "slow": 0.0, "telemetry": 0.0}

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        log.info(
            "Daemon starting",
            extra={
                "workflow": "daemon_start",
                "state": "started",
                "intent": "begin managing connectivity",
                "version": self.release.get("WIFUCKED_VERSION", "unknown"),
                "channel": self.release.get("WIFUCKED_CHANNEL", "unknown"),
                "mocked_hardware": self.hal.mocked,
            },
        )
        # Discover before the first decision so we never allocate against an
        # empty world and briefly conclude there is no connectivity.
        self.discover_once()
        self._attach_fabric_once()

    def _attach_fabric_once(self) -> None:
        """Register with the first configured fabric server, one time.

        Only the real tunnel knows how to attach; MockTunnel has nothing to do
        here. A retry loop belongs to a later phase (Phase 2's multi-server
        fabric) — for now a failed attach logs clearly and status() reports why,
        which is enough to diagnose from the dashboard.
        """
        if self._fabric_attach_tried or not isinstance(self.tunnel, WireGuardTunnel):
            return
        self._fabric_attach_tried = True
        servers = self.config.fabric.servers
        if not servers:
            log.info(
                "No fabric server configured; tunnel will stay down",
                extra={
                    "workflow": "fabric_attach",
                    "state": "skipped",
                    "intent": "establish the stable tunnel",
                    "reason": "config.fabric.servers is empty",
                },
            )
            return
        self.tunnel.attach(
            servers[0],
            username=self.config.fabric.username,
            password=self.config.fabric.password,
            version=self.release.get("WIFUCKED_VERSION", "0.0.0-dev"),
        )

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
        # Tell systemd we're up (no-op unless $NOTIFY_SOCKET is set, i.e.
        # unless we're actually running under a Type=notify unit).
        sd_notify("READY=1")
        while not self._stop.is_set():
            self.tick()
            # Feed the watchdog once per fast-loop iteration. WatchdogSec=120
            # in the unit file is only a real liveness contract if this keeps
            # happening — see docs/backlog/traffic-blockers.md item 2.
            sd_notify("WATCHDOG=1")
            self.clock.sleep(self.config.loops.fast_s)

    # -- loops ----------------------------------------------------------------

    def tick(self) -> None:
        """One scheduler pass. Runs whichever loops are due.

        Medium runs before fast when both are due on the same pass. The medium
        loop is what produces a new `Allocation`; the fast loop is what renders
        and reconciles it (ADR-007). Doing it the other way round meant that on
        every tick where a medium-loop decision landed, that tick's own fast
        loop reconciled against the *previous* allocation, and the fresh one
        only took effect a full second later. Harmless for activation (the new
        share simply appeared a tick late), but for *deactivation* it meant the
        kernel kept a withdrawn BACKUP route/shaping live for one extra tick
        after `backup_state` had already left ACTIVE — a real, if brief, gap
        between "we told it to stop" and "it stopped" that a scenario
        reading `enforcer.actual()` (see appliance/tests/scenarios/conftest.py)
        can and does catch.
        """
        now = self.clock.now()
        if now >= self._next["medium"]:
            self._next["medium"] = now + self.config.loops.medium_s
            self._medium_loop()
        if now >= self._next["fast"]:
            self._next["fast"] = now + self.config.loops.fast_s
            self._fast_loop()
        if now >= self._next["slow"]:
            self._next["slow"] = now + self.config.loops.slow_s
            self._slow_loop()
        # Telemetry gets its own cadence entry rather than riding the slow
        # loop (~300s): the documented flush_interval_s is 60s, and none of
        # fast/medium/slow match that, so tying it to any one of them either
        # flushes 5x slower than documented (slow loop) or flushes far more
        # often than needed (fast/medium loop, though telemetry.tick() itself
        # gates on flush_interval_s so that would only be wasteful, not
        # incorrect). See docs/backlog/traffic-blockers.md item 12.
        if now >= self._next["telemetry"]:
            self._next["telemetry"] = now + self.telemetry.flush_interval_s
            self.telemetry.tick()

    def _fast_loop(self) -> None:
        atomics = self.registry.all()

        self.radio_state = self.radio.observe(atomics)
        if self.radio_state.channel_conflict:
            self.radio.align(self.radio_state)

        if self.allocation is not None:
            by_id = {a.id: a for a in atomics}
            self.enforcer.reconcile(
                render(
                    self.allocation,
                    by_id,
                    profiles=self.profiles,
                    tunnel_ifname=self.tunnel.interface,
                )
            )
            self._bind_tunnel(by_id)

        self._update_led()

    def _medium_loop(self) -> None:
        self.discover_once()
        self._classify_lan_out_ports()
        self._measure()

        atomics = self.registry.all()
        demand = self.demand.sample()
        self.allocation = self.allocator.decide(atomics, demand)

        self._spend_liveness_budget()

    def _slow_loop(self) -> None:
        # Telemetry flushing runs on its own cadence entry in tick(), not
        # here — see the comment there and item 12 in
        # docs/backlog/traffic-blockers.md. registry.persist() stays on the
        # slow loop; it has no documented cadence of its own to honor.
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

        self._log_diagnostics_snapshot()

    def _log_diagnostics_snapshot(self) -> None:
        """Periodic DEBUG-level system snapshot for field debugging.

        Runs on the slow loop (~5 min): this is read-only diagnostic exhaust,
        not a decision input, so it does not need a cadence of its own the way
        telemetry does. Everything here is either already computed elsewhere
        (the registry) or explicitly read-only (``enforcer.raw_dump()``,
        ``hal.ap.status()``) — nothing in this method installs or removes
        kernel state (ADR-007, ADR-008). Feeds `journalctl -u wifucked`
        directly and the `/api/diagnostics/bundle` support bundle (SOP-009);
        it does not scan the radio itself, so it never adds an off-channel
        scan beyond what `Discoverer` already throttles (ADR-011).
        """
        ap_status = self.hal.ap.status()
        atomics = self.registry.present()
        kernel = self.enforcer.raw_dump()

        log.debug(
            "Periodic diagnostics snapshot",
            extra={
                "workflow": "diagnostics_snapshot",
                "state": "completed",
                "intent": "give field debugging a full picture without a live session",
                "ap_running": ap_status.running,
                "ap_channel": ap_status.channel,
                "ap_ssids": ap_status.ssids,
                "ap_associated_clients": ap_status.associated_clients,
                "wan_atomics": [
                    {
                        "id": a.id,
                        "kind": str(a.kind),
                        "mode": str(a.mode),
                        "health": str(a.health),
                    }
                    for a in atomics
                ],
                "nft_ruleset": kernel.get("nft_ruleset", ""),
                "tc_qdisc": kernel.get("tc_qdisc", ""),
                "ip_rule": kernel.get("ip_rule", ""),
                "ip_route": kernel.get("ip_route", ""),
            },
        )

    # -- steps ----------------------------------------------------------------

    def discover_once(self) -> None:
        self.registry.observe(self.discoverer.discover(self.hal))

    def _classify_lan_out_ports(self) -> None:
        """Apply any DHCP-attempt/passive-listen pipeline outcomes that
        finished since the last tick (ADR-023).

        `LanOutClassifier.consider()` is non-blocking — the bounded
        multi-second pipeline runs on its own background thread(s), never on
        this loop thread — so this is cheap to call every medium tick even
        though most calls apply nothing.
        """
        if self.lan_out_classifier is None:
            return
        for outcome in self.lan_out_classifier.consider(self.hal, self.registry.all()):
            self.registry.set_role_and_mode(
                outcome.atomic_id, outcome.role, outcome.mode, reason=outcome.reason
            )

    def _measure(self) -> None:
        now = self.clock.now()
        # Bound this pass's blocking active probing (see item 8 in
        # docs/backlog/traffic-blockers.md and probe.LinuxProber.begin_pass) so
        # a slow or hung `ping` can't push this tick() call — which the fast
        # loop's failover/reconciliation runs *after*, on the same thread —
        # past the fast loop's own cadence.
        self.prober.begin_pass(now)
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
            self.allocator.mark_liveness_probed(atomic)
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
            "version": self.release.get("WIFUCKED_VERSION", "0.0.0-dev"),
            "channel": self.release.get("WIFUCKED_CHANNEL", "development"),
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
                "lan_out": sum(1 for a in atomics if a.role is PortRole.LAN_OUT),
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
