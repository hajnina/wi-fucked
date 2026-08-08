#!/bin/bash
#
# DIAGNOSIS-PHASE tool (2026-08-08): one persistent, timestamped snapshot of
# every layer between "hostapd starts" and "a client has an address," taken
# once per boot after those units have had a chance to settle.
#
# Exists because journald was volatile (Storage=volatile) and the units that
# matter most for the current "no IP handed out" investigation — hostapd,
# dnsmasq, NetworkManager, systemd-networkd — never wrote anywhere else. A
# device power-cycled between a failed test and someone reading the logs lost
# the only record of what happened. journald is now persistent too
# (setup_rpi.sh), but this file stays as a single grep-able record that does
# not depend on journald's retention window and survives being read off the
# SD card directly, same rationale as wifucked-boot.log itself (SOP-009).
#
# Remove once docs/active-tests.md's "AP bring-up" entry is CONFIRMED and this
# stops being the active diagnostic path.
#
set -uo pipefail

exec >> /var/log/wifucked-boot.log 2>&1

echo "=== wifucked-diag-snapshot: $(date -u +%FT%TZ) ==="

section() {
    echo "--- $1"
    shift
    "$@" 2>&1 || echo "(command failed: $*)"
    echo
}

section "rfkill list" rfkill list
section "nmcli device status" nmcli device status
section "ip addr show" ip addr show
section "ip route show table all" ip route show table all
section "systemctl status hostapd" systemctl status hostapd --no-pager -l
section "systemctl status dnsmasq" systemctl status dnsmasq --no-pager -l
section "systemctl status systemd-networkd" systemctl status systemd-networkd --no-pager -l
section "systemctl status NetworkManager" systemctl status NetworkManager --no-pager -l
section "hostapd_cli status" hostapd_cli status
section "hostapd_cli list_sta" hostapd_cli list_sta
section "journalctl -u hostapd -u dnsmasq (this boot)" \
    journalctl -u hostapd -u dnsmasq -b --no-pager

echo "=== wifucked-diag-snapshot: done ==="
