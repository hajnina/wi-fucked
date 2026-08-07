"""Unit tests for the real (non-mock) LinuxEnforcer.

LinuxEnforcer shells out to ``tc``/``nft``/``ip`` — tools that are absent in CI
and would need root and a real kernel. So these tests mock ``subprocess.run``
and assert two things directly:

* the exact argv / stdin the enforcer *produces* for known desired state, and
* that ``actual()`` correctly *parses* representative JSON back into a
  DesiredState, so reconciliation diffs against reality.

This is the behaviour SOP-003 wants covered for ``enforce/``. The timeline-driven
scenario harness (``appliance/tests/scenarios/``) exercises allocator behaviour
through ``MockEnforcer`` and stays untouched; it cannot drive a real kernel.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from wifucked.allocator import Allocation, Share
from wifucked.atomics.model import Atomic, Capacity, Kind, Mode
from wifucked.enforce import (
    DesiredState,
    LinuxEnforcer,
    RouteRule,
    Shaping,
    _close,
    _parse_rate,
    _table_for_atomic,
    render,
)
from wifucked.policy import BEST_EFFORT, CRITICAL


def _completed(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _atomic(ifname: str = "wlan0", down: int = 10_000_000) -> Atomic:
    return Atomic(
        id="atomic-w",
        kind=Kind.WIFI,
        label="hotel wifi",
        mode=Mode.NORMAL,
        ifname=ifname,
        present=True,
        capacity=Capacity(down_bps=down, up_bps=down // 10, confidence=0.9, measured_at=0.0),
    )


# -- render ------------------------------------------------------------------


def test_render_emits_one_mark_per_profile_with_no_shares():
    desired = render(Allocation(primary_id=None, backup_active=False, shares=()), {})
    assert desired.marks == ((CRITICAL.vlan, CRITICAL.vlan), (BEST_EFFORT.vlan, BEST_EFFORT.vlan))
    assert desired.routes == ()
    assert desired.shaping == ()


def test_render_marks_are_independent_of_active_shares():
    atomic = _atomic()
    alloc = Allocation(
        primary_id=atomic.id,
        backup_active=False,
        shares=(Share(atomic.id, CRITICAL.name, 5_000_000),),
    )
    desired = render(alloc, {atomic.id: atomic})

    # Best-effort has no share, but its mark must still be present.
    assert (BEST_EFFORT.vlan, BEST_EFFORT.vlan) in desired.marks
    assert (CRITICAL.vlan, CRITICAL.vlan) in desired.marks
    # Routes remain allocation-derived, on this atomic's own table, but the
    # next hop is the tunnel (ADR-019), never the WAN atomic's own ifname.
    assert (
        RouteRule(fwmark=CRITICAL.vlan, table=_table_for_atomic(atomic.id), ifname="wg0")
        in desired.routes
    )
    # CAKE shaping is unaffected by ADR-019 — it still shapes the WAN
    # atomic's own physical interface, not the tunnel.
    assert any(
        s.ifname == "wlan0" and s.down_bps == int(10_000_000 * 0.95) for s in desired.shaping
    )


def test_render_routes_use_the_configured_tunnel_ifname():
    atomic = _atomic()
    alloc = Allocation(
        primary_id=atomic.id,
        backup_active=False,
        shares=(Share(atomic.id, CRITICAL.name, 5_000_000),),
    )
    desired = render(alloc, {atomic.id: atomic}, tunnel_ifname="wg7")

    assert all(r.ifname == "wg7" for r in desired.routes)


def test_render_derives_a_distinct_table_per_atomic():
    a = _atomic(ifname="wlan0")
    b = Atomic(
        id="atomic-b",
        kind=Kind.USB_TETHER,
        label="phone",
        mode=Mode.BACKUP,
        ifname="usb0",
        present=True,
        capacity=Capacity(down_bps=20_000_000, up_bps=8_000_000, confidence=0.9, measured_at=0.0),
    )
    alloc = Allocation(
        primary_id=a.id,
        backup_active=True,
        shares=(
            Share(a.id, CRITICAL.name, 5_000_000),
            Share(b.id, CRITICAL.name, 5_000_000),
        ),
    )
    desired = render(alloc, {a.id: a, b.id: b})

    # Both atomics' routes now point at the same tunnel interface (ADR-019),
    # so the table — not the ifname — is what must stay per-atomic distinct.
    # Both shares carry CRITICAL, so distinguish them by atomic-derived table.
    tables = {r.table for r in desired.routes}
    assert tables == {_table_for_atomic(a.id), _table_for_atomic(b.id)}
    assert all(r.ifname == "wg0" for r in desired.routes)


def test_render_skips_zero_ceiling_shares():
    atomic = _atomic()
    alloc = Allocation(
        primary_id=atomic.id,
        backup_active=False,
        shares=(Share(atomic.id, CRITICAL.name, 0),),
    )
    desired = render(alloc, {atomic.id: atomic})

    assert desired.routes == ()


# -- nftables ruleset construction -------------------------------------------


def test_nft_ruleset_uses_atomic_flush_idiom_and_marks_per_bss():
    script = LinuxEnforcer(lan_mode="two_bss", base_interface="wlan0")._nft_ruleset()
    assert "table inet wifucked" in script
    assert "flush table inet wifucked" in script
    assert "type filter hook prerouting priority mangle; policy accept;" in script
    # two_bss: critical lives on the second BSS, best-effort on the base BSS.
    assert 'iifname "wlan0_1.10" meta mark set 10' in script
    assert 'iifname "wlan0.20" meta mark set 20' in script


def test_nft_ruleset_chain_is_not_named_mark():
    """A chain literally named `mark` is a syntax error to real `nft` — it
    collides with the `meta mark set` statement's own grammar. Confirmed by
    running this exact ruleset through `nft -c -f -` during the QEMU
    packet-routing proof (`appliance/tests/qemu/`), not by inspection: this
    was a real, standing bug where `_apply_marks()` silently failed (logged
    and swallowed per ADR-008, never crashed) on every real box that ever
    ran this code, since before this PR.
    """
    script = LinuxEnforcer(lan_mode="two_psk", base_interface="wlan0")._nft_ruleset()
    assert "chain mark {" not in script
    assert "chain lan_mark {" in script


def test_nft_ruleset_two_psk_puts_both_profiles_on_base_bss():
    script = LinuxEnforcer(lan_mode="two_psk", base_interface="wlan0")._nft_ruleset()
    assert 'iifname "wlan0.10" meta mark set 10' in script
    assert 'iifname "wlan0.20" meta mark set 20' in script


def test_apply_marks_feeds_the_ruleset_to_nft_stdin():
    captured: dict = {}

    def fake(argv, **kwargs):
        captured["argv"] = argv
        captured["input"] = kwargs.get("input")
        return _completed()

    with patch("wifucked.enforce.subprocess.run", side_effect=fake):
        LinuxEnforcer()._apply_marks()

    assert captured["argv"] == ["nft", "-f", "-"]
    assert "flush table inet wifucked" in captured["input"]
    assert "meta mark set 10" in captured["input"]


# -- shaping and routing command construction --------------------------------


def test_apply_cake_argv():
    calls: list[list[str]] = []

    def fake(argv, **kwargs):
        calls.append(argv)
        return _completed()

    with patch("wifucked.enforce.subprocess.run", side_effect=fake):
        # `tc qdisc ... root` shapes egress, which is bounded by upload
        # capacity — `down_bps` here is deliberately different from
        # `up_bps` so this test would fail if the wrong field were used.
        LinuxEnforcer()._apply_cake(
            Shaping("wlan0", down_bps=1_000_000, up_bps=9_500_000, diffserv="diffserv4")
        )

    assert calls[0] == [
        "tc",
        "qdisc",
        "replace",
        "dev",
        "wlan0",
        "root",
        "cake",
        "bandwidth",
        "9500000bit",
        "diffserv4",
    ]


def test_apply_ingress_shaping_argv():
    """ADR-025: download is shaped via an IFB redirect since CAKE can only
    shape the direction traffic leaves an interface. `ifb-f2fb6f66` is
    `_ifb_for_ifname("wlan0")`.
    """
    calls: list[list[str]] = []

    def fake(argv, **kwargs):
        calls.append(argv)
        return _completed()

    with patch("wifucked.enforce.subprocess.run", side_effect=fake):
        LinuxEnforcer()._apply_ingress_shaping(
            Shaping("wlan0", down_bps=40_000_000, up_bps=9_500_000, diffserv="diffserv4")
        )

    assert ["ip", "link", "add", "ifb-f2fb6f66", "type", "ifb"] in calls
    assert ["ip", "link", "set", "dev", "ifb-f2fb6f66", "up"] in calls
    assert ["tc", "qdisc", "add", "dev", "wlan0", "handle", "ffff:", "ingress"] in calls
    assert [
        "tc",
        "filter",
        "replace",
        "dev",
        "wlan0",
        "parent",
        "ffff:",
        "protocol",
        "all",
        "prio",
        "10",
        "u32",
        "match",
        "u32",
        "0",
        "0",
        "action",
        "mirred",
        "egress",
        "redirect",
        "dev",
        "ifb-f2fb6f66",
    ] in calls
    # down_bps, not up_bps — the whole point of the IFB redirect.
    assert [
        "tc",
        "qdisc",
        "replace",
        "dev",
        "ifb-f2fb6f66",
        "root",
        "cake",
        "bandwidth",
        "40000000bit",
        "diffserv4",
    ] in calls


def test_apply_ingress_shaping_tolerates_already_present_ifb_and_ingress_qdisc():
    def fake(argv, **kwargs):
        if argv[:3] == ["ip", "link", "add"] or argv[:3] == ["tc", "qdisc", "add"]:
            return _completed(returncode=2, stderr="RTNETLINK answers: File exists")
        return _completed()

    # Must not raise — re-running against an already-set-up IFB is the
    # idempotent happy path (ADR-007: every tick reconciles from scratch).
    with patch("wifucked.enforce.subprocess.run", side_effect=fake):
        LinuxEnforcer()._apply_ingress_shaping(
            Shaping("wlan0", down_bps=40_000_000, up_bps=9_500_000, diffserv="diffserv4")
        )


def test_apply_route_installs_rule_with_deterministic_priority_and_default_route():
    calls: list[list[str]] = []

    def fake(argv, **kwargs):
        calls.append(argv)
        return _completed()

    with patch("wifucked.enforce.subprocess.run", side_effect=fake):
        LinuxEnforcer()._apply_route(RouteRule(fwmark=10, table=100, ifname="usb0"))

    assert ["ip", "rule", "add", "fwmark", "10", "lookup", "100", "priority", "10010"] in calls
    assert ["ip", "route", "replace", "default", "dev", "usb0", "table", "100"] in calls


def test_apply_route_tolerates_an_already_present_rule():
    def fake(argv, **kwargs):
        if argv[:3] == ["ip", "rule", "add"]:
            return _completed(returncode=2, stderr="RTNETLINK answers: File exists")
        return _completed()

    # Must not raise — the duplicate-add is the idempotent happy path.
    with patch("wifucked.enforce.subprocess.run", side_effect=fake):
        LinuxEnforcer()._apply_route(RouteRule(fwmark=10, table=100, ifname="usb0"))


# -- reading actual kernel state ---------------------------------------------

_TC_JSON = """
[
  {"kind":"noqueue","handle":"0:","dev":"lo","root":true,"refcnt":2},
  {"kind":"cake","handle":"800d:","dev":"wlan0","root":true,"refcnt":2,
   "options":{"bandwidth":"95Mbit","diffserv":"diffserv4","flowmode":"triple-isolate"}},
  {"kind":"cake","handle":"800e:","dev":"ifb-f2fb6f66","root":true,"refcnt":2,
   "options":{"bandwidth":"500Kbit","diffserv":"diffserv4","flowmode":"triple-isolate"}}
]
"""

_NFT_JSON = """
{"nftables":[
  {"metainfo":{"version":"1.1.1","json_schema_version":1}},
  {"table":{"family":"inet","name":"wifucked","handle":1}},
  {"chain":{"family":"inet","table":"wifucked","name":"mark","handle":1,
            "type":"filter","hook":"prerouting","prio":-150,"policy":"accept"}},
  {"rule":{"family":"inet","table":"wifucked","chain":"mark","handle":2,"expr":[
     {"match":{"op":"==","left":{"meta":{"key":"iifname"}},"right":"wlan0_1.10"}},
     {"mangle":{"key":{"meta":{"key":"mark"}},"value":10}}]}},
  {"rule":{"family":"inet","table":"wifucked","chain":"mark","handle":3,"expr":[
     {"match":{"op":"==","left":{"meta":{"key":"iifname"}},"right":"wlan0.20"}},
     {"mangle":{"key":{"meta":{"key":"mark"}},"value":20}}]}}
]}
"""

_RULE_JSON = """
[
  {"priority":0,"src":"all","table":"local"},
  {"priority":10010,"src":"all","fwmark":"0xa","table":"100"},
  {"priority":10020,"src":"all","fwmark":"0x14","table":"100"},
  {"priority":32766,"src":"all","table":"main"}
]
"""

_ROUTE_JSON_100 = '[{"dst":"default","dev":"usb0","flags":[]}]'


def _read_dispatch(argv, **kwargs):
    """Answers read commands with representative JSON; anything else is empty."""
    if argv[:2] == ["tc", "-j"]:
        return _completed(_TC_JSON)
    if argv[:2] == ["nft", "-j"]:
        return _completed(_NFT_JSON)
    if argv[:4] == ["ip", "-j", "rule", "show"]:
        return _completed(_RULE_JSON)
    if argv[:4] == ["ip", "-j", "route", "show"]:
        table = argv[argv.index("table") + 1]
        return _completed(_ROUTE_JSON_100 if table == "100" else "[]")
    return _completed()


def test_actual_parses_real_kernel_json():
    with patch("wifucked.enforce.subprocess.run", side_effect=_read_dispatch):
        state = LinuxEnforcer().actual()

    assert state is not None
    # `up_bps` is the real interface's own egress CAKE (`_apply_cake`);
    # `down_bps` is read back from its paired IFB device's egress CAKE
    # (`_apply_ingress_shaping`, ADR-025) — `ifb-f2fb6f66` is
    # `_ifb_for_ifname("wlan0")`.
    assert (
        Shaping(ifname="wlan0", down_bps=500_000, up_bps=95_000_000, diffserv="diffserv4")
        in state.shaping
    )
    assert (10, 10) in state.marks
    assert (20, 20) in state.marks
    assert RouteRule(fwmark=10, table=100, ifname="usb0") in state.routes
    assert RouteRule(fwmark=20, table=100, ifname="usb0") in state.routes


def test_actual_is_none_when_every_read_fails():
    def fake(argv, **kwargs):
        return _completed(returncode=1, stderr="Operation not permitted")

    with patch("wifucked.enforce.subprocess.run", side_effect=fake):
        assert LinuxEnforcer().actual() is None


# -- reconciliation diff -----------------------------------------------------


def _reconcile_dispatch(applied: list[list[str]]):
    def fake(argv, **kwargs):
        if len(argv) > 1 and argv[1] == "-j":
            return _read_dispatch(argv, **kwargs)
        applied.append(argv)
        return _completed()

    return fake


def test_reconcile_is_a_noop_when_kernel_already_matches():
    # Matches what _read_dispatch reports (both the real interface's egress
    # CAKE and its paired IFB's egress CAKE, down_bps=500_000 via
    # ifb-f2fb6f66), except a sub-1% up_bps rounding, which must be
    # tolerated.
    desired = DesiredState(
        shaping=(Shaping("wlan0", down_bps=500_000, up_bps=95_400_000, diffserv="diffserv4"),),
        routes=(RouteRule(10, 100, "usb0"), RouteRule(20, 100, "usb0")),
        marks=((10, 10), (20, 20)),
    )
    applied: list[list[str]] = []
    with patch("wifucked.enforce.subprocess.run", side_effect=_reconcile_dispatch(applied)):
        LinuxEnforcer().reconcile(desired)

    assert applied == []  # already converged — nothing programmed


def test_reconcile_programs_the_kernel_when_shaping_diverges():
    desired = DesiredState(
        shaping=(Shaping("wlan0", down_bps=40_000_000, up_bps=0, diffserv="diffserv4"),),
        routes=(),
        marks=((10, 10), (20, 20)),
    )
    applied: list[list[str]] = []
    with patch("wifucked.enforce.subprocess.run", side_effect=_reconcile_dispatch(applied)):
        LinuxEnforcer().reconcile(desired)

    assert any(a[:3] == ["tc", "qdisc", "replace"] for a in applied)


def test_dry_run_never_shells_out():
    def explode(*args, **kwargs):
        raise AssertionError("dry run must not execute commands")

    enforcer = LinuxEnforcer(dry_run=True)
    desired = render(
        Allocation("atomic-w", False, (Share("atomic-w", CRITICAL.name, 5_000_000),)),
        {"atomic-w": _atomic()},
    )
    with patch("wifucked.enforce.subprocess.run", side_effect=explode):
        enforcer.reconcile(desired)
        enforcer.reconcile(desired)  # converges via memory; still no exec


# -- parsing helpers ---------------------------------------------------------


def test_parse_rate_handles_units_and_unlimited():
    assert _parse_rate("95Mbit") == 95_000_000
    assert _parse_rate("500Kbit") == 500_000
    assert _parse_rate("1Gbit") == 1_000_000_000
    assert _parse_rate("12345bit") == 12345
    assert _parse_rate(67890) == 67890
    assert _parse_rate("unlimited") is None
    assert _parse_rate(None) is None
    assert _parse_rate("garbage") is None


def test_close_tolerates_cake_rounding_but_not_real_change():
    assert _close(100_000_000, 100_000_000)
    assert _close(100_000_000, 101_000_000)  # 1% — CAKE rendering noise
    assert not _close(100_000_000, 150_000_000)  # 50% — a real capacity change


# -- raw_dump (diagnostics) ---------------------------------------------------


def test_raw_dump_returns_output_of_each_readonly_command():
    outputs = {
        "nft": _completed(stdout="table inet wifucked { }\n"),
        "tc": _completed(stdout="qdisc cake 8001: dev wlan0 root\n"),
        "ip": _completed(stdout="0: from all lookup local\n"),
    }

    def fake(argv, **_kwargs):
        return outputs[argv[0]]

    with patch("wifucked.enforce.subprocess.run", side_effect=fake):
        dump = LinuxEnforcer().raw_dump()

    assert dump["nft_ruleset"] == "table inet wifucked { }\n"
    assert dump["tc_qdisc"] == "qdisc cake 8001: dev wlan0 root\n"
    assert dump["ip_rule"] == "0: from all lookup local\n"
    assert dump["ip_route"] == "0: from all lookup local\n"


def test_raw_dump_degrades_to_empty_string_per_command_on_failure():
    """One missing tool must not blank the rest of the dump (SOP-009)."""

    def fake(argv, **_kwargs):
        if argv[0] == "nft":
            raise FileNotFoundError("nft not found")
        return _completed(stdout="ok\n")

    with patch("wifucked.enforce.subprocess.run", side_effect=fake):
        dump = LinuxEnforcer().raw_dump()

    assert dump["nft_ruleset"] == ""
    assert dump["tc_qdisc"] == "ok\n"
