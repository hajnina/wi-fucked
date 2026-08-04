# Backlog: blockers to passing real traffic

Source: a full code review found the appliance cannot pass LAN traffic as
shipped (no NAT, disagreeing egress paths, one routing table instead of one
per atomic) plus 25 numbered defects total, ranging from that down to
cosmetic log duplication. This doc is the live, ordered backlog derived from
that review. One item = one PR.

**Keep this file current.** Every agent picking up an item updates its
Status line in the same PR that does the work — this file is a live tracker,
not a snapshot. See [SOP-010](../sop/SOP-010-keeping-documentation-current.md).

## How to pick up an item

1. Take the next item below with `Status: not started`, top to bottom —
   the order is load-bearing (later items assume earlier ones landed,
   especially the harness fix in item 3, which items 4 and 6 test against).
2. Set its status to `in progress` before starting.
3. Follow `CLAUDE.md` and the relevant SOPs (`docs/sop/`) — conventional
   commit, scenario test if you touch `policy/`, `allocator/`, `enforce/`,
   or `radio/`, update any doc/ADR/SOP your change makes wrong, in the same
   PR.
4. Open the PR (CLAUDE.md's git section: auto-open, watch it, auto-merge on
   green CI with no unresolved feedback).
5. Update this file's Status line to `PR #<n>` when opened, `merged` when
   merged, in the same PR that closes out the item (or a trivial follow-up
   commit to this doc if the tracker update didn't make it into the
   original PR).

## Ordering rationale

1. One-liners first (#4, #5 in the source review) — isolated, zero risk of
   touching anything else in flight.
2. Harness fix — makes the two product invariants (AP never drops, BACKUP
   carries zero bytes until active) actually falsifiable. Every later
   allocator/enforce PR should land against this, not the old harness.
3. `render()` fix — the single seam most other data-plane bugs run through.
4. ADR + NAT/tunnel-egress unification — the architectural decision every
   later egress-adjacent change depends on.
5. Allocator hysteresis/decision-record bugs — now testable thanks to the
   harness fix.
6. Concurrency and single-thread cadence — correctness/liveness under load.
7. Unauthenticated API — security-sensitive, independent of the above.
8. Discovery hygiene — storage wear + identity correctness.
9. Logging conformance.
10. Telemetry cadence and persisted-capacity confidence — these interact,
    do together, and land after the watchdog and hysteresis fixes since the
    review notes they compound.
11. Small independent fixes.
12. Wi-Fi-as-WAN NM conflict — largest standalone fix, can't be fully
    verified under `MOCK_HW=1`, needs an `active-tests.md` entry.

## Items

### 1. fix(fabric): set real version at build time
**Status:** in progress

`fabric/src/fabric/__init__.py:12` hardcodes `__version__ = "0.0.0-dev"`.
Inject the build version the same way the appliance pipeline does (see
`reusable_image_pipeline.yml:220-263`, which already names
`WIFUCKED_FABRIC_MIN=0.1.0`). Update the Dockerfile to inject it instead of
a bare `COPY src/ src/`. Verify `fabric_compatible()` passes against the
baked `WIFUCKED_FABRIC_MIN`.

### 2. fix(daemon): feed or remove the systemd watchdog
**Status:** merged

`wifucked.service:25` sets `WatchdogSec=120` with no `sd_notify`/
`WATCHDOG=1` anywhere and no `NotifyAccess=`. Either add a keepalive
`sd_notify(WATCHDOG=1)` call on the fast loop (needs `NotifyAccess=main` and
`Type=notify`) or remove `WatchdogSec` if not ready to commit to a liveness
contract yet. Prefer feeding it — check whether `sd_notify` is already
wrapped anywhere in `appliance/src/wifucked/`.

### 3. test(scenarios): assert on MockEnforcer/MockAp instead of Allocation.shares
**Status:** merged

`appliance/tests/scenarios/conftest.py`: rewrite `_backup_bytes()` (and/or
`Frame`/`_capture()`) to read from an `Enforcer` (`MockEnforcer.actual()` /
`.bytes_on()`, `enforce/__init__.py` ~125-161) that the harness must now
construct and route `Daemon.tick()`'s rendering through, not from
`Allocation.shares` directly. Drive `MockAp` from scenarios (e.g. allow a
scenario to simulate a client drop) so the "AP never drops / never loses
clients" invariant can actually fail. Prerequisite for item 4 — confirm with
the scenario suite that item 4's bug is caught red before it's fixed.

### 4. fix(enforce): table-per-atomic routing, skip zero-ceiling shares, honor quiesced, shape from up_bps
**Status:** not started

`enforce/__init__.py` `render()` (~78-122): derive `table` per atomic (e.g.
stable offset from atomic id) instead of the constant `100`; skip emitting a
`RouteRule`/`Shaping` for shares with `ceiling_bps == 0`; consume
`allocation.quiesced` to actively withhold/clear routing for quiesced
atomics rather than silently omitting them. `_apply_cake` (~264-296): shape
egress from `shaping.up_bps`, not `down_bps` — and decide whether ingress
(`down_bps`) needs an IFB device to actually be shaped at all (currently
nothing creates one). Update `architecture.md:155` if the table-numbering
scheme differs from "one per atomic" in any nuance. Requires a scenario test
(this module is in CLAUDE.md's required list) — must render an allocation
containing an active BACKUP and assert both NORMAL and BACKUP get
independent tables.

### 5. docs(adr): decide and document tunnel-vs-WAN egress ownership, then implement NAT
**Status:** not started

Write a new ADR (next number after ADR-017) deciding: does LAN client
egress route through `wg0` to the fabric (per ADR-005 / `architecture.md
:133-145`'s promise that client-visible IP survives a WAN swap), or directly
out the WAN interface? Neither is consistently true today
(`enforce.render()` sends marked traffic straight out `dev <wan_ifname>`,
`tunnel._configure_interface` sets `allowed-ips` to the tunnel pool not a
default route, `fabric/wireguard.py add_peer` never forwards/NATs peer
traffic). Once decided:
- If tunnel-owned: `enforce/__init__.py` routes must send LAN traffic to
  `wg0`, not the WAN ifname; `tunnel/__init__.py` `_configure_interface`
  needs a route capable of carrying default-route traffic; fabric side
  (`fabric/src/fabric/wireguard.py`, plus fabric forwarding config) needs
  both `ip_forward` and NAT/masquerade for tunnel peer traffic egressing the
  fabric's own WAN.
- If WAN-direct: add NAT (`nft ... masquerade` in a new `nat`/`postrouting`
  chain, `enforce._nft_ruleset()` ~333-345) for LAN→WAN, and reconcile this
  against ADR-005's IP-stability promise (likely requires amending or
  superseding ADR-005).
Either resolution needs a scenario test proving a LAN client's traffic both
traverses the chosen path and survives a simulated WAN swap.

### 6. fix(allocator): hysteresis stuck-in-ACTIVE, duplicate backup-is-primary shares, ARMING decision record lies
**Status:** PR #30

`allocator/__init__.py`:
- `_step_hysteresis` (~165-167): only `recovered` leaves ACTIVE — add the
  vanished-backup transition out of ACTIVE, and ensure the 120s activation
  dwell is still honored on re-activation (no zero-dwell bypass), fix the
  contradictory `action=allocate_normal` / `backup_state=active` decision
  record emitted every tick while backup is gone.
- `_build` (~257-303): fix the case where the backup atomic is also the
  primary (no NORMAL pool) — currently emits two conflicting `Share`
  entries for the same atomic/profile; should emit one.
- `decide()`/decision-record path (~318): fix the ARMING state reporting
  `action=no_connectivity` / "no usable NORMAL or BACKUP connection" while a
  healthy BACKUP is arming.
Needs a scenario test — reuse the fixed harness from item 3.

### 7. fix(daemon): thread-safe registry/telemetry access
**Status:** not started

`__main__.py` (~60, ~91) runs the loop thread and Flask `threaded=True`
concurrently against unguarded shared state: `Registry._atomics` (mutated by
both the API's `set_mode`/`persist()` and the loop's `observe()`), and
`Telemetry._buffer` (appended from both threads, sqlite opened with
`check_same_thread=False`). Add a lock (mirror the fabric's approach in
`fabric/peers.py:115-127`, which already does this correctly with a thread
lock + flock — reuse that pattern/reasoning).

### 8. perf(daemon): stop the medium loop from starving the fast loop
**Status:** not started

`daemon.tick()` runs loops sequentially in one thread; `LinuxProber
._active_probe` (`probe/__init__.py` ~319-334) blocks on up to two `ping`
invocations per atomic at `timeout=10`, starving the 1s fast loop
(failover/reconciliation/tunnel rebind). Move probing off the fast-loop
thread (separate thread/async, or cap total probe time per tick) so
`architecture.md:41-45`'s cadence table becomes actually true — update that
doc if the enforced cadence ends up different.

### 9. fix(api): require authentication, bind off 0.0.0.0
**Status:** not started

`config.py:64` / `api/__init__.py:83-100`: `POST /api/atomics/<id>/mode` and
`GET /api/diagnostics/bundle` are open to any network the appliance is
attached to, contradicting `architecture.md:143-145`'s "WANs are hostile,
LAN services never exposed through an arbitrary WAN." Add auth (token or
LAN-only bind) consistent with that architecture doc; adjust the doc if the
chosen mechanism differs from what it currently implies.

### 10. fix(discovery): dedupe SSID-only atomics, stop wearing the SD card
**Status:** not started

`discovery/__init__.py` (~51-70, ~52, and the ~10s AP-radio scan): every
visible SSID becomes a permanent persisted `Atomic`, and `persist()`
(`registry.py` ~198-228) rewrites the whole registry every slow loop —
unbounded growth plus SD-card wear ADR-010 exists to avoid. Bound what gets
persisted (e.g. only atomics that were ever connected, or apply an LRU/TTL),
fix connected-network matching to use SSID+BSSID (per ADR-002) instead of
SSID-only so two APs on one SSID don't collide on `ifname=wlan0`, and
stop/reduce the AP-radio off-channel scan (undercuts ADR-011's "AP is the
anchor" — may need its own decision if nontrivial).

### 11. fix(logging): stop duplicate message fields, fix fabric logging
**Status:** not started

`logging.py:22-29`: `_RESERVED` must also exclude `message` and `asctime`
(both written onto the record by `Formatter.format` before
`_ExtraFormatter` harvests `record.__dict__`), which currently causes every
log line to duplicate its own message/timestamp as extra fields.
`fabric/peers.py:38`, `fabric/wireguard.py:27`: replace `logging.getLogger`
with the project's `get_logger` convention, and configure a root
handler/level in the fabric app so `log.info` isn't silently dropped under
gunicorn's default level.

### 12. fix(telemetry): flush on the fast loop, restore capacity confidence across restarts
**Status:** not started

`daemon.py:211`: `telemetry.tick()` only runs in the slow loop, making the
documented 60s `flush_interval_s` effectively 300s — call it from the fast
loop (or whichever cadence matches the documented interval once item 8
lands). `registry.py` `persist()`/`_load` (~208-211 and load-side
reconstruction): persist `confidence`/`measured_at` too, not just
`down_bps`/`up_bps`, so `Capacity.known` doesn't reset to False (and
effective capacity to 0) on every restart — compounds with items 2 and 6,
land after both.

### 13. fix: small independent bugs
**Status:** not started

- `allocator/__init__.py` `due_for_liveness` (~195-211): stop mutating
  `last_liveness` as a side effect of a predicate check; make stamping
  explicit and separate from querying. Also fix the immediate-fire-on-
  first-call behavior for every BACKUP.
- `discovery/__init__.py` `ethernet_atomic` (~113): don't label non-USB
  Ethernet as `Kind.USB_ETHERNET`.
- `api/__init__.py` `api_decisions` (~67): don't 500 on a non-numeric
  `limit` query param — validate and return 400.

### 14. fix(hal/wifi): make Wi-Fi-as-WAN usable under unmanaged-devices
**Status:** not started

`setup_rpi.sh:84` marks `wlan0*` unmanaged by NetworkManager (correct,
needed for the AP), but `hal/linux.py` `LinuxWifi.scan()` /
`connect_station()` (~81, ~127) drive `nmcli dev wifi`, which won't
scan/connect on an interface NM doesn't manage — so `_discover_wifi` always
returns `[]` and the SHARED radio profile can never engage. Needs a real fix
(e.g. drive Wi-Fi-as-WAN via `iw`/`wpa_supplicant` directly instead of
`nmcli` — confirm whether this is a second radio or a virtual interface off
the same chip, since the Zero 2W is single-radio; may need its own design
decision, not just a driver swap). Can't be fully verified under
`MOCK_HW=1`; add a `docs/active-tests.md` entry (`UNCONFIRMED` until someone
runs it on hardware) rather than claiming it works from mocks alone.

## Verification (every item)

```
MOCK_HW=1 PYTHONPATH=appliance/src python3 -m pytest appliance/tests/ -v
./run_all_tests.sh
```

Items 3, 4, 6 must show the relevant scenario test failing on the pre-fix
code and passing after — that's the point of item 3. Item 14 additionally
needs a hardware run before its `active-tests.md` entry can leave
`UNCONFIRMED`.
