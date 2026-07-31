"""The stable tunnel and the server fabric.

Client sessions terminate at a fabric server rather than at the WAN, so the
client-visible IP never changes when a WAN does. That is what makes a WAN swap
survivable instead of a visible outage — the tunnel is not an enhancement to
failover, it is the mechanism that makes failover invisible (ADR-005).

WS-E owns this module. It ships the interface, the version-floor check, a mock,
and the real WireGuard-backed tunnel: handshake-based status and path migration
by rerouting the fabric endpoint onto the active WAN.
"""

from __future__ import annotations

import enum
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol

from dirty.logging import get_logger

log = get_logger("tunnel")

#: WireGuard rekeys roughly every 120 s (REKEY_AFTER_TIME). A peer that has not
#: completed a handshake in this long is treated as DOWN — the margin over 120 s
#: avoids flapping DOWN during a normal rekey gap. See wg(8) and the WireGuard
#: protocol timers.
_HANDSHAKE_STALE_S = 180.0

#: A tunnel command must never hang the fast loop. `wg`/`ip` are local and cheap;
#: if one blocks this long something is badly wrong and returning is safer than
#: waiting.
_CMD_TIMEOUT_S = 5.0


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


def _run(argv: list[str], timeout: float = _CMD_TIMEOUT_S) -> str | None:
    """Run a tunnel command, returning stdout on success or None on failure.

    Never raises: a control-plane crash must not take the tunnel — or the
    network — down (ADR-008). Callers that only care about success test the
    result with ``is not None`` (a successful command with empty stdout returns
    ``""``, which is falsy).
    """
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning(
            "Tunnel command failed to execute",
            extra={
                "workflow": "tunnel_command",
                "state": "failed",
                "intent": " ".join(argv[:2]),
                "argv": argv,
                "reason": "could not spawn process",
                "error": str(exc),
            },
        )
        return None
    if done.returncode != 0:
        log.warning(
            "Tunnel command returned non-zero",
            extra={
                "workflow": "tunnel_command",
                "state": "failed",
                "intent": " ".join(argv[:2]),
                "argv": argv,
                "returncode": done.returncode,
                "reason": (done.stderr or "").strip()[:200],
            },
        )
        return None
    return done.stdout


@dataclass(frozen=True, slots=True)
class PeerDump:
    """One peer row from ``wg show <if> dump``."""

    public_key: str
    endpoint: str | None
    #: Unix epoch seconds of the most recent handshake; 0 means "never".
    latest_handshake: int


def parse_wg_dump(output: str) -> list[PeerDump]:
    """Parse ``wg show <if> dump`` into peer rows.

    The dump format is tab-separated and machine-stable. The first line
    describes the interface (private-key, public-key, listen-port, fwmark); each
    subsequent line is a peer: public-key, preshared-key, endpoint, allowed-ips,
    latest-handshake, transfer-rx, transfer-tx, persistent-keepalive. We only
    need the public key, endpoint, and handshake time. Malformed lines are
    skipped rather than raising — a parse error must not stall the fast loop.
    """
    peers: list[PeerDump] = []
    lines = output.splitlines()
    # First non-empty line is the interface row; peers follow.
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) < 5:
            continue
        endpoint = fields[2] if fields[2] not in ("", "(none)") else None
        try:
            handshake = int(fields[4])
        except ValueError:
            handshake = 0
        peers.append(PeerDump(public_key=fields[0], endpoint=endpoint, latest_handshake=handshake))
    return peers


def endpoint_host(endpoint: str) -> str | None:
    """Extract the bare host/IP from a WireGuard ``host:port`` endpoint.

    Handles both ``203.0.113.5:51820`` and the bracketed IPv6 form
    ``[2001:db8::1]:51820``.
    """
    endpoint = endpoint.strip()
    if not endpoint:
        return None
    if endpoint.startswith("["):
        host, _, _ = endpoint[1:].partition("]")
        return host or None
    host = endpoint.rsplit(":", 1)[0]
    return host or None


def _default_gateway(ifname: str) -> str | None:
    """The next-hop gateway for the default route on ``ifname``, if any.

    A routed WAN (Wi-Fi, USB Ethernet) has a gateway; a point-to-point link
    (some cellular/USB tethers) may not, in which case the caller routes
    on-link via the device alone.
    """
    out = _run(["ip", "-o", "route", "show", "default", "dev", ifname])
    if not out:
        return None
    for line in out.splitlines():
        fields = line.split()
        if "via" in fields:
            return fields[fields.index("via") + 1]
    return None


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

    Rebinding rather than tearing down is the whole point: the WireGuard session
    survives the underlying WAN path changing, which is what carries client TCP
    connections across a failover (ADR-005). WireGuard itself does not care which
    interface it egresses on — it only cares that packets to the fabric endpoint
    take the right path. So a WAN change is expressed as a *host route*: pin the
    fabric endpoint's address to the chosen atomic's interface/gateway. The
    encrypted session, its keys, and every client connection riding inside it are
    untouched.

    This module shells out to ``wg`` and ``ip route`` directly rather than going
    through ``enforce/``. The enforce-only rule for ``tc``/``nft``/``ip`` governs
    the *forwarding* reconciliation plane; tunnel path-migration is WS-E's own
    kernel surface (see docs/architecture.md, which lists path migration under
    ``tunnel/``). The host route installed here is a single, additive
    ``route replace`` — never a teardown (ADR-008).

    Identity is carried by ``atomic_id`` (ADR-002); the ``ifname`` is read at the
    moment of use and never persisted as identity.
    """

    def __init__(
        self,
        fabric_min: str,
        interface: str = "wg0",
        server: FabricServer | None = None,
    ):
        self._fabric_min = fabric_min
        self._interface = interface
        #: The fabric this tunnel connects to, if known. Set by the daemon from a
        #: /health probe so status() can report INCOMPATIBLE on version skew.
        self._server = server
        #: Current carrying atomic — stable identity, not an ifname (ADR-002).
        self._via_atomic_id: str | None = None

    def set_server(self, server: FabricServer | None) -> None:
        """Record which fabric the tunnel is attached to (drives compatibility)."""
        self._server = server

    def _peer_endpoint_host(self) -> str | None:
        out = _run(["wg", "show", self._interface, "dump"])
        if not out:
            return None
        for peer in parse_wg_dump(out):
            if peer.endpoint:
                return endpoint_host(peer.endpoint)
        return None

    def status(self) -> TunnelStatus:
        # Version skew is reported before anything else: a clear INCOMPATIBLE is
        # far easier to diagnose in the field than a tunnel that half-works.
        if self._server is not None and not fabric_compatible(
            self._server.version, self._fabric_min
        ):
            return TunnelStatus(TunnelState.INCOMPATIBLE, self._server, self._via_atomic_id)

        out = _run(["wg", "show", self._interface, "dump"])
        if out is None:
            # No interface, or wg unavailable — the tunnel is not up.
            return TunnelStatus(TunnelState.DOWN, self._server, self._via_atomic_id)

        peers = parse_wg_dump(out)
        latest = max((p.latest_handshake for p in peers), default=0)
        age_s = (time.time() - latest) if latest > 0 else None

        if age_s is not None and age_s <= _HANDSHAKE_STALE_S:
            state = TunnelState.UP
        elif self._via_atomic_id is not None:
            # We have bound a WAN but the handshake is stale or has not landed
            # yet — the session is (re)establishing over the new path.
            state = TunnelState.CONNECTING
        else:
            state = TunnelState.DOWN

        return TunnelStatus(
            state,
            self._server,
            self._via_atomic_id,
            last_handshake_s=age_s,
        )

    def bind_to(self, atomic_id: str, ifname: str) -> bool:
        """Move the tunnel's egress onto ``ifname`` without dropping the session.

        Installs a host route for the fabric endpoint via the chosen WAN. Returns
        True when the route is in place (or already was), False on any failure —
        in which case nothing is torn down and the previous path stays as it was.
        """
        started = time.monotonic()
        if atomic_id == self._via_atomic_id:
            return True

        endpoint = self._peer_endpoint_host()
        if not endpoint:
            log.warning(
                "Cannot rebind tunnel: fabric endpoint unknown",
                extra={
                    "workflow": "tunnel_migrate",
                    "state": "failed",
                    "intent": "keep client sessions alive across a WAN change",
                    "atomic_from": self._via_atomic_id,
                    "atomic_to": atomic_id,
                    "ifname": ifname,
                    "reason": "wg reported no peer endpoint; is the tunnel configured?",
                },
            )
            return False

        gateway = _default_gateway(ifname)
        route = ["ip", "route", "replace", f"{endpoint}/32"]
        if gateway:
            route += ["via", gateway]
        route += ["dev", ifname]

        if _run(route) is None:
            log.warning(
                "Tunnel rebind failed",
                extra={
                    "workflow": "tunnel_migrate",
                    "state": "failed",
                    "intent": "keep client sessions alive across a WAN change",
                    "atomic_from": self._via_atomic_id,
                    "atomic_to": atomic_id,
                    "ifname": ifname,
                    "endpoint": endpoint,
                    "gateway": gateway,
                    "reason": "ip route replace did not apply; leaving previous path in place",
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                },
            )
            return False

        previous = self._via_atomic_id
        self._via_atomic_id = atomic_id
        log.info(
            "Tunnel moved to a different WAN",
            extra={
                "workflow": "tunnel_migrate",
                "state": "completed",
                "intent": "keep client sessions alive across a WAN change",
                "atomic_from": previous,
                "atomic_to": atomic_id,
                "ifname": ifname,
                "endpoint": endpoint,
                "gateway": gateway,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            },
        )
        return True
