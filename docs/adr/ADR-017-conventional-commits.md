# ADR-017 — Conventional commits are mandatory and CI-enforced

**Status:** Accepted
**Date:** 2026-07-30

## Context

[ADR-016](ADR-016-versioning.md) derives the release version from commit subjects.
That makes commit messages load-bearing infrastructure rather than a documentation
courtesy.

The failure mode is unusually bad. A commit that should read `feat!:` but reads
`fix:` produces a patch release containing a breaking change. Devices update
automatically — the OTA client sees a patch bump, treats it as safe, applies it,
and the appliance is now incompatible with the fabric or has an unmigrated config.
Nothing errors at build time. Nothing errors at release time. The failure appears
in the field, on devices, after the fact.

A convention this consequential cannot rely on people remembering it.

## Decision

**Conventional commits are mandatory and enforced by CI on every pull request.**

```
<type>(<optional scope>): <subject>
```

Types: `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `build`, `ci`, `chore`.
`!` after the type, or `BREAKING CHANGE:` in the body, marks a major bump.

A `commitlint` job runs before anything else in the pipeline and fails the PR on a
malformed subject. Squash-merge subjects are checked too, since that is the subject
that reaches `master` and drives the version.

**"Breaking" has a specific meaning here**, and it is not "a big change":

- The OTA package cannot be applied to the previous version.
- The fabric protocol changed — bump `WIFUCKED_FABRIC_MIN` too.
- Stored user configuration needs migration.
- A user-facing behaviour they relied on is gone.

A 2000-line refactor with identical behaviour is `refactor:`. A one-line config
schema change is `feat!:`.

## Consequences

**Easier:**

- Versions are derived automatically and mean something.
- `CHANGELOG.md` generates from the same data, so it is always accurate and nobody
  maintains it by hand.
- Commit history becomes scannable — `git log --oneline` reads as a list of
  changes rather than a list of moments.

**Harder:**

- A real constraint on contributors, and an early friction point for anyone who has
  not used the convention.
- **CI can enforce the format but not the judgement.** Choosing `fix:` for a
  breaking change passes lint and ships wrong. That is a review responsibility
  ([SOP-006](../sop/SOP-006-code-review.md)), and the reason "is this actually
  breaking?" is on the review checklist.
- Squash-merge subjects need attention at merge time, which is exactly when
  attention is lowest.

**Must stay true:** the mapping from type to bump stays stable. Changing it would
alter the meaning of past versions and needs a superseding ADR.

## Alternatives considered

**Convention without enforcement** — rejected; a convention that silently
mis-versions releases when forgotten will be forgotten.

**Manual version bumps** — moves the judgement to an explicit step, which is
appealing, but relies on remembering during rushed merges and reintroduces the file
race ([ADR-016](ADR-016-versioning.md)).

**A release-please style bot managing versions and changelogs** — solves the same
problem well, and worth revisiting. Rejected for now because it commits back to the
release branch, which is the specific pattern ADR-016 exists to eliminate.

**Labels on pull requests instead of commit types** — enforceable and visible in
the UI, but the information then lives in GitHub rather than in the repository, and
is lost on any migration. Commit messages are the more durable substrate.
