# ADR-025 — Download is shaped too, via an IFB redirect

**Status:** Accepted
**Date:** 2026-08-08

## Context

`enforce._apply_cake()` programs `tc qdisc ... dev <ifname> root cake bandwidth
<up_bps>bit` on each shaped atomic — `tc`'s root qdisc only ever governs
*egress*, the direction traffic leaves this box, which on a WAN atomic is
upload. Nothing in this codebase ever shaped the other direction. The
function's own docstring flagged this as a known gap since the PR that fixed
`_apply_cake` shaping the wrong direction outright (it had been programming
`down_bps` where `up_bps` belonged): "True ingress (download) shaping isn't
implemented anywhere in this codebase... CAKE can only shape the direction
traffic leaves an interface, so shaping download would need an IFB
(Intermediate Functional Block) device."

Leaving that gap open means only half of what CAKE exists to manage is
actually managed. On a real, asymmetric consumer connection, an unshaped
download direction lets its own queue build up — at the ISP's own buffer, or
on this box's own ingress before the kernel gets a chance to schedule it —
and that queueing delay (bufferbloat) is not confined to downloads: it also
degrades upload's throughput, because upload's own TCP streams depend on
ACKs arriving promptly, and those ACKs are competing with — and queued
behind — a saturated, unmanaged download queue on the same physical link.
Shaping only egress protects upload from causing this to download; it does
nothing to protect download from causing it to upload, or to itself.
`_usable_capacity`'s 5% margin already applied to both directions
(`render()`'s `Shaping(down_bps=..., up_bps=...)` both multiply the measured
figure by `0.95`) — the margin was always there for both; only the
*enforcement* of the download half was missing.

## Decision

**Download is shaped via an IFB device**, the standard Linux mechanism for
this: ingress traffic on a real interface is redirected (`tc filter ...
action mirred egress redirect dev <ifb>`) onto a virtual IFB device's own
egress, which CAKE can then shape exactly like any other egress.

- `_ifb_for_ifname(ifname)` derives a deterministic device name
  (`ifb-<hash prefix>`) from the real interface's name. Deliberately keyed
  by `ifname`, not atomic id, unlike `_table_for_atomic` — an IFB device is
  a purely runtime construct recreated fresh every time this module runs
  against a real ifname within one boot, never persisted or compared across
  a restart, so ADR-002's "never key identity by ifname" does not apply to
  a value with no persistent meaning to begin with.
- `_apply_ingress_shaping()` creates the IFB device, brings it up, attaches
  an ingress qdisc to the real interface, redirects onto the IFB, and
  shapes the IFB's own egress from `shaping.down_bps` — all idempotent
  (`tolerate_exists` on device/qdisc creation, `replace` for the filter and
  the CAKE qdisc itself), matching every other `enforce/` method's
  reconciliation contract (ADR-007) and never-tear-down contract (ADR-008).
- `_read_shaping()`/`_converged()` now read and compare `down_bps` for
  real, pairing each real interface's CAKE entry with its IFB's via the
  same deterministic naming — previously `down_bps` was read back as a
  hard-coded `0` and never compared at all.
- The image bake pipeline's capability gate now also checks the `ifb`
  kernel module is present, alongside the existing `sch_cake`/`wireguard`
  checks — the same "fail at build time, not silently in the field"
  philosophy already applied to everything else that gate covers.

## Consequences

**Easier:**

- Both directions of an asymmetric link are now actually managed by CAKE,
  not just the one that happened to get fixed first. The upload-starves-
  download (and vice versa) bufferbloat interaction CAKE exists to solve is
  now addressed on both sides of the pipe, not half of it.
- `_read_shaping()` and `_converged()` no longer have a permanently-`0`,
  never-comparable field — `Shaping.down_bps` means what it says everywhere
  it's read, not just where it's written.

**Harder:**

- Twice the kernel objects to reconcile per shaped atomic (an IFB device,
  an ingress qdisc, a redirect filter, and a second CAKE qdisc, versus one
  CAKE qdisc before) — more surface for `raw_dump()`/support diagnostics to
  show, more to reason about when debugging a shaping issue in the field.
- The `ifb` kernel module must be present and loadable on real hardware;
  unconfirmed until it has actually been watched working on a Pi (this
  entry belongs in `docs/active-tests.md` alongside every other
  never-yet-hardware-confirmed real-kernel behavior in this codebase, e.g.
  ADR-019's own tunnel-egress entry).
- The `u32 match u32 0 0` "match everything" idiom on the ingress filter is
  the classic, broadly-compatible way to redirect all traffic — chosen over
  the newer `matchall` action specifically for broader `tc` version
  compatibility, but it is one more thing a future reader has to already
  know the meaning of rather than something self-documenting in the argv
  itself.

**Must stay true:** an IFB device's name must never be persisted, compared
across a restart, or treated as identity for anything beyond this one
reconcile pass — the moment it needs to survive that, `_ifb_for_ifname`'s
whole justification for being keyed by `ifname` instead of atomic id stops
holding, and it would need to move to atomic-id keying like `_table_for_atomic`.

## Alternatives considered

**Leave it as a documented gap** — rejected. It had already been flagged
once and left open; the asymmetry it leaves in place (upload actively
protected from causing bufferbloat, download not) is exactly the kind of
half-finished implementation this project's own standards call out as worse
than not shipping the feature's name (`Shaping.down_bps` existing, and
being silently unenforced, is more misleading than the field not existing
at all).

**A separate `IngressShaping` dataclass / desired-state field instead of
reusing `Shaping.down_bps`** — rejected: `Shaping` already carries
`down_bps` for exactly this purpose (`render()` already computes it from
measured capacity), and it was already threaded through `DesiredState`,
`_converged()`, and every call site — the only thing missing was an
enforcer method that actually did something with it. Adding a parallel
data shape would duplicate structure that already existed and meant this.
