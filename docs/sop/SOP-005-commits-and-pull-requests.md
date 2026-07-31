# SOP-005 — Commits and pull requests

## Conventional commits are mandatory

**The release version is derived from commit subjects.** A sloppy subject line
does not just look untidy — it silently ships the wrong version number
([ADR-017](../adr/ADR-017-conventional-commits.md)). CI enforces the format;
CI cannot enforce whether you chose the right type.

```
<type>(<optional scope>): <subject>

<optional body>

<optional footer>
```

| Type | Use for | Version bump |
|---|---|---|
| `feat` | New user-visible capability | **minor** |
| `fix` | Bug fix | patch |
| `perf` | Performance improvement | patch |
| `refactor` | Restructuring, no behaviour change | patch |
| `docs` | Documentation, including SOPs and ADRs | patch |
| `test` | Tests only | patch |
| `build` | Build system, dependencies, image contents | patch |
| `ci` | Pipeline | patch |
| `chore` | Everything else | patch |

Add `!` after the type, or `BREAKING CHANGE:` in the body, for a **major** bump.

### What "breaking" actually means here

Not "a big change." It means one of:

- The OTA package cannot be applied to the previous version.
- The fabric protocol changed — an appliance and fabric at adjacent versions can
  no longer talk (bump `WIFUCKED_FABRIC_MIN`).
- A user's stored configuration needs migration.
- A user-facing behaviour they relied on is gone.

A 2000-line refactor with identical behaviour is `refactor:`. A one-line change to
the config schema is `feat!:`.

### Scopes

Use the module: `feat(allocator):`, `fix(discovery):`, `ci(bake):`,
`docs(sop):`. Optional, but it makes the changelog readable, and the changelog is
generated from these.

### Subject lines

Imperative mood, lowercase, no trailing period. Describe the change, not the file.

```
feat(allocator): activate backup only after sustained critical deficit
fix(discovery): key USB atomics on serial instead of enumeration order
docs(sop): add field-debugging procedure

feat: updates                      ← useless in a changelog
fix: fixed the thing               ← which thing
chore: wip                         ← not a change anyone can review
```

## Commit hygiene

- **One logical change per commit.** A reviewer should be able to understand a
  commit without holding the whole PR in their head.
- **No commented-out code.** Git remembers it.
- **No `[skip ci]`** on anything that touches `appliance/`, `fabric/`, or
  `scripts/`. Skipping the bake is how an unbuildable commit reaches `main`.
- Fixups get squashed before review, not after.

## Pull requests

**All PRs target `main`.** There is no other branch
([`../versioning.md`](../versioning.md)).

### Open automatically, don't wait to be asked

The moment work is committed and pushed to a branch, open the PR. Do not treat
PR creation as something that needs a separate request — a pushed branch with
no PR is unfinished work, not a deliberate pause point.

### Ownership and follow-through

Opening a PR creates an ownership obligation. Its author makes one CI-and-review
check five minutes after opening the PR or pushing an update, then makes one
check every 30 minutes. They respond to actionable feedback, fix failures, and
follow the PR through until it is merged or closed. Do not leave a PR for someone
else to discover after its checks fail or a reviewer comments.

### Merge automatically once green

Once CI passes and there is no unresolved reviewer feedback blocking the change,
merge it — do not leave a green PR idle waiting for someone to click the button.
If a reviewer has requested changes or left an open question, resolve it first;
silence from a reviewer within the check-in cadence above is not a block.

#### Waiting for CI without polling by hand

Don't burn a check-in on "still running." Block on a single script that returns
exactly once, when every check has left the pending state, then act on whatever
it reports. It must not silently loop forever — it runs a preflight first, and
it treats "the query broke" as a different outcome from "still pending":

```bash
#!/usr/bin/env bash
set -uo pipefail

PR="${1:?usage: wait-for-ci.sh <PR_NUMBER>}"
MAX_WAIT_SECONDS=5400   # 90 min ceiling — the bake job alone allows up to 120
POLL_SECONDS=20

# --- preflight: only start a watch we're certain can return ----------------
command -v gh >/dev/null 2>&1 || { echo "PREFLIGHT FAILED: gh CLI not found"; exit 2; }
gh auth status >/dev/null 2>&1 || { echo "PREFLIGHT FAILED: gh not authenticated"; exit 2; }

# Use gh's *built-in* --jq (no dependency on a system jq — a prior version of
# this script piped to an external jq that didn't exist in one environment
# and looped silently forever, since the failure was swallowed by 2>&1).
# Prove the exact query resolves before committing to the loop.
probe=$(gh pr checks "${PR}" --json bucket --jq 'length' 2>&1) || {
  echo "PREFLIGHT FAILED: 'gh pr checks ${PR}' did not return data: ${probe}"
  exit 2
}
[ "${probe}" -gt 0 ] 2>/dev/null || {
  echo "PREFLIGHT FAILED: PR #${PR} has no checks registered yet"
  exit 2
}
echo "preflight OK: PR #${PR} has ${probe} check(s); watching..."

# --- watch, bounded, and distinguishes "pending" from "broken" -------------
elapsed=0
while true; do
  done_flag=$(gh pr checks "${PR}" --json bucket --jq 'all(.[]; .bucket != "pending")' 2>&1)
  if [ "${done_flag}" = "true" ]; then
    echo "=== ALL CI FLOWS COMPLETE (after ${elapsed}s) ==="
    gh pr checks "${PR}"
    exit 0
  fi
  if [ "${done_flag}" != "false" ]; then
    echo "WATCH ABORTED after ${elapsed}s: gh query failed: ${done_flag}"
    exit 1
  fi
  sleep "${POLL_SECONDS}"
  elapsed=$((elapsed + POLL_SECONDS))
  if [ "${elapsed}" -ge "${MAX_WAIT_SECONDS}" ]; then
    echo "WATCH TIMED OUT after ${elapsed}s — CI still pending, check manually"
    gh pr checks "${PR}"
    exit 1
  fi
done
```

Run it in the background (Claude Code: `Bash` with `run_in_background: true`, or
plain `nohup ... &` at a terminal) so it produces one notification when CI settles
instead of a stream of "still pending" noise. This satisfies the five-minute and
30-minute check-in cadence above without babysitting a terminal — but it does not
replace follow-through: read the result, fix failures, and keep the PR moving.

If you also want a fixed-cadence backstop independent of this loop (e.g. across a
longer stretch where you might be away), Claude Code's `CronCreate` tool can
schedule a recurring "check PR #N, fix what's red" prompt — but note it is
session-only: the job dies with the session, so it does not substitute for
watching the PR through to green yourself.

### Don't leave a green PR open waiting for someone else's automation

If you have merge rights and the PR is green and mergeable, **merge it**. Do not
leave it open on the theory that some other automation — a bot, a scheduled
Routine, another session — will pick it up. A stale open PR is not a passive
state: every commit anyone else lands on `main` in the meantime is a chance for
it to drift out of sync and need a rebase, or worse, to merge cleanly on stale
assumptions and silently reintroduce something the other commit just fixed. A
task description or handoff note may say "an automated Routine will merge this"
— that is a claim about intent, not a guarantee about timing, and it is not a
reason to sit on green CI. Verify, then merge. If something is genuinely not
yours to merge (someone else's PR, a policy reason), say so explicitly rather
than leaving it ambiguous who's landing it.

### Auto-merge is real, but it doesn't replace watching your own PR

[`.github/workflows/automerge.yml`](../../.github/workflows/automerge.yml)
squash-merges a PR itself once its `CI` run reports success and `gh pr view`
shows it `OPEN`, not a draft, `MERGEABLE`, and `mergeStateStatus: CLEAN`. This
exists because this repo has no GitHub plan/branch-protection setup that makes
native "enable auto-merge" do anything — this is the DIY equivalent, built from
a `workflow_run` trigger and `gh`, not a paid feature. It deliberately refuses
to merge anything whose head repository isn't this one, so a fork's CI success
can never trigger a merge with this repo's token.

This does not change the ownership rule above. It fires once, after the fact;
if the mergeability check races GitHub's own async computation and comes back
`UNKNOWN` for too long, or the branch needed an update first, it logs why and
leaves the PR alone rather than looping. Watching your PR through to green and
merging it yourself if auto-merge doesn't get there is still the job — this
workflow is a backstop for the case where you can't, not a reason to stop
checking.

### Before opening

```bash
./run_all_tests.sh          # green, locally, on your branch
```

CI bakes a multi-gigabyte image. Discovering a lint error there wastes a runner
slot and everyone queued behind you.

### The description

Write for the reviewer, and for whoever reads it in a year while debugging.

```markdown
## What
One or two sentences. What changed, in behaviour terms.

## Why
The problem this solves. Link the issue or the scenario that motivated it.

## How
Only the parts that aren't obvious from the diff. Trade-offs you considered
and rejected are worth more here than a restatement of the code.

## Verification
What you ran, and what you observed. Scenario tests added, hardware checks
performed, what you deliberately did not test.

## Risk
What could this break? What should a reviewer look at hardest?
```

Empty sections are fine when genuinely empty. A PR body that just says "see title"
is not.

### What must be in the PR

- **Scenario test** if you touched `policy/`, `allocator/`, `enforce/`, or `radio/`
  ([SOP-003](SOP-003-testing.md)). Non-negotiable.
- **Regression test** if you fixed a bug — one that fails before your fix.
- **ADR** if you changed a boundary, persistence format, enforcement strategy,
  radio model, or release contract ([SOP-007](SOP-007-architectural-decisions.md)).
- **SOP update** if you changed how the team works
  ([SOP-010](SOP-010-keeping-documentation-current.md)).
- **Doc update** if you changed something the docs describe. Same PR, not later.

### Size

Large PRs get reviewed badly. If yours exceeds roughly 400 lines of substantive
diff, look for a seam — a preparatory refactor, a test-only commit, an interface
introduced ahead of its implementation. Where it genuinely cannot be split, say so
in the description and flag what to read first.

## Merging

- Squash merge. `main` history is one commit per change, and the squash subject
  becomes the changelog entry — check it before confirming.
- Green CI. Never merge red, never merge with the bake skipped.
- Delete the branch.

Every merge to `main` publishes an immutable release
([SOP-008](SOP-008-release-and-ota.md)). Merging is shipping. Treat it that way.
