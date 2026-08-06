#!/usr/bin/env python3
"""Runs inside the QEMU "appliance" guest for the WAN-chaos download proof
(docs/active-tests.md's entry for this test).

Unlike driver.py's one-shot render()/reconcile() snapshot (the ADR-019 packet
proof), this drives the actual **control loop** — the same steps
`wifucked.daemon.Daemon.tick()` performs each cycle (`_measure`, `_fast_loop`,
`_bind_tunnel`) — repeatedly, for the whole test duration, against two real
WAN links (eth1, eth2) the host is actively degrading with `tc netem`/`tbf`
(chaos_wan.sh) while a real HTTP download runs on the LAN side (curl, driven
from the orchestrator, in the lanclient netns).

Not reused verbatim: `wifucked.daemon.Daemon` itself, because its
`Discoverer` finds atomics via real USB/Wi-Fi enumeration, which has no
meaning for two virtio-net links in a synthetic topology (ADR-002 also
forbids treating that discovery machinery as optional plumbing to fake). This
script seeds a `Registry` directly with the two WAN atomics instead — control
plane logic (`Allocator`, `LinuxProber`, `WireGuardTunnel`, `LinuxEnforcer`)
is the real, unmodified module code the daemon calls; only discovery is
skipped.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import wifucked.probe as probe_mod
from wifucked.allocator import Allocator
from wifucked.atomics.model import Atomic, Capacity, Health, Kind, Mode
from wifucked.atomics.registry import Registry
from wifucked.clock import RealClock
from wifucked.demand import ClassDemand
from wifucked.enforce import LinuxEnforcer, render
from wifucked.hal.linux import LinuxNet
from wifucked.hal.mock import build_mock_hal
from wifucked.policy import BEST_EFFORT, SINGLE_LAN_PROFILES, Thresholds
from wifucked.probe import LinuxProber, fold, quality_of
from wifucked.telemetry import Telemetry
from wifucked.tunnel import DEFAULT_PRIVATE_KEY_FILE, DEFAULT_PUBLIC_KEY_FILE, WireGuardTunnel

FABRIC_URL = "http://203.0.113.10:8081"
RESULT_PATH = "/wifucked-qemu-result.json"
TICK_S = float(os.environ.get("WIFUCKED_CHAOS_TICK_S", "3"))
DURATION_S = float(os.environ.get("WIFUCKED_CHAOS_DURATION_S", "90"))

# The real probe targets (1.1.1.1, 8.8.8.8) don't exist in this topology —
# the "internet" netns stand-in does. Overriding module state at import time,
# same technique driver.py uses for FABRIC_URL's version floor.
probe_mod.PROBE_TARGETS = ("198.51.100.2",)


def log(msg: str) -> None:
    print(f"wifucked-chaos-test: {msg}", flush=True)


def generate_identity() -> None:
    DEFAULT_PRIVATE_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    priv = subprocess.run(
        ["wg", "genkey"], capture_output=True, text=True, check=True
    ).stdout.strip()
    pub = subprocess.run(
        ["wg", "pubkey"], input=priv, capture_output=True, text=True, check=True
    ).stdout.strip()
    DEFAULT_PRIVATE_KEY_FILE.write_text(priv + "\n")
    DEFAULT_PRIVATE_KEY_FILE.chmod(0o600)
    DEFAULT_PUBLIC_KEY_FILE.write_text(pub + "\n")
    log(f"generated device WireGuard identity, public key {pub}")


def main() -> int:
    result: dict = {"ticks": [], "primary_switches": []}

    generate_identity()

    clock = RealClock()
    hal = build_mock_hal()
    hal.net = LinuxNet()  # the one real capability this loop needs from the HAL
    hal.mocked = False

    telemetry = Telemetry(clock, db_path=None)
    registry = Registry(clock, state_path=None)
    prober = LinuxProber(hal, clock)
    allocator = Allocator(clock, telemetry)
    enforcer = LinuxEnforcer(dry_run=False, lan_mode="single", base_interface="eth0")
    tunnel = WireGuardTunnel(fabric_min="0.0.0", interface="wg0")

    attached = tunnel.attach(
        FABRIC_URL, username="qemutest", password="qemutest", version="9.9.9-qemutest"
    )
    log(f"tunnel attach ok={attached}")
    if not attached:
        result["fatal"] = "tunnel attach failed"
        _write(result)
        return 1

    wan_a = Atomic(
        id="wan-a",
        kind=Kind.WIFI,
        label="WAN A",
        mode=Mode.NORMAL,
        ifname="eth1",
        present=True,
        health=Health.GOOD,
        capacity=Capacity(
            down_bps=10_000_000, up_bps=5_000_000, confidence=0.9, measured_at=clock.now()
        ),
    )
    wan_b = Atomic(
        id="wan-b",
        kind=Kind.USB_ETHERNET,
        label="WAN B",
        mode=Mode.NORMAL,
        ifname="eth2",
        present=True,
        health=Health.GOOD,
        capacity=Capacity(
            down_bps=10_000_000, up_bps=5_000_000, confidence=0.9, measured_at=clock.now()
        ),
    )
    registry.observe([wan_a, wan_b])

    # First bind before any probing has happened — mirrors first boot,
    # where the daemon must pick *something* before it has measurements.
    tunnel.bind_to(wan_a.id, wan_a.ifname)
    current_via = wan_a.id
    result["primary_switches"].append({"t": 0.0, "to": current_via})

    demand = {
        BEST_EFFORT.name: ClassDemand(
            profile_name=BEST_EFFORT.name, down_bps=8_000_000, up_bps=200_000
        )
    }

    started = clock.now()
    tick = 0
    while clock.now() - started < DURATION_S:
        tick += 1
        now_rel = round(clock.now() - started, 1)

        prober.begin_pass(clock.now())
        for atomic in registry.present():
            observation = prober.observe(atomic)
            if observation is None:
                continue
            registry.update_capacity(atomic.id, fold(atomic.capacity, observation, clock.now()))
            updated = registry.get(atomic.id)
            if updated is not None:
                updated.quality = quality_of(observation)
                registry.update_health(atomic.id, _health_of(observation))

        atomics = registry.all()
        allocation = allocator.decide(atomics, demand)

        by_id = {a.id: a for a in atomics}
        if allocation.primary_id is not None:
            primary = by_id.get(allocation.primary_id)
            if primary is not None and primary.ifname and allocation.primary_id != current_via:
                bound = tunnel.bind_to(primary.id, primary.ifname)
                log(f"t=+{now_rel}s WAN swap {current_via} -> {primary.id} bound={bound}")
                if bound:
                    current_via = primary.id
                    result["primary_switches"].append({"t": now_rel, "to": current_via})

        desired = render(
            allocation, by_id, profiles=SINGLE_LAN_PROFILES, tunnel_ifname=tunnel.interface
        )
        enforcer.reconcile(desired)

        snapshot = {
            "t": now_rel,
            "via": current_via,
            "wan_a_health": by_id["wan-a"].health.value if "wan-a" in by_id else None,
            "wan_a_rtt_ms": by_id["wan-a"].quality.rtt_ms if "wan-a" in by_id else None,
            "wan_a_loss_pct": by_id["wan-a"].quality.loss_pct if "wan-a" in by_id else None,
            "wan_b_health": by_id["wan-b"].health.value if "wan-b" in by_id else None,
            "wan_b_rtt_ms": by_id["wan-b"].quality.rtt_ms if "wan-b" in by_id else None,
            "wan_b_loss_pct": by_id["wan-b"].quality.loss_pct if "wan-b" in by_id else None,
        }
        result["ticks"].append(snapshot)
        log(f"tick={tick} {json.dumps(snapshot)}")

        remaining = TICK_S - (clock.now() - started - now_rel)
        if remaining > 0:
            time.sleep(remaining)

    result["switch_count"] = len(result["primary_switches"]) - 1
    _write(result)
    return 0


_THRESHOLDS = Thresholds()


def _health_of(observation) -> Health:
    # Mirrors Daemon._health_of exactly (daemon.py) — deliberately not
    # imported, since Daemon carries a `self.config` dependency this driver
    # has no reason to construct just to reach `config.thresholds`.
    if observation.rtt_ms is not None and observation.rtt_ms > _THRESHOLDS.degraded_rtt_ms:
        return Health.DEGRADED
    if observation.loss_pct is not None and observation.loss_pct > _THRESHOLDS.degraded_loss_pct:
        return Health.DEGRADED
    return Health.GOOD


def _write(result: dict) -> None:
    with open(RESULT_PATH, "w") as f:
        json.dump(result, f, indent=2)
    print("WIFUCKED_QEMU_RESULT_START")
    print(json.dumps(result))
    print("WIFUCKED_QEMU_RESULT_END")


if __name__ == "__main__":
    sys.exit(main())
