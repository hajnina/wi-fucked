"""Fabric HTTP API.

Deliberately small. The appliance needs three things from a fabric server:
is it alive, is it compatible, and what should I connect to. Everything else is
WireGuard's job, in the kernel.
"""

from __future__ import annotations

import os
import time

from flask import Flask, jsonify, request

from fabric import MIN_APPLIANCE_VERSION, __version__

_STARTED = time.monotonic()


def version_tuple(version: str) -> tuple[int, int, int]:
    core = version.split("-", 1)[0].split("+", 1)[0]
    parts = [*core.split("."), "0", "0", "0"][:3]
    try:
        return tuple(int(p) for p in parts)  # type: ignore[return-value]
    except ValueError:
        return (0, 0, 0)


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health():
        """Liveness and capability, in one cheap call.

        The appliance polls this to rank servers, so it must stay dependency-free
        — a health endpoint that can hang is worse than no health endpoint.
        """
        return jsonify(
            {
                "ok": True,
                "version": __version__,
                "min_appliance_version": MIN_APPLIANCE_VERSION,
                "uptime_s": round(time.monotonic() - _STARTED, 1),
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
