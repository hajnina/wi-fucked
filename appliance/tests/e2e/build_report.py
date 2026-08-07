"""Builds the rich HTML report: real link-health/switch-timeline graphs, the
real decision log, and every screenshot captured during the run, in one
self-contained page.

Runs on the host after extracting the guest's results disk. Reads whatever
of these `run_e2e_ap_test.sh`/`guest/e2e_driver.sh` produced (all optional —
an early failure may not have gotten far enough to write them):

  - ``fragments/*.json`` — per-stage pass/fail (from ``aggregate_report.py``)
  - ``state_snapshots.json`` — ``{"snapshots": [...], "decisions": [...]}``,
    real ``/api/state``/``/api/decisions`` polls during the chaos window
  - ``chaos_summary.json`` — disconnect/switch counts derived from the above
  - ``screenshots/**/dashboard.png`` — every Playwright capture

No charting library: this repo's own dashboard is airgapped by policy
(SOP-003), and a CI report someone opens from a downloaded artifact zip with
no internet is under the same constraint in spirit, so the graphs here are
hand-rolled inline SVG rather than a JS charting dependency.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

_COLORS = {
    "wan_a": "#4C78A8",
    "wan_b": "#F58518",
    "wan_c": "#54A24B",
    "wan_d": "#B279A2",
}


def _color_for(atomic_id: str, index: int) -> str:
    palette = list(_COLORS.values())
    return palette[index % len(palette)]


def _svg_health_timeline(snapshots: list[dict], width: int = 900, height: int = 260) -> str:
    """RTT (ms) per atomic over time, one polyline per atomic."""
    series: dict[str, list[tuple[float, float]]] = {}
    max_t = 0.0
    max_rtt = 50.0
    for snap in snapshots:
        t = snap.get("t", 0.0)
        max_t = max(max_t, t)
        state = snap.get("state")
        if not state:
            continue
        for atomic in state.get("atomics", []):
            rtt = (atomic.get("quality") or {}).get("rtt_ms")
            if rtt is None:
                continue
            series.setdefault(atomic["id"], []).append((t, rtt))
            max_rtt = max(max_rtt, rtt)

    if not series or max_t == 0:
        return "<p><em>No RTT samples were captured.</em></p>"

    pad = 40
    plot_w, plot_h = width - 2 * pad, height - 2 * pad

    def sx(t: float) -> float:
        return pad + (t / max_t) * plot_w

    def sy(rtt: float) -> float:
        return pad + plot_h - (rtt / max_rtt) * plot_h

    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">']
    parts.append(
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" stroke="#ddd"/>'
    )
    # axes
    ax_bottom = pad + plot_h
    ax_right = pad + plot_w
    parts.append(f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{ax_bottom}" stroke="#999"/>')
    parts.append(
        f'<line x1="{pad}" y1="{ax_bottom}" x2="{ax_right}" y2="{ax_bottom}" stroke="#999"/>'
    )
    parts.append(f'<text x="4" y="{pad}" font-size="11" fill="#666">{max_rtt:.0f}ms</text>')
    parts.append(f'<text x="4" y="{ax_bottom}" font-size="11" fill="#666">0ms</text>')
    parts.append(f'<text x="{pad}" y="{height - 8}" font-size="11" fill="#666">0s</text>')
    label_x = ax_right - 30
    parts.append(
        f'<text x="{label_x}" y="{height - 8}" font-size="11" fill="#666">{max_t:.0f}s</text>'
    )

    legend_y = 14
    for index, (atomic_id, points) in enumerate(sorted(series.items())):
        color = _color_for(atomic_id, index)
        points.sort()
        path = " ".join(f"{sx(t):.1f},{sy(r):.1f}" for t, r in points)
        parts.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2"/>')
        lx = pad + index * 160
        parts.append(f'<rect x="{lx}" y="{legend_y - 9}" width="10" height="10" fill="{color}"/>')
        parts.append(
            f'<text x="{lx + 14}" y="{legend_y}" font-size="12" fill="#333">{atomic_id}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _svg_switch_timeline(snapshots: list[dict], width: int = 900, height: int = 90) -> str:
    """Which atomic was primary, as a step chart / colored timeline bar."""
    points = [
        (s.get("t", 0.0), s["state"]["allocation"].get("primary_id"))
        for s in snapshots
        if s.get("state") and s["state"].get("allocation")
    ]
    if not points:
        return "<p><em>No allocation snapshots were captured.</em></p>"

    max_t = max(t for t, _ in points) or 1.0
    ids = sorted({pid for _, pid in points if pid})
    color_of = {pid: _color_for(pid, i) for i, pid in enumerate(ids)}

    pad = 40
    plot_w = width - 2 * pad
    bar_y, bar_h = 30, 30

    def sx(t: float) -> float:
        return pad + (t / max_t) * plot_w

    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">']
    parts.append(
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" stroke="#ddd"/>'
    )
    for (t0, pid), (t1, _) in zip(points, [*points[1:], (max_t, None)], strict=True):
        color = color_of.get(pid, "#ccc") if pid else "#eee"
        parts.append(
            f'<rect x="{sx(t0):.1f}" y="{bar_y}" width="{max(1.0, sx(t1) - sx(t0)):.1f}" '
            f'height="{bar_h}" fill="{color}"/>'
        )
    legend_y = bar_y + bar_h + 20
    for i, pid in enumerate(ids):
        lx = pad + i * 160
        parts.append(
            f'<rect x="{lx}" y="{legend_y - 9}" width="10" height="10" fill="{color_of[pid]}"/>'
        )
        parts.append(f'<text x="{lx + 14}" y="{legend_y}" font-size="12" fill="#333">{pid}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _b64_image(path: Path) -> str | None:
    try:
        return base64.b64encode(path.read_bytes()).decode()
    except OSError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", required=True, type=Path)
    args = parser.parse_args()
    results = args.results_dir

    fragments = {}
    for path in (
        sorted((results / "fragments").glob("*.json")) if (results / "fragments").exists() else []
    ):
        fragments[path.stem] = json.loads(path.read_text())
    overall_pass = bool(fragments) and all(f.get("pass", False) for f in fragments.values())

    state_path = results / "state_snapshots.json"
    snapshots: list[dict] = []
    decisions: list[dict] = []
    if state_path.exists():
        blob = json.loads(state_path.read_text())
        snapshots = blob.get("snapshots", [])
        decisions = blob.get("decisions", [])

    chaos_summary = {}
    summary_path = results / "chaos_summary.json"
    if summary_path.exists():
        chaos_summary = json.loads(summary_path.read_text())

    screenshots = (
        sorted((results / "screenshots").rglob("dashboard.png"))
        if (results / "screenshots").exists()
        else []
    )

    stage_rows = "\n".join(
        f'<tr class="{"pass" if f.get("pass") else "fail"}">'
        f"<td>{name}</td><td>{'PASS' if f.get('pass') else 'FAIL'}</td>"
        f"<td>{f.get('duration_s', 0):.1f}s</td><td>{f.get('detail', '')}</td></tr>"
        for name, f in fragments.items()
    )

    decision_rows = "\n".join(
        f"<tr><td>{d.get('at', '')}</td><td>{d.get('action', '')}</td>"
        f"<td>{d.get('reason', '')}</td>"
        f"<td>{json.dumps(d.get('inputs', {}))}</td></tr>"
        for d in decisions[:200]
    )

    screenshot_html = "\n".join(
        f"<figure><figcaption>{path.parent.name}</figcaption>"
        f'<img src="data:image/png;base64,{_b64_image(path)}" alt="{path.parent.name}"></figure>'
        for path in screenshots
        if _b64_image(path)
    )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>wifucked E2E report</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 1000px; margin: 2rem auto;
        padding: 0 1rem; color: #222; }}
h1, h2 {{ border-bottom: 1px solid #ddd; padding-bottom: 0.3rem; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: 0.9rem; }}
td, th {{ border: 1px solid #ddd; padding: 4px 8px; text-align: left; vertical-align: top; }}
tr.pass {{ background: #eafbea; }}
tr.fail {{ background: #fdeaea; }}
.badge {{ font-size: 1.4rem; font-weight: bold; padding: 0.2rem 0.6rem; border-radius: 4px; }}
.badge.pass {{ background: #2e7d32; color: white; }}
.badge.fail {{ background: #c62828; color: white; }}
figure {{ display: inline-block; margin: 0 1rem 1rem 0; }}
figure img {{ max-width: 440px; border: 1px solid #ccc; }}
figcaption {{ font-size: 0.85rem; color: #555; }}
</style></head>
<body>
<h1>WI-FUCKED AP + routing E2E report</h1>
<p><span class="badge {"pass" if overall_pass else "fail"}">
{"PASS" if overall_pass else "FAIL"}</span></p>

<h2>Stages</h2>
<table><tr><th>Stage</th><th>Result</th><th>Duration</th><th>Detail</th></tr>
{stage_rows}
</table>

<h2>Real WAN health over time (RTT, ms, per atomic)</h2>
{_svg_health_timeline(snapshots)}

<h2>Real allocator: which WAN was primary, over time</h2>
{_svg_switch_timeline(snapshots)}
<p>{json.dumps(chaos_summary)}</p>

<h2>Real decision log ({len(decisions)} entries, newest first, showing up to 200)</h2>
<table><tr><th>At</th><th>Action</th><th>Reason</th><th>Inputs</th></tr>
{decision_rows}
</table>

<h2>Dashboard screenshots</h2>
{screenshot_html}

</body></html>
"""
    (results / "report.html").write_text(html)
    print(f"wrote {results / 'report.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
