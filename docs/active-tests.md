# Active tests — code that runs for real, unconfirmed on real hardware

This tracks the gap between "the code path exists and executes on a Pi" and
"a human has watched it work on a Pi." `MOCK_HW=1` and scenario tests validate
*logic* — that the state machine does what it says given a fake HAL. They
cannot validate whether `brcmfmac`, `hostapd`, or the driver underneath them
actually behaves the way the code assumes.

An entry belongs here when code that touches real, unmocked hardware behaviour
has been merged and is live in `build_linux_hal()` / the real control loop,
**and no one has yet confirmed the result on an actual device.** It is not for
things still gated behind a spike that hasn't run at all (those live in
[`radio-spike.md`](radio-spike.md)) — it's for things that will *actually
execute*, right now, on whatever hardware runs this daemon, with an outcome
nobody has observed.

## Rule for anyone (human or agent) touching this repo

Before assuming a piece of behaviour works because "the code is there": check
this file. If the relevant entry says `UNCONFIRMED`, **ask the user directly**
whether they've run it on real hardware and what happened, before building on
top of it or reporting it as working. When you do get a real result — success
or failure — update the entry and move it to `CONFIRMED` (or `BROKEN`, with the
finding) in the same session. An entry that never gets updated is worse than
no entry; it teaches people to stop trusting this file.

---

## Entries

### USB OTG forced host mode, persistent boot log, temporary HDMI console

**Status:** `UNCONFIRMED`
**Touches:** `appliance/setup_rpi.sh` (`dtoverlay=dwc2,dr_mode=host`, verbose boot),
`appliance/stage-custom/opt/wifucked/hdmi_console.sh`,
`appliance/stage-custom/etc/systemd/system/wifucked-console.service`,
`appliance/stage-custom/opt/wifucked/firstboot.sh` / `bootcount.sh` (persistent
`/var/log/wifucked-boot.log`)
**Related:** [ADR-011](adr/ADR-011-ap-is-the-anchor.md), #15, PR #16

**What actually runs today:** PR #16 forced the OTG port into USB host mode
(`dtoverlay=dwc2,dr_mode=host`) to fix "phone charges but never gets a data
connection," added a persistent on-disk boot log so a failed first boot isn't
lost when `journalctl`'s RAM-only storage is wiped by the next power cycle,
and added a temporary HDMI console (`wifucked-console.service`) streaming a
live status snapshot and `journalctl -f` to `tty1`. All of it is live in the
current image build and none of it has been run on a device — the PR's own
test plan flagged this and said the result belonged here; it was never added
until now.

**What is unconfirmed:** Whether forcing `dr_mode=host` actually makes phone
tethering enumerate reliably on this hardware; whether the HDMI console
renders usably on tty1 without fighting the verbose kernel boot output it's
layered under; whether the persistent boot log survives the SD-card-write
pattern it's meant to survive.

**Built-in fallback if it fails:** None — this *is* the fallback diagnostic
path for #15. If it doesn't work, there is currently no other way to observe
a first boot that produces no AP and no reachable dashboard.

**Next step:** Boot a real device with a monitor attached and confirm the
HDMI console appears, plug a phone into the OTG port and confirm it tenders a
data connection, and check `/var/log/wifucked-boot.log` after a power cycle.

**History:**
- 2026-08-02 — flagged as unconfirmed; the entry PR #16 promised but never
  added. Merged and live since 2026-08-01, not yet run against real hardware
  by anyone.

---

### AP bring-up: NetworkManager unmanaged-devices fix, rfkill unblock, static VLAN gateway addresses

**Status:** `UNCONFIRMED`
**Touches:** `appliance/setup_rpi.sh` (NetworkManager `unmanaged-devices`, `systemd-networkd` enablement),
`appliance/stage-custom/etc/systemd/system/hostapd.service.d/10-wifucked.conf` (new),
`appliance/src/wifucked/lan/__init__.py` (`networkd_config`, `networkd_unit_name`),
`appliance/stage-custom/opt/wifucked/firstboot.sh`
**Related:** [ADR-011](adr/ADR-011-ap-is-the-anchor.md), [SOP-009](sop/SOP-009-hardware-and-field-debugging.md), #15

**What actually runs today:** Issue #15 reported "no hotspot, ever" on first
real-hardware boot — neither SSID visible in a scan, no error surfaced
anywhere. Root-cause analysis (no hardware in the loop; reasoned from the
generated config and known `hostapd`/`NetworkManager` failure modes) found
three concrete defects, now fixed:

1. `NetworkManager`'s `unmanaged-devices` list named `ap0`, an interface that
   has never existed on this hardware. The real AP radio (`wlan0`, and the
   VLAN subinterfaces `hostapd` creates under it) was left under
   `NetworkManager`/`wpa_supplicant` control, which is a well-documented cause
   of `hostapd` failing to bind the radio at all. Now
   `interface-name:wlan0*`.
2. Nothing ever ran `rfkill unblock` on this image, since `wpa_supplicant`
   (which normally does this as a side effect) was never supposed to touch
   `wlan0`. A radio that boots soft-blocked fails `hostapd`'s `nl80211` driver
   init silently, with no SSID and no retry. `hostapd.service.d/10-wifucked.conf`
   now unblocks on every start and adds `Restart=on-failure`.
3. Nothing assigned an address to the VLAN subinterfaces `hostapd` creates for
   each service profile (`wlan0.20`, `wlan0_1.10`), so a client that *did*
   associate would get a DHCP lease pointing at a gateway that doesn't exist.
   `networkd_config()` now generates a `systemd-networkd` `.network` file per
   profile at first boot, matched by interface name so it applies whenever
   `hostapd` brings the subinterface up — independent of the daemon, same as
   `hostapd`/`dnsmasq` themselves.

**What is unconfirmed:** Whether these were in fact the causes of #15's "no
AP" symptom, whether `systemd-networkd` and `NetworkManager` coexist cleanly
on this exact Raspberry Pi OS Trixie image with this split of interfaces, and
whether `hostapd`'s per-BSS `vlan_id` subinterfaces actually appear with the
names `lan_ifname_for_profile()` assumes (only unit-tested against the
generator functions, never against a running `hostapd`).

**Built-in fallback if it fails:** None beyond `hostapd`'s own
`Restart=on-failure` — if the AP still doesn't come up, `wifucked-console.service`
(temporary HDMI bring-up console, see `TODO.md` item 2) is the way to observe
why on the next attempt.

**Next step:** Flash an image with this fix, boot a device, and confirm: SSID
visible in a scan, a client can associate and get a DHCP lease with a working
gateway on both `Stable_critical` and `Stable_besteffort`, and phone tethering
via OTG still works (the fix from PR #16 this builds on top of, also still
`UNCONFIRMED`).

**History:**
- 2026-08-02 — fix merged in response to #15 continuing to report "no AP" after
  PR #16 (which addressed OTG tethering and diagnostics, not this). Not yet
  run against real hardware by anyone.

---

### AP+STA SHARED profile, CSA channel-following

**Status:** `UNCONFIRMED`
**Touches:** `radio/__init__.py` (`RadioManager.observe`/`align`), `hal/linux.py`
(`LinuxAp.channel_switch`, `LinuxWifi.station_link`/`connect_station`/`capabilities`)
**Related:** [ADR-013](adr/ADR-013-radio-profiles.md), [ADR-014](adr/ADR-014-two-ssid-fallback.md),
[`radio-spike.md`](radio-spike.md) Q1–Q3

**What actually runs today:** `Daemon._fast_loop()` calls
`self.radio.observe(atomics)` and, on a channel conflict, `self.radio.align(...)`
on every fast-loop tick (~1s) whenever real hardware is in use. This is
unconditional — there is no capability probe gating it. The moment the radio
profile is `SHARED` and the station's channel differs from the AP's, the
daemon will call `hostapd_cli chan_switch` for real, against whatever clients
are actually associated to the AP at that moment.

**What is unconfirmed:**
- Whether `brcmfmac`/CYW43438 supports AP+STA concurrency at all on this exact
  kernel/driver/firmware combination (Q1).
- Whether `hostapd_cli chan_switch` succeeds, or returns "CSA is not supported"
  as widely reported for related Broadcom chips in this family (Q3) — see the
  literature review added to `radio-spike.md`.
- Whether associated clients survive the attempt if it does something.

**Built-in fallback if it fails:** `RadioManager.align()` sets
`csa_unavailable = True` after the first failed switch and stops retrying —
logged at `warning` with `workflow: csa_move`. So a failure degrades to "AP
stuck on the wrong channel until a USB WAN is plugged in," not a crash loop.
But the *first* attempt happens live and unobserved, against a real AP with
real associated clients, the first time a SHARED-profile channel conflict
occurs on real hardware.

**Next step:** run `radio-spike.md` Q1–Q3 on the actual Pi Zero 2W. Until an
entry here says `CONFIRMED` or `BROKEN`, treat every SHARED-profile channel
move on real hardware as a live experiment, not a known-working feature.

**History:**
- 2026-07-31 — flagged as unconfirmed; code path is live and unguarded as
  described above. Not yet run against real hardware by anyone.

---

## Template for new entries

```markdown
### <short name of the behaviour>

**Status:** `UNCONFIRMED` | `CONFIRMED` | `BROKEN`
**Touches:** <files/modules>
**Related:** <ADRs, spike doc, other refs>

**What actually runs today:** <what fires, under what condition, calling what
real command/syscall>

**What is unconfirmed:** <the specific claims nobody has observed>

**Built-in fallback if it fails:** <what happens to the system if the
assumption is wrong — or "none" if that's the honest answer>

**Next step:** <what running it once would look like>

**History:**
- <date> — <what happened, or that it's still unrun>
```
