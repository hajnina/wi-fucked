# SOP-008 — Release and OTA

## Merging is shipping

There is one channel. **Every merge to `main` that touches buildable paths
publishes an immutable release** that devices in the field will install. There is
no staging branch to catch a mistake ([`../versioning.md`](../versioning.md)).

If you are not ready for users to have it, do not merge it.

## How a release happens

You do not cut releases by hand. On push to `main`:

1. `next_version.sh` reads the last `vX.Y.Z` tag and scans commits since it.
2. Conventional-commit types determine the bump — `feat!:` major, `feat:` minor,
   else patch ([SOP-005](SOP-005-commits-and-pull-requests.md)).
3. Tests run. **They gate publish** — a red suite means no release.
4. The image bakes; the capability gate asserts the OS can actually enforce policy.
5. An annotated tag `vX.Y.Z` is created, and one immutable GitHub Release published.
6. The `fabric` container is pushed at the same version.

The version lives in the **git tag**, not in a file. Nothing commits back to
`main` during a release, so there is no push race to lose.

## Release assets

Names are constructible, so the OTA client never scrapes the API:

| Asset | Contents |
|---|---|
| `wifucked-<X.Y.Z>-arm64.img.zst` | Full SD image |
| `wifucked-<X.Y.Z>.wtf` | OTA update package |
| `manifest.json` | The one file the OTA client reads |
| `SHA256SUMS` | Checksums for both artifacts |
| `test-output.log` | The suite that gated this release |

**Never delete or overwrite a release.** History is what makes rollback possible.
If a release is bad, publish a newer one that fixes it.

## Before you touch the pipeline

The image bake is the most expensive thing in this repository — a full run is
tens of minutes on a runner. Respect that:

- Test workflow syntax locally with `actionlint` before pushing.
- Use `workflow_dispatch` with `dry_run` to exercise the release path without
  publishing.
- Do not disable the capability gate to get a build through. It exists because an
  image that cannot run CAKE or move the AP channel cannot do its job, and shipping
  one is worse than shipping nothing.
- Never add `continue-on-error` to a publish step. A green run with no assets is
  the worst possible outcome — it looks like success.

## Never ship a secret

**No credential goes into a release artifact.** Not a fabric key, not a WireGuard
private key, not a token — release assets are distributed, and anyone who can read
a release gets everything in it.

Device keys are generated **on-device at first boot**. CI greps the built package
for key-shaped content and fails the build if it finds any. Do not weaken that
check; if it false-positives, fix the pattern, and say so in the PR.

## Breaking changes and the fabric

The appliance and the fabric are two ends of one protocol and must stay compatible.

- `WIFUCKED_FABRIC_MIN` in `/etc/wifucked-release` is the protocol floor. An appliance
  refuses to attach to an older fabric rather than failing mysteriously mid-tunnel.
- Changing the protocol means `feat!:` **and** bumping `WIFUCKED_FABRIC_MIN` **and**
  an ADR. All three, same PR.
- Deploy the fabric before the appliance release reaches devices. A device that
  updates into a fabric that cannot serve it has no Internet, and no way to be told
  why.

## OTA safety rules

These protect the always-available promise during updates
([ADR-011](../adr/ADR-011-ap-is-the-anchor.md)):

1. **Control-plane-only updates must not restart `hostapd` or `dnsmasq`.** The AP
   does not drop for a daemon update. If your change requires an AP restart, that
   is a design decision needing an ADR, not an implementation detail.
2. **Updates apply to the inactive slot.** The running system is never modified in
   place.
3. **Every update is validated after boot** by the watchdog. Unhealthy → automatic
   rollback to the previous slot.
4. **Updates are scheduled for a quiet window** and never applied during active
   critical traffic.

## Testing an OTA before it reaches users

An update that bricks the appliance is unrecoverable in the field — nobody is
pulling the SD card out of a device in a van.

Before merging anything that touches `update_script.sh`, `stage-custom/`,
`setup_rpi.sh`, or the OTA client:

1. Bake the image via `workflow_dispatch`.
2. Flash a real Pi Zero 2W.
3. Apply the update from the *previous* release, not from a clean image — upgrade
   is the path users take, and the one that breaks.
4. Verify: AP stayed up, version advanced in `/etc/wifucked-release`, daemon healthy.
5. Deliberately break the health check and confirm the rollback fires.

Record the result in the PR under Verification. "Tested OTA" without saying from
which version is not a record.

## If a bad release ships

1. **Do not delete it.** Devices may already have it; a missing release breaks
   their rollback path.
2. Publish a fix forward — a new release with a `fix:` commit.
3. If devices are already broken, the watchdog rollback should have caught it. If
   it did not, that is a second bug and needs its own fix.
4. Write up what happened and which gate should have caught it. Add the gate. Most
   entries in [SOP-003](SOP-003-testing.md)'s invariants earned their place this
   way.
