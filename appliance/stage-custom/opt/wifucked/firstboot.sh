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

# journald is RAM-only on this image (Storage=volatile, setup_rpi.sh) to
# protect the SD card from wear, so a failure here leaves nothing to read
# once the device is power-cycled — exactly the case where there is no
# console yet either. Mirror the bake log's pattern: a small, persistent,
# on-disk record that survives a reboot and can be read by pulling the SD
# card, without needing the daemon, the AP, or a live console.
exec > >(tee -a /var/log/wifucked-boot.log) 2>&1

SENTINEL=/var/lib/wifucked/.identity-generated
STATE_DIR=/var/lib/wifucked

if [[ -f "${SENTINEL}" ]]; then
    echo "wifucked-firstboot: identity already generated; nothing to do"
    exit 0
fi

echo "wifucked-firstboot: generating LAN identity"
mkdir -p "${STATE_DIR}" /etc/hostapd

SERIAL="$(awk '/^Serial/ {print $3}' /proc/cpuinfo 2>/dev/null || true)"
if [[ -z "${SERIAL}" ]]; then
    # No serial means we cannot derive a stable identity. A random one is worse
    # than none: it would change on the next boot that also fails to read it.
    SERIAL="$(cat /etc/machine-id)"
    echo "wifucked-firstboot: WARNING no CPU serial; falling back to machine-id"
fi

# Probe what the radio can actually do and pick the LAN layout accordingly
# (ADR-014). Two BSS is preferred; one SSID with two PSKs is the sanctioned
# fallback when the driver refuses.
LAN_MODE=two_bss
if ! iw phy 2>/dev/null | grep -qE '#\{ AP \} <= [2-9]'; then
    LAN_MODE=two_psk
    echo "wifucked-firstboot: driver reports a single AP interface; using two-PSK layout"
fi

CHANNEL=6

PYTHONPATH=/opt/wifucked/current/src python3 - "${SERIAL}" "${LAN_MODE}" "${CHANNEL}" <<'PY'
import sys
from pathlib import Path

from wifucked.config import LanConfig
from wifucked.lan import derive_identity, dnsmasq_config, hostapd_config, wpa_psk_file
from wifucked.policy import DEFAULT_PROFILES

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

Path("/etc/dnsmasq.d/wifucked.conf").write_text(dnsmasq_config(config, DEFAULT_PROFILES))

# The label card. Printed on the device, and the only way a user learns the
# passphrase — so it is written where support can also read it back.
Path("/var/lib/wifucked/label.txt").write_text(
    f"""WI-FUCKED -> BALANCED

  {identity.besteffort_ssid}
      password: {identity.passphrase}
      for: everything

  {identity.critical_ssid}
      password: {identity.critical_passphrase}
      for: work devices, calls, anything that must not drop

  dashboard: http://wifucked.local  or  http://10.44.0.1

Factory reset: power-cycle three times within 60 seconds of boot.
This resets your Internet connections only. Your networks and passwords
never change.
"""
)
print(f"wifucked-firstboot: generated identity for {identity.besteffort_ssid}")
PY

chmod 600 /etc/hostapd/hostapd.conf

# The WireGuard identity that attaches this device to the fabric. Generated
# on-device, once, and never baked into an image or a release artifact — the
# private key must never leave this Pi (SOP-008, "Never ship a secret"; CI greps
# built packages for key-shaped content). Guarded independently of the identity
# sentinel so a key is never silently regenerated (which would invalidate the
# device's fabric registration) even if this block is reached again.
if [[ ! -f /etc/wireguard/wifucked-privatekey ]]; then
    echo "wifucked-firstboot: generating WireGuard keypair"
    install -d -m 700 /etc/wireguard
    ( umask 077; wg genkey | tee /etc/wireguard/wifucked-privatekey | wg pubkey \
        > /etc/wireguard/wifucked-publickey )
    chmod 600 /etc/wireguard/wifucked-privatekey
    echo "wifucked-firstboot: WireGuard keypair generated (private key stays on-device)"
else
    echo "wifucked-firstboot: WireGuard keypair already present; leaving it"
fi

touch "${SENTINEL}"
echo "wifucked-firstboot: identity is now immutable"
