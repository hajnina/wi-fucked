#!/bin/bash
#
# Factory reset with no button (ADR-015).
#
# Three power cycles within 60 seconds of boot means the user is deliberately
# asking for a reset — that does not happen by accident. A successful 60 seconds
# of uptime clears the counter.
#
# This resets WAN configuration ONLY. SSIDs, BSSID and LAN passphrases survive,
# so the user never loses their network — and lands back on a working dashboard
# to reconfigure. Resetting your WANs must never cost you your LAN.
#
set -euo pipefail

STATE_DIR=/var/lib/dirty
COUNTER="${STATE_DIR}/.bootcount"
THRESHOLD=3
CLEAR_AFTER=60

mkdir -p "${STATE_DIR}"

count=0
[[ -f "${COUNTER}" ]] && count="$(cat "${COUNTER}" 2>/dev/null || echo 0)"
count=$((count + 1))
echo "${count}" > "${COUNTER}"
echo "dirty-bootcount: boot ${count} of ${THRESHOLD}"

if [[ "${count}" -ge "${THRESHOLD}" ]]; then
    echo "dirty-bootcount: FACTORY RESET — clearing WAN configuration"

    # WAN configuration and learned history go.
    rm -f "${STATE_DIR}/atomics.json" "${STATE_DIR}/config.json"
    rm -f /etc/NetworkManager/system-connections/*.nmconnection 2>/dev/null || true

    # LAN identity, the telemetry database, and the firstboot sentinel stay.
    # Losing the sentinel would regenerate the SSIDs and disconnect every client
    # in the house to fix an unrelated problem.

    rm -f "${COUNTER}"
    logger -t dirty "factory reset performed via boot count; LAN identity preserved"
    systemctl restart dirty.service || true
    exit 0
fi

# Clear the counter once we have been up long enough to call this boot a
# success. If this path fails, every third reboot becomes a reset — which is why
# it runs detached and unconditionally.
(
    sleep "${CLEAR_AFTER}"
    rm -f "${COUNTER}"
    echo "dirty-bootcount: uptime threshold reached; counter cleared"
) &

exit 0
