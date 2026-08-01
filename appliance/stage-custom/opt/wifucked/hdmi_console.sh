#!/bin/bash
#
# TEMPORARY bring-up aid — see wifucked-console.service for why this exists
# and why it must not stay. Puts a live status snapshot and a streaming log
# tail on the physical HDMI output, so the device can be debugged with a
# monitor and nothing else while it cannot yet be reached over the network.
#
set -uo pipefail
# Deliberately no -e: a single failing status command (radio not up yet,
# hostapd not running yet) must not take the console down — showing "no
# such interface" IS the useful information here.

status_snapshot() {
    echo
    echo "================ status @ $(date -u +%FT%TZ) ================"
    echo "--- identity / boot"
    echo "sentinel: $([ -f /var/lib/wifucked/.identity-generated ] && echo present || echo MISSING)"
    echo "bootcount: $(cat /var/lib/wifucked/.bootcount 2>/dev/null || echo 0)"
    echo
    echo "--- units"
    systemctl --no-pager --plain list-units \
        'hostapd.service' 'dnsmasq.service' 'wifucked.service' \
        'wifucked-firstboot.service' 'wifucked-bootcount.service' 2>&1
    echo
    echo "--- radio / network"
    iw dev 2>&1
    ip -brief addr 2>&1
    echo
    echo "--- tunnel"
    wg show 2>&1
    echo
    echo "--- power"
    vcgencmd get_throttled 2>&1
    echo "==============================================================="
    echo
}

clear
echo "WI-FUCKED — TEMPORARY HDMI debug console"
echo "This is bring-up scaffolding, not the product's status channel."
echo "Remove wifucked-console.service once the device boots cleanly."
status_snapshot

# Refresh the status block on a timer, interleaved with the live journal
# stream below — good enough for a throwaway console, not worth a TUI.
( while true; do sleep 15; status_snapshot; done ) &
REFRESHER=$!
trap 'kill "${REFRESHER}" 2>/dev/null' EXIT

echo "--- streaming logs (journalctl -f) ---"
exec journalctl -f -o short-iso --no-hostname
