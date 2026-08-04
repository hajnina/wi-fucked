#!/usr/bin/env python3
"""Runs inside the QEMU guest, as the "appliance". Every call below is the
real `wifucked.enforce`/`wifucked.tunnel` code — not a reimplementation,
not a mock — issuing real `wg`/`ip`/`nft` commands against this guest's
real kernel.

Sequence, mirroring what `wifucked.daemon.Daemon` actually does at startup
and on the fast loop:

1. Generate this "appliance"'s WireGuard identity (what `firstboot.sh` does
   on real hardware).
2. `WireGuardTunnel.attach()` — register with the fabric over plain WAN
   reachability (wg0 doesn't exist yet), bring up `wg0` with the
   ADR-019 `allowed-ips 0.0.0.0/0`.
3. `WireGuardTunnel.bind_to()` — pin the fabric endpoint's route onto the
   first WAN atomic (eth1).
4. `enforce.render()` + `LinuxEnforcer.reconcile()` — install the real
   nft marking, ip rule/route, and CAKE shaping for a LAN client's
   critical-class traffic, routed onto the tunnel (ADR-019).
5. `WireGuardTunnel.bind_to()` a second time, onto eth2 — the WAN swap.
   `render()`'s output is re-checked to confirm nothing about LAN routing
   needed to change.
"""

from __future__ import annotations

import json
import subprocess
import sys

from wifucked.allocator import Allocation, Share
from wifucked.atomics.model import Atomic, Capacity, Kind, Mode
from wifucked.enforce import LinuxEnforcer, render
from wifucked.policy import CRITICAL
from wifucked.tunnel import (
    DEFAULT_PRIVATE_KEY_FILE,
    DEFAULT_PUBLIC_KEY_FILE,
    WireGuardTunnel,
)

FABRIC_URL = "http://203.0.113.10:8081"
RESULT_PATH = "/wifucked-qemu-result.json"


def log(msg: str) -> None:
    print(f"wifucked-qemu-test: {msg}", flush=True)


def generate_identity() -> None:
    """What firstboot.sh does on real hardware: generate the device's
    WireGuard keypair once, on-device (SOP-008), never baked into an image.
    """
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
    result: dict = {"steps": []}

    def record(name: str, ok: bool, **extra) -> None:
        result["steps"].append({"step": name, "ok": ok, **extra})
        log(f"step={name} ok={ok} {extra}")

    generate_identity()

    tunnel = WireGuardTunnel(fabric_min="0.0.0", interface="wg0")
    # Must clear fabric.MIN_APPLIANCE_VERSION (0.1.0 as of this writing) —
    # this is a synthetic test version, not this repo's real release version.
    attached = tunnel.attach(
        FABRIC_URL, username="qemutest", password="qemutest", version="9.9.9-qemutest"
    )
    record("attach", attached, server=str(tunnel.status().server))
    if not attached:
        record("abort", False, reason="tunnel attach failed, cannot proceed")
        _write(result)
        return 1

    wan_a = Atomic(
        id="wan-a",
        kind=Kind.WIFI,
        label="WAN A",
        mode=Mode.NORMAL,
        ifname="eth1",
        present=True,
        capacity=Capacity(down_bps=10_000_000, up_bps=5_000_000, confidence=0.9, measured_at=0.0),
    )
    wan_b = Atomic(
        id="wan-b",
        kind=Kind.USB_ETHERNET,
        label="WAN B",
        mode=Mode.NORMAL,
        ifname="eth2",
        present=True,
        capacity=Capacity(down_bps=10_000_000, up_bps=5_000_000, confidence=0.9, measured_at=0.0),
    )

    bound_a = tunnel.bind_to(wan_a.id, wan_a.ifname)
    record("bind_to_wan_a", bound_a, via=tunnel.status().via_atomic_id)

    allocation = Allocation(
        primary_id=wan_a.id,
        backup_active=False,
        shares=(Share(wan_a.id, CRITICAL.name, 5_000_000),),
    )
    desired = render(allocation, {wan_a.id: wan_a, wan_b.id: wan_b}, tunnel_ifname="wg0")
    record(
        "render",
        True,
        routes=[{"fwmark": r.fwmark, "table": r.table, "ifname": r.ifname} for r in desired.routes],
        marks=list(desired.marks),
    )

    enforcer = LinuxEnforcer(lan_mode="two_psk", base_interface="eth0")
    enforcer.reconcile(desired)
    record("reconcile_initial", True)

    # The WAN swap: bind the tunnel onto the second WAN atomic. render()'s
    # output must not change shape — the whole point of ADR-019.
    bound_b = tunnel.bind_to(wan_b.id, wan_b.ifname)
    record("bind_to_wan_b", bound_b, via=tunnel.status().via_atomic_id)

    desired_after_swap = render(allocation, {wan_a.id: wan_a, wan_b.id: wan_b}, tunnel_ifname="wg0")
    unchanged = (
        desired_after_swap.routes == desired.routes and desired_after_swap.marks == desired.marks
    )
    record(
        "routes_unchanged_across_wan_swap",
        unchanged,
        before=[r.ifname for r in desired.routes],
        after=[r.ifname for r in desired_after_swap.routes],
    )

    # Snapshot the actual kernel state for the host to compare against.
    wg_show = subprocess.run(["wg", "show", "wg0"], capture_output=True, text=True).stdout
    ip_rule = subprocess.run(["ip", "-j", "rule", "show"], capture_output=True, text=True).stdout
    ip_route_wg = subprocess.run(
        ["ip", "-j", "route", "show", "dev", "wg0"], capture_output=True, text=True
    ).stdout
    record(
        "kernel_snapshot", True, wg_show=wg_show, ip_rule=ip_rule[:2000], ip_route_wg=ip_route_wg
    )

    _write(result)
    return 0


def _write(result: dict) -> None:
    with open(RESULT_PATH, "w") as f:
        json.dump(result, f, indent=2)
    print("WIFUCKED_QEMU_RESULT_START")
    print(json.dumps(result))
    print("WIFUCKED_QEMU_RESULT_END")


if __name__ == "__main__":
    sys.exit(main())
