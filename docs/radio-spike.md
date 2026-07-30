# Radio capability spike — brief and findings

**Status: NOT YET RUN.** This document is currently a brief. When the spike is
executed, findings replace the brief and amend
[ADR-013](adr/ADR-013-radio-profiles.md) and
[ADR-014](adr/ADR-014-two-ssid-fallback.md).

---

## Why this is task zero

Two accepted ADRs rest on driver behaviour that **nobody has measured on this
chip**. They encode reasonable expectations, not facts, and they say so.

If those expectations are wrong, the LAN design changes — possibly substantially.
Everything downstream (classification, policy, the two-class contract) sits on top
of that design. Building it out first and discovering the radio cannot support it
would be the most expensive mistake available in this project.

So: measure first, on real hardware, before anything that depends on radio
behaviour is built.

**Timebox: one week.** Deliverable is this document, filled in. Spike code is
throwaway and is not merged.

---

## Hardware and software under test

- Raspberry Pi Zero 2W — CYW43438, 2.4 GHz only, 802.11 b/g/n, single stream
- Raspberry Pi OS Lite arm64 (the release pinned in `.github/builder.Dockerfile`)
- `brcmfmac` driver and the firmware shipped with that image

**Record exact versions.** Driver and firmware behaviour differs between releases,
and a finding without a version attached cannot be trusted later.

```bash
uname -a
modinfo brcmfmac | head -20
dmesg | grep -i 'brcmfmac.*firmware'
hostapd -v
iw --version
```

---

## Questions to answer

Answer each with a **yes/no plus evidence**. "Seemed to work" is not an answer.

### Q1 — Does AP+STA concurrency work at all?

Can the chip run an access point and a station simultaneously?

```bash
iw phy phy0 info | grep -A 20 'valid interface combinations'
```

Then actually do it: bring up an AP, join a network as a station, pass traffic both
ways at the same time.

**If no:** [ADR-013](adr/ADR-013-radio-profiles.md)'s SHARED profile is impossible.
A USB WAN becomes mandatory, which is a significant product change — the appliance
would not work with Wi-Fi as its only WAN.

### Q2 — Must AP and STA share a channel?

Expected yes. Confirm it, and confirm what happens when you try to violate it —
does the AP follow silently, does the station fail to associate, or does something
worse happen?

### Q3 — Does CSA work?

The load-bearing question for the always-available promise.

```bash
hostapd_cli chan_switch 5 2462     # move to channel 11
```

With clients associated. Measure:

- Do associated clients **stay associated**? (`hostapd_cli list_sta` before/after)
- How long is traffic interrupted? Run a continuous ping and count losses.
- Does a **TCP session survive**? Hold an SSH session across the move.
- Test with several client types — Android, iOS, macOS, Windows, a cheap IoT
  device. Client CSA handling varies, and the cheap IoT device is the one that will
  fail.

**If no:** the AP cannot follow a WAN channel without dropping clients. Either pin
the AP channel and restrict SHARED to same-channel WANs, or require a USB WAN for
off-channel networks. Both are meaningful product changes.

### Q4 — Do two BSS work?

```
# /etc/hostapd/hostapd.conf
interface=wlan0
ssid=Stable_critical
...
bss=wlan0_1
ssid=Stable_besteffort
```

- Do both SSIDs appear and accept clients?
- Do they work **while a station is also connected** (Q1 combination)?
- Do the BSSIDs differ sensibly?

**If no:** [ADR-014](adr/ADR-014-two-ssid-fallback.md)'s fallback becomes the
primary path — one SSID, two PSKs.

### Q5 — Does per-PSK VLAN assignment work?

The fallback must be verified even if Q4 succeeds, because it is the sanctioned
backup and an unverified fallback is not a fallback.

```
wpa_psk_file=/etc/hostapd/wpa_psk
vlan_file=/etc/hostapd/hostapd.vlan
dynamic_vlan=1
```

Confirm two passphrases place clients on different VLANs, and that `nftables` can
classify on the resulting interfaces.

### Q6 — What is the actual throughput?

Numbers, not impressions. `iperf3` against a LAN host, TCP, several runs:

| Configuration | Down | Up |
|---|---|---|
| AP only, one client | | |
| AP only, four clients | | |
| AP + STA concurrent (SHARED) | | |
| AP + STA + WireGuard | | |
| Two BSS, one client each | | |

The SHARED number is the one that matters most — it sets the honest ceiling for a
Wi-Fi-only deployment, and [`hardware.md`](hardware.md) currently *estimates* it at
roughly half of AP-only. Confirm or correct that.

### Q7 — What breaks under stress?

- Does the AP survive the station repeatedly connecting and disconnecting?
- Does it survive 50 CSA moves in a row?
- What happens on `brcmfmac` firmware crash — does the AP recover, and how long
  does it take?
- Behaviour under undervoltage (`vcgencmd get_throttled`)?

---

## Findings

> *To be completed by whoever runs the spike. Replace this section.*

### Summary

| Question | Answer | Confidence |
|---|---|---|
| Q1 AP+STA concurrency | | |
| Q2 Same-channel constraint | | |
| Q3 CSA works | | |
| Q4 Two BSS | | |
| Q5 Per-PSK VLAN | | |
| Q6 Throughput | | |
| Q7 Stress behaviour | | |

### Environment

```
(exact kernel, driver, firmware, hostapd versions)
```

### Detail

*Per question: what was tried, what was observed, raw command output where it
matters. Include the failures — a thing that did not work, and how it failed, is
often more useful later than a thing that did.*

### Consequences for the ADRs

*Which ADRs are confirmed, which need superseding, and what the superseding
decision should say.*

---

## Rules for running this

- **Measure, do not infer.** If you did not observe it, it is not a finding.
- **Record versions with every result.** A finding without a version is unusable in
  six months.
- **Write down what did not work.** Negative results are the point.
- **Do not merge spike code.** It exists to answer questions, not to be maintained.
- **Amend the ADRs in the same week.** A spike whose findings never reach the
  architecture was wasted effort ([SOP-007](sop/SOP-007-architectural-decisions.md)).

## Keeping this current

This document is the team's shared model of the hardware. Anything learned about
radio behaviour later — during Phase 1, during the field trial, from a firmware
update that changes something — gets added here in the same session it is
discovered ([SOP-009](sop/SOP-009-hardware-and-field-debugging.md)).

It is only worth anything if it stays accurate.
