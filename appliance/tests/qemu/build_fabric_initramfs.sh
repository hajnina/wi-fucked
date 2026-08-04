#!/bin/bash
# Builds the initramfs for the *fabric*-side QEMU guest — a second, separate
# VM from the appliance guest, running the real `fabric.app`/`fabric.wireguard`
# code. See fabric_guest_init.sh's header for why the fabric needs to be a
# QEMU guest too, not a host network namespace: it needs real kernel
# WireGuard support, which this sandbox's own host kernel lacks.
#
# Shares almost everything with build_initramfs.sh (same busybox, same
# ip/nft/wg/sysctl binaries, same kernel module set, same Python
# interpreter+stdlib) — the only differences are the payload (fabric package
# + Flask instead of wifucked package) and the init script.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../../.." && pwd)"
WORK="${QEMU_TEST_WORKDIR:-${HERE}/.work}"
KERNEL_DIR="${WORK}/kernel"
ROOTFS="${WORK}/initramfs-fabric-root"
OUT="${WORK}/initramfs-fabric.cpio.gz"

KVER="6.6.110-0-lts"

if [ ! -f "${KERNEL_DIR}/vmlinuz-lts" ] || [ ! -d "${KERNEL_DIR}/modloop-extract" ]; then
    echo "FATAL: run ${HERE}/download_kernel.sh first." >&2
    exit 1
fi

rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}"/{bin,sbin,lib,lib64,proc,sys,dev,etc,root,run}
mkdir -p "${ROOTFS}/lib/x86_64-linux-gnu"
mkdir -p "${ROOTFS}/lib/modules/${KVER}"
mkdir -p "${ROOTFS}/opt/fabric/src"
mkdir -p "${ROOTFS}/var/lib/fabric"

echo "--- busybox"
cp /bin/busybox "${ROOTFS}/bin/busybox"

copy_with_libs() {
    local bin="$1"
    local dest="$2"
    cp "${bin}" "${ROOTFS}${dest}"
    ldd "${bin}" 2>/dev/null | awk '{print $1, $3}' | while read -r _name path; do
        if [ -n "${path:-}" ] && [ -f "${path}" ]; then
            mkdir -p "${ROOTFS}$(dirname "${path}")"
            cp -n "${path}" "${ROOTFS}${path}" 2>/dev/null || true
        fi
    done
    ldd "${bin}" 2>/dev/null | grep -oE '/lib64/ld-linux-x86-64\.so\.2' | while read -r p; do
        mkdir -p "${ROOTFS}$(dirname "${p}")"
        cp -n "${p}" "${ROOTFS}${p}" 2>/dev/null || true
    done
}

echo "--- ip / nft / wg / sysctl"
mkdir -p "${ROOTFS}/usr/sbin" "${ROOTFS}/usr/bin"
copy_with_libs "$(command -v ip)" "/usr/sbin/ip"
copy_with_libs "$(command -v nft)" "/usr/sbin/nft"
copy_with_libs "$(command -v wg)" "/usr/bin/wg"
copy_with_libs "$(command -v sysctl)" "/usr/sbin/sysctl"

echo "--- python3 interpreter + stdlib"
PY_BIN="$(readlink -f "$(command -v python3)")"
PY_PREFIX="$(python3 -c 'import sys; print(sys.prefix)')"
copy_with_libs "${PY_BIN}" "/usr/bin/python3.11"
ln -sf python3.11 "${ROOTFS}/usr/bin/python3"
mkdir -p "${ROOTFS}/usr/lib"
cp -a "${PY_PREFIX}/lib/python3.11" "${ROOTFS}/usr/lib/python3.11"
rm -rf "${ROOTFS}/usr/lib/python3.11/test"
find "${ROOTFS}/usr/lib/python3.11" -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
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

echo "--- flask + its runtime deps (the real fabric.app needs a real WSGI stack)"
mkdir -p "${ROOTFS}/usr/lib/python3.11/site-packages"
for pkg in flask werkzeug jinja2 itsdangerous click markupsafe blinker; do
    python3 - "$pkg" <<'PYEOF'
import importlib, importlib.metadata, os, sys
name = sys.argv[1]
mod = importlib.import_module(name)
print(os.path.dirname(mod.__file__))
# werkzeug's dev server queries importlib.metadata for its own version at
# startup — without the matching *.dist-info alongside the package, that
# raises PackageNotFoundError and the whole app.run() call fails.
try:
    print(str(importlib.metadata.distribution(name)._path))
except importlib.metadata.PackageNotFoundError:
    pass
PYEOF
done | while read -r pkgdir; do
    cp -a "${pkgdir}" "${ROOTFS}/usr/lib/python3.11/site-packages/"
done
find "${ROOTFS}/usr/lib/python3.11/site-packages" -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

echo "--- fabric package (verbatim, the real code under test)"
cp -a "${REPO_ROOT}/fabric/src/fabric" "${ROOTFS}/opt/fabric/src/fabric"
find "${ROOTFS}/opt/fabric/src/fabric" -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

echo "--- kernel modules"
MODSRC="${KERNEL_DIR}/modloop-extract/modules/${KVER}"
grep -v '^#' "${HERE}/module_closure.txt" | grep -v '^[[:space:]]*$' | while read -r rel; do
    mkdir -p "${ROOTFS}/lib/modules/${KVER}/$(dirname "${rel}")"
    cp "${MODSRC}/${rel}" "${ROOTFS}/lib/modules/${KVER}/${rel}"
done

echo "--- init script"
install -m 0755 "${HERE}/fabric_guest_init.sh" "${ROOTFS}/init"
install -m 0755 "${HERE}/fabric_server.py" "${ROOTFS}/opt/fabric/fabric_server.py"

echo "--- packing cpio"
( cd "${ROOTFS}" && find . -print0 | cpio --null -ov --format=newc 2>/dev/null | gzip -1 > "${OUT}" )
echo "fabric initramfs written to ${OUT} ($(du -h "${OUT}" | cut -f1))"
