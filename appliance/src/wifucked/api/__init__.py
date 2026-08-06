"""REST API and dashboard.

The dashboard is a window into the machine's understanding of the network, not
primarily a configuration screen. The user should be able to answer: what do I
have, how good is it, what is happening, what happened, and what did it cost.

Every byte is served from the Pi. No CDN, no remote fonts, no external scripts —
the appliance is frequently the thing standing between the user and the
Internet, so a dashboard that needs the Internet is useless exactly when it is
needed. `appliance/tests/verify_no_external_assets.py` enforces this in CI.
"""

from __future__ import annotations

import hmac
import io
import json
import tarfile
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from wifucked.atomics import Mode
from wifucked.daemon import Daemon
from wifucked.logging import get_logger

log = get_logger("api")

_UI = Path(__file__).resolve().parent.parent / "ui"

#: Reachable with no token — liveness probes can't authenticate, and captive
#: portal probes are answered by devices that haven't joined anything yet.
#: Everything else (the dashboard page, its data, and every mutating or
#: diagnostic route) carries the device's own network state and is gated —
#: architecture.md: "LAN services are never exposed through an arbitrary WAN."
_UNAUTHENTICATED_PATHS = frozenset(
    {
        "/api/health",
        "/generate_204",
        "/gen_204",
        "/hotspot-detect.html",
        "/ncsi.txt",
    }
)


def create_app(daemon: Daemon, api_token: str = "") -> Flask:
    app = Flask(
        __name__,
        template_folder=str(_UI / "templates"),
        static_folder=str(_UI / "static"),
    )

    @app.before_request
    def require_auth():
        if request.path in _UNAUTHENTICATED_PATHS:
            return None
        auth = request.authorization
        valid = (
            bool(api_token)
            and auth is not None
            and hmac.compare_digest(auth.password or "", api_token)
        )
        if not valid:
            log.warning(
                "Rejected unauthenticated dashboard/API request",
                extra={
                    "workflow": "api_auth",
                    "state": "failed",
                    "intent": "keep the dashboard reachable only to holders of the on-device token",
                    "path": request.path,
                    "ip": request.remote_addr,
                },
            )
            response = jsonify({"error": "authentication required"})
            response.status_code = 401
            response.headers["WWW-Authenticate"] = 'Basic realm="wifucked"'
            return response
        return None

    def _timed(workflow: str):
        started = time.monotonic()

        def done(state: str, **extra) -> None:
            log.info(
                f"{workflow} {state}",
                extra={
                    "workflow": workflow,
                    "state": state,
                    "intent": "serve the dashboard or an API client",
                    "ip": request.remote_addr,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                    **extra,
                },
            )

        return done

    @app.get("/")
    def dashboard():
        return render_template("dashboard.html", state=daemon.state_snapshot())

    @app.get("/api/state")
    def api_state():
        return jsonify(daemon.state_snapshot())

    @app.get("/api/decisions")
    def api_decisions():
        raw = request.args.get("limit", "50")
        try:
            limit = min(int(raw), 500)
        except ValueError:
            log.warning(
                "Rejected non-numeric decisions limit",
                extra={
                    "workflow": "api_decisions",
                    "state": "failed",
                    "intent": "serve the requested number of recent decision records",
                    "reason": f"non-numeric limit {raw!r}",
                    "ip": request.remote_addr,
                },
            )
            return jsonify({"error": f"invalid limit {raw!r}"}), 400
        return jsonify([d.to_dict() for d in daemon.telemetry.recent_decisions(limit)])

    @app.get("/api/health")
    def api_health():
        """Liveness for the OTA watchdog. Deliberately cheap and dependency-free."""
        snapshot = daemon.state_snapshot()
        return jsonify(
            {
                "ok": True,
                "version": snapshot["version"],
                "ap_running": snapshot["radio"]["ap_running"],
                "present_atomics": snapshot["counts"]["present"],
            }
        )

    @app.post("/api/atomics/<atomic_id>/mode")
    def api_set_mode(atomic_id: str):
        done = _timed("set_mode")
        payload = request.get_json(silent=True) or {}
        raw = payload.get("mode", "")
        try:
            mode = Mode(raw)
        except ValueError:
            done("failed", atomic_id=atomic_id, reason=f"unknown mode {raw!r}")
            return jsonify({"error": f"unknown mode {raw!r}"}), 400

        atomic = daemon.registry.set_mode(atomic_id, mode)
        if atomic is None:
            done("failed", atomic_id=atomic_id, reason="no such atomic")
            return jsonify({"error": "unknown atomic"}), 404

        done("completed", atomic_id=atomic_id, mode=str(mode))
        return jsonify(atomic.to_dict())

    @app.get("/api/diagnostics/bundle")
    def api_bundle():
        """Support bundle.

        Contains no credentials and no payload data, so it is safe to attach to
        an issue. Keep it that way when extending it.
        """
        done = _timed("diagnostics_bundle")
        ap_status = daemon.hal.ap.status()
        kernel = daemon.enforcer.raw_dump()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, blob in (
                ("state.json", json.dumps(daemon.state_snapshot(), indent=2)),
                (
                    "decisions.json",
                    json.dumps(
                        [d.to_dict() for d in daemon.telemetry.recent_decisions(500)],
                        indent=2,
                    ),
                ),
                ("release", json.dumps(daemon.release, indent=2)),
                (
                    "radio_state.json",
                    json.dumps(
                        {
                            "ap_running": ap_status.running,
                            "ap_channel": ap_status.channel,
                            "ap_ssids": list(ap_status.ssids),
                            "ap_associated_clients": ap_status.associated_clients,
                            "wan_atomics": [
                                {
                                    "id": a.id,
                                    "kind": str(a.kind),
                                    "mode": str(a.mode),
                                    "health": str(a.health),
                                }
                                for a in daemon.registry.present()
                            ],
                        },
                        indent=2,
                    ),
                ),
                # Kernel state (ADR-007: read-only, never mutating). Only
                # `enforce/` is permitted to invoke `tc`/`nft`/`ip`
                # (enforce/__init__.py) — this reuses that module's own
                # readback rather than shelling out here.
                ("nft_ruleset.txt", kernel.get("nft_ruleset", "")),
                ("tc_qdisc.txt", kernel.get("tc_qdisc", "")),
                ("ip_rule.txt", kernel.get("ip_rule", "")),
                ("ip_route.txt", kernel.get("ip_route", "")),
            ):
                data = blob.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))

        done("completed", size_bytes=buffer.tell())
        return Response(
            buffer.getvalue(),
            mimetype="application/gzip",
            headers={"Content-Disposition": "attachment; filename=wifucked-diagnostics.tar.gz"},
        )

    # Captive-portal probes. Returning a redirect rather than the expected
    # 204/success is what makes the phone pop the portal open by itself.
    @app.get("/generate_204")
    @app.get("/gen_204")
    @app.get("/hotspot-detect.html")
    @app.get("/ncsi.txt")
    def captive_probe():
        if daemon.registry.normal_pool():
            return "", 204
        return app.redirect("/")

    return app
