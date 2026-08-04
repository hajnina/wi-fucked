# ADR-019 — LAN client egress routes through the tunnel, not the WAN directly

**Status:** Accepted
**Date:** 2026-08-04

## Context

[ADR-005](ADR-005-tunnel-is-mandatory.md) already decided that client sessions
terminate at the fabric so the client-visible IP survives a WAN swap, and
`docs/architecture.md:133-145` documents that as the product's behaviour today.
Neither promise was actually implemented for LAN traffic:

- `enforce/__init__.py`'s `render()` builds `RouteRule(fwmark=..., table=...,
  ifname=atomic.ifname)` — marked LAN traffic is policy-routed straight out
  the WAN atomic's interface, `wg0` never enters the picture.
- `tunnel/__init__.py`'s `_configure_interface()` sets WireGuard's
  `allowed-ips` to the fabric-assigned tunnel pool CIDR (e.g.
  `10.99.0.0/24`), which is correct for point-to-point tunnel-pool traffic
  but too narrow to carry default-route traffic even if something did route
  it onto `wg0` — WireGuard's crypto-routing drops any packet whose
  destination doesn't match a peer's `allowed-ips`.
- `fabric/src/fabric/wireguard.py`'s `add_peer()` only pins a peer's
  `allowed-ips` to its own `/32` tunnel address (correct, for validating what
  the fabric accepts *from* that peer) and does nothing to forward or NAT
  traffic *from* that peer onward to the Internet.
- There is no `ip_forward` or NAT anywhere in `appliance/` or `fabric/`
  (confirmed by grep across both trees as of this ADR).

So today a LAN client's traffic goes straight out whichever WAN is active,
NAT-less (in fact unrouted beyond the local WAN link, since there's no NAT
either — traffic egressing an atomic like this would need the WAN's own
NAT/DHCP-assigned public IP behaviour, and even that path was never
completed with a `masquerade` rule). Two architectural promises are broken at
once: the client-visible IP does *not* survive a WAN swap (ADR-005), and
every WAN atomic's ISP would see LAN client traffic in the clear rather than
tunnelled, which `architecture.md:143-145` explicitly calls out as the thing
the fabric tunnel exists to prevent ("WANs are hostile... LAN services never
exposed through an arbitrary WAN").

This item (backlog item 5, `docs/backlog/traffic-blockers.md`) exists to pick
one of the two consistent designs and make the code match it everywhere.

## Decision

**LAN client egress routes through `wg0` to the fabric.** The fabric decrypts,
forwards, and NATs it out its own WAN. This is not a new promise — it is
finishing the implementation of ADR-005, which already documented this as the
product's behaviour.

Concretely:

- `enforce/__init__.py` `render()` installs each `RouteRule`'s default route
  against the tunnel interface (`wg0`), not the WAN atomic's `ifname`. The
  per-atomic routing table scheme from backlog item 4 is unchanged — each
  atomic still gets its own deterministic table via `_table_for_atomic()` —
  but every table's default route now points at the same tunnel device. The
  tables remain atomic-scoped because CAKE shaping (a separate concern, keyed
  by `atomic.ifname`) and any future per-atomic routing policy still need
  that granularity; only the *next hop* for marked LAN traffic changes.
- `tunnel/__init__.py` `_configure_interface()` sets the fabric peer's
  `allowed-ips` to `0.0.0.0/0` instead of the tunnel pool CIDR, so WireGuard's
  crypto-routing accepts and encrypts default-route traffic, not just packets
  addressed inside the tunnel pool. `bind_to()`'s host route for the fabric
  endpoint's real address is unaffected — it governs how the *WireGuard
  transport itself* reaches the fabric across a WAN swap, a different
  concern from what LAN IP rules point at.
- `fabric/src/fabric/wireguard.py`'s `FabricWireGuard.ensure_ready()` now also
  enables `net.ipv4.ip_forward=1` and installs an `nftables` `postrouting`
  masquerade rule for traffic sourced from RFC1918 space.
- `add_peer()` widens each peer's `allowed-ips` from just its own `/32` to
  its `/32` plus the full RFC1918 space. **This was not the original plan
  for this PR** — it was assumed a peer's own tunnel address was enough,
  matching `add_peer`'s original docstring ("pinned to a single tunnel
  address"). The QEMU packet-routing proof (`appliance/tests/qemu/`) caught
  this as a real, reproducible failure, not a theoretical one: WireGuard's
  crypto-routing validates a decrypted packet's *source* address against the
  sending peer's `allowed-ips`, and a LAN client's forwarded packet carries
  the client's own private LAN address (e.g. `192.168.60.2`), never the
  peer's `/32` tunnel address (e.g. `10.99.0.2`). Pinned to `/32`, every
  single LAN client packet was silently dropped by the kernel the moment it
  reached the fabric — reachable only by sending a real packet through a
  real kernel and watching it vanish, which is exactly the gap a
  MOCK_HW-only scenario test cannot see (nothing in `MockEnforcer`/
  `MockTunnel` models WireGuard's crypto-routing at all). See the PR body's
  QEMU verification section for the exact command and log evidence.
- Two further real bugs, also found only by the QEMU proof and also not
  originally planned for this PR: `FabricWireGuard.ensure_ready()` now also
  installs `ip route replace <RFC1918 block> dev wg0` for each of the same
  three blocks — bare `wg` (unlike `wg-quick`) never installs kernel routes
  on its own, so without this the kernel had no reason to ever hand a reply
  packet to `wg0` at all, regardless of how correctly `allowed-ips` or the
  NAT rule were configured. And `net.ipv4.conf.default.forwarding` needs
  setting explicitly (not just `net.ipv4.ip_forward`, i.e.
  `conf.all.forwarding`) because `wg0` is created *after* boot — a write to
  `conf.all.forwarding` at boot time only propagates to interfaces that
  already exist at that moment. Both are fabric-guest/test-topology findings
  documented in `appliance/tests/qemu/`'s own comments and
  `fabric/src/fabric/wireguard.py`'s docstrings, not appliance-side changes.
- **What the QEMU proof did not confirm**: even with all of the above fixed,
  a full round-trip (reply routed all the way back through the fabric, back
  through WireGuard, back to the LAN client) was not achieved in the time
  available. Every piece of state on the fabric was independently confirmed
  correct by direct kernel inspection (`ip route get` resolves to `wg0`, the
  peer's `allowed-ips` covers the destination, the NAT rule's syntax and
  match are correct) — but WireGuard's own "sent" transfer counter never
  advances for the reply. See `docs/active-tests.md`'s ADR-019 entry for the
  full detail, including why this may be a limitation specific to the
  sandbox this was built in rather than the code itself, and exactly what
  running this again should confirm.

## Consequences

**Easier:**

- ADR-005's promise ("the client-visible IP is the fabric's and never changes
  when a WAN does") is now actually true for LAN client traffic, not just for
  the appliance's own control-plane calls to the fabric.
- A WAN swap becomes invisible below the tunnel: `enforce.render()`'s output
  for LAN routing doesn't change shape when the active atomic changes — only
  `tunnel.bind_to()`'s host route for the fabric endpoint moves. Fewer moving
  parts change per failover event.
- One security boundary (the tunnel) instead of a trust decision per WAN,
  consistent with `architecture.md:143-145`.

**Harder:**

- **The fabric is now in the forwarding path for every byte a LAN client
  sends**, not just tunnel-management traffic. A saturated or down fabric
  now means *no Internet at all* for LAN clients, not merely a stale
  `/health` response — this was already true in spirit per ADR-005's
  "must stay true" clause, but this PR is what makes it literally true in
  the data plane.
- WireGuard's per-packet crypto-routing check (`allowed-ips`) is now the
  same trust boundary as `nft`'s marking rules — a widened `allowed-ips`
  is a meaningful security control, not a routing nicety. Any future change
  narrowing it back down (e.g. split-tunnel, direct-to-WAN for specific
  low-value traffic) is itself an architectural decision, not a config
  tweak.
- **New residual risk, accepted for MVP scope:** widening every peer's
  `allowed-ips` to the full RFC1918 space (needed so LAN client traffic
  isn't dropped, see above) means one peer could source-spoof another
  peer's tunnel address, since the tunnel pool (`10.99.0.0/24` by default)
  is itself inside `10.0.0.0/8`. Connection-oriented and conntrack-tracked
  exchanges still route replies to the genuine owner, bounding the
  practical impact, but this is real, not hypothetical, and worse on a
  multi-tenant fabric than a single-appliance one. ADR-005 already scopes
  multi-server/multi-tenant fabric to Phase 2; hardening this (e.g.
  per-peer nftables source validation keyed to the exact assigned address
  for LAN-client-facing chains specifically, leaving the wider RFC1918
  allowance only where genuinely needed) belongs there, not in this PR.
- The fabric container now needs `NET_ADMIN` for `sysctl`/`nft`, in addition
  to the `NET_ADMIN` it already needed for `wg`/`ip`. No new capability grant
  is required (the existing grant already covers this), but it is now used
  for more than interface setup.
- CPU cost of NAT and forwarding on the fabric side, additive to the existing
  WireGuard encryption cost already documented in ADR-005.

**Must stay true:** the fabric container's `NET_ADMIN` capability keeps
covering `sysctl net.ipv4.ip_forward` and `nft` (both already covered per
Dockerfile comments on `wg`/`ip`); the tunnel pool stays IPv4-only (NAT is
implemented for IPv4 only, matching `fabric/src/fabric/peers.py`'s
`10.99.0.0/24` pool — this ADR does not add IPv6 support anywhere it wasn't
already absent).

## Alternatives considered

**WAN-direct with NAT on the appliance** — rejected. It would require
amending or superseding ADR-005's already-documented and already-shipped
promise that the client-visible IP survives a WAN swap — a much larger and
riskier change to make inside what is otherwise a bug-fix-shaped PR. It would
also mean every WAN atomic's ISP (including hostile public Wi-Fi, which
`architecture.md` explicitly calls out) sees LAN client traffic directly,
which is exactly what the fabric tunnel exists to prevent. If a future
product need genuinely requires split-tunnel or direct-WAN egress for
specific traffic classes, that is real enough to deserve its own ADR
superseding this one — not a default assumed here.

**Leave `allowed-ips` at the tunnel pool CIDR and NAT only tunnel-pool
traffic on the fabric** — rejected. LAN client traffic is addressed to
arbitrary Internet destinations, not to the tunnel pool; WireGuard would drop
it at the crypto-routing check before it ever reached the fabric's NAT rule.
This isn't a narrower version of the decision, it's a configuration that
silently drops all LAN client traffic while looking like it should work.

**Per-atomic tunnel interfaces (`wg0`, `wg1`, ... one per WAN)** — rejected.
It would let CAKE shaping and tunnel egress share one interface each and
remove the "which table means what" indirection this ADR keeps, but it
multiplies WireGuard sessions (and re-handshakes) per WAN swap instead of
reusing the single stable session that makes a WAN swap invisible in the
first place — reintroducing exactly the visible-outage problem ADR-005 exists
to prevent.
