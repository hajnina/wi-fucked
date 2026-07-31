# SOP-004 — Logging and observability

This appliance runs headless, in a van, on someone else's Wi-Fi, and you will
never reproduce the failure. **The log is the only debugging tool that reaches
you.** Treat every log line as a message to a future colleague who has no console,
no repro, and an annoyed user.

## Always use `get_logger`

```python
from wifucked.logging import get_logger

log = get_logger("allocator")     # → wifucked.allocator
```

Never `logging.getLogger` directly. `get_logger` returns a `ResilientLogger` that
will not crash the application when an `extra` payload collides with a reserved
`LogRecord` attribute (`name`, `msg`, `args`, `module`, …). A logging bug must
never take down the network.

Sub-loggers are rooted in `wifucked` and named after the module: `wifucked.discovery`,
`wifucked.enforce`, `wifucked.radio`, `wifucked.ota`.

## Required fields

Every log line that describes *work* carries these in `extra`:

| Field | Meaning |
|---|---|
| `workflow` | The operation — `wan_discovery`, `backup_activation`, `ota_apply`, `csa_move` |
| `state` | `started` · `processing` · `completed` · `failed` · `skipped` |
| `intent` | Why this is happening, in plain words |
| `duration_ms` | Anything that takes measurable time |
| `reason` / `error` | Any failure or fallback — plus `exc_info=True` |

```python
log.info(
    "Activated BACKUP atomic",
    extra={
        "workflow": "backup_activation",
        "state": "completed",
        "intent": "critical demand cannot be met by NORMAL pool",
        "atomic_id": atomic.id,
        "normal_capacity_bps": 1_400_000,
        "critical_demand_bps": 3_100_000,
        "duration_ms": 42,
    },
)
```

A workflow that starts must eventually log a terminal state — `completed`,
`failed`, or `skipped`. A `started` with no matching terminal line is how you get a
log that shows the system entering a function and nothing else, which tells you
almost nothing.

## Failures

```python
except Exception as exc:
    log.error(
        "Failed to apply CAKE qdisc; keeping previous shaping",
        extra={
            "workflow": "enforce_shaping",
            "state": "failed",
            "intent": "shape egress to measured capacity",
            "atomic_id": atomic.id,
            "target_bps": target_bps,
            "reason": "tc invocation returned non-zero",
            "error": str(exc),
        },
        exc_info=True,
    )
```

Note what the message says: not just that it failed, but **what the system did
instead**. A log line that reports a failure without stating the fallback leaves
the reader unable to tell whether the user was affected.

## Domain-specific requirements

### Radio and profile changes

These are the events that can break the always-available promise, so they get the
most detail. Any AP channel move, profile switch, or CSA logs: old channel, new
channel, why, how many clients were associated before, how many after.

```python
log.info(
    "AP channel moved to follow station",
    extra={
        "workflow": "csa_move",
        "state": "completed",
        "intent": "SHARED profile requires AP and station on one channel",
        "profile": "SHARED",
        "channel_from": 6, "channel_to": 11,
        "clients_before": 4, "clients_after": 4,
        "duration_ms": 310,
    },
)
```

`clients_after < clients_before` is a defect. Log it loudly enough to notice.

### Allocation decisions

Beyond logging, these write a structured **decision record** — this is an
architectural requirement, not a nicety
([ADR-009](../adr/ADR-009-decision-records.md)):

```python
telemetry.decisions.record(
    action="activate_backup",
    inputs={"normal_capacity_bps": 1_400_000, "critical_demand_bps": 3_100_000,
            "rtt_ms": 820, "loss_pct": 17.0},
    thresholds={"activation_deficit_bps": 500_000, "dwell_s": 120},
    reason="NORMAL capacity below critical demand beyond activation threshold",
)
```

The dashboard renders these directly. If a decision has no record, the machine
cannot explain itself, and the product loses its main differentiator.

### Cost

Every byte on a `BACKUP` atomic is accounted: bytes, activation duration,
activation reason, responsible traffic class, and the degradation that justified
it. A user asking "why did this cost me money?" must get a complete answer from
stored data, not a reconstruction.

### OTA

Explicit state at every step: version from, version to, target slot, health check
outcome, rollback decision and cause. An update that half-applied and rolled back
must leave a log that explains the whole sequence.

## What not to log

- **Credentials.** Wi-Fi passphrases, PSKs, WireGuard private keys, fabric tokens.
  Not at `DEBUG`, not "temporarily". Log the atomic ID and that authentication was
  attempted.
- **Payload.** No packet contents, no DNS query names, no destination addresses of
  user traffic. The appliance sees everything the user does; it records none of it.
  Aggregate counters only.
- **Per-packet anything.** A log line in a path that runs at packet rate will fill
  the SD card and destroy it. If it can happen more than a few times a second,
  count it and log the counter periodically.

## Volume and flash wear

The SD card is consumable and logging is the main thing that consumes it
([`../hardware.md`](../hardware.md)).

- `DEBUG` is for development, not for shipped defaults.
- Anything periodic logs at a period, not per iteration. The fast loop runs at
  ~1 Hz — it must not log at 1 Hz.
- Log state *changes*, not state. "Still healthy" every second is noise that
  crowds out the one line that mattered.
- Rotation is configured against a wear budget, not disk free space.

## Verifying your logging

Read your own output before requesting review:

```bash
MOCK_HW=1 WIFUCKED_SCENARIO=moving_van PYTHONPATH=appliance/src python3 -m wifucked
```

Ask the honest question: **if this had happened in the field and this were all I
had, could I tell what the system did and why?** If not, the logging is not
finished — and neither is the change.
