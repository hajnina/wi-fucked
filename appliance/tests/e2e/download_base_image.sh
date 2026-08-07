#!/bin/bash
# Fetches and caches the Debian 12 (bookworm) "generic" cloud qcow2 image.
#
# The "generic" variant, not "genericcloud": genericcloud ships a
# cloud-optimized kernel that commonly drops modules cloud VMs never need
# (wireless drivers included) — a real risk for a test whose entire premise
# is a real mac80211_hwsim radio. "generic" ships the same kernel flavour a
# normal Debian install uses, which is expected to carry mac80211_hwsim as a
# module. If a run's "hwsim module" stage fails, check this assumption first
# (see appliance/tests/e2e/README.md).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${WIFUCKED_E2E_WORKDIR:-${HERE}/.work}"
mkdir -p "${WORK}"

DEBIAN_VER="bookworm"
IMAGE="debian-12-generic-amd64.qcow2"
URL="https://cloud.debian.org/images/cloud/${DEBIAN_VER}/latest/${IMAGE}"
DEST="${WORK}/${IMAGE}"

if [ ! -f "${DEST}" ]; then
    echo "--- fetching ${URL}"
    curl -fSL --retry 3 -o "${DEST}.partial" "${URL}"
    mv "${DEST}.partial" "${DEST}"
else
    echo "--- using cached ${DEST}"
fi

echo "${DEST}"
