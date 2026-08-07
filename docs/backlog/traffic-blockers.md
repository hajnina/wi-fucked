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
**Status:** merged

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
**Status:** merged

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
**Status:** merged

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
**Status:** merged

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
**Status:** merged

`__main__.py` (~60, ~91) runs the loop thread and Flask `threaded=True`
concurrently against unguarded shared state: `Registry._atomics` (mutated by
both the API's `set_mode`/`persist()` and the loop's `observe()`), and
`Telemetry._buffer` (appended from both threads, sqlite opened with
`check_same_thread=False`). Add a lock (mirror the fabric's approach in
`fabric/peers.py:115-127`, which already does this correctly with a thread
lock + flock — reuse that pattern/reasoning).

### 8. perf(daemon): stop the medium loop from starving the fast loop
**Status:** merged

`daemon.tick()` runs loops sequentially in one thread; `LinuxProber
._active_probe` (`probe/__init__.py` ~319-334) blocks on up to two `ping`
invocations per atomic at `timeout=10`, starving the 1s fast loop
(failover/reconciliation/tunnel rebind). Move probing off the fast-loop
thread (separate thread/async, or cap total probe time per tick) so
`architecture.md:41-45`'s cadence table becomes actually true — update that
doc if the enforced cadence ends up different.

### 9. fix(api): require authentication, bind off 0.0.0.0
**Status:** merged

`config.py:64` / `api/__init__.py:83-100`: `POST /api/atomics/<id>/mode` and
`GET /api/diagnostics/bundle` are open to any network the appliance is
attached to, contradicting `architecture.md:143-145`'s "WANs are hostile,
LAN services never exposed through an arbitrary WAN." Add auth (token or
LAN-only bind) consistent with that architecture doc; adjust the doc if the
chosen mechanism differs from what it currently implies.

### 10. fix(discovery): dedupe SSID-only atomics, stop wearing the SD card
**Status:** merged

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
**Status:** merged

`logging.py:22-29`: `_RESERVED` must also exclude `message` and `asctime`
(both written onto the record by `Formatter.format` before
`_ExtraFormatter` harvests `record.__dict__`), which currently causes every
log line to duplicate its own message/timestamp as extra fields.
`fabric/peers.py:38`, `fabric/wireguard.py:27`: replace `logging.getLogger`
with the project's `get_logger` convention, and configure a root
handler/level in the fabric app so `log.info` isn't silently dropped under
gunicorn's default level.

### 12. fix(telemetry): flush on the fast loop, restore capacity confidence across restarts
**Status:** merged

`daemon.py:211`: `telemetry.tick()` only runs in the slow loop, making the
documented 60s `flush_interval_s` effectively 300s — call it from the fast
loop (or whichever cadence matches the documented interval once item 8
lands). `registry.py` `persist()`/`_load` (~208-211 and load-side
reconstruction): persist `confidence`/`measured_at` too, not just
`down_bps`/`up_bps`, so `Capacity.known` doesn't reset to False (and
effective capacity to 0) on every restart — compounds with items 2 and 6,
land after both.

### 13. fix: small independent bugs
**Status:** merged

- `allocator/__init__.py` `due_for_liveness` (~195-211): stop mutating
  `last_liveness` as a side effect of a predicate check; make stamping
  explicit and separate from querying. Also fix the immediate-fire-on-
  first-call behavior for every BACKUP.
- `discovery/__init__.py` `ethernet_atomic` (~113): don't label non-USB
  Ethernet as `Kind.USB_ETHERNET`.
- `api/__init__.py` `api_decisions` (~67): don't 500 on a non-numeric
  `limit` query param — validate and return 400.

### 14. fix(hal/wifi): make Wi-Fi-as-WAN usable under unmanaged-devices
**Status:** merged

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

### 15. fix(allocator): a first-time client can never get routed at all

**Status:** merged
**Found by:** `appliance/tests/e2e/`'s fabric/tunnel proof (PR #48) — the
first real, non-mocked, non-hand-seeded exercise of this exact path. Every
earlier proof of ADR-019 egress (including
`appliance/tests/qemu/run_wan_chaos_download_test.sh`) hand-seeded a fixed,
non-zero `ClassDemand` from the start (`chaos_driver.py`: `demand = {...
down_bps=8_000_000 ...}`), which is exactly what sidesteps this bug — none
of them ever exercised demand actually reaching zero-to-nonzero from a cold
boot.

**The bug:** `allocator/__init__.py` `_build()` (~312) computes a share's
`ceiling_bps` from `demand[profile].down_bps` alone. `demand/__init__.py`'s
`CounterDemand` docstring is explicit about direction: `down_bps` is the
LAN interface's *transmit* delta — traffic going *out* to clients, i.e.
replies — and `up_bps` is *receive* — traffic *in* from clients, i.e.
requests. A brand-new client's first packet (a SYN, a DNS query) is
`up_bps`, not `down_bps`: it arrives at the AP and increments a real RX
counter regardless of whether any route exists yet to forward it anywhere.
But `render()` (`enforce/__init__.py` ~150) only ever installs a policy
route for a share whose `ceiling_bps > 0`, and `ceiling_bps` only comes from
`down_bps` — which can only become non-zero *after* a reply has already
made it back through a route that, by construction, does not exist yet.
Confirmed directly in a real CI run: `wifucked.enforce`'s own structured
log showed `route_rules=0` on every single reconcile tick for the full
150s of a real client's real connection attempt (`ip rule show` /
`ip route show table 888` on the guest independently confirmed no policy
route was ever installed), and the real `curl` download through the real
tunnel timed out unable to even open a TCP connection
(`Failed to connect to ... Couldn't connect to server`) — not a throughput
problem, a routing-never-existed problem.

**Likely fix shape** (needs its own design pass, not a blind one-line
patch — this changes allocator contract, needs a scenario test per
SOP-003): the share-ceiling computation needs to account for `up_bps`
(client demand to be let *out*) as well as `down_bps`, or a NORMAL atomic
with unclaimed headroom needs some small non-demand-gated floor per profile
so a first packet always has somewhere to go, with demand-measured ceilings
taking over once real traffic is flowing. Either approach needs to preserve
ADR-006/ADR-022's money-safety guarantees for `BACKUP` — the fix must not
accidentally let a first-time client force capacity onto a metered
connection with no measured demand to justify it.

**Verification:** `appliance/tests/e2e/`'s fabric/tunnel proof (PR #48,
stage `18_tunnel_download_survives_chaos`) is the regression test — it
should go from FAIL to PASS once this is fixed, on real WAN atomics with
zero pre-seeded demand.

**Update 2026-08-07:** the fix merged (#51) and stage 18 was re-run on PR
#48 with it in place. Stage 18 still FAILs — but on different evidence,
see item 16. The `up_bps`/first-packet gap described above is fixed; a
second, separate bug sits behind it.

### 16. investigate: primary WAN stable and healthy, but zero bytes ever
### reach the far side of the tunnel

**Status:** open, not yet root-caused
**Found by:** re-run of `appliance/tests/e2e/`'s fabric/tunnel proof (PR
#48, run `31187384249`) after merging item 15's fix. Stages 1–17 all PASS,
including `17_wan_failover_observed`. Stage `18_tunnel_download_survives_chaos`
still FAILs: `curl: (28) Failed to connect to 198.51.100.2 port 8000 after
131154 ms`.

**What the artifacts actually show, distinct from item 15:**
- `state_snapshots.json`: `allocation.primary_id` goes `null` (first four
  snapshots, still discovering) then locks onto `usbeth:b9afbc8466` and
  **never changes again** for the rest of the 150s run. That atomic's
  `health` is `"good"` in every single snapshot for the entire run — the
  allocator picked a healthy WAN and correctly never had a reason to move
  off it. (The one atomic that ever shows `"degraded"`, `usbeth:29fbc08cfe`,
  is not primary and irrelevant to this path.) `17_wan_failover_observed`'s
  "1 switch" is just `null → usbeth:b9afbc8466`, not a real failover — no
  actual re-routing event happens in this run.
- `internet-httpd.log` (the real HTTP server behind the fabric that the
  download targets) is **completely empty** — zero requests arrived, for
  the full 131s curl waited. Not slow, not partial: nothing ever got there.
- `wg show` in the curl failure detail shows a live handshake ("34 seconds
  ago") but `transfer: 184 B received, 552 B sent` — the WireGuard tunnel
  itself is up and exchanging control traffic, just not carrying the
  client's actual TCP stream.
- No `wifucked.enforce` log lines appear in the captured guest console log
  at all for this run (item 15's investigation found them there directly),
  so whatever's happening here may be below or beside the allocator/enforce
  reconciliation loop entirely — e.g. NAT/masquerade on the tunnel egress,
  the policy-route table the client's traffic is actually classified into,
  or something MTU/PMTU-shaped over WireGuard. Needs its own trace, not a
  guess.

**Why this matters:** item 15 fixed "a client can never get a route
allocated." This is "even given a stable, correctly-chosen, healthy primary
WAN with a live WireGuard handshake, a real client's real TCP stream still
never arrives at the other end." Two independent breaks in the same path —
fixing one did not fix the other, and PR #48's stage 18 is the only test
that has ever caught either of them; every hand-seeded proof before it
sidestepped both.

**Next step:** needs someone to reproduce locally (or re-run the e2e job
with more capture around `wifucked.enforce`/`nft`/`ip rule`/`ip route`
state at the moment curl is issued) rather than being fixed blind. Until
then stage 18 stays red and PR #48 stays unmerged.

**Status:** root-caused — two independent bugs, one fixed here, one is CI
infrastructure debt

Added real diagnostics to the e2e harness itself (host-side `iptables -L
FORWARD`/`nft list ruleset`/`conntrack -L`/route-get, guest-side `ip
rule`/`ip route show table N`/`rp_filter`, an unbuffered Internet stand-in,
and a full-run dump of `wifucked.enforce`'s own reconcile log) instead of
continuing to guess from `wg show`'s byte counters alone. That surfaced two
real, independent things:

1. **CI-environment-only, real but not this bug's cause:** GitHub-hosted
   runners have `dockerd` running by default, which sets the host's
   `FORWARD` chain policy to `DROP` the moment it enables IP forwarding
   (moby/moby#50566, Debian bug #865975) — silently drops the CI harness's
   own WAN/tunnel/Internet-stand-in forwarding regardless of how correct
   `enforce`/`tunnel`/fabric NAT are. Fixed in the harness itself
   (`appliance/tests/e2e/run_e2e_ap_test.sh`, explicit `iptables -I FORWARD`
   accepts inserted into the same chain `dockerd` set the policy on) — see
   `docs/active-tests.md`'s ADR-019 entry. Real infrastructure debt worth
   having fixed, but proven **not** the reason stage 18 was failing: the
   fix alone did not change the outcome on a re-run.
2. **The actual cause — a second, independent capacity-side deadlock,
   same shape as item 15 but on the other half of the same expression:**
   `wifucked.enforce`'s own `route_rules=0` on **every single** reconcile
   tick across a full 150s+ run (`enforce_reconcile_trend.log`, a new
   full-run dump added specifically to catch this) proved no policy route
   was *ever* installed, for the entire run — not a timing race. Traced to
   `allocator/__init__.py` `_usable_capacity()`: it only counts an atomic's
   capacity once `confidence >= min_confidence`, and confidence only ever
   rises from `probe.PassiveProber`'s `fold()` on a *saturated* observation
   — which needs a route to already exist to carry the traffic that would
   saturate it. Item 15 fixed the *demand* half of `_build()`'s
   `min(want_bps, headroom)`; `headroom` (from `_usable_capacity()`) is the
   other half, and item 15's fix never touched it. Every scenario test that
   ever exercised this path — including item 15's own — hand-seeded
   `Capacity` via `harness.add_atomic(capacity_bps=...)`, exactly like every
   earlier proof hand-seeded demand, which is why no scenario test ever
   caught this either.

**Fix:** [ADR-024](../adr/ADR-024-capacity-bootstrap-floor.md) — a NORMAL
atomic that has never been measured at all (`capacity.measured_at is None`)
gets a small, fixed bootstrap headroom (`BOOTSTRAP_HEADROOM_BPS = 256_000`)
instead of zero, enough to open a route for a first client's first
connection and let it generate the traffic `PassiveProber` needs to produce
a real measurement. Never written into `Capacity` itself (stays out of
`down_bps`/`confidence` entirely, so it never reports as a measurement
anywhere), stops applying permanently the moment a real fold happens, and —
same scope as item 15 — NORMAL-only; BACKUP is unaffected (ADR-006).
Scenario coverage: `appliance/tests/scenarios/test_capacity_bootstrap.py`
(the deadlock itself, the one-time-only property, and BACKUP exclusion).

**Verification:** `appliance/tests/e2e/`'s fabric/tunnel proof (PR #48,
stage `18_tunnel_download_survives_chaos`) remains the regression test for
both bugs together — should go from FAIL to PASS with both fixes in place,
on real WAN atomics with zero pre-seeded demand *and* zero pre-seeded
capacity.

**Status update:** with both fixes above in place, stage 18 still failed —
but the actual cause turned out to be a third, unrelated bug (item 17,
below), not the tunnel/NAT path at all. Root-caused via a genuine real
packet capture on the appliance's own `wlan0`/`wg0` (`appliance/tests/e2e/`
now installs `tcpdump` and captures both, after an earlier attempt was
itself broken — see item 17's write-up): the client's SYN genuinely reaches
`wlan0` (six real retransmits, correct addressing) but never reaches `wg0`
at all, because the WAN atomics it would have routed through get pulled out
of the NORMAL pool entirely before the packet gets there.

### 17. fix(hal/linux): a working WAN atomic gets misclassified as a bare LAN-out port

**Status:** merged
**Found by:** debugging item 16 (above) — after both of item 16's own fixes
landed, `appliance/tests/e2e/`'s fabric/tunnel proof (PR #48) still failed
stage 18. A real packet capture proved the client's SYN reaches `wlan0` but
never reaches `wg0`; `state_snapshots.json` showed why: every WAN atomic's
`role` starts `"wan"` (the first two snapshots) and then flips to
`"lan_out"` for every snapshot after that, for all three atomics, for the
rest of the run. `Atomic.usable` requires `role is PortRole.WAN`, so once
misclassified an atomic drops out of the NORMAL pool entirely — headroom
goes back to zero and the route gets torn down, regardless of anything
item 16 fixed.

**The bug:** `lanout/__init__.py`'s `_is_candidate()` (ADR-023's DHCP-attempt
→ passive-listen → DHCP-server pipeline) only checks `present`/`role`/
`kind`/`health` — never whether the atomic already has a real, working
upstream connection. Every wired/USB-Ethernet interface is managed by
NetworkManager by default (`setup_rpi.sh`'s `unmanaged-devices` only covers
`wlan0*`/`wg0`), so a genuinely working WAN atomic already has an address
from NetworkManager by the time this classifier runs `hal.dhcp
.attempt_client_lease()` on it. That method (`hal/linux.py`'s `LinuxDhcp`)
unconditionally ran its own `dhclient -1` on the interface — competing with
whatever already holds the interface's DHCP client role for the same
BOOTP/DHCP socket, which can spuriously fail or time out. A failed attempt
falls through to passive-listen (which also may hear nothing, since the
real DHCP server already answered the earlier, real request) and then to
`became_dhcp_server` — misclassifying a live, working WAN port as bare and
starting a rogue DHCP server on a real upstream segment.

**Fix:** `LinuxDhcp.attempt_client_lease()` now checks for an existing
usable address first (`_read_lease()`, already used post-`dhclient` to read
the result back) and returns it immediately without ever invoking
`dhclient` — an existing lease already satisfies the method's own contract
("a lease means an upstream network exists here"), so there is no reason to
negotiate a new one and risk the conflict. Scoped to `hal/linux.py` only;
`lanout/__init__.py`'s sequencing is unchanged.

**Test coverage:** `appliance/tests/test_hal_linux.py`
`TestAttemptClientLeaseSkipsWhenAlreadyAddressed` — an existing address
short-circuits without running `dhclient` at all (asserts on it directly),
and the no-address path still runs `dhclient` exactly as before. This is
real-OS-process-conflict behavior `MOCK_HW=1`/scenario tests structurally
cannot reproduce (same caveat `LinuxDhcp`'s own docstring already carries)
— `appliance/tests/e2e/`'s fabric/tunnel proof is the only thing that can
confirm this against real `dhclient`/NetworkManager interaction.

**Verification:** `appliance/tests/e2e/`'s fabric/tunnel proof (PR #48,
stage `18_tunnel_download_survives_chaos`) — should go from FAIL to PASS
with this fix plus both of item 16's fixes in place.

## Verification (every item)

```
MOCK_HW=1 PYTHONPATH=appliance/src python3 -m pytest appliance/tests/ -v
./run_all_tests.sh
```

Items 3, 4, 6 must show the relevant scenario test failing on the pre-fix
code and passing after — that's the point of item 3. Item 14 additionally
needs a hardware run before its `active-tests.md` entry can leave
`UNCONFIRMED`.
