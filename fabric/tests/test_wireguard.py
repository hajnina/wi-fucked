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


def test_add_peer_pins_a_single_allowed_ip(tmp_path, monkeypatch):
    responder = _Responder([])
    monkeypatch.setattr(wg_mod, "_run", responder)
    _wg(tmp_path).add_peer("APPLIANCEPUB", "10.99.0.7")
    assert responder.calls[-1] == [
        "wg",
        "set",
        "wg0",
        "peer",
        "APPLIANCEPUB",
        "allowed-ips",
        "10.99.0.7/32",
    ]


def test_missing_wg_binary_becomes_wireguard_error(monkeypatch):
    def raise_oserror(*args, **kwargs):
        raise OSError("wg: command not found")

    # Real _run wrapping a subprocess that cannot spawn -> WireGuardError.
    monkeypatch.setattr(wg_mod.subprocess, "run", raise_oserror)
    with pytest.raises(WireGuardError):
        wg_mod.generate_private_key()
