"""Regression tests for backlog item 13, bug 1.

`Allocator.due_for_liveness` used to mutate `_h.last_liveness` as a side
effect of what was supposed to be a read-only predicate, and its guard
short-circuited to `False` on a fresh atomic's very first call (`last is
None`), which made the whole `and` False and fell through to stamp `now` and
return `True` immediately — firing on the very first call for every BACKUP
atomic regardless of `liveness_interval_s`.

The fix: `decide()` establishes a per-atomic liveness baseline the first time
it observes a present BACKUP atomic; `due_for_liveness()` is a pure query
against that baseline; `mark_liveness_probed()` is the explicit, deliberate
stamp a caller performs after actually spending the liveness budget.
"""

from __future__ import annotations

from wifucked.allocator import Allocator
from wifucked.atomics.model import Atomic, Health, Kind, Mode
from wifucked.clock import VirtualClock
from wifucked.policy import Thresholds
from wifucked.telemetry import Telemetry


def _backup(atomic_id: str = "usbtether:phone") -> Atomic:
    return Atomic(
        id=atomic_id,
        kind=Kind.USB_TETHER,
        label="Phone",
        mode=Mode.BACKUP,
        health=Health.GOOD,
        present=True,
    )


def _allocator(clock: VirtualClock, liveness_interval_s: float = 900.0) -> Allocator:
    return Allocator(
        clock,
        Telemetry(clock, None),
        thresholds=Thresholds(liveness_interval_s=liveness_interval_s),
    )


def test_fresh_backup_does_not_fire_before_decide_has_seen_it():
    """No baseline yet (decide() hasn't run) -> not due, not a crash."""
    clock = VirtualClock()
    allocator = _allocator(clock)
    atomic = _backup()

    assert allocator.due_for_liveness(atomic) is False


def test_fresh_backup_waits_a_full_interval_after_first_seen():
    """The historical bug fired on the very first call. It must not."""
    clock = VirtualClock()
    allocator = _allocator(clock, liveness_interval_s=900.0)
    atomic = _backup()

    allocator.decide([atomic], {})  # establishes the baseline
    assert allocator.due_for_liveness(atomic) is False, (
        "must not fire immediately on first observation"
    )

    clock.advance(899)
    allocator.decide([atomic], {})
    assert allocator.due_for_liveness(atomic) is False, "must wait the full interval"

    clock.advance(2)
    allocator.decide([atomic], {})
    assert allocator.due_for_liveness(atomic) is True, "due once the interval has elapsed"


def test_due_for_liveness_is_a_pure_query():
    """Calling the predicate repeatedly must not itself consume the due-ness —
    only `mark_liveness_probed` may do that."""
    clock = VirtualClock()
    allocator = _allocator(clock, liveness_interval_s=100.0)
    atomic = _backup()

    allocator.decide([atomic], {})
    clock.advance(101)
    allocator.decide([atomic], {})

    assert allocator.due_for_liveness(atomic) is True
    assert allocator.due_for_liveness(atomic) is True, "a pure check must not clear due-ness"
    assert allocator.due_for_liveness(atomic) is True


def test_mark_liveness_probed_resets_the_interval():
    clock = VirtualClock()
    allocator = _allocator(clock, liveness_interval_s=100.0)
    atomic = _backup()

    allocator.decide([atomic], {})
    clock.advance(101)
    allocator.decide([atomic], {})
    assert allocator.due_for_liveness(atomic) is True

    allocator.mark_liveness_probed(atomic)
    assert allocator.due_for_liveness(atomic) is False, "probing resets the wait"

    clock.advance(99)
    assert allocator.due_for_liveness(atomic) is False
    clock.advance(2)
    assert allocator.due_for_liveness(atomic) is True
