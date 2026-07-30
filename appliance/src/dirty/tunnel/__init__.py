"""The stable tunnel and the server fabric.

Client sessions terminate at a fabric server rather than at the WAN, so the
client-visible IP never changes when a WAN does. That is what makes a WAN swap
survivable instead of a visible outage — the tunnel is not an enhancement to
failover, it is the mechanism that makes failover invisible (ADR-005).

WS-E owns this module. Phase 0 ships the interface, the version-floor check, and
a mock; WireGuard management is stubbed.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol

from dirty.logging import get_logger

log = get_logger("tunnel")


class TunnelState(enum.StrEnum):
    DOWN = "down"
    CONNECTING = "connecting"
    UP = "up"
    #: The fabric is older than this appliance's protocol floor. Refusing is
    #: better than a tunnel that half-works in ways nobody can diagnose.
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class FabricServer:
    name: str
    endpoint: str
    public_key: str
    version: str | None = None
    healthy: bool = False
    rtt_ms: float | None = None


@dataclass(frozen=True, slots=True)
class TunnelStatus:
    state: TunnelState
    server: FabricServer | None
    #: The atomic currently carrying the tunnel.
    via_atomic_id: str | None = None
    last_handshake_s: float | None = None


class Tunnel(Protocol):
    def status(self) -> TunnelStatus: ...
    def bind_to(self, atomic_id: str, ifname: str) -> bool:
        """Move the tunnel onto a different WAN without dropping client sessions."""


def version_tuple(version: str) -> tuple[int, int, int]:
    """Parse a SemVer core. Prerelease and build metadata are ignored."""
    core = version.split("-", 1)[0].split("+", 1)[0]
    parts = [*core.split("."), "0", "0", "0"][:3]
    try:
        return tuple(int(p) for p in parts)  # type: ignore[return-value]
    except ValueError:
        return (0, 0, 0)


def fabric_compatible(server_version: str | None, minimum: str) -> bool:
    """Whether a fabric server meets this appliance's protocol floor.

    An unknown version fails closed. A tunnel that half-works is far harder to
    diagnose in the field than one that refuses to come up with a clear reason.
    """
    if not server_version:
        return False
    return version_tuple(server_version) >= version_tuple(minimum)


class MockTunnel(Tunnel):
    def __init__(self, fabric_min: str = "0.0.0"):
        self._fabric_min = fabric_min
        self._server = FabricServer(
            name="mock-fabric",
            endpoint="fabric.invalid:51820",
            public_key="mock",
            version="1.0.0",
            healthy=True,
            rtt_ms=24.0,
        )
        self._via: str | None = None

    def status(self) -> TunnelStatus:
        if not fabric_compatible(self._server.version, self._fabric_min):
            return TunnelStatus(TunnelState.INCOMPATIBLE, self._server, self._via)
        state = TunnelState.UP if self._via else TunnelState.DOWN
        return TunnelStatus(state, self._server, self._via, last_handshake_s=3.0)

    def bind_to(self, atomic_id: str, ifname: str) -> bool:
        if atomic_id == self._via:
            return True
        log.info(
            "Tunnel moved to a different WAN",
            extra={
                "workflow": "tunnel_migrate",
                "state": "completed",
                "intent": "keep client sessions alive across a WAN change",
                "atomic_from": self._via,
                "atomic_to": atomic_id,
                "ifname": ifname,
            },
        )
        self._via = atomic_id
        return True


class WireGuardTunnel(Tunnel):
    """WireGuard-backed tunnel.

    WS-E owns the implementation: `wg set` to rebind the endpoint to a new
    source interface, handshake monitoring, and fabric health checks. Rebinding
    rather than tearing down is the whole point — the WireGuard session survives
    the underlying path changing, which is what carries client TCP connections
    across a failover.
    """

    def __init__(self, fabric_min: str, interface: str = "wg0"):
        self._fabric_min = fabric_min
        self._interface = interface

    def status(self) -> TunnelStatus:
        return TunnelStatus(TunnelState.DOWN, None)

    def bind_to(self, atomic_id: str, ifname: str) -> bool:
        log.warning(
            "Tunnel binding not implemented",
            extra={
                "workflow": "tunnel_migrate",
                "state": "skipped",
                "intent": "keep client sessions alive across a WAN change",
                "atomic_id": atomic_id,
                "ifname": ifname,
                "reason": "WireGuard management is WS-E scope; see docs/roadmap.md",
            },
        )
        return False
