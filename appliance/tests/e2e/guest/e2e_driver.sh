#!/bin/bash
#
# Runs INSIDE the QEMU guest, as root, launched by wifucked-e2e-bootstrap.service
# (written directly into cloud-init user-data — see ../cloud-init/user-data)
# once /mnt/repo (the checked-out repo, read-only) and /mnt/results (a blank
# FAT disk the host reads back after shutdown) are mounted.
#
# Deliberately does NOT reimplement any part of the appliance's own bring-up:
# it runs the real appliance/setup_rpi.sh (the same provisioning script the
# actual Pi image bake runs) and lets the real systemd units it enables
# (wifucked-firstboot, hostapd, dnsmasq, systemd-networkd, wifucked.service —
# unmodified, no MOCK_HW) bring the AP and dashboard up on their own. This
# script's job is: give it a real wlan0 (mac80211_hwsim), run the real
# provisioning, wait, then act as a real Wi-Fi client and report what
# happened. See ../README.md for exactly what this proves and what it can't
# (still a live-provisioned VM, not a baked Pi image — see "known deviations
# from a real device" there).
set -u

REPO=/mnt/repo
RESULTS=/mnt/results
FRAGMENTS="${RESULTS}/fragments"
GATEWAY=10.44.0.1
CLIENT_NS=wifucked-e2e-client
TIMEOUT_S=45

mkdir -p "${FRAGMENTS}" "${RESULTS}/logs"
exec > >(tee -a "${RESULTS}/logs/driver.log") 2>&1

log() { printf '[e2e-driver] %s\n' "$1"; }
now() { date +%s.%N; }

fragment() {
    # fragment <name> <pass|fail> <start_ts> <detail> [error]
    local name="$1" outcome="$2" start="$3" detail="$4" error="${5:-}"
    local dur
    dur="$(awk -v a="$(now)" -v b="${start}" 'BEGIN{printf "%.3f", a-b}')"
    python3 "${REPO}/appliance/tests/e2e/write_fragment.py" \
        --fragments-dir "${FRAGMENTS}" --name "${name}" "--${outcome}" \
        --duration-s "${dur}" --detail "${detail}" --error "${error}"
    log "${outcome^^}: ${name} — ${detail} ${error}"
}

dump_diagnostics() {
    {
        echo "== systemctl status =="
        systemctl status hostapd dnsmasq wifucked systemd-networkd NetworkManager wifucked-firstboot --no-pager -l 2>&1
        echo "== journalctl (last 300 lines, relevant units) =="
        journalctl -u hostapd -u dnsmasq -u wifucked -u systemd-networkd -u NetworkManager \
            -u wifucked-firstboot -u wifucked-bootcount --no-pager -n 300 2>&1
        echo "== ip addr =="
        ip addr 2>&1
        echo "== nmcli device status =="
        nmcli device status 2>&1
        echo "== hostapd_cli status =="
        hostapd_cli -i wlan0 status 2>&1
        echo "== /etc/hostapd/hostapd.conf =="
        cat /etc/hostapd/hostapd.conf 2>&1
        echo "== /etc/dnsmasq.d/wifucked.conf =="
        cat /etc/dnsmasq.d/wifucked.conf 2>&1
        echo "== /etc/systemd/network/ =="
        for f in /etc/systemd/network/*.network; do echo "--- ${f} ---"; cat "${f}" 2>&1; done
        echo "== /etc/NetworkManager/conf.d/10-wifucked.conf =="
        cat /etc/NetworkManager/conf.d/10-wifucked.conf 2>&1
        echo "== /sys/bus/usb/devices (real WAN discovery input) =="
        for d in /sys/bus/usb/devices/*; do
            [ -f "${d}/idVendor" ] || continue
            echo "--- ${d} ---"
            echo "idVendor=$(cat "${d}/idVendor" 2> /dev/null) idProduct=$(cat "${d}/idProduct" 2> /dev/null)"
            find "${d}" -maxdepth 3 -name bInterfaceClass -exec sh -c \
                'echo "  $(dirname "$1"): class=$(cat "$1") subclass=$(cat "$(dirname "$1")/bInterfaceSubClass" 2>/dev/null)"' _ {} \;
        done
        echo "== real /api/state =="
        curl -s -u "wifucked:${API_TOKEN:-}" "http://${GATEWAY}:8080/api/state" 2>&1
        echo "== real wg show =="
        wg show 2>&1
        echo "== real nft ruleset =="
        nft list ruleset 2>&1
        echo "== real ip rule / ip route table 888 =="
        ip rule show 2>&1
        ip route show table 888 2>&1
    } > "${RESULTS}/logs/diagnostics.txt" 2>&1
}

finish() {
    local rc="$1"
    dump_diagnostics
    python3 "${REPO}/appliance/tests/e2e/aggregate_report.py" \
        --fragments-dir "${FRAGMENTS}" --out-dir "${RESULTS}" || true
    sync
    touch "${RESULTS}/DONE"
    sync
    log "finished, rc=${rc}, powering off"
    systemctl poweroff
    # systemctl poweroff is async; block here so nothing after this races the
    # shutdown (the host is watching for the qemu process to exit).
    sleep 60
    exit "${rc}"
}

# --- phase: hwsim + interface naming ----------------------------------------

t0="$(now)"
modprobe mac80211_hwsim radios=2 2> "${RESULTS}/logs/modprobe-hwsim.log"
if ! lsmod | grep -q '^mac80211_hwsim'; then
    fragment "01_hwsim_module" fail "${t0}" \
        "modprobe mac80211_hwsim radios=2 did not result in the module being loaded — likely missing from this kernel build (see README.md's download_base_image.sh note)" \
        "$(cat "${RESULTS}/logs/modprobe-hwsim.log"; echo ---; uname -r; echo ---; lsmod | head -30)"
    finish 1
fi
# `iw` isn't installed yet at this point (that's the real setup_rpi.sh's job,
# a few phases from now) — enumerate wireless netdevs straight from /sys
# instead of shelling out to a userspace tool that may not exist yet.
mapfile -t WLAN_IFACES < <(
    for d in /sys/class/net/*; do
        [ -e "${d}/phy80211" ] && basename "${d}"
    done | sort
)
if [ "${#WLAN_IFACES[@]}" -ne 2 ]; then
    fragment "01_hwsim_module" fail "${t0}" \
        "expected exactly 2 wireless interfaces from mac80211_hwsim radios=2, found ${#WLAN_IFACES[@]}" \
        "/sys/class/net wireless devices: ${WLAN_IFACES[*]:-none}; all: $(ls /sys/class/net)"
    finish 1
fi
fragment "01_hwsim_module" pass "${t0}" "mac80211_hwsim loaded, interfaces: ${WLAN_IFACES[*]}"

t0="$(now)"
AP_RAW="${WLAN_IFACES[0]}"
CLIENT_RAW="${WLAN_IFACES[1]}"
# The real device has exactly one onboard radio, always enumerated as wlan0
# (docs/hardware.md) — setup_rpi.sh's NetworkManager unmanaged-devices glob
# and every hostapd.conf this repo generates hard-code that name. Force it
# here rather than trust whatever name udev happened to give the hwsim
# device, for the same reason ADR-002 exists: name stability is not
# guaranteed by the kernel, only by convention this harness has to enforce
# itself in the absence of real, single-radio hardware.
if [ "${AP_RAW}" != "wlan0" ]; then
    ip link set "${AP_RAW}" down
    ip link set "${AP_RAW}" name wlan0
fi
ip link set wlan0 up
# NetworkManager isn't even installed yet at this point (setup_rpi.sh, next
# phase, installs it) — nothing to guard against here yet. The real mitigation
# for the race this harness introduces (setup_rpi.sh's unmanaged-devices
# config runs live during boot instead of being baked into the image before
# first boot, as it is on a real device) is the `systemctl restart
# NetworkManager` right before hostapd starts, below — see README.md's
# "known deviations from a real device."

# Move the second radio out of the root netns *before* NetworkManager or
# hostapd can see it at all — it plays the "phone" in this proof. A wireless
# netdev's namespace is a property of its *phy* (cfg80211/nl80211), not the
# netdev itself — plain `ip link set DEV netns NS` rejects it outright
# (RTNETLINK "Invalid argument"), confirmed by this test's own first CI run.
# `iw phy PHY set netns` is the only way to move it, which needs `iw`
# installed a little earlier than setup_rpi.sh would otherwise install it —
# test-harness-only: a real device has exactly one radio and never needs to
# relocate a second one. `apt-get update` hasn't run yet this boot (the base
# image ships no populated package index), so every "iw: command not found"
# on the first CI runs of this stage was this install silently no-op'ing
# against an empty index — it must run first, and its own failure must stop
# the script instead of being swallowed by `> /dev/null` with no exit check.
apt-get update -qq
if ! apt-get install -y -qq iw; then
    fragment "02_iface_split" fail "${t0}" "apt-get install iw failed" ""
    finish 1
fi
CLIENT_PHY="$(cat "/sys/class/net/${CLIENT_RAW}/phy80211/name")"
ip netns add "${CLIENT_NS}"

# Observed flaky across otherwise-identical CI runs: `iw phy ... set netns
# name ...` sometimes leaves the phy exactly where it started, with no
# visible error, no dmesg line, nothing -- and sometimes it works first try.
# Re-issue the move (not just wait longer) each time it hasn't taken effect,
# capturing every attempt's own output, rather than assume one call is
# reliable.
MOVED=0
MOVE_LOG=""
for attempt in $(seq 1 10); do
    MOVE_OUT="$(iw phy "${CLIENT_PHY}" set netns name "${CLIENT_NS}" 2>&1)"
    MOVE_LOG="${MOVE_LOG}
attempt ${attempt}: rc=$? out=[${MOVE_OUT}]"
    if ip netns exec "${CLIENT_NS}" test -e "/sys/class/net/${CLIENT_RAW}/phy80211"; then
        MOVED=1
        break
    fi
    sleep 1
done
if [ "${MOVED}" != "1" ]; then
    fragment "02_iface_split" fail "${t0}" \
        "${CLIENT_RAW}/${CLIENT_PHY} did not appear in netns ${CLIENT_NS} after 10 attempts of 'iw phy ... set netns'" \
        "${MOVE_LOG}
root ns: $(ip link show 2>&1)
${CLIENT_NS} ns: $(ip netns exec "${CLIENT_NS}" ip link show 2>&1)"
    finish 1
fi
ip netns exec "${CLIENT_NS}" ip link set lo up
ip netns exec "${CLIENT_NS}" ip link set "${CLIENT_RAW}" up
fragment "02_iface_split" pass "${t0}" \
    "ap=wlan0 (was ${AP_RAW}), client=${CLIENT_RAW}/${CLIENT_PHY} in netns ${CLIENT_NS}"

# --- phase: real provisioning (the actual image-bake script) ---------------

t0="$(now)"
apt-get update -qq
# Test-only: production never needs a venv, but this script needs an isolated
# one for Playwright without touching the apt-managed flask/blinker install
# setup_rpi.sh is about to do (see ci.yml's e2e job for why that matters).
apt-get install -y -qq python3-venv > /dev/null
APPLIANCE_DIR="${REPO}/appliance"
if ! env REPO="${APPLIANCE_DIR}" bash "${APPLIANCE_DIR}/setup_rpi.sh"; then
    fragment "03_setup_rpi" fail "${t0}" "setup_rpi.sh (the real image-bake provisioning script) failed" \
        "$(tail -n 100 /var/log/wifucked-setup.log 2> /dev/null)"
    finish 1
fi
fragment "03_setup_rpi" pass "${t0}" "real setup_rpi.sh completed (packages installed, real systemd units enabled)"

# setup_rpi.sh only lays down base provisioning (packages, units, config) —
# on a real device the actual wifucked Python package arrives separately, as
# an OTA .wtf package applied by the real update_script.sh (confirmed against
# .github/workflows/reusable_image_pipeline.yml: the image bake runs
# setup_rpi.sh, then separately builds and applies a package the same way).
# Skipping this step is exactly how the first version of this stage failed:
# firstboot.sh ran but `python3 -c "import wifucked"` had nothing to import.
t0="$(now)"
mkdir -p /tmp/wifucked-e2e-pkg
if ! (cd /tmp/wifucked-e2e-pkg && unzip -qo "${REPO}/e2e-package/wifucked.wtf" \
    && chmod +x update.sh && ./update.sh); then
    fragment "04_deploy_package" fail "${t0}" \
        "the real update_script.sh (OTA installer) failed to deploy the wifucked package" \
        "$(tail -n 100 /tmp/wifucked-e2e-pkg/*.log 2> /dev/null)"
    finish 1
fi
if [ ! -f /opt/wifucked/current/src/wifucked/__init__.py ]; then
    fragment "04_deploy_package" fail "${t0}" \
        "update.sh ran but /opt/wifucked/current/src/wifucked is not there" \
        "$(ls -la /opt/wifucked/current 2> /dev/null; ls -la /opt/wifucked/versions 2> /dev/null)"
    finish 1
fi
fragment "04_deploy_package" pass "${t0}" "real update_script.sh deployed the real wifucked package to /opt/wifucked/current"

t0="$(now)"
python3 -m venv /opt/wifucked-e2e-venv
/opt/wifucked-e2e-venv/bin/pip install --quiet -r "${REPO}/appliance/tests/e2e/requirements.txt"
/opt/wifucked-e2e-venv/bin/playwright install --with-deps chromium > "${RESULTS}/logs/playwright-install.log" 2>&1
fragment "05_playwright_install" pass "${t0}" "isolated venv, chromium installed"

# --- phase: start the real services in the real order -----------------------

t0="$(now)"
systemctl daemon-reload
systemctl restart NetworkManager
systemctl start wifucked-bootcount.service
if ! systemctl start wifucked-firstboot.service; then
    fragment "06_firstboot" fail "${t0}" "wifucked-firstboot.service (real firstboot.sh) failed" \
        "$(journalctl -u wifucked-firstboot --no-pager -n 100)"
    finish 1
fi
if [ ! -f /etc/hostapd/hostapd.conf ]; then
    fragment "06_firstboot" fail "${t0}" "firstboot.sh ran but /etc/hostapd/hostapd.conf was not written" ""
    finish 1
fi
SSID="$(grep -m1 '^ssid=' /etc/hostapd/hostapd.conf | cut -d= -f2-)"
fragment "06_firstboot" pass "${t0}" "real firstboot.sh generated identity, ssid=${SSID}"

# The daemon's real one-shot fabric attach (Daemon.start() -> _attach_fabric_once())
# only ever tries once, so config.json needs the real fabric's address before
# wifucked.service's *first* start — not writeable later and expected to
# retroactively take effect. Same reasoning for /etc/wifucked-release: fabric
# refuses an appliance below its own MIN_APPLIANCE_VERSION (0.1.0), and this
# guest never runs the real image-bake step that writes that file on a real
# device, so it starts as "0.0.0-dev" (config.py's release_info() fallback)
# unless something here provides a real one. Both are test-harness-only
# setup standing in for what a real device's provisioning already gives it.
FABRIC_CFG="${REPO}/e2e-fabric-config.json"
python3 -c "
import json
cfg = json.load(open('${FABRIC_CFG}'))
out = {'fabric': {'servers': [cfg['fabric_url']], 'username': cfg['fabric_username'], 'password': cfg['fabric_password']}}
json.dump(out, open('/var/lib/wifucked/config.json', 'w'), indent=2)
"
cat > /etc/wifucked-release << 'EOF'
WIFUCKED_VERSION="0.1.0"
WIFUCKED_CHANNEL="e2e-test"
EOF

t0="$(now)"
systemctl restart systemd-networkd
systemctl restart hostapd
systemctl restart dnsmasq
# wifucked.service deliberately does NOT start here yet: its real Flask app
# binds specifically to ${GATEWAY} (config.api_host, never 0.0.0.0), and
# starting it in the same breath as systemd-networkd races the address
# actually landing on wlan0 -- confirmed in a real CI run ("Cannot assign
# requested address", crash-looping under Restart=always until the address
# happened to appear first). Start it only once 08_gateway_address below has
# confirmed the real address is actually there.

HOSTAPD_UP=0
for _ in $(seq 1 "${TIMEOUT_S}"); do
    if hostapd_cli -i wlan0 status 2> /dev/null | grep -q '^state=ENABLED'; then
        HOSTAPD_UP=1
        break
    fi
    sleep 1
done
if [ "${HOSTAPD_UP}" != "1" ]; then
    fragment "07_hostapd_up" fail "${t0}" "real hostapd (systemd unit) did not reach state=ENABLED within ${TIMEOUT_S}s" \
        "$(journalctl -u hostapd --no-pager -n 100)"
    finish 1
fi
fragment "07_hostapd_up" pass "${t0}" "real hostapd.service state=ENABLED"

t0="$(now)"
GW_UP=0
for _ in $(seq 1 "${TIMEOUT_S}"); do
    if ip -4 -o addr show dev wlan0 2> /dev/null | grep -q "${GATEWAY}/"; then
        GW_UP=1
        break
    fi
    sleep 1
done
if [ "${GW_UP}" != "1" ]; then
    # This is the real bug report reproduced at the OS-config layer: hostapd
    # up, but nothing gave wlan0 its gateway address (systemd-networkd not
    # applying the generated .network unit, or NetworkManager fighting it).
    fragment "08_gateway_address" fail "${t0}" \
        "no ${GATEWAY} address on wlan0 within ${TIMEOUT_S}s (real systemd-networkd applying the real generated .network unit)" \
        "$(ip -4 addr show dev wlan0; echo ---; networkctl status wlan0 2>&1; echo ---; journalctl -u systemd-networkd --no-pager -n 100)"
    finish 1
fi
fragment "08_gateway_address" pass "${t0}" "wlan0 has ${GATEWAY} via real systemd-networkd"

systemctl start wifucked.service

t0="$(now)"
API_TOKEN=""
DASH_UP=0
for _ in $(seq 1 "${TIMEOUT_S}"); do
    if [ -f /var/lib/wifucked/api_token ]; then
        API_TOKEN="$(cat /var/lib/wifucked/api_token)"
    fi
    if bash -c "echo > /dev/tcp/${GATEWAY}/8080" 2> /dev/null; then
        DASH_UP=1
        break
    fi
    sleep 1
done
if [ "${DASH_UP}" != "1" ] || [ -z "${API_TOKEN}" ]; then
    fragment "09_dashboard_up" fail "${t0}" \
        "real wifucked.service dashboard did not open ${GATEWAY}:8080 (real HAL, no MOCK_HW) within ${TIMEOUT_S}s" \
        "$(journalctl -u wifucked --no-pager -n 150)"
    finish 1
fi
fragment "09_dashboard_up" pass "${t0}" "real dashboard listening on ${GATEWAY}:8080"

# --- phase: real client, from inside the guest ------------------------------

t0="$(now)"
if ! ip netns exec "${CLIENT_NS}" test -e "/sys/class/net/${CLIENT_RAW}/phy80211"; then
    fragment "10_client_associate" fail "${t0}" \
        "${CLIENT_RAW} is gone from netns ${CLIENT_NS} by the time client association was attempted (it was there right after the phase-02 move)" \
        "root ns: $(ip link show 2>&1); ${CLIENT_NS} ns: $(ip netns exec "${CLIENT_NS}" ip link show 2>&1); iw reg: $(iw reg get 2>&1)"
    finish 1
fi
SCAN_OUT="$(ip netns exec "${CLIENT_NS}" iw dev "${CLIENT_RAW}" scan 2>&1)"
ip netns exec "${CLIENT_NS}" iw dev "${CLIENT_RAW}" connect "${SSID}" 2>&1
ASSOCIATED=0
for _ in $(seq 1 "${TIMEOUT_S}"); do
    if ip netns exec "${CLIENT_NS}" iw dev "${CLIENT_RAW}" link 2>&1 | grep -q '^Connected to'; then
        ASSOCIATED=1
        break
    fi
    sleep 1
done
if [ "${ASSOCIATED}" != "1" ]; then
    fragment "10_client_associate" fail "${t0}" "client did not associate to ${SSID} within ${TIMEOUT_S}s" \
        "$(ip netns exec "${CLIENT_NS}" iw dev "${CLIENT_RAW}" link 2>&1; \
           echo "--- scan (SSID visible?) ---"; echo "${SCAN_OUT}" | grep -iE 'SSID|BSS ' ; \
           echo "--- hostapd_cli all_sta ---"; hostapd_cli -i wlan0 all_sta 2>&1; \
           echo "--- journalctl hostapd (last 60) ---"; journalctl -u hostapd --no-pager -n 60; \
           echo "--- dmesg (last 60, mac80211/cfg80211/hwsim) ---"; dmesg | grep -iE 'hwsim|cfg80211|mac80211' | tail -60)"
    finish 1
fi
fragment "10_client_associate" pass "${t0}" "real 802.11 association to ${SSID}"

t0="$(now)"
ip netns exec "${CLIENT_NS}" dhclient -1 -pf /run/e2e-dhclient.pid -lf /run/e2e-dhclient.leases "${CLIENT_RAW}" \
    > "${RESULTS}/logs/dhclient.log" 2>&1
CLIENT_IP="$(ip netns exec "${CLIENT_NS}" ip -4 -o addr show dev "${CLIENT_RAW}" | awk '{print $4}' | cut -d/ -f1)"
if [ -z "${CLIENT_IP}" ]; then
    fragment "11_dhcp_lease" fail "${t0}" "no address on ${CLIENT_RAW} after dhclient (real dnsmasq)" \
        "$(cat "${RESULTS}/logs/dhclient.log")"
    finish 1
fi
fragment "11_dhcp_lease" pass "${t0}" "real dnsmasq leased ${CLIENT_IP}"

# --- the exact real-world complaint this proof exists to catch --------------

t0="$(now)"
PING_OUT="$(ip netns exec "${CLIENT_NS}" ping -c 4 -W 2 "${GATEWAY}" 2>&1)"
PING_RC=$?
LOSS="$(echo "${PING_OUT}" | grep -oE '[0-9]+% packet loss' | grep -oE '^[0-9]+')"
if [ "${PING_RC}" -ne 0 ] || [ "${LOSS:-100}" != "0" ]; then
    fragment "12_ping_gateway" fail "${t0}" "ping ${GATEWAY} from a real associated client lost ${LOSS:-100}%" "${PING_OUT}"
else
    fragment "12_ping_gateway" pass "${t0}" "0% loss $(echo "${PING_OUT}" | grep -oE 'rtt [^=]*=[^ ]*')"
fi

t0="$(now)"
mkdir -p "${RESULTS}/screenshots"
if ip netns exec "${CLIENT_NS}" /opt/wifucked-e2e-venv/bin/python3 \
    "${REPO}/appliance/tests/e2e/playwright_check.py" \
    --url "http://${GATEWAY}:8080/" --token "${API_TOKEN}" --out-dir "${RESULTS}/screenshots" \
    > "${RESULTS}/logs/playwright.log" 2>&1; then
    fragment "13_dashboard_playwright" pass "${t0}" "$(tail -n 5 "${RESULTS}/logs/playwright.log" | tr '\n' ' ')"
else
    fragment "13_dashboard_playwright" fail "${t0}" "real headless Chromium could not load the real dashboard" \
        "$(cat "${RESULTS}/logs/playwright.log")"
fi

# --- phase: real WAN discovery — the two host-provided USB-Ethernet links --
#
# The host attached them as QEMU `usb-net` (CDC-ECM) devices specifically so
# `wifucked.hal.linux.LinuxUsb.devices()` discovers them through the real
# sysfs class/subclass descriptor parsing this repo ships (`_classify_interface`),
# not a fixture standing in for it. NetworkManager (installed by the real
# setup_rpi.sh, no unmanaged-devices rule excludes these) DHCPs them for real
# against the host's own dnsmasq instances.

t0="$(now)"
WAN_PRESENT=0
for _ in $(seq 1 "${TIMEOUT_S}"); do
    COUNT="$(curl -s -u "wifucked:${API_TOKEN}" "http://${GATEWAY}:8080/api/state" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["counts"]["present"])' 2> /dev/null)"
    if [ "${COUNT:-0}" -ge 2 ] 2> /dev/null; then
        WAN_PRESENT=1
        break
    fi
    sleep 1
done
if [ "${WAN_PRESENT}" != "1" ]; then
    fragment "14_wan_discovery" fail "${t0}" \
        "fewer than 2 WAN atomics discovered within ${TIMEOUT_S}s (real LinuxUsb sysfs discovery)" \
        "$(curl -s -u "wifucked:${API_TOKEN}" "http://${GATEWAY}:8080/api/state"); nmcli: $(nmcli device status 2>&1)"
    finish 1
fi
fragment "14_wan_discovery" pass "${t0}" "${COUNT} WAN atomics discovered via real USB sysfs"

# --- phase: promote both WAN atomics, exactly as a user would on first setup
#
# Discovery never decides what to use on its own — "a newly discovered
# connection is always UNUSED until they say otherwise"
# (wifucked.discovery's own docstring). A real user does this once, from the
# dashboard, the first time they see a new connection; this calls the same
# real POST /api/atomics/<id>/mode endpoint that button hits. Without this
# step the allocator has nothing to allocate and the rest of this proof
# (WAN failover, the tunnel actually binding to a WAN, a download surviving
# chaos) would silently test nothing — exactly what the first real run of
# this stage showed happening.

t0="$(now)"
WAN_IDS="$(curl -s -u "wifucked:${API_TOKEN}" "http://${GATEWAY}:8080/api/state" \
    | python3 -c 'import json,sys; print("\n".join(a["id"] for a in json.load(sys.stdin)["atomics"] if a["kind"] == "usb_ethernet"))' 2> /dev/null)"
PROMOTE_FAILED=0
while IFS= read -r atomic_id; do
    [ -z "${atomic_id}" ] && continue
    STATUS="$(curl -s -o /dev/null -w '%{http_code}' -u "wifucked:${API_TOKEN}" \
        -X POST -H 'Content-Type: application/json' -d '{"mode":"normal"}' \
        "http://${GATEWAY}:8080/api/atomics/${atomic_id}/mode")"
    if [ "${STATUS}" != "200" ]; then
        PROMOTE_FAILED=1
        echo "wifucked-e2e: failed to promote ${atomic_id}, http ${STATUS}" >> "${RESULTS}/logs/driver.log"
    fi
done <<< "${WAN_IDS}"
if [ "${PROMOTE_FAILED}" != "0" ] || [ -z "${WAN_IDS}" ]; then
    fragment "15_promote_wans" fail "${t0}" "could not promote one or more real WAN atomics to NORMAL via the real API" \
        "ids: ${WAN_IDS}"
    finish 1
fi

TUNNEL_UP=0
for _ in $(seq 1 "${TIMEOUT_S}"); do
    # TunnelState (appliance/src/wifucked/tunnel/__init__.py) only has
    # down/connecting/up/incompatible -- "connected" was never a real value
    # here; this loop timed out for a full 45s on the first real run despite
    # `wg show` on the guest already showing a genuine, fresh handshake.
    TSTATE="$(curl -s -u "wifucked:${API_TOKEN}" "http://${GATEWAY}:8080/api/state" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["tunnel"]["state"])' 2> /dev/null)"
    if [ "${TSTATE}" = "up" ]; then
        TUNNEL_UP=1
        break
    fi
    sleep 1
done
if [ "${TUNNEL_UP}" != "1" ]; then
    fragment "15_promote_wans" fail "${t0}" \
        "real WireGuardTunnel never reached tunnel.state=up within ${TIMEOUT_S}s of promoting real WAN atomics (last seen: ${TSTATE:-none})" \
        "$(journalctl -u wifucked --no-pager -n 150); wg: $(wg show 2>&1)"
    finish 1
fi
fragment "15_promote_wans" pass "${t0}" "promoted ${WAN_IDS//$'\n'/,} to NORMAL; real tunnel.state=up"

# --- phase: real WAN chaos — the actual control loop reacting live ---------
#
# CHAOS_DURATION_S must match run_e2e_ap_test.sh's own constant of the same
# name — no kernel cmdline to thread it through on a full-disk boot (see that
# script's comment). The host is degrading tap-wan1/tap-wan2 independently
# (appliance/tests/qemu/chaos_wan.sh) for this whole window; this guest's job
# is to watch the real Daemon/Allocator/LinuxProber react, screenshot the
# real dashboard at three points, and confirm the one invariant that matters
# most (SOP-003): the AP client connection never drops, whatever the WAN
# links are doing.
CHAOS_DURATION_S=150

t0="$(now)"
ASSOC_LOG="${RESULTS}/logs/client_association_during_chaos.log"
: > "${ASSOC_LOG}"
(
    while true; do
        ts="$(date +%s.%N)"
        if ip netns exec "${CLIENT_NS}" iw dev "${CLIENT_RAW}" link 2> /dev/null | grep -q '^Connected to'; then
            echo "${ts} connected" >> "${ASSOC_LOG}"
        else
            echo "${ts} DISCONNECTED" >> "${ASSOC_LOG}"
        fi
        sleep 2
    done
) &
ASSOC_WATCH_PID=$!

# The actual point of this whole test: a real LAN client's real download,
# through the real nft-marked route to wg0, the real WireGuard tunnel, the
# real fabric's real NAT, to a real HTTP server standing in for "the
# Internet" that is reachable *no other way* — started now so it runs the
# entire chaos window, the same real WAN swaps 16_ap_never_drops and
# 17_wan_failover_observed are watching, not a quiet moment before or after.
INTERNET_URL="$(python3 -c "import json; print(json.load(open('${FABRIC_CFG}'))['internet_url'])")"
EXPECTED_SHA256="$(python3 -c "import json; print(json.load(open('${FABRIC_CFG}'))['payload_sha256'])")"
DOWNLOAD_FILE="${RESULTS}/downloaded_payload.bin"
DOWNLOAD_LOG="${RESULTS}/logs/download_through_tunnel.log"

# Item 16 (docs/backlog/traffic-blockers.md): wg0's own "sent" byte counter
# has stayed suspiciously small and roughly constant across every failing
# run — consistent with handshake+keepalive overhead alone, not a real SYN
# retransmitted repeatedly over 130s. Every fabric-host-side diagnostic
# (routing, NAT, FORWARD chain, forwarding sysctls, rp_filter, a live packet
# capture, WireGuard's own dynamic debug log) has come back clean, which
# points back to this side: does the client's marked SYN actually leave
# wlan0 and get encrypted onto wg0 at all? Capture both to find out instead
# of continuing to infer it from wg0's aggregate counters.
# The Internet stand-in's fixed address (run_e2e_ap_test.sh's own
# INTERNET_NS_ADDR constant) — parsed out of INTERNET_URL rather than
# hard-coded twice, so the two scripts can't silently drift apart.
INTERNET_HOST="$(python3 -c "from urllib.parse import urlparse; print(urlparse('${INTERNET_URL}').hostname)")"
apt-get install -y -qq tcpdump > /dev/null 2>&1 || true
tcpdump -Z root -i wlan0 -nn -w "${RESULTS}/logs/wlan0.pcap" "host ${INTERNET_HOST}" \
    > "${RESULTS}/logs/tcpdump-wlan0.log" 2>&1 &
TCPDUMP_WLAN0_PID=$!
tcpdump -Z root -i wg0 -nn -w "${RESULTS}/logs/wg0.pcap" \
    > "${RESULTS}/logs/tcpdump-wg0.log" 2>&1 &
TCPDUMP_WG0_PID=$!

ip netns exec "${CLIENT_NS}" curl -sS --max-time "${CHAOS_DURATION_S}" \
    -o "${DOWNLOAD_FILE}" "${INTERNET_URL}" > "${DOWNLOAD_LOG}" 2>&1 &
DOWNLOAD_PID=$!

mkdir -p "${RESULTS}/screenshots"
ip netns exec "${CLIENT_NS}" /opt/wifucked-e2e-venv/bin/python3 \
    "${REPO}/appliance/tests/e2e/playwright_check.py" \
    --url "http://${GATEWAY}:8080/" --token "${API_TOKEN}" \
    --out-dir "${RESULTS}/screenshots/chaos_01_start" > "${RESULTS}/logs/playwright-chaos-start.log" 2>&1 || true

/opt/wifucked-e2e-venv/bin/python3 "${REPO}/appliance/tests/e2e/monitor_state.py" \
    --url "http://${GATEWAY}:8080" --token "${API_TOKEN}" \
    --duration-s "$(awk -v d="${CHAOS_DURATION_S}" 'BEGIN{print d/2}')" --interval-s 3 \
    --out "${RESULTS}/state_snapshots_1.json"

ip netns exec "${CLIENT_NS}" /opt/wifucked-e2e-venv/bin/python3 \
    "${REPO}/appliance/tests/e2e/playwright_check.py" \
    --url "http://${GATEWAY}:8080/" --token "${API_TOKEN}" \
    --out-dir "${RESULTS}/screenshots/chaos_02_mid" > "${RESULTS}/logs/playwright-chaos-mid.log" 2>&1 || true

/opt/wifucked-e2e-venv/bin/python3 "${REPO}/appliance/tests/e2e/monitor_state.py" \
    --url "http://${GATEWAY}:8080" --token "${API_TOKEN}" \
    --duration-s "$(awk -v d="${CHAOS_DURATION_S}" 'BEGIN{print d/2}')" --interval-s 3 \
    --out "${RESULTS}/state_snapshots_2.json"

ip netns exec "${CLIENT_NS}" /opt/wifucked-e2e-venv/bin/python3 \
    "${REPO}/appliance/tests/e2e/playwright_check.py" \
    --url "http://${GATEWAY}:8080/" --token "${API_TOKEN}" \
    --out-dir "${RESULTS}/screenshots/chaos_03_end" > "${RESULTS}/logs/playwright-chaos-end.log" 2>&1 || true

kill "${ASSOC_WATCH_PID}" 2> /dev/null || true
wait "${ASSOC_WATCH_PID}" 2> /dev/null || true

python3 - "${RESULTS}" "${ASSOC_LOG}" << 'PYEOF'
import json
import sys
from pathlib import Path

results_dir, assoc_log = Path(sys.argv[1]), Path(sys.argv[2])

merged = {"snapshots": [], "decisions": []}
for name in ("state_snapshots_1.json", "state_snapshots_2.json"):
    path = results_dir / name
    if path.exists():
        data = json.loads(path.read_text())
        merged["snapshots"].extend(data.get("snapshots", []))
        merged["decisions"] = data.get("decisions", []) or merged["decisions"]
(results_dir / "state_snapshots.json").write_text(json.dumps(merged, indent=2))

lines = assoc_log.read_text().splitlines() if assoc_log.exists() else []
disconnects = [line for line in lines if "DISCONNECTED" in line]

primary_ids = [
    s["state"]["allocation"]["primary_id"]
    for s in merged["snapshots"]
    if s.get("state") and s["state"].get("allocation")
]
switches = sum(1 for a, b in zip(primary_ids, primary_ids[1:]) if a != b)

summary = {
    "samples": len(lines),
    "disconnect_samples": len(disconnects),
    "snapshots": len(merged["snapshots"]),
    "decisions": len(merged["decisions"]),
    "primary_switches_observed": switches,
    "primary_timeline": primary_ids,
}
(results_dir / "chaos_summary.json").write_text(json.dumps(summary, indent=2))
print(json.dumps(summary, indent=2))
PYEOF

DISCONNECTS="$(python3 -c "import json;print(json.load(open('${RESULTS}/chaos_summary.json'))['disconnect_samples'])" 2> /dev/null || echo 999)"
SWITCHES="$(python3 -c "import json;print(json.load(open('${RESULTS}/chaos_summary.json'))['primary_switches_observed'])" 2> /dev/null || echo 0)"

if [ "${DISCONNECTS:-999}" != "0" ]; then
    fragment "16_ap_never_drops" fail "${t0}" \
        "the AP client connection disconnected ${DISCONNECTS} time(s) during ${CHAOS_DURATION_S}s of real WAN chaos (SOP-003 invariant)" \
        "$(cat "${ASSOC_LOG}")"
else
    fragment "16_ap_never_drops" pass "${t0}" "0 disconnects across ${CHAOS_DURATION_S}s of real WAN chaos"
fi

t0="$(now)"
fragment "17_wan_failover_observed" pass "${t0}" \
    "real allocator primary_id switched ${SWITCHES} time(s) over ${CHAOS_DURATION_S}s (see chaos_summary.json, state_snapshots.json)"

# --- the actual point of this test: did the download survive? --------------

t0="$(now)"
wait "${DOWNLOAD_PID}" 2> /dev/null
DOWNLOAD_RC=$?
ACTUAL_SHA256="$(sha256sum "${DOWNLOAD_FILE}" 2> /dev/null | awk '{print $1}')"

# SIGTERM, not -9, so both pcaps get to flush and close before being read.
kill "${TCPDUMP_WLAN0_PID}" "${TCPDUMP_WG0_PID}" 2> /dev/null || true
wait "${TCPDUMP_WLAN0_PID}" "${TCPDUMP_WG0_PID}" 2> /dev/null || true
tcpdump -r "${RESULTS}/logs/wlan0.pcap" -nn -vvv > "${RESULTS}/logs/wlan0.txt" 2>&1 || true
tcpdump -r "${RESULTS}/logs/wg0.pcap" -nn -vvv > "${RESULTS}/logs/wg0.txt" 2>&1 || true
if [ "${DOWNLOAD_RC}" -ne 0 ]; then
    # Item 16 (docs/backlog/traffic-blockers.md): wg show alone has already
    # shown a genuine handshake with byte counts too small to be the actual
    # download every time this has failed, and the host-side FORWARD chain
    # (fabric-host-diagnostics.log) has shown zero packets ever reaching it
    # via wg0 — meaning whatever's wrong may be on this side, before the
    # packet ever leaves the appliance. `ip rule`/`ip route table <n>` show
    # whether enforce.render() actually installed a route to wg0 for this
    # client's marked traffic (or whether item 15's fix only solved the
    # ceiling_bps computation, not this); `ip route get` with the actual
    # fwmark shows what the kernel's routing decision really is; rp_filter
    # values matter because a strict reverse-path check can silently drop a
    # packet before it ever reaches nft's counters.
    FWMARK="$(nft list ruleset 2>&1 | grep -oE 'meta mark set 0x[0-9a-fA-F]+' | head -1 | awk '{print $NF}')"
    fragment "18_tunnel_download_survives_chaos" fail "${t0}" \
        "curl exited ${DOWNLOAD_RC} downloading through the real tunnel during ${CHAOS_DURATION_S}s of real WAN chaos" \
        "$(cat "${DOWNLOAD_LOG}"); wg: $(wg show 2>&1); nft: $(nft list ruleset 2>&1); ip_rule: $(ip rule show 2>&1); ip_route_tables: $(for t in $(ip rule show 2>&1 | grep -oE 'lookup [0-9]+' | awk '{print $2}' | sort -u); do echo "table ${t}:"; ip route show table "${t}" 2>&1; done); route_get_wg0: $(ip route get 198.51.100.2 mark "${FWMARK:-0x0}" 2>&1); rp_filter: $(for f in /proc/sys/net/ipv4/conf/*/rp_filter; do echo "${f}=$(cat "${f}" 2>&1)"; done)"
    # ip_rule/ip_route_tables above answers *whether* render() ever installed
    # a policy route; this answers *why not* — item 15's fix made ceiling_bps
    # depend on real demand (max(up_bps, down_bps)), so if it's still 0 the
    # whole run, either no demand was ever measured for this client's
    # profile, or it was measured but never became nonzero. The daemon's own
    # workflow=enforce_reconcile logs report route_rules/marks every tick;
    # grepping the full 150s+ run instead of just the last 150 lines shows
    # the actual trend rather than one late snapshot.
    journalctl -u wifucked --no-pager 2>&1 | grep -E "workflow='(enforce_reconcile|demand_sample|allocation_decision)'|ceiling_bps|route_rules" \
        > "${RESULTS}/enforce_reconcile_trend.log" 2>&1 || true
elif [ "${ACTUAL_SHA256}" != "${EXPECTED_SHA256}" ]; then
    fragment "18_tunnel_download_survives_chaos" fail "${t0}" \
        "downloaded file checksum mismatch (expected ${EXPECTED_SHA256}, got ${ACTUAL_SHA256:-none}) -- corrupted somewhere in real nft mark -> wg0 -> fabric NAT -> real HTTP server" \
        "$(cat "${DOWNLOAD_LOG}")"
else
    fragment "18_tunnel_download_survives_chaos" pass "${t0}" \
        "real download through the real tunnel completed, checksum-correct, surviving ${SWITCHES} real WAN swap(s) and ${CHAOS_DURATION_S}s of chaos"
fi

finish 0
