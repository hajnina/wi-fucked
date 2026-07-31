from __future__ import annotations

import pytest

from fabric.config import ConfigError, load_config


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FABRIC_ADDRESS", "FABRIC_USERNAME", "FABRIC_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def test_load_config_reads_all_three(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("FABRIC_ADDRESS", "fabric.example.com:51820")
    monkeypatch.setenv("FABRIC_USERNAME", "admin")
    monkeypatch.setenv("FABRIC_PASSWORD", "hunter2")

    config = load_config()

    assert config.address == "fabric.example.com:51820"
    assert config.username == "admin"
    assert config.password == "hunter2"


@pytest.mark.parametrize("missing", ["FABRIC_ADDRESS", "FABRIC_USERNAME", "FABRIC_PASSWORD"])
def test_load_config_fails_closed_on_missing_var(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("FABRIC_ADDRESS", "fabric.example.com:51820")
    monkeypatch.setenv("FABRIC_USERNAME", "admin")
    monkeypatch.setenv("FABRIC_PASSWORD", "hunter2")
    monkeypatch.delenv(missing, raising=False)

    with pytest.raises(ConfigError, match=missing):
        load_config()


def test_load_config_fails_closed_on_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("FABRIC_ADDRESS", "fabric.example.com:51820")
    monkeypatch.setenv("FABRIC_USERNAME", "")
    monkeypatch.setenv("FABRIC_PASSWORD", "hunter2")

    with pytest.raises(ConfigError, match="FABRIC_USERNAME"):
        load_config()
