"""The controller.

Given available connectivity, measured capacity, current demand, service
priority, and cost policy — how should traffic be allocated right now?

This is a **reference implementation**: correct on the two invariants, and
deliberately simple everywhere else. WS-C owns it and will replace the decision
logic with something better. What must not change without an ADR is the shape:

* every allocation change writes a decision record (ADR-009);
* hysteresis is asymmetric and dwell-based — no exceptions, ever;
* best-effort can never cause BACKUP activation;
* a low-confidence capacity estimate is not treated as a measurement.

Any change here needs a scenario test (docs/sop/SOP-003).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from wifucked.atomics.model import Atomic, Health, Mode
from wifucked.clock import Clock
from wifucked.demand import ClassDemand
from wifucked.logging import get_logger
from wifucked.policy import DEFAULT_PROFILES, Priority, ServiceProfile, Thresholds
from wifucked.probe import decay_confidence
from wifucked.telemetry import Telemetry

log = get_logger("allocator")


class BackupState(enum.StrEnum):
    IDLE = "idle"
    #: Deficit seen; waiting out the activation dwell before spending money.
    ARMING = "arming"
    ACTIVE = "active"
    #: Recovered; waiting out the recovery dwell before releasing.
    RELEASING = "releasing"


@dataclass(frozen=True, slots=True)
class Share:
    atomic_id: str
    profile_name: str
    #: Ceiling for this class on this atomic. 0 means "not routed here".
    ceiling_bps: int


@dataclass(frozen=True, slots=True)
class Allocation:
    """The desired state that `enforce` reconciles the kernel against."""

    primary_id: str | None
    backup_active: bool
    shares: tuple[Share, ...] = ()
    #: Atomics that must carry no traffic at all.
    quiesced: tuple[str, ...] = ()

    def key(self) -> tuple:
        """Comparable identity — used to detect a genuine change."""
        return (
            self.primary_id,
            self.backup_active,
            tuple(sorted((s.atomic_id, s.profile_name, s.ceiling_bps) for s in self.shares)),
        )


@dataclass
class _Hysteresis:
    state: BackupState = BackupState.IDLE
    since: float = 0.0
    transitions: int = 0
    active_atomic_id: str | None = None
    #: Monotonic time of the last liveness probe per BACKUP atomic.
    last_liveness: dict[str, float] = field(default_factory=dict)


class Allocator:
    def __init__(
        self,
        clock: Clock,
        telemetry: Telemetry,
        thresholds: Thresholds | None = None,
        profiles: tuple[ServiceProfile, ...] = DEFAULT_PROFILES,
    ):
        self._clock = clock
        self._telemetry = telemetry
        self._t = thresholds or Thresholds()
        self._profiles = profiles
        self._h = _Hysteresis(since=clock.now())
        self._last: Allocation | None = None

    @property
    def backup_state(self) -> BackupState:
        return self._h.state

    @property
    def transitions(self) -> int:
        """How many times BACKUP has actually activated or released."""
        return self._h.transitions

    def decide(self, atomics: list[Atomic], demand: dict[str, ClassDemand]) -> Allocation:
        now = self._clock.now()

        pool = [a for a in atomics if a.in_normal_pool]
        backups = [a for a in atomics if a.mode is Mode.BACKUP and a.present]

        # Establish a liveness baseline the first time we see a BACKUP atomic
        # present, so `due_for_liveness` has something to measure elapsed time
        # against. Deliberately not "probed" — just "clock starts now" — so a
        # freshly-seen BACKUP waits a full `liveness_interval_s` before its
        # first probe fires, instead of firing immediately because there was
        # no prior timestamp to compare against.
        for backup in backups:
            self._h.last_liveness.setdefault(backup.id, now)

        normal_capacity = self._usable_capacity(pool, now)
        critical_demand = self._demand_for(demand, Priority.CRITICAL)
        deficit = critical_demand - normal_capacity

        self._step_hysteresis(now, deficit, normal_capacity, critical_demand, backups)

        backup = self._chosen_backup(backups)
        backup_active = self._h.state is BackupState.ACTIVE and backup is not None
        primary = self._best(pool, now) or (backup if backup_active else None)

        allocation = self._build(pool, primary, backup, backup_active, normal_capacity, demand)

        if self._last is None or allocation.key() != self._last.key():
            self._record(allocation, normal_capacity, critical_demand, deficit)
        self._last = allocation
        return allocation

    # -- hysteresis -----------------------------------------------------------

    def _step_hysteresis(
        self,
        now: float,
        deficit: int,
        normal_capacity: int,
        critical_demand: int,
        backups: list[Atomic],
    ) -> None:
        """Asymmetric, dwell-based state machine.

        Activation needs a deficit sustained past `activation_dwell_s`; recovery
        needs a surplus sustained past `recovery_dwell_s`. Different thresholds
        and different dwells, so oscillating input cannot produce oscillating
        output. This is mandatory, not a tuning choice.
        """
        t = self._t
        in_deficit = deficit > t.activation_deficit_bps
        recovered = normal_capacity >= critical_demand + t.recovery_margin_bps
        have_backup = any(b.health is not Health.DOWN for b in backups)

        state = self._h.state
        elapsed = now - self._h.since

        if state is BackupState.IDLE:
            if in_deficit and have_backup:
                self._enter(BackupState.ARMING, now)

        elif state is BackupState.ARMING:
            if not in_deficit or not have_backup:
                # Degradation passed on its own. Tolerating slower service is the
                # correct outcome — no money spent.
                self._enter(BackupState.IDLE, now)
            elif elapsed >= t.activation_dwell_s:
                self._enter(BackupState.ACTIVE, now)
                self._h.transitions += 1

        elif state is BackupState.ACTIVE:
            if not have_backup:
                # The atomic actually carrying BACKUP traffic is gone. There is
                # nothing left to release — go straight to IDLE rather than
                # staying ACTIVE forever with no backup to be active on.
                self._enter(BackupState.IDLE, now)
            elif recovered:
                self._enter(BackupState.RELEASING, now)

        elif state is BackupState.RELEASING:
            if not recovered:
                # Re-activation is a fresh activation, not a bounce: route
                # through ARMING so it honors the same activation dwell as a
                # first-time arm. Without this a single bad sample right after
                # recovery flips straight back to ACTIVE with zero dwell.
                self._enter(BackupState.ARMING, now)
            elif elapsed >= t.recovery_dwell_s:
                self._enter(BackupState.IDLE, now)
                self._h.transitions += 1

    def _enter(self, state: BackupState, now: float) -> None:
        if state is self._h.state:
            return
        log.info(
            "BACKUP state transition",
            extra={
                "workflow": "backup_activation",
                "state": "processing",
                "intent": "protect critical traffic without spending money early",
                "from": str(self._h.state),
                "to": str(state),
                "dwelled_s": round(now - self._h.since, 1),
            },
        )
        self._h.state = state
        self._h.since = now

    # -- liveness -------------------------------------------------------------

    def due_for_liveness(self, atomic: Atomic) -> bool:
        """Whether a BACKUP atomic is due its accounted liveness probe (ADR-006).

        A few hundred bytes at a long interval, so we know the backup works
        before we need it. Every byte is accounted and shown to the user — the
        "zero bytes" claim carries an asterisk, and the asterisk is visible.

        Pure predicate — read-only, no side effects. `decide()` establishes
        the per-atomic baseline (so a freshly-seen BACKUP doesn't fire
        immediately) and `mark_liveness_probed()` records that a probe
        actually happened; callers must call the latter themselves once they
        act on a `True` result here, or this will return `True` forever.
        """
        if atomic.mode is not Mode.BACKUP or not atomic.present:
            return False
        if self._h.state is BackupState.ACTIVE:
            return False  # it is carrying real traffic; no probe needed
        last = self._h.last_liveness.get(atomic.id)
        if last is None:
            # No baseline yet — decide() hasn't observed this atomic as a
            # present BACKUP yet. Not due until a baseline exists.
            return False
        return self._clock.now() - last >= self._t.liveness_interval_s

    def mark_liveness_probed(self, atomic: Atomic) -> None:
        """Record that a liveness probe was actually sent for `atomic` now.

        Explicit, deliberate side effect — call this only after actually
        spending the liveness budget for `atomic`, never as a byproduct of
        merely checking `due_for_liveness`.
        """
        self._h.last_liveness[atomic.id] = self._clock.now()

    # -- helpers --------------------------------------------------------------

    def _usable_capacity(self, pool: list[Atomic], now: float) -> int:
        """Capacity we are willing to believe.

        A low-confidence estimate is a guess, and treating a guess as a
        measurement is how the allocator ends up spending money because it
        mistrusted a link it had simply never measured.
        """
        total = 0
        for atomic in pool:
            if decay_confidence(atomic.capacity, now) >= self._t.min_confidence:
                total += atomic.capacity.down_bps
        return total

    def _best(self, pool: list[Atomic], now: float) -> Atomic | None:
        """Best NORMAL atomic. Failover, not aggregation (ADR-004)."""
        if not pool:
            return None
        healthy = [a for a in pool if a.health is Health.GOOD] or pool
        return max(
            healthy,
            key=lambda a: (
                decay_confidence(a.capacity, now) >= self._t.min_confidence,
                a.capacity.down_bps,
                -(a.quality.rtt_ms or 9999),
            ),
        )

    def _chosen_backup(self, backups: list[Atomic]) -> Atomic | None:
        usable = [b for b in backups if b.health is not Health.DOWN]
        if not usable:
            return None
        return max(usable, key=lambda a: a.capacity.down_bps)

    def _demand_for(self, demand: dict[str, ClassDemand], priority: Priority) -> int:
        total = 0
        for profile in self._profiles:
            if profile.priority is priority:
                entry = demand.get(profile.name)
                if entry:
                    total += entry.down_bps
        return total

    def _build(
        self,
        pool: list[Atomic],
        primary: Atomic | None,
        backup: Atomic | None,
        backup_active: bool,
        normal_capacity: int,
        demand: dict[str, ClassDemand],
    ) -> Allocation:
        shares: list[Share] = []
        quiesced: list[str] = []

        # `primary` can equal `backup` when the NORMAL pool is empty and
        # BACKUP has been promoted to primary (see `decide()`). There is no
        # separate NORMAL capacity in that case, so the headroom-based
        # primary path below doesn't apply — the backup-active block further
        # down (full demand, gated by `may_use_backup`) is the only share
        # builder that should run for this atomic. Building both would emit
        # two conflicting `Share` entries for the same atomic/profile pair.
        if primary is not None and primary is not backup:
            headroom = max(0, normal_capacity)
            for profile in sorted(self._profiles, key=lambda p: p.priority):
                want = demand.get(profile.name)
                want_bps = want.down_bps if want else 0
                # Critical is served first; best-effort gets what survives. This
                # is what "protect the important traffic" means in practice — not
                # that critical takes everything.
                give = min(want_bps, headroom) if headroom else 0
                headroom = max(0, headroom - give)
                shares.append(Share(primary.id, profile.name, give))

        if backup is not None:
            if backup_active:
                # Only classes permitted to spend money may route here. This is
                # what stops best-effort forcing a metered connection open.
                for profile in self._profiles:
                    if profile.may_use_backup:
                        want = demand.get(profile.name)
                        shares.append(Share(backup.id, profile.name, want.down_bps if want else 0))
                    else:
                        shares.append(Share(backup.id, profile.name, 0))
            else:
                quiesced.append(backup.id)

        for atomic in pool:
            if primary is None or atomic.id != primary.id:
                quiesced.append(atomic.id)

        return Allocation(
            primary_id=primary.id if primary else None,
            backup_active=backup_active,
            shares=tuple(shares),
            quiesced=tuple(sorted(set(quiesced))),
        )

    def _record(
        self,
        allocation: Allocation,
        normal_capacity: int,
        critical_demand: int,
        deficit: int,
    ) -> None:
        if allocation.backup_active:
            action, reason = (
                "activate_backup",
                "NORMAL capacity below critical demand beyond the activation threshold",
            )
        elif self._h.state is BackupState.ARMING:
            # A healthy BACKUP is waiting out the activation dwell. It is not
            # yet carrying traffic, but there is nothing wrong with
            # connectivity — reporting "no usable connection" here would be a
            # lie the dashboard would show to a user watching a healthy
            # backup arm.
            action, reason = (
                "arming_backup",
                "NORMAL capacity below critical demand; BACKUP arming, dwell not yet elapsed",
            )
        elif allocation.primary_id is None:
            action, reason = "no_connectivity", "no usable NORMAL or BACKUP connection"
        else:
            action, reason = (
                "allocate_normal",
                "NORMAL capacity is sufficient for critical demand",
            )

        self._telemetry.record_decision(
            action=action,
            reason=reason,
            inputs={
                "normal_capacity_bps": normal_capacity,
                "critical_demand_bps": critical_demand,
                "deficit_bps": deficit,
                "primary_id": allocation.primary_id,
                "backup_state": str(self._h.state),
            },
            thresholds={
                "activation_deficit_bps": self._t.activation_deficit_bps,
                "activation_dwell_s": self._t.activation_dwell_s,
                "recovery_margin_bps": self._t.recovery_margin_bps,
                "recovery_dwell_s": self._t.recovery_dwell_s,
            },
        )
