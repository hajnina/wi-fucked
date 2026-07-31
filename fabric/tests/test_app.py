from __future__ import annotations

import base64

import pytest

from fabric.app import create_app
from fabric.config import FabricConfig

_CONFIG = FabricConfig(address="fabric.example.com:51820", username="admin", password="hunter2")


def _basic_auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client():
    app = create_app(config=_CONFIG)
    app.config["TESTING"] = True
    return app.test_client()


def test_health_requires_no_auth(client) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["address"] == "fabric.example.com:51820"


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


def test_register_accepts_correct_credentials(client) -> None:
    # Still unimplemented (WS-E scope) — the point here is that valid
    # credentials get past auth and reach the real handler, not 401.
    resp = client.post(
        "/register",
        json={"version": "0.1.0", "public_key": "x"},
        headers=_basic_auth_header("admin", "hunter2"),
    )
    assert resp.status_code == 501


def test_register_still_validates_version_once_authenticated(client) -> None:
    resp = client.post(
        "/register",
        json={"version": "0.0.1", "public_key": "x"},
        headers=_basic_auth_header("admin", "hunter2"),
    )
    assert resp.status_code == 409
