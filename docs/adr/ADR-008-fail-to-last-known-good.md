# ADR-008 — Fail to last-known-good; never tear down on the way out

**Status:** Accepted
**Date:** 2026-07-30

## Context

The appliance is the user's only route to the Internet. It runs unattended, in
places where nobody can service it, for users who cannot debug it.

The `dirty` daemon is a Python program under active development by people learning
the system. It will crash. It will be OOM-killed on a 512 MB device. It will be
restarted by OTA updates. That is expected and acceptable.

What is not acceptable is a control-plane bug becoming a user-facing outage.

Conventional cleanup discipline actively causes that. The tidy habit — remove your
qdiscs, flush your rules, drop your routes on shutdown — means that every crash
takes the user's Internet with it. The tidier the code, the worse the failure.

## Decision

**Kernel state outlives the process that installed it, deliberately.**

- **There is no cleanup path** that removes qdiscs, flushes nftables, or drops
  routes. No `atexit` handler, no `finally` teardown, no signal handler that
  unwinds.
- When the daemon dies, traffic keeps flowing under the last policy it installed.
- On restart, the daemon reconciles ([ADR-007](ADR-007-reconciliation.md)) rather
  than initialising from empty.
- Component failures fall back to last-known-good behaviour, not to a safe-looking
  blank slate. A capacity estimator that fails keeps the previous estimate; a
  shaping call that fails keeps the previous shaping.

Combined with [ADR-011](ADR-011-ap-is-the-anchor.md), a dead daemon means the user
keeps both their LAN *and* their Internet — they simply stop getting adaptation.

## Consequences

**Easier:**

- Control-plane bugs degrade adaptivity instead of causing outages. The failure
  mode is "the network stopped getting smarter", not "the network stopped".
- Updates and restarts are cheap, which makes OTA far less frightening.
- Crash recovery and normal operation share one code path.

**Harder:**

- **Stale policy can persist.** A daemon that dies right after shaping to 1 Mbps
  leaves the link at 1 Mbps even after conditions improve. Mitigated by the
  supervisor restarting quickly — but during the gap the user has a working, badly
  shaped network. That is the accepted trade.
- Developers must resist a strong and normally correct instinct. This is the rule
  most likely to be violated by someone doing a genuine favour, which is why it is
  called out in `CLAUDE.md`, [SOP-002](../sop/SOP-002-writing-code.md), and the
  review checklist.
- Debugging is harder: state on the device may have been installed by a process
  that no longer exists. `tc qdisc show` is the truth, not the daemon's model.

**Must stay true:** the supervisor restarts the daemon promptly, and reconciliation
converges. If reconciliation could not repair stale state, this decision would be
unsafe.

## Alternatives considered

**Clean up on exit** — rejected: it converts every crash into an outage.

**Clean up only on graceful shutdown** — sounds like a reasonable compromise, but
graceful shutdown is exactly when an OTA restart happens, so the AP and routing
would drop during every update. It also means the rare path differs from the common
one, guaranteeing it is the less tested.

**A separate watchdog process that restores state** — an extra moving part that can
itself fail, to solve a problem better solved by not tearing down in the first
place.
