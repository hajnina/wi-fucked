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

## What it does

On the **WAN** side it discovers every usable connection — Wi-Fi networks, USB
phone tethering, USB Ethernet, cellular modems — and models each as an *atomic*
with one of three modes:

| Mode | Meaning |
|---|---|
| `NORMAL` | Part of the active connectivity pool. Use it freely. |
| `BACKUP` | Expensive. Preserve for emergencies. Should consume **zero bytes** at rest. |
| `UNUSED` | Known to exist, never used automatically. |

On the **LAN** side it presents two networks that never go away:

| SSID | For |
|---|---|
| `Stable_critical` | Meetings, SSH, VoIP. Continuity beats throughput. Protected first. |
| `Stable_besteffort` | Everything else. Absorbs degradation so critical doesn't have to. |

Between them sits a control loop that continuously measures real capacity,
estimates demand, weighs cost, and reprograms the kernel's traffic control to
match. When a WAN dies, sessions survive — they ride a tunnel that terminates at a
remote fabric, so the client-visible IP never changes.

## Status

**Phase 0 — Hello World.** The repository, image pipeline, release mechanics, and
module skeleton are real. The capacity engine, allocator, and enforcement layer are
interfaces with mock implementations. See [`docs/roadmap.md`](docs/roadmap.md).

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
