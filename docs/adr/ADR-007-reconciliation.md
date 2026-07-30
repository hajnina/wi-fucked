# ADR-007 — Enforcement is reconciliation, not command

**Status:** Accepted
**Date:** 2026-07-30

## Context

The daemon programs kernel state — qdiscs, nftables rules, routing tables, `ip
rule` entries — and that state can diverge from what the daemon believes it
installed:

- An interface goes down and comes back; its qdisc is gone.
- NetworkManager rewrites a routing table.
- The daemon restarts after a crash and has no memory of what it installed.
- Someone runs `tc` by hand while debugging.
- An OTA update replaces the daemon mid-flight.

A fire-and-forget model — install a rule, assume it persists — fails silently in
all of these. Traffic keeps flowing, unshaped and unclassified, while the dashboard
reports a policy that is no longer in effect. The user experiences bufferbloat that
the system insists it is preventing.

## Decision

**Enforcement works by reconciliation.** The allocator declares *desired* kernel
state. The fast loop reads *actual* kernel state, diffs it against desired, and
applies only the difference.

- Reconciliation is idempotent and runs every fast-loop tick (~1 s).
- The daemon never assumes a rule it installed is still present.
- On start, the daemon reconciles rather than initialising — whatever is already in
  the kernel is the starting point, not a blank slate.

## Consequences

**Easier:**

- Self-healing. Interface churn, external interference, and crashes are all
  repaired automatically within a second, with no special-case handling for each.
- Restart is not a special case. Cold start and steady state run the same code
  path, which means the rarely-exercised path is the one exercised constantly.
- It composes with [ADR-008](ADR-008-fail-to-last-known-good.md): the daemon can
  leave state behind on exit precisely because the next start will reconcile it.

**Harder:**

- Reading kernel state is more work than writing it, and must be cheap enough to
  run at 1 Hz on a 1 GHz A53. Parsing `tc` and `nft` output is fiddly and a likely
  source of bugs.
- Every enforcement action must be expressible as a comparison, which constrains
  how rules are structured — they need stable identifiers to diff against.
- A reconciliation bug is a *persistent* bug: it re-applies every second. A loop
  that fights another daemon over the same resource will do so at 1 Hz forever.

**Must stay true:** reading kernel state stays cheap. If it becomes expensive,
reconciliation moves to a slower cadence for some resources — which needs measuring
first, not assuming.

## Alternatives considered

**Fire-and-forget** — rejected; it is the failure mode this ADR exists to prevent,
and it fails invisibly.

**Event-driven repair via netlink subscription** — cheaper than polling and worth
adding as an *accelerator* (react immediately to a link event rather than waiting
for the tick). Rejected as the primary mechanism because it only catches changes it
observes; a crash-restart or a missed event leaves the divergence permanent.
Reconciliation catches everything, including what we forgot to subscribe to.

**Kernel-side watchdogs** — no general mechanism exists for the state we care
about, and inventing one is far more work than reading it.
