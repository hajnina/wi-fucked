"""Fabric-side WireGuard control: the server's own identity, its ``wg0``
interface, and adding appliance peers.

Touching the interface needs ``NET_ADMIN``. When the capability is missing (or
the host has no WireGuard support) these functions raise :class:`WireGuardError`
with a clear message, so ``/register`` can answer ``503`` instead of the
container crashing.

**MVP identity persistence.** The server private key is read from
``FABRIC_WG_PRIVATE_KEY_FILE`` when that file exists; otherwise it is generated
once per process and best-effort written back there. Without a mounted volume
for that path, a container restart changes the fabric's public key and every
appliance must re-register. That is a documented limitation, not a bug — see
``fabric/README.md``. Persisting identity properly is a mounted-volume
deployment concern, deliberately out of MVP scope.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("fabric.wireguard")

DEFAULT_INTERFACE = os.getenv("FABRIC_WG_INTERFACE", "wg0")
DEFAULT_KEY_FILE = Path(os.getenv("FABRIC_WG_PRIVATE_KEY_FILE", "/var/lib/fabric/wg-privatekey"))
DEFAULT_LISTEN_PORT = int(os.getenv("FABRIC_WG_LISTEN_PORT", "51820"))

_CMD_TIMEOUT_S = 5.0


class WireGuardError(RuntimeError):
    """A ``wg``/``ip`` command failed — typically missing NET_ADMIN or kernel
    WireGuard support."""


def _run(argv: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=_CMD_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # tool absent, timeout
        raise WireGuardError(f"could not run {' '.join(argv[:2])}: {exc}") from exc


def _checked(argv: list[str], *, stdin: str | None = None, tolerate: str | None = None) -> str:
    """Run a command, raising WireGuardError on failure.

    ``tolerate`` is a substring of stderr that means "already in the desired
    state" (e.g. ``File exists`` from ``ip link add`` on a second call), making
    interface setup idempotent.
    """
    done = _run(argv, stdin=stdin)
    if done.returncode != 0:
        stderr = (done.stderr or "").strip()
        if tolerate and tolerate in stderr:
            return done.stdout
        detail = stderr or f"exit {done.returncode}"
        raise WireGuardError(f"{' '.join(argv[:3])} failed: {detail}")
    return done.stdout


def generate_private_key() -> str:
    return _checked(["wg", "genkey"]).strip()


def public_key_of(private_key: str) -> str:
    return _checked(["wg", "pubkey"], stdin=private_key).strip()


class FabricWireGuard:
    """Manages one WireGuard interface for the fabric server."""

    def __init__(
        self,
        address: str,
        pool_cidr: str,
        interface: str = DEFAULT_INTERFACE,
        key_file: Path | str = DEFAULT_KEY_FILE,
        listen_port: int = DEFAULT_LISTEN_PORT,
    ) -> None:
        self._interface = interface
        self._address = address
        self._prefix = pool_cidr.split("/", 1)[1] if "/" in pool_cidr else "24"
        self._key_file = Path(key_file)
        self._listen_port = listen_port
        self._private_key: str | None = None
        self._public_key: str | None = None
        self._ready = False

    def _load_or_create_key(self) -> str:
        if self._private_key is not None:
            return self._private_key
        try:
            existing = self._key_file.read_text().strip()
        except OSError:
            existing = ""
        if existing:
            self._private_key = existing
            return existing

        key = generate_private_key()
        # Best-effort persistence. If the path is not writable (non-root user, no
        # mounted volume) the key lives only for this process — documented.
        try:
            self._key_file.parent.mkdir(parents=True, exist_ok=True)
            self._key_file.write_text(key + "\n")
            self._key_file.chmod(0o600)
        except OSError as exc:
            log.warning(
                "Fabric WireGuard key not persisted; identity is process-lived",
                extra={"workflow": "wg_identity", "state": "skipped", "reason": str(exc)},
            )
        self._private_key = key
        return key

    @property
    def public_key(self) -> str:
        if self._public_key is None:
            self._public_key = public_key_of(self._load_or_create_key())
        return self._public_key

    @property
    def listen_port(self) -> int:
        return self._listen_port

    def ensure_ready(self) -> None:
        """Bring ``wg0`` up with the server's key and address. Idempotent.

        Raises WireGuardError if the interface cannot be configured (e.g. no
        NET_ADMIN). Cheap and safe to call on every registration.
        """
        if self._ready:
            return

        private_key = self._load_or_create_key()

        _checked(
            ["ip", "link", "add", "dev", self._interface, "type", "wireguard"],
            tolerate="File exists",
        )

        # The private key is fed via a mode-0600 temp file rather than the command
        # line, so it never appears in the process table or in a log.
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write(private_key)
            key_path = handle.name
        try:
            os.chmod(key_path, 0o600)
            _checked(
                [
                    "wg",
                    "set",
                    self._interface,
                    "private-key",
                    key_path,
                    "listen-port",
                    str(self._listen_port),
                ]
            )
        finally:
            with contextlib.suppress(OSError):
                os.unlink(key_path)

        _checked(
            ["ip", "address", "add", f"{self._address}/{self._prefix}", "dev", self._interface],
            tolerate="File exists",
        )
        _checked(["ip", "link", "set", "up", "dev", self._interface])
        self._ready = True
        log.info(
            "Fabric WireGuard interface ready",
            extra={
                "workflow": "wg_setup",
                "state": "completed",
                "interface": self._interface,
                "address": self._address,
                "listen_port": self._listen_port,
            },
        )

    def add_peer(self, public_key: str, address: str) -> None:
        """Add (or update) an appliance peer, pinned to a single tunnel address."""
        _checked(["wg", "set", self._interface, "peer", public_key, "allowed-ips", f"{address}/32"])
        log.info(
            "Added WireGuard peer",
            extra={
                "workflow": "peer_add",
                "state": "completed",
                "interface": self._interface,
                "allowed_ip": f"{address}/32",
            },
        )
