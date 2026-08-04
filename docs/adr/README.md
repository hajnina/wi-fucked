# Architecture Decision Records

Decisions that shape this system, with the context that made them reasonable and
the consequences accepted along with them.

**ADRs are immutable.** To change a decision, write a new ADR that supersedes the
old one — never edit the original. The old context is the valuable part: it lets a
future reader tell whether the decision still applies or whether the world moved.
See [SOP-007](../sop/SOP-007-architectural-decisions.md).

## Index

| # | Decision | Status |
|---|---|---|
| [001](ADR-001-control-plane-data-plane.md) | Control plane in Python, data plane in the kernel | Accepted |
| [002](ADR-002-atomic-identity.md) | Atomic identity from stable properties, never interface names | Accepted |
| [003](ADR-003-passive-capacity-estimation.md) | Capacity estimated passively by default | Accepted |
| [004](ADR-004-failover-not-aggregation.md) | Failover, not aggregation, on the base hardware | Accepted |
| [005](ADR-005-tunnel-is-mandatory.md) | The stable tunnel is mandatory; fabric is MVP scope | Accepted |
| [006](ADR-006-backup-liveness-budget.md) | BACKUP gets a small, accounted liveness budget | Accepted |
| [007](ADR-007-reconciliation.md) | Enforcement is reconciliation, not command | Accepted |
| [008](ADR-008-fail-to-last-known-good.md) | Fail to last-known-good; never tear down on the way out | Accepted |
| [009](ADR-009-decision-records.md) | Every allocation change writes a decision record | Accepted |
| [010](ADR-010-state-storage.md) | SQLite for telemetry, tmpfs for hot writes, bounded by construction | Accepted |
| [011](ADR-011-ap-is-the-anchor.md) | The AP is the anchor; hostapd does not depend on the daemon | Accepted |
| [012](ADR-012-immutable-ssid.md) | SSID and BSSID are immutable after first boot | Accepted |
| [013](ADR-013-radio-profiles.md) | Two radio profiles: ANCHOR and SHARED | Accepted ⚠ |
| [014](ADR-014-two-ssid-fallback.md) | Two SSIDs preferred; one SSID with two PSKs as fallback | Accepted ⚠ |
| [015](ADR-015-boot-count-factory-reset.md) | Factory reset by boot count, WAN config only | Accepted |
| [016](ADR-016-versioning.md) | Tag-derived SemVer, one channel, immutable releases | Accepted |
| [017](ADR-017-conventional-commits.md) | Conventional commits mandatory and CI-enforced | Accepted |
| [018](ADR-018-main-release-channel.md) | Main is the sole release channel | Accepted |
| [019](ADR-019-lan-egress-through-the-tunnel.md) | LAN client egress routes through the tunnel, not the WAN directly | Accepted |

⚠ — rests on driver behaviour that is expected but **not yet verified on
hardware**. Expect superseding ADRs once [`../radio-spike.md`](../radio-spike.md)
reports. Do not treat these as settled facts.

## Reading order

If you are new, read them in this order rather than by number:

1. **[001](ADR-001-control-plane-data-plane.md)** — the split everything else
   follows from.
2. **[011](ADR-011-ap-is-the-anchor.md)**, **[008](ADR-008-fail-to-last-known-good.md)**
   — how "always available" is actually achieved.
3. **[002](ADR-002-atomic-identity.md)** — the abstraction most easily broken by
   well-meaning code.
4. **[004](ADR-004-failover-not-aggregation.md)**, **[013](ADR-013-radio-profiles.md)**
   — what one radio costs, and how it is worked around.
5. The rest as they become relevant to what you are building.

## Writing one

Copy [`TEMPLATE.md`](TEMPLATE.md), take the next free number, keep it to one page,
and be honest in the Consequences section — an ADR listing only benefits is
marketing, and the next person pays for it.

Add your entry to the index above in the same pull request.
