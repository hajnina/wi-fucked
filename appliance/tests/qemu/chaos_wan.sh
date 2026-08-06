#!/bin/bash
# Degrades tap-wan1/tap-wan2 (the two "shitty" WAN links, host side) on an
# independent, time-varying schedule while the WAN-chaos download proof runs.
#
# Shapes with `tc netem` (loss/delay/jitter) when the kernel running this has
# the module; falls back to `tc tbf` (rate only) plus real link down/up
# (`ip link set ... down`/`up`, a real, unambiguous outage — no packet ever
# gets a chance to be dropped-vs-delayed-vs-corrupted, the interface simply
# isn't there) when it doesn't. Checked once at startup, not assumed: this
# was built in a sandbox whose kernel has `tbf`/`htb`/`pfifo` compiled in but
# no `netem`/`cake`/`fq_codel` module and no `modprobe` to load one — a
# restricted container kernel, not this repo's build target. A future run on
# a normal Linux host should pick up real netem loss/jitter automatically.
#
# Independent, time-varying, not a single scripted failover: each link gets
# its own phase-shifted cycle of good/throttled/outage windows so the two
# links are rarely bad at exactly the same moment (an aggregation problem,
# which ADR-004 explicitly does not attempt to solve) but do overlap
# partially by design (a genuine "both links are having a bad time"
# stretch), while never *both* going to a full simultaneous outage — that
# would test raw TCP timeout tolerance, not this appliance's failover.
#
# Usage: chaos_wan.sh <tap-wan1> <tap-wan2> <duration_s> <log_file>
set -euo pipefail

TAP_A="${1:?tap-wan1 device}"
TAP_B="${2:?tap-wan2 device}"
DURATION_S="${3:?duration}"
LOG="${4:?log file}"

: > "${LOG}"
log() { echo "$(date +%s.%N) $*" >> "${LOG}"; }

NETEM_AVAILABLE=0
if tc qdisc replace dev "${TAP_A}" root netem delay 1ms 2>/dev/null; then
    NETEM_AVAILABLE=1
fi
tc qdisc del dev "${TAP_A}" root 2>/dev/null || true
log "netem_available=${NETEM_AVAILABLE}"

apply_good() {
    local dev="$1"
    if [ "${NETEM_AVAILABLE}" = "1" ]; then
        tc qdisc replace dev "${dev}" root netem delay 15ms 5ms loss 0.1% rate 8mbit 2>>"${LOG}" || true
    else
        tc qdisc replace dev "${dev}" root tbf rate 8mbit burst 64kbit latency 400ms 2>>"${LOG}" || true
    fi
    ip link set "${dev}" up 2>>"${LOG}" || true
}

apply_throttled() {
    local dev="$1"
    if [ "${NETEM_AVAILABLE}" = "1" ]; then
        tc qdisc replace dev "${dev}" root netem delay 180ms 60ms loss 6% rate 256kbit 2>>"${LOG}" || true
    else
        tc qdisc replace dev "${dev}" root tbf rate 128kbit burst 8kbit latency 800ms 2>>"${LOG}" || true
    fi
    ip link set "${dev}" up 2>>"${LOG}" || true
}

apply_wrecked() {
    local dev="$1"
    if [ "${NETEM_AVAILABLE}" = "1" ]; then
        tc qdisc replace dev "${dev}" root netem delay 400ms 150ms loss 25% rate 64kbit 2>>"${LOG}" || true
        ip link set "${dev}" up 2>>"${LOG}" || true
    else
        # No loss/delay shaping available — a near-dead link, faithfully
        # represented as a real (if crude) outage rather than faked.
        ip link set "${dev}" down 2>>"${LOG}" || true
    fi
}

apply_down() {
    local dev="$1"
    ip link set "${dev}" down 2>>"${LOG}" || true
}

# One phase step: 12s. wan-a and wan-b run independent cycles, offset so
# their bad windows mostly (not always) miss each other — plus two
# deliberately adversarial phases (index 5-6) where *both* are throttled at
# the same time. That's the case that actually exercises whether the
# allocator can tell the two apart when neither is simply "good": before the
# probe-budget fix (docs/active-tests.md), the second-probed atomic could go
# an entire bad patch without being re-measured at all, so a window where
# both links are simultaneously non-ideal is exactly where a stale, unverified
# health value would have gone unnoticed. Never both fully down at once,
# still — that tests raw TCP timeout tolerance, not this appliance's failover.
CYCLE_A=(good good throttled wrecked good throttled throttled down good good throttled good)
CYCLE_B=(throttled good good good wrecked throttled throttled good throttled good good good)
STEP_S=12
N=${#CYCLE_A[@]}

started=$(date +%s)
i=0
while true; do
    now=$(date +%s)
    elapsed=$((now - started))
    [ "${elapsed}" -ge "${DURATION_S}" ] && break

    idx=$((i % N))
    a="${CYCLE_A[$idx]}"
    b="${CYCLE_B[$idx]}"
    log "t=+${elapsed}s phase=${idx} wan-a=${a} wan-b=${b}"
    "apply_${a}" "${TAP_A}"
    "apply_${b}" "${TAP_B}"

    i=$((i + 1))
    sleep "${STEP_S}"
done

log "chaos schedule complete, restoring both links to good"
apply_good "${TAP_A}"
apply_good "${TAP_B}"
