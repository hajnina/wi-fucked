# ADR-015 — Factory reset is triggered by boot count and resets WAN config only

**Status:** Accepted
**Date:** 2026-07-30

## Context

Users need a way to recover a device that is misconfigured badly enough that the
dashboard is unreachable — a WAN policy that broke routing, a bad captive-portal
interaction, a network the appliance keeps trying to join and failing.

The conventional mechanism is a recessed reset button. **The Pi Zero 2W has no
button**, and the bill of materials is fixed at one Pi Zero 2W, so adding one is
not available. GPIO is exposed, but a soldered button is extra hardware.

That leaves only signals a user can produce with the one control they have: the
power supply.

## Decision

**Factory reset is triggered by power-cycling the device three times within 60
seconds of boot.**

A small systemd unit runs early at boot, increments a counter in persistent
storage, and schedules it to be cleared after 60 seconds of successful uptime. Three
increments without a clear means the user has deliberately power-cycled three times
in quick succession, which does not happen by accident.

**The reset clears WAN configuration only:**

| Reset | Preserved |
|---|---|
| Known networks and their modes | **SSIDs and BSSID** ([ADR-012](ADR-012-immutable-ssid.md)) |
| Service-profile customisation | **LAN passphrases** |
| Learned capacity history | Device identity and fabric keys |
| Cost accounting | The installed firmware version |

The user never loses their LAN as a side effect of resetting their WANs. Every
client device stays joined.

## Consequences

**Easier:**

- Recovery from any WAN misconfiguration, with no hardware, no console, and no
  instructions beyond "unplug and replug three times".
- Because the LAN survives, the user reaches the dashboard immediately afterwards to
  reconfigure — the reset leads somewhere useful rather than to a blank device.
- Nothing to document about which button and how long to hold it.

**Harder:**

- **Discoverability is poor.** Nobody guesses this. It must be on the label, in the
  quick-start, and in the dashboard.
- A user power-cycling three times because the device seems stuck — plausible
  behaviour — triggers an unintended reset. Mitigated by the reset being cheap:
  they lose WAN configuration, not their network, and reconfiguring is a few taps.
- The counter must survive power loss but be cleared reliably after successful
  boot. A bug that fails to clear turns every third reboot into a reset.
- Counter writes hit the SD card on every boot. Small, but must not be chatty.

**Must stay true:** the clear-after-60-seconds path is reliable. This deserves an
explicit test, because the failure mode — resets that fire spontaneously — would be
severe and easy to miss during development, where devices are reflashed rather than
rebooted repeatedly.

## Alternatives considered

**GPIO button** — the obvious answer, unavailable under the fixed BOM. Should the
BOM ever gain an enclosure with a button, this ADR would be superseded; the
boot-count path would remain as a fallback.

**Reset via dashboard only** — useless in the case that matters, where the failure
prevents reaching the dashboard.

**Reset by joining a special SSID or entering a magic passphrase** — requires the
LAN to work, which it does, but is far less discoverable than a power cycle and
much stranger to explain.

**Full reset including LAN identity** — conventional, and wrong here for the reasons
in [ADR-012](ADR-012-immutable-ssid.md): it would cost the user every client device
in the house to fix an unrelated problem.
