# Versioning and releases

The decision and its rationale are [ADR-016](adr/ADR-016-versioning.md). This is
the operational reference.

## One channel

**`main` is the only branch.** Every push to it that touches buildable paths
produces exactly one immutable release. There is no `alpha`, no `beta`, no
`develop`, and no staging.

Merging is shipping ([SOP-008](sop/SOP-008-release-and-ota.md)).

## The version lives in a git tag

Not in a file. `scripts/next_version.sh` reads the last `vX.Y.Z` tag and scans the
commits since it:

| Commit contains | Bump | Example |
|---|---|---|
| `feat!:` / `fix!:` / `BREAKING CHANGE:` in body | **major** | `1.4.2` → `2.0.0` |
| `feat:` | **minor** | `1.4.2` → `1.5.0` |
| anything else | **patch** | `1.4.2` → `1.4.3` |

Highest bump found in the range wins. A repository with no tags starts at `0.1.0`.

**Nothing is committed back to `main` during a release.** No `VERSION` file, no
bot commit, no `[skip ci]`, no push race.

## Version strings by build kind

| Trigger | Version | Published |
|---|---|---|
| PR → `main` | `X.Y.Z-pr<N>.<sha7>` | Workflow artifact, 14-day retention |
| `workflow_dispatch` dry run | `X.Y.Z-rc.<sha7>` | Artifact only |
| Push → `main` | `X.Y.Z` | **Immutable release**, tag `vX.Y.Z`, marked latest |

`-pr` and `-rc` are real SemVer prereleases, so they sort *below* the release they
precede. An OTA client comparing versions can never mistake a PR build for a
shipped one.

## Release assets

Names are constructible from the version, so the OTA client never scrapes the API:

| Asset | Contents |
|---|---|
| `wifucked-<X.Y.Z>-arm64.img.zst` | Full SD-card image |
| `wifucked-<X.Y.Z>.wtf` | OTA update package (*Wi-Fucked Transfer Format*) |
| `manifest.json` | The one file the OTA client reads |
| `SHA256SUMS` | Checksums for both artifacts |
| `test-output.log` | The suite that gated this release |

Plus the matching fabric container: `ghcr.io/hajnina/wi-fucked/fabric:X.Y.Z`.

### `manifest.json`

```json
{
  "version": "1.4.0",
  "released_at": "2026-07-30T14:22:11Z",
  "commit": "abc1234",
  "image_url":   "https://github.com/.../wifucked-1.4.0-arm64.img.zst",
  "package_url": "https://github.com/.../wifucked-1.4.0.wtf",
  "sha256": { "image": "…", "package": "…" },
  "fabric_image": "ghcr.io/hajnina/wi-fucked/fabric:1.4.0",
  "min_upgradable_from": "1.0.0"
}
```

## No rolling tags

GitHub already provides a stable URL for the newest release:

```
https://github.com/hajnina/wi-fucked/releases/latest/download/manifest.json
```

So there is no `v-latest` tag to maintain, and — critically — no tag that gets
deleted and recreated on every build. **Releases are never deleted or overwritten.**
That permanence is what makes rollback to a specific known-good version possible,
which matters a great deal on a device that updates itself unattended.

If a release is bad, publish a newer one that fixes it. Never delete the bad one —
devices may already have it, and removing it breaks their rollback path.

## What the device knows about itself

`/etc/wifucked-release`, written into the image at bake time:

```sh
WIFUCKED_VERSION=1.4.0
WIFUCKED_COMMIT=abc1234
WIFUCKED_BUILD=42
WIFUCKED_BUILT_AT=2026-07-30T14:22:11Z
WIFUCKED_CHANNEL=main
WIFUCKED_FABRIC_MIN=1.2.0
```

`WIFUCKED_FABRIC_MIN` is the **protocol floor**. The appliance refuses to attach to a
fabric older than this rather than failing mysteriously mid-tunnel. Changing the
tunnel protocol means all three, in one PR: a `feat!:` commit, a `WIFUCKED_FABRIC_MIN`
bump, and an ADR.

## Appliance and fabric are two ends of one protocol

They must stay compatible, and the fabric is deployed **first**. A device that
updates into a fabric which cannot serve it has no Internet — and therefore no way
to be told why, and no way to receive a fix.

## Changelog

`CHANGELOG.md` is generated from the same conventional-commit scan that produces
the version. Nobody maintains it by hand, so it cannot drift from what actually
shipped.

## Practical notes

- **Check the squash-merge subject before confirming.** That subject is what
  reaches `main`, drives the version, and becomes the changelog entry.
- **CI enforces the commit format; it cannot check your judgement.** Choosing
  `fix:` for a breaking change passes lint and ships wrong — that is caught in
  review ([SOP-006](sop/SOP-006-code-review.md)).
- **"Breaking" is specific here**: the OTA package won't apply to the previous
  version, the fabric protocol changed, stored config needs migration, or a relied-on
  behaviour is gone. Size of diff is irrelevant.
