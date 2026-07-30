# ADR-002 — Atomic identity derives from stable properties, never interface names

**Status:** Accepted
**Date:** 2026-07-30

## Context

The product promises plug-and-play: unplug the phone, plug it back in, and it is
recognised, reconnected, and its previous policy restored — with no
reconfiguration. The same promise applies to a Wi-Fi network the device saw last
month at a campsite.

Linux interface names do not support that promise. `wlan1` becomes `wlan2` after a
reboot with a different USB enumeration order. `usb0` is assigned in plug order, so
two phones swap identities depending on which was connected first. An interface
that disappears and returns may come back under a different name entirely.

Any state keyed on `ifname` — remembered mode, learned capacity history, cost
accounting — silently attaches to the wrong connection when names shift. The
failure is invisible: nothing errors, the device just starts treating the metered
phone as the free hotel Wi-Fi.

## Decision

**An atomic's identity derives from stable properties of the connection itself,
never from a kernel interface name.**

| Atomic type | Identity derived from |
|---|---|
| Wi-Fi | SSID + BSSID OUI prefix |
| USB tether | USB vendor ID + product ID + serial |
| USB Ethernet | MAC address |
| Cellular modem | IMEI |

An `ifname` is a *current fact about* an atomic, valid only for as long as you hold
it. Read it at the moment of use; never persist it, never compare it, never key on
it.

## Consequences

**Easier:**

- Plug-and-play works as promised. Recognition across replug, reboot, and
  re-enumeration is automatic rather than best-effort.
- Historical learning is meaningful — capacity history for "campsite Wi-Fi"
  attaches to that network, not to whichever interface happened to hold it.
- Cost accounting is trustworthy. Bytes charged to a metered atomic really were
  its bytes.

**Harder:**

- Identity derivation has edge cases that must be handled deliberately: BSSID
  changes on the same SSID (roaming between APs of one network), USB devices with
  no serial, MAC randomisation. Each needs a documented rule rather than an
  accident.
- Every module must carry atomic IDs rather than interface names, which is more
  verbose at call sites and requires a lookup at the point of kernel interaction.

**Must stay true:** the chosen properties remain stable in practice. USB devices
without serials are the known weak point — they fall back to
vendor+product+port-path, which is stable only if the user uses the same port.

## Alternatives considered

**Key on `ifname`** — rejected; it is the bug this ADR exists to prevent.

**Persistent udev rules pinning names** — pins names for known devices but does
nothing for a Wi-Fi network, which has no device at all. Solves a fraction of the
problem and adds system-level state that must itself be managed.

**Let the user name every connection** — contradicts "discover, don't configure",
and does not help on the first sighting, which is exactly when the system must
already behave correctly.
