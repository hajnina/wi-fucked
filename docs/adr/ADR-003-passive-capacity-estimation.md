# ADR-003 — Capacity is estimated passively by default

**Status:** Accepted
**Date:** 2026-07-30

## Context

The system must know what each connection can actually carry, in both directions,
and must keep knowing as conditions change. Configured numbers are useless — a
"20 Mbps" hotel connection delivers 4 Mbps at 8pm.

The obvious approach is an active speedtest. It has three problems on this product:

1. **It burns the bandwidth it measures.** On a 4 Mbps link shared with a user in a
   meeting, a saturating probe *is* the outage.
2. **It costs money.** On a metered `BACKUP` connection, probing spends exactly the
   resource the system is supposed to be conserving.
3. **It corrupts the demand signal.** Probe traffic is indistinguishable from user
   traffic in the counters, so the demand estimator learns from its own noise.

Meanwhile, most of the information is already available for free. When users
naturally saturate a link, achieved throughput *is* the capacity. Latency rising
under load reveals the queue depth and the knee.

## Decision

**Capacity is estimated passively by default:** observed throughput during natural
saturation, combined with latency-under-load to locate the bufferbloat knee.

Active probing is available but:

- **opt-in**, never a default;
- **`NORMAL` atomics only** — never on `BACKUP`, at any time, for any reason;
- rate-limited and yielded to user traffic.

`BACKUP` links get a validation probe only at the moment of activation, plus the
liveness budget in [ADR-006](ADR-006-backup-liveness-budget.md).

## Consequences

**Easier:**

- The system costs the user nothing to run. No mystery data consumption, no
  contention with the traffic it exists to protect.
- Estimates reflect conditions under real workloads, including the interaction
  between upload saturation and download performance — which is the failure mode
  the product exists to manage.

**Harder:**

- **Capacity is unknown until the link is used.** A freshly connected idle WAN has
  no estimate. The model must carry an explicit *confidence* alongside its value,
  and the allocator must behave sensibly at low confidence rather than trusting a
  guess.
- Underestimation is possible on links that are never saturated, so a good
  connection can look mediocre. Historical learning
  ([`../roadmap.md`](../roadmap.md), Phase 2) partly compensates.
- Estimation logic is subtler than "run iperf and read the number", and it is
  where measurement bugs will hide.

**Must stay true:** users generate enough traffic to reveal capacity. On a device
that is idle for days, estimates go stale — the confidence value must decay to
reflect that rather than reporting old numbers as current.

## Alternatives considered

**Periodic active speedtests** — rejected for the three reasons above. The metered
case alone is disqualifying: a product whose core promise is "never spend your
backup data" cannot spend backup data measuring itself.

**Trust the user's configured numbers** — contradicts "discover, don't configure",
and the numbers are usually wrong. Real capacity is a property of the moment, not
of the contract.

**Packet-pair / packet-train probing** — cheap in bytes and genuinely clever, but
notoriously unreliable over Wi-Fi, where MAC-layer retries and aggregation destroy
the timing assumptions. Not foreclosed as a future confidence input; rejected as a
primary source.
