from __future__ import annotations

import base64

import pytest

from fabric import MIN_APPLIANCE_VERSION, __version__
from fabric.app import create_app
from fabric.config import FabricConfig
from fabric.peers import PeerRegistry
from fabric.wireguard import WireGuardError

_CONFIG = FabricConfig(address="fabric.example.com:51820", username="admin", password="hunter2")


def _basic_auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


_AUTH = _basic_auth_header("admin", "hunter2")


class _FakeWireGuard:
    """Stands in for FabricWireGuard so /register runs without wg or root."""

    def __init__(self, *, ready_error=None, peer_error=None):
        self.public_key = "FABRICPUBKEY"
        self._ready_error = ready_error
        self._peer_error = peer_error
        self.peers: list[tuple[str, str]] = []

    def ensure_ready(self) -> None:
        if self._ready_error:
            raise self._ready_error

    def add_peer(self, public_key: str, address: str) -> None:
        if self._peer_error:
            raise self._peer_error
        self.peers.append((public_key, address))


def _client(tmp_path, wireguard=None):
    registry = PeerRegistry(path=tmp_path / "peers.json")
    app = create_app(
        config=_CONFIG,
        registry=registry,
        wireguard=wireguard or _FakeWireGuard(),
    )
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def client(tmp_path):
    return _client(tmp_path)


# --- auth and version (unchanged behaviour, still gated before registration) --


def test_health_requires_no_auth(client) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["address"] == "fabric.example.com:51820"
    assert body["version"] == __version__
    assert body["min_appliance_version"] == MIN_APPLIANCE_VERSION


def test_register_rejects_missing_credentials(client) -> None:
    resp = client.post("/register", json={"version": "0.1.0", "public_key": "x"})
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith("Basic")


def test_register_rejects_wrong_credentials(client) -> None:
    resp = client.post(
        "/register",
        json={"version": "0.1.0", "public_key": "x"},
        headers=_basic_auth_header("admin", "wrong-password"),
    )
    assert resp.status_code == 401


def test_register_requires_public_key(client) -> None:
    resp = client.post("/register", json={"version": "0.1.0"}, headers=_AUTH)
    assert resp.status_code == 400


def test_register_still_validates_version_once_authenticated(client) -> None:
    resp = client.post(
        "/register",
        json={"version": "0.0.1", "public_key": "x"},
        headers=_AUTH,
    )
    assert resp.status_code == 409


# --- real registration ------------------------------------------------------


def test_register_allocates_address_and_returns_tunnel_params(client) -> None:
    resp = client.post(
        "/register",
        json={"version": "0.1.0", "public_key": "APPLIANCEPUB"},
        headers=_AUTH,
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["assigned_address"] == "10.99.0.2/32"
    assert body["fabric_public_key"] == "FABRICPUBKEY"
    assert body["endpoint"] == "fabric.example.com:51820"
    assert body["tunnel_pool"] == "10.99.0.0/24"


def test_register_is_idempotent_for_a_known_appliance(client) -> None:
    payload = {"version": "0.1.0", "public_key": "APPLIANCEPUB"}
    first = client.post("/register", json=payload, headers=_AUTH).get_json()
    second = client.post("/register", json=payload, headers=_AUTH).get_json()
    assert first["assigned_address"] == second["assigned_address"] == "10.99.0.2/32"


def test_register_503_when_tunnel_backend_unavailable(tmp_path) -> None:
    wg = _FakeWireGuard(ready_error=WireGuardError("Operation not permitted"))
    client = _client(tmp_path, wireguard=wg)
    resp = client.post(
        "/register",
        json={"version": "0.1.0", "public_key": "APPLIANCEPUB"},
        headers=_AUTH,
    )
    assert resp.status_code == 503
    assert "unavailable" in resp.get_json()["error"]


def test_register_503_when_peer_add_fails(tmp_path) -> None:
    wg = _FakeWireGuard(peer_error=WireGuardError("wg set failed"))
    client = _client(tmp_path, wireguard=wg)
    resp = client.post(
        "/register",
        json={"version": "0.1.0", "public_key": "APPLIANCEPUB"},
        headers=_AUTH,
    )
    assert resp.status_code == 503
