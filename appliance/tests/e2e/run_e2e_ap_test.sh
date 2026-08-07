#!/bin/bash
#
# The AP + dashboard E2E proof — real QEMU, real systemd, real HAL.
#
# Boots a real Debian guest with QEMU and lets it run the actual
# appliance/setup_rpi.sh (the real image-bake provisioning script) live,
# which enables the actual systemd units — hostapd, dnsmasq,
# systemd-networkd, NetworkManager with the real unmanaged-devices config,
# wifucked-firstboot (the real firstboot.sh), and wifucked.service itself
# with no MOCK_HW override, so it drives the real Linux HAL. A second
# mac80211_hwsim radio, moved into its own network namespace inside the same
# guest kernel, plays a real Wi-Fi client: real 802.11 association, a real
# DHCP lease from the real dnsmasq, a real ping at the gateway, and a real
# headless Chromium (Playwright) at the real dashboard.
#
# This exists because an earlier version of this test (see git history)
# short-circuited exactly the layers most likely to hide a real bug: it
# called wifucked.lan's config-generating functions directly instead of
# running firstboot.sh, hand-assigned the gateway address instead of letting
# systemd-networkd apply it, and ran the daemon under MOCK_HW=1. All three of
# those are gone here — see README.md's "what changed and why."
#
# Also stands up two real WAN links: `tap-wan1`/`tap-wan2`, host-side taps
# presented to the guest as real USB-Ethernet devices (QEMU `usb-net`, CDC-ECM
# — the real `LinuxUsb.devices()` sysfs classification code discovers these
# for real, no fixture, no fake). The host acts as the "ISP" for each (DHCP +
# NAT to the real Internet, so the daemon's real, hardcoded probe targets —
# 1.1.1.1/8.8.8.8 — are genuinely reachable) and degrades them on an
# independent schedule with `appliance/tests/qemu/chaos_wan.sh` while the
# real control loop (Discoverer, Allocator, LinuxProber, WireGuardTunnel,
# LinuxEnforcer — all real, `wifucked.service` unmodified) reacts, so this
# proves real WAN failover, not just AP bring-up. See README.md.
#
# Requires root (QEMU networking is fine unprivileged, but network
# namespaces/bridges/taps on the host build the topology) plus
# qemu-system-x86_64, genisoimage, mkfs.vfat (dosfstools), mtools, dnsmasq,
# and iptables. See README.md.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../../.." && pwd)"

RESULTS_DIR="${1:-${REPO_ROOT}/e2e-artifacts}"
mkdir -p "${RESULTS_DIR}"

WORKDIR="$(mktemp -d /tmp/wifucked-e2e-qemu.XXXXXX)"
QEMU_TIMEOUT_S="${WIFUCKED_E2E_TIMEOUT_S:-1500}"
export MTOOLS_SKIP_CHECK=1

# Must match guest/e2e_driver.sh's own CHAOS_DURATION_S — there is no kernel
# cmdline to pass it through with a full-disk (not -kernel/-append) boot, so
# it is a constant in both places rather than threaded through.
CHAOS_DURATION_S=150
WAN1_SUBNET="10.77.1"
WAN2_SUBNET="10.77.2"

# The real fabric (fabric/src/fabric — real Flask app, real WireGuard, real
# NAT) runs directly on this host, not in a container or another guest: this
# CI runner has real kernel WireGuard support, unlike the constrained sandbox
# appliance/tests/qemu/'s ADR-019 proof was built in, which is the whole
# reason that proof never closed its own "final leg." Bound to WAN1's own
# bridge gateway address — reachable from *both* WAN subnets via their
# default routes, since this host is directly connected to both and already
# forwards between them (ip_forward=1 below), no extra routing needed.
FABRIC_HTTP_ADDR="${WAN1_SUBNET}.1"
FABRIC_HTTP_PORT=8081
FABRIC_WG_PORT=51820
FABRIC_USERNAME="e2e"
FABRIC_PASSWORD="e2e-test-$$"
# Stands in for "the Internet" — reachable only through the fabric's own
# NAT/forwarding (ADR-019), on a small private link the fabric's masquerade
# rule (which matches RFC1918 *source* addresses, not destination) doesn't
# need to know about specifically.
INTERNET_HOST_ADDR="198.51.100.1"
INTERNET_NS_ADDR="198.51.100.2"
INTERNET_HTTP_PORT=8000
INTERNET_PAYLOAD_MB=1

log() { printf '[e2e-host] %s\n' "$1"; }

# shellcheck disable=SC2329  # invoked indirectly via `trap ... EXIT` below
cleanup() {
    if [ -n "${QEMU_PID:-}" ] && kill -0 "${QEMU_PID}" 2> /dev/null; then
        log "qemu still running at cleanup; killing pid ${QEMU_PID}"
        kill -9 "${QEMU_PID}" 2> /dev/null || true
    fi
    if [ -n "${CHAOS_PID:-}" ] && kill -0 "${CHAOS_PID}" 2> /dev/null; then
        kill -9 "${CHAOS_PID}" 2> /dev/null || true
    fi
    [ -n "${DNSMASQ_WAN1_PID:-}" ] && kill "${DNSMASQ_WAN1_PID}" 2> /dev/null || true
    [ -n "${DNSMASQ_WAN2_PID:-}" ] && kill "${DNSMASQ_WAN2_PID}" 2> /dev/null || true
    [ -n "${FABRIC_PID:-}" ] && kill "${FABRIC_PID}" 2> /dev/null || true
    [ -n "${INTERNET_HTTPD_PID:-}" ] && kill "${INTERNET_HTTPD_PID}" 2> /dev/null || true
    [ -n "${TCPDUMP_PID:-}" ] && kill "${TCPDUMP_PID}" 2> /dev/null || true
    [ -n "${WG_DEBUG_WATCH_PID:-}" ] && kill "${WG_DEBUG_WATCH_PID}" 2> /dev/null || true
    iptables -t nat -D POSTROUTING -s "${WAN1_SUBNET}.0/24" -o "${DEFAULT_IFACE:-eth0}" -j MASQUERADE 2> /dev/null || true
    iptables -t nat -D POSTROUTING -s "${WAN2_SUBNET}.0/24" -o "${DEFAULT_IFACE:-eth0}" -j MASQUERADE 2> /dev/null || true
    for fwd_if in wg0 veth-inet tap-wan1 tap-wan2; do
        iptables -D FORWARD -i "${fwd_if}" -j ACCEPT 2> /dev/null || true
        iptables -D FORWARD -o "${fwd_if}" -j ACCEPT 2> /dev/null || true
    done
    ip link del tap-wan1 2> /dev/null || true
    ip link del tap-wan2 2> /dev/null || true
    ip link del br-wan1 2> /dev/null || true
    ip link del br-wan2 2> /dev/null || true
    ip link del veth-inet 2> /dev/null || true
    ip netns del wifucked-e2e-internet 2> /dev/null || true
    ip link del wg0 2> /dev/null || true
    rm -rf "${WORKDIR}"
}
trap cleanup EXIT

for bin in qemu-system-x86_64 genisoimage mkfs.vfat mcopy curl qemu-img dnsmasq iptables wg python3; do
    command -v "${bin}" > /dev/null || { echo "FATAL: ${bin} not installed" >&2; exit 1; }
done

# --- WAN topology: two host-shaped taps, presented as real USB-Ethernet ----

log "building WAN topology (2 taps, DHCP, NAT to the real Internet)"
ip link add br-wan1 type bridge
ip link add br-wan2 type bridge
ip addr add "${WAN1_SUBNET}.1/24" dev br-wan1
ip addr add "${WAN2_SUBNET}.1/24" dev br-wan2
ip link set br-wan1 up
ip link set br-wan2 up
ip tuntap add dev tap-wan1 mode tap
ip tuntap add dev tap-wan2 mode tap
ip link set tap-wan1 master br-wan1
ip link set tap-wan2 master br-wan2
ip link set tap-wan1 up
ip link set tap-wan2 up

# DHCP-only (--port=0 disables its own DNS resolver) — the host plays "the
# ISP" for each simulated WAN, same as a real tethered phone or USB Ethernet
# dongle would hand out an address and a gateway.
dnsmasq --interface=br-wan1 --bind-interfaces --except-interface=lo --port=0 \
    --dhcp-range="${WAN1_SUBNET}.50,${WAN1_SUBNET}.100,12h" \
    --dhcp-option="option:router,${WAN1_SUBNET}.1" \
    --pid-file="${WORKDIR}/dnsmasq-wan1.pid" --log-facility="${WORKDIR}/dnsmasq-wan1.log"
sleep 1
DNSMASQ_WAN1_PID="$(cat "${WORKDIR}/dnsmasq-wan1.pid" 2> /dev/null || true)"
dnsmasq --interface=br-wan2 --bind-interfaces --except-interface=lo --port=0 \
    --dhcp-range="${WAN2_SUBNET}.50,${WAN2_SUBNET}.100,12h" \
    --dhcp-option="option:router,${WAN2_SUBNET}.1" \
    --pid-file="${WORKDIR}/dnsmasq-wan2.pid" --log-facility="${WORKDIR}/dnsmasq-wan2.log"
sleep 1
DNSMASQ_WAN2_PID="$(cat "${WORKDIR}/dnsmasq-wan2.pid" 2> /dev/null || true)"

# The daemon's real active prober pings real 1.1.1.1/8.8.8.8 (hardcoded,
# appliance/src/wifucked/probe/__init__.py) — NAT both WAN subnets to the
# runner's own real Internet egress so those probes are genuinely real,
# rather than overriding the probe targets to fit a synthetic topology.
sysctl -w net.ipv4.ip_forward=1 > /dev/null
DEFAULT_IFACE="$(ip route show default | awk '{print $5; exit}')"
iptables -t nat -A POSTROUTING -s "${WAN1_SUBNET}.0/24" -o "${DEFAULT_IFACE}" -j MASQUERADE
iptables -t nat -A POSTROUTING -s "${WAN2_SUBNET}.0/24" -o "${DEFAULT_IFACE}" -j MASQUERADE

# GitHub-hosted runners have dockerd running by default, and dockerd sets
# the host's FORWARD chain policy to DROP the moment it enables IP
# forwarding for its own bridge networks (well-documented upstream:
# moby/moby#50566, Debian bug #865975) — it does this once at daemon
# startup, independent of anything this script does. That silently drops
# every packet this test's own gateway topology needs forwarded: LAN client
# -> wg0 -> fabric -> the Internet stand-in's netns, and the reply back.
# `ip_forward=1`, the WireGuard config, and the NAT rule below can all be
# completely correct and this still eats every packet, because FORWARD
# filtering is a separate netfilter hook from routing and NAT — this is
# exactly the failure mode item 16 (docs/backlog/traffic-blockers.md) found:
# a stable primary WAN, a live WireGuard handshake, zero bytes ever arriving.
# Insert an explicit accept ahead of whatever set that policy, in the same
# `filter`/`FORWARD` chain (not a competing chain — a competing chain's
# ACCEPT does not override another chain's DROP for the same packet, only a
# rule *inside* the chain that already owns the DROP does), scoped to the
# interfaces this test's topology actually uses.
for fwd_if in wg0 veth-inet tap-wan1 tap-wan2; do
    iptables -I FORWARD 1 -i "${fwd_if}" -j ACCEPT
    iptables -I FORWARD 1 -o "${fwd_if}" -j ACCEPT
done

# --- "the Internet" stand-in: reachable only through the fabric's real NAT -

log "building the Internet stand-in (real http.server behind the fabric)"
ip netns add wifucked-e2e-internet
ip link add veth-inet type veth peer name veth-inet-ns
ip link set veth-inet-ns netns wifucked-e2e-internet
ip addr add "${INTERNET_HOST_ADDR}/30" dev veth-inet
ip link set veth-inet up
ip netns exec wifucked-e2e-internet ip link set lo up
ip netns exec wifucked-e2e-internet ip addr add "${INTERNET_NS_ADDR}/30" dev veth-inet-ns
ip netns exec wifucked-e2e-internet ip link set veth-inet-ns up
ip netns exec wifucked-e2e-internet ip route add default via "${INTERNET_HOST_ADDR}"

PAYLOAD_DIR="${WORKDIR}/internet-payload"
mkdir -p "${PAYLOAD_DIR}"
dd if=/dev/urandom of="${PAYLOAD_DIR}/payload.bin" bs=1M count="${INTERNET_PAYLOAD_MB}" status=none
PAYLOAD_SHA256="$(sha256sum "${PAYLOAD_DIR}/payload.bin" | awk '{print $1}')"
log "Internet stand-in payload: ${INTERNET_PAYLOAD_MB}MB, sha256=${PAYLOAD_SHA256}"
# PYTHONUNBUFFERED=1: python's stdout is block-buffered once it isn't a
# tty (true here, redirected to a file), so http.server's own request log
# lines — including whether it never received anything at all — would
# otherwise sit in an unflushed buffer and be lost when cleanup() kills
# this process. An empty internet-httpd.log from a buffered process proves
# nothing about whether a request arrived; this makes an empty file after
# a real run actually mean "zero requests," not "buffering ate the log."
PYTHONUNBUFFERED=1 ip netns exec wifucked-e2e-internet python3 -m http.server "${INTERNET_HTTP_PORT}" \
    --directory "${PAYLOAD_DIR}" --bind "${INTERNET_NS_ADDR}" \
    > "${WORKDIR}/internet-httpd.log" 2>&1 &
INTERNET_HTTPD_PID=$!

# Item 16 (docs/backlog/traffic-blockers.md): every diagnostic short of an
# actual packet capture has now been checked and ruled out for this exact
# failure (routing, NAT rule syntax, FORWARD chain policy, per-interface
# forwarding sysctls, rp_filter, dmesg) while a real WireGuard decrypt still
# happens and zero packets ever reach the FORWARD hook on wg0 or veth-inet.
# `-i any` picks up interfaces that don't exist yet when tcpdump starts
# (including wg0, created later by the fabric's own `ensure_ready()` on the
# appliance's first registration) — starting it here, before the fabric
# even runs, means the capture can't miss the moment wg0 first appears.
tcpdump -i any -nn -w "${WORKDIR}/capture.pcap" \
    "host ${INTERNET_HOST_ADDR} or host ${INTERNET_NS_ADDR}" \
    > "${WORKDIR}/tcpdump.log" 2>&1 &
TCPDUMP_PID=$!

# capture.pcap coming back empty (zero packets, on any interface, for the
# whole run) on the previous run means the decrypted plaintext packet never
# reaches the network stack's normal receive path at all — tcpdump hooks in
# before netfilter, so a drop invisible to it and to iptables/nft/conntrack
# alike is almost certainly happening *inside* the WireGuard driver itself,
# before wg_packet_consume_data_done() hands the packet to netif_rx(). The
# most likely candidate at that stage is the allowed-ips *source* check
# (wg_allowedips_lookup_src) silently rejecting the decrypted packet's
# source address — which is a different check from the crypto-routing that
# already-live counters (`wg show ... transfer`) reflect, since WireGuard's
# rx byte accounting happens at successful decrypt, before this check runs.

# --- the real fabric: real Flask app, real WireGuard, real NAT -------------
#
# Runs directly on this host (root, for NET_ADMIN — the whole script is
# already sudo for the taps/iptables above), not in a container: the only
# thing fabric/Dockerfile's container buys over that is process isolation
# this ephemeral CI runner doesn't need, and a container would need its own
# network plumbing to reach the WAN bridges anyway.

log "starting the real fabric (${FABRIC_HTTP_ADDR}:${FABRIC_HTTP_PORT}, wg :${FABRIC_WG_PORT})"
python3 -m venv "${WORKDIR}/fabric-venv"
"${WORKDIR}/fabric-venv/bin/pip" install --quiet -r "${REPO_ROOT}/fabric/requirements.txt"
mkdir -p /var/lib/fabric
env \
    FABRIC_ADDRESS="${FABRIC_HTTP_ADDR}:${FABRIC_WG_PORT}" \
    FABRIC_USERNAME="${FABRIC_USERNAME}" \
    FABRIC_PASSWORD="${FABRIC_PASSWORD}" \
    FABRIC_WG_LISTEN_PORT="${FABRIC_WG_PORT}" \
    PYTHONPATH="${REPO_ROOT}/fabric/src" \
    "${WORKDIR}/fabric-venv/bin/python3" -c "
from fabric.app import create_app
create_app().run(host='${FABRIC_HTTP_ADDR}', port=${FABRIC_HTTP_PORT})
" > "${WORKDIR}/fabric.log" 2>&1 &
FABRIC_PID=$!
sleep 2
if ! kill -0 "${FABRIC_PID}" 2> /dev/null; then
    log "FATAL: fabric process did not stay running"
    cat "${WORKDIR}/fabric.log" >&2
    exit 1
fi

# `wg0` (and the wireguard kernel module) doesn't exist until the fabric's
# ensure_ready() runs, which only happens lazily on the appliance's first
# /register call — minutes from now, once the guest has booted far enough.
# /sys/kernel/debug/dynamic_debug/control only has entries for a module
# already loaded, so enabling this any earlier (tried once, item 16) is a
# silent no-op. Poll for wg0 in the background and enable the module's own
# debug logging the moment it actually exists, so a real allowed-ips
# rejection - the leading suspect once routing/NAT/forwarding/rp_filter/
# tcpdump all came back clean - prints straight to dmesg.
(
    for _ in $(seq 1 280); do
        if ip link show wg0 > /dev/null 2>&1; then
            echo "module wireguard +p" > /sys/kernel/debug/dynamic_debug/control 2>&1 || true
            break
        fi
        sleep 1
    done
) &
WG_DEBUG_WATCH_PID=$!

# --- base image (cached) -----------------------------------------------------

BASE_IMAGE="$("${HERE}/download_base_image.sh" | tail -n1)"

# --- build the three input disks --------------------------------------------

log "building repo ISO (read-only /mnt/repo in the guest)"
STAGE="${WORKDIR}/repo-stage"
mkdir -p "${STAGE}"
if command -v rsync > /dev/null; then
    rsync -a --exclude='.git' --exclude='appliance/tests/e2e/.work' --exclude='appliance/tests/qemu/.work' \
        "${REPO_ROOT}/" "${STAGE}/"
else
    cp -r "${REPO_ROOT}/." "${STAGE}/"
    rm -rf "${STAGE}/.git" "${STAGE}/appliance/tests/e2e/.work" "${STAGE}/appliance/tests/qemu/.work"
fi

# The real application code isn't deployed by setup_rpi.sh (base provisioning
# only) — on a real device it arrives as a separate OTA .wtf package applied
# by the real update_script.sh (see .github/workflows/reusable_image_pipeline.yml:
# bake runs setup_rpi.sh, then separately builds and applies a package the
# same way). Build one with the repo's own real packaging script so the guest
# can apply it the same real way — see guest/e2e_driver.sh's "deploy_package"
# stage.
log "building the real OTA package (scripts/build_package.sh)"
mkdir -p "${STAGE}/e2e-package"
"${REPO_ROOT}/scripts/build_package.sh" "0.0.0-e2e" "${STAGE}/e2e-package/wifucked.wtf"

# The guest needs the fabric's address/credentials to write a real
# config.json (wifucked.config's real schema) before wifucked.service's own
# one-shot fabric attach, plus the Internet stand-in's URL and expected
# checksum to verify a real download against. Plain JSON, read by
# guest/e2e_driver.sh off the read-only repo ISO — no network round-trip
# needed to hand this off, it's already known before boot.
cat > "${STAGE}/e2e-fabric-config.json" << EOF
{
  "fabric_url": "http://${FABRIC_HTTP_ADDR}:${FABRIC_HTTP_PORT}",
  "fabric_username": "${FABRIC_USERNAME}",
  "fabric_password": "${FABRIC_PASSWORD}",
  "internet_url": "http://${INTERNET_NS_ADDR}:${INTERNET_HTTP_PORT}/payload.bin",
  "payload_sha256": "${PAYLOAD_SHA256}"
}
EOF

genisoimage -quiet -r -J -o "${WORKDIR}/repo.iso" "${STAGE}"

log "building cloud-init seed ISO"
genisoimage -quiet -V cidata -J -r -o "${WORKDIR}/seed.img" \
    "${HERE}/cloud-init/meta-data" "${HERE}/cloud-init/user-data"

log "building results disk (blank FAT, 64MB)"
dd if=/dev/zero of="${WORKDIR}/results.img" bs=1M count=64 status=none
mkfs.vfat -F 32 -n RESULTS "${WORKDIR}/results.img" > /dev/null

log "creating disposable OS overlay (backing file: ${BASE_IMAGE})"
qemu-img create -f qcow2 -F qcow2 -b "${BASE_IMAGE}" "${WORKDIR}/os.qcow2" > /dev/null

# --- boot ---------------------------------------------------------------------

ACCEL="tcg"
[ -w /dev/kvm ] && ACCEL="kvm:tcg"
log "booting (accel=${ACCEL}, timeout=${QEMU_TIMEOUT_S}s)"

qemu-system-x86_64 \
    -machine "pc,accel=${ACCEL}" \
    -cpu max \
    -m 3072 \
    -smp 2 \
    -drive file="${WORKDIR}/os.qcow2",if=virtio,format=qcow2 \
    -drive file="${WORKDIR}/seed.img",media=cdrom,if=ide \
    -drive file="${WORKDIR}/repo.iso",media=cdrom,if=ide \
    -drive file="${WORKDIR}/results.img",if=virtio,format=raw \
    -netdev user,id=net0 -device virtio-net-pci,netdev=net0 \
    -netdev tap,id=wan1net,ifname=tap-wan1,script=no,downscript=no \
    -netdev tap,id=wan2net,ifname=tap-wan2,script=no,downscript=no \
    -device qemu-xhci,id=usbbus \
    -device usb-net,bus=usbbus.0,netdev=wan1net \
    -device usb-net,bus=usbbus.0,netdev=wan2net \
    -display none -serial "file:${WORKDIR}/console.log" -monitor none \
    -no-reboot \
    > "${WORKDIR}/qemu.log" 2>&1 &
QEMU_PID=$!

# Chaos starts as soon as the guest does and runs for CHAOS_DURATION_S; the
# guest doesn't reach its own WAN-chaos monitoring phase until well after
# hostapd/dashboard bring-up (~30-60s in), so the first stretch of chaos
# output lands before anything is watching it, which is fine — the schedule
# is a fixed, repeating cycle (chaos_wan.sh), not a one-shot triggered by the
# guest's readiness.
"${REPO_ROOT}/appliance/tests/qemu/chaos_wan.sh" tap-wan1 tap-wan2 "${CHAOS_DURATION_S}" \
    "${WORKDIR}/chaos_wan.log" &
CHAOS_PID=$!

# A guest-initiated ACPI poweroff does not reliably make the qemu *process*
# itself exit in every environment this has been observed to run in (the
# guest cleanly reaches its own poweroff in well under a minute, but the host
# process can outlive that indefinitely) — so process exit alone is not a
# safe completion signal. The guest's own finish() fsyncs and writes DONE to
# the results disk before ever calling `systemctl poweroff`; poll for that
# file directly (mtools reads the raw image file, unaffected by whatever
# advisory lock qemu itself holds on it) and only fall back to the full
# timeout if it never appears at all.
WAITED=0
DONE_AT=""
while kill -0 "${QEMU_PID}" 2> /dev/null; do
    if [ "${WAITED}" -ge "${QEMU_TIMEOUT_S}" ]; then
        log "TIMEOUT after ${QEMU_TIMEOUT_S}s; killing qemu (pid ${QEMU_PID})"
        break
    fi
    if [ -z "${DONE_AT}" ] && mdir -i "${WORKDIR}/results.img" ::DONE > /dev/null 2>&1; then
        DONE_AT="${WAITED}"
        log "guest signalled DONE at ~${WAITED}s; giving qemu up to 15s more to exit on its own"
    fi
    if [ -n "${DONE_AT}" ] && [ "$((WAITED - DONE_AT))" -ge 15 ]; then
        log "qemu process did not exit within 15s of guest DONE; killing pid ${QEMU_PID}"
        break
    fi
    sleep 5
    WAITED=$((WAITED + 5))
done
if kill -0 "${QEMU_PID}" 2> /dev/null; then
    kill -9 "${QEMU_PID}" 2> /dev/null || true
    TIMED_OUT=1
else
    TIMED_OUT=0
fi
wait "${QEMU_PID}" 2> /dev/null || true
log "qemu exited after ~${WAITED}s (guest DONE at ~${DONE_AT:-never}s)"

if kill -0 "${CHAOS_PID}" 2> /dev/null; then
    kill "${CHAOS_PID}" 2> /dev/null || true
fi
wait "${CHAOS_PID}" 2> /dev/null || true

cp -f "${WORKDIR}/console.log" "${RESULTS_DIR}/console.log" 2> /dev/null || true
cp -f "${WORKDIR}/chaos_wan.log" "${RESULTS_DIR}/chaos_wan.log" 2> /dev/null || true
cp -f "${WORKDIR}"/dnsmasq-wan*.log "${RESULTS_DIR}/" 2> /dev/null || true
cp -f "${WORKDIR}/fabric.log" "${RESULTS_DIR}/fabric.log" 2> /dev/null || true
cp -f "${WORKDIR}/internet-httpd.log" "${RESULTS_DIR}/internet-httpd.log" 2> /dev/null || true

# SIGTERM (not -9) so tcpdump gets to flush and close capture.pcap properly
# before it's read — a killed-mid-write pcap can truncate or corrupt.
if [ -n "${TCPDUMP_PID:-}" ] && kill -0 "${TCPDUMP_PID}" 2> /dev/null; then
    kill "${TCPDUMP_PID}" 2> /dev/null || true
    wait "${TCPDUMP_PID}" 2> /dev/null || true
fi
cp -f "${WORKDIR}/capture.pcap" "${RESULTS_DIR}/capture.pcap" 2> /dev/null || true
tcpdump -r "${WORKDIR}/capture.pcap" -nn -vvv > "${RESULTS_DIR}/capture.txt" 2>&1 || true
{
    echo "== host-side fabric wg0 =="
    wg show wg0 2>&1
    echo "== host-side fabric peers.json =="
    cat /var/lib/fabric/peers.json 2>&1
    # Item 16 (docs/backlog/traffic-blockers.md): the WireGuard layer has
    # tested healthy every time this has failed (real handshake, matching
    # send/receive byte counts on both ends) while curl never got a
    # SYN-ACK back. That points at the FORWARD hook on this host — the
    # kernel treats a decrypted wg0 packet destined off-host as a forward
    # (wg0 -> veth-inet), a separate netfilter hook from wg0's own
    # crypto-routing and from the fabric's own NAT rule. Capture the
    # actual chain state and connection tracking instead of continuing to
    # infer this from wg's byte counters alone.
    echo "== host iptables FORWARD chain =="
    iptables -L FORWARD -n -v --line-numbers 2>&1
    echo "== host nft ruleset (iptables-nft and the fabric's own NAT table share this host netns) =="
    nft list ruleset 2>&1
    echo "== host conntrack (if available) =="
    conntrack -L 2>&1
    echo "== host route lookup for the Internet stand-in =="
    ip route get "${INTERNET_NS_ADDR}" 2>&1
    # ADR-019's own QEMU proof already found that net.ipv4.ip_forward=1
    # (conf.all.forwarding) does not retroactively cover an interface's own
    # per-interface forwarding flag for interfaces created after that write
    # — the kernel's ip_forward() checks the INBOUND interface's own flag
    # specifically, before netfilter's FORWARD table is ever consulted, so a
    # 0 here silently drops the packet with zero evidence in any nft/iptables
    # counter. A route now exists to wg0 and a real WG decrypt happens (item
    # 16, capacity fix) but still zero forward-hook packets on wg0 or
    # veth-inet — check exactly here before guessing again.
    echo "== host per-interface forwarding flags =="
    for f in /proc/sys/net/ipv4/conf/all/forwarding /proc/sys/net/ipv4/conf/default/forwarding \
        /proc/sys/net/ipv4/conf/wg0/forwarding /proc/sys/net/ipv4/conf/veth-inet/forwarding \
        /proc/sys/net/ipv4/conf/tap-wan1/forwarding /proc/sys/net/ipv4/conf/tap-wan2/forwarding; do
        echo "${f}=$(cat "${f}" 2>&1)"
    done
    echo "== host per-interface rp_filter (fabric side, not just the guest) =="
    for f in /proc/sys/net/ipv4/conf/*/rp_filter; do echo "${f}=$(cat "${f}" 2>&1)"; done
    echo "== wireguard kernel module dynamic debug output (dropped/rejected packets, if the kernel supports it) =="
    dmesg 2>&1 | grep -i wireguard
    echo "== recent kernel drop-relevant log lines (martian, filter, etc.) =="
    dmesg 2>&1 | tail -100
} > "${RESULTS_DIR}/fabric-host-diagnostics.log" 2>&1 || true

# --- extract results ---------------------------------------------------------

EXTRACT_DIR="${WORKDIR}/extracted"
mkdir -p "${EXTRACT_DIR}"
mcopy -s -i "${WORKDIR}/results.img" '::*' "${EXTRACT_DIR}/" 2> "${WORKDIR}/mcopy.log" || true
if [ -d "${EXTRACT_DIR}" ]; then
    cp -r "${EXTRACT_DIR}/." "${RESULTS_DIR}/" 2> /dev/null || true
fi

if [ ! -f "${RESULTS_DIR}/DONE" ]; then
    # The guest never reached its own finish() — crashed, hung, or the boot
    # itself never got far enough to mount the results disk. Whatever
    # fragments exist (there may be none) get aggregated as-is below; this
    # extra fragment makes the timeout/crash itself visible as a stage
    # instead of the report silently having fewer rows than expected.
    mkdir -p "${RESULTS_DIR}/fragments"
    python3 "${HERE}/write_fragment.py" \
        --fragments-dir "${RESULTS_DIR}/fragments" --name "00_guest_completed" --fail \
        --duration-s "${WAITED}" \
        --detail "guest never wrote DONE to the results disk (timed_out=${TIMED_OUT})" \
        --error "$(tail -n 60 "${RESULTS_DIR}/console.log" 2> /dev/null)"
fi

python3 "${HERE}/aggregate_report.py" \
    --fragments-dir "${RESULTS_DIR}/fragments" --out-dir "${RESULTS_DIR}"
REPORT_RC=$?

python3 "${HERE}/build_report.py" --results-dir "${RESULTS_DIR}" || \
    log "WARNING build_report.py failed; report.md/junit.xml above are still valid"

# This whole script runs under sudo (QEMU networking, taps, iptables) and
# some of what lands in RESULTS_DIR is written by daemonized root processes
# (dnsmasq's own --log-facility file, in particular) with restrictive
# permissions — unreadable by the actions runner's normal, non-root user, so
# `actions/upload-artifact` fails outright trying to zip it. This isn't
# sensitive output (see ci.yml's job — none of it is), so make it all
# world-readable rather than track down each writer's own umask individually.
chmod -R a+rX "${RESULTS_DIR}" 2> /dev/null || true

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
        cat "${RESULTS_DIR}/report.md"
        echo ""
        echo "Full report with graphs and every screenshot: see the \`report.html\` file in this job's uploaded artifact."
    } >> "${GITHUB_STEP_SUMMARY}"
fi

if [ "${REPORT_RC}" != "0" ]; then
    log "RESULT: FAIL — see ${RESULTS_DIR}/report.md and report.html"
    exit 1
fi
log "RESULT: PASS — see ${RESULTS_DIR}/report.md and report.html"
exit 0
