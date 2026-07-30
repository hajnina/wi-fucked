#!/bin/bash
#
# Base image provisioning. Runs inside the chroot during the image bake.
#
# This installs packages and lays down the systemd units. It deliberately does
# NOT generate LAN identity — that happens once, on the device, at first boot
# (ADR-012), because identity derives from the Pi's serial and there is no Pi
# here. Baking it in the image would give every device the same SSID.
#
set -euo pipefail

exec > >(tee -a /var/log/dirty-setup.log) 2>&1
echo "=== dirty: base provisioning starting at $(date -u +%FT%TZ) ==="

REPO="${REPO:-/tmp/repo}"
BASE_DIR=/opt/dirty
STATE_DIR=/var/lib/dirty

# --- packages ---------------------------------------------------------------

echo "--- installing packages"
apt-get update
# shellcheck disable=SC2046 # deliberate word splitting: one package per line
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    $(grep -vE '^\s*(#|$)' "${REPO}/apt_deps.txt" | tr '\n' ' ')

# --- directory layout -------------------------------------------------------
#
# Versions live side by side and `current` is a symlink, so an update is an
# atomic symlink swap and a rollback is swapping it back.

echo "--- creating layout"
mkdir -p "${BASE_DIR}/versions" "${BASE_DIR}/snapshots" "${STATE_DIR}"

# --- systemd units ----------------------------------------------------------

echo "--- installing units and configuration"
cp -r "${REPO}/stage-custom/etc/." /etc/
cp -r "${REPO}/stage-custom/opt/." /opt/
chmod 755 /opt/dirty/*.sh

# hostapd and dnsmasq are enabled independently of dirty.service. If the daemon
# is absent, crashed, or being updated, the AP keeps serving (ADR-011). Do not
# add a dependency between them.
systemctl unmask hostapd || true
systemctl enable hostapd
systemctl enable dnsmasq
systemctl enable dirty-firstboot.service
systemctl enable dirty-bootcount.service
systemctl enable dirty.service

# NetworkManager must not manage the AP interface or fight us over the WAN
# routes we install.
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/10-dirty.conf <<'EOF'
[main]
dns=none

[keyfile]
unmanaged-devices=interface-name:ap0;interface-name:wg0
EOF

# --- forwarding -------------------------------------------------------------

cat > /etc/sysctl.d/90-dirty.conf <<'EOF'
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
# The appliance holds multiple default routes; strict reverse-path filtering
# drops replies that arrive on a different WAN than the request left by.
net.ipv4.conf.all.rp_filter=2
EOF

# --- SD card survival -------------------------------------------------------
#
# Write wear and power-loss corruption are the dominant field failure modes on
# this hardware (docs/hardware.md), so hot writes go to tmpfs and telemetry is
# flushed periodically (ADR-010).

cat > /etc/tmpfiles.d/dirty.conf <<'EOF'
d /run/dirty 0755 root root -
EOF

sed -i 's/#Storage=auto/Storage=volatile/' /etc/systemd/journald.conf || true
sed -i 's/#RuntimeMaxUse=/RuntimeMaxUse=32M/' /etc/systemd/journald.conf || true

# --- capability check -------------------------------------------------------
#
# Fail the bake here rather than shipping an image that cannot enforce policy.
# CI asserts the same things, but a broken base image should not get that far.

echo "--- verifying data-plane capability"
# `uname -r` inside the qemu-aarch64-static chroot reports the HOST kernel
# (the syscall isn't virtualized), not the Pi kernel whose modules are on
# disk here. Plain `modinfo` looks up /lib/modules/$(uname -r) and always
# misses. Point it at the kernel version actually installed in the image.
KVER=$(find /lib/modules -mindepth 1 -maxdepth 1 -printf '%f\n' | head -n1)
modinfo -k "${KVER}" sch_cake  > /dev/null || { echo "FATAL: sch_cake missing";  exit 1; }
modinfo -k "${KVER}" wireguard > /dev/null || { echo "FATAL: wireguard missing"; exit 1; }
command -v nft > /dev/null || { echo "FATAL: nft missing"; exit 1; }
command -v tc  > /dev/null || { echo "FATAL: tc missing"; exit 1; }
command -v hostapd > /dev/null || { echo "FATAL: hostapd missing"; exit 1; }

echo "=== dirty: base provisioning complete at $(date -u +%FT%TZ) ==="
