"""Tunnel-address allocation and the persistent peer registry.

Every appliance that registers is handed a stable address from a small private
pool (``10.99.0.0/24`` by default; ``.1`` is the fabric's own wg0 address, so
appliances get ``.2`` upward). The mapping public-key -> address is persisted as
JSON — small, rarely written, human-readable, the same convention the appliance
uses for its own state (ADR-010). When the registry is on a mounted volume a
re-registering appliance keeps its address across fabric restarts.

Two gunicorn workers can call :meth:`PeerRegistry.allocate` at the same instant,
and a silent double-allocation would hand two appliances the same tunnel IP. The
read-modify-write is therefore guarded twice:

* a process-local ``threading.Lock`` serialises threads inside one worker, and
* an ``fcntl.flock`` on a sidecar lock file serialises across worker processes.

``fcntl`` is POSIX-only; on a non-POSIX dev machine it is absent and only the
threading lock applies, which is sufficient there because the multi-process
gunicorn case does not arise. Production is Linux.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows dev only; production is Linux
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger("fabric.peers")

DEFAULT_POOL = os.getenv("FABRIC_TUNNEL_POOL", "10.99.0.0/24")
DEFAULT_REGISTRY_PATH = Path(os.getenv("FABRIC_PEER_REGISTRY", "/var/lib/fabric/peers.json"))

#: The fabric server itself takes the first host address in the pool; appliances
#: are allocated from the next one upward.
_SERVER_HOST_OFFSET = 1

_process_lock = threading.Lock()


class PoolExhausted(RuntimeError):
    """No free address remains in the tunnel pool."""


@dataclass(frozen=True, slots=True)
class Allocation:
    public_key: str
    #: Bare host address, e.g. ``10.99.0.2`` (no prefix).
    address: str
    #: True when freshly allocated, False when the public key was already known.
    created: bool


class PeerRegistry:
    def __init__(
        self,
        path: Path | str = DEFAULT_REGISTRY_PATH,
        pool: str = DEFAULT_POOL,
    ) -> None:
        self._path = Path(path)
        self._network = ipaddress.ip_network(pool)
        self._lock_path = self._path.with_name(self._path.name + ".lock")

    @property
    def server_address(self) -> str:
        """The fabric's own address on the tunnel (``.1`` of the pool)."""
        return str(self._network.network_address + _SERVER_HOST_OFFSET)

    @property
    def pool_cidr(self) -> str:
        return str(self._network)

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            # A corrupt registry is logged, not fatal — but we must not silently
            # start reissuing addresses that are already in use, so callers that
            # cannot tolerate this should back it up. For MVP we start empty and
            # log loudly.
            log.error(
                "Peer registry unreadable; starting from empty",
                extra={"workflow": "peer_alloc", "state": "failed", "path": str(self._path)},
            )
            return {}
        peers = data.get("peers", {})
        return peers if isinstance(peers, dict) else {}

    def _store(self, peers: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps({"peers": peers}, indent=2, sort_keys=True))
        os.replace(tmp, self._path)  # atomic on POSIX and Windows

    def _next_free(self, peers: dict[str, str]) -> str:
        used = set(peers.values())
        used.add(self.server_address)
        for host in self._network.hosts():
            candidate = str(host)
            if candidate not in used:
                return candidate
        raise PoolExhausted(f"tunnel pool {self._network} has no free address")

    @contextlib.contextmanager
    def _guard(self):
        with _process_lock:
            if fcntl is None:
                yield
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._lock_path, "w") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)

    def allocate(self, public_key: str) -> Allocation:
        """Return this public key's tunnel address, allocating one if needed.

        Idempotent: a public key that is already known gets its existing address
        back, so re-registration (after a fabric restart, say) is safe and does
        not consume a second address.
        """
        with self._guard():
            peers = self._load()
            existing = peers.get(public_key)
            if existing is not None:
                return Allocation(public_key, existing, created=False)
            address = self._next_free(peers)
            peers[public_key] = address
            self._store(peers)
            log.info(
                "Allocated tunnel address",
                extra={
                    "workflow": "peer_alloc",
                    "state": "completed",
                    "address": address,
                    "peer_count": len(peers),
                },
            )
            return Allocation(public_key, address, created=True)
