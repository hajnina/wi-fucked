#!/bin/sh
#
# Resolves FABRIC_ADDRESS/FABRIC_USERNAME/FABRIC_PASSWORD before gunicorn
# starts. This runs exactly once, as PID 1, before any gunicorn worker
# exists — so the interactive wizard (fabric.setup_wizard) never races
# multiple workers prompting the same terminal for input. Workers just
# inherit whatever this process resolves into the environment before exec.
set -eu

if [ -z "${FABRIC_ADDRESS:-}" ] || [ -z "${FABRIC_USERNAME:-}" ] || [ -z "${FABRIC_PASSWORD:-}" ]; then
    wizard_output="$(python3 -m fabric.setup_wizard)" || exit 1
    eval "${wizard_output}"
fi

exec "$@"
