# ADR-018 — Main is the sole release channel

**Status:** Accepted
**Date:** 2026-07-31

## Context

ADR-016 chose one immutable, tag-derived release channel and named it
`master`. The repository's intended default branch and its established GitHub
branch are `main`. Leaving the implementation, workflows, and contributor
guidance split between the two names prevents compliant pull requests and can
silently stop releases from being built.

The one-channel, tag-derived SemVer release contract remains correct. Only the
channel's branch name changes.

## Decision

**`main` is the sole release branch.** Pull requests target `main`; pushes to
`main` trigger the immutable release workflow; manifests and baked images record
`main` as their channel. ADR-018 supersedes ADR-016 only where it identifies
the release branch as `master`.

## Consequences

- Workflow triggers, release scripts, manifests, tests, and contributor
  instructions use `main` consistently.
- Historical references to `master` in ADR-016 and ADR-017 remain unchanged as
  records of the former decision; this ADR defines the active contract.
- The repository must keep `main` as its default branch. Renaming it requires a
  new ADR and a coordinated workflow and documentation update.

## Alternatives considered

**Keep `master`** — rejected because it contradicts the requested default branch
and the existing repository branch users contribute to.

**Support both branches** — rejected because it creates two release channels,
which violates the one-channel release contract and makes versioning ambiguous.
