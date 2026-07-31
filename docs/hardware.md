# Hardware envelope

The bill of materials is **one Raspberry Pi Zero 2W and nothing else**. Everything
in this document follows from that.

This is a real constraint with real consequences. Designing as though it weren't
would produce an architecture that quietly doesn't fit the hardware — the worst
kind of wrong, because it looks fine until integration.

## What the Pi Zero 2W is

| Property | Value | What it forces |
|---|---|---|
| SoC | RP3A0 — quad Cortex-A53 @ 1 GHz | Adequate for a control plane. Not for touching packets. |
| RAM | **512 MB** | Lean daemon. No in-memory time-series. No ML at runtime. |
| Radio | CYW43438 — **2.4 GHz only**, 802.11 b/g/n, single stream, single antenna | **One radio total.** AP and station contend for the same airtime and must share one channel. |
| USB | **One** micro-USB OTG data port | Phone tethering is the primary non-Wi-Fi WAN. |
| Ethernet | None | Only via a USB adapter the user supplies. |
| Storage | microSD | Write wear and power-loss corruption are the #1 field failure mode. |
| Input | No button | Factory reset must be software-only. |
| Output | One green ACT LED, GPIO-controlled | The only status channel on a headless device. |

## The consequence that shapes the product

**The onboard radio cannot be a station on two Wi-Fi networks at once.** The
vision's picture of Hotel Wi-Fi *and* Campsite Wi-Fi both `NORMAL` and both
carrying traffic simultaneously is not reachable on this BOM.

So, explicitly:

> **On the base BOM, WI-FUCKED delivers seamless, session-preserving *failover*
> between WANs — not bandwidth aggregation.**

This is the right trade, not a consolation prize:

- The product exists to rescue connections that are *bad* — 4 Mbps down,
  0.5 Mbps up, 700 ms RTT, 20% loss. Aggregating two bad links is worth less than
  making one bad link feel stable.
- What actually delivers the core promise — *failure manifests as degradation, not
  a broken network* — is the tunnel plus fast failover, not aggregation. Sessions
  survive because they terminate at the fabric, not because bytes were striped.
- Aggregation is a **"plug in a USB Wi-Fi dongle" upgrade**. Because atomics are
  modelled as N from day one, adding concurrency needs no redesign — only lifting
  [ADR-004](adr/ADR-004-failover-not-aggregation.md).

## Throughput envelope

State this plainly so nobody benchmarks the appliance against a Pi 5 and files a
bug.

| Path | Realistic ceiling |
|---|---|
| 2.4 GHz single-stream, good conditions | ~20–25 Mbps TCP |
| SHARED profile (AP + station share airtime) | roughly half of the above |
| WireGuard on the A53 | ~30–50 Mbps |
| **Design target** | **10–15 Mbps of genuinely stable throughput** |

The radio is the binding constraint, not the crypto. That is fine — the target
number sits comfortably above the links this appliance exists to rescue.

## Radio profiles

One radio, two jobs. See [ADR-013](adr/ADR-013-radio-profiles.md).

| Profile | Selected when | Behaviour |
|---|---|---|
| **ANCHOR** *(preferred)* | ≥1 USB WAN present — phone tether, USB Wi-Fi, USB Ethernet | Radio is **AP only**. Fixed channel. Never moves. Genuinely always-on. |
| **SHARED** *(fallback)* | Wi-Fi is the only available WAN | Radio runs AP + station concurrently on **one shared channel**. The AP follows the station. |

In SHARED, a WAN channel change is handled with **hostapd CSA (Channel Switch
Announcement)** so associated clients follow the move without losing association.
That is precisely the difference between "always available" and "available except
when it isn't."

Profile transitions are non-disruptive by construction: ANCHOR→SHARED may CSA
once; SHARED→ANCHOR requires no change at all.

## Known driver risks

These are unresolved until the Phase 0 spike ([`radio-spike.md`](radio-spike.md))
answers them on real hardware. **Do not build on a guess.**

| Risk | Impact if it fails | Mitigation |
|---|---|---|
| Multi-BSS on `brcmfmac` | Cannot serve two SSIDs from one radio | Fall back to one SSID + two PSKs with per-PSK VLAN assignment ([ADR-014](adr/ADR-014-two-ssid-fallback.md)) |
| AP+STA concurrency | SHARED profile impossible; USB WAN becomes mandatory | Ship ANCHOR-only and require a USB WAN, documented honestly |
| CSA under `brcmfmac` | AP channel moves drop clients | Pin the AP channel and restrict SHARED to same-channel WANs, or require USB WAN for off-channel networks |
| USB OTG power budget | Phone tethering unstable under load | Document a powered-hub recommendation; detect and surface undervoltage |

## SD card survival

An always-on appliance that writes telemetry continuously will destroy a cheap SD
card, and power loss during a write will corrupt the filesystem. Both are handled
architecturally rather than hoped away:

- Hot telemetry writes go to `tmpfs`, flushed to SQLite periodically
  ([ADR-010](adr/ADR-010-state-storage.md)).
- Ring-buffer retention — the database has a bounded size by construction.
- Log rotation sized against a flash-wear budget, not against disk free space.
- OTA writes to an inactive slot; the running system is never modified in place.
- SD health is surfaced in the dashboard *before* failure.

## Undervoltage

The Pi Zero 2W browns out under a marginal supply, and USB tethering draws through
the same rail. Undervoltage events are read from the SoC's throttling flags,
logged, and surfaced — a large fraction of "the appliance is flaky" reports in the
field will be a bad power supply, and the device should be able to say so itself.
