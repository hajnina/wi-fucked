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


@dataclass(slots=True)
class Config:
    lan: LanConfig = field(default_factory=LanConfig)
    fabric: FabricConfig = field(default_factory=FabricConfig)
    loops: LoopConfig = field(default_factory=LoopConfig)
    thresholds: Thresholds = field(default_factory=Thresholds)
    state_dir: Path = DEFAULT_STATE_DIR
    api_host: str = "0.0.0.0"  # noqa: S104 - the LAN is who this serves
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
