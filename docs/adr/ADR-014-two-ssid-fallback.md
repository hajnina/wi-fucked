# ADR-014 — Two SSIDs preferred; one SSID with two PSKs is the sanctioned fallback

**Status:** Accepted
**Date:** 2026-07-30
**Note:** The primary path depends on unverified driver behaviour. See
[`../radio-spike.md`](../radio-spike.md).

## Context

The user-facing contract is two networks with different service guarantees:

| SSID | For |
|---|---|
| `Stable_critical` | Meetings, SSH, VoIP. Protected first when capacity is scarce. |
| `Stable_besteffort` | Everything else. Absorbs degradation. |

Serving two SSIDs from one radio requires multiple BSS. The `brcmfmac` FullMAC
driver's support for this is limited and historically unreliable — it may work, it
may work only without a concurrent station, or it may not work at all on this chip.

That is a single point of failure sitting directly under the product's core
user-facing abstraction, and it will not be resolved until measured on hardware.
Building everything downstream on the assumption that it works would mean
discovering otherwise very late.

## Decision

**Primary:** two BSS via `hostapd bss=`, giving two genuine SSIDs.

**Sanctioned fallback:** one SSID with **two PSKs**, using `wpa_psk_file` with
per-PSK VLAN assignment. Different passphrase → different VLAN → different service
class.

The fallback is a first-class supported configuration, not a degraded emergency
mode. Everything above the LAN layer — classification, policy, allocation,
telemetry — sees VLANs, not SSIDs, so **the choice is invisible to the rest of the
system.**

First boot probes the driver and selects. The dashboard states which is in use and
gives the user the right joining instructions for it.

## Consequences

**Easier:**

- A driver limitation cannot block the product. The fallback needs no architectural
  change, only different `hostapd` configuration.
- Classifying on VLAN rather than SSID is the right design regardless — it is what
  `nftables` marks on, and it keeps `enforce/` independent of radio topology.
- The fallback user story is nearly as simple: *"use this password for work
  devices, this one for everything else."*

**Harder:**

- Two supported LAN configurations means two paths to test, and CI must cover both.
- The fallback's user experience is slightly worse: one network name, and a device
  is placed in a class by which password it was given — hard to change afterwards
  without forgetting the network.
- First-boot probing adds a step to the most fragile part of provisioning, and a
  wrong answer produces a device with no working LAN.
- Documentation and support must cover both.

**Must stay true:** per-PSK VLAN assignment works in the shipped `hostapd`. If
*both* mechanisms fail, the two-class contract cannot be delivered over Wi-Fi at
all and would need rethinking — the reason the spike is the first task in Phase 0.

## Alternatives considered

**Two SSIDs only, no fallback** — leaves the product's core abstraction hostage to
a driver quirk with no plan B.

**One SSID, classify by device MAC** — no second passphrase to explain, but the
user must enrol every device by MAC, contradicting "stupid simple by default", and
MAC randomisation breaks it silently.

**One SSID, classify by traffic inspection** — automatic, and appealing until you
consider that it means inspecting user traffic to guess intent. It would be wrong
often, unexplainable when wrong, and at odds with treating user traffic as
none of the appliance's business.
