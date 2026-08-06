# ADR-021 — The hotspot is open, unauthenticated, on first boot

**Status:** Accepted
**Date:** 2026-08-06

## Context

ADR-012 derives the passphrase from the Pi's serial and puts it nowhere except
`hostapd.conf` and a label card the provisioning script writes to
`/var/lib/wifucked/label.txt`, meant to be printed and stuck on the enclosure.

That assumes an enclosure and a label printer exist. On a bare Pi Zero 2W in
development — no screen, no buttons, no case, no label — there is no channel to
read that file before joining the network it describes: reading it requires
either a serial console, an Ethernet cable into a USB-OTG port, or pulling the SD
card, none of which is "the first thing before I set up anything else." A
device whose only way to prove you can reach it is a secret already stored on
it is unreachable by construction the first time.

## Decision

**In `lan_mode = "single"` (ADR-020's current default), `hostapd.conf` ships
with no WPA at all on first boot** — `LanConfig.open_on_first_boot` defaults to
`True`, and `hostapd_config(..., open_network=True)` omits `wpa`,
`wpa_key_mgmt`, `rsn_pairwise`, and `wpa_passphrase` entirely.

The derived passphrase from ADR-012 is still computed and still written to the
label (`firstboot.sh`), captioned as the value to hand-apply if the network is
later secured — restated in `hostapd.conf` and a `systemctl restart hostapd`.
Nothing does that automatically; there is no dashboard flow yet that flips the
network from open to secured. SSID and BSSID are unaffected and remain
immutable exactly as ADR-012 requires — this ADR changes only whether a
passphrase is enforced, not the identity story.

The two-class layouts (`two_bss`, `two_psk`) are unverified and not what ships
today (ADR-020); `open_network` has no effect on them and they keep requiring
their PSK.

## Consequences

**Easier:**

- A bare, headless Pi with no case, no label, and no console is reachable the
  moment it boots — matching the actual hardware this ships on today.

**Harder / foreclosed, for now:**

- The hotspot shares whatever WAN is configured with anyone in radio range
  until a human manually edits `hostapd.conf` and restarts `hostapd`. There is
  no automatic transition to secured and no expiry — an appliance left running
  past first setup is an open AP indefinitely unless someone acts on it.
- This is a real regression against ADR-012's own warning that "a weak
  derivation is a security problem baked into every unit" — an open network is
  weaker than a weak derivation. It is accepted here as a bootstrapping problem
  for hardware with no other channel in, not as a statement that the tradeoff
  is free.
- `user-journey.md` J1 is updated to describe this; a production unit with a
  printed label and an enclosure has no such bootstrapping problem and should
  revisit whether `open_on_first_boot` should default to `False` for that
  build, which this ADR does not decide.

**Must stay true:** SSID/BSSID immutability (ADR-012) and the derived
passphrase's availability on the label are unaffected — this only removes
enforcement of that passphrase at the radio, not its generation.

## Alternatives considered

**Ship a fixed, identical passphrase on every unit (e.g. `12345678`).**
Rejected outright: unlike an open network, which is visibly insecure, a fixed
shared secret looks secure and is not — every unit in the fleet shares one
password forever unless a user changes it, which is worse than no password at
all and exactly the "problem baked into every unit" ADR-012 warns against.

**Gate `open_on_first_boot` behind an explicit dev/`MOCK_HW` flag, secured by
default on real hardware.** Closer to ADR-012's intent, but the actual problem
— no channel to read a label on bare hardware — is not specific to
development; a shipped unit without a case has the identical problem. Left as
a config default (`LanConfig.open_on_first_boot`) rather than an environment
special-case so a real product build can override it deliberately once it has
an enclosure and a label to rely on, instead of the behaviour being invisible
outside `MOCK_HW`.

**Leave first boot secured and rely on the label.** The status quo, and the
thing this ADR replaces — correct once there is a physical label, actively
unreachable without one.
