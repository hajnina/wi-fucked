#!/usr/bin/env python3
"""Airgap gate.

The appliance is frequently the thing standing between the user and the
Internet, so a dashboard that needs the Internet is useless exactly when it is
needed. Every byte must be served from the Pi.

Runs in CI before the image is baked. If you need a library, vendor it into
``ui/static/``.

Usage: verify_no_external_assets.py [root]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS = (
    re.compile(r"""https?://(?!localhost|127\.0\.0\.1|dirty\.local)""", re.I),
    re.compile(r"""(?:src|href)\s*=\s*["']//""", re.I),
    re.compile(r"""@import\s+url\(\s*["']?https?:""", re.I),
    re.compile(r"""fonts\.(?:googleapis|gstatic)\.com""", re.I),
    re.compile(r"""cdn(?:js)?\.|unpkg\.com|jsdelivr\.net""", re.I),
)

SUFFIXES = {".html", ".htm", ".css", ".js", ".jinja", ".j2"}

#: Comment lines may legitimately mention a URL — a link to an ADR, a spec, or a
#: docs page. Only shipped references matter.
COMMENT = re.compile(r"^\s*(?:#|//|/\*|\*|<!--)")


def scan(root: Path) -> list[tuple[Path, int, str]]:
    findings: list[tuple[Path, int, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, start=1):
            if COMMENT.match(line):
                continue
            for pattern in PATTERNS:
                if pattern.search(line):
                    findings.append((path, number, line.strip()[:120]))
                    break
    return findings


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else "appliance").resolve()
    if not root.exists():
        print(f"airgap: nothing to scan at {root}", file=sys.stderr)
        return 1

    findings = scan(root)
    if not findings:
        print(f"airgap: OK — no external asset references under {root}")
        return 0

    print(f"airgap: FAILED — {len(findings)} external reference(s):", file=sys.stderr)
    for path, number, line in findings:
        print(f"  {path}:{number}: {line}", file=sys.stderr)
    print(
        "\nThe dashboard must work with no Internet. Vendor the asset into "
        "appliance/src/dirty/ui/static/ instead.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
