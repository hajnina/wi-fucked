# ADR-013 — Two radio profiles: ANCHOR and SHARED

**Status:** Accepted
**Date:** 2026-07-30
**Note:** Rests on driver behaviour that is *expected* but not yet verified. See
[`../radio-spike.md`](../radio-spike.md); expect a superseding ADR once measured.

## Context

The Pi Zero 2W has one Wi-Fi radio (CYW43438, 2.4 GHz, single stream) that must
serve two jobs: run the LAN access point, and — when Wi-Fi is the only WAN — act as
a station on someone else's network.

The `brcmfmac` driver supports AP+STA concurrency, but with a hard constraint:
**both interfaces must be on the same channel.** A single radio cannot listen on
two channels at once.

That creates a direct conflict with [ADR-012](ADR-012-immutable-ssid.md) and the
always-available promise. If the AP is pinned to channel 6 and the hotel Wi-Fi is
on channel 11, one of them has to move. Pinning the AP means most WANs are
unusable; moving the AP means clients disconnect — unless the move is announced.

## Decision

**Two profiles, selected automatically by what WAN hardware is present.**

| Profile | Selected when | Radio behaviour |
|---|---|---|
| **ANCHOR** *(preferred)* | ≥1 USB WAN present — phone tether, USB Wi-Fi, USB Ethernet | Radio is **AP only**. Fixed channel. Never moves. |
| **SHARED** *(fallback)* | Wi-Fi is the only available WAN | AP + station concurrently on **one shared channel**; the AP follows the station. |

In SHARED, channel moves use **hostapd CSA (Channel Switch Announcement)** so
associated clients follow the move without re-associating. CSA is what makes the
difference between "always available" and "available except when it isn't".

Profile transitions are non-disruptive by construction: ANCHOR→SHARED may CSA once;
SHARED→ANCHOR requires no change.

The daemon selects the profile; `hostapd` executes the move
([ADR-011](ADR-011-ap-is-the-anchor.md) — the daemon requests, it does not own).

## Consequences

**Easier:**

- One radio serves both roles without dropping clients, on the base BOM.
- Plugging in any USB WAN automatically upgrades the user to a fixed, never-moving
  AP channel — the best available behaviour, with no configuration.
- The profile is an explicit, observable state the dashboard can explain.

**Harder:**

- **SHARED halves throughput.** AP and station share airtime on one radio, so the
  ~20–25 Mbps ceiling becomes ~10–12 Mbps. The dashboard must say so rather than
  leave the user wondering.
- CSA is disruptive for a few hundred milliseconds even when it works, and some
  client implementations handle it poorly. Every move is logged with client counts
  before and after ([SOP-004](../sop/SOP-004-logging-and-observability.md)).
- Two profiles double the state space for anything touching the radio, and the
  transition between them is the highest-risk moment in the system.
- Channel selection in SHARED is dictated by the WAN, so the AP can be forced onto
  a congested channel.

**Must stay true — and is not yet verified:**

- `brcmfmac` supports AP+STA concurrency on this chip.
- CSA works on this driver.
- Two BSS can run alongside a station ([ADR-014](ADR-014-two-ssid-fallback.md)).

If AP+STA concurrency does not work, SHARED is impossible and a USB WAN becomes
mandatory — a significant product change requiring a superseding ADR.

## Alternatives considered

**Pin the AP channel; only use same-channel WANs** — keeps the AP perfectly stable
but makes most real networks unusable. Unacceptable for a product whose job is
using whatever Wi-Fi is available.

**AP-only always; require a USB WAN** — genuinely the most robust option, and what
ANCHOR is. Rejected as the *only* mode because it makes a phone or dongle mandatory,
which the fixed BOM does not include.

**Move the AP without CSA** — clients disconnect and re-associate on every WAN
change. Directly contradicts the always-available promise.
