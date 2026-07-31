"""The atomic registry — current world state, and the modes the user chose.

Discovery reports what is present *right now*. The registry merges that against
what we already knew, so an atomic that disappears keeps its mode, its learned
capacity, and its cost history, and is recognised instantly when it returns.
That recognition is the plug-and-play promise (ADR-002).
"""

from __future__ import annotations

import json
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
        if state_path:
            self._load()

    # -- access ---------------------------------------------------------------

    def all(self) -> list[Atomic]:
        return sorted(self._atomics.values(), key=lambda a: (not a.present, a.label))

    def get(self, atomic_id: str) -> Atomic | None:
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

        for fresh in seen:
            seen_ids.add(fresh.id)
            known = self._atomics.get(fresh.id)
            if known is None:
                self._atomics[fresh.id] = replace(
                    fresh, first_seen=now, last_seen=now, present=True
                )
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
            )
            if was_absent:
                log.info(
                    "Known connection returned; restoring previous policy",
                    extra={
                        "workflow": "wan_discovery",
                        "state": "completed",
                        "intent": "recognise a connection without reconfiguration",
                        "atomic_id": fresh.id,
                        "label": fresh.label,
                        "mode": str(known.mode),
                        "absent_for_s": round(now - (known.last_seen or now), 1),
                    },
                )

        for atomic in list(self._atomics.values()):
            if atomic.present and atomic.id not in seen_ids:
                self._atomics[atomic.id] = replace(
                    atomic, present=False, health=Health.ABSENT, ifname=None
                )
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
        atomic = self._atomics.get(atomic_id)
        if atomic is None:
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

        previous = atomic.mode
        self._atomics[atomic_id] = replace(atomic, mode=mode)
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
        return self._atomics[atomic_id]

    def update_capacity(self, atomic_id: str, capacity: Capacity) -> None:
        atomic = self._atomics.get(atomic_id)
        if atomic is not None:
            self._atomics[atomic_id] = replace(atomic, capacity=capacity)

    def update_health(self, atomic_id: str, health: Health) -> None:
        atomic = self._atomics.get(atomic_id)
        if atomic is None or atomic.health is health:
            return
        self._atomics[atomic_id] = replace(atomic, health=health)
        log.info(
            "Connection health changed",
            extra={
                "workflow": "health_transition",
                "state": "completed",
                "intent": "track whether a connection can carry traffic",
                "atomic_id": atomic_id,
                "label": atomic.label,
                "health_from": str(atomic.health),
                "health_to": str(health),
            },
        )

    def add_cost(self, atomic_id: str, *, consumed: int = 0, liveness: int = 0) -> None:
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

    def persist(self) -> None:
        if not self._state_path:
            return
        payload = {
            "version": 1,
            "atomics": {
                atomic.id: {
                    "kind": str(atomic.kind),
                    "label": atomic.label,
                    "mode": str(atomic.mode),
                    "capacity": {
                        "down_bps": atomic.capacity.down_bps,
                        "up_bps": atomic.capacity.up_bps,
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
                for atomic in self._atomics.values()
            },
        }
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

        for atomic_id, entry in payload.get("atomics", {}).items():
            try:
                self._atomics[atomic_id] = Atomic(
                    id=atomic_id,
                    kind=Kind(entry["kind"]),
                    label=entry.get("label", atomic_id),
                    mode=Mode(entry.get("mode", "unused")),
                    health=Health.ABSENT,
                    capacity=Capacity(**entry.get("capacity", {})),
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
