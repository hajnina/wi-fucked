"""Configuration.

Every key has a default that produces a working appliance. The device must boot
and serve its SSIDs with no configuration file at all — first boot has none, and
neither does a factory reset (ADR-015).

Configuration is JSON: small, rarely written, human-readable, hand-editable
(ADR-010).
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

from wifucked.logging import get_logger
from wifucked.policy import Thresholds

log = get_logger("config")

DEFAULT_STATE_DIR = Path(os.getenv("WIFUCKED_STATE_DIR", "/var/lib/wifucked"))
RELEASE_FILE = Path(os.getenv("WIFUCKED_RELEASE_FILE", "/etc/wifucked-release"))


@dataclass(slots=True)
class LanConfig:
    critical_ssid: str = "Stable_critical"
    besteffort_ssid: str = "Stable_besteffort"
    address: str = "10.44.0.1"
    prefix: int = 24
    #: Set at first boot from the radio capability probe (ADR-014). "two_bss" or
    #: "two_psk"; everything above the LAN layer sees VLANs either way.
    lan_mode: str = "two_bss"


@dataclass(slots=True)
class FabricConfig:
    servers: list[str] = field(default_factory=list)
    interface: str = "wg0"
    #: Basic Auth credentials for the fabric's /register endpoint. The fabric
    #: guards every endpoint but /health (see fabric/src/fabric/app.py) — an
    #: appliance with no credentials configured simply never attaches, rather
    #: than trying and failing repeatedly against a server it can't reach.
    username: str = ""
    password: str = ""


@dataclass(slots=True)
class LoopConfig:
    fast_s: float = 1.0
    medium_s: float = 10.0
    slow_s: float = 300.0
    #: Minimum time between active Wi-Fi scans, independent of medium_s. On the
    #: Zero 2W's single shared radio an active scan typically has to leave the
    #: AP's serving channel briefly, which cuts against ADR-011's "AP is the
    #: anchor" guarantee — kept well above medium_s so discovery doesn't scan
    #: on every tick (ADR-011).
    wifi_scan_min_interval_s: float = 120.0


@dataclass(slots=True)
class Config:
    lan: LanConfig = field(default_factory=LanConfig)
    fabric: FabricConfig = field(default_factory=FabricConfig)
    loops: LoopConfig = field(default_factory=LoopConfig)
    thresholds: Thresholds = field(default_factory=Thresholds)
    state_dir: Path = DEFAULT_STATE_DIR
    #: Bound to the LAN gateway address (matches ``LanConfig.address``), never to
    #: 0.0.0.0. Every WAN atomic (Wi-Fi station, USB tether, cellular) is a
    #: separate interface with its own address; binding here means the dashboard
    #: is unreachable from any of them even before the token check below runs
    #: (ADR: architecture.md "WANs are hostile"). Defense in depth, not the only
    #: layer — see ``api_token``.
    api_host: str = "10.44.0.1"
    api_port: int = 8080

    @property
    def registry_path(self) -> Path:
        return self.state_dir / "atomics.json"

    @property
    def telemetry_path(self) -> Path:
        return self.state_dir / "telemetry.db"

    @property
    def config_path(self) -> Path:
        return self.state_dir / "config.json"

    @property
    def api_token_path(self) -> Path:
        return self.state_dir / "api_token"

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["state_dir"] = str(self.state_dir)
        return payload


def _merge(target, values: dict) -> None:
    for key, value in values.items():
        if hasattr(target, key):
            setattr(target, key, value)


def load(path: Path | None = None) -> Config:
    """Load configuration, falling back to working defaults at every step."""
    config = Config()
    if path is None:
        path = config.config_path

    if not path.exists():
        log.info(
            "No configuration file; using defaults",
            extra={
                "workflow": "config_load",
                "state": "completed",
                "intent": "boot into a working state with no configuration",
                "path": str(path),
            },
        )
        return config

    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.error(
            "Configuration unreadable; falling back to defaults",
            extra={
                "workflow": "config_load",
                "state": "failed",
                "intent": "apply the user's settings",
                "path": str(path),
                "reason": "unreadable or malformed configuration file",
                "error": str(exc),
            },
            exc_info=True,
        )
        return config

    _merge(config.lan, payload.get("lan", {}))
    _merge(config.fabric, payload.get("fabric", {}))
    _merge(config.loops, payload.get("loops", {}))
    if "thresholds" in payload:
        try:
            config.thresholds = Thresholds(**payload["thresholds"])
        except TypeError as exc:
            log.warning(
                "Unknown threshold keys; using default thresholds",
                extra={
                    "workflow": "config_load",
                    "state": "skipped",
                    "intent": "apply the user's tuning",
                    "reason": "threshold block did not match the expected schema",
                    "error": str(exc),
                },
            )

    for key in ("api_host", "api_port"):
        if key in payload:
            setattr(config, key, payload[key])
    if "state_dir" in payload:
        config.state_dir = Path(payload["state_dir"])

    log.info(
        "Configuration loaded",
        extra={
            "workflow": "config_load",
            "state": "completed",
            "intent": "apply the user's settings",
            "path": str(path),
        },
    )
    return config


def load_or_create_api_token(config: Config, *, persist: bool = True) -> str:
    """Load the dashboard/API's auth token, generating one on first run.

    Mirrors the WireGuard keypair pattern in ``firstboot.sh``: a secret
    generated on-device, once, and never baked into an image or committed to
    ``config.json`` (that file is meant to be human-readable and hand-editable
    per ADR-010 — a bearer token doesn't belong in it). Stored at
    ``config.api_token_path`` with owner-only permissions, exactly like
    ``/etc/wireguard/wifucked-privatekey``.

    Under ``MOCK_HW`` (``persist=False``, matching ``Daemon(persist=...)``)
    nothing touches disk: a fresh token is minted for this process only, the
    same way registry/telemetry persistence is skipped in that mode.
    """
    if not persist:
        return secrets.token_urlsafe(32)

    path = config.api_token_path
    try:
        existing = path.read_text().strip()
    except OSError:
        existing = ""
    if existing:
        return existing

    token = secrets.token_urlsafe(32)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(token)
    path.chmod(0o600)
    log.info(
        "Generated dashboard API token",
        extra={
            "workflow": "api_token_init",
            "state": "completed",
            "intent": "gate the dashboard/API to holders of the on-device token, "
            "consistent with 'LAN services never exposed through an arbitrary WAN'",
            "path": str(path),
        },
    )
    return token


def release_info() -> dict[str, str]:
    """Read ``/etc/wifucked-release``, baked into the image at build time."""
    info: dict[str, str] = {}
    try:
        for line in RELEASE_FILE.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                info[key.strip()] = value.strip().strip('"')
    except OSError:
        info = {"WIFUCKED_VERSION": "0.0.0-dev", "WIFUCKED_CHANNEL": "development"}
    return info
