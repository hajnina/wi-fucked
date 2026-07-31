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
