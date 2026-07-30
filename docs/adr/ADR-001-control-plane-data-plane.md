# ADR-001 — Control plane in Python, data plane in the kernel

**Status:** Accepted
**Date:** 2026-07-30

## Context

The appliance runs on a Raspberry Pi Zero 2W: quad Cortex-A53 at 1 GHz, 512 MB
RAM. It must sustain 10–15 Mbps of shaped, classified, tunnelled traffic while
simultaneously measuring capacity, estimating demand, and deciding allocation.

A userspace forwarder in Python could not do the first part at all, let alone
while doing the second. Even a C forwarder would be a poor use of this SoC when
Linux already ships CAKE, nftables, policy routing, and WireGuard — all of which
solve exactly the problems this product needs solved, in kernel space, tested by
everyone.

The product is not a better packet forwarder. It is an intelligent control layer
around proven networking primitives.

## Decision

**The control plane is Python. The data plane is the kernel. No packet is ever
touched by Python.**

The `dirty` daemon observes the network, decides what should happen, and programs
`tc`/CAKE, `nftables`, and policy routing to make it happen. Forwarding, shaping,
queueing, and encryption occur entirely in kernel space.

`enforce/` is the only module permitted to invoke `tc`, `nft`, or `ip`.

## Consequences

**Easier:**

- Throughput is bounded by the radio, not by the control language. Python's
  performance is irrelevant to user traffic.
- The daemon can crash, be restarted, or be updated without dropping a packet —
  which is what makes [ADR-008](ADR-008-fail-to-last-known-good.md) and
  [ADR-011](ADR-011-ap-is-the-anchor.md) possible at all.
- We inherit years of hardening in CAKE and nftables rather than reimplementing
  queue management badly.

**Harder:**

- Everything the system wants to do must be expressible as kernel state. Where a
  policy cannot be expressed in `tc` + `nftables` + routing, it cannot be
  implemented — that constraint shapes what the allocator is allowed to decide.
- Debugging spans two worlds. The daemon's model and the kernel's reality can
  diverge, which is why enforcement is reconciliation
  ([ADR-007](ADR-007-reconciliation.md)) rather than fire-and-forget.
- Per-packet decisions are impossible. This forecloses per-packet load balancing
  and is one of the reasons for [ADR-004](ADR-004-failover-not-aggregation.md).

**Must stay true:** the kernel primitives keep doing what we need. If a future
requirement genuinely cannot be expressed in kernel state, that needs a new ADR,
not a quiet userspace relay.

## Alternatives considered

**Userspace forwarding in Python** — dismissed on arithmetic. The Pi Zero 2W could
not forward the target throughput from Python while also running the control loop.

**Userspace forwarding in C or Rust** — feasible, but it would reimplement CAKE
and nftables worse, add a large attack surface on a device that terminates hostile
public Wi-Fi, and make the daemon a single point of failure for the user's
network.

**eBPF/XDP for the data plane** — genuinely attractive for classification, and not
foreclosed by this decision: eBPF is kernel-side, so adding it later is consistent
with this ADR. Rejected for now as unnecessary complexity when `nftables` marking
already does the job at these rates.
