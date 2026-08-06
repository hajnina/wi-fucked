#!/bin/bash
# Builds the appliance-guest initramfs for the WAN-chaos download proof —
# the same payload as build_initramfs.sh (busybox, real ip/nft/wg/sysctl/tc,
# real Python, the real wifucked package, WireGuard+CAKE kernel modules),
# just with chaos_guest_init.sh/chaos_driver.py installed as /init instead
# of guest_init.sh/driver.py. See build_initramfs.sh's own header for why
# this doesn't duplicate that file's logic.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ROOTFS_NAME="initramfs-chaos-root" \
OUT_NAME="initramfs-chaos.cpio.gz" \
INIT_SCRIPT="${HERE}/chaos_guest_init.sh" \
DRIVER_SCRIPT="${HERE}/chaos_driver.py" \
DRIVER_DEST="chaos_driver.py" \
    "${HERE}/build_initramfs.sh"
