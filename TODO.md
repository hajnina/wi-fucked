# TODO

Handover items that need a human. Delete each entry once it's done.

---

## 2. Remove the temporary HDMI bring-up console — **before any real release**

`wifucked-console.service` / `appliance/stage-custom/opt/wifucked/hdmi_console.sh`
were added to debug why the first real-hardware boots produced no AP and no
observable failure (no journal persistence, no console). They are explicitly
marked temporary in every file they touch (`setup_rpi.sh`, the unit itself)
because they trade away two things this project protects on purpose: SD card
survival (continuous writes/streaming to disk and tty1) and the "ACT LED is
the only status channel" design (`docs/hardware.md`) — a shipped device has
no monitor attached.

Delete `wifucked-console.service`, `hdmi_console.sh`, the verbose-boot
`cmdline.txt` edit, and the `tee`/`exec >>` persistent-log lines in
`firstboot.sh`/`bootcount.sh` once devices are booting and reachable
reliably enough that this isn't needed to see what's happening.

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
