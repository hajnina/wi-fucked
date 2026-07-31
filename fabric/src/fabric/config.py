"""Fabric configuration: the address it advertises, and the admin credentials
that guard every endpoint except /health.

Resolved once by docker-entrypoint.sh (interactively, if needed) before
gunicorn starts. By the time this module is imported inside a gunicorn
worker, the environment is already settled — no worker ever prompts anyone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

REQUIRED_ENV_VARS = ("FABRIC_ADDRESS", "FABRIC_USERNAME", "FABRIC_PASSWORD")


class ConfigError(RuntimeError):
    """Required configuration is missing."""


@dataclass(frozen=True, slots=True)
class FabricConfig:
    #: The host:port an appliance should connect to. The container doesn't
    #: know its own public address, so an operator must supply it.
    address: str
    username: str
    password: str


def load_config() -> FabricConfig:
    values = {name: os.environ.get(name) for name in REQUIRED_ENV_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError("missing required environment variable(s): " + ", ".join(missing))
    return FabricConfig(
        address=values["FABRIC_ADDRESS"],
        username=values["FABRIC_USERNAME"],
        password=values["FABRIC_PASSWORD"],
    )
