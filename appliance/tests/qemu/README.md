# QEMU packet-routing proof (ADR-019 / backlog item 5)

Boots two independent QEMU virtual machines — the appliance and the fabric —
each running a real Linux kernel with real WireGuard, CAKE, and netfilter
kernel modules, and drives the **actual** `wifucked.enforce`/
`wifucked.tunnel`/`fabric.wireguard` Python code inside them (not mocks, not
a reimplementation). It then injects a real, 802.1Q VLAN-tagged ICMP packet
on a simulated LAN segment and checks how far it gets.

See [`docs/active-tests.md`](../../../docs/active-tests.md)'s ADR-019 entry
for exactly what this proved and what it didn't, with evidence — read that
before trusting a "PASS" or "FAIL" from a run in isolation.

## Running it

```bash
sudo appliance/tests/qemu/run_packet_routing_test.sh
```

Requires root (network namespaces, tap devices) and `qemu-system-x86_64`
(`apt-get install qemu-system-x86`). No `/dev/kvm` is required or used — this
runs under TCG (software) emulation, which is slower (expect 1-3 minutes)
but works anywhere.

First run fetches an Alpine Linux kernel + module set (~160 MB, cached under
`.work/kernel/` — `download_kernel.sh`) and builds two initramfs images
(`build_initramfs.sh`, `build_fabric_initramfs.sh`). Pass `--rebuild` to
force rebuilding the initramfs images (e.g. after editing the appliance or
fabric source) without re-fetching the kernel.

## What's actually running, and why it's shaped this way

- **Two separate VMs**, not one: the fabric needs real kernel WireGuard
  support too, and this was built in a sandbox whose own host kernel has
  neither `CONFIG_WIREGUARD` nor `CONFIG_VLAN_8021Q` (confirmed via
  `/proc/config.gz`) and no matching `/lib/modules` to load either — so both
  had to move into a purpose-fetched guest kernel that does.
- **The appliance guest** has three virtio-net adapters (LAN, WAN-A, WAN-B)
  and runs `driver.py`, which calls the real `WireGuardTunnel.attach()`,
  `bind_to()` (twice, simulating a WAN swap), and `enforce.render()` +
  `LinuxEnforcer.reconcile()` — the same calls `wifucked.daemon.Daemon`
  makes.
- **The fabric guest** has one virtio-net adapter and runs the real
  `fabric.app` Flask application (`fabric_server.py`), so a `/register` call
  exercises the genuine `fabric.wireguard.FabricWireGuard.ensure_ready()`/
  `add_peer()` code, ADR-019's NAT/forwarding included.
- **The host** provides the network topology (`topology.sh`): a LAN-client
  network namespace, an "Internet" network namespace standing in for some
  destination past the fabric's own WAN, and Linux bridges connecting them
  to the two VMs' tap devices.
- **`vlan_ping.py`** hand-crafts an 802.1Q-tagged ICMP echo over a raw
  socket from the host's LAN-client namespace, because this sandbox's own
  host kernel has no `CONFIG_VLAN_8021Q` to create a real VLAN
  sub-interface — see that file's docstring for the full reasoning. The
  *guest's* `eth0.10` on the other end is a real kernel VLAN subinterface
  doing real de-encapsulation; only the host-side injection is hand-rolled.

## Known sandbox limitations hit while building this

Documented here so a future run in a different environment isn't confused by
workarounds that may no longer be necessary there:

- This sandbox's `tcpdump` does not capture real traffic, even in the host's
  default network namespace, independent of anything in this test. Interface
  packet counters (`/sys/class/net/*/statistics/*`) and WireGuard's own
  `wg show ... transfer` counters are used instead, wherever a diagnostic
  needs to observe traffic.
- The Alpine kernel this test fetches has no `CONFIG_PACKET` (`AF_PACKET`
  raw sockets fail with `EAFNOSUPPORT` inside the guest), so an in-guest
  packet sniffer isn't available either — same reasoning applies.

## Files

| File | Role |
|---|---|
| `download_kernel.sh` | Fetches the Alpine kernel + module set (cached) |
| `build_initramfs.sh` | Builds the appliance guest's initramfs |
| `build_fabric_initramfs.sh` | Builds the fabric guest's initramfs |
| `guest_init.sh` | Appliance guest's `/init` (PID 1) |
| `fabric_guest_init.sh` | Fabric guest's `/init` (PID 1) |
| `driver.py` | Runs inside the appliance guest; the real `wifucked` calls |
| `fabric_server.py` | Runs inside the fabric guest; the real `fabric.app` |
| `topology.sh` | Host-side network namespaces, bridges, and taps |
| `vlan_ping.py` | Hand-crafted VLAN-tagged ICMP injector (host side) |
| `module_closure.txt` | Dependency-ordered kernel module list |
| `run_packet_routing_test.sh` | Orchestrates all of the above |
