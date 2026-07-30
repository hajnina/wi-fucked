# TODO

Handover items that need a human. Delete each entry once it's done.

---

## 1. Install the CI workflows — **blocks all CI**

**Status:** blocked on credentials, not on work. The files are written, linted
with `actionlint`, and shipped in [`workflows.zip`](workflows.zip) at the repo
root.

They could not be pushed: the session's git credential is an OAuth App without
`workflow` scope, and GitHub refuses any push that creates or updates a file
under `.github/workflows/`.

```
! [remote rejected] refusing to allow an OAuth App to create or update
  workflow `.github/workflows/ci.yml` without `workflow` scope
```

### To install

```bash
unzip workflows.zip          # lands .github/workflows/*.yml at the repo root
rm workflows.zip
git add .github/workflows
git commit -m "ci: single-channel workflows for PR checks and master releases"
git push
```

`workflows.zip` contains, with repo-relative paths:

| File | Purpose |
|---|---|
| `.github/workflows/ci.yml` | PR checks — commitlint, ruff, shellcheck, actionlint, tests, unpublished bake |
| `.github/workflows/master_release.yml` | Push to `master` → version → test → bake → one immutable release |
| `.github/workflows/reusable_image_pipeline.yml` | The image bake, called by both |

sha256: `02e749d8ac9b4c4e271a0cec2d0a764ff84cf3e9a6160ec8c64d19e4f896929f`

Once installed, delete this section and `workflows.zip`.

---

## 2. Rename the default branch to `master`

The repository's default branch is currently `main`. Everything is written
against `master`: the workflow triggers, the branch rule in `CLAUDE.md`, and
[`docs/versioning.md`](docs/versioning.md).

In **Settings → Branches**: rename `main` → `master`, then protect it. PRs target
`master` and nothing else — there is no `alpha`, `beta`, or `develop`.

---

## 3. Run the radio capability spike — **blocks the LAN design**

**This is task zero of Phase 0.** See [`docs/radio-spike.md`](docs/radio-spike.md)
for the full brief; it is written so a junior can execute it unattended.

[ADR-013](docs/adr/ADR-013-radio-profiles.md) and
[ADR-014](docs/adr/ADR-014-two-ssid-fallback.md) are marked ⚠ **unverified**.
They encode reasonable expectations about `brcmfmac`, not measured facts. The
answers decide whether the appliance can serve two SSIDs from one radio, whether
SHARED profile is possible at all, and whether CSA keeps clients associated
across a channel move.

Nothing that depends on radio behaviour should be built until this reports.
Timeboxed to one week; the deliverable is the findings section of that document
plus superseding ADRs.

---

## 4. Provision the fabric before the first release reaches devices

The appliance and the fabric are two ends of one tunnel protocol and must stay
version-matched ([ADR-005](docs/adr/ADR-005-tunnel-is-mandatory.md)). A device
that updates into a fabric which cannot serve it has no Internet — and therefore
no way to be told why, and no way to receive a fix.

Deploy `ghcr.io/hajnina/wi-fucked/fabric` **first**, always.

---

## 5. Choose a licence

Currently unlicensed. Phase 3 in [`docs/roadmap.md`](docs/roadmap.md).

---

## Not this repository: a credential is leaking in Gutiva

Out of scope here and deliberately untouched, but worth acting on.

`scripts/build_poop.sh:66-73` copies `tailscale.key` into the `.poop` package,
and that package is uploaded as a GitHub release asset
(`reusable_firmware_pipeline.yml:382`). Anyone with read access to Gutiva's
releases has the Tailscale auth key.

Four other defects in that pipeline are documented — with the reasoning for how
this repository avoids each — in
[`docs/sop/SOP-008-release-and-ota.md`](docs/sop/SOP-008-release-and-ota.md).
