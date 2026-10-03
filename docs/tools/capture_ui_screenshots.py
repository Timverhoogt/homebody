"""Re-capture the sanitized dashboard screenshots in docs/assets from the real static UI.

The page is served from homebody/static inside a headless browser. Every request is answered
locally: /api/status returns the synthetic, non-private status below, /api/robot/options returns
the app's real allowlists, and everything else gets an empty 404, so nothing reaches a network,
robot or camera. Captures are 1440x900 and saved as metadata-free WebP.

    uv pip install playwright pillow
    python docs/tools/capture_ui_screenshots.py [--chromium /path/to/chrome]
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import tempfile
from pathlib import Path

from PIL import Image
from playwright.sync_api import Route, sync_playwright

from homebody.robot_tools import robot_control_options

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "homebody" / "static"
ASSETS = ROOT / "docs" / "assets"

SYNTHETIC_STATUS = {
    "app": "homebody",
    "runtime": {
        "state": "standby",
        "detail": "Folded · torque released · local wake listening",
        "power_mode": "standby",
        "motors_enabled": False,
        "head_safely_folded": True,
        "transcript": "Camera and cloud voice are off until you choose a path.",
        "response_preview": "Ready for a guarded local demo.",
    },
    "config": {"camera_enabled": False},
    "kids_mode": {},
}


def _serve(route: Route) -> None:
    rest = route.request.url.split("://", 1)[1]
    path = rest.split("/", 1)[1].split("?", 1)[0] if "/" in rest else ""
    if path in ("", "index.html"):
        route.fulfill(path=str(STATIC / "index.html"), content_type="text/html")
    elif path == "manifest.webmanifest":
        route.fulfill(path=str(STATIC / path), content_type="application/manifest+json")
    elif path.startswith("static/") and (STATIC / path[len("static/") :]).is_file():
        file = STATIC / path[len("static/") :]
        route.fulfill(path=str(file), content_type=mimetypes.guess_type(file.name)[0] or "application/octet-stream")
    elif path == "api/status":
        route.fulfill(content_type="application/json", body=json.dumps(SYNTHETIC_STATUS))
    elif path == "api/robot/options":
        route.fulfill(content_type="application/json", body=json.dumps(robot_control_options()))
    else:
        route.fulfill(status=404, content_type="application/json", body="{}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--chromium", help="Chromium executable, if Playwright's own is not installed")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chromium)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
        context.route("**/*", _serve)
        page = context.new_page()
        page.goto("http://homebody.invalid/#dashboard")
        page.wait_for_function("document.body.dataset.runtimeState === 'standby'")
        page.wait_for_timeout(500)
        captures = {"ui-dashboard": Path(tmp) / "dashboard.png"}
        page.screenshot(path=str(captures["ui-dashboard"]))
        page.click("#tab-robot")
        page.wait_for_timeout(500)
        captures["ui-robot"] = Path(tmp) / "robot.png"
        page.screenshot(path=str(captures["ui-robot"]))
        browser.close()
        for name, png in captures.items():
            # Re-encoding through Pillow writes no EXIF or XMP metadata.
            Image.open(png).convert("RGB").save(ASSETS / f"{name}.webp", "WEBP", quality=86, method=6)
            print(f"wrote docs/assets/{name}.webp")


if __name__ == "__main__":
    main()
