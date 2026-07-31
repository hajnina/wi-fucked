# ADR-011 — The AP is the anchor; hostapd and dnsmasq do not depend on the daemon

**Status:** Accepted
**Date:** 2026-07-30

## Context

The Stable WiFi must **always** be available. Not "highly available", not
"available except during updates" — always. It is the one thing the user
interacts with, and the one thing that must never make them think about the
appliance.

The obvious design has the daemon own the AP: it decides the channel, writes the
configuration, starts `hostapd`, manages the lifecycle. That is tidy, and it makes
the Stable WiFi exactly as reliable as the least reliable Python code in the
project. Every crash, every OOM kill on a 512 MB device, every OTA restart, every
unhandled exception in an unrelated module becomes a LAN outage — and the user
loses not just Internet but the dashboard that would explain why.

## Decision

**The AP is the anchor. `hostapd` and `dnsmasq` are independent systemd units with
no dependency on `wifucked.service`.**

- They start at boot, before and independently of the daemon.
- They do not have `Requires=`, `BindsTo=`, or `PartOf=` pointing at the daemon.
- The daemon may *reconfigure* them — rewrite config, request a channel change via
  `hostapd_cli` — but never owns their lifecycle.
- If the daemon is absent, crashed, or being updated, the AP keeps serving and
  clients keep their DHCP leases.

Combined with [ADR-008](ADR-008-fail-to-last-known-good.md), a dead daemon leaves
the user with a working LAN *and* working Internet under the last installed policy.
They lose adaptation, not connectivity.

## Consequences

**Easier:**

- The Stable WiFi's reliability is that of `hostapd` — mature, battle-tested C —
  rather than that of a Python daemon under active development.
- Control-plane-only OTA updates restart the daemon without dropping the AP, so the
  common update path is invisible to users.
- Debugging is possible when things break: the dashboard is reachable even when the
  daemon is not.

**Harder:**

- The daemon cannot assume it knows the AP's state; it must query
  (`hostapd_cli status`) rather than remember. Consistent with
  [ADR-007](ADR-007-reconciliation.md).
- Channel changes are a request, not a command — the daemon asks `hostapd` to CSA
  and observes the result. Failure handling belongs at the call site.
- Configuration is split: `hostapd.conf` on disk plus runtime `hostapd_cli` calls.
  The first-boot generator and the daemon must not fight over the file.
- A misconfiguration that stops `hostapd` starting is now outside the daemon's
  ability to repair. First-boot generation must be conservative and validated.

**Must stay true:** `hostapd` remains able to run without the daemon. Any feature
requiring the daemon in the AP path — dynamic per-client policy, for instance —
needs a superseding ADR and a very good reason.

## Alternatives considered

**Daemon owns the AP lifecycle** — rejected: it makes the product's most important
promise depend on its least mature component.

**Daemon owns it, with a supervisor that restarts on failure** — reduces outage
duration but does not eliminate it, and adds a moving part. A five-second AP outage
per crash still drops sessions and still teaches users that the appliance is
unreliable.

**Run `hostapd` in a container for isolation** — no meaningful benefit on a
single-purpose 512 MB device, and adds startup latency to the one thing that must
be up fastest at boot.
