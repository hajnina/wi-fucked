#!/bin/bash
#
# The single entrypoint for testing. CI runs exactly this, so a green run here
# means a green run there — and discovering a lint error on a runner that is
# baking a multi-gigabyte image is rude to whoever is queued behind you.
#
# Nothing here needs a Raspberry Pi. MOCK_HW=1 is the primary development path.
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

export MOCK_HW=1
export PYTHONPATH="appliance/src:fabric/src"

FAILURES=0
step() { printf '\n=== %s\n' "$1"; }
fail() { echo "FAILED: $1"; FAILURES=$((FAILURES + 1)); }

# --- airgap -----------------------------------------------------------------
# The dashboard must work with no Internet. Runs first because it is instant and
# because a CDN reference invalidates everything downstream.

step "Airgap check"
python3 appliance/tests/verify_no_external_assets.py appliance || fail "airgap"

# --- lint -------------------------------------------------------------------

if command -v ruff > /dev/null; then
    step "Ruff"
    ruff check appliance/src fabric/src appliance/tests || fail "ruff check"
    ruff format --check appliance/src fabric/src appliance/tests || fail "ruff format"
else
    echo "ruff not installed; skipping (pip install -r appliance/requirements-dev.txt)"
fi

if command -v shellcheck > /dev/null; then
    step "Shellcheck"
    shellcheck appliance/*.sh scripts/*.sh appliance/stage-custom/opt/dirty/*.sh \
        run_all_tests.sh || fail "shellcheck"
else
    echo "shellcheck not installed; skipping"
fi

# --- tests ------------------------------------------------------------------

step "Unit tests"
python3 -m pytest appliance/tests/ -q --ignore=appliance/tests/scenarios || fail "unit tests"

# The ones that matter most: control behaviour over time, plus the two
# invariants — the AP never drops, and BACKUP carries zero bytes until critical
# demand genuinely cannot be met.
step "Scenario tests"
python3 -m pytest appliance/tests/scenarios/ -q || fail "scenario tests"

# --- packaging --------------------------------------------------------------
# Cheap, and catches a broken package builder before it costs an image bake.

step "Package builder"
TMP_PKG="$(mktemp -d)/smoke.wtf"
./scripts/build_package.sh 0.0.0-smoke "${TMP_PKG}" > /dev/null || fail "build_package"
for required in update.sh NEWVERSION APP.zip apt_deps.txt py_deps.txt; do
    unzip -l "${TMP_PKG}" | grep -q "${required}" || fail "package missing ${required}"
done
rm -rf "$(dirname "${TMP_PKG}")"
echo "Package contents OK"

# --- result -----------------------------------------------------------------

printf '\n'
if [ "${FAILURES}" -ne 0 ]; then
    echo "FAILED: ${FAILURES} step(s)."
    exit 1
fi
echo "All tests passed."
