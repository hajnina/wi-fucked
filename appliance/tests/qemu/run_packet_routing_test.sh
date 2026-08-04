#!/bin/bash
# The QEMU real-packet-routing proof for ADR-019 / backlog item 5
# (docs/backlog/traffic-blockers.md).
#
# Boots TWO real Linux kernels in QEMU (TCG software emulation — no
# /dev/kvm needed, confirmed absent in this sandbox): one is the
# "appliance" (three virtio-net adapters: LAN, WAN-A, WAN-B), the other is
# the "fabric" (one virtio-net adapter). Both run the actual
# `wifucked`/`fabric` Python packages against their own real kernels —
# real `ip`/`nft`/`wg`, real WireGuard and CAKE kernel modules, real
# nftables NAT. See topology.sh and {guest,fabric_guest}_init.sh for why
# it's two VMs rather than one VM plus a host-side stand-in for the fabric:
# this sandbox's own host kernel has neither CONFIG_WIREGUARD nor
# CONFIG_VLAN_8021Q, so both had to move into guest kernels that do.
#
# A simulated LAN client then sends one real, 802.1Q VLAN-tagged ICMP echo
# through the whole path: LAN -> appliance's nft marking -> policy route to
# wg0 -> real WireGuard encryption -> fabric decrypt -> real
# forwarding+NAT -> a host netns standing in for "the Internet" -> and back.
#
# Requires root (network namespaces, taps) and qemu-system-x86_64.
#
# Usage: ./run_packet_routing_test.sh [--rebuild]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${QEMU_TEST_WORKDIR:-${HERE}/.work}"
RUNTIME="${WORK}/runtime"
APPLIANCE_LOG="${RUNTIME}/qemu-appliance-serial.log"
FABRIC_LOG="${RUNTIME}/qemu-fabric-serial.log"
APPLIANCE_PID_FILE="${RUNTIME}/qemu-appliance.pid"
FABRIC_PID_FILE="${RUNTIME}/qemu-fabric.pid"

REBUILD=0
[ "${1:-}" = "--rebuild" ] && REBUILD=1

if [ "$(id -u)" -ne 0 ]; then
    echo "FATAL: must run as root (network namespaces, tap devices)." >&2
    exit 1
fi
if ! command -v qemu-system-x86_64 >/dev/null 2>&1; then
    echo "FATAL: qemu-system-x86_64 not found — apt-get install qemu-system-x86" >&2
    exit 1
fi

mkdir -p "${RUNTIME}"

# shellcheck disable=SC2317  # the whole function is invoked indirectly via `trap ... EXIT`
cleanup() {
    status=$?
    for pf in "${APPLIANCE_PID_FILE}" "${FABRIC_PID_FILE}"; do
        if [ -f "${pf}" ]; then
            kill "$(cat "${pf}")" 2>/dev/null || true
            rm -f "${pf}"
        fi
    done
    "${HERE}/topology.sh" down || true
    exit "${status}"
}
trap cleanup EXIT

echo "=== 1/6: kernel + modules"
if [ ! -f "${WORK}/kernel/vmlinuz-lts" ] || [ ! -d "${WORK}/kernel/modloop-extract" ]; then
    "${HERE}/download_kernel.sh"
fi

echo "=== 2/6: initramfs images (appliance + fabric)"
if [ "${REBUILD}" = "1" ] || [ ! -f "${WORK}/initramfs.cpio.gz" ]; then
    "${HERE}/build_initramfs.sh"
fi
if [ "${REBUILD}" = "1" ] || [ ! -f "${WORK}/initramfs-fabric.cpio.gz" ]; then
    "${HERE}/build_fabric_initramfs.sh"
fi

echo "=== 3/6: host network topology"
"${HERE}/topology.sh" up

echo "=== 4/6: booting the fabric guest"
rm -f "${FABRIC_LOG}"
qemu-system-x86_64 \
    -M pc -m 512M -smp 1 \
    -kernel "${WORK}/kernel/vmlinuz-lts" \
    -initrd "${WORK}/initramfs-fabric.cpio.gz" \
    -append "console=ttyS0 panic=-1 quiet" \
    -netdev tap,id=net0,ifname=tap-fabric,script=no,downscript=no \
    -device virtio-net-pci,netdev=net0 \
    -nographic -serial "file:${FABRIC_LOG}" \
    -no-reboot -display none -monitor none \
    -pidfile "${FABRIC_PID_FILE}" \
    &
FABRIC_BG_PID=$!

echo "waiting for the fabric guest (WIFUCKED_FABRIC_QEMU_READY)..."
FABRIC_READY=0
for _ in $(seq 1 180); do
    if [ -f "${FABRIC_LOG}" ] && grep -q "WIFUCKED_FABRIC_QEMU_READY" "${FABRIC_LOG}" 2>/dev/null; then
        FABRIC_READY=1
        break
    fi
    if ! kill -0 "${FABRIC_BG_PID}" 2>/dev/null; then
        echo "FATAL: fabric guest exited before READY — see ${FABRIC_LOG}" >&2
        break
    fi
    sleep 1
done
if [ "${FABRIC_READY}" != "1" ]; then
    echo "=== fabric guest never reached READY. Serial log:" >&2
    cat "${FABRIC_LOG}" >&2 2>/dev/null || true
    exit 1
fi
echo "fabric guest ready."

echo "=== 5/6: booting the appliance guest"
rm -f "${APPLIANCE_LOG}"
qemu-system-x86_64 \
    -M pc -m 768M -smp 2 \
    -kernel "${WORK}/kernel/vmlinuz-lts" \
    -initrd "${WORK}/initramfs.cpio.gz" \
    -append "console=ttyS0 panic=-1 quiet" \
    -netdev tap,id=net0,ifname=tap-lan,script=no,downscript=no \
    -device virtio-net-pci,netdev=net0 \
    -netdev tap,id=net1,ifname=tap-wan1,script=no,downscript=no \
    -device virtio-net-pci,netdev=net1 \
    -netdev tap,id=net2,ifname=tap-wan2,script=no,downscript=no \
    -device virtio-net-pci,netdev=net2 \
    -nographic -serial "file:${APPLIANCE_LOG}" \
    -no-reboot -display none -monitor none \
    -pidfile "${APPLIANCE_PID_FILE}" \
    &
APPLIANCE_BG_PID=$!

echo "waiting for the appliance guest (WIFUCKED_QEMU_READY)..."
APPLIANCE_READY=0
for _ in $(seq 1 240); do
    if [ -f "${APPLIANCE_LOG}" ] && grep -q "WIFUCKED_QEMU_READY" "${APPLIANCE_LOG}" 2>/dev/null; then
        APPLIANCE_READY=1
        break
    fi
    if ! kill -0 "${APPLIANCE_BG_PID}" 2>/dev/null; then
        echo "FATAL: appliance guest exited before READY — see ${APPLIANCE_LOG}" >&2
        break
    fi
    sleep 1
done
if [ "${APPLIANCE_READY}" != "1" ]; then
    echo "=== appliance guest never reached READY. Serial log:" >&2
    cat "${APPLIANCE_LOG}" >&2 2>/dev/null || true
    exit 1
fi

echo "=== appliance guest ready. Driver result:"
sed -n '/WIFUCKED_QEMU_RESULT_START/,/WIFUCKED_QEMU_RESULT_END/p' "${APPLIANCE_LOG}" | sed '1d;$d' | python3 -m json.tool || true

echo "=== 6/6: packet test — LAN client pings through the real path"
set +e
: > "${RUNTIME}/ping.log"
for _attempt in 1 2 3 4 5; do
    ip netns exec lanclient python3 "${HERE}/vlan_ping.py" veth-lc \
        --vlan 10 --src-ip 192.168.60.2 --dst-ip 198.51.100.2 --gateway-ip 192.168.60.1 \
        --timeout 4 >> "${RUNTIME}/ping.log" 2>&1
    PING_RESULT=$?
    [ "${PING_RESULT}" -eq 0 ] && break
    sleep 3
done
set -e
cat "${RUNTIME}/ping.log"
echo "=== eth0 packet counters observed by the appliance guest during the test:"
grep "t=+" "${APPLIANCE_LOG}" || true
echo "=== 'internet' netns rx/tx packet counters (the actual ping target):"
ip netns exec internet cat /sys/class/net/veth-inet/statistics/rx_packets
ip netns exec internet cat /sys/class/net/veth-inet/statistics/tx_packets

echo "=== waiting (up to 60s) for the fabric guest's own diagnostic dump to flush..."
for _ in $(seq 1 60); do
    grep -q "Power down" "${FABRIC_LOG}" 2>/dev/null && break
    sleep 1
done
echo "=== fabric guest's nftables ruleset and routing table:"
sed -n '/-- ip route show/,/Power down/p' "${FABRIC_LOG}" || true

if [ "${PING_RESULT}" -eq 0 ]; then
    echo
    echo "=== PASS: LAN client traffic reached the simulated Internet through"
    echo "    the real tunnel + real fabric NAT (ADR-019's data path, proven"
    echo "    with real packets across real virtio-net adapters, on two"
    echo "    real Linux kernels, in QEMU)."
else
    echo
    echo "=== FAIL: the full round trip did not complete. See ${RUNTIME}/ping.log,"
    echo "    ${APPLIANCE_LOG}, and ${FABRIC_LOG}."
    echo
    echo "    This does NOT mean nothing was proven — see"
    echo "    docs/active-tests.md's ADR-019 entry for exactly what was"
    echo "    independently confirmed (real WireGuard handshake, real"
    echo "    encrypt/decrypt of the injected packet, real nft/route state"
    echo "    on both VMs) versus what remains open (the final reply leg)."
fi

exit "${PING_RESULT}"
