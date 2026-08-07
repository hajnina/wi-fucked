#!/bin/bash
#
# The AP + dashboard E2E proof.
#
# Boots the real hostapd, the real dnsmasq, and the real wifucked dashboard
# (Daemon + Flask app, MOCK_HW=1 for the HAL only — SOP-003's standard,
# required seam, not a shortcut specific to this test), wired to a real
# kernel 802.11 stack via mac80211_hwsim: two virtual radios that exchange
# genuine 802.11 management/data frames over a simulated medium (real
# hostapd/wpa_supplicant/nl80211 code paths, no RF, no Raspberry Pi).
#
# A second network namespace stands in for a phone or laptop: it associates
# to the AP for real, gets a real DHCP lease from the real dnsmasq, pings the
# gateway, and drives a real headless Chromium (Playwright) at the real
# dashboard URL over that path — reproducing, end to end, the exact complaint
# this test exists to catch: "I get an IP but I can't ping the gateway and I
# can't open the setup interface."
#
# See README.md in this directory for what this does and does not prove, and
# docs/active-tests.md's "AP bring-up" entry for the real-hardware gap it
# does not close (no real brcmfmac firmware, no real RF).
#
# Requires root (network namespaces, mac80211_hwsim, binding hostapd to a
# real interface) — this is why it is not part of run_all_tests.sh (SOP-003)
# and instead runs as its own CI job, same posture as appliance/tests/qemu/.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../../.." && pwd)"

NS_AP="wifucked-e2e-ap"
NS_CLIENT="wifucked-e2e-client"
GATEWAY="10.44.0.1"
CHANNEL=6
TOKEN="e2e-test-token-$$"
TIMEOUT_S=20

WORKDIR="$(mktemp -d /tmp/wifucked-e2e.XXXXXX)"
RESULTS_DIR="${1:-${REPO_ROOT}/e2e-artifacts}"
FRAGMENTS_DIR="${WORKDIR}/fragments"
mkdir -p "${RESULTS_DIR}" "${FRAGMENTS_DIR}"

PYTHON="${WIFUCKED_E2E_PYTHON:-python3}"
export PYTHONPATH="${REPO_ROOT}/appliance/src"

HOSTAPD_PID=""
DNSMASQ_PID=""
DAEMON_PID=""
MODULE_LOADED_HERE=0
OVERALL_FAIL=0

log() { printf '[e2e] %s\n' "$1"; }

fragment() {
    # fragment <name> <pass|fail> <duration_s> <detail> [error]
    local name="$1" outcome="$2" duration="$3" detail="$4" error="${5:-}"
    "${PYTHON}" "${HERE}/write_fragment.py" \
        --fragments-dir "${FRAGMENTS_DIR}" --name "${name}" "--${outcome}" \
        --duration-s "${duration}" --detail "${detail}" --error "${error}"
    if [ "${outcome}" = "fail" ]; then
        OVERALL_FAIL=1
        log "FAIL: ${name} — ${detail} ${error}"
    else
        log "PASS: ${name} — ${detail}"
    fi
}

# shellcheck disable=SC2329  # invoked indirectly via `trap ... EXIT` below
cleanup() {
    log "cleaning up"
    for pid in "${DAEMON_PID}" "${DNSMASQ_PID}" "${HOSTAPD_PID}"; do
        [ -n "${pid}" ] && kill "${pid}" > /dev/null 2>&1 || true
    done
    sleep 1
    for pid in "${DAEMON_PID}" "${DNSMASQ_PID}" "${HOSTAPD_PID}"; do
        [ -n "${pid}" ] && kill -9 "${pid}" > /dev/null 2>&1 || true
    done
    ip netns del "${NS_CLIENT}" > /dev/null 2>&1 || true
    ip netns del "${NS_AP}" > /dev/null 2>&1 || true
    if [ "${MODULE_LOADED_HERE}" = "1" ]; then
        rmmod mac80211_hwsim > /dev/null 2>&1 || true
    fi
    rm -rf "${WORKDIR}"
}
trap cleanup EXIT

if [ "$(id -u)" -ne 0 ]; then
    echo "run_e2e_ap_test.sh requires root (network namespaces, mac80211_hwsim, hostapd)" >&2
    exit 1
fi

command -v hostapd > /dev/null || { echo "FATAL: hostapd not installed" >&2; exit 1; }
command -v dnsmasq > /dev/null || { echo "FATAL: dnsmasq not installed" >&2; exit 1; }
command -v iw > /dev/null || { echo "FATAL: iw not installed" >&2; exit 1; }

# --- bring up two real 802.11 radios ----------------------------------------

t0=$(date +%s.%N)
BEFORE_IFACES="$(iw dev | awk '/Interface/ {print $2}' | sort)"
if ! grep -q '^mac80211_hwsim ' /proc/modules 2> /dev/null; then
    modprobe mac80211_hwsim radios=2
    MODULE_LOADED_HERE=1
else
    log "mac80211_hwsim already loaded; reusing (radios may be >2)"
fi
sleep 1
AFTER_IFACES="$(iw dev | awk '/Interface/ {print $2}' | sort)"
mapfile -t NEW_IFACES < <(comm -13 <(echo "${BEFORE_IFACES}") <(echo "${AFTER_IFACES}"))
t1=$(date +%s.%N)

if [ "${#NEW_IFACES[@]}" -lt 2 ]; then
    fragment "01_hwsim_radios" fail "$(echo "${t1} - ${t0}" | bc)" \
        "expected 2 new interfaces from mac80211_hwsim, got ${#NEW_IFACES[@]}" \
        "iw dev after modprobe: ${AFTER_IFACES}"
    exit 1
fi
AP_IFACE="${NEW_IFACES[0]}"
CLIENT_IFACE="${NEW_IFACES[1]}"
fragment "01_hwsim_radios" pass "$(echo "${t1} - ${t0}" | bc)" \
    "ap=${AP_IFACE} client=${CLIENT_IFACE}"

# --- split into two network namespaces --------------------------------------

t0=$(date +%s.%N)
ip netns add "${NS_AP}"
ip netns add "${NS_CLIENT}"
AP_PHY="$(iw dev "${AP_IFACE}" info | awk '/wiphy/ {print "phy"$2}')"
CLIENT_PHY="$(iw dev "${CLIENT_IFACE}" info | awk '/wiphy/ {print "phy"$2}')"
iw phy "${AP_PHY}" set netns name "${NS_AP}"
iw phy "${CLIENT_PHY}" set netns name "${NS_CLIENT}"
ip netns exec "${NS_AP}" ip link set lo up
ip netns exec "${NS_CLIENT}" ip link set lo up
ip netns exec "${NS_AP}" ip link set "${AP_IFACE}" up
ip netns exec "${NS_CLIENT}" ip link set "${CLIENT_IFACE}" up
t1=$(date +%s.%N)
fragment "02_netns_split" pass "$(echo "${t1} - ${t0}" | bc)" \
    "ap netns=${NS_AP} (${AP_PHY}/${AP_IFACE}), client netns=${NS_CLIENT} (${CLIENT_PHY}/${CLIENT_IFACE})"

# --- render the real config (wifucked.lan, same calls as firstboot.sh) -----

t0=$(date +%s.%N)
CONF_DIR="${WORKDIR}/conf"
if ! "${PYTHON}" "${HERE}/render_configs.py" \
    --interface "${AP_IFACE}" --channel "${CHANNEL}" --lan-mode single \
    --open-network --out-dir "${CONF_DIR}" > "${WORKDIR}/identity.json"; then
    t1=$(date +%s.%N)
    fragment "03_render_config" fail "$(echo "${t1} - ${t0}" | bc)" \
        "wifucked.lan.hostapd_config()/dnsmasq_config() failed" "see ${WORKDIR}/identity.json"
    exit 1
fi
SSID="$("${PYTHON}" -c "import json;print(json.load(open('${WORKDIR}/identity.json'))['ssid'])")"
t1=$(date +%s.%N)
fragment "03_render_config" pass "$(echo "${t1} - ${t0}" | bc)" "ssid=${SSID} (open, ADR-021 default)"

# --- start the real hostapd --------------------------------------------------

t0=$(date +%s.%N)
ip netns exec "${NS_AP}" hostapd -B -P "${WORKDIR}/hostapd.pid" \
    -f "${WORKDIR}/hostapd.log" "${CONF_DIR}/hostapd.conf"
HOSTAPD_STARTED=0
for _ in $(seq 1 "${TIMEOUT_S}"); do
    if ip netns exec "${NS_AP}" hostapd_cli -i "${AP_IFACE}" -p /var/run/hostapd status \
        2> /dev/null | grep -q "^state=ENABLED"; then
        HOSTAPD_STARTED=1
        break
    fi
    sleep 1
done
HOSTAPD_PID="$(cat "${WORKDIR}/hostapd.pid" 2> /dev/null || true)"
t1=$(date +%s.%N)
if [ "${HOSTAPD_STARTED}" != "1" ]; then
    fragment "04_hostapd_up" fail "$(echo "${t1} - ${t0}" | bc)" \
        "hostapd did not reach state=ENABLED within ${TIMEOUT_S}s" "$(cat "${WORKDIR}/hostapd.log" 2> /dev/null)"
    exit 1
fi
fragment "04_hostapd_up" pass "$(echo "${t1} - ${t0}" | bc)" "hostapd state=ENABLED, pid=${HOSTAPD_PID}"

# --- gateway address (stands in for the networkd unit firstboot.sh writes) -

ip netns exec "${NS_AP}" ip addr add "${GATEWAY}/24" dev "${AP_IFACE}"

# --- start the real dnsmasq --------------------------------------------------

t0=$(date +%s.%N)
ip netns exec "${NS_AP}" dnsmasq --conf-file="${CONF_DIR}/dnsmasq.conf" \
    --interface="${AP_IFACE}" --bind-interfaces --pid-file="${WORKDIR}/dnsmasq.pid" \
    --log-facility="${WORKDIR}/dnsmasq.log"
sleep 1
DNSMASQ_PID="$(cat "${WORKDIR}/dnsmasq.pid" 2> /dev/null || true)"
t1=$(date +%s.%N)
if [ -z "${DNSMASQ_PID}" ] || ! kill -0 "${DNSMASQ_PID}" 2> /dev/null; then
    fragment "05_dnsmasq_up" fail "$(echo "${t1} - ${t0}" | bc)" \
        "dnsmasq did not stay running" "$(cat "${WORKDIR}/dnsmasq.log" 2> /dev/null)"
    exit 1
fi
fragment "05_dnsmasq_up" pass "$(echo "${t1} - ${t0}" | bc)" "dnsmasq pid=${DNSMASQ_PID}"

# --- start the real dashboard daemon ----------------------------------------

t0=$(date +%s.%N)
ip netns exec "${NS_AP}" env MOCK_HW=1 WIFUCKED_STATE_DIR="${WORKDIR}/state" PYTHONPATH="${PYTHONPATH}" \
    "${PYTHON}" "${HERE}/e2e_daemon.py" --token "${TOKEN}" \
    > "${WORKDIR}/daemon.log" 2>&1 &
DAEMON_PID=$!
DAEMON_UP=0
for _ in $(seq 1 "${TIMEOUT_S}"); do
    if ip netns exec "${NS_AP}" bash -c "echo > /dev/tcp/${GATEWAY}/8080" 2> /dev/null; then
        DAEMON_UP=1
        break
    fi
    sleep 1
done
t1=$(date +%s.%N)
if [ "${DAEMON_UP}" != "1" ]; then
    fragment "06_daemon_up" fail "$(echo "${t1} - ${t0}" | bc)" \
        "dashboard never opened ${GATEWAY}:8080 within ${TIMEOUT_S}s" "$(tail -n 40 "${WORKDIR}/daemon.log")"
    exit 1
fi
fragment "06_daemon_up" pass "$(echo "${t1} - ${t0}" | bc)" "listening on ${GATEWAY}:8080, pid=${DAEMON_PID}"

# --- client: real association over the real 802.11 stack -------------------

t0=$(date +%s.%N)
ip netns exec "${NS_CLIENT}" iw dev "${CLIENT_IFACE}" connect "${SSID}"
ASSOCIATED=0
for _ in $(seq 1 "${TIMEOUT_S}"); do
    if ip netns exec "${NS_CLIENT}" iw dev "${CLIENT_IFACE}" link | grep -q "^Connected to"; then
        ASSOCIATED=1
        break
    fi
    sleep 1
done
t1=$(date +%s.%N)
if [ "${ASSOCIATED}" != "1" ]; then
    fragment "07_client_associate" fail "$(echo "${t1} - ${t0}" | bc)" \
        "client did not associate to ${SSID} within ${TIMEOUT_S}s" \
        "$(ip netns exec "${NS_CLIENT}" iw dev "${CLIENT_IFACE}" link)"
    exit 1
fi
fragment "07_client_associate" pass "$(echo "${t1} - ${t0}" | bc)" "associated to ${SSID}"

# --- client: real DHCP lease from the real dnsmasq --------------------------

t0=$(date +%s.%N)
DHCP_BIN=""
if command -v dhclient > /dev/null; then
    DHCP_BIN="dhclient"
    ip netns exec "${NS_CLIENT}" dhclient -1 -pf "${WORKDIR}/dhclient.pid" \
        -lf "${WORKDIR}/dhclient.leases" "${CLIENT_IFACE}" > "${WORKDIR}/dhclient.log" 2>&1
elif command -v udhcpc > /dev/null; then
    DHCP_BIN="udhcpc"
    ip netns exec "${NS_CLIENT}" udhcpc -i "${CLIENT_IFACE}" -n -q > "${WORKDIR}/dhclient.log" 2>&1
fi
CLIENT_IP="$(ip netns exec "${NS_CLIENT}" ip -4 -o addr show dev "${CLIENT_IFACE}" \
    | awk '{print $4}' | cut -d/ -f1)"
t1=$(date +%s.%N)
if [ -z "${DHCP_BIN}" ]; then
    fragment "08_dhcp_lease" fail "0" "no DHCP client installed (need dhclient or udhcpc)"
    exit 1
fi
if [ -z "${CLIENT_IP}" ]; then
    fragment "08_dhcp_lease" fail "$(echo "${t1} - ${t0}" | bc)" \
        "no address on ${CLIENT_IFACE} after ${DHCP_BIN}" "$(cat "${WORKDIR}/dhclient.log")"
    exit 1
fi
fragment "08_dhcp_lease" pass "$(echo "${t1} - ${t0}" | bc)" "${DHCP_BIN} leased ${CLIENT_IP}"

# --- client: ping the gateway (the exact real-world complaint) -------------

t0=$(date +%s.%N)
PING_OUT="$(ip netns exec "${NS_CLIENT}" ping -c 4 -W 2 "${GATEWAY}" 2>&1)"
PING_RC=$?
t1=$(date +%s.%N)
LOSS="$(echo "${PING_OUT}" | grep -oE '[0-9]+% packet loss' | grep -oE '^[0-9]+')"
if [ "${PING_RC}" -ne 0 ] || [ "${LOSS:-100}" != "0" ]; then
    fragment "09_ping_gateway" fail "$(echo "${t1} - ${t0}" | bc)" \
        "ping ${GATEWAY} from client lost ${LOSS:-100}%" "${PING_OUT}"
else
    RTT="$(echo "${PING_OUT}" | grep -oE 'rtt [^=]*= [^ ]*' || true)"
    fragment "09_ping_gateway" pass "$(echo "${t1} - ${t0}" | bc)" "0% loss, ${RTT}"
fi

# --- client: real headless Chromium at the real dashboard URL --------------

t0=$(date +%s.%N)
PW_OUT_DIR="${RESULTS_DIR}/screenshots"
mkdir -p "${PW_OUT_DIR}"
if ip netns exec "${NS_CLIENT}" "${PYTHON}" "${HERE}/playwright_check.py" \
    --url "http://${GATEWAY}:8080/" --token "${TOKEN}" --out-dir "${PW_OUT_DIR}" \
    > "${WORKDIR}/playwright.log" 2>&1; then
    t1=$(date +%s.%N)
    fragment "10_dashboard_playwright" pass "$(echo "${t1} - ${t0}" | bc)" \
        "$(tail -n 5 "${WORKDIR}/playwright.log" | tr '\n' ' ')"
else
    t1=$(date +%s.%N)
    fragment "10_dashboard_playwright" fail "$(echo "${t1} - ${t0}" | bc)" \
        "Playwright check failed" "$(cat "${WORKDIR}/playwright.log")"
fi

# --- aggregate ---------------------------------------------------------------

cp -f "${WORKDIR}"/*.log "${RESULTS_DIR}/" 2> /dev/null || true
"${PYTHON}" "${HERE}/aggregate_report.py" --fragments-dir "${FRAGMENTS_DIR}" --out-dir "${RESULTS_DIR}"
REPORT_RC=$?

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    cat "${RESULTS_DIR}/report.md" >> "${GITHUB_STEP_SUMMARY}"
fi

if [ "${OVERALL_FAIL}" != "0" ] || [ "${REPORT_RC}" != "0" ]; then
    log "RESULT: FAIL — see ${RESULTS_DIR}/report.md"
    exit 1
fi
log "RESULT: PASS — see ${RESULTS_DIR}/report.md"
exit 0
