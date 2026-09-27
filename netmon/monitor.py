"""Background ping monitor: one thread per configured host, in-memory history."""

import json
import logging
import platform
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from pathlib import Path

import storage

log = logging.getLogger("netmon")

HOSTS_FILE = Path(__file__).parent / "hosts.json"
HOSTS_EXAMPLE_FILE = Path(__file__).parent / "hosts.example.json"
HISTORY_LEN = 180  # samples kept per host for the graph
IS_WINDOWS = platform.system().lower() == "windows"

PUBLIC_IP_INTERVAL_S = 60
PUBLIC_IP_HISTORY_LEN = 1440  # 24h at 1-minute resolution
PUBLIC_IP_SERVICES = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)

LATENCY_RE_WIN = re.compile(r"time[=<]([\d.]+)ms")
LATENCY_RE_UNIX = re.compile(r"time=([\d.]+) ?ms")


def _ping_once(host: str, timeout_s: float) -> float | None:
    """Return latency in ms, or None if the ping failed/timed out."""
    timeout_ms = max(1, int(timeout_s * 1000))
    if IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout_s))), host]

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s + 1,
        )
    except subprocess.TimeoutExpired:
        return None

    elapsed_ms = (time.monotonic() - started) * 1000
    if proc.returncode != 0:
        return None

    out = proc.stdout
    match = LATENCY_RE_WIN.search(out) or LATENCY_RE_UNIX.search(out)
    if match:
        return float(match.group(1))
    return elapsed_ms


class HostMonitor:
    """Owns the ping loop and rolling stats for a single host."""

    def __init__(self, config: dict):
        self.lock = threading.Lock()
        self.config = config
        self.history: deque = deque(maxlen=HISTORY_LEN)
        self.total = 0
        self.success = 0
        self.fail = 0
        self.consecutive_fail = 0
        self.status = "unknown"
        self.last_change_ts = time.time()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def update_config(self, config: dict):
        with self.lock:
            self.config = config

    def _run(self):
        while not self._stop.is_set():
            with self.lock:
                host = self.config["host"]
                interval_s = self.config.get("interval_s", 0.1)
                timeout_s = self.config.get("timeout_s", 2)
                name = self.config.get("name", host)

            latency = _ping_once(host, timeout_s)
            ok = latency is not None
            now = time.time()

            with self.lock:
                self.total += 1
                prev_status = self.status
                if ok:
                    self.success += 1
                    self.consecutive_fail = 0
                    self.status = "up"
                else:
                    self.fail += 1
                    self.consecutive_fail += 1
                    self.status = "down"
                if self.status != prev_status:
                    self.last_change_ts = now
                self.history.append({"t": now, "latency_ms": latency})

            storage.record_ping(self.config["id"], now, latency)

            if ok:
                log.info(
                    "ping ok",
                    extra={
                        "workflow": "host_ping",
                        "state": "completed",
                        "intent": "sample reachability/latency for subjective network monitor",
                        "host_id": self.config["id"],
                        "host_name": name,
                        "host": host,
                        "latency_ms": round(latency, 2),
                    },
                )
            else:
                log.warning(
                    "ping failed",
                    extra={
                        "workflow": "host_ping",
                        "state": "failed",
                        "intent": "sample reachability/latency for subjective network monitor",
                        "host_id": self.config["id"],
                        "host_name": name,
                        "host": host,
                        "timeout_s": timeout_s,
                        "consecutive_fail": self.consecutive_fail,
                        "reason": "no reply within timeout",
                    },
                )
                if prev_status != "down":
                    log.warning(
                        "host transitioned to down",
                        extra={
                            "workflow": "host_status_change",
                            "state": "failed",
                            "intent": "surface reachability drop for alerting/beep",
                            "host_id": self.config["id"],
                            "host_name": name,
                            "host": host,
                        },
                    )

            self._stop.wait(interval_s)

    def snapshot(self) -> dict:
        with self.lock:
            avg_latency = None
            recent = [h["latency_ms"] for h in self.history if h["latency_ms"] is not None]
            if recent:
                avg_latency = round(sum(recent) / len(recent), 2)
            return {
                **self.config,
                "status": self.status,
                "total": self.total,
                "success": self.success,
                "fail": self.fail,
                "consecutive_fail": self.consecutive_fail,
                "uptime_pct": round(100 * self.success / self.total, 2) if self.total else None,
                "avg_latency_ms": avg_latency,
                "last_change_ts": self.last_change_ts,
                "history": list(self.history),
            }


def _fetch_public_ip(timeout_s: float = 5.0) -> str | None:
    for url in PUBLIC_IP_SERVICES:
        try:
            with urllib.request.urlopen(url, timeout=timeout_s) as resp:
                ip = resp.read().decode().strip()
            if ip:
                return ip
        except (urllib.error.URLError, OSError, TimeoutError):
            continue
    return None


class PublicIPMonitor:
    """Polls an external service once a minute and logs any change of IP."""

    def __init__(self, interval_s: float = PUBLIC_IP_INTERVAL_S):
        self.interval_s = interval_s
        self.lock = threading.Lock()
        self.current_ip: str | None = None
        self.last_change_ts: float | None = None
        self.history: deque = deque(maxlen=PUBLIC_IP_HISTORY_LEN)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            ip = _fetch_public_ip()
            now = time.time()

            with self.lock:
                previous_ip = self.current_ip
                self.history.append({"t": now, "ip": ip})
                if ip is not None and ip != previous_ip:
                    self.current_ip = ip
                    self.last_change_ts = now

            storage.record_public_ip(now, ip)

            if ip is None:
                log.warning(
                    "public IP lookup failed",
                    extra={
                        "workflow": "public_ip_check",
                        "state": "failed",
                        "intent": "sample the laptop's current public IP once a minute",
                        "reason": "all public IP lookup services unreachable/timed out",
                    },
                )
            elif previous_ip is not None and ip != previous_ip:
                log.warning(
                    "public IP changed",
                    extra={
                        "workflow": "public_ip_change",
                        "state": "completed",
                        "intent": "surface WAN/VPN egress changes as they happen",
                        "previous_ip": previous_ip,
                        "new_ip": ip,
                    },
                )
            else:
                log.info(
                    "public IP sample",
                    extra={
                        "workflow": "public_ip_check",
                        "state": "completed",
                        "intent": "sample the laptop's current public IP once a minute",
                        "ip": ip,
                    },
                )

            self._stop.wait(self.interval_s)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "current_ip": self.current_ip,
                "last_change_ts": self.last_change_ts,
                "history": list(self.history),
            }


class MonitorManager:
    """Loads hosts.json, owns the per-host threads, persists config edits."""

    def __init__(self, hosts_file: Path = HOSTS_FILE):
        self.hosts_file = hosts_file
        self.lock = threading.Lock()
        self.monitors: dict[str, HostMonitor] = {}
        self.public_ip = PublicIPMonitor()
        self.public_ip.start()
        self._load()

    def _load(self):
        if self.hosts_file.exists():
            configs = json.loads(self.hosts_file.read_text())
        elif HOSTS_EXAMPLE_FILE.exists():
            # First run: seed a real, editable hosts.json (gitignored) from
            # the tracked example so the GUI has something to show.
            configs = json.loads(HOSTS_EXAMPLE_FILE.read_text())
            self.hosts_file.write_text(json.dumps(configs, indent=2))
        else:
            configs = []
        for cfg in configs:
            self._spawn(cfg)

    def _spawn(self, cfg: dict):
        mon = HostMonitor(cfg)
        self.monitors[cfg["id"]] = mon
        mon.start()

    def _persist(self):
        configs = [m.config for m in self.monitors.values()]
        self.hosts_file.write_text(json.dumps(configs, indent=2))

    def list_hosts(self) -> list[dict]:
        with self.lock:
            return [m.snapshot() for m in self.monitors.values()]

    def add_host(self, cfg: dict) -> dict:
        with self.lock:
            cfg = dict(cfg)
            cfg.setdefault("id", uuid.uuid4().hex[:8])
            cfg.setdefault("vpn", "")
            cfg.setdefault("interval_s", 0.1)
            cfg.setdefault("timeout_s", 2)
            cfg.setdefault("beep_on_drop", True)
            if cfg["id"] in self.monitors:
                raise ValueError(f"host id {cfg['id']} already exists")
            self._spawn(cfg)
            self._persist()
            log.info(
                "host added",
                extra={
                    "workflow": "host_config_change",
                    "state": "completed",
                    "intent": "user added a host to monitor via GUI",
                    "host_id": cfg["id"],
                    "host": cfg["host"],
                },
            )
            return cfg

    def update_host(self, host_id: str, updates: dict) -> dict:
        with self.lock:
            mon = self.monitors.get(host_id)
            if not mon:
                raise KeyError(host_id)
            new_cfg = {**mon.config, **updates, "id": host_id}
            mon.update_config(new_cfg)
            self._persist()
            log.info(
                "host updated",
                extra={
                    "workflow": "host_config_change",
                    "state": "completed",
                    "intent": "user edited host settings via GUI",
                    "host_id": host_id,
                    "fields_changed": list(updates.keys()),
                },
            )
            return new_cfg

    def delete_host(self, host_id: str):
        with self.lock:
            mon = self.monitors.pop(host_id, None)
            if not mon:
                raise KeyError(host_id)
            mon.stop()
            self._persist()
            log.info(
                "host removed",
                extra={
                    "workflow": "host_config_change",
                    "state": "completed",
                    "intent": "user removed a monitored host via GUI",
                    "host_id": host_id,
                },
            )
