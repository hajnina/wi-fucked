# ADR-010 — SQLite for telemetry, tmpfs for hot writes, bounded by construction

**Status:** Accepted
**Date:** 2026-07-30

## Context

The appliance must retain detailed history — per-atomic capacity, throughput, RTT,
jitter, loss, availability, demand by service class, allocation, failures,
recoveries, backup consumption, decision records — in order to answer the question
that justifies the product: *did this actually make the Internet better?*

That is a continuous time-series workload on a device with 512 MB of RAM and a
microSD card, where **write wear and power-loss corruption are the dominant field
failure modes**. An appliance that destroys its own storage in six months is not an
appliance.

Gutiva's pattern of JSON files rewritten on every change is the wrong shape twice
over: rewriting a whole file per sample is maximally destructive to flash, and JSON
has no way to answer a range query without loading everything.

## Decision

Three tiers, chosen by access pattern:

| Data | Store | Why |
|---|---|---|
| Current state | In memory | Rebuilt from discovery on start; never needs to survive |
| Configuration | JSON on disk | Small, rarely written, human-readable and hand-editable |
| Telemetry, history, decisions | **SQLite in WAL mode** | Real queries, atomic commits, survives power loss |

With two rules that make it survivable on SD:

- **Hot writes buffer in `tmpfs`** and flush to SQLite periodically (default 60 s).
  A power cut loses at most one flush interval of telemetry — an acceptable loss,
  unlike a corrupted database.
- **Retention is a ring buffer.** The database has a bounded maximum size *by
  construction*, not by a cleanup job that might not run. Old samples are rolled up
  into coarser buckets, then dropped.

## Consequences

**Easier:**

- The dashboard's history views are SQL queries rather than in-memory aggregation
  over parsed files.
- WAL mode gives atomic commits and crash safety — a power cut mid-write leaves a
  valid database.
- Disk usage cannot grow unboundedly, so the device cannot fill its own card.
- Flash wear is proportional to flush interval, which is one tunable number.

**Harder:**

- Two write paths (buffer and flush) rather than one, and a window in which
  telemetry exists only in RAM. The dashboard must read across both to show live
  data.
- Rollup logic is real work and must be correct — a bug that drops the wrong bucket
  silently loses history.
- SQLite on a 512 MB device needs deliberate configuration: bounded cache, no
  memory-mapped I/O of a large database.
- Retention limits are a genuine product constraint. "What happened last month?"
  may only be answerable at hourly resolution.

**Must stay true:** the flush interval stays long enough to protect the card and
short enough that data loss on power cut is tolerable. If sample rates grow, this
needs re-measuring rather than re-guessing.

## Alternatives considered

**JSON files** — the inherited pattern. Rejected: full-file rewrites destroy flash,
and there is no query capability. Retained for configuration, where it is a good
fit.

**A real time-series database** — Prometheus, InfluxDB and friends are the right
tool at the wrong scale. Memory footprints alone disqualify them on 512 MB.

**Write telemetry straight to SQLite with no buffer** — simpler, and a plausible
starting point. Rejected on wear: sub-minute samples across a dozen atomics is a
continuous write stream to a consumer SD card.

**Ship telemetry to the fabric and keep nothing locally** — attractive for
analysis, but the device must explain itself while offline, which is exactly when
the user most wants an explanation. Remote telemetry is a Phase 2 *addition*, not a
replacement.
