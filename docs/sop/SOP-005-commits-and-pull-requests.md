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
  no longer talk (bump `DIRTY_FABRIC_MIN`).
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

### Ownership and follow-through

Opening a PR creates an ownership obligation. Its author checks CI and review
activity at least every 30 minutes, responds to actionable feedback, fixes
failures, and follows the PR through until it is merged or closed. Do not leave
a PR for someone else to discover after its checks fail or a reviewer comments.

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
