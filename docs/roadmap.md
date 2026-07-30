# Roadmap

Four phases: Hello World → MVP → Beta → Production.

Each has a **hard exit criterion** — a demonstrable behaviour, not a list of merged
pull requests. A phase is done when the criterion is met on real hardware, and not
before.

---

## Phase 0 — Hello World

**Goal: the pipeline is real before any product logic exists.**

The most common way a project like this fails is building six months of clever
control logic on top of a release process nobody has tested, then discovering that
OTA bricks devices. So the first thing that works end to end is the boring part.

### Task zero — the radio capability spike

**Before anything downstream is built.** See
[`radio-spike.md`](radio-spike.md) for the brief.

The answers decide between [ADR-013](adr/ADR-013-radio-profiles.md)'s profiles and
[ADR-014](adr/ADR-014-two-ssid-fallback.md)'s fallback. Nothing that depends on
radio behaviour should be built on a guess — those two ADRs are currently marked
unverified for exactly this reason.

Timeboxed to one week. Deliverable is a written findings document that amends the
ADRs.

### In parallel

- Repo skeleton, `CLAUDE.md`, SOPs, ADRs, docs
- `dirty` daemon starts, logs, serves a dashboard that honestly says "no atomics"
- `MOCK_HW=1` runs the full control loop on a laptop
- The scenario harness exists — so Phase 1 has a target to build against, not an
  afterthought
- CI green; first release `v0.1.0`

### Exit criterion

A push to `main` produces an image that, on a real Pi Zero 2W:

1. boots and brings up the Stable SSIDs within ~40 s
2. serves the dashboard
3. **survives `kill -9` on the daemon without dropping the AP**
4. takes an OTA update from the previous release
5. rolls back automatically when the health check is deliberately broken

It does nothing useful. The entire pipeline — bake, version, release, install,
validate, roll back — is proven before a single product decision depends on it.

---

## Phase 1 — MVP

**Goal: the appliance actually does its job on one radio.**

| WS | Scope |
|---|---|
| **WS-A** Connectivity | Discovery of Wi-Fi / USB-tether / USB-Ethernet atomics with stable identity; `NORMAL`/`BACKUP`/`UNUSED` persisted; recognition across replug and reboot |
| **WS-B** Measurement | RTT, jitter, loss; passive capacity estimation both directions with explicit confidence; bufferbloat detection; per-class demand |
| **WS-C** Control | Priority ladder; hysteresis state machine; `BACKUP` activation with liveness budget; cost accounting |
| **WS-D** Enforcement | CAKE per WAN; nftables classification; policy routing; **failover** (not aggregation); reconciliation loop |
| **WS-E** Fabric | WireGuard tunnel to a single fabric server; session survival across WAN change |
| **WS-F** Surface | Dashboard: current state, decision journal, last hour; ACT-LED beacon; captive portal |
| **WS-G** Platform | Radio profile switching with CSA; OTA proven on hardware; boot-count reset |

### Exit criterion

The moving-van scenario, on real hardware:

> Wi-Fi as `NORMAL`, USB phone tether as `BACKUP`. Withdraw the Wi-Fi. Restore it
> congested — 800 ms RTT, heavy loss, saturated upload. Then withdraw it again.

Assert:

- Critical traffic stays usable throughout
- Best-effort absorbs the degradation
- `BACKUP` carries **zero bytes** until critical demand genuinely cannot be met
- **No TCP session dies**
- **The Stable SSIDs never drop** — not once, through every transition

---

## Phase 2 — Beta

**Goal: survive contact with the real world.**

- Multi-server fabric with migration; server failure handled like WAN failure
- Per-network historical learning — evidence, never truth; **current state always
  wins**
- Telemetry retention and rollups; SD-wear telemetry
- Cost accounting with budgets and alerts
- Cellular via ModemManager
- Staged OTA rollout with proven rollback at scale
- **Field trial: ≥5 devices, ≥30 days, in genuinely bad networks**

### Exit criterion

30 days of continuous field operation with **no manual intervention**, and
telemetry that answers the question that justifies the product — *did this actually
make the Internet better?* — with data rather than opinion.

If the telemetry cannot answer that, the observability work is not finished,
regardless of how well the appliance behaved.

---

## Phase 3 — Production

**Goal: a product, not a project.**

- Threat model executed. WANs treated as hostile; LAN services never exposed
  through an arbitrary WAN; key handling reviewed
- Advanced configuration surface — cost policies, custom service profiles,
  time-based rules — **without contaminating the default experience**
- Remote diagnostics and support tooling
- Manufacturing and provisioning flow
- **Optional aggregation** via USB radios, superseding
  [ADR-004](adr/ADR-004-failover-not-aggregation.md) for users who add hardware
- Licence chosen

### Exit criterion

A non-technical user unboxes it, plugs in a phone, and never opens the dashboard
again.

---

## Workstream dependencies

```
WS-G platform ──┐
WS-A connectivity ──┴──> WS-B measurement ──> WS-C control ──> WS-D enforcement
                                      └──> WS-F surface
WS-E fabric ─────────────────────────────────> WS-D
```

**WS-A and WS-G unblock everything** and start first.

After that, three tracks run in parallel from roughly week two:

- **WS-D** builds against hand-written fake allocations before WS-C is real.
- **WS-F** builds against recorded telemetry fixtures before WS-B produces any.
- **WS-E** is almost independent — the tunnel does not care how allocation is
  decided.

This is deliberate. The interfaces exist from Phase 0 with mock implementations
behind them ([SOP-001](sop/SOP-001-taking-on-work.md)), so juniors replace one fake
at a time and **always have a working system to test against**. Nobody is blocked
waiting for someone else's module to become real.

## Ownership boundaries

Each workstream owns its modules and may not change another's public interface
without an ADR. Touching another workstream's *implementation* is normal; changing
its *contract* is a conversation. See [SOP-001](sop/SOP-001-taking-on-work.md) for
the table.

## What is deliberately not planned yet

Phase 3's aggregation work and the advanced configuration surface are sketched, not
specified. Specifying them now would mean guessing at what the Phase 2 field trial
teaches — and the field trial exists precisely because those guesses would be
wrong.
