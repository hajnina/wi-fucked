"""Combines the AP E2E proof's individual result fragments into one report.

The orchestrator (``run_e2e_ap_test.sh``) writes one small JSON file per stage
(hostapd bring-up, association, DHCP, ping, the Playwright dashboard check)
into ``--fragments-dir`` as it goes. This script reads all of them and writes:

  - ``report.json`` — machine-readable, one object per stage plus an overall
    ``pass`` bool, for anything that wants to consume this later.
  - ``report.md`` — human-readable summary, written to ``$GITHUB_STEP_SUMMARY``
    when running in CI so the numbers are visible without opening artifacts.
  - ``junit.xml`` — one ``<testcase>`` per stage, so CI's test-reporting UI
    shows this next to the pytest suites instead of as an opaque shell exit
    code.
"""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def _testcase(name: str, fragment: dict) -> ET.Element:
    tc = ET.Element("testcase", classname="wifucked.e2e.ap_dashboard", name=name)
    tc.set("time", str(fragment.get("duration_s", 0)))
    if not fragment.get("pass", False):
        failure = ET.SubElement(tc, "failure")
        failure.set("message", fragment.get("error", "check failed"))
        failure.text = json.dumps(fragment, indent=2)
    return tc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fragments-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    fragments: dict[str, dict] = {}
    for path in sorted(args.fragments_dir.glob("*.json")):
        fragments[path.stem] = json.loads(path.read_text())

    overall_pass = bool(fragments) and all(f.get("pass", False) for f in fragments.values())

    report = {"pass": overall_pass, "stages": fragments}
    (args.out_dir / "report.json").write_text(json.dumps(report, indent=2))

    lines = [
        "## AP + dashboard E2E proof",
        "",
        f"**Result: {'PASS' if overall_pass else 'FAIL'}**",
        "",
        "| Stage | Result | Detail |",
        "|---|---|---|",
    ]
    for name, fragment in fragments.items():
        status = "PASS" if fragment.get("pass") else "FAIL"
        detail = fragment.get("detail", fragment.get("error", ""))
        lines.append(f"| {name} | {status} | {detail} |")
    lines.append("")
    (args.out_dir / "report.md").write_text("\n".join(lines))
    print("\n".join(lines))

    suite = ET.Element(
        "testsuite",
        name="wifucked.e2e.ap_dashboard",
        tests=str(len(fragments)),
        failures=str(sum(1 for f in fragments.values() if not f.get("pass"))),
    )
    for name, fragment in fragments.items():
        suite.append(_testcase(name, fragment))
    ET.ElementTree(suite).write(args.out_dir / "junit.xml", encoding="utf-8", xml_declaration=True)

    return 0 if overall_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
