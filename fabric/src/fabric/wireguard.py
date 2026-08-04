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
import os
import subprocess
import tempfile
from pathlib import Path

from fabric.logging import get_logger

log = get_logger("wireguard")

DEFAULT_INTERFACE = os.getenv("FABRIC_WG_INTERFACE", "wg0")
DEFAULT_KEY_FILE = Path(os.getenv("FABRIC_WG_PRIVATE_KEY_FILE", "/var/lib/fabric/wg-privatekey"))
DEFAULT_LISTEN_PORT = int(os.getenv("FABRIC_WG_LISTEN_PORT", "51820"))

#: Name of the nftables table this module owns for NAT (ADR-019), mirroring
#: `wifucked.enforce`'s single-table-per-owner pattern so the whole ruleset
#: can be replaced atomically without touching anything else on the box.
_NAT_TABLE = "fabric_nat"

#: LAN client traffic forwarded through a peer's tunnel carries the *client's*
#: private address, not the peer's own `/32` tunnel address — WireGuard's
#: crypto-routing validates a decrypted packet's source against the sending
#: peer's `allowed-ips`, so a peer pinned to only its own `/32` would have
#: every LAN client's packet silently dropped the moment it left the LAN
#: (found by the QEMU packet-routing proof in
#: `appliance/tests/qemu/`, not by inspection — see the PR body). Every
#: appliance's LAN is RFC1918 by construction (`hostapd`/`dnsmasq` only ever
#: hand out private addresses), so each peer is additionally allowed the
#: full RFC1918 space rather than a specific subnet the fabric would
#: otherwise have to be told about per-appliance.
_RFC1918_RANGES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")

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
        self._pool_cidr = pool_cidr
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
        self._route_rfc1918_via_wireguard()
        self._enable_forwarding_and_nat()
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

    def _route_rfc1918_via_wireguard(self) -> None:
        """Give the kernel a reason to ever hand a packet to ``wg0`` at all.

        ``wg set ... allowed-ips`` (``add_peer``) configures WireGuard's own
        *internal* crypto-routing — which peer a packet gets encrypted for,
        and which source addresses are accepted from a peer on decrypt. It
        does **not** touch the kernel's ordinary routing table; that's what
        ``wg-quick`` normally does on top of plain ``wg``, and this class
        deliberately shells out to bare ``wg`` (see the module docstring).

        Without an explicit route, a reply to a LAN client behind an
        appliance — address space is RFC1918, per `add_peer`'s widened
        `allowed-ips` — has nowhere to go: `ip route`'s lookup finds no
        match for it, so the kernel drops it before WireGuard's own peer
        selection ever gets a chance to run. Found by the QEMU
        packet-routing proof (`appliance/tests/qemu/`) as "WireGuard
        receives and decrypts the appliance's packet fine, but never sends
        anything back" — a route, not a crypto or NAT problem, was the
        actual missing piece.

        One route per RFC1918 block, all pointed at this device with no
        gateway (WireGuard doesn't use one — `wg0`'s own transmit path picks
        the peer). Additive and idempotent (`route replace`), matching this
        module's existing conventions and ADR-008 (appliance-side; the
        fabric has no all-encompassing "never tear down" ADR of its own but
        follows the same instinct — nothing here removes a route).
        """
        for cidr in _RFC1918_RANGES:
            _checked(["ip", "route", "replace", cidr, "dev", self._interface])

    def _enable_forwarding_and_nat(self) -> None:
        """Turn this server into an actual gateway for tunnel-peer traffic.

        ADR-019: LAN client egress on the appliance side rides this tunnel
        all the way to the Internet now, not just tunnel-management traffic.
        Without this, ``add_peer``'s ``allowed-ips`` lets a peer's packets
        *arrive* on ``wg0``, but the kernel would never forward them onward —
        every LAN client behind an attached appliance would be silently
        unreachable past the fabric.

        Both steps are additive/idempotent, consistent with the rest of this
        module and with ADR-008 (never a teardown path): re-enabling
        forwarding is a no-op if already on, and the nftables ruleset is
        declared via the same table-flush-repopulate idiom
        `wifucked.enforce` uses, inside one atomic ``nft -f -`` transaction,
        so masquerading is never briefly absent.
        """
        _checked(["sysctl", "-w", "net.ipv4.ip_forward=1"])

        # Masquerades on the full RFC1918 space, not just the tunnel pool: a
        # LAN client's forwarded packet carries the client's own private
        # address (see `add_peer`'s docstring for why `allowed-ips` had to
        # widen to match) — matching only `self._pool_cidr` here would leave
        # every such packet un-NATed and therefore unrouteable back once it
        # left the fabric's own WAN. `self._pool_cidr` (e.g. 10.99.0.0/24)
        # is itself always inside 10.0.0.0/8, so it is deliberately not
        # listed a second time — nft rejects overlapping literal prefixes in
        # one anonymous set.
        saddr_set = ", ".join(_RFC1918_RANGES)
        ruleset = (
            f"table ip {_NAT_TABLE}\n"
            f"flush table ip {_NAT_TABLE}\n"
            f"table ip {_NAT_TABLE} {{\n"
            "    chain postrouting {\n"
            "        type nat hook postrouting priority srcnat; policy accept;\n"
            f'        ip saddr {{ {saddr_set} }} oifname != "{self._interface}" masquerade\n'
            "    }\n"
            "}\n"
        )
        _checked(["nft", "-f", "-"], stdin=ruleset)
        log.info(
            "Fabric forwarding and NAT enabled",
            extra={
                "workflow": "wg_setup",
                "state": "completed",
                "intent": "forward and masquerade tunnel-peer traffic egressing the fabric's WAN",
                "interface": self._interface,
                "pool_cidr": self._pool_cidr,
            },
        )

    def add_peer(self, public_key: str, address: str) -> None:
        """Add (or update) an appliance peer.

        ``allowed-ips`` covers the peer's own tunnel address (needed for the
        appliance's own control-plane traffic, e.g. re-registration) plus the
        full RFC1918 space (needed so the *LAN clients behind* that
        appliance aren't dropped by crypto-routing — see `_RFC1918_RANGES`).

        Residual risk, accepted for MVP scope (ADR-005: multi-server/Phase 2
        is explicitly later): every peer on this fabric is allowed the same
        RFC1918 space, including the tunnel pool itself, so one peer could
        source-spoof another peer's tunnel address within that space. Return
        traffic for a connection-oriented or conntrack-tracked exchange would
        still route to the genuine owner, not the spoofer, which bounds the
        practical impact — but this is a real, not just theoretical, gap a
        single-tenant-per-fabric deployment doesn't have. Worth hardening
        (e.g. per-peer nftables source validation keyed to the exact assigned
        address for anything client-facing) before a multi-tenant fabric
        ships; out of scope for this PR.
        """
        allowed = ",".join([f"{address}/32", *_RFC1918_RANGES])
        _checked(["wg", "set", self._interface, "peer", public_key, "allowed-ips", allowed])
        log.info(
            "Added WireGuard peer",
            extra={
                "workflow": "peer_add",
                "state": "completed",
                "interface": self._interface,
                "allowed_ip": allowed,
            },
        )
