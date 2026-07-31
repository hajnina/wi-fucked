"""Fabric HTTP API.

Deliberately small. The appliance needs three things from a fabric server:
is it alive, is it compatible, and what should I connect to. Everything else is
WireGuard's job, in the kernel.
"""

from __future__ import annotations

import hmac
import os
import time

from flask import Flask, jsonify, request

from fabric import MIN_APPLIANCE_VERSION, __version__
from fabric.config import ConfigError, FabricConfig, load_config

_STARTED = time.monotonic()

#: Reachable with no credentials — liveness probes and load balancers can't
#: authenticate, and a health check that requires auth isn't a health check.
_UNAUTHENTICATED_PATHS = frozenset({"/health"})


def version_tuple(version: str) -> tuple[int, int, int]:
    core = version.split("-", 1)[0].split("+", 1)[0]
    parts = [*core.split("."), "0", "0", "0"][:3]
    try:
        return tuple(int(p) for p in parts)  # type: ignore[return-value]
    except ValueError:
        return (0, 0, 0)


def create_app(config: FabricConfig | None = None) -> Flask:
    if config is None:
        try:
            config = load_config()
        except ConfigError as exc:
            # Raised from module import under gunicorn — docker-entrypoint.sh
            # guarantees these are set by the time this runs, so hitting this
            # means the container was started wrong (e.g. CMD run directly,
            # bypassing the entrypoint).
            raise SystemExit(f"FATAL: {exc}") from exc

    app = Flask(__name__)

    @app.before_request
    def require_auth():
        if request.path in _UNAUTHENTICATED_PATHS:
            return None
        auth = request.authorization
        valid = (
            auth is not None
            and hmac.compare_digest(auth.username or "", config.username)
            and hmac.compare_digest(auth.password or "", config.password)
        )
        if not valid:
            response = jsonify({"error": "authentication required"})
            response.status_code = 401
            response.headers["WWW-Authenticate"] = 'Basic realm="fabric"'
            return response
        return None

    @app.get("/health")
    def health():
        """Liveness and capability, in one cheap call.

        The appliance polls this to rank servers, so it must stay dependency-free
        — a health endpoint that can hang is worse than no health endpoint. Left
        unauthenticated deliberately: it carries no secret, and requiring auth
        would break liveness probes and load balancers that can't supply one.
        """
        return jsonify(
            {
                "ok": True,
                "version": __version__,
                "min_appliance_version": MIN_APPLIANCE_VERSION,
                "uptime_s": round(time.monotonic() - _STARTED, 1),
                "address": config.address,
                "region": os.getenv("FABRIC_REGION", "unknown"),
            }
        )

    @app.post("/register")
    def register():
        """Register an appliance and hand back tunnel parameters.

        Version compatibility is checked in both directions. An incompatible
        appliance is told so explicitly — a clear refusal is far easier to
        diagnose in the field than a tunnel that half-works.
        """
        payload = request.get_json(silent=True) or {}
        appliance_version = payload.get("version", "")
        public_key = payload.get("public_key", "")

        if not public_key:
            return jsonify({"error": "public_key is required"}), 400

        if version_tuple(appliance_version) < version_tuple(MIN_APPLIANCE_VERSION):
            return (
                jsonify(
                    {
                        "error": "appliance too old for this fabric",
                        "appliance_version": appliance_version,
                        "min_appliance_version": MIN_APPLIANCE_VERSION,
                    }
                ),
                409,
            )

        # WS-E: allocate a tunnel address, add the WireGuard peer, return the
        # server's public key and endpoint.
        return (
            jsonify({"error": "peer registration not implemented", "detail": "WS-E scope"}),
            501,
        )

    return app


app = create_app()
