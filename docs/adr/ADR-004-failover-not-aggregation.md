# ADR-004 — Failover, not aggregation, on the base hardware

**Status:** Accepted
**Date:** 2026-07-30

## Context

The product vision shows several Wi-Fi networks simultaneously in `NORMAL` mode,
cooperating as one connectivity pool.

The Pi Zero 2W has **one** Wi-Fi radio — a 2.4 GHz single-stream CYW43438. It
cannot be a station on two Wi-Fi networks at once. With the base bill of materials,
simultaneous multi-Wi-Fi is not merely difficult; it is unavailable.

Separately, even where multiple paths do exist (Wi-Fi plus USB tethering), striping
a single flow across paths with different latency, loss, and capacity reorders
packets badly enough to collapse TCP throughput — often below that of the better
path alone. Doing it well requires a reordering endpoint and per-packet sequencing,
neither of which exists yet.

## Decision

**On the base hardware, DIRTY delivers seamless, session-preserving failover
between WANs — not bandwidth aggregation.**

Where genuine concurrency exists (a USB WAN alongside Wi-Fi), traffic is balanced
**per flow**, never per packet. Per-packet striping is out of scope until the
fabric can reorder, and lifting that requires a superseding ADR.

## Consequences

**Easier:**

- Matches the hardware honestly instead of promising something it cannot do.
- Per-flow balancing needs only `nftables` marks and policy routing — no sequencing,
  no reorder buffer, no new protocol.
- The thing users actually feel — *the network didn't break when the Wi-Fi died* —
  comes from the tunnel plus fast failover
  ([ADR-005](ADR-005-tunnel-is-mandatory.md)), not from aggregation.

**Harder:**

- **Total bandwidth is that of the best single path**, not the sum. A user with two
  8 Mbps links gets 8, not 16. This must be stated plainly in the dashboard rather
  than left for them to discover.
- A single large flow cannot exceed one path's capacity, even with idle capacity
  elsewhere.
- The vision's multi-`NORMAL` illustration is aspirational on this BOM. Product
  copy must not imply otherwise.

**Must stay true:** the target market's links are bad enough that stability matters
more than sum-of-bandwidth. This holds for the stated use cases — hotel, campsite,
café, tethering — and would stop holding if the product moved towards users with
several good connections.

## Alternatives considered

**Per-packet striping across paths** — rejected for now. Without fabric-side
reordering it destroys TCP, and it is the easiest way to build something that
benchmarks impressively and feels terrible. Genuinely revisitable once the fabric
can resequence; that is Phase 3 and needs a superseding ADR.

**Require a USB Wi-Fi dongle in the base BOM** — rejected: the BOM is fixed at one
Pi Zero 2W. Adding a dongle remains a supported *upgrade* that unlocks real
concurrency, and the architecture models atomics as N precisely so this needs no
redesign.

**MPTCP** — would give genuine aggregation with session survival, but requires
support at both ends for every flow. Feasible only for traffic terminating at our
own fabric, which is a subset. Worth revisiting alongside fabric-side reordering.
