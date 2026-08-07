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
mapfile -t WLAN_IFACES < <(iw dev 2> /dev/null | awk '/Interface/ {print $2}' | sort)
if [ "${#WLAN_IFACES[@]}" -ne 2 ]; then
    fragment "01_hwsim_module" fail "${t0}" \
        "expected exactly 2 wireless interfaces from mac80211_hwsim radios=2, found ${#WLAN_IFACES[@]}" \
        "iw dev: ${WLAN_IFACES[*]:-none}"
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
# Belt-and-suspenders against a real race this harness introduces that a real
# device does not have: on real hardware, setup_rpi.sh's NetworkManager
# unmanaged-devices config is baked into the image before first boot, so NM
# never sees wlan0 unmanaged. Here, setup_rpi.sh runs live during this boot,
# so there is a real window where NM could grab wlan0 first. Documented in
# README.md's "known deviations from a real device."
nmcli device set wlan0 managed no 2> /dev/null || true

# Move the second radio out of the root netns *before* NetworkManager or
# hostapd can see it at all — it plays the "phone" in this proof.
ip netns add "${CLIENT_NS}"
ip link set "${CLIENT_RAW}" netns "${CLIENT_NS}"
ip netns exec "${CLIENT_NS}" ip link set lo up
ip netns exec "${CLIENT_NS}" ip link set "${CLIENT_RAW}" up
fragment "02_iface_split" pass "${t0}" "ap=wlan0 (was ${AP_RAW}), client=${CLIENT_RAW} in netns ${CLIENT_NS}"

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

t0="$(now)"
python3 -m venv /opt/wifucked-e2e-venv
/opt/wifucked-e2e-venv/bin/pip install --quiet -r "${REPO}/appliance/tests/e2e/requirements.txt"
/opt/wifucked-e2e-venv/bin/playwright install --with-deps chromium > "${RESULTS}/logs/playwright-install.log" 2>&1
fragment "04_playwright_install" pass "${t0}" "isolated venv, chromium installed"

# --- phase: start the real services in the real order -----------------------

t0="$(now)"
systemctl daemon-reload
systemctl restart NetworkManager
systemctl start wifucked-bootcount.service
if ! systemctl start wifucked-firstboot.service; then
    fragment "05_firstboot" fail "${t0}" "wifucked-firstboot.service (real firstboot.sh) failed" \
        "$(journalctl -u wifucked-firstboot --no-pager -n 100)"
    finish 1
fi
if [ ! -f /etc/hostapd/hostapd.conf ]; then
    fragment "05_firstboot" fail "${t0}" "firstboot.sh ran but /etc/hostapd/hostapd.conf was not written" ""
    finish 1
fi
SSID="$(grep -m1 '^ssid=' /etc/hostapd/hostapd.conf | cut -d= -f2-)"
fragment "05_firstboot" pass "${t0}" "real firstboot.sh generated identity, ssid=${SSID}"

t0="$(now)"
systemctl restart systemd-networkd
systemctl restart hostapd
systemctl restart dnsmasq
systemctl start wifucked.service

HOSTAPD_UP=0
for _ in $(seq 1 "${TIMEOUT_S}"); do
    if hostapd_cli -i wlan0 status 2> /dev/null | grep -q '^state=ENABLED'; then
        HOSTAPD_UP=1
        break
    fi
    sleep 1
done
if [ "${HOSTAPD_UP}" != "1" ]; then
    fragment "06_hostapd_up" fail "${t0}" "real hostapd (systemd unit) did not reach state=ENABLED within ${TIMEOUT_S}s" \
        "$(journalctl -u hostapd --no-pager -n 100)"
    finish 1
fi
fragment "06_hostapd_up" pass "${t0}" "real hostapd.service state=ENABLED"

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
    fragment "07_gateway_address" fail "${t0}" \
        "no ${GATEWAY} address on wlan0 within ${TIMEOUT_S}s (real systemd-networkd applying the real generated .network unit)" \
        "$(ip -4 addr show dev wlan0; echo ---; networkctl status wlan0 2>&1; echo ---; journalctl -u systemd-networkd --no-pager -n 100)"
    finish 1
fi
fragment "07_gateway_address" pass "${t0}" "wlan0 has ${GATEWAY} via real systemd-networkd"

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
    fragment "08_dashboard_up" fail "${t0}" \
        "real wifucked.service dashboard did not open ${GATEWAY}:8080 (real HAL, no MOCK_HW) within ${TIMEOUT_S}s" \
        "$(journalctl -u wifucked --no-pager -n 150)"
    finish 1
fi
fragment "08_dashboard_up" pass "${t0}" "real dashboard listening on ${GATEWAY}:8080"

# --- phase: real client, from inside the guest ------------------------------

t0="$(now)"
ip netns exec "${CLIENT_NS}" iw dev "${CLIENT_RAW}" connect "${SSID}"
ASSOCIATED=0
for _ in $(seq 1 "${TIMEOUT_S}"); do
    if ip netns exec "${CLIENT_NS}" iw dev "${CLIENT_RAW}" link | grep -q '^Connected to'; then
        ASSOCIATED=1
        break
    fi
    sleep 1
done
if [ "${ASSOCIATED}" != "1" ]; then
    fragment "09_client_associate" fail "${t0}" "client did not associate to ${SSID} within ${TIMEOUT_S}s" \
        "$(ip netns exec "${CLIENT_NS}" iw dev "${CLIENT_RAW}" link)"
    finish 1
fi
fragment "09_client_associate" pass "${t0}" "real 802.11 association to ${SSID}"

t0="$(now)"
ip netns exec "${CLIENT_NS}" dhclient -1 -pf /run/e2e-dhclient.pid -lf /run/e2e-dhclient.leases "${CLIENT_RAW}" \
    > "${RESULTS}/logs/dhclient.log" 2>&1
CLIENT_IP="$(ip netns exec "${CLIENT_NS}" ip -4 -o addr show dev "${CLIENT_RAW}" | awk '{print $4}' | cut -d/ -f1)"
if [ -z "${CLIENT_IP}" ]; then
    fragment "10_dhcp_lease" fail "${t0}" "no address on ${CLIENT_RAW} after dhclient (real dnsmasq)" \
        "$(cat "${RESULTS}/logs/dhclient.log")"
    finish 1
fi
fragment "10_dhcp_lease" pass "${t0}" "real dnsmasq leased ${CLIENT_IP}"

# --- the exact real-world complaint this proof exists to catch --------------

t0="$(now)"
PING_OUT="$(ip netns exec "${CLIENT_NS}" ping -c 4 -W 2 "${GATEWAY}" 2>&1)"
PING_RC=$?
LOSS="$(echo "${PING_OUT}" | grep -oE '[0-9]+% packet loss' | grep -oE '^[0-9]+')"
if [ "${PING_RC}" -ne 0 ] || [ "${LOSS:-100}" != "0" ]; then
    fragment "11_ping_gateway" fail "${t0}" "ping ${GATEWAY} from a real associated client lost ${LOSS:-100}%" "${PING_OUT}"
else
    fragment "11_ping_gateway" pass "${t0}" "0% loss $(echo "${PING_OUT}" | grep -oE 'rtt [^=]*=[^ ]*')"
fi

t0="$(now)"
mkdir -p "${RESULTS}/screenshots"
if ip netns exec "${CLIENT_NS}" /opt/wifucked-e2e-venv/bin/python3 \
    "${REPO}/appliance/tests/e2e/playwright_check.py" \
    --url "http://${GATEWAY}:8080/" --token "${API_TOKEN}" --out-dir "${RESULTS}/screenshots" \
    > "${RESULTS}/logs/playwright.log" 2>&1; then
    fragment "12_dashboard_playwright" pass "${t0}" "$(tail -n 5 "${RESULTS}/logs/playwright.log" | tr '\n' ' ')"
else
    fragment "12_dashboard_playwright" fail "${t0}" "real headless Chromium could not load the real dashboard" \
        "$(cat "${RESULTS}/logs/playwright.log")"
fi

finish 0
