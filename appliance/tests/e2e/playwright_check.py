"""Loads the real dashboard over the real AP-facing network path and proves it.

Run inside the client network namespace (so the HTTP request actually
traverses the same L2/L3 path a real associated Wi-Fi client's browser would),
pointed at the gateway address dnsmasq handed out. Exercises:

  - DNS is not required (bare IP, matching how a phone's captive-portal probe
    or a manually-typed dashboard URL would reach it).
  - The real Flask app, real Jinja templates, real static CSS — no mocking
    above the HAL.
  - HTTP Basic Auth against the dashboard's real token check
    (``wifucked.api.create_app``'s ``before_request`` hook).

Produces a JSON result file with pass/fail per check, timings, and a full-page
screenshot, so a CI run leaves behind evidence instead of a bare exit code.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--timeout-ms", type=int, default=15_000)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {"url": args.url, "checks": {}, "pass": False}

    with sync_playwright() as pw:
        # --no-sandbox: this process runs as root inside a network namespace
        # (ip netns exec), and Chromium's own sandbox refuses to start as root.
        browser = pw.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context(
            http_credentials={"username": "wifucked", "password": args.token},
            ignore_https_errors=True,
        )
        page = context.new_page()

        try:
            started = time.monotonic()
            response = page.goto(args.url, timeout=args.timeout_ms, wait_until="load")
            load_ms = round((time.monotonic() - started) * 1000, 1)
            result["load_ms"] = load_ms

            status = response.status if response else None
            result["checks"]["http_status_200"] = status == 200
            result["http_status"] = status

            title = page.title()
            result["title"] = title
            result["checks"]["title_matches"] = "WI-FUCKED" in title

            heading_visible = page.locator("h1").first.is_visible()
            result["checks"]["heading_visible"] = heading_visible

            screenshot_path = args.out_dir / "dashboard.png"
            page.screenshot(path=str(screenshot_path), full_page=True)
            result["screenshot"] = str(screenshot_path)

            # /api/health is unauthenticated in production (liveness probes
            # can't hold a token) — a separate request proves that path too,
            # not just the token-gated dashboard page.
            health_started = time.monotonic()
            health_response = context.request.get(
                args.url.rstrip("/") + "/api/health", timeout=args.timeout_ms
            )
            result["health_ms"] = round((time.monotonic() - health_started) * 1000, 1)
            result["checks"]["health_status_200"] = health_response.status == 200
            try:
                health_body = health_response.json()
                result["checks"]["health_ok_true"] = health_body.get("ok") is True
                result["health"] = health_body
            except ValueError:
                result["checks"]["health_ok_true"] = False

        except Exception as exc:  # captured as a test result, not swallowed
            result["error"] = f"{type(exc).__name__}: {exc}"
            failure_path = args.out_dir / "dashboard-failure.png"
            try:
                page.screenshot(path=str(failure_path), full_page=True)
                result["screenshot"] = str(failure_path)
            except Exception as screenshot_exc:  # best-effort diagnostic only
                result["screenshot_error"] = f"{type(screenshot_exc).__name__}: {screenshot_exc}"
        finally:
            context.close()
            browser.close()

    result["pass"] = (
        bool(result["checks"]) and all(result["checks"].values()) and "error" not in result
    )

    (args.out_dir / "playwright_result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
