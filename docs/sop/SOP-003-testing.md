# SOP-003 — Testing

## Everything runs without hardware

No test may require a Pi, a radio, or root. `MOCK_HW=1` is the primary development
path, not a convenience.

```bash
./run_all_tests.sh                                              # everything
MOCK_HW=1 PYTHONPATH=appliance/src python3 -m pytest appliance/tests/ -v
MOCK_HW=1 PYTHONPATH=appliance/src python3 -m pytest appliance/tests/scenarios/ -v
```

If a change seems to need hardware to test, the fix is to add a HAL seam, not to
skip the test.

## The two invariants

**Every scenario test asserts these, whatever else it checks.** They are the
product promises; a change that breaks either is a release blocker regardless of
what else it improves.

1. **The AP never drops.** Not across WAN churn, profile switches, channel moves,
   or daemon restarts. Clients that were associated stay associated.
2. **`BACKUP` carries zero bytes** until critical demand genuinely cannot be met by
   the `NORMAL` pool — beyond the accounted liveness budget
   ([ADR-006](../adr/ADR-006-backup-liveness-budget.md)).

`appliance/tests/scenarios/conftest.py` provides `assert_invariants(timeline)`.
Call it. It is cheap and it catches the class of bug that would otherwise be found
by a user in a van.

## Scenario tests are mandatory for control code

**Any change to `policy/`, `allocator/`, `enforce/`, or `radio/` requires a
scenario test.** No exceptions, including for changes that look obviously safe —
those are precisely the ones that misbehave only in the field.

A scenario drives the control loop through a scripted timeline of world events and
asserts on outcomes:

```python
def test_backup_holds_through_moderate_degradation(harness):
    harness.add_atomic("hotel-wifi", mode=NORMAL, capacity_bps=8_000_000)
    harness.add_atomic("phone-usb", mode=BACKUP, capacity_bps=20_000_000)
    harness.set_demand(critical_bps=2_000_000, besteffort_bps=15_000_000)

    harness.run_for(minutes=5)

    # Demand exceeds NORMAL capacity, but critical is still being met.
    # Tolerating slow service is correct; spending money is not.
    assert harness.bytes_on("phone-usb") == 0
    assert harness.served_bps("critical") >= 2_000_000
    assert_invariants(harness.timeline)
```

Write the scenario before the implementation where you can. It clarifies the
requirement better than a ticket does.

## What to test at which level

| Level | Use for | Keep it |
|---|---|---|
| **Unit** | Pure logic — version parsing, identity derivation, capacity maths, config merge | Fast, no I/O, no clock |
| **Scenario** | Control behaviour over time — allocation, hysteresis, failover, profile switching | Deterministic; drive the clock, never sleep |
| **Integration** | Module wiring — daemon starts, API answers, telemetry writes | Under `MOCK_HW=1` |
| **Real-kernel proof** | Boundaries no mock can stand in for — real `nft`/`wg`/`tc`, real `hostapd`/`dnsmasq`, real 802.11 association | Root-requiring, outside `run_all_tests.sh`, own CI job or manual — see below |
| **Hardware** | Only what genuinely cannot be mocked — driver firmware behaviour, real RF, real throughput | Manual, documented in [SOP-009](SOP-009-hardware-and-field-debugging.md) |

## Real-kernel proofs: the one exception to "no root"

"No test may require... root" above describes the mandatory default path
(`run_all_tests.sh`, what every PR must pass). It does not mean this repo may
never verify something a mock cannot: whether real `hostapd` accepts a
generated config, whether a real WireGuard handshake completes, whether real
`nft` marking is syntactically valid. Those need a real kernel network stack,
which needs root — [`appliance/tests/qemu/`](../../appliance/tests/qemu/)
(full guest kernels, for WireGuard/CAKE/nftables) and
[`appliance/tests/e2e/`](../../appliance/tests/e2e/) (network namespaces +
`mac80211_hwsim`, for hostapd/dnsmasq/the dashboard) both exist for exactly
this. They are not exempt from being trustworthy: each documents, in its own
README, precisely what a passing run confirms and what it doesn't (usually:
real Linux kernel networking, but not real Pi hardware/firmware/RF). Keep
that distinction explicit rather than letting a green root-requiring proof
read as "confirmed on hardware" — see
[`docs/active-tests.md`](../active-tests.md).

`appliance/tests/e2e/`'s harness is light enough to run in CI on every PR (no
kernel fetch, no initramfs build — see its own job in `ci.yml`); the heavier
QEMU proofs under `appliance/tests/qemu/` are not currently wired into CI and
run manually, documented as such in `docs/active-tests.md`.

## Time is injected, never slept

Scenario tests must not call `time.sleep`. The harness owns a virtual clock;
advance it. A test suite that takes real minutes to exercise a hysteresis window
will be deleted by the first person in a hurry.

```python
harness.run_for(minutes=30)     # instant
time.sleep(1800)                # never
```

## Anti-flap coverage

Hysteresis is mandatory in the product ([ADR-006](../adr/ADR-006-backup-liveness-budget.md)),
so it is mandatory in the tests. Any change to activation or recovery thresholds
needs a test that feeds oscillating input and asserts the output does *not*
oscillate:

```python
def test_oscillating_capacity_does_not_flap_backup(harness):
    for _ in range(20):
        harness.set_capacity("hotel-wifi", 1_000_000); harness.run_for(seconds=20)
        harness.set_capacity("hotel-wifi", 9_000_000); harness.run_for(seconds=20)

    assert harness.count_transitions("phone-usb") <= 2
```

## The airgap check

The dashboard serves every byte from the Pi. `verify_no_external_assets.py` fails
the build if any template references an external host — no CDN, no Google Fonts,
no remote script. It runs in CI before the image is baked.

If you need a library, vendor it into `ui/static/`.

## Coverage

No numeric target. Targets get gamed with tests that execute lines without
asserting behaviour. The bar is judgement:

- Every branch in `allocator/` and `policy/` is exercised by a scenario.
- Every failure path logs what it says it logs.
- Every bug fixed gets a regression test that fails before the fix.

## Before requesting review

```bash
./run_all_tests.sh
```

Green, locally, on your branch. "CI will catch it" is not a testing strategy — CI
bakes a multi-gigabyte image, and burning a runner slot to discover a typo is rude
to whoever is queued behind you.
