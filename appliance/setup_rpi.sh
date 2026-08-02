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

exec > >(tee -a /var/log/wifucked-setup.log) 2>&1
echo "=== wifucked: base provisioning starting at $(date -u +%FT%TZ) ==="

REPO="${REPO:-/tmp/repo}"
BASE_DIR=/opt/wifucked
STATE_DIR=/var/lib/wifucked

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
chmod 755 /opt/wifucked/*.sh

# hostapd and dnsmasq are enabled independently of wifucked.service. If the daemon
# is absent, crashed, or being updated, the AP keeps serving (ADR-011). Do not
# add a dependency between them.
systemctl unmask hostapd || true
systemctl enable hostapd
systemctl enable dnsmasq
systemctl enable wifucked-firstboot.service
systemctl enable wifucked-bootcount.service
systemctl enable wifucked.service
systemctl enable wifucked-watchdog.timer

# TEMPORARY, DESTRUCTIVE, bring-up only — see wifucked-console.service and
# hdmi_console.sh. This exists because the first real-hardware boots have
# been failing with no way to observe why (no AP, no console). It trades
# away two things this project otherwise protects on purpose:
#   - the SD card survival story (ADR-010, docs/hardware.md): this puts a
#     live log stream on tty1 and persistent boot logs on disk, which is
#     exactly the continuous-write pattern that architecture exists to avoid.
#   - the "ACT LED is the only status channel" design (docs/hardware.md):
#     this assumes a monitor is attached, which a shipped device will not
#     have.
# DELETE this unit once devices boot reliably and reachably. Do not let it
# reach a "production" image.
systemctl enable wifucked-console.service

# NetworkManager must not manage the AP interface or fight us over the WAN
# routes we install. The AP radio is always named wlan0 on this hardware (one
# onboard radio, per docs/hardware.md) and hostapd splits it into wlan0_1 plus
# per-profile VLAN subinterfaces (wlan0.<vlan>, wlan0_1.<vlan> — see
# lan_ifname_for_profile()); the glob covers the base BSS and every
# subinterface hostapd creates under it. Previously this listed "ap0", an
# interface that has never existed on this hardware — NetworkManager kept
# managing the real AP radio and its wpa_supplicant backend contended with
# hostapd for wlan0, which is consistent with "no SSID ever appears" (#15).
systemctl unmask systemd-networkd || true
systemctl enable systemd-networkd
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/10-wifucked.conf <<'EOF'
[main]
dns=none

[keyfile]
unmanaged-devices=interface-name:wlan0*;interface-name:wg0
EOF

# --- USB OTG host mode -------------------------------------------------------
#
# The single micro-USB port is the primary non-Wi-Fi WAN (docs/hardware.md) —
# phone tethering, a USB Ethernet dongle. It must always come up as a USB
# *host* so it can enumerate whatever is plugged in. Left to the dwc2
# controller's default ID-pin sensing, role negotiation is not reliable across
# every cable/adapter combination: the port can come up not requesting the
# host role, in which case a phone just charges and never sees a data
# connection. This is never a gadget port on this hardware, so force host mode
# unconditionally rather than trust auto-negotiation.
echo "--- forcing USB OTG host mode"
cat >> /boot/config.txt <<'EOF'

# WI-FUCKED: the OTG port is always a USB host (tethering/USB-Ethernet WAN),
# never a gadget. Do not rely on ID-pin auto-negotiation for this.
dtoverlay=dwc2,dr_mode=host
EOF

# --- TEMPORARY: verbose boot on HDMI -----------------------------------------
#
# See wifucked-console.service. `quiet`/`splash`/a low loglevel hide exactly
# the kernel and systemd messages that matter while chasing a boot that
# produces no AP and no visible failure. DELETE this block along with that
# unit once bring-up is done — a shipped device should boot quietly.
echo "--- enabling verbose boot output"
if [ -f /boot/cmdline.txt ]; then
    sed -i -E 's/\bquiet\b//g; s/\bsplash\b//g; s/\bloglevel=[0-9]+\b//g' /boot/cmdline.txt
    sed -i -E 's/[[:space:]]+/ /g; s/^ +//; s/ +$//' /boot/cmdline.txt
    grep -q '\bconsole=tty1\b' /boot/cmdline.txt || sed -i 's/$/ console=tty1/' /boot/cmdline.txt
fi

# --- forwarding -------------------------------------------------------------

cat > /etc/sysctl.d/90-wifucked.conf <<'EOF'
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

cat > /etc/tmpfiles.d/wifucked.conf <<'EOF'
d /run/wifucked 0755 root root -
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

echo "=== wifucked: base provisioning complete at $(date -u +%FT%TZ) ==="
