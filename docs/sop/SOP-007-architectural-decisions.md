# SOP-007 — Architectural decisions

## ADR or not?

Write an ADR when a change affects:

- A **module boundary** — what a module owns, or what it exposes.
- A **persistence format** — config schema, telemetry schema, decision-record shape.
- The **enforcement strategy** — which kernel primitives are used and how.
- The **radio model** — profiles, channel policy, SSID/BSSID handling.
- The **release contract** — versioning, asset names, the OTA manifest, the fabric
  protocol floor.
- A **product promise** — anything touching the two invariants
  ([SOP-003](SOP-003-testing.md)).

You do not need one for: adding a feature within an existing boundary, fixing a
bug, refactoring without changing an interface, tuning a threshold that the
existing ADR already anticipated.

**The tell:** if you are about to write a code comment explaining *why the
architecture is like this*, that comment is an ADR. Write it there and link to it.

## ADRs are immutable

An ADR records a decision **at a point in time**, with the context that made it
reasonable. That context is the valuable part — it lets a future reader tell
whether the decision still applies, or whether the world changed.

So:

- **Never edit a decision.** Write a new ADR that supersedes it.
- The old one stays, with a `Superseded by ADR-0NN` header added.
- The new one explains *what changed in the world* that made the old call wrong —
  not just that it is now wrong.

Typo fixes and broken links are fine to edit. The decision and its reasoning are
not.

This is the opposite of how SOPs work, deliberately
([SOP-010](SOP-010-keeping-documentation-current.md)).

## Writing one

Copy [`../adr/TEMPLATE.md`](../adr/TEMPLATE.md). Number it sequentially — take the
next free number; races are resolved by whoever merges second renumbering.

```
docs/adr/ADR-018-short-kebab-title.md
```

### The sections that matter

**Context.** What is true about the world that forces a decision. Constraints,
measurements, hardware facts, prior decisions it interacts with. A reader in two
years should be able to tell whether this context still holds.

**Decision.** One paragraph, stated plainly, in the present tense. "The AP is the
anchor" — not "we should probably make the AP the anchor."

**Consequences.** Honest, including the bad ones. Every real decision costs
something; an ADR listing only benefits is marketing, and the next person will
discover the cost the hard way. State what this makes harder, what it forecloses,
and what has to be true for it to keep making sense.

**Alternatives considered.** What you rejected and why. This is what stops the
same debate reopening every six months — and occasionally it shows the rejected
option is now the right one.

### Length

One page. If it needs more, the decision is probably several decisions.

## Getting it approved

An ADR goes through review like code, in the PR that implements it — not before,
not after. Reviewing a decision separately from its consequences produces ADRs
that read well and don't survive implementation.

For a decision large enough that implementation would be wasted if rejected, open
the ADR alone first and say so in the description.

## When you find an ADR is wrong

This will happen — particularly to the ones written before the hardware spike
([`../radio-spike.md`](../radio-spike.md)), which encode reasonable expectations
about driver behaviour rather than verified facts.

1. Do not work around it quietly. A workaround that contradicts a documented
   decision is invisible to everyone reading the documentation.
2. Do not edit it.
3. Write the superseding ADR, stating what you learned that changed the answer.
4. Update every reference — `CLAUDE.md`, the SOPs, the other ADRs. `grep` for the
   old number.

Discovering an ADR is wrong is good. It means the decision was written down
precisely enough to be falsifiable, which is the entire point.

## The index

`docs/adr/README.md` lists every ADR with status. Update it in the same PR.
