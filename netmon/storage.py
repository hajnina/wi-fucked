"""SQLite-backed sample history, so diagnostics queries survive past the
in-memory sparkline window and can cover arbitrary FROM/TO ranges."""

import sqlite3
import threading
from pathlib import Path

DB_FILE = Path(__file__).parent / "netmon.db"

_local = threading.local()


def _conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        _local.conn = conn
    return conn


def init_db():
    conn = _conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ping_samples (
            host_id TEXT NOT NULL,
            ts REAL NOT NULL,
            latency_ms REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ping_host_ts ON ping_samples(host_id, ts)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public_ip_samples (
            ts REAL NOT NULL,
            ip TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pubip_ts ON public_ip_samples(ts)"
    )
    conn.commit()


def record_ping(host_id: str, ts: float, latency_ms: float | None):
    conn = _conn()
    conn.execute(
        "INSERT INTO ping_samples (host_id, ts, latency_ms) VALUES (?, ?, ?)",
        (host_id, ts, latency_ms),
    )
    conn.commit()


def record_public_ip(ts: float, ip: str | None):
    conn = _conn()
    conn.execute(
        "INSERT INTO public_ip_samples (ts, ip) VALUES (?, ?)", (ts, ip)
    )
    conn.commit()


def query_buckets(host_id: str, ts_from: float, ts_to: float, bucket_s: float) -> list[dict]:
    """Bucket every sample in [ts_from, ts_to] into fixed-width windows."""
    conn = _conn()
    rows = conn.execute(
        "SELECT ts, latency_ms FROM ping_samples "
        "WHERE host_id=? AND ts>=? AND ts<=? ORDER BY ts",
        (host_id, ts_from, ts_to),
    ).fetchall()

    buckets: dict[float, dict] = {}
    for ts, latency in rows:
        bucket_start = ts_from + ((ts - ts_from) // bucket_s) * bucket_s
        b = buckets.setdefault(
            bucket_start, {"total": 0, "success": 0, "fail": 0, "latencies": []}
        )
        b["total"] += 1
        if latency is None:
            b["fail"] += 1
        else:
            b["success"] += 1
            b["latencies"].append(latency)

    result = []
    for bucket_start in sorted(buckets):
        b = buckets[bucket_start]
        avg = round(sum(b["latencies"]) / len(b["latencies"]), 2) if b["latencies"] else None
        result.append(
            {
                "bucket_start": bucket_start,
                "total": b["total"],
                "success": b["success"],
                "fail": b["fail"],
                "avg_latency_ms": avg,
            }
        )
    return result


def query_drop_events(host_id: str, ts_from: float, ts_to: float) -> list[dict]:
    """Collapse consecutive down samples into discrete outage events."""
    conn = _conn()
    rows = conn.execute(
        "SELECT ts, latency_ms FROM ping_samples "
        "WHERE host_id=? AND ts>=? AND ts<=? ORDER BY ts",
        (host_id, ts_from, ts_to),
    ).fetchall()

    events = []
    started_ts = None
    prev_ts = None
    for ts, latency in rows:
        down = latency is None
        if down and started_ts is None:
            started_ts = ts
        elif not down and started_ts is not None:
            events.append(
                {
                    "started_ts": started_ts,
                    "ended_ts": prev_ts,
                    "duration_s": round(prev_ts - started_ts, 1),
                    "ongoing": False,
                }
            )
            started_ts = None
        prev_ts = ts

    if started_ts is not None:
        events.append(
            {
                "started_ts": started_ts,
                "ended_ts": prev_ts,
                "duration_s": round((prev_ts - started_ts), 1) if prev_ts else 0,
                "ongoing": True,
            }
        )
    events.reverse()
    return events


def query_ip_periods(ts_from: float, ts_to: float) -> list[dict]:
    """Collapse consecutive same-IP samples into discrete holding periods."""
    conn = _conn()
    rows = conn.execute(
        "SELECT ts, ip FROM public_ip_samples WHERE ts>=? AND ts<=? ORDER BY ts",
        (ts_from, ts_to),
    ).fetchall()

    periods = []
    current_ip = None
    current_start = None
    last_ts = None
    for ts, ip in rows:
        if ip is None:
            continue
        if current_ip is None:
            current_ip = ip
            current_start = ts
        elif ip != current_ip:
            periods.append(
                {
                    "ip": current_ip,
                    "started_ts": current_start,
                    "ended_ts": last_ts,
                    "duration_s": round(last_ts - current_start, 1),
                    "ongoing": False,
                }
            )
            current_ip = ip
            current_start = ts
        last_ts = ts

    if current_ip is not None:
        periods.append(
            {
                "ip": current_ip,
                "started_ts": current_start,
                "ended_ts": last_ts,
                "duration_s": round(last_ts - current_start, 1) if last_ts else 0,
                "ongoing": True,
            }
        )
    periods.reverse()
    return periods


def query_summary(host_id: str, ts_from: float, ts_to: float) -> dict:
    conn = _conn()
    row = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN latency_ms IS NULL THEN 1 ELSE 0 END), "
        "AVG(latency_ms) FROM ping_samples WHERE host_id=? AND ts>=? AND ts<=?",
        (host_id, ts_from, ts_to),
    ).fetchone()
    total, fail, avg_latency = row
    total = total or 0
    fail = fail or 0
    return {
        "total": total,
        "success": total - fail,
        "fail": fail,
        "uptime_pct": round(100 * (total - fail) / total, 2) if total else None,
        "avg_latency_ms": round(avg_latency, 2) if avg_latency is not None else None,
    }
