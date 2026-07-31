"""WireGuardTunnel: dump parsing, status derivation, and path rebinding.

No real WireGuard or root: `dirty.tunnel._run` (the only thing that shells out)
is monkeypatched with a scripted responder, the same seam hal/linux.py uses.
"""

from __future__ import annotations

import dirty.tunnel as tunnel_mod
from dirty.tunnel import (
    FabricServer,
    TunnelState,
    WireGuardTunnel,
    endpoint_host,
    parse_wg_dump,
)

# A realistic `wg show wg0 dump`: interface line, then one peer line. Fields are
# tab-separated; peer columns are pubkey, psk, endpoint, allowed-ips,
# latest-handshake, rx, tx, keepalive.
_IFACE = "AAAA=\tBBBB=\t51820\toff"
_PEER = "CCCC=\t(none)\t203.0.113.5:51820\t10.99.0.2/32\t{handshake}\t1024\t2048\t25"


def _dump(handshake: int) -> str:
    return _IFACE + "\n" + _PEER.format(handshake=handshake)


class _Responder:
    """Maps an argv prefix to a stdout string (or None to simulate failure)."""

    def __init__(self, responses: dict[tuple[str, ...], str | None]):
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=5.0):
        self.calls.append(argv)
        for prefix, out in self.responses.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return out
        return None


def _install(monkeypatch, responder: _Responder) -> None:
    monkeypatch.setattr(tunnel_mod, "_run", responder)


# --- pure parsing -----------------------------------------------------------


def test_parse_wg_dump_extracts_peer_fields():
    peers = parse_wg_dump(_dump(1_700_000_000))
    assert len(peers) == 1
    assert peers[0].public_key == "CCCC="
    assert peers[0].endpoint == "203.0.113.5:51820"
    assert peers[0].latest_handshake == 1_700_000_000


def test_parse_wg_dump_handles_no_peers_and_junk():
    assert parse_wg_dump(_IFACE) == []
    assert parse_wg_dump("") == []
    assert parse_wg_dump(_IFACE + "\ngarbage") == []  # <5 fields -> skipped


def test_endpoint_host_ipv4_and_ipv6():
    assert endpoint_host("203.0.113.5:51820") == "203.0.113.5"
    assert endpoint_host("[2001:db8::1]:51820") == "2001:db8::1"
    assert endpoint_host("") is None


# --- status -----------------------------------------------------------------


def test_status_up_on_recent_handshake(monkeypatch):
    monkeypatch.setattr(tunnel_mod.time, "time", lambda: 1_700_000_030)
    _install(monkeypatch, _Responder({("wg", "show"): _dump(1_700_000_000)}))
    tun = WireGuardTunnel(fabric_min="0.1.0")
    status = tun.status()
    assert status.state is TunnelState.UP
    assert status.last_handshake_s == 30


def test_status_down_on_stale_handshake_before_any_bind(monkeypatch):
    monkeypatch.setattr(tunnel_mod.time, "time", lambda: 1_700_000_000 + 500)
    _install(monkeypatch, _Responder({("wg", "show"): _dump(1_700_000_000)}))
    tun = WireGuardTunnel(fabric_min="0.1.0")
    assert tun.status().state is TunnelState.DOWN


def test_status_connecting_when_bound_but_handshake_stale(monkeypatch):
    monkeypatch.setattr(tunnel_mod.time, "time", lambda: 1_700_000_000 + 500)
    responder = _Responder(
        {
            ("wg", "show"): _dump(1_700_000_000),
            ("ip", "-o", "route"): "default via 192.168.1.1 dev wlan0",
            ("ip", "route", "replace"): "",
        }
    )
    _install(monkeypatch, responder)
    tun = WireGuardTunnel(fabric_min="0.1.0")
    assert tun.bind_to("wifi-hotel", "wlan0") is True
    assert tun.status().state is TunnelState.CONNECTING


def test_status_down_when_wg_unavailable(monkeypatch):
    _install(monkeypatch, _Responder({("wg", "show"): None}))
    tun = WireGuardTunnel(fabric_min="0.1.0")
    assert tun.status().state is TunnelState.DOWN


def test_status_incompatible_on_version_skew(monkeypatch):
    _install(monkeypatch, _Responder({("wg", "show"): _dump(1_700_000_000)}))
    old = FabricServer(name="f", endpoint="f:51820", public_key="k", version="0.0.9")
    tun = WireGuardTunnel(fabric_min="0.1.0", server=old)
    # Incompatible short-circuits before any handshake check.
    assert tun.status().state is TunnelState.INCOMPATIBLE


# --- bind_to (path migration) ----------------------------------------------


def test_bind_to_installs_host_route_via_gateway(monkeypatch):
    responder = _Responder(
        {
            ("wg", "show"): _dump(1_700_000_000),
            ("ip", "-o", "route"): "default via 192.168.1.1 dev wlan0",
            ("ip", "route", "replace"): "",
        }
    )
    _install(monkeypatch, responder)
    tun = WireGuardTunnel(fabric_min="0.1.0")

    assert tun.bind_to("wifi-hotel", "wlan0") is True

    route_calls = [c for c in responder.calls if c[:3] == ["ip", "route", "replace"]]
    assert route_calls == [
        ["ip", "route", "replace", "203.0.113.5/32", "via", "192.168.1.1", "dev", "wlan0"]
    ]
    assert tun.status().via_atomic_id == "wifi-hotel"


def test_bind_to_without_gateway_routes_on_link(monkeypatch):
    responder = _Responder(
        {
            ("wg", "show"): _dump(1_700_000_000),
            ("ip", "-o", "route"): "",  # point-to-point link, no gateway
            ("ip", "route", "replace"): "",
        }
    )
    _install(monkeypatch, responder)
    tun = WireGuardTunnel(fabric_min="0.1.0")

    assert tun.bind_to("phone-usb", "usb0") is True
    route_calls = [c for c in responder.calls if c[:3] == ["ip", "route", "replace"]]
    assert route_calls == [["ip", "route", "replace", "203.0.113.5/32", "dev", "usb0"]]


def test_bind_to_is_idempotent_noop_for_same_atomic(monkeypatch):
    responder = _Responder(
        {
            ("wg", "show"): _dump(1_700_000_000),
            ("ip", "-o", "route"): "default via 192.168.1.1 dev wlan0",
            ("ip", "route", "replace"): "",
        }
    )
    _install(monkeypatch, responder)
    tun = WireGuardTunnel(fabric_min="0.1.0")
    tun.bind_to("wifi-hotel", "wlan0")
    before = len(responder.calls)
    assert tun.bind_to("wifi-hotel", "wlan0") is True
    assert responder.calls[before:] == []  # no further commands run


def test_bind_to_fails_when_endpoint_unknown(monkeypatch):
    # wg has no peer endpoint yet -> cannot pin a route.
    _install(monkeypatch, _Responder({("wg", "show"): _IFACE}))
    tun = WireGuardTunnel(fabric_min="0.1.0")
    assert tun.bind_to("wifi-hotel", "wlan0") is False
    assert tun.status().via_atomic_id is None


def test_bind_to_leaves_previous_path_on_route_failure(monkeypatch):
    responder = _Responder(
        {
            ("wg", "show"): _dump(1_700_000_000),
            ("ip", "-o", "route"): "default via 192.168.1.1 dev wlan0",
            ("ip", "route", "replace"): None,  # route command fails
        }
    )
    _install(monkeypatch, responder)
    tun = WireGuardTunnel(fabric_min="0.1.0")
    tun.bind_to("wifi-hotel", "wlan0")  # first move succeeds? no — replace fails
    assert tun.status().via_atomic_id is None
