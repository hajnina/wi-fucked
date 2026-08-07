# ADR-024 — A never-measured NORMAL atomic gets a small bootstrap headroom floor

**Status:** Accepted
**Date:** 2026-08-07

## Context

[ADR-003](ADR-003-passive-capacity-estimation.md) decided capacity is estimated
passively, from real traffic naturally saturating a link, and explicitly
accepted the consequence: *"Capacity is unknown until the link is used. A
freshly connected idle WAN has no estimate."* Its "must stay true" clause
assumed *"users generate enough traffic to reveal capacity."*

That assumption turned out to be circular. `allocator/__init__.py`
`_usable_capacity()` only counts an atomic's `capacity.down_bps` toward
`normal_capacity` once `capacity.confidence` clears `min_confidence`.
`_build()`'s share ceiling is `min(want_bps, headroom)`, where `headroom`
comes straight from `normal_capacity`. So a NORMAL atomic that has never
been measured contributes **zero** headroom, meaning `ceiling_bps` is zero
for every share on it, meaning `enforce.render()` (which only installs a
route for `ceiling_bps > 0`) never opens a route — meaning no traffic can
ever reach it — meaning `probe.PassiveProber`'s `fold()` never sees a
saturated observation to raise `confidence` above zero in the first place.
No route without capacity, no capacity without a route.

Confirmed directly, not inferred: `appliance/tests/e2e/`'s real fabric/tunnel
proof (PR #48, stage `18_tunnel_download_survives_chaos`, backlog item 16)
showed `route_rules=0` on **every single** `wifucked.enforce` reconcile tick
across a full 150s+ run — a real, freshly-promoted NORMAL atomic, a real
client, and never once a route. Every atomic's `capacity.known` stayed
`false` in every state snapshot across every run of this test. This is a
second, independent instance of the same deadlock class as backlog item 15
(`docs/backlog/traffic-blockers.md`) — item 15 fixed the *demand* side of
`_build()`'s `min(want_bps, headroom)`; this is the *capacity* side of the
same expression, and item 15's fix alone did not touch it.

Every earlier proof of this path (scenario tests, `run_wan_chaos_download_test.sh`)
hand-seeded a fixed `Capacity` directly, exactly like item 15's demand
hand-seeding — which is exactly what let this sit undiscovered.

## Decision

**A NORMAL atomic that has never been measured at all (`capacity.measured_at
is None`) contributes a small, fixed bootstrap headroom** to
`_usable_capacity()`'s total, instead of zero — enough for a first client's
first connection to get a real route and start generating the traffic
`PassiveProber` needs to produce an actual, confident measurement. Once a
single passive fold happens (`measured_at` is no longer `None`), the
bootstrap contribution stops applying to that atomic permanently, even if
confidence later decays below `min_confidence` on a stale, aged-out
estimate — a real (if old) measurement is not the same situation as never
having one, and does not get the guess re-applied on top of it.

This does not weaken ADR-003's core claim that capacity is observed, not
configured — the floor is not offered as a capacity estimate, is never
written into `Capacity.down_bps` itself, never appears with any confidence
value, and never survives past an atomic's first real measurement. It is
scoped to the one situation ADR-003 already flagged as a gap but assumed
would resolve itself ("users generate enough traffic") and turned out not
to, mechanically, for every atomic, always.

The floor value is deliberately small — enough to open a route and carry a
first packet, not to volunteer as a working estimate of a "typical" WAN.
`BOOTSTRAP_HEADROOM_BPS = 256_000` (256 kbps): usable for a SYN, a DNS
query, a small page fetch, or the start of a saturating download that will
itself supply the real measurement within one probing window; small enough
that granting it to every never-measured atomic in a pool does not create a
meaningful over-commitment even before their first real numbers land.

`BACKUP` atomics are unaffected — item 15's demand-side fix was NORMAL-only
for the same reason, and this is the capacity-side half of the identical
expression, so it inherits the identical scope: ADR-006's liveness-budget
accounting must never see a guessed, unmeasured floor as a reason to spend
metered money.

## Consequences

**Easier:**

- A brand-new appliance, or any NORMAL atomic that has simply never carried
  traffic yet, can now actually be used — closing the deadlock stage 18
  found, the same way item 15 closed the demand-side half of it.
- `PassiveProber` gets what it always needed to work at all: real traffic to
  observe. The floor's whole purpose is to make itself unnecessary as soon
  as possible, on every atomic, exactly once.

**Harder:**

- `_usable_capacity()`'s total is no longer purely "capacity we have
  actually observed" — for atomics still in their bootstrap window, it is
  partly "capacity we're willing to spend to find out." Anyone reading
  `normal_capacity` as a measured quantity must know this distinction now
  exists.
- A pool with several simultaneously never-measured atomics sums several
  bootstrap floors, a mild over-commitment versus any single atomic's real
  (unknown) capacity. Deliberately accepted as bounded and small rather than
  engineered away, per the floor's own sizing rationale above.
- One more piece of allocator state to reason about in review: "is this
  headroom real or bootstrap" now matters when debugging a share.

**Must stay true:** the bootstrap floor must never be readable as, logged
as, or fed into anything that treats it as a `Capacity.confidence` above
zero — it is a `_usable_capacity()`-local addition, not a value that ever
gets written back onto the `Atomic`. If a future change makes it easier to
accidentally persist or display the floor as if it were a measurement, that
change has broken this ADR's boundary and needs its own fix, not a workaround
here.

## Alternatives considered

**Synthetic self-probe burst** — have the daemon actively push a short burst
of real traffic through a freshly-promoted atomic itself, specifically to
saturate it once and get a real measurement without waiting for a LAN
client. Preserves ADR-003's "never guess" principle more purely than a
floor does, and was seriously considered. Rejected for this fix: it is
meaningfully more code (a new active-traffic code path, its own scenario
tests, its own interaction with ADR-006 money-safety on any atomic that
could be BACKUP-adjacent), and — same objection ADR-003 raised against
active speedtests generally — it spends real bytes and real time doing
something a real client's own first packet already does for free the moment
a route exists for it to use. Worth reconsidering later if a bootstrapped
floor proves too coarse in practice; not needed to close this deadlock.

**Raise `min_confidence` down to always pass at confidence 0** — rejected:
this is not scoped to "never measured," it also stops the allocator from
ever treating low-confidence-but-real (aged, stale) estimates as
untrustworthy, which is precisely what `min_confidence` exists to gate.
Would quietly reopen a door ADR-003 closed on purpose.

**A fixed, larger default `Capacity` seeded at atomic construction** —
rejected: this writes directly into `Capacity.down_bps`/`confidence`, so it
would report as a real measurement everywhere the dashboard, decision
records, and telemetry read those fields — exactly the "guess treated as a
measurement" ADR-003 was written to prevent. This ADR's floor is
deliberately kept out of `Capacity` entirely for that reason.
