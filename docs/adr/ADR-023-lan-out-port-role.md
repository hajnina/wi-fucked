# ADR-023 — LAN-out ports get a role, not a mode, and a background pipeline decides it

**Status:** Accepted
**Date:** 2026-08-07

## Context

ADR-022 decided *that* a wired/USB-Ethernet port with no upstream should
switch into DHCP-server mode after a DHCP-attempt/passive-listen sequence,
and that this is per-port, conservative, and never applies to a WAN pool
member speculatively. It did not decide the concrete shape of *how*: where
the result lives on the `Atomic` model, how a multi-second bounded pipeline
runs without blocking the daemon's fast/medium loops (`daemon.py`'s `tick()`
runs everything on one thread — see its own docstring on why medium runs
before fast), or how a LAN-out port's traffic reaches
`enforce.render()`'s output. Those are boundary-level decisions (a new field
on the atomic model, a new HAL protocol, a new module, a change to
`DesiredState`'s shape) that ADR-022 deliberately left open, and SOP-007
calls out that combination as needing its own ADR rather than being folded
into ADR-022's record of *why* auto-role-detection exists at all.

Two existing constraints shape every option here:

- **ADR-002**: an atomic's identity is SSID+BSSID / USB vendor+product+serial
  / MAC / IMEI — never `ifname`. Whatever represents "this port is a LAN-out
  port now" must not compromise that.
- **`daemon.py`'s threading model**: everything runs on one thread per
  `tick()`. A DHCP client attempt and a passive listen are each several
  seconds of real wall-clock waiting (ADR-022's Decision section calls this
  cost out explicitly as the price of not putting a rogue DHCP server on
  someone else's network) — running either synchronously inside a loop
  iteration would stall failover/reconciliation for that long, which is
  exactly the kind of daemon-caused outage ADR-011/ADR-008 exist to prevent
  the AP from ever depending on.

## Decision

**A wired port's outcome is recorded as a new `PortRole` field on `Atomic`
(`WAN` or `LAN_OUT`), orthogonal to `Mode`. The three-step pipeline
(DHCP-attempt → passive-listen → maybe-become-server) runs on a background
thread per port, owned by a new `wifucked.lanout.LanOutClassifier`, and its
finished results are applied to the registry by `daemon.py`'s medium loop —
never synchronously inside a loop iteration.**

- `PortRole.WAN` is the default for every atomic (matches ADR-022's
  discovery default of `Mode.NORMAL`). Only `_discover_usb`'s USB_ETHERNET /
  ETHERNET kinds ever become candidates for reclassification —
  `USB_TETHER` is explicitly excluded (a phone already carries its own
  connectivity via the modem's NAT; there is no "bare downstream port"
  reading for it).
- `Mode` stays "what the user permitted"; `PortRole` is "what the pipeline
  found this port to physically be." A `LAN_OUT` port's `Mode` is always
  set to `UNUSED` by the classifier alongside the role change (one atomic
  `Registry.set_role_and_mode()` call, never two separate writes that could
  observably land apart) — it is never a WAN pool member, and
  `Atomic.usable` checks `role is PortRole.WAN` as a second, explicit guard
  on top of the `Mode.UNUSED` check, not because either alone is
  insufficient today, but because the two facts (permission vs. physical
  role) are conceptually independent and a future change to one must not
  silently break the other's guarantee.
- The pipeline (`wifucked.lanout._run_pipeline`) runs on a
  `ThreadPoolExecutor` owned by `LanOutClassifier`, submitted from
  `Daemon._classify_lan_out_ports()` (called every medium tick, right after
  discovery) for any present, `Health.GOOD`, `PortRole.WAN`,
  `USB_ETHERNET`/`ETHERNET` atomic not already in flight or decided.
  `consider()` itself never blocks — it submits new work and harvests
  whatever finished since the last call, mirroring how `Discoverer` already
  separates "kick off a scan" from "the loop that calls it."
- A replug (present → absent → present again) clears the classifier's
  "already decided" marker for that atomic id, so a port moved to a
  different network gets reconsidered rather than permanently keeping its
  first answer — the same "recognise, don't assume" posture ADR-002 already
  takes for everything else about an atomic except this one physically
  re-derived fact.
- Three new `DhcpHal` methods (`attempt_client_lease`,
  `passive_listen_for_foreign_server`, `start_server`) land in `hal/base.py`
  alongside the existing HAL protocols, with `MockDhcp`/`LinuxDhcp`
  implementations in the same commit (SOP-002's rule for adding a HAL
  capability). `passive_listen_for_foreign_server` defaults to `True`
  ("something might be there") on any ambiguous, undetermined, or
  tool-failure outcome — the conservative direction ADR-022's Decision
  section requires, and the one both the mock's default and the Linux
  implementation's error path enforce explicitly.
- `enforce.DesiredState` gains `lan_out_marks: tuple[tuple[str, int], ...]`
  — `(ifname, fwmark)` pairs, separate from the existing `marks` field
  because LAN-out ifnames are genuinely dynamic (a port can appear or
  disappear between reconciles) where the AP's own VLAN-interface marks are
  static per `lan_mode`/`base_interface`, fixed at `LinuxEnforcer`
  construction. `render()` marks every present `PortRole.LAN_OUT` atomic's
  `ifname` with `BEST_EFFORT`'s vlan/fwmark — the same class an
  undifferentiated AP LAN client gets under ADR-020's default hotspot mode
  — and that mark rides whatever `RouteRule` the allocator's own
  `BEST_EFFORT` share already installs to the tunnel interface (ADR-019).
  No new routing table, no ifname-specific route: the fwmark is what ties a
  LAN-out port's traffic to the exact same tunnel-routed path an AP client's
  traffic already takes.

## Consequences

**Easier:**

- LAN-out ports reuse ADR-019's tunnel-routed egress mechanism completely —
  `enforce.render()` needed one new field and about a dozen lines, not a
  parallel routing scheme.
- `PortRole` being orthogonal to `Mode` means the allocator, the dashboard's
  existing mode-based counts, and every scenario test that reasons about
  `Mode.NORMAL`/`BACKUP`/`UNUSED` did not need to change at all — a
  `LAN_OUT` atomic simply never appears in the WAN pool, the same as any
  other `Mode.UNUSED` atomic, for a reason the dashboard can now also show
  (`role`).
- The background-thread pipeline pattern (`LanOutClassifier`) is reusable if
  a future feature needs another bounded, per-atomic background probe.

**Harder / foreclosed:**

- There is a real, if small, window between a wired port first appearing
  (immediately `Mode.NORMAL` per ADR-022) and the pipeline's verdict landing
  (up to `dhcp_client_timeout_s + passive_listen_timeout_s`, ~23s at the
  defaults) where the port sits in the registry looking like an ordinary WAN
  atomic with unmeasured capacity. In practice this is inert — the allocator
  will not route real traffic to an atomic with no confirmed capacity — but
  it is a real inconsistency in the model for that window, not eliminated by
  this design. Flagged here rather than silently accepted; closing it
  properly would mean a third, transient `Mode`/role state, judged not worth
  the complexity for a ~23s window.
- `enforce.LinuxEnforcer._read_marks()`'s real-kernel readback cannot
  recover a LAN-out ifname's actual vlan number from `nft`'s output (only
  the AP's VLAN-suffixed interface names encode it) — it falls back to
  treating the mark value itself as the vlan, which happens to be correct
  for every mark this codebase produces (vlan always equals fwmark, both AP
  and LAN-out) but is a coincidence of the current design, not a structural
  guarantee, and is called out in the code comment where it happens.
- Two more background threads' worth of complexity in a daemon that was
  previously single-threaded-per-tick by design. Bounded (`max_workers=4`,
  one future per in-flight port) and isolated (a pipeline exception is
  caught and logged, never propagated), but it is a real new failure surface
  to reason about.

**Must stay true:** `passive_listen_for_foreign_server` must keep defaulting
to "heard something" on every ambiguous or undetermined case — this is the
one guarantee the whole feature's safety rests on, and it is unit-tested
directly (`appliance/tests/test_lanout.py`) rather than left to be exercised
only incidentally by a scenario test.

## Alternatives considered

**Fold the pipeline's outcome into `Mode` itself (a fourth value,
`LAN_OUT`).** Rejected: `Mode` is documented as "what the user has
permitted us to do with a connection" — a LAN-out port isn't a permission
choice at all, it's a fact about what the port turned out to be. Conflating
them would mean every place that pattern-matches on `Mode` (allocator,
dashboard counts, scenario harnesses) would need a new case for a concept
that has nothing to do with WAN pool membership, and a LAN-out port
genuinely can be independently `UNUSED` today and something else if a future
design ever lets a user override the classifier's verdict.

**Run the pipeline synchronously in the medium loop, budgeted like
`LinuxProber`'s active-probe pass.** `LoopConfig.probe_budget_s` already
solves a similar problem for `probe/` by capping wall-clock work per pass
and picking up where it left off next tick. Considered, rejected here
because the probe budget's cost of "delayed" is a stale RTT estimate — cheap
to be wrong about for a few ticks. This pipeline's `dhcp_client_timeout_s +
passive_listen_timeout_s` is 15-25s at reasonable settings; chunking that
across medium-loop ticks (10s cadence) would mean a single port
classification spans several ticks with no clean resumption point mid-`tcpdump`,
and would still tie up the loop thread for whatever chunk size was chosen.
A background thread with the full timeout budget is simpler and never
blocks the fast loop's failover/reconciliation at all, which is the
property that actually matters (ADR-011).

**A new top-level `LanOutAtomic` type, separate from `Atomic`.** Rejected:
it would duplicate `Atomic`'s identity/health/capacity fields for what is,
after classification, still fundamentally "one independently usable network
interface, just used in the other direction" — and `enforce.render()`,
`Registry.persist()`, and the dashboard's atomic list would all need a
second code path to merge the two types back together for display. A field
on the existing model is a smaller, more honest change.

---
