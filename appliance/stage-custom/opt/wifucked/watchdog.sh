#!/bin/bash
#
# OTA rollback watchdog (SOP-008, ADR-008, ADR-011).
#
# update_script.sh applies a new version by swapping the `current` symlink and
# removing the `healthy` sentinel, leaving the previous slot's real path in
# `previous`. This script is the other half of that contract: it runs once, a
# grace period after boot, decides whether the running version is healthy, and
# either marks it healthy or rolls back to the previous slot.
#
# It touches ONLY the `current` symlink, its own sentinels, and wifucked.service.
# It never restarts hostapd or dnsmasq (the AP is the anchor and must survive a
# rollback unconditionally, ADR-011) and never removes qdiscs, nftables rules or
# routes (kernel state outlives the process that installed it, ADR-008). It
# never reboots: a service restart is exactly how update_script.sh itself
# recovers and is proven not to drop the AP.
#
set -euo pipefail

log() { echo "[$(date -u +%FT%TZ)] wifucked-watchdog: $1"; }

if [[ "${EUID}" -ne 0 ]]; then
    log "must run as root"
    exit 1
fi

BASE_DIR=/opt/wifucked
CURRENT="${BASE_DIR}/current"
HEALTHY="${BASE_DIR}/healthy"
PREVIOUS_FILE="${BASE_DIR}/previous"
STAMP="${BASE_DIR}/rollback-attempted"
CONFIG_FILE="${WIFUCKED_STATE_DIR:-/var/lib/wifucked}/config.json"

# Number of health probes and the gap between them. The timer already waits for
# the daemon to come up (OnBootSec); these retries only absorb a transient
# hiccup, so that a slow single probe never triggers a destructive rollback.
HEALTH_ATTEMPTS=3
HEALTH_GAP_S=5

# The daemon serves /api/health on api_port (default 8080). A user who moved the
# port in config.json would otherwise be probed on the wrong port and wrongly
# rolled back, so read it back the same cheap way — no jq dependency.
PORT=8080
if [[ -r "${CONFIG_FILE}" ]]; then
    parsed="$(grep -oE '"api_port"[[:space:]]*:[[:space:]]*[0-9]+' "${CONFIG_FILE}" \
        | grep -oE '[0-9]+' | tail -n1 || true)"
    [[ -n "${parsed}" ]] && PORT="${parsed}"
fi

# One health probe. Cheap systemd liveness first (catches a process that is not
# even running), then the daemon's own /api/health. Returns non-zero only on a
# definite negative, never on ambiguity we could act on destructively.
health_ok() {
    systemctl is-active --quiet wifucked.service || return 1
    local body
    body="$(curl -sf --max-time 5 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null)" || return 1
    printf '%s' "${body}" | grep -qE '"ok"[[:space:]]*:[[:space:]]*true' || return 1
    return 0
}

if ! command -v curl >/dev/null 2>&1; then
    # No probe tool means we cannot prove the version bad. Rolling back on the
    # basis of a missing tool would be a self-inflicted outage, so refuse.
    log "curl not found; cannot validate health, leaving current version in place"
    exit 1
fi

log "validating current version (port ${PORT})"

healthy=0
attempt=1
while true; do
    if health_ok; then
        healthy=1
        break
    fi
    if [[ "${attempt}" -ge "${HEALTH_ATTEMPTS}" ]]; then
        break
    fi
    log "health probe ${attempt}/${HEALTH_ATTEMPTS} failed; retrying in ${HEALTH_GAP_S}s"
    attempt=$((attempt + 1))
    sleep "${HEALTH_GAP_S}"
done

CUR="$(readlink -f "${CURRENT}" 2>/dev/null || true)"

if [[ "${healthy}" -eq 1 ]]; then
    touch "${HEALTHY}"
    # The current version proved itself, so any earlier rollback attempt is now
    # ancient history; drop the audit stamp to keep the tree clean.
    rm -f "${STAMP}"
    log "current version healthy (${CUR:-unknown}); marked healthy after ${attempt} probe(s)"
    exit 0
fi

# --- unhealthy: consider a rollback ----------------------------------------

REVERT_TO=""
[[ -r "${PREVIOUS_FILE}" ]] && REVERT_TO="$(cat "${PREVIOUS_FILE}" 2>/dev/null || true)"
REVERT_REAL="$(readlink -f "${REVERT_TO}" 2>/dev/null || true)"

if [[ -z "${REVERT_TO}" || ! -d "${REVERT_REAL}" ]]; then
    log "current version UNHEALTHY (${CUR:-unknown}) but no valid previous slot to roll back to; leaving last-known-good in place (wifucked.service Restart=always keeps retrying)"
    exit 1
fi

# Rollback-loop guard, keyed to the version paths themselves so it is inherently
# self-clearing. A rollback repoints `current` at `previous`; `previous` is only
# ever rewritten by update_script.sh on a real update. So immediately after a
# rollback, current == previous, and this equality suppresses any further
# rollback on subsequent boots even if the reverted version is itself unhealthy
# (e.g. an environmental fault, not a bad update). The next genuine update makes
# current and previous distinct again, which naturally re-arms rollback — no
# stale sentinel to clear, and a re-released same-version path still rolls back
# correctly because previous still differs from the freshly-swapped current.
if [[ "${REVERT_REAL}" == "${CUR}" ]]; then
    log "current version UNHEALTHY (${CUR:-unknown}) and previous slot is the same version; already rolled back once, refusing to loop (leaving last-known-good in place)"
    exit 1
fi

log "current version UNHEALTHY (${CUR:-unknown}); rolling back to previous slot (${REVERT_REAL})"

# Record which version we rolled back away from, for the field log. This is an
# audit breadcrumb only — the loop guard above is what actually prevents a loop.
printf '%s\n' "${CUR}" > "${STAMP}"

ln -sfn "${REVERT_REAL}" "${CURRENT}"
log "current -> ${REVERT_REAL}"

# Restart only the control plane. hostapd/dnsmasq are untouched (ADR-011); the
# AP keeps serving and clients keep their leases across this restart, exactly as
# they do across a normal update.
if systemctl restart wifucked.service; then
    log "rollback complete; wifucked.service restarted on ${REVERT_REAL}"
else
    log "WARNING wifucked.service restart failed after rollback; Restart=always will retry on ${REVERT_REAL}"
fi

# Do not touch the healthy sentinel here: the reverted version has not been
# validated in this run. The next boot's watchdog validates it and, finding
# current == previous, will mark it healthy or stop without looping.
exit 0
