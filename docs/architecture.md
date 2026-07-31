# Architecture

## The split that everything else follows

> **Control plane is Python. Data plane is the kernel. No packet is ever touched
> by Python.**

The `wifucked` daemon observes the network, decides what should happen, and *programs*
`tc`/CAKE, `nftables`, and policy routing to make it happen. Forwarding, shaping,
and queueing occur entirely in kernel space.

Two properties fall out of this, and both are load-bearing:

1. **Throughput does not depend on the control language.** A Python daemon on a
   1 GHz A53 could not forward 15 Mbps and also think. It doesn't have to.
2. **The daemon can die without taking the network down.** Kernel rules persist.
   See [ADR-008](adr/ADR-008-fail-to-last-known-good.md).

## Two worlds

```
   WAN — expected to be chaotic          LAN — deliberately boring

   Wi-Fi hotel ──┐                              Stable_critical
   Wi-Fi camp  ──┤                              Stable_besteffort
   USB phone   ──┤                                    │
   USB eth     ──┤                            ┌───────┼───────┐
   Cellular    ──┘                            │       │       │
        │                                   laptop  phone    TV
        ▼
      WI-FUCKED ──────── stable tunnel ──────── fabric ──── Internet
```

Client devices never learn which WAN is in use, never need special software, and
never reconnect because a WAN changed.

## Control loops

Three loops at different cadences over one shared state store.

| Loop | Period | Responsibility |
|---|---|---|
| **Fast** | ~1 s | Liveness, failover, hysteresis timers, enforcement reconciliation |
| **Medium** | ~10 s | Capacity re-estimation, demand measurement, allocation decisions |
| **Slow** | ~5 min | History compaction, per-network learning, telemetry rollup |

Splitting them matters: failover must be fast, but re-deciding allocation every
second would flap ([ADR-006](adr/ADR-006-backup-liveness-budget.md) and the
hysteresis machine in `allocator/`), and learning is expensive enough that it must
not sit in the hot path.

## Modules

```
wifucked/
├── atomics/     Atomic model · NORMAL/BACKUP/UNUSED · stable identity · persistence
├── discovery/   wifi · USB tether · USB ethernet · ModemManager → atomics
├── probe/       RTT · jitter · loss · capacity estimation · bufferbloat detection
├── capacity/    per-atomic, per-direction model (EWMA + confidence + history)
├── demand/      per-service-class demand, measured separately up and down
├── policy/      service profiles · priority ladder · cost policy
├── allocator/   THE controller: capacity + demand + cost → allocation. Hysteresis.
├── enforce/     tc/CAKE · nftables marks · policy routing (one table per atomic)
├── radio/       profile selection (ANCHOR/SHARED) · CSA orchestration · hostapd config
├── tunnel/      WireGuard management · fabric health · path migration
├── lan/         AP configuration · DHCP · DNS · captive portal
├── telemetry/   SQLite time-series · event log · decision journal
├── api/  ui/    REST + airgapped dashboard
└── hal/         hardware abstraction; MOCK_HW=1 swaps in fakes
```

### The dependency direction

```
discovery ──> atomics <── policy
                 │
                 ▼
   probe ──> capacity ──┐
                        ├──> allocator ──> enforce ──> kernel
   demand ──────────────┘        │
                                 ├──> telemetry (decision records)
   radio ──> lan                 │
   tunnel <──────────────────────┘
```

`atomics/` is the centre. Nothing below it may import from above it. `enforce/` is
the only module permitted to shell out to `tc`, `nft`, or `ip`.

## Atomic identity

**This is the load-bearing abstraction of the whole system.**

An atomic is one independently usable Internet connection. Its identity must
survive:

- `wlan1` → `wlan2` renumbering across reboots
- USB re-enumeration when a phone is unplugged and replugged
- DHCP lease changes, gateway changes, IP changes
- the connection disappearing for a week and coming back

So identity derives from stable properties — SSID + BSSID prefix, USB
vendor/product/serial, MAC, modem IMEI — and **never** from a kernel interface
name. The plug-and-play promise ("unplug the phone, plug it back in, it just
works, no reconfiguration") depends entirely on this holding.

See [ADR-002](adr/ADR-002-atomic-identity.md).

## Modes

| Mode | Contract |
|---|---|
| `NORMAL` | Part of the active pool. The allocator uses it freely. |
| `BACKUP` | Expensive. **Zero bytes at rest**, beyond a small accounted liveness budget. Activates only when critical demand genuinely cannot be met. |
| `UNUSED` | Discovered, never used automatically. Discovery does not imply permission. |

`BACKUP` is paid insurance. Minimising its use is an optimisation objective in its
own right, not a side effect — the system measures bytes, activation duration,
activation reason, responsible traffic class, and the degradation that justified
it, and shows all of it.

## Service profiles

| Profile | Priority | May use BACKUP |
|---|---|---|
| `Stable_critical` | 1 | Yes |
| `Stable_besteffort` | 2 | **Never** |

Priority does not mean critical gets all bandwidth. It means that when resources
become constrained, critical is protected first and best-effort absorbs the
degradation. Best-effort must never be able to force paid connectivity merely by
wanting more bandwidth.

## The stable tunnel

Client sessions terminate at a remote fabric server, not at the WAN. This is what
makes a WAN swap survivable: the client-visible IP never changes, so TCP
connections and long-lived sessions ride through the transition.

Without it, multipath means NAT changes and dead sessions — which is why the
fabric is MVP scope rather than a later addition
([ADR-005](adr/ADR-005-tunnel-is-mandatory.md)).

WANs are treated as hostile — public Wi-Fi especially. The tunnel is the security
boundary between the wifucked Internet and the balanced LAN, and LAN services are
never exposed through an arbitrary WAN.

## Enforcement

`enforce/` renders the allocator's decision into kernel state:

- **CAKE** per WAN egress, with bandwidth set from the capacity model and
  `diffserv` mapping for the two service classes
- **nftables** marks classifying traffic by originating VLAN / SSID into service
  classes
- **policy routing** — one routing table per atomic, `ip rule` selecting by mark

It works by **reconciliation, not command**: desired state is declared, the fast
loop diffs actual kernel state against it and repairs the difference. That makes
the system self-healing after a crash, after manual `tc` fiddling, and across
interface churn. See [ADR-007](adr/ADR-007-reconciliation.md).

## Always-available LAN

The AP is the anchor. `hostapd` and `dnsmasq` are independent systemd units with
no dependency on `wifucked.service`. If the daemon crashes, is being updated, or is
wedged, the AP keeps serving and clients keep their leases — and because kernel
rules persist, Internet keeps working too.

SSIDs and BSSID are derived from the Pi serial at first boot and never change.
Only the channel moves, and only via CSA.

See [ADR-011](adr/ADR-011-ap-is-the-anchor.md),
[ADR-012](adr/ADR-012-immutable-ssid.md),
[ADR-013](adr/ADR-013-radio-profiles.md).

## Explainability

Every allocation change writes a structured **decision record** — inputs,
thresholds, chosen action, reason — to `telemetry.decisions`. The dashboard renders
these directly:

```
BACKUP ACTIVE

Reason:              NORMAL WAN degradation
Observed:            RTT 820 ms · loss 17% · capacity 1.4 Mbps
Critical demand:     3.1 Mbps
Action:              BACKUP activated
Best-effort traffic: restricted
```

This is an architectural constraint, not a UI feature — retrofitting it means
reconstructing intent that was never recorded. See
[ADR-009](adr/ADR-009-decision-records.md).

## State

- **Current state** — in memory. Rebuilt from discovery on start.
- **Configuration** — JSON on disk, small, human-readable, hand-editable.
- **Telemetry and history** — SQLite in WAL mode, hot writes buffered in `tmpfs`
  and flushed periodically, with ring-buffer retention so the database has a
  bounded size by construction.

See [ADR-010](adr/ADR-010-state-storage.md).

## Hardware abstraction

`hal/` provides mockable interfaces for everything that touches real hardware or
privileged system state — radio, netlink, USB enumeration, LED, system facts.
`MOCK_HW=1` swaps in fakes so the entire control loop runs on a laptop.

This is the primary development path, not a testing convenience. A module that can
only be exercised on a Pi cannot be iterated on, and will not get the scenario
coverage the control logic needs.
