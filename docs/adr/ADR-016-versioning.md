# ADR-016 — Tag-derived SemVer, one channel, immutable releases

**Status:** Superseded by [ADR-018](ADR-018-main-release-channel.md)
**Date:** 2026-07-30

## Context

The project needs a versioning scheme for an appliance that updates itself over the
air, paired with a server-side component that must stay protocol-compatible.

The inherited pattern from Gutiva is instructive mainly as a list of things not to
do:

- **Three branches, each auto-bumping a different SemVer component** — patch on
  `alpha`, minor on `beta`, major on `main`. The version encodes *which branch
  built it*, not what changed. A major bump on every push to `main` is
  meaningless.
- **A `VERSION` file bumped by CI and pushed back to the branch being built**,
  behind a five-attempt retry-and-rebase loop. The race exists only because the
  version lives in a mutable file on that branch.
- **Rolling release tags deleted and recreated on every build**
  (`gh release delete --cleanup-tag`). No history, no immutable artifacts, no way
  to roll a device back to a known-good image.
- **PR builds published into the rolling `alpha` release**, so the channel devices
  could follow was full of untested builds.

For a device that updates itself, the ability to roll back to a specific known-good
version is not a nicety.

## Decision

**The source of truth is an annotated git tag `vX.Y.Z` on `master`, not a file.**

- CI reads the last tag, scans commits since it, and derives the bump from
  conventional-commit types: `feat!:`/`BREAKING CHANGE:` → major, `feat:` → minor,
  else patch ([ADR-017](ADR-017-conventional-commits.md)).
- **Nothing is committed back to `master` during a release.** No push race to lose.
- **One channel.** Every push to `master` produces exactly one immutable release.
  Nothing is ever deleted or overwritten.
- **No rolling pointer tags.** `/releases/latest/download/<asset>` already provides
  a stable URL.
- PR builds are versioned `X.Y.Z-pr<N>.<sha7>` — real SemVer prereleases that sort
  *below* the release they precede — and are uploaded as workflow artifacts, never
  published.

Assets carry constructible names, so the OTA client never scrapes the API.
`WIFUCKED_FABRIC_MIN` in `/etc/wifucked-release` records the protocol floor: an appliance
refuses an older fabric rather than failing mysteriously mid-tunnel
([ADR-005](ADR-005-tunnel-is-mandatory.md)).

## Consequences

**Easier:**

- The version means something. `1.4.0` → `1.5.0` says a feature landed, regardless
  of which machine built it.
- Rollback to any prior version is always possible, because every release still
  exists.
- No bot commits, no `[skip ci]`, no retry loop, no race.
- Prerelease ordering means an OTA client can never mistake a PR build for shipped.

**Harder:**

- **Commit discipline becomes load-bearing.** A mistyped subject silently
  mis-versions a release — hence [ADR-017](ADR-017-conventional-commits.md) and CI
  enforcement.
- Releases accumulate. A busy month produces many, and the list needs pruning for
  readability even though nothing is deleted.
- Merging is shipping. There is no staging branch to catch a mistake, which raises
  the bar on review and CI ([SOP-008](../sop/SOP-008-release-and-ota.md)).
- Appliance and fabric versions must be managed together.

**Must stay true:** tags are never deleted or moved. A moved tag would break
rollback and desynchronise every device's view of history.

## Alternatives considered

**Keep the three-branch model** — rejected; the version would encode branch rather
than change, and three channels for one product is two more than anyone can keep
straight.

**`VERSION` file bumped by CI** — the inherited approach. Rejected for the push
race, which is inherent rather than incidental.

**`VERSION` file bumped by humans in the PR** — explicit and race-free, but relies
on remembering, and gets forgotten during exactly the rushed merges where the
version matters most.

**Date-based versioning (`2026.07.30`)** — unambiguous and easy to generate, but
carries no compatibility information. `WIFUCKED_FABRIC_MIN` needs a comparable
ordering with meaning, which SemVer provides and dates do not.
