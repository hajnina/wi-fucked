"""The atomic registry — current world state, and the modes the user chose.

Discovery reports what is present *right now*. The registry merges that against
what we already knew, so an atomic that disappears keeps its mode, its learned
capacity, and its cost history, and is recognised instantly when it returns.
That recognition is the plug-and-play promise (ADR-002).
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

from wifucked.atomics.model import Atomic, Capacity, Cost, Health, Mode
from wifucked.clock import Clock
from wifucked.logging import get_logger

log = get_logger("atomics")


class Registry:
    """Holds every atomic we have ever seen, present or not."""

    def __init__(self, clock: Clock, state_path: Path | None = None):
        self._clock = clock
        self._state_path = state_path
        self._atomics: dict[str, Atomic] = {}
        # Guards every read and mutation of ``self._atomics``. The loop thread's
        # ``observe()`` (called each medium loop from discovery) and the API
        # thread's ``set_mode()``/``persist()`` (POST /api/atomics/<id>/mode)
        # touch this dict concurrently with no other synchronisation — a plain
        # dict mutated from two threads can raise ``RuntimeError: dictionary
        # changed size during iteration`` on a reader, or hand back a torn view
        # mid-merge. Mirrors ``fabric/peers.py``'s ``PeerRegistry._guard()``,
        # minus the ``fcntl.flock`` half: that guards multiple *processes*
        # sharing a peers file, which does not apply here — the appliance
        # daemon is single-process, so a threading.Lock is sufficient.
        self._lock = threading.Lock()
        if state_path:
            self._load()

    # -- access ---------------------------------------------------------------

    def all(self) -> list[Atomic]:
        with self._lock:
            return sorted(self._atomics.values(), key=lambda a: (not a.present, a.label))

    def get(self, atomic_id: str) -> Atomic | None:
        with self._lock:
            return self._atomics.get(atomic_id)

    def present(self) -> list[Atomic]:
        return [a for a in self.all() if a.present]

    def normal_pool(self) -> list[Atomic]:
        return [a for a in self.all() if a.in_normal_pool]

    def backups(self) -> list[Atomic]:
        return [a for a in self.all() if a.mode is Mode.BACKUP and a.present]

    # -- mutation -------------------------------------------------------------

    def observe(self, seen: list[Atomic]) -> None:
        """Merge a discovery sweep into the registry.

        Discovered fields (presence, ifname, attributes) come from the sweep.
        Remembered fields (mode, capacity, cost) are preserved from what we
        already held — discovery must never reset a user's choice.
        """
        now = self._clock.now()
        seen_ids = set()
        # Collected inside the lock, logged outside it — logging is I/O and
        # need not happen while another thread is blocked on the registry.
        discovered: list[Atomic] = []
        returned: list[Atomic] = []  # the *known* (pre-merge) atomic
        disappeared: list[Atomic] = []

        with self._lock:
            for fresh in seen:
                seen_ids.add(fresh.id)
                known = self._atomics.get(fresh.id)
                if known is None:
                    self._atomics[fresh.id] = replace(
                        fresh, first_seen=now, last_seen=now, present=True
                    )
                    discovered.append(fresh)
                    continue

                was_absent = not known.present
                self._atomics[fresh.id] = replace(
                    known,
                    label=fresh.label,
                    ifname=fresh.ifname,
                    attributes=fresh.attributes,
                    present=True,
                    last_seen=now,
                    health=fresh.health if was_absent else known.health,
                    # Sticky: once actually connected to, always considered so —
                    # a later scan reporting "not connected right now" must not
                    # erase that this atomic is worth remembering (persist() below
                    # relies on this to bound what gets written to disk).
                    ever_connected=known.ever_connected or fresh.ever_connected,
                )
                if was_absent:
                    returned.append(known)

            for atomic in list(self._atomics.values()):
                if atomic.present and atomic.id not in seen_ids:
                    self._atomics[atomic.id] = replace(
                        atomic, present=False, health=Health.ABSENT, ifname=None
                    )
                    disappeared.append(atomic)

        for fresh in discovered:
            log.info(
                "Discovered new connection",
                extra={
                    "workflow": "wan_discovery",
                    "state": "completed",
                    "intent": "surface a new connection for the user to classify",
                    "atomic_id": fresh.id,
                    "kind": str(fresh.kind),
                    "label": fresh.label,
                    "mode": str(fresh.mode),
                },
            )
        for known in returned:
            log.info(
                "Known connection returned; restoring previous policy",
                extra={
                    "workflow": "wan_discovery",
                    "state": "completed",
                    "intent": "recognise a connection without reconfiguration",
                    "atomic_id": known.id,
                    "label": known.label,
                    "mode": str(known.mode),
                    "absent_for_s": round(now - (known.last_seen or now), 1),
                },
            )
        for atomic in disappeared:
            log.warning(
                "Connection disappeared",
                extra={
                    "workflow": "wan_discovery",
                    "state": "completed",
                    "intent": "stop allocating traffic to a vanished connection",
                    "atomic_id": atomic.id,
                    "label": atomic.label,
                    "mode": str(atomic.mode),
                    "reason": "not present in discovery sweep",
                },
            )

    def set_mode(self, atomic_id: str, mode: Mode) -> Atomic | None:
        with self._lock:
            atomic = self._atomics.get(atomic_id)
            if atomic is None:
                found = False
            else:
                previous = atomic.mode
                self._atomics[atomic_id] = replace(atomic, mode=mode)
                updated = self._atomics[atomic_id]
                found = True

        if not found:
            log.warning(
                "Mode change requested for unknown connection",
                extra={
                    "workflow": "set_mode",
                    "state": "failed",
                    "intent": "apply a user's classification choice",
                    "atomic_id": atomic_id,
                    "reason": "no such atomic in registry",
                },
            )
            return None

        log.info(
            "Connection mode changed",
            extra={
                "workflow": "set_mode",
                "state": "completed",
                "intent": "apply a user's classification choice",
                "atomic_id": atomic_id,
                "label": atomic.label,
                "mode_from": str(previous),
                "mode_to": str(mode),
            },
        )
        self.persist()
        return updated

    def update_capacity(self, atomic_id: str, capacity: Capacity) -> None:
        with self._lock:
            atomic = self._atomics.get(atomic_id)
            if atomic is not None:
                self._atomics[atomic_id] = replace(atomic, capacity=capacity)

    def update_health(self, atomic_id: str, health: Health) -> None:
        with self._lock:
            atomic = self._atomics.get(atomic_id)
            if atomic is None or atomic.health is health:
                return
            previous_health = atomic.health
            self._atomics[atomic_id] = replace(atomic, health=health)
            label = atomic.label
        log.info(
            "Connection health changed",
            extra={
                "workflow": "health_transition",
                "state": "completed",
                "intent": "track whether a connection can carry traffic",
                "atomic_id": atomic_id,
                "label": label,
                "health_from": str(previous_health),
                "health_to": str(health),
            },
        )

    def add_cost(self, atomic_id: str, *, consumed: int = 0, liveness: int = 0) -> None:
        with self._lock:
            atomic = self._atomics.get(atomic_id)
            if atomic is None:
                return
            cost = atomic.cost
            self._atomics[atomic_id] = replace(
                atomic,
                cost=replace(
                    cost,
                    consumed_bytes=cost.consumed_bytes + consumed,
                    liveness_bytes=cost.liveness_bytes + liveness,
                ),
            )

    # -- persistence ----------------------------------------------------------
    #
    # Only the remembered fields are stored. Presence and ifname are rediscovered
    # on every start; writing them would just be a lie waiting to be read back.
    #
    # What gets a row at all is bounded (ADR-010): every SSID the radio has ever
    # scanned would otherwise become a permanent entry, growing the file forever
    # and wearing the SD card on every rewrite. An atomic only earns persistence
    # once the user has actually decided something about it (`mode != UNUSED`)
    # or it was genuinely connected to at least once (`ever_connected`) — a
    # network glimpsed once and never joined is dropped from the write, though
    # it stays in the in-memory registry for as long as the process runs.

    def _worth_persisting(self, atomic: Atomic) -> bool:
        return atomic.mode is not Mode.UNUSED or atomic.ever_connected

    @staticmethod
    def _reconstruct_capacity(
        entry: dict, elapsed_since_persist: float, now_monotonic: float
    ) -> Capacity:
        """Rebuild a `Capacity` from a persisted entry, translating its
        monotonic-clock `measured_age_s` (see persist()) back into a
        `measured_at` timestamp valid against *this* process's `Clock.now()`.
        """
        measured_age_s = entry.get("measured_age_s")
        measured_at = (
            now_monotonic - (measured_age_s + elapsed_since_persist)
            if measured_age_s is not None
            else None
        )
        return Capacity(
            down_bps=entry.get("down_bps", 0),
            up_bps=entry.get("up_bps", 0),
            confidence=entry.get("confidence", 0.0),
            measured_at=measured_at,
        )

    def persist(self) -> None:
        if not self._state_path:
            return
        # Snapshot under the lock, then build and write the payload outside it —
        # JSON serialisation and the disk write are comparatively slow, and
        # holding the lock across them would block the loop thread's observe()
        # (or the API thread's set_mode()) for the duration of a write.
        # ``Atomic`` instances are only ever replaced wholesale via
        # dataclasses.replace(), never mutated in place, so a snapshot taken
        # under the lock is safe to read afterwards without it.
        with self._lock:
            total = len(self._atomics)
            candidates = [a for a in self._atomics.values() if self._worth_persisting(a)]
        # ``capacity.measured_at`` is a ``Clock.now()`` (monotonic) timestamp,
        # meaningful only as a difference against another reading from the same
        # Clock instance in the same process (see clock.py). It cannot be
        # persisted and reloaded verbatim: CLOCK_MONOTONIC's reference point
        # does not survive a reboot, so a raw persisted value could come back
        # *larger* than the freshly-started process's ``now()``, making
        # ``decay_confidence()``'s ``age = max(0.0, now - measured_at)`` clamp
        # to 0 — a measurement from hours ago would read as "just measured",
        # which is a false-confidence bug in the opposite direction from the
        # one this fix closes (docs/backlog/traffic-blockers.md item 12).
        # Instead we persist how *stale* each measurement already was at
        # persist time (``measured_age_s``, monotonic-clock delta, immune to
        # the reboot discontinuity) plus one wall-clock anchor
        # (``persisted_at_wall``) for the whole payload, and on load add the
        # wall-clock time that has passed since to get the true elapsed age.
        persisted_at_wall = self._clock.wall()
        payload = {
            "version": 1,
            "persisted_at_wall": persisted_at_wall,
            "atomics": {
                atomic.id: {
                    "kind": str(atomic.kind),
                    "label": atomic.label,
                    "mode": str(atomic.mode),
                    "ever_connected": atomic.ever_connected,
                    "capacity": {
                        "down_bps": atomic.capacity.down_bps,
                        "up_bps": atomic.capacity.up_bps,
                        "confidence": atomic.capacity.confidence,
                        "measured_age_s": (
                            max(0.0, self._clock.now() - atomic.capacity.measured_at)
                            if atomic.capacity.measured_at is not None
                            else None
                        ),
                    },
                    "cost": {
                        "metered": atomic.cost.metered,
                        "consumed_bytes": atomic.cost.consumed_bytes,
                        "liveness_bytes": atomic.cost.liveness_bytes,
                        "activations": atomic.cost.activations,
                        "active_seconds": atomic.cost.active_seconds,
                    },
                    "attributes": atomic.attributes,
                }
                for atomic in candidates
            },
        }
        log.info(
            "Persisting atomic registry",
            extra={
                "workflow": "registry_persist",
                "state": "processing",
                "intent": "remember user mode choices and ever-connected atomics across reboot",
                "total_atomics": total,
                "persisted_atomics": len(candidates),
                "skipped_atomics": total - len(candidates),
            },
        )
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(self._state_path)
        except OSError as exc:
            log.error(
                "Could not persist atomic state; continuing with in-memory state",
                extra={
                    "workflow": "registry_persist",
                    "state": "failed",
                    "intent": "remember user mode choices across reboot",
                    "path": str(self._state_path),
                    "reason": "write failed",
                    "error": str(exc),
                },
                exc_info=True,
            )

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.error(
                "Could not read atomic state; starting with an empty registry",
                extra={
                    "workflow": "registry_load",
                    "state": "failed",
                    "intent": "restore remembered connections at startup",
                    "path": str(self._state_path),
                    "reason": "unreadable or malformed state file",
                    "error": str(exc),
                },
                exc_info=True,
            )
            return

        from wifucked.atomics.model import Kind  # local import: avoids a cycle at import time

        # See persist()'s comment on why measured_at cannot be persisted
        # verbatim. ``elapsed_since_persist`` is how much wall-clock time has
        # passed since this file was written — 0 (and thus a no-op below) for
        # a normal restart moments after a crash, correctly large after a Pi
        # sat powered off for a week. Missing/malformed ``persisted_at_wall``
        # (e.g. a file from before this field existed) degrades to 0, which is
        # the conservative direction for age (undercounts elapsed time rather
        # than fabricating a possibly-huge one).
        persisted_at_wall = payload.get("persisted_at_wall")
        now_wall = self._clock.wall()
        elapsed_since_persist = (
            max(0.0, now_wall - persisted_at_wall)
            if isinstance(persisted_at_wall, (int, float))
            else 0.0
        )
        now_monotonic = self._clock.now()

        for atomic_id, entry in payload.get("atomics", {}).items():
            try:
                self._atomics[atomic_id] = Atomic(
                    id=atomic_id,
                    kind=Kind(entry["kind"]),
                    label=entry.get("label", atomic_id),
                    mode=Mode(entry.get("mode", "unused")),
                    health=Health.ABSENT,
                    ever_connected=entry.get("ever_connected", False),
                    capacity=self._reconstruct_capacity(
                        entry.get("capacity", {}), elapsed_since_persist, now_monotonic
                    ),
                    cost=Cost(**entry.get("cost", {})),
                    attributes=entry.get("attributes", {}),
                )
            except (KeyError, ValueError) as exc:
                log.warning(
                    "Skipping unreadable atomic entry",
                    extra={
                        "workflow": "registry_load",
                        "state": "skipped",
                        "intent": "restore remembered connections at startup",
                        "atomic_id": atomic_id,
                        "reason": "entry did not match the expected schema",
                        "error": str(exc),
                    },
                )

        log.info(
            "Restored remembered connections",
            extra={
                "workflow": "registry_load",
                "state": "completed",
                "intent": "recognise known connections without reconfiguration",
                "count": len(self._atomics),
            },
        )
