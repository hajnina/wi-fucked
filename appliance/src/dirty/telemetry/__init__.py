"""Telemetry — the decision journal, the event log, and the time series.

The decision journal is the product's main differentiator: the machine explains
itself from what it recorded at the moment of deciding, not from a
reconstruction after the fact (ADR-009).

Storage is SQLite with hot writes buffered in memory and flushed periodically,
and retention is a ring buffer so the database is bounded by construction — the
SD card is consumable and telemetry is what consumes it (ADR-010).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from dirty.clock import Clock
from dirty.logging import get_logger

log = get_logger("telemetry")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        REAL NOT NULL,
    action    TEXT NOT NULL,
    reason    TEXT NOT NULL,
    inputs    TEXT NOT NULL,
    thresholds TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_at ON decisions(at);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       REAL NOT NULL,
    kind     TEXT NOT NULL,
    subject  TEXT,
    detail   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_at ON events(at);

CREATE TABLE IF NOT EXISTS samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         REAL NOT NULL,
    atomic_id  TEXT NOT NULL,
    down_bps   INTEGER,
    up_bps     INTEGER,
    rtt_ms     REAL,
    loss_pct   REAL
);
CREATE INDEX IF NOT EXISTS samples_at ON samples(at);
"""


@dataclass(frozen=True, slots=True)
class Decision:
    at: float
    action: str
    reason: str
    inputs: dict
    thresholds: dict

    def to_dict(self) -> dict:
        return {
            "at": self.at,
            "action": self.action,
            "reason": self.reason,
            "inputs": self.inputs,
            "thresholds": self.thresholds,
        }


@dataclass
class _Buffer:
    """Hot writes live here until the next flush.

    Sub-minute samples across a dozen atomics is a continuous write stream that
    would destroy a consumer SD card. A power cut costs at most one flush
    interval of telemetry, which is a far better trade than a dead card.
    """

    decisions: list[Decision] = field(default_factory=list)
    events: list[tuple[float, str, str | None, dict]] = field(default_factory=list)
    samples: list[tuple[float, str, int, int, float | None, float | None]] = field(
        default_factory=list
    )

    def empty(self) -> bool:
        return not (self.decisions or self.events or self.samples)


class Telemetry:
    def __init__(
        self,
        clock: Clock,
        db_path: Path | None = None,
        *,
        flush_interval_s: float = 60.0,
        retain_decisions: int = 5_000,
        retain_events: int = 20_000,
        retain_samples: int = 200_000,
    ):
        self._clock = clock
        self._db_path = db_path
        self._flush_interval = flush_interval_s
        self._retain = {
            "decisions": retain_decisions,
            "events": retain_events,
            "samples": retain_samples,
        }
        self._buffer = _Buffer()
        self._last_flush = clock.now()
        self._conn: sqlite3.Connection | None = None
        #: Kept in memory so the dashboard can render live data that has not been
        #: flushed yet, without reading across two stores.
        self._recent: list[Decision] = []

        if db_path is not None:
            self._open(db_path)

    # -- recording ------------------------------------------------------------

    def record_decision(
        self, action: str, reason: str, inputs: dict, thresholds: dict | None = None
    ) -> Decision:
        decision = Decision(
            at=self._clock.wall(),
            action=action,
            reason=reason,
            inputs=inputs,
            thresholds=thresholds or {},
        )
        self._buffer.decisions.append(decision)
        self._recent.append(decision)
        del self._recent[:-200]
        log.info(
            "Allocation decision recorded",
            extra={
                "workflow": "decision_record",
                "state": "completed",
                "intent": "let the machine explain this decision later",
                "action": action,
                "reason": reason,
                **{f"in_{k}": v for k, v in inputs.items()},
            },
        )
        return decision

    def record_event(self, kind: str, detail: dict, subject: str | None = None) -> None:
        self._buffer.events.append((self._clock.wall(), kind, subject, detail))

    def record_sample(
        self,
        atomic_id: str,
        down_bps: int,
        up_bps: int,
        rtt_ms: float | None = None,
        loss_pct: float | None = None,
    ) -> None:
        self._buffer.samples.append(
            (self._clock.wall(), atomic_id, down_bps, up_bps, rtt_ms, loss_pct)
        )

    # -- reading --------------------------------------------------------------

    def recent_decisions(self, limit: int = 50) -> list[Decision]:
        if self._conn is None:
            return list(reversed(self._recent[-limit:]))

        stored: list[Decision] = []
        try:
            rows = self._conn.execute(
                "SELECT at, action, reason, inputs, thresholds "
                "FROM decisions ORDER BY at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            stored = [Decision(r[0], r[1], r[2], json.loads(r[3]), json.loads(r[4])) for r in rows]
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            log.error(
                "Could not read decision journal; serving in-memory decisions only",
                extra={
                    "workflow": "telemetry_read",
                    "state": "failed",
                    "intent": "show the user why the appliance acted",
                    "reason": "query failed",
                    "error": str(exc),
                },
                exc_info=True,
            )

        # Unflushed decisions are newer than anything in the database.
        merged = list(reversed(self._buffer.decisions)) + stored
        return merged[:limit]

    # -- persistence ----------------------------------------------------------

    def tick(self) -> None:
        """Called from the slow loop. Flushes when the interval has elapsed."""
        if self._clock.now() - self._last_flush >= self._flush_interval:
            self.flush()

    def flush(self) -> None:
        self._last_flush = self._clock.now()
        if self._conn is None or self._buffer.empty():
            self._buffer = _Buffer()
            return

        buffered = self._buffer
        self._buffer = _Buffer()
        try:
            with self._conn:
                self._conn.executemany(
                    "INSERT INTO decisions (at, action, reason, inputs, thresholds) "
                    "VALUES (?,?,?,?,?)",
                    [
                        (
                            d.at,
                            d.action,
                            d.reason,
                            json.dumps(d.inputs),
                            json.dumps(d.thresholds),
                        )
                        for d in buffered.decisions
                    ],
                )
                self._conn.executemany(
                    "INSERT INTO events (at, kind, subject, detail) VALUES (?,?,?,?)",
                    [(at, kind, subj, json.dumps(d)) for at, kind, subj, d in buffered.events],
                )
                self._conn.executemany(
                    "INSERT INTO samples (at, atomic_id, down_bps, up_bps, rtt_ms, loss_pct) "
                    "VALUES (?,?,?,?,?,?)",
                    buffered.samples,
                )
            self._trim()
        except sqlite3.Error as exc:
            log.error(
                "Telemetry flush failed; the buffered window is lost",
                extra={
                    "workflow": "telemetry_flush",
                    "state": "failed",
                    "intent": "persist telemetry across reboot",
                    "decisions": len(buffered.decisions),
                    "events": len(buffered.events),
                    "samples": len(buffered.samples),
                    "reason": "sqlite write failed",
                    "error": str(exc),
                },
                exc_info=True,
            )

    def close(self) -> None:
        self.flush()
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _open(self, db_path: Path) -> None:
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
            # WAL gives atomic commits, so a power cut leaves a valid database.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            # Bounded cache: 512 MB total means SQLite does not get to be greedy.
            self._conn.execute("PRAGMA cache_size=-2000")
            self._conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            self._conn = None
            log.error(
                "Telemetry database unavailable; running in memory only",
                extra={
                    "workflow": "telemetry_init",
                    "state": "failed",
                    "intent": "persist telemetry and decisions across reboot",
                    "path": str(db_path),
                    "reason": "could not open or initialise the database",
                    "error": str(exc),
                },
                exc_info=True,
            )

    def _trim(self) -> None:
        """Ring-buffer retention: the database is bounded by construction."""
        if self._conn is None:
            return
        with self._conn:
            for table, keep in self._retain.items():
                self._conn.execute(
                    f"DELETE FROM {table} WHERE id <= "  # noqa: S608 - table names are literals above
                    f"(SELECT MAX(id) FROM {table}) - ?",
                    (keep,),
                )
