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
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "EventSource",
        "sendBeacon",
        "getUserMedia",
        "mediaDevices",
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "document.cookie",
        "speechSynthesis",
        "new Audio",
        "AudioContext",
        "requestAnimationFrame",
        "setInterval",
        "http://",
        "https://",
        "import(",
    ):
        assert forbidden not in source, forbidden
    css = (STATIC / "scene.css").read_text(encoding="utf-8")
    assert "url(http" not in css and "@import" not in css
    assert "prefers-reduced-motion" in css
    assert '[data-motion="off"]' in css


def test_motion_primitive_cannot_initiate_requests_storage_media_or_observers() -> None:
    source = (STATIC / "motion.js").read_text(encoding="utf-8")
    for forbidden in (
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "EventSource",
        "sendBeacon",
        "getUserMedia",
        "mediaDevices",
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "document.cookie",
        "MutationObserver",
        "ResizeObserver",
        "IntersectionObserver",
        "requestAnimationFrame",
        "setInterval",
        "http://",
        "https://",
        "import(",
        "innerHTML",
    ):
        assert forbidden not in source, forbidden
    css = (STATIC / "scene.css").read_text(encoding="utf-8")
    reduced = css.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert ".hb-enter" in reduced and "button[data-hb-motion]" in reduced


def test_scene_assets_ship_with_the_page_service_worker_and_package() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    worker = (STATIC / "service-worker.js").read_text(encoding="utf-8")
    version = re.search(r'homebody-shell-v(\d+)"', worker).group(1)
    for asset in ("scene.css", "scene.js", "motion.js"):
        assert f"/static/{asset}?v={version}" in html
        assert f'"/static/{asset}?v={version}"' in worker
    # scene.js and motion.js load before the scripts that mount or call them.
    assert html.index("/static/scene.js") < html.index("/static/motion.js") < html.index("/static/owner.js")
    assert html.index("/static/owner.js") < html.index("/static/camera.js") < html.index("/static/main.js")
    for host in (
        "kids-stage",
        "everyday-companion",
        "hero-presence",
        "camera-lens",
        "workspace-native-scene",
        "update-rail",
    ):
        assert f'id="{host}"' in html
    assert "kids-mascot" not in html and "hero-ring" not in html and "mark-eye" not in html
    assert '"static/*"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_one_component_backs_every_mount_and_owner_loss_resets_it() -> None:
    main = (STATIC / "main.js").read_text(encoding="utf-8")
    assert main.count("window.HomebodyScene?.mount(") == 3
    assert 'mount($("kids-stage")' in main and 'mount($("everyday-companion"), { variant: "compact"' in main
    assert 'mount($("hero-presence"), { variant: "figure"' in main
    for name, host in (
        ("camera.js", "camera-lens"),
        ("native-workspace.js", "workspace-native-scene"),
        ("updates.js", "update-rail"),
    ):
        source = (STATIC / name).read_text(encoding="utf-8")
        assert source.count("window.HomebodyScene?.mount(") == 1, name
        assert host in source, name
    owner = (STATIC / "owner.js").read_text(encoding="utf-8")
    assert "HomebodyScene?.resetAll()" in owner and "HomebodyMotion?.reset()" in owner
    # Scene bindings add no storage keys.
    stage_block = main.split("// The Kids stage, the Everyday companion and the Home hero", 1)[1].split(
        "renderKidsStage();", 1
    )[0]
    assert "Storage" not in stage_block and "fetch(" not in stage_block


def test_built_wheel_carries_the_current_scene_assets() -> None:
    import zipfile

    wheels = sorted((ROOT / "dist").glob("reachy_mini_homebody-*.whl"))
    if not wheels:
        pytest.skip("no wheel has been built in dist/")
    with zipfile.ZipFile(wheels[-1]) as wheel:
        names = set(wheel.namelist())
        for asset in (
            "scene.js",
            "scene.css",
            "motion.js",
            "index.html",
            "service-worker.js",
            "main.js",
            "owner.js",
            "camera.js",
            "updates.js",
            "workspace.js",
            "native-workspace.js",
            "style.css",
        ):
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
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
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
        ("story", "preview"),
        ("buddy", "preview"),
        ("calm", "ended"),
        ("quiz", "offline"),
        ("quiz", "ending"),
        ("riddles", "listening"),
        ("riddles", "thinking"),
        ("riddles", "thinking"),
        ("riddles", "speaking"),
        ("riddles", "waiting"),
        ("riddles", "waiting"),
        ("riddles", "paused"),
        ("ispy", "searching"),
        ("buddy", "speaking"),
        ("calm", "speaking"),
    ]
    assert all(set(view) == {"scene", "phase", "chip", "title", "detail"} for view in views)
    # Only the live search claims the camera; nothing else mentions it being on.
    assert [view["chip"] for view in views].count("Camera on") == 1
    assert views[10]["title"] == "Waiting"  # unrecognised state: neutral, never "Listening"


def test_adapters_never_echo_private_or_unbounded_fields() -> None:
    secret = {
        "nickname": "Sam",
        "transcript": "blue cup",
        "response_preview": "It is the lamp",
        "target": "lamp",
        "session_id": "kids-abc",
        "last_error": "Traceback secret",
    }
    views = _run(
        [
            [
                "kidsView",
                {"kids": {**_kids("ispy"), **secret, "profile": {"activity": "ispy", **secret}}, "state": "speaking"},
            ],
            ["kidsView", {"kids": {"active": False, "last_end_reason": "Traceback secret"}, "selected": "buddy"}],
            ["companionView", {"state": "error", "detail": "Traceback secret"}],
        ]
    )
    text = json.dumps(views)
    for value in set(secret.values()):
        assert value not in text


def test_companion_adapter_states() -> None:
    views = _run(
        [
            ["companionView", {"state": "listening", "powerMode": "awake"}],
            ["companionView", {"state": "looking", "powerMode": "awake"}],
            ["companionView", {"state": "speaking", "powerMode": "awake"}],
            ["companionView", {"state": "waiting_for_wake_word", "powerMode": "standby"}],
            ["companionView", {"state": "meeting", "powerMode": "meeting"}],
            ["companionView", {"state": "listening", "kidsActive": True}],
            ["companionView", {"offline": True}],
            ["companionView", {"state": "connecting_realtime"}],
        ]
    )
    assert [view["phase"] for view in views] == [
        "listening",
        "thinking",
        "speaking",
        "waiting",
        "paused",
        "paused",
        "offline",
        "waiting",
    ]
    assert all(view["scene"] == "companion" for view in views)


def test_presence_adapter_distinguishes_awake_standby_sleep_and_stays_neutral() -> None:
    cases = [
        {"state": "waiting_for_wake_word", "powerMode": "awake"},
        {"state": "waiting_for_wake_word", "powerMode": "standby"},
        {"state": "listening", "powerMode": "standby"},
        {"state": "synthesizing", "powerMode": "awake"},
        {"state": "looking", "powerMode": "awake"},
        {"state": "announcing", "powerMode": "awake"},
        {"state": "listening", "powerMode": "meeting"},
        {"state": "listening", "powerMode": "sleep"},
        {"state": "configuration_error", "powerMode": "awake"},
        {"state": "listening", "kidsActive": True},
        {"state": "speaking", "offline": True},
        {"state": "starting", "powerMode": "awake"},
        {},
    ]
    views = _run([["presenceView", case] for case in cases])
    assert [view["phase"] for view in views] == [
        "waiting",
        "standby",
        "listening",
        "thinking",
        "thinking",
        "speaking",
        "paused",
        "asleep",
        "trouble",
        "paused",
        "offline",
        "idle",
        "idle",
    ]
    assert all(
        view["scene"] == "presence" and set(view) == {"scene", "phase", "chip", "title", "detail"} for view in views
    )


def test_workbench_adapter_never_claims_success_or_progress_it_was_not_given() -> None:
    cases = [
        {},
        {"showing": True},
        {"showing": True, "configured": True},
        {"showing": True, "bound": True},
        {"showing": True, "bound": True, "pending": True},
        {"showing": True, "status": "running"},
        {"showing": True, "status": "verifying"},
        {"showing": True, "status": "stopping"},
        {"showing": True, "status": "completed"},
        {"showing": True, "status": "completed", "verified": True},
        {"showing": True, "status": "submission_unknown"},
        {"showing": True, "status": "running", "uncertain": True},
        {"showing": True, "status": "failed"},
        {"showing": True, "status": "cancelled"},
        {"showing": True, "status": "brand_new_status"},
        {"showing": False, "status": "running"},
        {"offline": True, "showing": True, "status": "completed", "verified": True},
    ]
    views = _run([["workbenchView", case] for case in cases])
    assert [view["phase"] for view in views] == [
        "idle",
        "idle",
        "idle",
        "discussing",
        "approval",
        "working",
        "working",
        "stopping",
        "finished",
        "ready",
        "uncertain",
        "uncertain",
        "trouble",
        "stopped",
        "idle",
        "idle",
        "offline",
    ]
    assert [view["title"] for view in views].count("Ready to test") == 1
    text = json.dumps(views).lower()
    assert "%" not in text and "progress estimate" in text


def test_lens_adapter_only_accepts_known_camera_phases() -> None:
    phases = ["off", "ready", "connecting", "live", "held", "paused", "privacy", "trouble", "offline", "streaming", ""]
    views = _run(
        [["lensView", {"phase": phase}] for phase in phases] + [["lensView", {"phase": "live", "offline": True}]]
    )
    assert [view["phase"] for view in views] == [
        "off",
        "ready",
        "connecting",
        "live",
        "held",
        "paused",
        "privacy",
        "trouble",
        "offline",
        "off",
        "off",
        "offline",
    ]
    assert [view["title"] for view in views].count("Live") == 1


def test_updater_adapter_follows_the_journal_and_never_fabricates_success() -> None:
    cases = [
        {},
        {"checked": True},
        {"checked": True, "available": True},
        {"checking": True},
        {"supported": False, "checked": True},
        *(
            {"job": state}
            for state in ("queued", "downloading", "installing", "stopping", "starting", "verifying", "rolling_back")
        ),
        {"job": "succeeded"},
        {"job": "succeeded", "acknowledged": True, "checked": True},
        {"job": "rolled_back"},
        {"job": "failed"},
        {"job": "rollback_failed"},
        {"job": "verifying", "unreachable": True},
        {"job": "succeeded", "offline": True},
        {"job": "unknown_future_state", "checked": True},
    ]
    views = _run([["updaterView", case] for case in cases])
    assert [view["phase"] for view in views] == [
        "idle",
        "idle",
        "available",
        "checking",
        "idle",
        "queued",
        "downloading",
        "installing",
        "restarting",
        "restarting",
        "verifying",
        "rolling_back",
        "succeeded",
        "idle",
        "rolled_back",
        "failed",
        "failed",
        "unreachable",
        "offline",
        "idle",
    ]
    assert [view["chip"] for view in views[5:11]] == [f"Step {n} of 5" for n in (1, 2, 3, 4, 4, 5)]
    assert [view["phase"] for view in views].count("succeeded") == 1
    assert "%" not in json.dumps(views)


def test_adult_adapters_ignore_private_fields() -> None:
    secret = {
        "transcript": "secret words",
        "response_preview": "private reply",
        "session_id": "native-xyz",
        "run_id": "run-xyz",
        "text": "do the private thing",
        "error": "Traceback secret",
        "nickname": "Sam",
    }
    views = _run(
        [
            ["presenceView", {"state": "speaking", **secret}],
            ["workbenchView", {"showing": True, "status": "running", **secret}],
            ["lensView", {"phase": "live", **secret}],
            ["updaterView", {"job": "failed", **secret}],
        ]
    )
    text = json.dumps(views)
    for value in secret.values():
        assert value not in text
