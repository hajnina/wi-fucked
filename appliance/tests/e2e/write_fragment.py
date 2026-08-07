"""Tiny helper: write one stage's result fragment as JSON.

Exists so ``run_e2e_ap_test.sh`` never hand-builds JSON in bash (a reliable
source of quoting bugs) — one process per fragment, called from the shell.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fragments-dir", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--pass", dest="ok", action="store_true")
    parser.add_argument("--fail", dest="ok", action="store_false")
    parser.add_argument("--duration-s", type=float, default=0.0)
    parser.add_argument("--detail", default="")
    parser.add_argument("--error", default="")
    parser.set_defaults(ok=None)
    args = parser.parse_args()

    if args.ok is None:
        raise SystemExit("must pass --pass or --fail")

    args.fragments_dir.mkdir(parents=True, exist_ok=True)
    fragment = {
        "pass": args.ok,
        "duration_s": round(args.duration_s, 3),
        "detail": args.detail,
    }
    if args.error:
        fragment["error"] = args.error
    (args.fragments_dir / f"{args.name}.json").write_text(json.dumps(fragment, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
