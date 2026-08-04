#!/bin/bash
# Builds a minimal initramfs for the QEMU packet-routing proof
# (docs/active-tests.md, backlog item 5 / ADR-019).
#
# What goes in, and why:
#
# - busybox (static) for basic shell/init plumbing.
# - the REAL `ip`, `nft`, `wg`, `sysctl` binaries (copied from the build host,
#   with their shared-library closure resolved via `ldd`) — not busybox
#   applets, because the whole point of this proof is that the exact tools
#   `wifucked.enforce.LinuxEnforcer` and `wifucked.tunnel.WireGuardTunnel`
#   shell out to are the ones doing the work.
# - the real CPython interpreter + stdlib, so the guest runs the actual
#   `wifucked.enforce`/`wifucked.tunnel` Python modules (imported from a
#   verbatim copy of `appliance/src/wifucked`), not a reimplementation.
# - the WireGuard and CAKE kernel modules (plus their transitive dependency
#   closure) extracted from Alpine's netboot `modloop-lts`, matched to the
#   exact kernel build (`vmlinuz-lts`) this test boots — the sandbox this
#   ran in has neither module built into its own host kernel, so the guest
#   kernel is deliberately a different, purpose-fetched one.
#
# Run `download_kernel.sh` first if vmlinuz-lts/modloop-lts aren't present.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../../.." && pwd)"
WORK="${QEMU_TEST_WORKDIR:-${HERE}/.work}"
KERNEL_DIR="${WORK}/kernel"
ROOTFS="${WORK}/initramfs-root"
OUT="${WORK}/initramfs.cpio.gz"

KVER="6.6.110-0-lts"

if [ ! -f "${KERNEL_DIR}/vmlinuz-lts" ] || [ ! -d "${KERNEL_DIR}/modloop-extract" ]; then
    echo "FATAL: ${KERNEL_DIR}/vmlinuz-lts and modloop-extract must exist first." >&2
    echo "Run: ${HERE}/download_kernel.sh" >&2
    exit 1
fi

rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}"/{bin,sbin,lib,lib64,proc,sys,dev,etc,root,run}
mkdir -p "${ROOTFS}/lib/x86_64-linux-gnu"
mkdir -p "${ROOTFS}/lib/modules/${KVER}"
mkdir -p "${ROOTFS}/opt/wifucked/src"
mkdir -p "${ROOTFS}/etc/wireguard"

echo "--- busybox"
cp /bin/busybox "${ROOTFS}/bin/busybox"
# Symlinks are created at boot via `busybox --install`, not here — doing it
# in the init script keeps this build step a plain file copy.

copy_with_libs() {
    # Copies a dynamically-linked binary plus every shared library `ldd`
    # reports for it, preserving each library's absolute path so the
    # dynamic linker finds them at the same paths inside the guest.
    local bin="$1"
    local dest="$2"
    cp "${bin}" "${ROOTFS}${dest}"
    ldd "${bin}" 2>/dev/null | awk '{print $1, $3}' | while read -r _name path; do
        if [ -n "${path:-}" ] && [ -f "${path}" ]; then
            mkdir -p "${ROOTFS}$(dirname "${path}")"
            cp -n "${path}" "${ROOTFS}${path}" 2>/dev/null || true
        fi
    done
    # ld-linux itself (reported as a bare path, not "name => path")
    ldd "${bin}" 2>/dev/null | grep -oE '/lib64/ld-linux-x86-64\.so\.2' | while read -r p; do
        mkdir -p "${ROOTFS}$(dirname "${p}")"
        cp -n "${p}" "${ROOTFS}${p}" 2>/dev/null || true
    done
}

echo "--- ip / nft / wg / sysctl (real binaries, not busybox applets)"
mkdir -p "${ROOTFS}/usr/sbin" "${ROOTFS}/usr/bin"
copy_with_libs "$(command -v ip)" "/usr/sbin/ip"
copy_with_libs "$(command -v nft)" "/usr/sbin/nft"
copy_with_libs "$(command -v wg)" "/usr/bin/wg"
copy_with_libs "$(command -v sysctl)" "/usr/sbin/sysctl"
if command -v tc >/dev/null 2>&1; then
    copy_with_libs "$(command -v tc)" "/usr/sbin/tc"
else
    echo "    (tc not found on host — CAKE shaping argv is not exercised by this proof, only marking/routing/NAT are)"
fi

echo "--- python3 interpreter + stdlib"
PY_BIN="$(readlink -f "$(command -v python3)")"
PY_PREFIX="$(python3 -c 'import sys; print(sys.prefix)')"
copy_with_libs "${PY_BIN}" "/usr/bin/python3.11"
ln -sf python3.11 "${ROOTFS}/usr/bin/python3"
mkdir -p "${ROOTFS}/usr/lib"
cp -a "${PY_PREFIX}/lib/python3.11" "${ROOTFS}/usr/lib/python3.11"
# Test suite and other bulk-but-unused stdlib content bloats the image for no
# benefit in a guest that runs one driver script.
rm -rf "${ROOTFS}/usr/lib/python3.11/test" "${ROOTFS}/usr/lib/python3.11/__pycache__"
find "${ROOTFS}/usr/lib/python3.11" -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
# lib-dynload's compiled extension modules (_socket, _ssl, etc.) have their
# own shared-library dependencies (e.g. libssl) — resolve those too.
if [ -d "${ROOTFS}/usr/lib/python3.11/lib-dynload" ]; then
    find "${ROOTFS}/usr/lib/python3.11/lib-dynload" -name '*.so' | while read -r so; do
        ldd "${so}" 2>/dev/null | awk '{print $3}' | while read -r path; do
            if [ -n "${path:-}" ] && [ -f "${path}" ]; then
                mkdir -p "${ROOTFS}$(dirname "${path}")"
                cp -n "${path}" "${ROOTFS}${path}" 2>/dev/null || true
            fi
        done
    done
fi

echo "--- wifucked appliance package (verbatim, the real code under test)"
cp -a "${REPO_ROOT}/appliance/src/wifucked" "${ROOTFS}/opt/wifucked/src/wifucked"
find "${ROOTFS}/opt/wifucked/src/wifucked" -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

echo "--- kernel modules (wireguard, cake, nat closure)"
MODSRC="${KERNEL_DIR}/modloop-extract/modules/${KVER}"
grep -v '^#' "${HERE}/module_closure.txt" | grep -v '^[[:space:]]*$' | while read -r rel; do
    mkdir -p "${ROOTFS}/lib/modules/${KVER}/$(dirname "${rel}")"
    cp "${MODSRC}/${rel}" "${ROOTFS}/lib/modules/${KVER}/${rel}"
done

echo "--- init script"
install -m 0755 "${HERE}/guest_init.sh" "${ROOTFS}/init"
install -m 0755 "${HERE}/driver.py" "${ROOTFS}/opt/wifucked/driver.py"

echo "--- packing cpio"
( cd "${ROOTFS}" && find . -print0 | cpio --null -ov --format=newc 2>/dev/null | gzip -1 > "${OUT}" )
echo "initramfs written to ${OUT} ($(du -h "${OUT}" | cut -f1))"
