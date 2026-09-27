"""Subjective network monitor.

Simple standalone GUI + API for watching reachability/latency of a handful
of endpoints (e.g. behind different VPNs) from a laptop. Not part of the
wifucked daemon/appliance - just a personal diagnostic tool.

Run:
    python netmon/app.py

Then open http://localhost:8000
"""

import logging
import logging.handlers
import time
from pathlib import Path

from flask import Flask, jsonify, request

import storage
from monitor import MonitorManager

LOG_DIR = Path(__file__).parent / "logs"
LOG_FILE = LOG_DIR / "netmon.log"
MAX_LOG_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB


def _configure_logging():
    LOG_DIR.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=MAX_LOG_BYTES, backupCount=2
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger = logging.getLogger("netmon")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())
    return logger


log = _configure_logging()
storage.init_db()
app = Flask(__name__, static_folder="templates", static_url_path="")
manager = MonitorManager()


def _parse_range():
    """FROM/TO/bucket query params, defaulting to the last hour in 1-min buckets."""
    now = time.time()
    ts_to = float(request.args.get("to", now))
    ts_from = float(request.args.get("from", ts_to - 3600))
    bucket_s = float(request.args.get("bucket_s", 60))
    return ts_from, ts_to, bucket_s


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/api/hosts")
def api_list_hosts():
    return jsonify(manager.list_hosts())


@app.get("/api/publicip")
def api_public_ip():
    return jsonify(manager.public_ip.snapshot())


@app.get("/api/publicip/history")
def api_public_ip_history():
    ts_to = float(request.args.get("to", time.time()))
    ts_from = float(request.args.get("from", 0))
    return jsonify(storage.query_ip_periods(ts_from, ts_to))


@app.get("/api/hosts/<host_id>/buckets")
def api_host_buckets(host_id):
    ts_from, ts_to, bucket_s = _parse_range()
    return jsonify(storage.query_buckets(host_id, ts_from, ts_to, bucket_s))


@app.get("/api/hosts/<host_id>/events")
def api_host_events(host_id):
    ts_from, ts_to, _ = _parse_range()
    return jsonify(storage.query_drop_events(host_id, ts_from, ts_to))


@app.get("/api/hosts/<host_id>/summary")
def api_host_summary(host_id):
    ts_from, ts_to, _ = _parse_range()
    return jsonify(storage.query_summary(host_id, ts_from, ts_to))


@app.post("/api/hosts")
def api_add_host():
    body = request.get_json(force=True)
    if not body.get("host") or not body.get("name"):
        return jsonify({"error": "name and host are required"}), 400
    try:
        cfg = manager.add_host(body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(cfg), 201


@app.put("/api/hosts/<host_id>")
def api_update_host(host_id):
    body = request.get_json(force=True)
    try:
        cfg = manager.update_host(host_id, body)
    except KeyError:
        return jsonify({"error": "not found"}), 404
    return jsonify(cfg)


@app.delete("/api/hosts/<host_id>")
def api_delete_host(host_id):
    try:
        manager.delete_host(host_id)
    except KeyError:
        return jsonify({"error": "not found"}), 404
    return "", 204


if __name__ == "__main__":
    log.info(
        "netmon starting",
        extra={
            "workflow": "netmon_startup",
            "state": "started",
            "intent": "start subjective network monitor GUI on port 8000",
        },
    )
    app.run(host="0.0.0.0", port=8000, threaded=True)
