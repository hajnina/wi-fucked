#!/bin/bash
#
# Generate manifest.json — the one file the OTA client reads.
#
# Asset names are constructible from the version, so the client never scrapes
# the GitHub API. It fetches this manifest from
# /releases/latest/download/manifest.json and everything else follows.
#
# Usage: gen_manifest.sh <VERSION> <IMAGE> <PACKAGE> [OUTPUT]
#
set -euo pipefail

VERSION="${1:?usage: gen_manifest.sh <VERSION> <IMAGE> <PACKAGE> [OUTPUT]}"
IMAGE="${2:?missing image path}"
PACKAGE="${3:?missing package path}"
OUTPUT="${4:-manifest.json}"

REPO="${GITHUB_REPOSITORY:-hajnina/wi-fucked}"
COMMIT="$(git rev-parse --short=7 HEAD 2>/dev/null || echo unknown)"
BUILT_AT="$(date -u +%FT%TZ)"
BASE="https://github.com/${REPO}/releases/download/v${VERSION}"

sha_of() { sha256sum "$1" | cut -d' ' -f1; }

# The oldest version this package can upgrade *from*. Bump it when update.sh
# stops being able to migrate an older layout, so an ancient device is told to
# reflash rather than half-updating into a broken state.
MIN_UPGRADABLE="${DIRTY_MIN_UPGRADABLE:-0.1.0}"

# The fabric protocol floor. An appliance refuses an older fabric rather than
# failing mysteriously mid-tunnel (ADR-005).
FABRIC_MIN="${DIRTY_FABRIC_MIN:-0.1.0}"

cat > "${OUTPUT}" <<EOF
{
  "version": "${VERSION}",
  "released_at": "${BUILT_AT}",
  "commit": "${COMMIT}",
  "channel": "master",
  "image_url": "${BASE}/$(basename "${IMAGE}")",
  "package_url": "${BASE}/$(basename "${PACKAGE}")",
  "sha256": {
    "image": "$(sha_of "${IMAGE}")",
    "package": "$(sha_of "${PACKAGE}")"
  },
  "size_bytes": {
    "image": $(stat -c %s "${IMAGE}"),
    "package": $(stat -c %s "${PACKAGE}")
  },
  "fabric_image": "ghcr.io/${REPO}/fabric:${VERSION}",
  "fabric_min": "${FABRIC_MIN}",
  "min_upgradable_from": "${MIN_UPGRADABLE}"
}
EOF

echo "Wrote ${OUTPUT}:"
cat "${OUTPUT}"
