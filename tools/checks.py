"""Focused development checks and a separate complete release gate.

Selection is conservative, not a coverage proof. Unknown/shared changes run all unit tests.
Browser tests run in bounded batches; every batch must finish successfully. No xdist/shared
runtime concurrency and no test removal. Reports/JUnit are local untracked artifacts.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RISK_GROUPS = {
    "voice": (
        "kids",
        "voice",
        "live",
        "realtime",
        "runtime",
        "session",
        "announc",
        "owner",
        "presence",
        "speaker",
        "assistant_modes",
    ),
    "workspace": ("native", "workspace", "github", "agent", "owner"),
    "updates": ("update", "release", "bootstrap", "managed", "scene_assets"),
    "camera": ("camera", "ispy", "kids", "owner", "local_vision"),
    "ui": ("scene", "motion", "motor_ui", "announc", "presence", "assistant_modes", "owner"),
}


def test_files(root: Path, browser: bool) -> list[str]:
    return sorted(
        str(p.relative_to(root))
        for p in (root / "tests").glob("test_*.py")
        if p.name.endswith("_browser.py") == browser
    )


def select_changed(paths: list[str], root: Path = ROOT) -> tuple[list[str], list[str], str]:
    """Return affected unit/browser modules, or all units for any unknown dependency."""
    units, browsers = test_files(root, False), test_files(root, True)
    selected_units, selected_browser = set(), set()
    full = False
    for path in paths:
        if path.startswith((".artifacts/", "dist/")):
            continue
        if path.startswith("tests/test_") and path.endswith(".py"):
            if path in units:
                selected_units.add(path)
            elif path in browsers:
                selected_browser.add(path)
            continue
        if path.startswith("docs/") or path in {"README.md", "CHANGELOG.md"}:
            selected_units.update(p for p in units if any(k in p for k in ("docs", "publication", "roadmap")))
            continue
        name = Path(path).name
        if path in {"homebody/static/main.js", "homebody/static/index.html", "homebody/static/style.css"}:
            selected_browser.update(browsers)  # Shared UI can affect every panel and privacy boundary.
            selected_units.update(p for p in units if any(k in p for k in RISK_GROUPS["ui"]))
            continue
        if name in {"config.py", "__init__.py", "conftest.py", "pyproject.toml"} or path.startswith("tools/"):
            full = True
            continue
        group = None
        if any(k in name for k in ("voice", "audio", "speech", "kids", "announc", "runtime", "stt")):
            group = "voice"
        elif any(k in name for k in ("workspace", "native", "github", "agent")):
            group = "workspace"
        elif any(k in name for k in ("update", "release", "bootstrap")):
            group = "updates"
        elif any(k in name for k in ("camera", "vision", "ispy")):
            group = "camera"
        elif path.startswith("homebody/static/"):
            group = "ui"
        if group is None:
            full = True
        else:
            keys = RISK_GROUPS[group]
            selected_units.update(p for p in units if any(k in p for k in keys))
            selected_browser.update(p for p in browsers if any(k in p for k in keys))
    if full:
        return units, browsers, "unknown/shared change: complete coverage"
    if not selected_units and not selected_browser:
        return units, [], "no affected module proven: all unit tests"
    return sorted(selected_units), sorted(selected_browser), "conservative affected-risk groups"


def changed_paths(base: str) -> list[str]:
    subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    tracked = subprocess.check_output(["git", "diff", "--name-only", base, "--"], cwd=ROOT, text=True)
    untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard"], cwd=ROOT, text=True)
    return sorted(set((tracked + untracked).splitlines()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=["changed", "unit", "browser", "release"])
    parser.add_argument("paths", nargs="*", help="Explicit test files or changed source paths")
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--browser-python", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--screenshots", action="store_true", help="Capture composed motion-review artifacts")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--batch-timeout", type=int, default=180)
    args = parser.parse_args()
    browser_python = args.browser_python or args.python
    units, browsers = test_files(ROOT, False), test_files(ROOT, True)
    reason = "complete gate"
    if args.profile == "changed":
        units, browsers, reason = select_changed(args.paths or changed_paths(args.base))
    elif args.profile == "unit":
        units = args.paths or units
        browsers = []
    elif args.profile == "browser":
        units = []
        browsers = args.paths or browsers
    for path in units + browsers:
        if not (ROOT / path).resolve().is_relative_to(ROOT) or not (ROOT / path).is_file():
            parser.error(f"not a repository test file: {path}")
    print(
        json.dumps(
            {"profile": args.profile, "reason": reason, "unit_modules": units, "browser_modules": browsers}, indent=2
        ),
        flush=True,
    )
    if args.dry_run:
        return 0
    if not 1 <= args.batch_size <= 16 or args.batch_timeout < 10:
        parser.error("batch size must be 1–16 and timeout at least 10 seconds")
    stamp = str(time.time_ns())
    output = ROOT / ".artifacts" / "test-runs" / stamp
    output.mkdir(parents=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix="homebody-tests-") as isolated:
        env = dict(os.environ)
        for key in list(env):
            if key.endswith(("_API_KEY", "_AUTH_TOKEN")) or key in {
                "HASS_TOKEN",
                "HASS_URL",
                "PYTHONPATH",
                "PYTHONHOME",
            }:
                env.pop(key, None)
        env["HERMES_HOME"] = isolated
        env["HOMEBODY_TEST_SCREENSHOTS"] = "1" if args.screenshots else "0"

        def run(command: list[str], name: str, timeout: int) -> bool:
            start = time.monotonic()
            child = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True)
            try:
                code = child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Stop the whole owned test group, including Playwright/Chromium descendants.
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
                code = 124
            rows.append({"stage": name, "seconds": round(time.monotonic() - start, 3), "exit_code": code})
            (output / "timings.json").write_text(json.dumps(rows, indent=2) + "\n")
            return code == 0

        if args.profile == "release":
            if not run([args.python, "-m", "build", "--wheel"], "build", 180):
                return 1
        if units:
            command = [
                args.python,
                "-m",
                "pytest",
                *units,
                "-o",
                "addopts=",
                "-q",
                "--durations=15",
                "--junitxml",
                str(output / "unit.xml"),
            ]
            if not run(command, "unit", 300):
                return 1
        for module in browsers:
            collected = subprocess.run(
                [browser_python, "-m", "pytest", module, "--collect-only", "-q", "-o", "addopts="],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            ids = [line.strip() for line in collected.stdout.splitlines() if line.startswith(module + "::")]
            if collected.returncode != 0 or not ids:
                print(collected.stdout + collected.stderr, flush=True)
                return 1  # Missing/failed browser collection is not a successful empty gate.
            for offset in range(0, len(ids), args.batch_size):
                name = f"{Path(module).stem}-{offset // args.batch_size}"
                command = [
                    browser_python,
                    "-m",
                    "pytest",
                    *ids[offset : offset + args.batch_size],
                    "-o",
                    "addopts=",
                    "-o",
                    "faulthandler_timeout=45",
                    "-q",
                    "--durations=5",
                    "--junitxml",
                    str(output / (name + ".xml")),
                ]
                if not run(command, name, args.batch_timeout):
                    return 1
                report = ET.parse(output / (name + ".xml"))
                if report.findall(".//testcase/skipped"):
                    rows[-1].update(exit_code=1, reason="browser gate cannot pass with skipped tests")
                    (output / "timings.json").write_text(json.dumps(rows, indent=2) + "\n")
                    print("Browser tests skipped: install/repair the browser environment; gate failed.", flush=True)
                    return 1
    print(f"Verified report: {output / 'timings.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
