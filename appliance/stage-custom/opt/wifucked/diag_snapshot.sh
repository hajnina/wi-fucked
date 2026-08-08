#!/bin/bash
#
# DIAGNOSIS-PHASE tool (2026-08-08): a persistent, timestamped snapshot of
# every layer between "hostapd starts" and "a client has an address," taken
# every minute (wifucked-diag-snapshot.timer) rather than once at boot — the
# state that matters (radio association, DHCP, whether the daemon is
# crash-looping) can change at any point during a live test, not just at
# startup, and a single boot-time sample cannot show that.
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
# Bounded, same reasoning as the daemon's own RotatingFileHandler
# (logging.py): a snapshot every minute forever is a real SD-wear cost, so
# once the target file passes _MAX_BYTES this keeps only the tail before
# appending, rather than growing without limit.
#
# Remove (this script and its .service/.timer) once docs/active-tests.md's
# "AP bring-up" entry is CONFIRMED and this stops being the active diagnostic
# path.
#
set -uo pipefail

_LOG=/var/log/wifucked-boot.log
_MAX_BYTES=$((16 * 1024 * 1024))
_KEEP_BYTES=$((8 * 1024 * 1024))

if [[ -f "${_LOG}" ]] && [[ "$(stat -c%s "${_LOG}" 2> /dev/null || echo 0)" -gt "${_MAX_BYTES}" ]]; then
    tail -c "${_KEEP_BYTES}" "${_LOG}" > "${_LOG}.tmp" && mv "${_LOG}.tmp" "${_LOG}"
fi

exec >> "${_LOG}" 2>&1

echo "=== wifucked-diag-snapshot: $(date -u +%FT%TZ) (uptime $(cut -d' ' -f1 /proc/uptime 2> /dev/null || echo '?')s) ==="

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
