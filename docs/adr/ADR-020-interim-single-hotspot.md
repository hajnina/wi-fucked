# ADR-020 — Interim default: one hotspot, no Wi-Fi WAN, until the radio spike lands

**Status:** Accepted
**Date:** 2026-08-06

## Context

The radio capability spike (`docs/radio-spike.md`, TODO item 3) has not run. It is
task zero of Phase 0 and gates [ADR-013](ADR-013-radio-profiles.md) and
[ADR-014](ADR-014-two-ssid-fallback.md) — both explicitly marked unverified. Until
it runs, whether the two-SSID design (multi-BSS, or the two-PSK fallback) actually
works on this hardware's `brcmfmac` driver is a guess, not a fact.

That guess was, in practice, the default: `LanConfig.lan_mode` defaulted to
`"two_bss"`, the *primary and least-proven* path in ADR-014. On real hardware this
produced no AP at all — no `Stable_critical`, no `Stable_besteffort`, nothing —
because the shipped default asked `hostapd` to do the one thing the spike exists to
verify, and never validated the sanctioned fallback path either.

A device that boots and serves nothing is worse than a device that boots and serves
one plain, unremarkable Wi-Fi network. The exit criterion for Phase 0 is that the
Stable SSIDs come up within ~40s on real hardware — that has to be true before
anything else about this product matters.

## Decision

**The default LAN configuration is now `lan_mode = "single"`: one SSID, one PSK, no
VLAN split, no multi-BSS, no dynamic-VLAN PSK file.** It is the configuration with
the fewest driver assumptions available — every Wi-Fi AP driver can serve one BSS
with one passphrase, so this is the one config guaranteed not to be the reason the
hotspot fails to come up.

Because there is only one LAN class in this mode, all LAN traffic is treated as
`BEST_EFFORT` (`policy.profiles_for_lan_mode("single")` returns `(BEST_EFFORT,)`
alone, never `CRITICAL`). This is the fail-safe direction: `BEST_EFFORT` cannot
trigger `BACKUP` (`may_use_backup=False`), so collapsing the two classes into one
cannot accidentally spend the user's money on a metered connection that only
best-effort traffic asked for. There is no "critical" fast lane in this mode — that
is the cost of simplicity, not an oversight.

WAN discovery is restricted to USB (`Kind.USB_TETHER`, `Kind.USB_ETHERNET`) by
default. `discovery.discover()` / `Discoverer` no longer scan for Wi-Fi WAN
networks unless a caller explicitly opts in (`include_wifi_wan=True`). The single
onboard radio's only job, by default, is broadcasting the hotspot; it is not shared
with a Wi-Fi station link. This sidesteps the AP-anchor tension ADR-011 and the
Wi-Fi discovery module already worried about (off-channel scans disturbing the AP)
by not scanning at all.

`hostapd_config()`'s `"two_bss"` and `"two_psk"` code paths are **not removed.**
They are exactly what ADR-013/ADR-014 describe, and the radio spike's job is to
determine which of them (if either) actually works — deleting them would mean
rewriting them from the findings instead of switching a default. `"single"` is a
third mode alongside them, not a replacement for the eventual two-class design.

## Consequences

**Easier:**

- The Phase 0 exit criterion ("AP comes up on real hardware") no longer depends on
  unverified multi-BSS or dynamic-VLAN behaviour.
- One less thing that can silently fail: no first-boot driver probe, no fallback
  selection logic, no two-passphrase user story to get right on day one.
- The user's mental model on first boot is what they already understand: one
  Wi-Fi network, one password.

**Harder / foreclosed, for now:**

- No `Stable_critical` / `Stable_besteffort` separation. Every scenario-test
  invariant about critical traffic being protected first is vacuous in this mode —
  there is only one class, and it is the unprotected one. This is intentional: see
  "must stay true" below.
- No Wi-Fi as a WAN source. A device whose only realistic connectivity is hotel
  Wi-Fi (the README's own example) cannot use it while `include_wifi_wan` stays
  off by default. USB tethering, USB Ethernet, and onboard Ethernet (where present)
  are the only WAN atomics discovered.
- This is explicitly an interim default, not a redesign. It must be revisited —
  and `lan_mode` switched back to whichever of `two_bss`/`two_psk` the spike
  validates — once `docs/radio-spike.md`'s findings land. This ADR does not
  supersede ADR-012, ADR-013, or ADR-014; it changes what ships *while they remain
  unverified*.

**Must stay true for this to keep making sense:** `profiles_for_lan_mode("single")`
must never include `CRITICAL`. If a future change adds a second class back onto a
single physical SSID without also updating this mapping, `BACKUP`'s
zero-bytes-at-rest invariant (ADR-006) is only one bad merge away from being
violated by ordinary best-effort traffic.

## Alternatives considered

**Fix the `two_bss` default's underlying driver problem directly.** Rejected: that
is precisely what the radio spike is for, and guessing at a fix without the
spike's findings is the same mistake that produced this bug in the first place.

**Default to `two_psk` instead of `single`.** Closer to the product's real shape
(genuine VLAN separation survives), but it is still unverified — ADR-014 only
calls it "sanctioned," not "measured working on this driver." `single` has no
open question at all: a plain single-BSS `hostapd` config is not something any
`nl80211` driver on this hardware could plausibly fail to serve.

**Leave Wi-Fi WAN discovery on.** Rejected for now because a radio that is both
serving the AP and scanning for WAN networks is the exact off-channel risk
`discovery/__init__.py` already throttles around (`DEFAULT_WIFI_SCAN_MIN_INTERVAL_S`)
— removing it entirely by default is a stronger, simpler guarantee than throttling
it while the AP's own reliability is still unproven. USB is a wired, off-radio
connection and carries no such risk.
