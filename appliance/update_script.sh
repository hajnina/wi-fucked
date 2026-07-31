#!/bin/bash
#
# Package installer. Executed by the OTA client (and by the CI inject stage)
# from inside an extracted .wtf package.
#
# The rule that governs this script: a control-plane-only update must not
# restart hostapd or dnsmasq. The AP does not drop for a daemon update (ADR-011,
# SOP-008). Anything that would bounce the AP needs an ADR, not a line here.
#
set -euo pipefail

log() { echo "[$(date -u +%FT%TZ)] wifucked-update: $1"; }

if [[ "${EUID}" -ne 0 ]]; then
    log "must run as root"
    exit 1
fi

BASE_DIR=/opt/wifucked
VERSIONS_DIR="${BASE_DIR}/versions"
CURRENT="${BASE_DIR}/current"
HEALTHY="${BASE_DIR}/healthy"

NEW_VERSION="$(cat NEWVERSION)"
TARGET="${VERSIONS_DIR}/${NEW_VERSION}"

log "starting update to ${NEW_VERSION}"

# --- 1. dependencies --------------------------------------------------------

if [[ -f apt_deps.txt ]]; then
    log "installing apt dependencies"
    apt-get update
    # shellcheck disable=SC2046 # deliberate word splitting: one package per line
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        $(grep -vE '^\s*(#|$)' apt_deps.txt | tr '\n' ' ')
fi

if [[ -s py_deps.txt ]]; then
    log "installing python dependencies"
    python3 -m pip install --break-system-packages -r py_deps.txt || \
        log "WARNING pip install failed; continuing with what is present"
fi

# --- 2. unpack into a new slot ----------------------------------------------
#
# The running version is never modified in place. An update that fails leaves
# the previous slot untouched and the symlink still pointing at it.

log "unpacking application into ${TARGET}"
rm -rf "${TARGET}"
mkdir -p "${TARGET}"
unzip -qo APP.zip -d "${TARGET}"

# --- 3. system files --------------------------------------------------------

if [[ -d stage-custom ]]; then
    log "updating system configuration"
    cp -r stage-custom/etc/. /etc/
    cp -r stage-custom/opt/. /opt/
    chmod 755 /opt/wifucked/*.sh
    systemctl daemon-reload
fi

# --- 4. atomic swap ---------------------------------------------------------

PREVIOUS=""
[[ -L "${CURRENT}" ]] && PREVIOUS="$(readlink -f "${CURRENT}")"

log "switching current -> ${TARGET}"
ln -sfn "${TARGET}" "${CURRENT}"
echo "${NEW_VERSION}" > "${BASE_DIR}/VERSION"

# Clear the health sentinel: the watchdog sets it again once the new version has
# proved itself, and its absence is what triggers rollback.
rm -f "${HEALTHY}"
[[ -n "${PREVIOUS}" ]] && echo "${PREVIOUS}" > "${BASE_DIR}/previous"

# --- 5. restart the control plane only --------------------------------------
#
# hostapd and dnsmasq are deliberately untouched. Clients keep their association
# and their leases; the user experiences nothing.

log "restarting control plane (AP untouched)"
systemctl restart wifucked.service || log "WARNING wifucked.service restart failed"

log "update to ${NEW_VERSION} applied"
