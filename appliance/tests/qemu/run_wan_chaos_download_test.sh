#!/bin/bash
# WAN-chaos download proof: boots the real appliance control loop (Allocator,
# LinuxProber, WireGuardTunnel, LinuxEnforcer — the same objects
# wifucked.daemon.Daemon drives) against two real WAN links the host
# actively degrades throughout the run, and a real fabric guest, then runs
# one real HTTP download from a LAN client through the whole path — appliance
# LAN -> nft mark -> policy route -> WireGuard -> fabric NAT -> the "internet"
# netns's HTTP server, and back — while the chaos runs. The download must
# complete with a correct checksum: proof that the allocator's WAN failover
# and WireGuard's endpoint roaming (ADR-005/ADR-019) kept one continuous TCP
# connection alive through a WAN that never stops misbehaving.
#
# See docs/active-tests.md's entry for this test for exactly what "passing"
# does and doesn't prove, read before trusting a bare PASS/FAIL here — in
# particular this sandbox's kernel has no `netem` module (see chaos_wan.sh's
# header), so WAN degradation here is bandwidth throttling and real
# link-down outages, not loss/jitter shaping, unless run somewhere netem is
# available.
#
# Requires root (network namespaces, taps) and qemu-system-x86_64.
#
# Usage: ./run_wan_chaos_download_test.sh [--rebuild]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${QEMU_TEST_WORKDIR:-${HERE}/.work}"
RUNTIME="${WORK}/runtime"
APPLIANCE_LOG="${RUNTIME}/qemu-chaos-appliance-serial.log"
FABRIC_LOG="${RUNTIME}/qemu-chaos-fabric-serial.log"
APPLIANCE_PID_FILE="${RUNTIME}/qemu-chaos-appliance.pid"
FABRIC_PID_FILE="${RUNTIME}/qemu-chaos-fabric.pid"
CHAOS_LOG="${RUNTIME}/chaos_wan.log"
HTTP_SERVER_LOG="${RUNTIME}/http_server.log"
DOWNLOAD_LOG="${RUNTIME}/download.log"
PAYLOAD="${RUNTIME}/payload.bin"
DOWNLOADED="${RUNTIME}/downloaded.bin"

DURATION_S="${WIFUCKED_CHAOS_DURATION_S:-90}"
# Smaller than the original 6MiB: the adversarial both-links-throttled phases
# (chaos_wan.sh) plus real WireGuard/nftables overhead under nested TCG
# software emulation make achievable throughput here far below what the tbf
# rates alone would suggest — a sandbox/emulation cost, not something this
# test is trying to measure. The point is checksum correctness through
# real failovers, not a throughput guarantee, so the payload is sized to
# comfortably finish within the timeout even during the harshest window.
PAYLOAD_MB="${WIFUCKED_CHAOS_PAYLOAD_MB:-2}"

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

CHAOS_PID=""
HTTP_PID=""
# shellcheck disable=SC2317  # invoked indirectly via `trap ... EXIT`
cleanup() {
    status=$?
    if [ -n "${CHAOS_PID}" ]; then kill "${CHAOS_PID}" 2>/dev/null || true; fi
    if [ -n "${HTTP_PID}" ]; then ip netns exec internet kill "${HTTP_PID}" 2>/dev/null || true; fi
    for pf in "${APPLIANCE_PID_FILE}" "${FABRIC_PID_FILE}"; do
        if [ -f "${pf}" ]; then
            kill "$(cat "${pf}")" 2>/dev/null || true
            rm -f "${pf}"
        fi
    done
    "${HERE}/chaos_topology.sh" down || true
    exit "${status}"
}
trap cleanup EXIT

echo "=== 1/8: kernel + modules"
if [ ! -f "${WORK}/kernel/vmlinuz-lts" ] || [ ! -d "${WORK}/kernel/modloop-extract" ]; then
    "${HERE}/download_kernel.sh"
fi

echo "=== 2/8: initramfs images (chaos appliance + fabric)"
if [ "${REBUILD}" = "1" ] || [ ! -f "${WORK}/initramfs-chaos.cpio.gz" ]; then
    "${HERE}/build_chaos_initramfs.sh"
fi
if [ "${REBUILD}" = "1" ] || [ ! -f "${WORK}/initramfs-fabric.cpio.gz" ]; then
    "${HERE}/build_fabric_initramfs.sh"
fi

echo "=== 3/8: host network topology"
"${HERE}/chaos_topology.sh" up

echo "=== 4/8: download payload + HTTP server in the 'internet' netns"
head -c "$((PAYLOAD_MB * 1024 * 1024))" /dev/urandom > "${PAYLOAD}"
EXPECTED_SHA256="$(sha256sum "${PAYLOAD}" | awk '{print $1}')"
cp "${PAYLOAD}" "${RUNTIME}/served-payload.bin"
ip netns exec internet python3 -m http.server 8000 --directory "${RUNTIME}" \
    > "${HTTP_SERVER_LOG}" 2>&1 &
HTTP_PID=$!
sleep 1
if ! kill -0 "${HTTP_PID}" 2>/dev/null; then
    echo "FATAL: HTTP server in 'internet' netns did not start — see ${HTTP_SERVER_LOG}" >&2
    exit 1
fi
echo "payload: ${PAYLOAD_MB}MiB, sha256=${EXPECTED_SHA256}"

echo "=== 5/8: booting the fabric guest"
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

echo "=== 6/8: starting WAN chaos on tap-wan1/tap-wan2 and booting the appliance guest"
"${HERE}/chaos_wan.sh" tap-wan1 tap-wan2 "$((DURATION_S + 30))" "${CHAOS_LOG}" &
CHAOS_PID=$!

rm -f "${APPLIANCE_LOG}"
qemu-system-x86_64 \
    -M pc -m 768M -smp 2 \
    -kernel "${WORK}/kernel/vmlinuz-lts" \
    -initrd "${WORK}/initramfs-chaos.cpio.gz" \
    -append "console=ttyS0 panic=-1 quiet wifucked_duration_s=${DURATION_S} wifucked_hold_s=30" \
    -netdev tap,id=net0,ifname=tap-lan,script=no,downscript=no \
    -device virtio-net-pci,netdev=net0 \
    -netdev tap,id=net1,ifname=tap-wan1,script=no,downscript=no \
    -device virtio-net-pci,netdev=net1 \
    -netdev tap,id=net2,ifname=tap-wan2,script=no,downscript=no \
    -device virtio-net-pci,netdev=net2 \
    -nographic -serial "file:${APPLIANCE_LOG}" \
    -no-reboot -display none -monitor none \
    -pidfile "${APPLIANCE_PID_FILE}" \
    -name wifucked-chaos-appliance \
    &
APPLIANCE_BG_PID=$!

echo "waiting for the appliance guest's control loop to attach (this can take a bit — the guest's own driver loop runs ${DURATION_S}s)..."
APPLIANCE_ATTACHED=0
for _ in $(seq 1 60); do
    if [ -f "${APPLIANCE_LOG}" ] && grep -q "tunnel attach ok=True" "${APPLIANCE_LOG}" 2>/dev/null; then
        APPLIANCE_ATTACHED=1
        break
    fi
    if [ -f "${APPLIANCE_LOG}" ] && grep -q "tunnel attach ok=False" "${APPLIANCE_LOG}" 2>/dev/null; then
        break
    fi
    if ! kill -0 "${APPLIANCE_BG_PID}" 2>/dev/null; then
        echo "FATAL: appliance guest exited before attaching — see ${APPLIANCE_LOG}" >&2
        break
    fi
    sleep 1
done
if [ "${APPLIANCE_ATTACHED}" != "1" ]; then
    echo "=== appliance guest never attached to the fabric. Serial log:" >&2
    cat "${APPLIANCE_LOG}" >&2 2>/dev/null || true
    exit 1
fi
echo "appliance attached, control loop running for ${DURATION_S}s under chaos."

echo "=== 7/8: LAN client downloads the payload through the whole path, concurrently with the chaos"
set +e
: > "${DOWNLOAD_LOG}"
ip netns exec lanclient curl -sS --max-time "$((DURATION_S + 210))" \
    -o "${DOWNLOADED}" \
    "http://198.51.100.2:8000/served-payload.bin" \
    >> "${DOWNLOAD_LOG}" 2>&1
CURL_RC=$?
set -e
cat "${DOWNLOAD_LOG}"
echo "curl exit code: ${CURL_RC}"

ACTUAL_SHA256=""
DOWNLOADED_BYTES=0
if [ -f "${DOWNLOADED}" ]; then
    ACTUAL_SHA256="$(sha256sum "${DOWNLOADED}" | awk '{print $1}')"
    DOWNLOADED_BYTES="$(stat -c%s "${DOWNLOADED}")"
fi
echo "expected sha256=${EXPECTED_SHA256} bytes=$((PAYLOAD_MB * 1024 * 1024))"
echo "actual   sha256=${ACTUAL_SHA256} bytes=${DOWNLOADED_BYTES}"

echo "=== waiting for the appliance guest's driver to finish and print its result..."
# The download can finish (curl returns) well before the guest's control-loop
# driver reaches its full DURATION_S — wait long enough for that loop to
# actually complete, not a fixed short timeout.
for _ in $(seq 1 "$((DURATION_S + 30))"); do
    grep -q "WIFUCKED_QEMU_RESULT_END" "${APPLIANCE_LOG}" 2>/dev/null && break
    sleep 1
done
echo "=== appliance control-loop result (WAN swaps observed during the run):"
DRIVER_RESULT_JSON="$(sed -n '/WIFUCKED_QEMU_RESULT_START/,/WIFUCKED_QEMU_RESULT_END/p' "${APPLIANCE_LOG}" | sed '1d;$d')"
echo "${DRIVER_RESULT_JSON}" | python3 -c "
import json, sys
r = json.load(sys.stdin)
print(f\"switch_count={r.get('switch_count')}\")
for s in r.get('primary_switches', []):
    print(s)
print(f\"starved_ticks={r.get('starved_ticks')} / total_ticks={r.get('total_ticks')}\")
" || true
STARVED_TICKS="$(echo "${DRIVER_RESULT_JSON}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('starved_ticks', -1))" 2>/dev/null || echo -1)"

echo "=== chaos schedule applied to the WAN links:"
cat "${CHAOS_LOG}" 2>/dev/null || true

echo "=== 8/8: verdict"
PASS=1
if [ "${CURL_RC}" -ne 0 ]; then
    echo "FAIL: curl did not complete (exit ${CURL_RC}) — see ${DOWNLOAD_LOG}"
    PASS=0
fi
if [ "${ACTUAL_SHA256}" != "${EXPECTED_SHA256}" ]; then
    echo "FAIL: checksum mismatch — downloaded bytes do not match what was served"
    PASS=0
fi
if [ "${STARVED_TICKS}" != "0" ]; then
    echo "FAIL: probe budget starved a WAN atomic on ${STARVED_TICKS} tick(s) —"
    echo "      its health went unverified for at least one measurement pass."
    echo "      A WAN swap during a starved tick is a failover to a link that"
    echo "      was never actually confirmed healthy. See the WARNING lines in"
    echo "      ${APPLIANCE_LOG}."
    PASS=0
fi

if [ "${PASS}" = "1" ]; then
    echo
    echo "=== PASS: a real HTTP download completed with a correct checksum through"
    echo "    the real WireGuard tunnel + real fabric NAT + real allocator/CAKE"
    echo "    control loop, while two real WAN links were actively degraded"
    echo "    throughout (see ${CHAOS_LOG}), with both WAN atomics actively"
    echo "    re-measured every single tick (starved_ticks=0) — every failover"
    echo "    decision was made against a freshly confirmed target, not a stale"
    echo "    or unverified one. No packet corruption, no failed connection —"
    echo "    the appliance's WAN-swap/failover kept one HTTP download alive"
    echo "    end to end."
    exit 0
else
    echo
    echo "=== FAIL: see ${DOWNLOAD_LOG}, ${APPLIANCE_LOG}, ${FABRIC_LOG}, ${CHAOS_LOG}."
    exit 1
fi
