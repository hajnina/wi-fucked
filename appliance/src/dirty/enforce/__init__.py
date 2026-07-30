"""Enforcement — render an Allocation into kernel state.

Two rules govern this module, and both are easy to get backwards:

**Reconciliation, not command** (ADR-007). Declare desired state; read actual
state; apply the difference. Never assume a rule you installed is still there —
an interface bounce, NetworkManager, a debugging session, or a daemon restart
will all have removed it.

**Never tear down** (ADR-008). There is no cleanup path here. No ``atexit``, no
``finally`` that flushes, no shutdown handler that removes qdiscs. Kernel state
outlives the process deliberately, so that a control-plane crash degrades
adaptivity rather than causing an outage.

`enforce` is the only module permitted to invoke ``tc``, ``nft``, or ``ip``.

WS-D owns this module. Phase 0 ships the reconciliation skeleton and the desired
state model; the command rendering is stubbed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol

from dirty.allocator import Allocation
from dirty.atomics.model import Atomic
from dirty.logging import get_logger
from dirty.policy import DEFAULT_PROFILES, ServiceProfile

log = get_logger("enforce")


@dataclass(frozen=True, slots=True)
class Shaping:
    """Desired CAKE shaping for one interface."""

    ifname: str
    down_bps: int
    up_bps: int
    diffserv: str = "diffserv4"


@dataclass(frozen=True, slots=True)
class RouteRule:
    """One policy-routing entry: fwmark → routing table → interface."""

    fwmark: int
    table: int
    ifname: str


@dataclass(frozen=True, slots=True)
class DesiredState:
    shaping: tuple[Shaping, ...]
    routes: tuple[RouteRule, ...]
    marks: tuple[tuple[int, int], ...]  # (vlan, fwmark)

    def key(self) -> tuple:
        return (self.shaping, self.routes, self.marks)


class Enforcer(Protocol):
    def reconcile(self, desired: DesiredState) -> None: ...
    def actual(self) -> DesiredState | None: ...


def render(
    allocation: Allocation,
    atomics: dict[str, Atomic],
    profiles: tuple[ServiceProfile, ...] = DEFAULT_PROFILES,
) -> DesiredState:
    """Turn an allocation into the kernel state that would implement it."""
    shaping: list[Shaping] = []
    routes: list[RouteRule] = []
    marks: list[tuple[int, int]] = []

    table = 100
    for share in allocation.shares:
        atomic = atomics.get(share.atomic_id)
        if atomic is None or not atomic.ifname:
            continue
        profile = next((p for p in profiles if p.name == share.profile_name), None)
        if profile is None:
            continue
        fwmark = profile.vlan
        marks.append((profile.vlan, fwmark))
        routes.append(RouteRule(fwmark=fwmark, table=table, ifname=atomic.ifname))

    for atomic_id in {s.atomic_id for s in allocation.shares}:
        atomic = atomics.get(atomic_id)
        if atomic is None or not atomic.ifname or not atomic.capacity.known:
            continue
        shaping.append(
            Shaping(
                ifname=atomic.ifname,
                # Shape slightly under measured capacity so the queue lives here,
                # where CAKE can manage it, rather than in the ISP's buffer.
                down_bps=int(atomic.capacity.down_bps * 0.95),
                up_bps=int(atomic.capacity.up_bps * 0.95),
            )
        )

    return DesiredState(
        shaping=tuple(sorted(shaping, key=lambda s: s.ifname)),
        routes=tuple(sorted(set(routes), key=lambda r: (r.fwmark, r.ifname))),
        marks=tuple(sorted(set(marks))),
    )


class MockEnforcer(Enforcer):
    """Records what would have been applied. Used by MOCK_HW and scenario tests."""

    def __init__(self) -> None:
        self.applied: list[DesiredState] = []
        self._actual: DesiredState | None = None

    def reconcile(self, desired: DesiredState) -> None:
        if self._actual is not None and self._actual.key() == desired.key():
            return
        self.applied.append(desired)
        self._actual = desired
        log.debug(
            "Reconciled kernel state (mock)",
            extra={
                "workflow": "enforce_reconcile",
                "state": "completed",
                "intent": "apply the allocator's decision to the data plane",
                "shaped_interfaces": len(desired.shaping),
                "route_rules": len(desired.routes),
            },
        )

    def actual(self) -> DesiredState | None:
        return self._actual

    def bytes_on(self, ifname: str) -> int:
        """Bytes this interface was ever permitted to carry.

        Scenario tests use this to assert the BACKUP-carries-zero invariant.
        """
        return sum(
            s.down_bps + s.up_bps
            for state in self.applied
            for s in state.shaping
            if s.ifname == ifname
        )


class LinuxEnforcer(Enforcer):
    """Programs tc/CAKE, nftables and policy routing.

    WS-D owns the implementation. The reconciliation loop and the safety
    properties are settled; what remains is rendering the commands and parsing
    actual state back out.

    Note what is *absent*: any method that removes state. That is deliberate and
    load-bearing (ADR-008) — do not add one.
    """

    def __init__(self, dry_run: bool = False):
        self._dry_run = dry_run
        self._actual: DesiredState | None = None

    def reconcile(self, desired: DesiredState) -> None:
        current = self.actual()
        if current is not None and current.key() == desired.key():
            return

        log.info(
            "Reconciling kernel state",
            extra={
                "workflow": "enforce_reconcile",
                "state": "started",
                "intent": "apply the allocator's decision to the data plane",
                "shaped_interfaces": len(desired.shaping),
                "route_rules": len(desired.routes),
                "dry_run": self._dry_run,
            },
        )

        for shaping in desired.shaping:
            self._apply_cake(shaping)

        self._actual = desired

    def actual(self) -> DesiredState | None:
        # WS-D: parse `tc qdisc show`, `nft list ruleset`, `ip rule show` back
        # into a DesiredState so the diff is against reality rather than memory.
        return self._actual

    def _apply_cake(self, shaping: Shaping) -> None:
        argv = [
            "tc",
            "qdisc",
            "replace",
            "dev",
            shaping.ifname,
            "root",
            "cake",
            "bandwidth",
            f"{shaping.down_bps}bit",
            shaping.diffserv,
        ]
        if self._dry_run:
            log.info(
                "Would apply CAKE",
                extra={
                    "workflow": "enforce_shaping",
                    "state": "skipped",
                    "intent": "shape egress to measured capacity",
                    "ifname": shaping.ifname,
                    "target_bps": shaping.down_bps,
                    "reason": "dry run",
                },
            )
            return
        self._run(argv, shaping)

    def _run(self, argv: list[str], shaping: Shaping) -> None:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            log.error(
                "Failed to invoke tc; keeping previous shaping",
                extra={
                    "workflow": "enforce_shaping",
                    "state": "failed",
                    "intent": "shape egress to measured capacity",
                    "ifname": shaping.ifname,
                    "target_bps": shaping.down_bps,
                    "reason": "could not spawn tc",
                    "error": str(exc),
                },
                exc_info=True,
            )
            return

        if done.returncode != 0:
            log.error(
                "tc rejected the qdisc; keeping previous shaping",
                extra={
                    "workflow": "enforce_shaping",
                    "state": "failed",
                    "intent": "shape egress to measured capacity",
                    "ifname": shaping.ifname,
                    "target_bps": shaping.down_bps,
                    "returncode": done.returncode,
                    "reason": (done.stderr or "").strip()[:200],
                },
            )
