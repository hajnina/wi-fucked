# SOP-002 — Writing code

## The rule that outranks the others

**No packet is ever touched by Python.** The daemon programs the kernel; it is
never in the data path. If you are writing a socket that relays user traffic, a
loop over client connections, or anything that reads and re-sends payload bytes,
stop — that belongs in the kernel as a rule the daemon installs
([ADR-001](../adr/ADR-001-control-plane-data-plane.md)).

## Module boundaries

Respect the dependency direction. `atomics/` is the centre; nothing below it may
import from above it.

```
discovery ──> atomics <── policy
                 │
                 ▼
   probe ──> capacity ──┐
                        ├──> allocator ──> enforce ──> kernel
   demand ──────────────┘        │
                                 ├──> telemetry
   radio ──> lan                 │
   tunnel <──────────────────────┘
```

**`enforce/` is the only module permitted to shell out to `tc`, `nft`, or `ip`.**
If another module needs kernel state changed, it expresses that as desired state
and lets `enforce/` reconcile it. A stray `subprocess.run(["ip", ...])` in
`allocator/` breaks the reconciliation model and will be sent back.

## Identity

Never store or compare a kernel interface name as identity. `wlan1` becomes
`wlan2`; `usb0` moves. Use the atomic's stable ID
([ADR-002](../adr/ADR-002-atomic-identity.md)).

```python
# Wrong — breaks the moment USB re-enumerates
if atomic.ifname == self.active_ifname: ...
seen_networks[iface.name] = capacity

# Right
if atomic.id == self.active_atomic_id: ...
seen_networks[atomic.id] = capacity
```

An `ifname` is a *current fact about* an atomic, valid only for as long as you
hold it. Read it at the moment of use; never persist it.

## Never tear down on the way out

The daemon dying must not take the network with it
([ADR-008](../adr/ADR-008-fail-to-last-known-good.md)).

```python
# Wrong — a control-plane crash becomes a user-facing outage
atexit.register(lambda: subprocess.run(["nft", "flush", "ruleset"]))

def shutdown(self):
    self.enforcer.clear_all_qdiscs()
```

There is no cleanup path that removes qdiscs, flushes nftables, or drops routes.
State installed in the kernel outlives the process that installed it, deliberately.
On restart the daemon reconciles — it does not start from a blank slate.

## Hardware access goes through the HAL

Anything touching a radio, netlink, USB enumeration, the LED, or privileged system
facts goes through `hal/`, so `MOCK_HW=1` can replace it.

```python
from wifucked.hal import get_hal

hal = get_hal()          # real or mock, decided by MOCK_HW
scan = hal.wifi.scan()
```

Adding a hardware capability means adding it to the HAL interface *and* the mock,
in the same commit. A mock that lags the interface silently disables testing for
everyone else.

## Typing

Public functions and all module boundaries are annotated. Internal helpers may be
loose where it genuinely aids readability. `ruff` enforces the floor; taste covers
the rest.

Prefer explicit dataclasses over dictionaries for anything crossing a module
boundary. A `dict` with implied keys is a schema nobody can find.

## Units — name them

Bandwidth, latency, and byte counts are the most common source of silent
factor-of-1000 bugs in this kind of system. Put the unit in the name.

```python
capacity_bps      not  capacity
rtt_ms            not  rtt
consumed_bytes    not  consumed
duration_ms       not  duration
```

Internally: bits per second for capacity, milliseconds for latency, bytes for
volume. Convert at the display edge, never in the middle.

## Failure handling

Fail towards a working network. When a component cannot do its job:

1. Log it with `reason` and `exc_info=True` ([SOP-004](SOP-004-logging-and-observability.md)).
2. Fall back to the last known-good behaviour, not to a blank slate.
3. Surface it in telemetry so the dashboard can explain the degradation.
4. Keep running. A crashed daemon is worse than a degraded one.

Bare `except: pass` is never acceptable. `except Exception` with a logged reason
and a deliberate fallback is often exactly right.

## Configuration

New configuration keys need a default that makes the appliance work without them.
The device must boot and serve its SSIDs with an empty config file — a first boot
has no configuration, and neither does a factory reset.

## Comments

Comment *why*, not *what*. If you are about to explain why the architecture is
shaped a certain way, that is an ADR — write it there and link to it
([SOP-007](SOP-007-architectural-decisions.md)).

Match the density of the surrounding code. This codebase is not heavily commented;
matching it is better than being unilaterally more thorough.

## Before you call it done

```bash
ruff check appliance/src fabric/src
ruff format --check appliance/src fabric/src
shellcheck appliance/*.sh scripts/*.sh
MOCK_HW=1 PYTHONPATH=appliance/src python3 -m pytest appliance/tests/ -v
```

Or just `./run_all_tests.sh`, which does all of it.
