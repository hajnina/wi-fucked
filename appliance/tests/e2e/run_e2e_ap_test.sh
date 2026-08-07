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
# Requires root (QEMU networking is fine unprivileged, but network
# namespaces on the host build the input images) plus qemu-system-x86_64,
# genisoimage, mkfs.vfat (dosfstools), and mtools. See README.md.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../../.." && pwd)"

RESULTS_DIR="${1:-${REPO_ROOT}/e2e-artifacts}"
mkdir -p "${RESULTS_DIR}"

WORKDIR="$(mktemp -d /tmp/wifucked-e2e-qemu.XXXXXX)"
QEMU_TIMEOUT_S="${WIFUCKED_E2E_TIMEOUT_S:-1200}"
export MTOOLS_SKIP_CHECK=1

log() { printf '[e2e-host] %s\n' "$1"; }

# shellcheck disable=SC2329  # invoked indirectly via `trap ... EXIT` below
cleanup() {
    if [ -n "${QEMU_PID:-}" ] && kill -0 "${QEMU_PID}" 2> /dev/null; then
        log "qemu still running at cleanup; killing pid ${QEMU_PID}"
        kill -9 "${QEMU_PID}" 2> /dev/null || true
    fi
    rm -rf "${WORKDIR}"
}
trap cleanup EXIT

for bin in qemu-system-x86_64 genisoimage mkfs.vfat mcopy curl qemu-img; do
    command -v "${bin}" > /dev/null || { echo "FATAL: ${bin} not installed" >&2; exit 1; }
done

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
    -display none -serial "file:${WORKDIR}/console.log" -monitor none \
    -no-reboot \
    > "${WORKDIR}/qemu.log" 2>&1 &
QEMU_PID=$!

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

cp -f "${WORKDIR}/console.log" "${RESULTS_DIR}/console.log" 2> /dev/null || true

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

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    cat "${RESULTS_DIR}/report.md" >> "${GITHUB_STEP_SUMMARY}"
fi

if [ "${REPORT_RC}" != "0" ]; then
    log "RESULT: FAIL — see ${RESULTS_DIR}/report.md"
    exit 1
fi
log "RESULT: PASS — see ${RESULTS_DIR}/report.md"
exit 0
