"""The capacity model.

Capacity is an observed property of a connection *at a point in time*, not a
configured constant. The estimator lives in :mod:`dirty.probe`; this module owns
the longer-lived view — per-network history, and eventually the learning that
turns "this campsite is 40 Mbps in the morning and 1 Mbps at night" into a
prediction.

WS-B owns this module. Phase 2 adds learning; Phase 0 ships the interface and
the one rule that must not be broken when it arrives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from dirty.atomics.model import Capacity

__all__ = ["Capacity", "HistoricalHint", "Learner", "NullLearner", "blend"]


@dataclass(frozen=True, slots=True)
class HistoricalHint:
    """What history suggests this connection can do right now."""

    atomic_id: str
    down_bps: int
    up_bps: int
    #: How much this hint should be trusted, given how much history exists and
    #: how well past predictions held up.
    confidence: float
    basis: str = ""


class Learner(Protocol):
    def hint(self, atomic_id: str, at_wall: float) -> HistoricalHint | None:
        """A prediction for this connection at this time of day, if any."""

    def observe(self, atomic_id: str, capacity: Capacity, at_wall: float) -> None:
        """Feed a measurement into the model."""


class NullLearner(Learner):
    """No history, no predictions. The Phase 0 and Phase 1 behaviour."""

    def hint(self, atomic_id: str, at_wall: float) -> HistoricalHint | None:
        return None

    def observe(self, atomic_id: str, capacity: Capacity, at_wall: float) -> None:
        return None


def blend(measured: Capacity, hint: HistoricalHint | None) -> Capacity:
    """Combine a live measurement with a historical hint.

    **Learned information is evidence, never truth: the current network state
    always wins.** A hint may only fill a gap where there is no measurement — it
    can never override, moderate, or argue with one. A campsite that was fast
    last week is not fast now if we just measured it slow, and an appliance that
    believed otherwise would be confidently wrong at exactly the wrong moment.
    """
    if measured.known or hint is None:
        return measured
    return Capacity(
        down_bps=hint.down_bps,
        up_bps=hint.up_bps,
        # Deliberately capped below anything a real measurement produces, so the
        # allocator can tell a recollection from an observation.
        confidence=min(hint.confidence, 0.4),
        measured_at=measured.measured_at,
    )
