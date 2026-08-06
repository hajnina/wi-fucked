# Active tests — code that runs for real, unconfirmed on real hardware

This tracks the gap between "the code path exists and executes on a Pi" and
"a human has watched it work on a Pi." `MOCK_HW=1` and scenario tests validate
*logic* — that the state machine does what it says given a fake HAL. They
cannot validate whether `brcmfmac`, `hostapd`, or the driver underneath them
actually behaves the way the code assumes.

An entry belongs here when code that touches real, unmocked hardware behaviour
has been merged and is live in `build_linux_hal()` / the real control loop,
**and no one has yet confirmed the result on an actual device.** It is not for
things still gated behind a spike that hasn't run at all (those live in
[`radio-spike.md`](radio-spike.md)) — it's for things that will *actually
execute*, right now, on whatever hardware runs this daemon, with an outcome
nobody has observed.

## Rule for anyone (human or agent) touching this repo

Before assuming a piece of behaviour works because "the code is there": check
this file. If the relevant entry says `UNCONFIRMED`, **ask the user directly**
whether they've run it on real hardware and what happened, before building on
top of it or reporting it as working. When you do get a real result — success
or failure — update the entry and move it to `CONFIRMED` (or `BROKEN`, with the
finding) in the same session. An entry that never gets updated is worse than
no entry; it teaches people to stop trusting this file.

---

## Entries

### USB OTG forced host mode, persistent boot log, temporary HDMI console

**Status:** `UNCONFIRMED`
**Touches:** `appliance/setup_rpi.sh` (`dtoverlay=dwc2,dr_mode=host`, verbose boot),
`appliance/stage-custom/opt/wifucked/hdmi_console.sh`,
`appliance/stage-custom/etc/systemd/system/wifucked-console.service`,
`appliance/stage-custom/opt/wifucked/firstboot.sh` / `bootcount.sh` (persistent
`/var/log/wifucked-boot.log`)
**Related:** [ADR-011](adr/ADR-011-ap-is-the-anchor.md), #15, PR #16

**What actually runs today:** PR #16 forced the OTG port into USB host mode
(`dtoverlay=dwc2,dr_mode=host`) to fix "phone charges but never gets a data
connection," added a persistent on-disk boot log so a failed first boot isn't
lost when `journalctl`'s RAM-only storage is wiped by the next power cycle,
and added a temporary HDMI console (`wifucked-console.service`) streaming a
live status snapshot and `journalctl -f` to `tty1`. All of it is live in the
current image build and none of it has been run on a device — the PR's own
test plan flagged this and said the result belonged here; it was never added
until now.

**What is unconfirmed:** Whether forcing `dr_mode=host` actually makes phone
tethering enumerate reliably on this hardware; whether the HDMI console
renders usably on tty1 without fighting the verbose kernel boot output it's
layered under; whether the persistent boot log survives the SD-card-write
pattern it's meant to survive.

**Built-in fallback if it fails:** None — this *is* the fallback diagnostic
path for #15. If it doesn't work, there is currently no other way to observe
a first boot that produces no AP and no reachable dashboard.

**Next step:** Boot a real device with a monitor attached and confirm the
HDMI console appears, plug a phone into the OTG port and confirm it tenders a
data connection, and check `/var/log/wifucked-boot.log` after a power cycle.

**History:**
- 2026-08-02 — flagged as unconfirmed; the entry PR #16 promised but never
  added. Merged and live since 2026-08-01, not yet run against real hardware
  by anyone.

---

### AP bring-up: NetworkManager unmanaged-devices fix, rfkill unblock, static VLAN gateway addresses

**Status:** `UNCONFIRMED`
**Touches:** `appliance/setup_rpi.sh` (NetworkManager `unmanaged-devices`, `systemd-networkd` enablement),
`appliance/stage-custom/etc/systemd/system/hostapd.service.d/10-wifucked.conf` (new),
`appliance/src/wifucked/lan/__init__.py` (`networkd_config`, `networkd_unit_name`),
`appliance/stage-custom/opt/wifucked/firstboot.sh`
**Related:** [ADR-011](adr/ADR-011-ap-is-the-anchor.md), [SOP-009](sop/SOP-009-hardware-and-field-debugging.md), #15

**What actually runs today:** Issue #15 reported "no hotspot, ever" on first
real-hardware boot — neither SSID visible in a scan, no error surfaced
anywhere. Root-cause analysis (no hardware in the loop; reasoned from the
generated config and known `hostapd`/`NetworkManager` failure modes) found
three concrete defects, now fixed:

1. `NetworkManager`'s `unmanaged-devices` list named `ap0`, an interface that
   has never existed on this hardware. The real AP radio (`wlan0`, and the
   VLAN subinterfaces `hostapd` creates under it) was left under
   `NetworkManager`/`wpa_supplicant` control, which is a well-documented cause
   of `hostapd` failing to bind the radio at all. Now
   `interface-name:wlan0*`.
2. Nothing ever ran `rfkill unblock` on this image, since `wpa_supplicant`
   (which normally does this as a side effect) was never supposed to touch
   `wlan0`. A radio that boots soft-blocked fails `hostapd`'s `nl80211` driver
   init silently, with no SSID and no retry. `hostapd.service.d/10-wifucked.conf`
   now unblocks on every start and adds `Restart=on-failure`.
3. Nothing assigned an address to the VLAN subinterfaces `hostapd` creates for
   each service profile (`wlan0.20`, `wlan0_1.10`), so a client that *did*
   associate would get a DHCP lease pointing at a gateway that doesn't exist.
   `networkd_config()` now generates a `systemd-networkd` `.network` file per
   profile at first boot, matched by interface name so it applies whenever
   `hostapd` brings the subinterface up — independent of the daemon, same as
   `hostapd`/`dnsmasq` themselves.

**What is unconfirmed:** Whether these were in fact the causes of #15's "no
AP" symptom, whether `systemd-networkd` and `NetworkManager` coexist cleanly
on this exact Raspberry Pi OS Trixie image with this split of interfaces, and
whether `hostapd`'s per-BSS `vlan_id` subinterfaces actually appear with the
names `lan_ifname_for_profile()` assumes (only unit-tested against the
generator functions, never against a running `hostapd`).

**Built-in fallback if it fails:** None beyond `hostapd`'s own
`Restart=on-failure` — if the AP still doesn't come up, `wifucked-console.service`
(temporary HDMI bring-up console, see `TODO.md` item 2) is the way to observe
why on the next attempt.

**Next step:** Flash an image with this fix, boot a device, and confirm: SSID
visible in a scan, a client can associate and get a DHCP lease with a working
gateway on both `Stable_critical` and `Stable_besteffort`, and phone tethering
via OTG still works (the fix from PR #16 this builds on top of, also still
`UNCONFIRMED`).

**History:**
- 2026-08-02 — fix merged in response to #15 continuing to report "no AP" after
  PR #16 (which addressed OTG tethering and diagnostics, not this). Not yet
  run against real hardware by anyone.

---

### AP+STA SHARED profile, CSA channel-following

**Status:** `UNCONFIRMED`
**Touches:** `radio/__init__.py` (`RadioManager.observe`/`align`), `hal/linux.py`
(`LinuxAp.channel_switch`, `LinuxWifi.station_link`/`connect_station`/`capabilities`)
**Related:** [ADR-013](adr/ADR-013-radio-profiles.md), [ADR-014](adr/ADR-014-two-ssid-fallback.md),
[`radio-spike.md`](radio-spike.md) Q1–Q3

**What actually runs today:** `Daemon._fast_loop()` calls
`self.radio.observe(atomics)` and, on a channel conflict, `self.radio.align(...)`
on every fast-loop tick (~1s) whenever real hardware is in use. This is
unconditional — there is no capability probe gating it. The moment the radio
profile is `SHARED` and the station's channel differs from the AP's, the
daemon will call `hostapd_cli chan_switch` for real, against whatever clients
are actually associated to the AP at that moment.

**What is unconfirmed:**
- Whether `brcmfmac`/CYW43438 supports AP+STA concurrency at all on this exact
  kernel/driver/firmware combination (Q1).
- Whether `hostapd_cli chan_switch` succeeds, or returns "CSA is not supported"
  as widely reported for related Broadcom chips in this family (Q3) — see the
  literature review added to `radio-spike.md`.
- Whether associated clients survive the attempt if it does something.

**Built-in fallback if it fails:** `RadioManager.align()` sets
`csa_unavailable = True` after the first failed switch and stops retrying —
logged at `warning` with `workflow: csa_move`. So a failure degrades to "AP
stuck on the wrong channel until a USB WAN is plugged in," not a crash loop.
But the *first* attempt happens live and unobserved, against a real AP with
real associated clients, the first time a SHARED-profile channel conflict
occurs on real hardware.

**Next step:** run `radio-spike.md` Q1–Q3 on the actual Pi Zero 2W. Until an
entry here says `CONFIRMED` or `BROKEN`, treat every SHARED-profile channel
move on real hardware as a live experiment, not a known-working feature.

**History:**
- 2026-07-31 — flagged as unconfirmed; code path is live and unguarded as
  described above. Not yet run against real hardware by anyone.

---

### Wi-Fi-as-WAN via iw/wpa_supplicant instead of nmcli

**Status:** `UNCONFIRMED`
**Touches:** `appliance/src/wifucked/hal/linux.py` (`LinuxWifi.scan`,
`LinuxWifi.connect_station`, `LinuxWifi.disconnect_station`), `appliance/apt_deps.txt`
(`iw`, `wpasupplicant`, `isc-dhcp-client`)
**Related:** [`radio-spike.md`](radio-spike.md) Q1, backlog item 14
(`docs/backlog/traffic-blockers.md`), the "AP+STA SHARED profile, CSA
channel-following" entry above

**What actually runs today:** `wlan0*` is `unmanaged-devices` for
NetworkManager (`setup_rpi.sh`, so hostapd can own the AP radio undisturbed —
ADR-011), which meant `nmcli`-based `scan()`/`connect_station()` silently
failed on this interface and `_discover_wifi()` never found a Wi-Fi WAN to
offer. This PR reimplements both directly:
- `scan()` runs `iw dev wlan0 scan` and parses the BSS-block dump format
  (`_parse_scan_dump`).
- `connect_station()` writes a scoped `wpa_supplicant` config to the tmpfs
  runtime dir (`/run/wifucked`), starts a detached `wpa_supplicant -i wlan0`
  bound only to that interface, polls `station_link()` (already `iw`-based)
  for association, then runs `dhclient -1` for an address.
- `disconnect_station()` kills that `wpa_supplicant` via its pidfile and
  flushes the interface's address.

Both of these now execute for real whenever real hardware discovers a Wi-Fi
network and the allocator asks it to connect — this is live in
`_discover_wifi()` / `build_linux_hal()`, not gated behind anything.

**What is unconfirmed:**
- Whether `iw scan` / `wpa_supplicant` can even run against `wlan0` *while
  hostapd also holds `wlan0` for the AP* — this is exactly `radio-spike.md`
  Q1 (AP+STA concurrency), which is still `NOT YET RUN`. If the chip cannot
  do AP+STA concurrently, this code path either fails harmlessly (scan
  returns nothing, connect never associates) or — the case nobody has
  observed — disrupts the AP while attempting to.
- The `iw dev <ifname> scan` parser (`_parse_scan_dump`) is unit-tested only
  against a hand-built fixture matching `iw`'s documented output format, not
  a real capture from this chip's driver/firmware.
- The `wpa_supplicant` + `dhclient` connect flow has never been run on a
  device — timing (association wait, DHCP lease time), whether `nl80211`
  driver selection is right for `brcmfmac`, and whether the pidfile-based
  `disconnect_station()` cleanup actually kills the right process, are all
  unverified.

**Built-in fallback if it fails:** `connect_station()` cleans up after
itself (`disconnect_station()`) on any failed step and returns `False`;
`_discover_wifi()` and the allocator already treat "no networks" / "connect
failed" as ordinary absence, not a crash. Worst case if AP+STA concurrency
doesn't work at all: Wi-Fi-as-WAN never engages (same net effect as before
this fix, `_discover_wifi()` returning nothing), or, unconfirmed and worse,
an active connect attempt disrupts the AP's own radio state while hostapd is
using it — this is the scenario Q1 exists to rule in or out before anyone
trusts SHARED profile in the field.

**Next step:** run `radio-spike.md` Q1 first — it gates whether this code
path is safe to exercise at all with the AP live. Then, on a device with the
image built from this change, force a scan (unplug other WANs so
Wi-Fi-as-WAN is the connection under test) and confirm: `scan()` returns real
networks, `connect_station()` associates and gets an address, and the AP
(watch `hostapd_cli list_sta`) does not drop clients during any of it.

**History:**
- 2026-08-04 — implemented in response to backlog item 14; parser logic
  unit-tested against a synthetic fixture, full connect/disconnect flow not
  yet run against real hardware by anyone.

---

### ADR-019 tunnel-owned LAN egress: full round-trip through real WireGuard + NAT

**Status:** `UNCONFIRMED`
**Touches:** `appliance/src/wifucked/enforce/__init__.py` (`render()`'s
`tunnel_ifname` routing), `appliance/src/wifucked/tunnel/__init__.py`
(`allowed-ips 0.0.0.0/0`), `fabric/src/fabric/wireguard.py`
(`_route_rfc1918_via_wireguard`, `_enable_forwarding_and_nat`, widened
`add_peer` allowed-ips)
**Related:** [ADR-019](adr/ADR-019-lan-egress-through-the-tunnel.md), backlog
item 5, `appliance/tests/qemu/`

**What actually runs today:** ADR-019's code is live and unguarded — every
`render()` call routes LAN traffic to the tunnel interface, and the fabric
forwards+NATs tunnel-peer traffic on every `/register`. This is not gated
behind anything; it runs on every real allocation the daemon renders.

**What was independently confirmed**, via a two-VM QEMU harness
(`appliance/tests/qemu/run_packet_routing_test.sh` — real Linux kernels, real
`wg`/`nft`/`ip` binaries, real kernel WireGuard/CAKE/netfilter modules, the
actual `wifucked.enforce`/`wifucked.tunnel`/`fabric.wireguard` code, not
mocks), each checked by direct inspection of live kernel state rather than
inferred from a passing exit code:
- A real WireGuard handshake completes between two independently-booted VMs
  (appliance and fabric), and `wg show` reports real key exchange and a real
  transfer counter.
- `enforce.render()`+`LinuxEnforcer.reconcile()` installs correct, real
  `nft`/`ip rule`/`ip route` state for a LAN client's marked traffic, routed
  to `wg0`, confirmed via `nft list ruleset`/`ip -j rule show`.
- A WAN swap (`tunnel.bind_to()` from one atomic to another) changes only
  which interface carries the tunnel — `render()`'s routing output is
  provably unchanged, confirmed on real installed kernel state, not just
  asserted in a scenario test.
- A LAN-client-shaped packet (802.1Q VLAN 10, hand-crafted since this
  sandbox's host kernel has no `CONFIG_VLAN_8021Q` to run a real one) sent
  into the appliance guest is genuinely marked, routed to `wg0`, and
  WireGuard-encrypted — the appliance's own `wg show` transfer-sent counter
  increases by exactly the encrypted packet's size at the moment the LAN
  packet is injected.
- The fabric genuinely decrypts it — its `wg show` transfer-received counter
  increases by the same amount at the same moment.
- The fabric's NAT/forwarding/routing setup is syntactically and semantically
  correct by every inspectable measure: `nft list ruleset` shows the
  masquerade rule with the right match; `ip route get 192.168.60.99`
  (an address in the LAN client's subnet) correctly resolves to `dev wg0`;
  `wg show wg0` on the fabric shows the appliance peer with `allowed ips`
  correctly widened to `10.99.0.2/32, 10.0.0.0/8, 172.16.0.0/12,
  192.168.0.0/16`.
- Three real, previously-undetected bugs were found and fixed by this proof,
  independent of ADR-019's own design (see the PR body for full detail):
  `enforce._nft_ruleset()`'s chain was literally named `mark`, which real
  `nft` rejects as a syntax error (this had been silently failing — logged
  and swallowed per ADR-008 — since before this PR, on every real box that
  ever ran it); `FabricWireGuard` had no route for RFC1918 traffic onto
  `wg0` (bare `wg` doesn't install routes the way `wg-quick` does); and
  `net.ipv4.conf.default.forwarding` needed setting explicitly because
  `wg0` is created after boot, too late to inherit the boot-time
  `ip_forward=1` write.

**What is unconfirmed:** The final leg — a reply from the simulated
"Internet" target routing all the way back through the fabric's NAT,
back through WireGuard, back to the LAN client — did not complete within
the time available. Every piece of state on the fabric was independently
confirmed correct (`ip route get` resolves to `wg0`, the peer's
`allowed-ips` covers the destination, NAT rule syntax and match are
correct), yet `wg show`'s "sent" counter on the fabric never advances
beyond the initial handshake, meaning WireGuard itself never transmits the
reply — even though the kernel's own routing decision says it should. A
second, ADR-019-independent test (a locally-raw-crafted, RFC1918-source-
spoofed packet sent directly from the fabric guest, bypassing `wg0`
and the appliance entirely) hit the identical symptom, which suggests
this may be an artifact of the synthetic three-layer test harness itself
(nested TCG virtualization, this sandbox's own missing `AF_PACKET`/
`tcpdump` support encountered while debugging) rather than an ADR-019 code
defect — but that is a hypothesis, not a confirmed conclusion, and is
exactly the kind of claim this file exists to avoid making without
evidence.

**Built-in fallback if it fails:** None new — if the full round-trip
genuinely doesn't work on real hardware, LAN clients get no Internet at all
through NORMAL atomics (the daemon has no WAN-direct fallback path;
ADR-019 replaced the previous, also-broken WAN-direct code entirely). This
is the same "no built-in fallback" posture the pre-ADR-019 code already had
(egress didn't work at all before this PR either, per the backlog item this
closes), so this is not a regression in blast radius — it's the same gap,
now with three more real bugs fixed and one narrower, better-characterized
open question in front of it.

**Next step:** Run `appliance/tests/qemu/run_packet_routing_test.sh` again
with more time budget than this session had, or — more reliably — run the
equivalent flow against two real Raspberry Pi devices (or a Pi appliance
plus the containerized fabric) on a real network, where the missing
`AF_PACKET`/`CONFIG_VLAN_8021Q`/kernel-module constraints this sandbox
imposed don't apply and a real `tcpdump` can watch the packet directly.

**History:**
- 2026-08-04 — QEMU proof built from scratch this session (kernel, two
  custom initramfs images, host network namespace topology, a
  hand-rolled VLAN-tagged packet injector). Found and fixed three real bugs
  independent of the ADR-019 design itself. The final round-trip was not
  achieved; every other claim above was independently confirmed against
  live kernel state, not inferred.

### Interim single-hotspot default (ADR-020)

**Status:** `UNCONFIRMED`
**Touches:** `appliance/stage-custom/opt/wifucked/firstboot.sh` (`LAN_MODE` no
longer probed, fixed to `single`), `appliance/src/wifucked/lan/__init__.py`
(`hostapd_config`'s `"single"` branch, `lan_ifname_for_profile`),
`appliance/src/wifucked/config.py` (`LanConfig.lan_mode` default),
`appliance/src/wifucked/discovery/__init__.py` (`Discoverer` defaults to
USB-only)
**Related:** [ADR-020](adr/ADR-020-interim-single-hotspot.md), the "AP
bring-up" entry above, #15

**What actually runs today:** First boot no longer probes `iw phy` for
multi-BSS support and no longer picks between `two_bss`/`two_psk`. It writes a
single-BSS `hostapd.conf` unconditionally — one SSID, one passphrase, no
`vlan_id`, no VLAN subinterfaces for `systemd-networkd` to address. The
running daemon discovers only USB tethering and USB Ethernet as WAN by
default (`Config.lan.wan_uses_wifi = False`); the radio is not shared between
AP and station.

**What is unconfirmed:** Whether a single-BSS `hostapd.conf` actually brings
up the AP on this hardware — this is expected to be *more* likely to work
than the two-BSS config #15 reported failing on (fewer driver assumptions),
but nobody has watched it boot. Also unconfirmed: whether the label text
generated for single mode (`firstboot.sh`) matches what actually gets
printed/read by support.

**Built-in fallback if it fails:** Same as the "AP bring-up" entry above —
`hostapd`'s own `Restart=on-failure`, and `wifucked-console.service` for
observing why on the next attempt.

**Next step:** Flash an image with this change, boot a device, confirm one
SSID is visible in a scan, a client can associate and get a DHCP lease with a
working gateway, and USB tethering still works for WAN.

**History:**
- 2026-08-06 — change made in response to a direct report of "no hotspot,
  ever" on real hardware; not yet run against real hardware by anyone.

---

## Template for new entries

```markdown
### <short name of the behaviour>

**Status:** `UNCONFIRMED` | `CONFIRMED` | `BROKEN`
**Touches:** <files/modules>
**Related:** <ADRs, spike doc, other refs>

**What actually runs today:** <what fires, under what condition, calling what
real command/syscall>

**What is unconfirmed:** <the specific claims nobody has observed>

**Built-in fallback if it fails:** <what happens to the system if the
assumption is wrong — or "none" if that's the honest answer>

**Next step:** <what running it once would look like>

**History:**
- <date> — <what happened, or that it's still unrun>
```
