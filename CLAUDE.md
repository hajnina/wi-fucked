# CLAUDE.md

Guidance for Claude Code (claude.ai/code) and for humans working in this repo.
These rules override defaults. If a rule here conflicts with what you were about
to do, the rule wins.

## Git

**Once work is committed and pushed to a branch, open a pull request for it
automatically** — do not wait to be asked. Its author is responsible for it:
make one CI-and-review check five minutes after opening it or pushing an update,
then make one check every 30 minutes until it is merged or closed. Address
actionable feedback and failures. **Merge automatically once CI is green** and
there is no unresolved reviewer feedback blocking it; do not leave a passing PR
idle waiting for a human to click merge. Everything else about git remains
off-limits unless the user explicitly asks.

## Standard Operating Procedures — read these

The SOPs in [`docs/sop/`](docs/sop/) are **binding**. They are not style
suggestions. This file states the rules; the SOPs tell you how to work.

| SOP | Read it |
|---|---|
| [SOP-001 Taking on work](docs/sop/SOP-001-taking-on-work.md) | Before writing any code |
| [SOP-002 Writing code](docs/sop/SOP-002-writing-code.md) | Every time |
| [SOP-003 Testing](docs/sop/SOP-003-testing.md) | Before claiming something works |
| [SOP-004 Logging and observability](docs/sop/SOP-004-logging-and-observability.md) | Every time you add a log line |
| [SOP-005 Commits and pull requests](docs/sop/SOP-005-commits-and-pull-requests.md) | Before you commit |
| [SOP-006 Code review](docs/sop/SOP-006-code-review.md) | Reviewing, or requesting review |
| [SOP-007 Architectural decisions](docs/sop/SOP-007-architectural-decisions.md) | Changing a boundary |
| [SOP-008 Release and OTA](docs/sop/SOP-008-release-and-ota.md) | Touching the pipeline or shipping |
| [SOP-009 Hardware and field debugging](docs/sop/SOP-009-hardware-and-field-debugging.md) | Works locally, not on a Pi |
| [SOP-010 Keeping documentation current](docs/sop/SOP-010-keeping-documentation-current.md) | Continuously |

**Three obligations, not optional:**

1. **Follow them.** If an SOP is wrong for your situation, change it — do not
   silently ignore it. Invisible deviation is how documentation stops being
   trusted.
2. **Keep them current.** A change that makes an SOP wrong is not finished until
   that SOP is right, **in the same pull request**. Documentation upkeep is part of
   the work, never follow-up.
3. **Keep this file in sync.** `CLAUDE.md` states the rules and links to the SOPs
   for detail. A rule that changes must change in both
   ([SOP-010](docs/sop/SOP-010-keeping-documentation-current.md)).

An SOP describing a workflow nobody follows is worse than no SOP — it teaches new
people the wrong thing with the authority of a written procedure.

## The one rule everything else follows

> **The control plane is Python. The data plane is the kernel.
> No packet is ever touched by Python.**

The `wifucked` daemon observes, decides, and *programs* `tc`/CAKE, `nftables`, and
policy routing. Forwarding, shaping, and queueing happen entirely in kernel space.

If you find yourself writing a socket that relays user traffic, a `select()` loop
over client connections, or anything that reads and re-sends payload bytes — stop.
That belongs in the kernel, expressed as a rule the daemon installs. See
[ADR-001](docs/adr/ADR-001-control-plane-data-plane.md).

## Project overview

**WI-FUCKED → BALANCED** is an autonomous connectivity appliance on a Raspberry Pi
Zero 2W. It aggregates chaotic WAN connectivity (Wi-Fi, USB tethering, USB
Ethernet, cellular) and presents LAN clients two stable SSIDs — `Stable_critical`
and `Stable_besteffort` — that never go away.

- `appliance/` — runs on the Pi
- `fabric/` — the remote tunnel endpoint (container)
- `scripts/` — version, packaging, manifest
- `.github/` — image bake pipeline

## Development commands

```bash
# Run the daemon on a laptop, no hardware
MOCK_HW=1 PYTHONPATH=appliance/src python3 -m wifucked

# Run it with a scripted scenario instead of static mocks
MOCK_HW=1 WIFUCKED_SCENARIO=moving_van PYTHONPATH=appliance/src python3 -m wifucked

# Everything
./run_all_tests.sh

# Just the unit tests
MOCK_HW=1 PYTHONPATH=appliance/src python3 -m pytest appliance/tests/ -v
```

`MOCK_HW=1` is not optional tooling — it is the primary development path. Every
module must be exercisable without a Pi, a radio, or root. If you write something
that can only be tested on hardware, you have written it wrong.

## Architecture rules

These are enforced in review. Each links to the ADR that explains why.

1. **No userspace forwarding.** ([ADR-001](docs/adr/ADR-001-control-plane-data-plane.md))
2. **Atomics are identified by stable properties, never by interface name.**
   `wlan1` becomes `wlan2` across reboots and USB re-enumeration. Key off SSID +
   BSSID prefix, USB vendor/product/serial, MAC, or modem IMEI. Any code that
   stores or compares `ifname` as identity is a bug.
   ([ADR-002](docs/adr/ADR-002-atomic-identity.md))
3. **Enforcement is reconciliation, not command.** Declare the desired kernel
   state; the fast loop diffs actual against desired and repairs. Never assume a
   rule you installed is still there. ([ADR-007](docs/adr/ADR-007-reconciliation.md))
4. **Fail to last-known-good.** If the daemon dies, kernel rules stay in place and
   the user's network keeps working. Nothing may tear down forwarding on the way
   out — no cleanup handlers that flush tables, no `atexit` that removes qdiscs.
   ([ADR-008](docs/adr/ADR-008-fail-to-last-known-good.md))
5. **The AP is the anchor.** `hostapd` and `dnsmasq` are independent systemd units
   with no dependency on `wifucked.service`. The daemon may reconfigure them; it must
   never own their lifecycle. ([ADR-011](docs/adr/ADR-011-ap-is-the-anchor.md))
6. **SSID and BSSID are immutable** after first boot. Only the channel may move,
   and only via CSA. ([ADR-012](docs/adr/ADR-012-immutable-ssid.md))
7. **Every allocation change writes a decision record.** Explainability is an
   architectural constraint, not a dashboard feature.
   ([ADR-009](docs/adr/ADR-009-decision-records.md))

## Logging standards

Rigorous logging is how this system stays debuggable in the field, where you have
no console and no reproduction. All new code MUST comply.

1. **Auditability.** Every meaningful action is logged with complete contextual
   metadata — every state transition, every decision, every external command.
2. **Context-rich records.** A reader must get the *complete* picture from the log
   alone: what happened, what went wrong, what the intent was, what the context
   was. Include variable states, timings (`duration_ms`), explicit workflow states,
   and explicit reasons for failures and fallbacks.
3. **Proper logger hierarchy.** Use sub-loggers rooted in `wifucked` —
   `get_logger("allocator")` yields `wifucked.allocator`. Never a bare string
   disconnected from the application root.
4. **Resilience.** Never crash on a malformed log message. `ResilientLogger`
   catches `KeyError` conflicts from overlapping `extra` keys (`name`, `msg`,
   `args`) and degrades to a stringified payload. Use `get_logger`, never
   `logging.getLogger` directly.

### Required `extra` fields

Every log call that describes work carries:

| Field | Meaning |
|---|---|
| `workflow` | The operation, e.g. `wan_discovery`, `backup_activation`, `ota_apply` |
| `state` | `started` / `processing` / `completed` / `failed` / `skipped` |
| `intent` | Why this is happening, in plain words |
| `duration_ms` | For anything that takes measurable time |
| `reason` / `error` | On any failure or fallback. Plus `exc_info=True`. |

```python
log.info(
    "Activated BACKUP atomic",
    extra={
        "workflow": "backup_activation",
        "state": "completed",
        "intent": "critical demand cannot be met by NORMAL pool",
        "atomic_id": atomic.id,
        "normal_capacity_bps": 1_400_000,
        "critical_demand_bps": 3_100_000,
        "duration_ms": 42,
    },
)
```

### Domain-specific requirements

- **Radio and profile changes.** Log the full picture around any AP channel move,
  profile switch, or CSA: old channel, new channel, why, how many clients were
  associated, and whether they survived. These are the events that break the
  "always available" promise, so they get the most scrutiny.
- **Allocation decisions.** Beyond logging, they write a structured decision record
  via `telemetry.decisions` — inputs, thresholds, chosen action, reason. The
  dashboard renders these directly. ([ADR-009](docs/adr/ADR-009-decision-records.md))
- **Cost.** Any byte sent over a `BACKUP` atomic is accounted and logged with the
  traffic class responsible and the degradation that justified it.
- **OTA.** Explicit state for every step: version from, version to, slot, health
  check outcome, rollback decision and cause.

## Commit convention

**Conventional commits are mandatory and CI-enforced.** The release version is
derived from commit subjects, so a sloppy subject line silently mis-versions a
release. ([ADR-017](docs/adr/ADR-017-conventional-commits.md))

```
<type>(<optional scope>): <subject>
```

Types: `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `build`, `ci`, `chore`.

| Commit | Version bump |
|---|---|
| `feat!:` or `BREAKING CHANGE:` in the body | **major** — `1.4.2` → `2.0.0` |
| `feat:` | **minor** — `1.4.2` → `1.5.0` |
| anything else | **patch** — `1.4.2` → `1.4.3` |

A breaking change means: the OTA package cannot be applied to the previous
version, the fabric protocol changed, or a user's configuration needs migration.
It does *not* mean "a big change."

## Branch rules

**One branch: `main`. All pull requests target `main`.** There is no `alpha`,
no `beta`, no `develop`. One channel, one release stream.

Every push to `main` that touches buildable paths produces one immutable release.
Releases are never deleted or overwritten. ([`docs/versioning.md`](docs/versioning.md))

## Testing rules

- Everything runs under `MOCK_HW=1`. No test may require a Pi.
- **Any change to `policy/`, `allocator/`, `enforce/`, or `radio/` requires a
  scenario test.** These are the modules where a plausible-looking change produces
  a system that misbehaves only in the field. `appliance/tests/scenarios/` drives
  the control loop through a scripted timeline and asserts on outcomes.
- Two invariants every scenario test must uphold, whatever else it checks:
  - **The AP never drops.** Not across WAN churn, profile switches, or daemon
    restarts.
  - **`BACKUP` carries zero bytes** until critical demand genuinely cannot be met.
- The dashboard is airgapped. `appliance/tests/verify_no_external_assets.py` fails
  the build if any template references an external host. Do not add a CDN link.

## ADR process

Architectural decisions live in `docs/adr/`. They are numbered, immutable records.

- Changing a decision means **writing a new ADR that supersedes the old one**, not
  editing the old one. The old one gets a `Superseded by ADR-0NN` header and stays.
- Anything that changes a module boundary, a persistence format, the enforcement
  strategy, the radio model, or the release contract needs an ADR before the code.
- If you are about to write a comment explaining why the architecture is the way it
  is, that comment is an ADR. Write it there and link to it.

## Things that will get a PR sent back

- Storing `ifname` as an atomic's identity.
- A cleanup path that removes qdiscs, flushes nftables, or drops routes.
- `logging.getLogger` instead of `get_logger`.
- A log line describing work without `workflow` / `state` / `intent`.
- A change to the allocator without a scenario test.
- An external asset reference in the dashboard.
- A commit subject that isn't a conventional commit.
- Anything that makes the Stable SSIDs depend on the daemon being alive.
- A credential or payload byte in a log line, a release asset, or a diagnostics
  bundle.
- A change that makes a document wrong, without fixing the document in the same PR.

The full review checklist is [SOP-006](docs/sop/SOP-006-code-review.md).
