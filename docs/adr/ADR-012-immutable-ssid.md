# ADR-012 — SSID and BSSID are immutable after first boot

**Status:** Accepted
**Date:** 2026-07-30

## Context

Client devices remember Wi-Fi networks by SSID and BSSID. Change either and every
phone, laptop, TV, and thermostat in the household treats it as a *different*
network: they will not auto-join, they prompt for credentials again, and anything
without a UI simply falls off and does not come back.

For an appliance whose central promise is a network that never goes away, an SSID
change is indistinguishable from a total failure — arguably worse, because it
requires the user to intervene on every device they own.

Several plausible-looking features would change these values: regenerating
identifiers on reconfiguration, deriving the SSID from the active WAN, resetting
identity on factory reset, letting the daemon pick a BSSID at startup. Each is
individually reasonable and collectively fatal.

## Decision

**SSID and BSSID are derived from the Pi's serial number at first boot and never
change.**

- Generated once, on first boot, by the provisioning script — not by the daemon.
- Stored in `hostapd.conf` and never regenerated.
- **Factory reset does not change them.** It resets WAN configuration only
  ([ADR-015](ADR-015-boot-count-factory-reset.md)); the user never loses their LAN
  as a side effect of resetting their WANs.
- OTA updates never rewrite them.
- **Only the channel may change**, and only via CSA
  ([ADR-013](ADR-013-radio-profiles.md)), which associated clients follow without
  re-joining.

The user may rename the SSID deliberately, through an explicit action that warns
them every device will need to re-join. That is a choice they make, not something
the system does to them.

## Consequences

**Easier:**

- Clients join once and stay joined for the life of the device.
- Headless devices — thermostats, cameras, printers — keep working across every
  reconfiguration, update, and reset.
- Support becomes simpler: "did the network name change?" is answerable with a flat
  no.

**Harder:**

- Two appliances in radio range with the same serial-derivation collision would
  clash. The derivation must include enough entropy; the serial provides it, but
  the derivation function must not truncate it away.
- The default passphrase is derived and printed on the label, so it must be
  generated with real entropy — a weak derivation is a security problem baked into
  every unit.
- Any future feature that wants a dynamic SSID — a guest network, a per-WAN name —
  needs a superseding ADR rather than an implementation.

**Must stay true:** first-boot generation is reliable and runs exactly once. A bug
that regenerates on every boot would be catastrophic and near-invisible in testing,
where devices are reflashed constantly. This deserves an explicit test.

## Alternatives considered

**Regenerate on factory reset** — the conventional behaviour, and wrong here. A
user resetting their WAN configuration because a hotel network misbehaved would
lose every client device in the house. The two concerns are unrelated and must not
be coupled.

**Derive the SSID from the active WAN** — surfaces information the user does not
need in the one place that must never change.

**Let the daemon generate identity at startup** — makes the LAN depend on the
daemon, contradicting [ADR-011](ADR-011-ap-is-the-anchor.md), and risks
regeneration on any startup-path bug.

**Fixed SSID across all units** — trivially simple, but neighbouring appliances
would collide and clients would roam between strangers' devices.
