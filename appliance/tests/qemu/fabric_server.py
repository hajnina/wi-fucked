#!/usr/bin/env python3
"""Runs the real `fabric.app` Flask application inside the "fabric" netns.

Not a mock and not a reimplementation: this imports `fabric.app.create_app()`
and serves it with Werkzeug's dev server, so `/register` calls exercise the
genuine `fabric.wireguard.FabricWireGuard.ensure_ready()`/`add_peer()` code
(ADR-019's forwarding + NAT included) against this netns's real kernel.
"""

from __future__ import annotations

from fabric.app import create_app

if __name__ == "__main__":
    app = create_app()
    # 0.0.0.0 is intentional here: this runs inside an isolated QEMU/netns
    # test guest, reachable only via a private bridge this harness owns —
    # not a deployment posture, so the usual "don't bind all interfaces"
    # concern doesn't apply.
    app.run(host="0.0.0.0", port=8081, threaded=True)  # noqa: S104
