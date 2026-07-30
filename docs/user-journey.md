# User journey

From unboxing to long-term maintenance. Every step here is a product requirement,
not an illustration — if the implementation makes one of these worse, that is a
regression.

The user's mental model must stay: *I have some Internet connections. DIRTY makes
them work.*

---

## J1 — Unbox and first boot

Flash the image, or receive a pre-flashed card. Apply power.

Within roughly 40 seconds, both networks are live:

```
Stable_critical
Stable_besteffort
```

**There is no separate "setup" SSID.** The network the user joins on day one is the
network they keep forever ([ADR-012](adr/ADR-012-immutable-ssid.md)). This matters
more than it looks: a setup-then-rename flow means every device in the house has to
be re-joined the moment configuration finishes, which is exactly the experience the
product exists to avoid.

The default passphrase is derived from the Pi's serial and printed on the label.

Joining `Stable_besteffort` with no WAN configured raises a captive portal.

**What the user sees:** a network appeared, they joined it, and a page opened by
itself. They have not read anything yet.

---

## J2 — First WAN

The portal shows what the appliance has discovered. Two paths:

### Plug a phone into USB

```
New connection discovered

  Martin's Phone
  USB tethering

  [ NORMAL ]  [ BACKUP ]  [ UNUSED ]
```

One tap. `BACKUP` for a metered phone is the common case and should read as the
obvious choice for a phone.

### Pick a Wi-Fi network

A scanned list, a password field, connect. In SHARED profile the AP moves to that
network's channel via CSA, and associated clients follow without noticing
([ADR-013](adr/ADR-013-radio-profiles.md)).

### Then it gets out of the way

```
Hotel WiFi        connected · measuring…
Hotel WiFi        14 Mbps ↓  ·  3 Mbps ↑  ·  38 ms
```

Capacity appears once there is real traffic to learn from
([ADR-003](adr/ADR-003-passive-capacity-estimation.md)) — the portal says
"measuring", not a fabricated number.

**What the user sees:** they told it about one connection, and it started working.

---

## J3 — Daily use

Devices join `Stable_besteffort` by default. The work laptop and the VoIP handset
join `Stable_critical`.

The user does nothing. WANs come and go. The appliance rebalances.

The dashboard lives at two stable addresses, both of which always work:

```
http://dirty.local      (mDNS)
http://10.44.0.1        (always, even when mDNS doesn't)
```

**What the user sees:** nothing. This is the goal state, and most of the product's
value is measured in how long a user stays here.

---

## J4 — Travel and environment change

The van drives. The environment changes completely.

- **Previously-seen networks reconnect automatically**, identified by stable
  properties rather than interface names
  ([ADR-002](adr/ADR-002-atomic-identity.md)). The campsite Wi-Fi from last month
  is recognised, and its previous mode and learned history are restored.
- **New networks appear as `UNUSED`.** Discovery does not imply permission — the
  appliance never auto-joins something it has not been told to use.
- Promotion to `NORMAL` or `BACKUP` is one tap.

New discoveries surface in the dashboard, and once via captive-portal interception
on the next HTTP request. **Once.** An appliance that nags every time it sees a new
SSID in a café is an appliance that gets unplugged.

**What the user sees:** they arrived somewhere new and the Internet already worked,
or they tapped once to approve a network.

---

## J5 — Something's wrong

### The dashboard explains itself

Straight from the decision journal ([ADR-009](adr/ADR-009-decision-records.md)) —
these are recorded facts from the moment of decision, not a reconstruction:

```
BACKUP ACTIVE

Reason:              NORMAL WAN degradation
Observed:            RTT 820 ms · loss 17% · capacity 1.4 Mbps
Critical demand:     3.1 Mbps
Action:              BACKUP activated
Best-effort traffic: restricted
Data used:           37 MB
```

And when nothing is wrong, it says so with equal specificity — which is what a user
worried about their data allowance actually wants to see:

```
BACKUP

Status:      Not used
Data today:  0 MB
Reason:      NORMAL WAN healthy
```

### The LED

The only status channel on a headless device with no screen. The onboard ACT LED,
driven via `/sys/class/leds`:

| Pattern | Meaning |
|---|---|
| Solid | Healthy WAN, everything nominal |
| Slow blink | Degraded — working, but the dashboard has something to say |
| Fast blink | No WAN at all |
| SOS | Daemon down. The AP is still up; the dashboard still loads. |

### Factory reset, with no button

Power-cycle three times within 60 seconds of boot
([ADR-015](adr/ADR-015-boot-count-factory-reset.md)).

This resets **WAN configuration only**. SSIDs, BSSID, and LAN passphrases survive,
so no client device is disturbed and the user lands back on a working dashboard to
reconfigure. Resetting your WANs must never cost you your LAN.

---

## J6 — Updates

### The common case is invisible

**Control-plane-only updates restart the daemon without touching `hostapd` or
`dnsmasq`. The AP does not drop.** Clients keep their association and their leases
throughout ([ADR-011](adr/ADR-011-ap-is-the-anchor.md)).

The user experiences nothing at all.

### When a reboot is genuinely needed

- Applied to the inactive slot; the running system is never modified in place.
- Scheduled for a quiet window, and never during active critical traffic.
- Roughly 30 seconds of downtime.
- The watchdog validates after boot. Unhealthy → automatic rollback to the previous
  slot.

Because every release is immutable and permanent
([ADR-016](adr/ADR-016-versioning.md)), rolling back to a specific known-good
version is always possible.

**What the user sees:** usually nothing. Occasionally a brief reboot at a time they
were not using it.

---

## J7 — Long-term maintenance

The appliance is expected to run for years, unattended, on a consumable SD card.

- **Telemetry retention is a ring buffer** — bounded size by construction, rolled
  up into coarser buckets as it ages ([ADR-010](adr/ADR-010-state-storage.md)). The
  device cannot fill its own card.
- **SD health is surfaced before failure**, not after. A card showing errors gets a
  dashboard warning while the user still has a working device to read it on.
- **Undervoltage is detected and reported.** A large share of "it's being flaky"
  reports are a marginal power supply, and the device should be able to say so
  itself rather than leaving the user to guess.
- **One-click diagnostics bundle** for support — logs, decision journal, telemetry
  rollups, kernel network state. **No credentials, no payload data**, so it is safe
  to send to anyone.

**What the user sees:** it keeps working. When it eventually cannot, it tells them
which part is failing, in time to do something about it.

---

## The measure of success

The user stops thinking about their Internet connection.

They can plug in Wi-Fi, plug in a phone, drive somewhere, lose Wi-Fi, gain Wi-Fi,
change cellular towers, saturate their upload, and lose a fabric server — while
their devices continue to experience one coherent network.

Ideally they notice nothing but a brief increase in latency.
