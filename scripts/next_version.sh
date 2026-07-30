#!/bin/bash
#
# Derive the next version from git tags and conventional commits (ADR-016).
#
# The source of truth is an annotated tag `vX.Y.Z` on master — not a file. That
# is why there is no VERSION file to bump, nothing committed back to the branch
# being built, and no push race to lose.
#
# Usage:
#   next_version.sh                      # emit KEY=value pairs
#   next_version.sh --github             # also append to $GITHUB_OUTPUT
#   next_version.sh --pr 42              # prerelease for PR #42
#   next_version.sh --rc                 # release-candidate prerelease
#
set -euo pipefail

MODE="release"
PR_NUMBER=""
GITHUB_MODE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --github) GITHUB_MODE=true; shift ;;
        --pr)     MODE="pr"; PR_NUMBER="${2:?--pr needs a number}"; shift 2 ;;
        --rc)     MODE="rc"; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

SHA="$(git rev-parse --short=7 HEAD)"

LAST_TAG="$(git describe --tags --abbrev=0 --match 'v[0-9]*.[0-9]*.[0-9]*' 2>/dev/null || true)"

if [[ -z "${LAST_TAG}" ]]; then
    # No releases yet. A first release is 0.1.0 rather than 0.0.1 — the project
    # exists, which is a feature.
    MAJOR=0; MINOR=1; PATCH=0
    BUMP="initial"
    RANGE="HEAD"
    BASE="0.0.0"
else
    BASE="${LAST_TAG#v}"
    IFS='.' read -r MAJOR MINOR PATCH <<< "${BASE}"
    RANGE="${LAST_TAG}..HEAD"

    SUBJECTS="$(git log --format='%s' "${RANGE}" 2>/dev/null || true)"
    BODIES="$(git log --format='%B' "${RANGE}" 2>/dev/null || true)"

    # Highest bump found in the range wins.
    if grep -qE '^[a-z]+(\([^)]*\))?!:' <<< "${SUBJECTS}" \
       || grep -qE '^BREAKING[ -]CHANGE:' <<< "${BODIES}"; then
        BUMP="major"
        MAJOR=$((MAJOR + 1)); MINOR=0; PATCH=0
    elif grep -qE '^feat(\([^)]*\))?:' <<< "${SUBJECTS}"; then
        BUMP="minor"
        MINOR=$((MINOR + 1)); PATCH=0
    else
        BUMP="patch"
        PATCH=$((PATCH + 1))
    fi
fi

CORE="${MAJOR}.${MINOR}.${PATCH}"

case "${MODE}" in
    # Real SemVer prereleases: they sort BELOW the release they precede, so an
    # OTA client can never mistake a PR build for something shipped.
    pr)      VERSION="${CORE}-pr${PR_NUMBER}.${SHA}"; PUBLISH=false ;;
    rc)      VERSION="${CORE}-rc.${SHA}";             PUBLISH=false ;;
    release) VERSION="${CORE}";                       PUBLISH=true  ;;
esac

emit() {
    echo "$1"
    if [[ "${GITHUB_MODE}" == true && -n "${GITHUB_OUTPUT:-}" ]]; then
        echo "$1" >> "${GITHUB_OUTPUT}"
    fi
}

emit "version=${VERSION}"
emit "core=${CORE}"
emit "tag=v${CORE}"
emit "bump=${BUMP}"
emit "previous=${BASE}"
emit "commit=${SHA}"
emit "publish=${PUBLISH}"
