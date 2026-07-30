# ADR-009 — Every allocation change writes a structured decision record

**Status:** Accepted
**Date:** 2026-07-30

## Context

The product's main differentiator is that it explains itself. A user should be able
to see not only what the appliance is doing but *why*:

```
BACKUP ACTIVE

Reason:              NORMAL WAN degradation
Observed:            RTT 820 ms · loss 17% · capacity 1.4 Mbps
Critical demand:     3.1 Mbps
Action:              BACKUP activated
Best-effort traffic: restricted
```

Without this, an autonomous system is a black box that occasionally spends the
user's money for reasons they cannot audit. With it, it is an understandable
machine.

The temptation is to treat this as a dashboard feature — render something readable
from current state when the page loads. That does not work. By the time a user asks
why `BACKUP` activated, the conditions that caused it are gone. Current state
cannot explain a past decision, and reconstructing intent from logs written for a
different purpose produces plausible fiction.

## Decision

**Every allocation change writes a structured decision record at the moment of
decision**, containing the inputs, the thresholds compared against, the action
chosen, and the reason.

```python
telemetry.decisions.record(
    action="activate_backup",
    inputs={"normal_capacity_bps": 1_400_000, "critical_demand_bps": 3_100_000,
            "rtt_ms": 820, "loss_pct": 17.0},
    thresholds={"activation_deficit_bps": 500_000, "dwell_s": 120},
    reason="NORMAL capacity below critical demand beyond activation threshold",
)
```

This is an **architectural constraint on the allocator**, not a logging convention.
The allocator may not change allocation without recording why. The dashboard
renders these records directly rather than deriving explanations.

## Consequences

**Easier:**

- The machine can explain any past decision exactly, with the numbers it actually
  used — not the numbers that apply now.
- Debugging control behaviour becomes a query rather than an investigation. "Why
  did it flap at 3am?" is answerable from the device.
- Cost is auditable: every `BACKUP` byte traces to a recorded justification.
- Tuning thresholds is empirical, because the record captures what the thresholds
  were at the time.

**Harder:**

- The allocator carries a persistence dependency, which complicates its unit tests
  — mitigated by an in-memory recorder in the test harness.
- Records consume storage on a device with a wear-limited SD card, so they are
  subject to ring-buffer retention like all telemetry
  ([ADR-010](ADR-010-state-storage.md)). Retention must be long enough to cover "it
  did something odd last week."
- Discipline required: a new decision path that forgets to record is invisible
  precisely when it matters. Caught in review
  ([SOP-006](../sop/SOP-006-code-review.md)).

**Must stay true:** the record schema stays stable enough for the dashboard to
render old records after upgrades. Schema changes need migration or versioned
records.

## Alternatives considered

**Derive explanations from current state at render time** — rejected: current state
cannot explain a past decision, which is the only kind anyone asks about.

**Rely on the structured logs** — logs carry much of the same data, but they are
written for debugging, rotate aggressively, and have no schema the UI can depend
on. Parsing prose logs to build a user-facing explanation is fragile in both
directions. The decision journal is a first-class data structure; logs remain
complementary.

**Record only exceptional decisions** — cheaper, but "nothing happened, and here is
why that was correct" is genuinely useful: *"BACKUP not used — NORMAL capacity is
sufficient"* is exactly what a worried user wants to see.
