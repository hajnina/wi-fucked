#!/bin/bash
#
# First boot: derive this device's LAN identity, once, and never again.
#
# SSIDs, BSSID and passphrases derive from the Pi's serial and are immutable
# afterwards (ADR-012). Clients join once and stay joined for the life of the
# device — including across factory reset, which resets WAN configuration only
# (ADR-015).
#
# The sentinel is what makes this run exactly once. A bug that re-derived on
# every boot would silently change what every client sees, and would be nearly
# invisible in development where devices are reflashed rather than rebooted.
#
set -euo pipefail

SENTINEL=/var/lib/dirty/.identity-generated
STATE_DIR=/var/lib/dirty

if [[ -f "${SENTINEL}" ]]; then
    echo "dirty-firstboot: identity already generated; nothing to do"
    exit 0
fi

echo "dirty-firstboot: generating LAN identity"
mkdir -p "${STATE_DIR}" /etc/hostapd

SERIAL="$(awk '/^Serial/ {print $3}' /proc/cpuinfo 2>/dev/null || true)"
if [[ -z "${SERIAL}" ]]; then
    # No serial means we cannot derive a stable identity. A random one is worse
    # than none: it would change on the next boot that also fails to read it.
    SERIAL="$(cat /etc/machine-id)"
    echo "dirty-firstboot: WARNING no CPU serial; falling back to machine-id"
fi

# Probe what the radio can actually do and pick the LAN layout accordingly
# (ADR-014). Two BSS is preferred; one SSID with two PSKs is the sanctioned
# fallback when the driver refuses.
LAN_MODE=two_bss
if ! iw phy 2>/dev/null | grep -qE '#\{ AP \} <= [2-9]'; then
    LAN_MODE=two_psk
    echo "dirty-firstboot: driver reports a single AP interface; using two-PSK layout"
fi

CHANNEL=6

PYTHONPATH=/opt/dirty/current/src python3 - "${SERIAL}" "${LAN_MODE}" "${CHANNEL}" <<'PY'
import sys
from pathlib import Path

from dirty.config import LanConfig
from dirty.lan import derive_identity, dnsmasq_config, hostapd_config, wpa_psk_file
from dirty.policy import DEFAULT_PROFILES

serial, lan_mode, channel = sys.argv[1], sys.argv[2], int(sys.argv[3])
config = LanConfig(lan_mode=lan_mode)
identity = derive_identity(serial, config)

Path("/etc/hostapd/hostapd.conf").write_text(
    hostapd_config(identity, channel, lan_mode)
)
if lan_mode == "two_psk":
    psk = Path("/etc/hostapd/wpa_psk")
    psk.write_text(wpa_psk_file(identity))
    psk.chmod(0o600)

Path("/etc/dnsmasq.d/dirty.conf").write_text(dnsmasq_config(config, DEFAULT_PROFILES))

# The label card. Printed on the device, and the only way a user learns the
# passphrase — so it is written where support can also read it back.
Path("/var/lib/dirty/label.txt").write_text(
    f"""DIRTY -> BALANCED

  {identity.besteffort_ssid}
      password: {identity.passphrase}
      for: everything

  {identity.critical_ssid}
      password: {identity.critical_passphrase}
      for: work devices, calls, anything that must not drop

  dashboard: http://dirty.local  or  http://10.44.0.1

Factory reset: power-cycle three times within 60 seconds of boot.
This resets your Internet connections only. Your networks and passwords
never change.
"""
)
print(f"dirty-firstboot: generated identity for {identity.besteffort_ssid}")
PY

chmod 600 /etc/hostapd/hostapd.conf
touch "${SENTINEL}"
echo "dirty-firstboot: identity is now immutable"
