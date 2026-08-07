"""Enforcement — render an Allocation into kernel state.

Two rules govern this module, and both are easy to get backwards:

**Reconciliation, not command** (ADR-007). Declare desired state; read actual
state; apply the difference. Never assume a rule you installed is still there —
an interface bounce, NetworkManager, a debugging session, or a daemon restart
will all have removed it.

**Never tear down** (ADR-008). There is no cleanup path here. No ``atexit``, no
``finally`` that flushes, no shutdown handler that removes qdiscs. Kernel state
outlives the process deliberately, so that a control-plane crash degrades
adaptivity rather than causing an outage. "Converged" therefore means *every
desired rule is present*, never *the kernel holds exactly this and nothing else*
— leftover state from a previous allocation is last-known-good, not garbage.

`enforce` is the only module permitted to invoke ``tc``, ``nft``, or ``ip``.

WS-D owns this module.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol

from wifucked.allocator import Allocation
from wifucked.atomics.model import Atomic, PortRole
from wifucked.lan import lan_ifname_for_profile
from wifucked.logging import get_logger
from wifucked.policy import BEST_EFFORT, DEFAULT_PROFILES, ServiceProfile

log = get_logger("enforce")

#: Name of the single nftables table this module owns. Everything it installs
#: lives here so the whole ruleset can be replaced atomically without touching
#: anyone else's rules.
_TABLE = "wifucked"

#: Name of the LAN-marking chain within `_TABLE`. Not "mark" — see
#: `_nft_ruleset()`'s comment for why that name is rejected by real nft.
_MARK_CHAIN = "lan_mark"

#: Policy-routing table numbers are handed out per atomic, not shared. Base of
#: the range and its width — 900 tables is comfortably beyond any realistic
#: atomic count and keeps the numbers away from low-numbered tables reserved
#: by the kernel (``local``/``main``/``default`` = 255/254/253) and anything an
#: operator might hand-add near the bottom of the space.
_TABLE_BASE = 100
_TABLE_SPAN = 900


def _table_for_atomic(atomic_id: str) -> int:
    """Deterministic, collision-resistant routing table number for one atomic.

    Derived from a stable hash of the atomic's id (ADR-002 identity — never
    `ifname`), not assigned sequentially. Sequential assignment would require
    remembering an id -> table mapping across ticks and restarts to stay
    stable for a given atomic; hashing the id directly gives the same table
    every time, for every process, with no state to carry. `hash()` is not
    used here because Python randomizes string hashing per-process
    (`PYTHONHASHSEED`), which would reassign every atomic's table on every
    daemon restart — wasteful (old rules become orphaned "leftover state",
    permitted by ADR-008 but pointless to create) even though not unsafe.
    `sha256` has no such randomization.
    """
    digest = hashlib.sha256(atomic_id.encode()).hexdigest()
    return _TABLE_BASE + (int(digest, 16) % _TABLE_SPAN)


@dataclass(frozen=True, slots=True)
class Shaping:
    """Desired CAKE shaping for one interface."""

    ifname: str
    down_bps: int
    up_bps: int
    diffserv: str = "diffserv4"


@dataclass(frozen=True, slots=True)
class RouteRule:
    """One policy-routing entry: fwmark → routing table → interface."""

    fwmark: int
    table: int
    ifname: str


@dataclass(frozen=True, slots=True)
class DesiredState:
    shaping: tuple[Shaping, ...]
    routes: tuple[RouteRule, ...]
    marks: tuple[tuple[int, int], ...]  # (vlan, fwmark)
    #: LAN-out ports (ADR-023): (ifname, fwmark) pairs for wired ports the
    #: DHCP-attempt/passive-listen pipeline switched into DHCP-server mode.
    #: Kept separate from `marks` because these ifnames are dynamic (a port
    #: can appear, disappear, or move which physical socket it's in) where
    #: the AP-VLAN marks `marks` otherwise carries are static per lan_mode —
    #: `_nft_ruleset()` needs the real ifname to build a rule from, which
    #: `marks` alone (vlan/fwmark only) cannot supply.
    lan_out_marks: tuple[tuple[str, int], ...] = ()

    def key(self) -> tuple:
        return (self.shaping, self.routes, self.marks, self.lan_out_marks)


class Enforcer(Protocol):
    def reconcile(self, desired: DesiredState) -> None: ...
    def actual(self) -> DesiredState | None: ...
    def raw_dump(self) -> dict[str, str]: ...


#: Default tunnel interface name, matching `tunnel.WireGuardTunnel`'s default.
#: `render()` accepts an override so callers stay driven by the actual
#: configured tunnel interface (`config.fabric.interface`) rather than two
#: modules independently agreeing on a string.
_DEFAULT_TUNNEL_IFNAME = "wg0"


def render(
    allocation: Allocation,
    atomics: dict[str, Atomic],
    profiles: tuple[ServiceProfile, ...] = DEFAULT_PROFILES,
    tunnel_ifname: str = _DEFAULT_TUNNEL_IFNAME,
) -> DesiredState:
    """Turn an allocation into the kernel state that would implement it.

    LAN client egress is tunnel-owned (ADR-019): every marked route's default
    hop is the tunnel interface, never the WAN atomic's own `ifname`. The
    atomic-keyed routing table still exists per share (`_table_for_atomic`) —
    that grouping remains useful for CAKE shaping and any future per-atomic
    routing policy — but the next hop it points at is always the tunnel, so a
    WAN swap changes only which atomic carries the tunnel
    (`tunnel.bind_to`), not the shape of anything this function produces.
    """
    shaping: list[Shaping] = []
    routes: list[RouteRule] = []

    # LAN-origin marking is static: a profile's traffic is marked by the LAN
    # interface it arrives on, whether or not a WAN atomic currently serves it.
    # Dropping the mark when a profile has no active share would leave that
    # traffic unclassified the moment an atomic appeared to steer it.
    marks: list[tuple[int, int]] = [(p.vlan, p.vlan) for p in profiles]

    # LAN-out ports (ADR-023): a wired port the DHCP-attempt/passive-listen
    # pipeline switched into DHCP-server mode gets the same treatment as an
    # AP LAN client — marked BEST_EFFORT (the class ADR-020's default hotspot
    # mode already uses for undifferentiated LAN traffic) and, via that
    # mark's fwmark, routed through the tunnel by whichever RouteRule the
    # BEST_EFFORT share above already installs. No separate routing table or
    # route is needed here: the fwmark is what ties the two together.
    lan_out_marks: list[tuple[str, int]] = sorted(
        {
            (atomic.ifname, BEST_EFFORT.vlan)
            for atomic in atomics.values()
            if atomic.role is PortRole.LAN_OUT and atomic.present and atomic.ifname
        }
    )
    if lan_out_marks:
        marks.append((BEST_EFFORT.vlan, BEST_EFFORT.vlan))

    for share in allocation.shares:
        if share.ceiling_bps == 0:
            # "0 means not routed here" (Share's own docstring) — installing a
            # route anyway would steer a profile's traffic onto an atomic the
            # allocator explicitly decided should carry none of it (e.g. a
            # best-effort class that isn't permitted to use BACKUP).
            continue
        # The allocator's contract (`allocator._build`) is that a quiesced
        # atomic never appears in `shares` at all — quiescing and sharing are
        # mutually exclusive outcomes for a given atomic on a given tick. This
        # check makes that contract load-bearing here rather than merely
        # assumed: if it ever breaks, `render()` fails loudly instead of
        # silently installing a route for traffic that was supposed to be
        # withheld.
        if share.atomic_id in allocation.quiesced:
            raise ValueError(
                f"atomic {share.atomic_id!r} is both quiesced and shared — "
                "allocator contract violated"
            )
        atomic = atomics.get(share.atomic_id)
        if atomic is None or not atomic.ifname:
            continue
        profile = next((p for p in profiles if p.name == share.profile_name), None)
        if profile is None:
            continue
        fwmark = profile.vlan
        table = _table_for_atomic(share.atomic_id)
        # The next hop is the tunnel (ADR-019), not `atomic.ifname` — the WAN
        # atomic still gates whether this route exists at all (no atomic, no
        # ifname, no route), it just no longer supplies the egress device.
        routes.append(RouteRule(fwmark=fwmark, table=table, ifname=tunnel_ifname))

    for atomic_id in {s.atomic_id for s in allocation.shares}:
        atomic = atomics.get(atomic_id)
        if atomic is None or not atomic.ifname or not atomic.capacity.known:
            continue
        shaping.append(
            Shaping(
                ifname=atomic.ifname,
                # Shape slightly under measured capacity so the queue lives here,
                # where CAKE can manage it, rather than in the ISP's buffer.
                down_bps=int(atomic.capacity.down_bps * 0.95),
                up_bps=int(atomic.capacity.up_bps * 0.95),
            )
        )

    return DesiredState(
        shaping=tuple(sorted(shaping, key=lambda s: s.ifname)),
        routes=tuple(sorted(set(routes), key=lambda r: (r.fwmark, r.ifname))),
        marks=tuple(sorted(set(marks))),
        lan_out_marks=tuple(lan_out_marks),
    )


class MockEnforcer(Enforcer):
    """Records what would have been applied. Used by MOCK_HW and scenario tests."""

    def __init__(self) -> None:
        self.applied: list[DesiredState] = []
        self._actual: DesiredState | None = None

    def reconcile(self, desired: DesiredState) -> None:
        if self._actual is not None and self._actual.key() == desired.key():
            return
        self.applied.append(desired)
        self._actual = desired
        log.debug(
            "Reconciled kernel state (mock)",
            extra={
                "workflow": "enforce_reconcile",
                "state": "completed",
                "intent": "apply the allocator's decision to the data plane",
                "shaped_interfaces": len(desired.shaping),
                "route_rules": len(desired.routes),
            },
        )

    def actual(self) -> DesiredState | None:
        return self._actual

    def bytes_on(self, ifname: str) -> int:
        """Bytes this interface was ever permitted to carry.

        Scenario tests use this to assert the BACKUP-carries-zero invariant.
        """
        return sum(
            s.down_bps + s.up_bps
            for state in self.applied
            for s in state.shaping
            if s.ifname == ifname
        )

    def raw_dump(self) -> dict[str, str]:
        """No real kernel to read under MOCK_HW; nothing to dump."""
        return {}


class LinuxEnforcer(Enforcer):
    """Programs tc/CAKE, nftables and policy routing.

    Note what is *absent*: any method that removes state. That is deliberate and
    load-bearing (ADR-008) — do not add one.
    """

    def __init__(
        self,
        dry_run: bool = False,
        lan_mode: str = "two_bss",
        base_interface: str = "wlan0",
        profiles: tuple[ServiceProfile, ...] = DEFAULT_PROFILES,
    ):
        self._dry_run = dry_run
        self._lan_mode = lan_mode
        self._base_interface = base_interface
        self._profiles = profiles
        #: Only consulted in dry-run, where nothing is really installed and there
        #: is no kernel to read back.
        self._actual: DesiredState | None = None

    def reconcile(self, desired: DesiredState) -> None:
        current = self.actual()
        if self._converged(current, desired):
            return

        started = time.monotonic()
        log.info(
            "Reconciling kernel state",
            extra={
                "workflow": "enforce_reconcile",
                "state": "started",
                "intent": "apply the allocator's decision to the data plane",
                "shaped_interfaces": len(desired.shaping),
                "route_rules": len(desired.routes),
                "marks": len(desired.marks),
                "dry_run": self._dry_run,
            },
        )

        for shaping in desired.shaping:
            self._apply_cake(shaping)
        self._apply_marks(desired.lan_out_marks)
        for route in desired.routes:
            self._apply_route(route)

        self._actual = desired
        log.info(
            "Reconciled kernel state",
            extra={
                "workflow": "enforce_reconcile",
                "state": "completed",
                "intent": "apply the allocator's decision to the data plane",
                "shaped_interfaces": len(desired.shaping),
                "route_rules": len(desired.routes),
                "marks": len(desired.marks),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "dry_run": self._dry_run,
            },
        )

    def actual(self) -> DesiredState | None:
        if self._dry_run:
            return self._actual
        return self._read_actual()

    def raw_dump(self) -> dict[str, str]:
        """Human-readable kernel state, for the diagnostics bundle and the
        periodic debug snapshot (``daemon._slow_loop``) — never for parsing.

        Read-only, same as everything else in this module (ADR-007); nothing
        here installs or removes state. Failures degrade to an empty string
        per command rather than raising, so one missing tool never blanks the
        rest of the dump.
        """
        return {
            "nft_ruleset": self._exec(
                ["nft", "list", "ruleset"],
                workflow="diagnostics_dump",
                intent="capture nftables marking state for support",
            )
            or "",
            "tc_qdisc": self._exec(
                ["tc", "-s", "qdisc", "show"],
                workflow="diagnostics_dump",
                intent="capture CAKE shaping and drop/backlog stats for support",
            )
            or "",
            "ip_rule": self._exec(
                ["ip", "rule", "show"],
                workflow="diagnostics_dump",
                intent="capture policy routing state for support",
            )
            or "",
            "ip_route": self._exec(
                ["ip", "route", "show", "table", "all"],
                workflow="diagnostics_dump",
                intent="capture per-atomic routing tables for support",
            )
            or "",
        }

    # -- convergence ----------------------------------------------------------

    def _converged(self, current: DesiredState | None, desired: DesiredState) -> bool:
        """Is every piece of ``desired`` already present in ``current``?

        Subset, not equality: ADR-008 forbids tearing down, so the kernel may
        legitimately hold leftover rules from a previous allocation. Requiring
        exact equality would mean those leftovers block convergence forever and
        we would re-program every tick.
        """
        if current is None:
            return not (desired.shaping or desired.routes or desired.marks)

        current_shaping = {s.ifname: s for s in current.shaping}
        for want in desired.shaping:
            have = current_shaping.get(want.ifname)
            # CAKE renders the shaped rate to a rounded string (e.g. "95Mbit"),
            # so an exact bit-for-bit comparison would rarely converge. Compare
            # within a tolerance and re-shape only on a meaningful change.
            #
            # `_apply_cake` programs `up_bps` (egress is bounded by upload
            # capacity, see `_apply_cake`'s docstring), and `_read_shaping`
            # reads that same applied rate back into `up_bps` — so the
            # convergence check must compare `up_bps`, not `down_bps`.
            # `down_bps` isn't actually enforced by anything in this codebase
            # today (no IFB device — see `_apply_cake`), so it can't be read
            # back or compared.
            if have is None or have.diffserv != want.diffserv:
                return False
            if not _close(have.up_bps, want.up_bps):
                return False

        current_routes = {(r.fwmark, r.table, r.ifname) for r in current.routes}
        if any((r.fwmark, r.table, r.ifname) not in current_routes for r in desired.routes):
            return False

        current_marks = set(current.marks)
        return all(m in current_marks for m in desired.marks)

    # -- shaping --------------------------------------------------------------

    def _apply_cake(self, shaping: Shaping) -> None:
        # `tc qdisc ... dev <ifname> root` shapes *egress* off this box, which is
        # bounded by our upload capacity, not our download capacity — using
        # `down_bps` here shaped the wrong direction entirely. True ingress
        # (download) shaping isn't implemented anywhere in this codebase: CAKE
        # can only shape the direction traffic leaves an interface, so shaping
        # download would need an IFB (Intermediate Functional Block) device to
        # redirect ingress through an egress qdisc, and nothing here creates
        # one. Flagged as a known gap (see PR body) rather than added here —
        # this fix corrects the direction that was actively wrong; adding a
        # whole new virtual-device lifecycle is a separate change.
        argv = [
            "tc",
            "qdisc",
            "replace",
            "dev",
            shaping.ifname,
            "root",
            "cake",
            "bandwidth",
            f"{shaping.up_bps}bit",
            shaping.diffserv,
        ]
        if self._dry_run:
            log.info(
                "Would apply CAKE",
                extra={
                    "workflow": "enforce_shaping",
                    "state": "skipped",
                    "intent": "shape egress to measured upload capacity",
                    "ifname": shaping.ifname,
                    "target_bps": shaping.up_bps,
                    "reason": "dry run",
                },
            )
            return
        self._exec(
            argv,
            workflow="enforce_shaping",
            intent="shape egress to measured upload capacity",
            ifname=shaping.ifname,
            target_bps=shaping.up_bps,
        )

    # -- marking --------------------------------------------------------------

    def _apply_marks(self, lan_out_marks: tuple[tuple[str, int], ...] = ()) -> None:
        """Install the static LAN-origin marking ruleset, plus any dynamic
        LAN-out port marks (ADR-023).

        The AP-VLAN mapping (which LAN interface carries which profile) does
        not change per allocation, so the simplest correct thing is to
        declare the whole ``wifucked`` table every reconcile and let nft
        replace it atomically. ``lan_out_marks`` is genuinely dynamic — a
        port can appear or disappear between reconciles — so it comes from
        ``desired`` each call rather than being baked in at construction
        like ``self._profiles``/``self._lan_mode`` are. The leading
        ``table`` + ``flush table`` lines make the redeclaration idempotent:
        the add creates the table if absent so the flush cannot fail, the
        flush empties it, and the body re-populates it — all inside a
        single ``nft -f -`` transaction, so there is no window where marking is
        absent (nftables wiki, "Atomic rule replacement").
        """
        ruleset = self._nft_ruleset(lan_out_marks)
        if self._dry_run:
            log.info(
                "Would apply nftables marking",
                extra={
                    "workflow": "enforce_marking",
                    "state": "skipped",
                    "intent": "mark LAN traffic by originating interface",
                    "profiles": len(self._profiles),
                    "lan_out_ports": len(lan_out_marks),
                    "reason": "dry run",
                },
            )
            return
        self._exec(
            ["nft", "-f", "-"],
            workflow="enforce_marking",
            intent="mark LAN traffic by originating interface",
            stdin=ruleset,
            profiles=len(self._profiles),
            lan_out_ports=len(lan_out_marks),
        )

    def _nft_ruleset(self, lan_out_marks: tuple[tuple[str, int], ...] = ()) -> str:
        lines = [
            f"table inet {_TABLE}",
            f"flush table inet {_TABLE}",
            f"table inet {_TABLE} {{",
            # Not named "mark": that collides with an nftables grammar
            # keyword (the `meta mark set` statement itself), and real
            # `nft` — confirmed via the QEMU packet-routing proof in
            # appliance/tests/qemu/, not by inspection — rejects a chain
            # literally named `mark` with a syntax error, even quoted. This
            # was a real, standing bug: `_apply_marks()` has been failing
            # (gracefully, per ADR-008 — logged and swallowed, not crashed)
            # on every real box that ever ran this, since before this PR.
            f"    chain {_MARK_CHAIN} {{",
            "        type filter hook prerouting priority mangle; policy accept;",
        ]
        for profile in self._profiles:
            ifname = lan_ifname_for_profile(profile, self._lan_mode, self._base_interface)
            lines.append(f'        iifname "{ifname}" meta mark set {profile.vlan}')
        for ifname, fwmark in lan_out_marks:
            # LAN-out ports (ADR-023): a wired port serving DHCP outward,
            # marked the same way an AP-side VLAN interface is above — just
            # keyed by a dynamic ifname instead of a static VLAN subinterface.
            lines.append(f'        iifname "{ifname}" meta mark set {fwmark}')
        lines += ["    }", "}", ""]
        return "\n".join(lines)

    # -- routing --------------------------------------------------------------

    def _apply_route(self, route: RouteRule) -> None:
        # A deterministic priority makes the rule idempotent: re-adding it fails
        # with "File exists" (which we tolerate) rather than piling up a
        # duplicate every tick — exactly the persistent bug ADR-007 warns about.
        pref = 10000 + route.fwmark
        rule_argv = [
            "ip",
            "rule",
            "add",
            "fwmark",
            str(route.fwmark),
            "lookup",
            str(route.table),
            "priority",
            str(pref),
        ]
        route_argv = [
            "ip",
            "route",
            "replace",
            "default",
            "dev",
            route.ifname,
            "table",
            str(route.table),
        ]
        if self._dry_run:
            log.info(
                "Would apply policy route",
                extra={
                    "workflow": "enforce_routing",
                    "state": "skipped",
                    "intent": "steer marked traffic to its atomic's table",
                    "fwmark": route.fwmark,
                    "table": route.table,
                    "ifname": route.ifname,
                    "reason": "dry run",
                },
            )
            return
        self._exec(
            rule_argv,
            workflow="enforce_routing",
            intent="steer marked traffic to its atomic's table",
            tolerate_exists=True,
            fwmark=route.fwmark,
            table=route.table,
        )
        self._exec(
            route_argv,
            workflow="enforce_routing",
            intent="install the default route for this atomic's table",
            fwmark=route.fwmark,
            table=route.table,
            ifname=route.ifname,
        )

    # -- reading actual state -------------------------------------------------

    def _read_actual(self) -> DesiredState | None:
        shaping = self._read_shaping()
        marks = self._read_marks()
        routes = self._read_routes()
        if shaping is None and marks is None and routes is None:
            # Could not read anything — treat as "unknown", forcing reconcile to
            # apply. Applying is idempotent, so a failed read never hurts.
            return None
        return DesiredState(
            shaping=tuple(sorted(shaping or (), key=lambda s: s.ifname)),
            routes=tuple(sorted(routes or (), key=lambda r: (r.fwmark, r.ifname))),
            marks=tuple(sorted(marks or ())),
        )

    def _read_shaping(self) -> list[Shaping] | None:
        out = self._exec(
            ["tc", "-j", "qdisc", "show"],
            workflow="enforce_readback",
            intent="read installed qdiscs to diff against desired",
        )
        if out is None:
            return None
        try:
            entries = json.loads(out)
        except json.JSONDecodeError as exc:
            _log_parse_failure("tc", exc)
            return None
        result: list[Shaping] = []
        for qdisc in entries:
            if qdisc.get("kind") != "cake":
                continue
            dev = qdisc.get("dev")
            options = qdisc.get("options") or {}
            up_bps = _parse_rate(options.get("bandwidth"))
            if not dev or up_bps is None:
                continue
            result.append(
                Shaping(
                    ifname=dev,
                    down_bps=0,  # not enforced anywhere yet — no IFB device, see _apply_cake.
                    up_bps=up_bps,  # the single egress rate CAKE actually shapes.
                    diffserv=str(options.get("diffserv", "diffserv4")),
                )
            )
        return result

    def _read_marks(self) -> list[tuple[int, int]] | None:
        out = self._exec(
            ["nft", "-j", "list", "ruleset"],
            workflow="enforce_readback",
            intent="read installed nft marking rules to diff against desired",
        )
        if out is None:
            return None
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            _log_parse_failure("nft", exc)
            return None
        marks: list[tuple[int, int]] = []
        for item in data.get("nftables", []):
            rule = item.get("rule")
            if not rule or rule.get("table") != _TABLE:
                continue
            iifname: str | None = None
            markval: int | None = None
            for expr in rule.get("expr", []):
                match = expr.get("match")
                if match and _meta_key(match.get("left")) == "iifname":
                    iifname = match.get("right")
                mangle = expr.get("mangle")
                if mangle and _meta_key(mangle.get("key")) == "mark":
                    markval = mangle.get("value")
            if iifname is None or markval is None:
                continue
            try:
                fwmark = int(markval)
            except ValueError:
                continue
            try:
                # AP-VLAN interfaces are named `<bss>.<vlan>` — the vlan
                # number is the suffix (see `lan_ifname_for_profile`).
                vlan = int(str(iifname).rsplit(".", 1)[1])
            except (ValueError, IndexError):
                # A LAN-out port's ifname (ADR-023, e.g. "eth1") carries no
                # such suffix. Both this codebase's AP-VLAN marks and its
                # LAN-out marks always set vlan == fwmark (see `render()`),
                # so falling back to the mark value itself keeps this tuple
                # comparable against `DesiredState.marks` for convergence —
                # imperfect (it can't distinguish a genuinely wrong vlan
                # from a missing suffix), but correct for every mark this
                # codebase actually produces today.
                vlan = fwmark
            marks.append((vlan, fwmark))
        return marks

    def _read_routes(self) -> list[RouteRule] | None:
        out = self._exec(
            ["ip", "-j", "rule", "show"],
            workflow="enforce_readback",
            intent="read installed ip rules to diff against desired",
        )
        if out is None:
            return None
        try:
            rules = json.loads(out)
        except json.JSONDecodeError as exc:
            _log_parse_failure("ip rule", exc)
            return None
        result: list[RouteRule] = []
        for rule in rules:
            fwmark = _parse_int(rule.get("fwmark"))
            table = _parse_int(rule.get("table"))
            if fwmark is None or table is None:
                continue
            dev = self._read_table_default_dev(table)
            if dev is None:
                continue
            result.append(RouteRule(fwmark=fwmark, table=table, ifname=dev))
        return result

    def _read_table_default_dev(self, table: int) -> str | None:
        out = self._exec(
            ["ip", "-j", "route", "show", "table", str(table)],
            workflow="enforce_readback",
            intent="read a routing table's default route to diff against desired",
        )
        if out is None:
            return None
        try:
            routes = json.loads(out)
        except json.JSONDecodeError as exc:
            _log_parse_failure("ip route", exc)
            return None
        for route in routes:
            if route.get("dst") == "default":
                return route.get("dev")
        return None

    # -- command runner -------------------------------------------------------

    def _exec(
        self,
        argv: list[str],
        *,
        workflow: str,
        intent: str,
        stdin: str | None = None,
        tolerate_exists: bool = False,
        **ctx: object,
    ) -> str | None:
        """Run a command, returning stdout or None. Never raises.

        Failures are logged and swallowed — a control-plane hiccup keeps the
        last-known-good kernel state rather than crashing the daemon (ADR-008).
        """
        try:
            done = subprocess.run(
                argv, input=stdin, capture_output=True, text=True, timeout=10, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.error(
                "Command failed to spawn; keeping previous kernel state",
                extra={
                    "workflow": workflow,
                    "state": "failed",
                    "intent": intent,
                    "argv": argv,
                    "reason": "could not spawn process",
                    "error": str(exc),
                    **ctx,
                },
                exc_info=True,
            )
            return None

        if done.returncode != 0:
            stderr = (done.stderr or "").strip()
            if tolerate_exists and "exist" in stderr.lower():
                # The rule is already installed — the idempotent happy path.
                log.debug(
                    "Rule already present; nothing to do",
                    extra={
                        "workflow": workflow,
                        "state": "skipped",
                        "intent": intent,
                        "argv": argv,
                        "reason": "rule already installed",
                        **ctx,
                    },
                )
                return None
            log.warning(
                "Command returned non-zero; keeping previous kernel state",
                extra={
                    "workflow": workflow,
                    "state": "failed",
                    "intent": intent,
                    "argv": argv,
                    "returncode": done.returncode,
                    "reason": stderr[:200],
                    **ctx,
                },
            )
            return None
        return done.stdout


def _meta_key(node: object) -> str | None:
    """Extract ``meta.key`` from an nft JSON expression operand, if present."""
    if isinstance(node, dict):
        meta = node.get("meta")
        if isinstance(meta, dict):
            return meta.get("key")
    return None


def _parse_int(value: object) -> int | None:
    """Parse an int that iproute2 may render as a decimal or ``0x`` hex string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 16) if text.lower().startswith("0x") else int(text)
        except ValueError:
            return None
    return None


def _parse_rate(value: object) -> int | None:
    """Parse a CAKE bandwidth field into bits per second.

    ``tc -j`` renders the shaped rate as a unit-suffixed string ("95Mbit") and
    reports an unset shaper as "unlimited". Returns None when the rate is
    unknown, which forces a re-apply rather than a false match.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if not text or text == "unlimited":
        return None
    units = {
        "tbit": 1_000_000_000_000,
        "gbit": 1_000_000_000,
        "mbit": 1_000_000,
        "kbit": 1_000,
        "gibit": 1024**3,
        "mibit": 1024**2,
        "kibit": 1024,
        "bit": 1,
    }
    for suffix in sorted(units, key=len, reverse=True):
        if text.endswith(suffix):
            number = text[: -len(suffix)].strip()
            try:
                return int(float(number) * units[suffix])
            except ValueError:
                return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _close(a: int, b: int, tolerance: float = 0.02) -> bool:
    """Whether two bandwidths are equal within CAKE's rendering resolution."""
    if a == b:
        return True
    ceiling = max(abs(a), abs(b))
    return abs(a - b) <= ceiling * tolerance


def _log_parse_failure(tool: str, exc: Exception) -> None:
    log.warning(
        "Could not parse kernel state; assuming divergence",
        extra={
            "workflow": "enforce_readback",
            "state": "failed",
            "intent": "read actual kernel state to diff against desired",
            "tool": tool,
            "reason": "malformed JSON output",
            "error": str(exc),
        },
    )
