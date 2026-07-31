"""Interactive first-run setup for FABRIC_ADDRESS/FABRIC_USERNAME/FABRIC_PASSWORD.

Invoked by docker-entrypoint.sh, once, before gunicorn starts — never from
inside a gunicorn worker. Prints `export KEY=VALUE` lines to stdout so the
entrypoint can `eval` them into the environment that gunicorn (and all its
workers) then inherits. Nothing is written to disk: no credential file to
secure, rotate, or clean up.

Fails fast with no prompt at all when stdin isn't a TTY (a detached or
orchestrated container) — a wizard that silently blocks forever on `input()`
is worse than a container that exits with a clear instruction.
"""

from __future__ import annotations

import getpass
import shlex
import sys

from fabric.config import REQUIRED_ENV_VARS


def prompt_for_config() -> dict[str, str]:
    print("=== Fabric first-run setup ===", file=sys.stderr)
    print(
        "FABRIC_ADDRESS / FABRIC_USERNAME / FABRIC_PASSWORD are not set. "
        "Enter them now, or set the environment variables to skip this next run.",
        file=sys.stderr,
    )

    address = ""
    while not address:
        address = input("Public address devices will connect to (host:port): ").strip()

    username = ""
    while not username:
        username = input("Admin username: ").strip()

    while True:
        password = getpass.getpass("Admin password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password and password == confirm:
            break
        print("Passwords were empty or did not match — try again.", file=sys.stderr)

    return {"FABRIC_ADDRESS": address, "FABRIC_USERNAME": username, "FABRIC_PASSWORD": password}


def main() -> int:
    if not sys.stdin.isatty():
        print(
            "FATAL: " + ", ".join(REQUIRED_ENV_VARS) + " must be set as "
            "environment variables (no TTY available for interactive setup).",
            file=sys.stderr,
        )
        return 1

    values = prompt_for_config()
    for key, value in values.items():
        print(f"export {key}={shlex.quote(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
