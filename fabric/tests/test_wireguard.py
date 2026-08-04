"""Fabric WireGuard control, with the subprocess seam mocked.

No real `wg`/`ip` or root: `fabric.wireguard._run` is replaced with a scripted
responder returning fake CompletedProcess results.
"""

from __future__ import annotations

import subprocess

import pytest

import fabric.wireguard as wg_mod
from fabric.wireguard import FabricWireGuard, WireGuardError


def _completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


class _Responder:
    def __init__(self, rules):
        # rules: list of (argv-prefix tuple, CompletedProcess-factory)
        self.rules = rules
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, stdin=None):
        self.calls.append(argv)
        for prefix, factory in self.rules:
            if tuple(argv[: len(prefix)]) == prefix:
                return factory(argv, stdin)
        return _completed(argv)  # default success, empty stdout


def _wg(tmp_path, **kw):
    return FabricWireGuard(
        address="10.99.0.1",
        pool_cidr="10.99.0.0/24",
        key_file=tmp_path / "wg-privatekey",
        **kw,
    )


def test_public_key_generates_and_persists_key(tmp_path, monkeypatch):
    def responder(argv, *, stdin=None):
        if argv[:2] == ["wg", "genkey"]:
            return _completed(argv, stdout="PRIVKEYVALUE\n")
        if argv[:2] == ["wg", "pubkey"]:
            assert stdin == "PRIVKEYVALUE"  # piped, never on the command line
            return _completed(argv, stdout="PUBKEYVALUE\n")
        return _completed(argv)

    monkeypatch.setattr(wg_mod, "_run", responder)
    wg = _wg(tmp_path)
    assert wg.public_key == "PUBKEYVALUE"
    # Persisted with restrictive perms so a mounted volume keeps identity stable.
    assert (tmp_path / "wg-privatekey").read_text().strip() == "PRIVKEYVALUE"


def test_public_key_reuses_existing_key_file(tmp_path, monkeypatch):
    (tmp_path / "wg-privatekey").write_text("EXISTINGPRIV\n")

    def responder(argv, *, stdin=None):
        assert argv[:2] != ["wg", "genkey"], "should not regenerate an existing key"
        if argv[:2] == ["wg", "pubkey"]:
            assert stdin == "EXISTINGPRIV"
            return _completed(argv, stdout="EXISTINGPUB\n")
        return _completed(argv)

    monkeypatch.setattr(wg_mod, "_run", responder)
    assert _wg(tmp_path).public_key == "EXISTINGPUB"


def test_ensure_ready_is_idempotent_on_existing_interface(tmp_path, monkeypatch):
    responder = _Responder(
        [
            (("wg", "genkey"), lambda a, s: _completed(a, stdout="P\n")),
            # ip link add on a second call fails with "File exists" — tolerated.
            (
                ("ip", "link", "add"),
                lambda a, s: _completed(a, returncode=1, stderr="RTNETLINK answers: File exists"),
            ),
            (
                ("ip", "address", "add"),
                lambda a, s: _completed(a, returncode=1, stderr="RTNETLINK answers: File exists"),
            ),
        ]
    )
    monkeypatch.setattr(wg_mod, "_run", responder)
    wg = _wg(tmp_path)
    wg.ensure_ready()  # must not raise despite "File exists"

    ran = [c[:3] for c in responder.calls]
    assert ["ip", "link", "add"] in ran
    assert ["ip", "link", "set"] in ran  # brought up


def test_ensure_ready_raises_without_net_admin(tmp_path, monkeypatch):
    responder = _Responder(
        [
            (("wg", "genkey"), lambda a, s: _completed(a, stdout="P\n")),
            (
                ("ip", "link", "add"),
                lambda a, s: _completed(a, returncode=1, stderr="Operation not permitted"),
            ),
        ]
    )
    monkeypatch.setattr(wg_mod, "_run", responder)
    with pytest.raises(WireGuardError, match="Operation not permitted"):
        _wg(tmp_path).ensure_ready()


def test_ensure_ready_enables_forwarding_and_installs_nat(tmp_path, monkeypatch):
    """ADR-019: LAN client egress rides the tunnel all the way here, so the
    fabric must actually forward and masquerade tunnel-peer traffic, not
    just accept it onto ``wg0``."""
    responder = _Responder([(("wg", "genkey"), lambda a, s: _completed(a, stdout="P\n"))])
    monkeypatch.setattr(wg_mod, "_run", responder)
    _wg(tmp_path).ensure_ready()

    sysctl_call = next(c for c in responder.calls if c[:1] == ["sysctl"])
    assert sysctl_call == ["sysctl", "-w", "net.ipv4.ip_forward=1"]

    nft_call = next(c for c in responder.calls if c[:2] == ["nft", "-f"])
    assert nft_call == ["nft", "-f", "-"]


def test_ensure_ready_routes_rfc1918_via_wireguard(tmp_path, monkeypatch):
    """`wg set ... allowed-ips` alone never gets a reply to a LAN client
    routed onto `wg0` — it configures WireGuard's own internal peer
    selection, not the kernel's ordinary routing table (that's `wg-quick`'s
    job, and this class deliberately uses bare `wg`). Without an explicit
    `ip route`, the kernel has no reason to ever hand a reply packet to
    `wg0` in the first place. Found by the QEMU packet-routing proof
    (`appliance/tests/qemu/`): WireGuard was decrypting the appliance's
    packet fine but never sending anything back, and the actual cause was
    a missing route, not a crypto or NAT bug.
    """
    responder = _Responder([(("wg", "genkey"), lambda a, s: _completed(a, stdout="P\n"))])
    monkeypatch.setattr(wg_mod, "_run", responder)
    _wg(tmp_path).ensure_ready()

    route_calls = [c for c in responder.calls if c[:3] == ["ip", "route", "replace"]]
    routed_cidrs = {c[3] for c in route_calls}
    assert routed_cidrs == {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"}
    assert all(c[-2:] == ["dev", "wg0"] for c in route_calls)


def test_ensure_ready_nat_ruleset_masquerades_the_pool(tmp_path, monkeypatch):
    captured: dict = {}

    def responder(argv, *, stdin=None):
        if argv[:2] == ["nft", "-f"]:
            captured["stdin"] = stdin
        if argv[:2] == ["wg", "genkey"]:
            return _completed(argv, stdout="P\n")
        return _completed(argv)

    monkeypatch.setattr(wg_mod, "_run", responder)
    _wg(tmp_path).ensure_ready()

    ruleset = captured["stdin"]
    assert "table ip fabric_nat" in ruleset
    assert "flush table ip fabric_nat" in ruleset
    assert "type nat hook postrouting" in ruleset
    assert "masquerade" in ruleset
    assert 'oifname != "wg0"' in ruleset
    # RFC1918, not the tunnel pool — a LAN client's forwarded packet carries
    # the client's own private address, not the tunnel pool address (see
    # `add_peer`'s docstring). The pool CIDR itself is a subset of
    # 10.0.0.0/8 and deliberately not listed a second time.
    assert "10.0.0.0/8" in ruleset
    assert "172.16.0.0/12" in ruleset
    assert "192.168.0.0/16" in ruleset
    assert "10.99.0.0/24" not in ruleset


def test_add_peer_allows_the_tunnel_address_and_rfc1918_for_forwarded_lan_traffic(
    tmp_path, monkeypatch
):
    """A peer's `allowed-ips` must cover more than its own `/32`: LAN client
    traffic forwarded through the tunnel carries the *client's* private
    address, and WireGuard's crypto-routing drops any decrypted packet whose
    source isn't in the sending peer's `allowed-ips` (ADR-019 — found via
    the QEMU packet-routing proof in `appliance/tests/qemu/`, which
    initially failed silently at exactly this check).
    """
    responder = _Responder([])
    monkeypatch.setattr(wg_mod, "_run", responder)
    _wg(tmp_path).add_peer("APPLIANCEPUB", "10.99.0.7")
    call = responder.calls[-1]
    assert call[:5] == ["wg", "set", "wg0", "peer", "APPLIANCEPUB"]
    assert call[5] == "allowed-ips"
    allowed = call[6].split(",")
    assert "10.99.0.7/32" in allowed
    assert "10.0.0.0/8" in allowed
    assert "172.16.0.0/12" in allowed
    assert "192.168.0.0/16" in allowed


def test_missing_wg_binary_becomes_wireguard_error(monkeypatch):
    def raise_oserror(*args, **kwargs):
        raise OSError("wg: command not found")

    # Real _run wrapping a subprocess that cannot spawn -> WireGuardError.
    monkeypatch.setattr(wg_mod.subprocess, "run", raise_oserror)
    with pytest.raises(WireGuardError):
        wg_mod.generate_private_key()
