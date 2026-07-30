# ADR-006 — BACKUP gets a small, accounted liveness budget

**Status:** Accepted
**Date:** 2026-07-30

## Context

Two product requirements collide.

**A `BACKUP` connection should consume zero bytes** when not needed, even if it has
idle capacity. It represents real money, and minimising its use is an optimisation
objective in itself.

**Activation must not flap**, and it must work when needed. But you cannot fail
over onto a link whose health is unknown. A phone that has silently lost its data
allowance, drifted out of coverage, or dropped its tether looks identical to a
healthy one until you try to use it — and the moment you discover it is dead is
precisely the moment the primary has already failed.

Strict zero means the first activation is a gamble taken during an outage.

## Decision

**`BACKUP` atomics get a liveness budget:** a small, bounded, accounted probe at a
configurable interval — a few hundred bytes, default every 15 minutes.

- A full validation probe runs only at activation.
- Every liveness byte is accounted and displayed in the dashboard, alongside
  activation data, under its own label.
- The budget is user-configurable, including to zero for users who genuinely prefer
  the gamble.

Roughly 3 KB per day. Set against the alternative — discovering a dead backup
during an outage — this is the honest trade, and the dashboard states it rather
than hiding it.

## Consequences

**Easier:**

- Activation decisions rest on known link health, so failover works when it
  matters.
- Hysteresis has a real signal to work with rather than inferring from silence.
- The cost is visible. A user who asks "why did this use 90 KB this month?" gets a
  complete answer.

**Harder:**

- **"Zero bytes" is now "nearly zero bytes"**, and the product must say so plainly.
  A hidden asterisk here would be a trust problem far more damaging than the data.
- One more configurable knob, and one more thing to explain.
- On an extremely constrained plan the budget is non-trivial, hence the option to
  disable it — with the consequence stated.

**Must stay true:** the budget stays small. Any growth in liveness traffic needs a
superseding ADR, not a default change. This is the number that must not creep.

## Alternatives considered

**Strict zero bytes** — rejected. It makes the first activation an untested leap
during an outage, which is when reliability matters most. A backup you cannot trust
is not insurance.

**Probe only at activation** — no periodic cost, but the dashboard cannot honestly
report backup availability between activations, and hysteresis has nothing to work
with. It also concentrates all the risk at the worst moment.

**Infer health from the modem or OS link state** — free, and worth using as an
additional signal, but link-up says nothing about whether data actually flows.
Tethered phones report a healthy link with an exhausted allowance routinely. Useful
as a fast negative, insufficient as a positive.
