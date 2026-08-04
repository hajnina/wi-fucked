#!/bin/bash
# Fetches an x86_64 Linux kernel + matching kernel-module set with WireGuard
# and CAKE support, for the QEMU packet-routing proof.
#
# Why not the host kernel: the sandbox this was built in has neither
# `CONFIG_WIREGUARD` nor `CONFIG_NET_SCH_CAKE` (confirmed via
# `/proc/config.gz`), and there is no `/boot/vmlinuz` to boot as a guest
# kernel even if it did. Alpine's netboot kernel is a few MB, ships as a
# plain bzImage QEMU can `-kernel` boot directly, and its separate
# `modloop` squashfs carries the two modules this proof needs plus their
# dependency closure — matched to the exact same kernel build, which
# matters because kernel modules are ABI-locked to the kernel they were
# built against.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${QEMU_TEST_WORKDIR:-${HERE}/.work}"
KERNEL_DIR="${WORK}/kernel"
mkdir -p "${KERNEL_DIR}"

ALPINE_VER="v3.19"
BASE="https://dl-cdn.alpinelinux.org/alpine/${ALPINE_VER}/releases/x86_64/netboot"

if [ ! -f "${KERNEL_DIR}/vmlinuz-lts" ]; then
    echo "--- fetching vmlinuz-lts"
    curl -fSL --retry 3 -o "${KERNEL_DIR}/vmlinuz-lts" "${BASE}/vmlinuz-lts"
fi

if [ ! -f "${KERNEL_DIR}/modloop-lts" ]; then
    echo "--- fetching modloop-lts (~150 MB; only the module closure in"
    echo "    module_closure.txt gets copied into the initramfs afterward)"
    curl -fSL --retry 3 -o "${KERNEL_DIR}/modloop-lts" "${BASE}/modloop-lts"
fi

if [ ! -d "${KERNEL_DIR}/modloop-extract" ]; then
    echo "--- extracting modloop-lts (squashfs)"
    command -v unsquashfs >/dev/null 2>&1 || {
        echo "FATAL: unsquashfs not found — apt-get install squashfs-tools" >&2
        exit 1
    }
    unsquashfs -d "${KERNEL_DIR}/modloop-extract" "${KERNEL_DIR}/modloop-lts" >/dev/null
fi

echo "kernel + modules ready under ${KERNEL_DIR}"
