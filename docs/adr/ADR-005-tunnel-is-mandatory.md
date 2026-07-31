# ADR-005 — The stable tunnel is mandatory, and the fabric is MVP scope

**Status:** Accepted
**Date:** 2026-07-30

## Context

The core product promise is that when connectivity fails, the user experiences
increased latency or reduced performance — not a broken network. Existing sessions,
TCP connections, and interactive traffic survive.

Without a tunnel, they cannot. When the appliance switches from hotel Wi-Fi to
phone tethering, the public IP changes. Every established TCP connection is now
addressed to an IP the device no longer holds; every NAT mapping is gone. The SSH
session dies, the video call drops, the download restarts. The user experiences
exactly the broken network the product exists to prevent — and worse, they
experience it *because* of the failover.

Fast, clean failover without a tunnel is still a visible outage. The tunnel is not
an enhancement to failover; it is the mechanism that makes failover invisible.

## Decision

**Client traffic is tunnelled to a remote fabric server, and the fabric is MVP
scope — not a later addition.**

WireGuard carries the tunnel. Sessions terminate at the fabric, so the
client-visible IP is the fabric's and never changes when a WAN does. The tunnel is
re-established over whichever WAN is active; the appliance-to-fabric path changes
while the client-to-Internet path does not.

The tunnel is also the security boundary. WANs — public Wi-Fi especially — are
treated as hostile, and LAN services are never exposed through an arbitrary WAN.

## Consequences

**Easier:**

- WAN changes become invisible to clients. This is the product.
- One security boundary rather than a trust decision per WAN.
- Fabric-side reordering becomes possible later, which is the precondition for
  lifting [ADR-004](ADR-004-failover-not-aggregation.md).

**Harder:**

- **The product requires server infrastructure.** This is no longer a device you
  buy once — it has a hosting cost, an operational burden, and a business-model
  implication. That is a real cost, accepted deliberately.
- **The fabric is a dependency and a potential single point of failure.** Multi-server
  with migration is Phase 2 precisely because one server is a liability.
- All traffic takes a detour, adding latency and consuming appliance CPU for
  encryption. WireGuard on the A53 caps around 30–50 Mbps — above the radio's
  ceiling, so not currently binding, but it is a real ceiling.
- Version skew between appliance and fabric can break connectivity entirely, which
  is why `WIFUCKED_FABRIC_MIN` exists ([ADR-016](ADR-016-versioning.md)).

**Must stay true:** the fabric stays reachable and adequately provisioned. A
saturated fabric degrades every user simultaneously — a failure mode with no
equivalent in a tunnel-less design.

## Alternatives considered

**No tunnel, fast failover only** — rejected. Sessions die on every switch, which
defeats the core promise. It would be a cheaper product that does not do the one
thing that matters.

**Tunnel optional, off by default** — rejected. The product's behaviour would
differ fundamentally between configurations, doubling the test matrix and making
"does it work?" unanswerable without asking which mode.

**Existing commercial relay services** — plausible for a prototype, but hands the
core differentiator, the cost structure, and the ability to add reordering to a
third party.
