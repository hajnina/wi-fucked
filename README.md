# Wi-Fucked → BALANCED

> Is you wifi fucked? **DEAL WITH IT.**

An autonomous connectivity appliance for a Raspberry Pi Zero 2W. Plug in whatever
Internet you have — hotel Wi-Fi, a phone on USB, a campsite hotspot — and WI-FUCKED
figures out how to make it feel stable.

```
      crappy Wi-Fi   phone   cellular
           │           │        │
           ▼           ▼        ▼
        unreliable / changing / expensive
                       │
                    WI-FUCKED          "I'll figure this out."
                       │
                    BALANCED
                       │
                 USER'S DEVICES     "It just works."
```

The Internet underneath may be chaotic. The user's network should not be.

---

## What it's meant to do

On the **WAN** side it discovers every usable connection — Wi-Fi networks, USB
phone tethering, USB Ethernet, cellular modems — and models each as an *atomic*
with one of three modes:

| Mode | Meaning |
|---|---|
| `NORMAL` | Part of the active connectivity pool. Use it freely. |
| `BACKUP` | Expensive. Preserve for emergencies. Should consume **zero bytes** at rest. |
| `UNUSED` | Known to exist, never used automatically. |

On the **LAN** side the target design is two networks that never go away —
`Stable_critical` for things that must not drop, `Stable_besteffort` for
everything else — with a control loop that measures real capacity, estimates
demand, weighs cost, and reprograms the kernel's traffic control to match. When a
WAN dies, sessions survive over a tunnel to a remote fabric, so the client-visible
IP never changes. That target design is described in full in
[`docs/architecture.md`](docs/architecture.md) and
[`docs/user-journey.md`](docs/user-journey.md).

**What currently ships is a deliberately smaller subset of that** — see below.

## Status

**Phase 0 — Hello World**, still in progress. See
[`docs/roadmap.md`](docs/roadmap.md) for the full phase plan
(Hello World → MVP → Beta → Production) and its exit criteria.

### Upon flashing the SD card, you currently can:

- Boot a Raspberry Pi Zero 2W into a working `wifucked` daemon that logs, serves
  a dashboard, and survives being killed and restarted (ADR-008).
- Get **one plain Wi-Fi hotspot** — a single SSID and passphrase, derived from
  the Pi's serial and printed on the device label, immutable after first boot
  (ADR-012). Not the two-SSID `Stable_critical`/`Stable_besteffort` split the
  target design describes yet — see
  [ADR-020](docs/adr/ADR-020-interim-single-hotspot.md) for why.
- Plug in a **USB phone (tethering) or a USB Ethernet adapter** and have it
  discovered as WAN automatically. Wi-Fi is *not* currently used as a WAN
  source by default — the onboard radio's only job right now is broadcasting
  the hotspot ([ADR-020](docs/adr/ADR-020-interim-single-hotspot.md)).
- Run the entire control loop — discovery, capacity estimation, allocation,
  enforcement — with `MOCK_HW=1` on a laptop, no Pi required.
- Take an OTA update from a previous release and roll back automatically if a
  health check fails.

### What is not yet implemented

- **Two-class LAN (`Stable_critical` / `Stable_besteffort`).** The allocator,
  enforcement, and demand-accounting logic for it exists and is unit- and
  scenario-tested under `MOCK_HW=1`, but it is not what ships by default — see
  [ADR-020](docs/adr/ADR-020-interim-single-hotspot.md). It is blocked on **the
  radio capability spike** (task zero of Phase 0,
  [`docs/radio-spike.md`](docs/radio-spike.md)): whether this hardware's Wi-Fi
  driver can actually serve two SSIDs (or the two-PSK fallback) has not been
  measured. [ADR-013](docs/adr/ADR-013-radio-profiles.md) and
  [ADR-014](docs/adr/ADR-014-two-ssid-fallback.md) are marked unverified until
  it runs.
- **Wi-Fi as a WAN source**, and the AP+STA channel-sharing (CSA) logic that
  goes with it — implemented, but unconfirmed on real hardware and off by
  default (`docs/active-tests.md`).
- **Cellular modems via ModemManager.**
- **Passive capacity estimation, bufferbloat detection, and the allocator's
  hysteresis/cost logic against real traffic** — these run against mock and
  scripted hardware in tests, not yet validated on a Pi under real WAN churn.
- **Multi-server fabric, historical learning, cost budgets/alerts, staged OTA
  rollout, a field trial** — all Phase 2+, not started.

None of this has been confirmed against a live device — see
[`docs/active-tests.md`](docs/active-tests.md) for what's merged and running
but not yet watched work on real hardware.

## Quick start (no hardware)

```bash
pip install -r appliance/requirements.txt
MOCK_HW=1 PYTHONPATH=appliance/src python3 -m wifucked
```

Dashboard on <http://localhost:8080>. The mock HAL presents three fake atomics so
the whole control loop runs on a laptop.

```bash
./run_all_tests.sh          # everything, MOCK_HW=1
```

## Repository layout

| Path | Contents |
|---|---|
| `appliance/` | What runs on the Pi — the `wifucked` daemon, provisioning, systemd units |
| `fabric/` | The remote tunnel endpoint, shipped as a container |
| `scripts/` | Version calculation, update-package builder, manifest generation |
| `docs/` | Architecture, ADRs, roadmap, hardware envelope, user journey |
| `.github/` | Image bake pipeline |

## Documentation

Start here, in this order:

1. [`docs/architecture.md`](docs/architecture.md) — how the system is put together
2. [`docs/hardware.md`](docs/hardware.md) — what a Pi Zero 2W can and cannot do, and what that costs
3. [`docs/user-journey.md`](docs/user-journey.md) — first boot to long-term maintenance
4. [`docs/roadmap.md`](docs/roadmap.md) — Hello World → MVP → Beta → Production
5. [`docs/adr/`](docs/adr/) — the decisions, and why they're load-bearing
6. [`docs/contributing.md`](docs/contributing.md) — commit convention, review bar, ADR process
7. [`docs/versioning.md`](docs/versioning.md) — how versions and releases work

`CLAUDE.md` carries the rules that must not be broken. Read it before writing code.

## Releases

One channel. Every push to `main` produces one immutable release, versioned from
conventional commits. Latest image and update package:

```
https://github.com/hajnina/wi-fucked/releases/latest
```

## Licence

Not yet chosen — see `docs/roadmap.md`, Phase 3.
