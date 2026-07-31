#!/bin/bash
#
# Build a .wtf update package — Wi-Fucked Transfer Format.
#
# Usage: build_package.sh <VERSION> <OUTPUT>
#
# NO SECRETS GO IN HERE. Release assets are distributed, and anyone who can read
# a release gets everything inside it. Device keys are generated on-device at
# first boot, and CI greps this package for key-shaped content as a gate
# (SOP-008). Do not weaken that.
#
set -euo pipefail

VERSION="${1:?usage: build_package.sh <VERSION> <OUTPUT>}"
OUTPUT="${2:?usage: build_package.sh <VERSION> <OUTPUT>}"

[[ "${OUTPUT}" != /* ]] && OUTPUT="$(pwd)/${OUTPUT}"
mkdir -p "$(dirname "${OUTPUT}")"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPLIANCE="${REPO_ROOT}/appliance"

if [[ ! -d "${APPLIANCE}/src/wifucked" ]]; then
    echo "error: cannot find appliance/src/wifucked under ${REPO_ROOT}" >&2
    exit 1
fi

STAGING="$(mktemp -d)"
trap 'rm -rf "${STAGING}"' EXIT

echo "Building wifucked ${VERSION} -> ${OUTPUT}"

# 1. Application code.
( cd "${APPLIANCE}" && zip -qr "${STAGING}/APP.zip" src/ \
    -x '*/__pycache__/*' -x '*.pyc' )

# 2. Dependency manifests.
cp "${APPLIANCE}/apt_deps.txt" "${STAGING}/"
cp "${APPLIANCE}/requirements.txt" "${STAGING}/py_deps.txt"

# 3. System configuration: units, hostapd/dnsmasq drop-ins, helper scripts.
cp -r "${APPLIANCE}/stage-custom" "${STAGING}/"

# 4. The installer, and the version it installs.
cp "${APPLIANCE}/update_script.sh" "${STAGING}/update.sh"
chmod +x "${STAGING}/update.sh"
echo "${VERSION}" > "${STAGING}/NEWVERSION"

# 5. Refuse to ship a credential. This is a gate, not a warning — a leaked
#    fabric key cannot be un-leaked once a release is published.
if grep -rIlE 'PRIVATE KEY|BEGIN OPENSSH|wpa_passphrase=[a-z0-9]{8}' "${STAGING}" \
   --exclude='*.zip' 2>/dev/null | grep -q .; then
    echo "FATAL: key-shaped content found in the package. Refusing to build." >&2
    grep -rIlE 'PRIVATE KEY|BEGIN OPENSSH|wpa_passphrase=[a-z0-9]{8}' "${STAGING}" \
        --exclude='*.zip' >&2
    exit 1
fi

( cd "${STAGING}" && zip -qr "${OUTPUT}" \
    update.sh py_deps.txt apt_deps.txt APP.zip NEWVERSION stage-custom )

echo "Built $(basename "${OUTPUT}") ($(du -h "${OUTPUT}" | cut -f1))"
