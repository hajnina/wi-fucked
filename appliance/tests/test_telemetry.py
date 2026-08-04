"""Telemetry — concurrent access from the loop thread and the API thread.

Backlog item 7: ``record_decision()`` (and friends) is called from the loop
thread throughout allocator/, discovery/, etc.; ``recent_decisions()`` is read
from the API thread (GET /api/decisions, the diagnostics bundle) while
``flush()`` runs from the loop thread's slow-loop tick. The sqlite connection
is opened with ``check_same_thread=False``, which lifts sqlite3's same-thread
restriction but does not make concurrent statement execution on one
Connection object safe by itself — it still needs external serialisation.
These tests spin up real threads to exercise that, since a single-threaded
test cannot say anything about a race.
"""

from __future__ import annotations

import threading

from wifucked.clock import RealClock
from wifucked.telemetry import Telemetry


class TestConcurrency:
    def test_concurrent_record_and_read_do_not_raise_or_corrupt(self, tmp_path):
        db_path = tmp_path / "telemetry.sqlite3"
        telemetry = Telemetry(RealClock(), db_path, flush_interval_s=0.01)
        errors: list[BaseException] = []
        stop = threading.Event()

        def recorder():
            # Mimics allocator/discovery code scattered across the loop thread.
            try:
                for i in range(300):
                    telemetry.record_decision(
                        action="activate_backup",
                        reason="normal pool insufficient",
                        inputs={"i": i},
                        thresholds={"min_bps": 1_000_000},
                    )
                    telemetry.record_event("wan_flap", {"atomic_id": "wifi:hotel"})
                    telemetry.record_sample("wifi:hotel", 1_000, 500, rtt_ms=20.0, loss_pct=0.0)
            except BaseException as exc:
                errors.append(exc)
            finally:
                stop.set()

        def flusher():
            # Mimics Telemetry.tick() called from the slow loop.
            try:
                while not stop.is_set():
                    telemetry.flush()
            except BaseException as exc:
                errors.append(exc)

        def reader():
            # Mimics GET /api/decisions on the Flask thread.
            try:
                while not stop.is_set():
                    decisions = telemetry.recent_decisions(limit=50)
                    for d in decisions:
                        d.to_dict()
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=recorder),
            threading.Thread(target=flusher),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not any(t.is_alive() for t in threads), "a thread hung — likely a deadlock"
        assert errors == [], f"concurrent access raised: {errors}"

        telemetry.flush()
        telemetry.close()

        # A torn write under concurrent flush() would leave sqlite unreadable
        # or short; every recorded decision must be findable afterwards.
        reopened = Telemetry(RealClock(), db_path)
        stored = reopened.recent_decisions(limit=1000)
        assert len(stored) == 300, "decisions lost or duplicated under concurrent flush"
        reopened.close()

    def test_concurrent_record_decision_loses_no_updates(self):
        telemetry = Telemetry(RealClock())  # in-memory only, no db_path
        threads_n, records_per_thread = 8, 100
        errors: list[BaseException] = []

        def hammer():
            try:
                for i in range(records_per_thread):
                    telemetry.record_decision("noop", "test", {"i": i})
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == []
        # With no db_path, recent_decisions() serves straight from the
        # in-memory ``_recent`` ring, which is capped at 200 by design. What
        # matters for this test is that concurrent appends to that ring never
        # raise and never leave it short of the cap — a lost update or a
        # torn ``del self._recent[:-200]`` would show up as fewer than 200.
        assert len(telemetry.recent_decisions(limit=10_000)) == 200
