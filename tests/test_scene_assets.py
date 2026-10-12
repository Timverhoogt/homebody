"""Static and adapter checks for the shared scene component (homebody/static/scene.js).

The adapters run in node with a minimal window stub; no browser, network or device is involved.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "homebody" / "static"
SCENE = STATIC / "scene.js"


def test_scene_component_cannot_initiate_requests_storage_media_or_timers_loops() -> None:
    source = SCENE.read_text(encoding="utf-8")
    for forbidden in (
        "fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "getUserMedia",
        "mediaDevices", "localStorage", "sessionStorage", "indexedDB", "document.cookie",
        "speechSynthesis", "new Audio", "AudioContext", "requestAnimationFrame", "setInterval",
        "http://", "https://", "import(",
    ):
        assert forbidden not in source, forbidden
    css = (STATIC / "scene.css").read_text(encoding="utf-8")
    assert "url(http" not in css and "@import" not in css
    assert "prefers-reduced-motion" in css
    assert '[data-motion="off"]' in css


def test_scene_assets_ship_with_the_page_service_worker_and_package() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    worker = (STATIC / "service-worker.js").read_text(encoding="utf-8")
    version = re.search(r'homebody-shell-v(\d+)"', worker).group(1)
    for asset in ("scene.css", "scene.js"):
        assert f"/static/{asset}?v={version}" in html
        assert f'"/static/{asset}?v={version}"' in worker
    # scene.js loads before the scripts that mount it.
    assert html.index("/static/scene.js") < html.index("/static/owner.js") < html.index("/static/main.js")
    assert 'id="kids-stage"' in html and 'id="everyday-companion"' in html
    assert "kids-mascot" not in html
    assert '"static/*"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_one_component_is_mounted_exactly_twice_and_owner_loss_resets_it() -> None:
    main = (STATIC / "main.js").read_text(encoding="utf-8")
    assert main.count("window.HomebodyScene?.mount(") == 2
    assert 'mount($("kids-stage")' in main and 'mount($("everyday-companion"), { variant: "compact"' in main
    assert "HomebodyScene?.resetAll()" in (STATIC / "owner.js").read_text(encoding="utf-8")
    # Scene bindings add no storage keys.
    stage_block = main.split("// The Kids stage and the Everyday companion", 1)[1].split("renderKidsStage();", 1)[0]
    assert "Storage" not in stage_block and "fetch(" not in stage_block


def test_built_wheel_carries_the_current_scene_assets() -> None:
    import zipfile

    wheels = sorted((ROOT / "dist").glob("reachy_mini_homebody-*.whl"))
    if not wheels:
        pytest.skip("no wheel has been built in dist/")
    with zipfile.ZipFile(wheels[-1]) as wheel:
        names = set(wheel.namelist())
        for asset in ("scene.js", "scene.css", "index.html", "service-worker.js", "main.js", "owner.js"):
            assert f"homebody/static/{asset}" in names
            assert wheel.read(f"homebody/static/{asset}") == (STATIC / asset).read_bytes(), asset
        assert not any("showcase" in name for name in names)


HARNESS = r"""
const fs = require("fs"); const vm = require("vm");
const sandbox = { window: {}, document: {} };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), sandbox);
const S = sandbox.window.HomebodyScene;
const cases = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(cases.map(([fn, arg]) => S[fn](arg))));
"""


def _run(cases):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, "-e", HARNESS, str(SCENE), json.dumps(cases)],
        capture_output=True, text=True, timeout=20, check=True,
    )
    return json.loads(result.stdout)


def _kids(activity="buddy", **extra):
    return {"active": True, "profile": {"activity": activity}, **extra}


def test_kids_adapter_maps_only_categorical_status_to_phases() -> None:
    cases = [
        ["kidsView", {"selected": "story"}],
        ["kidsView", {"selected": "nonsense"}],
        ["kidsView", {"selected": "calm", "kids": {"active": False, "last_end_reason": "time_limit"}}],
        ["kidsView", {"selected": "quiz", "offline": True}],
        ["kidsView", {"selected": "quiz", "ending": True, "kids": _kids("quiz")}],
        ["kidsView", {"kids": _kids("riddles"), "state": "listening"}],
        ["kidsView", {"kids": _kids("riddles"), "state": "transcribing"}],
        ["kidsView", {"kids": _kids("riddles"), "state": "synthesizing"}],
        ["kidsView", {"kids": _kids("riddles"), "state": "announcing"}],
        ["kidsView", {"kids": _kids("riddles"), "state": "waiting_for_wake_word"}],
        ["kidsView", {"kids": _kids("riddles"), "state": "stopping"}],
        ["kidsView", {"kids": _kids("riddles"), "state": "listening", "powerMode": "sleep"}],
        ["kidsView", {"kids": _kids("ispy", camera_active=True), "state": "speaking"}],
        ["kidsView", {"kids": _kids("buddy", camera_active=True), "state": "speaking"}],
        ["kidsView", {"kids": _kids("unknown-activity"), "state": "speaking", "selected": "calm"}],
    ]
    views = _run(cases)
    phases = [(view["scene"], view["phase"]) for view in views]
    assert phases == [
        ("story", "preview"), ("buddy", "preview"), ("calm", "ended"), ("quiz", "offline"),
        ("quiz", "ending"), ("riddles", "listening"), ("riddles", "thinking"), ("riddles", "thinking"),
        ("riddles", "speaking"), ("riddles", "waiting"), ("riddles", "waiting"), ("riddles", "paused"),
        ("ispy", "searching"), ("buddy", "speaking"), ("calm", "speaking"),
    ]
    assert all(set(view) == {"scene", "phase", "chip", "title", "detail"} for view in views)
    # Only the live search claims the camera; nothing else mentions it being on.
    assert [view["chip"] for view in views].count("Camera on") == 1
    assert views[10]["title"] == "Waiting"  # unrecognised state: neutral, never "Listening"


def test_adapters_never_echo_private_or_unbounded_fields() -> None:
    secret = {"nickname": "Sam", "transcript": "blue cup", "response_preview": "It is the lamp",
              "target": "lamp", "session_id": "kids-abc", "last_error": "Traceback secret"}
    views = _run([
        ["kidsView", {"kids": {**_kids("ispy"), **secret, "profile": {"activity": "ispy", **secret}},
                      "state": "speaking"}],
        ["kidsView", {"kids": {"active": False, "last_end_reason": "Traceback secret"}, "selected": "buddy"}],
        ["companionView", {"state": "error", "detail": "Traceback secret"}],
    ])
    text = json.dumps(views)
    for value in set(secret.values()):
        assert value not in text


def test_companion_adapter_states() -> None:
    views = _run([
        ["companionView", {"state": "listening", "powerMode": "awake"}],
        ["companionView", {"state": "looking", "powerMode": "awake"}],
        ["companionView", {"state": "speaking", "powerMode": "awake"}],
        ["companionView", {"state": "waiting_for_wake_word", "powerMode": "standby"}],
        ["companionView", {"state": "meeting", "powerMode": "meeting"}],
        ["companionView", {"state": "listening", "kidsActive": True}],
        ["companionView", {"offline": True}],
        ["companionView", {"state": "connecting_realtime"}],
    ])
    assert [view["phase"] for view in views] == [
        "listening", "thinking", "speaking", "waiting", "paused", "paused", "offline", "waiting",
    ]
    assert all(view["scene"] == "companion" for view in views)
