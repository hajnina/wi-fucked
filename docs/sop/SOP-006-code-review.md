# SOP-006 — Code review

Review is where architecture survives contact with a growing team. Most of what
follows is about catching the specific mistakes this system makes expensive.

## What to check, in order

Work down. Stop at the first level that fails — there is no point discussing
naming in a change that violates an ADR.

### 1. Does it contradict an ADR?

The most expensive class of mistake available here, because it usually works
locally and fails in the field. Check against [`../adr/`](../adr/):

- Packet handling in Python? → [ADR-001](../adr/ADR-001-control-plane-data-plane.md)
- `ifname` stored or compared as identity? → [ADR-002](../adr/ADR-002-atomic-identity.md)
- Active probing on a `BACKUP` atomic? → [ADR-003](../adr/ADR-003-passive-capacity-estimation.md)
- Cleanup path that flushes kernel state? → [ADR-008](../adr/ADR-008-fail-to-last-known-good.md)
- Anything making the SSIDs depend on `dirty.service`? → [ADR-011](../adr/ADR-011-ap-is-the-anchor.md)
- SSID or BSSID computed anywhere but first boot? → [ADR-012](../adr/ADR-012-immutable-ssid.md)

If the change *should* contradict an ADR, that is fine — it needs a superseding
ADR in the same PR ([SOP-007](SOP-007-architectural-decisions.md)).

### 2. Are the invariants still true?

- **Can the AP drop as a result of this change?** Anything touching `radio/`,
  `lan/`, provisioning, or systemd units gets this question asked explicitly.
- **Can `BACKUP` now carry bytes it shouldn't?** Anything touching `allocator/`,
  `policy/`, or `probe/`.

### 3. Is the required test there?

`policy/`, `allocator/`, `enforce/`, `radio/` → scenario test, mandatory, no
exceptions for "obviously safe" changes. Bug fix → regression test that fails
before the fix.

A test that executes the code without asserting on behaviour does not count. Ask:
would this test fail if the logic were inverted?

### 4. Would the logs be enough in the field?

You cannot reproduce a customer's van. Read the added log lines and ask whether
they would let you diagnose this code path with nothing else
([SOP-004](SOP-004-logging-and-observability.md)).

- Required `extra` fields present — `workflow`, `state`, `intent`?
- Does every `started` have a terminal state?
- Do failures say what the system did *instead*?
- Any credential, payload, or per-packet logging? → block.

### 5. Correctness and units

- Bandwidth in `_bps`, latency in `_ms`, volume in `_bytes`. A missing unit suffix
  is where the factor-of-1000 bugs live.
- Failure paths fall back to last-known-good, not to a blank slate.
- New config keys have defaults that work on a device with no config file.

### 6. Everything else

Naming, structure, duplication, readability. Real, but last.

## How to write review comments

**Distinguish blocking from non-blocking.** An unlabelled comment reads as
blocking, which stalls PRs over preferences.

```
blocking: this stores ifname as identity — breaks on USB re-enumeration (ADR-002)
question: is the 120s dwell deliberate here, or copied from the activation path?
nit: `cap` reads as capability; `capacity_bps` is clearer
praise: the fallback when tc fails is exactly right, and well logged
```

**Say why, and prefer a concrete alternative.** "This is wrong" costs the author a
round trip. "This breaks when USB re-enumerates — use `atomic.id`" does not.

**Be specific about severity.** A theoretical concern flagged as a defect wastes
time; a real defect flagged as a nit gets merged.

**`praise:` is not padding.** Reviewers who only ever point at problems train
authors to dread review, and the good patterns never get reinforced.

## For authors

- Respond to every comment — even "done" — so nothing looks ignored.
- Disagreeing is fine and expected. Say why. If it doesn't converge in two rounds,
  pull in a third person rather than grinding in the thread.
- Push fixes as new commits during review; squash at merge. Force-pushing mid-review
  destroys the reviewer's place.
- If review reveals the change is wrong-shaped, close it and reopen. Salvaging a
  bad design through review comments produces the worst code in any repository.

## Turnaround

Aim for one working day. A PR sitting unreviewed goes stale, conflicts, and its
author loses the context needed to respond well. If you cannot review properly
within a day, say so in the PR so someone else can pick it up — silence is the
expensive option.

## What review does not cover

- **Formatting.** `ruff format` decides. Never review it.
- **Whether CI passes.** CI decides. Don't hand-verify what the pipeline verifies.
- **Hardware behaviour.** Review cannot tell you whether `brcmfmac` supports CSA.
  If a change depends on unverified driver behaviour, the blocking comment is
  "needs a spike" ([SOP-009](SOP-009-hardware-and-field-debugging.md)), not an
  opinion about the code.
