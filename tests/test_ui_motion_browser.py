# ruff: noqa: F811 -- imported pytest fixtures are intentionally used as test parameters
"""Real Chromium: the app-wide motion language on Home, Workspace, Camera, Updates and buttons.

Every API reply is an explicit SIMULATED fixture; nothing contacts Reachy, Hermes, a camera, the
updater or a provider. Screenshots land in .artifacts/ui-motion/ (untracked) for visual review.
"""

import json
import time
from pathlib import Path

import pytest
from test_assistant_modes_browser import ui  # noqa: F401
from test_workspace_browser import workspace_browser  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".artifacts" / "ui-motion"
STORAGE = "JSON.stringify([Object.keys(localStorage).sort(), Object.keys(sessionStorage).sort()])"
HIT_TEST = """selector => {
    const el = document.querySelector(selector); const r = el.getBoundingClientRect();
    const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return {ok: top === el || el.contains(top), width: r.width, height: r.height, right: r.right};
}"""
HIDE = """() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
    document.dispatchEvent(new Event('visibilitychange')); }"""
SHOW = """() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
    document.dispatchEvent(new Event('visibilitychange')); }"""


def _shot(page, name, **kwargs):
    # Headless Chromium renders no frame until one is requested; let entrances finish first so a
    # still shows the composed state rather than frame zero of a fade.
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
    page.wait_for_timeout(300)
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), **kwargs)


def _scene(page, host):
    return page.evaluate(
        """host => { const s = document.querySelector(`#${host} .hb-scene`);
        return {scene: s.dataset.scene, phase: s.dataset.phase, motion: s.dataset.motion,
                reduced: s.dataset.reduced, title: s.querySelector('.hb-scene-title').textContent,
                text: s.textContent + ' ' + s.querySelector('.hb-scene-canvas').getAttribute('aria-label')}; }""",
        host,
    )


def _running(page, selector):
    return page.evaluate(
        """selector => document.getAnimations().filter(a => a.playState === 'running'
        && a instanceof CSSAnimation && a.effect && a.effect.target && a.effect.target.closest
        && a.effect.target.closest(selector)).length""",
        selector,
    )


def _apply(page, state, runtime_state="waiting_for_wake_word", power="awake", **runtime):
    state["runtime"].update(state=runtime_state, power_mode=power, kids_mode={}, **runtime)
    page.evaluate("s => updateStatus({config:s.config,runtime:s.runtime})", state)


def _owner_loss(page):
    page.route("**/api/agent/activity", lambda route: route.fulfill(status=401, json={}))
    page.evaluate("fetch('/api/agent/activity')")


# ---------- Showcase ----------


@pytest.mark.parametrize("width", [320, 390, 1280])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_showcase_renders_every_adult_state_offline(width, scheme):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900}, color_scheme=scheme)
        errors, requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request.url))
        page.goto((ROOT / "docs" / "scenes" / "ui-motion-showcase.html").as_uri())
        page.wait_for_timeout(600)
        assert "SIMULATED STATES" in page.inner_text(".simulated")
        counts = page.evaluate("""() => Object.fromEntries(['presence', 'workbench', 'lens', 'updater']
            .map(s => [s, document.querySelectorAll(`svg.hb-art-${s}`).length]))""")
        assert counts == {"presence": 10, "workbench": 12, "lens": 9, "updater": 13}
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        for surface in ("presence", "workbench", "lens", "updater", "buttons"):
            section = page.locator(f'[data-surface="{surface}"]')
            section.scroll_into_view_if_needed()
            page.wait_for_timeout(150)
            _shot(page, f"showcase-{surface}-{width}-{scheme}", clip=section.bounding_box())
        assert all(url.startswith("file://") for url in requests)
        assert not errors
        browser.close()


# ---------- Home: presence ----------


def test_home_presence_is_the_one_hero_figure_and_follows_categorical_state(ui):
    page, state, posts, errors = ui
    hero = page.evaluate("""() => ({figures: document.querySelectorAll('.status-card .hb-scene').length,
        legacy: document.querySelectorAll('.hero-face > svg, .hero-ring').length,
        focusable: document.querySelectorAll('#hero-presence [tabindex], #hero-presence button').length,
        hidden: document.querySelector('#hero-presence').getAttribute('aria-hidden')})""")
    assert hero == {"figures": 1, "legacy": 0, "focusable": 0, "hidden": "true"}
    for runtime_state, power, phase in [
        ("waiting_for_wake_word", "awake", "waiting"),
        ("waiting_for_wake_word", "standby", "standby"),
        ("listening", "awake", "listening"),
        ("transcribing", "awake", "thinking"),
        ("looking", "awake", "thinking"),
        ("speaking", "awake", "speaking"),
        ("meeting", "meeting", "paused"),
        ("sleep", "sleep", "asleep"),
        ("error", "awake", "trouble"),
        ("starting", "awake", "idle"),
        ("some_future_state", "unknown", "idle"),
    ]:
        _apply(page, state, runtime_state, power)
        assert _scene(page, "hero-presence")["phase"] == phase, (runtime_state, power)
    # Kids active elsewhere: neutral and locked, never "listening" for the adult hero.
    state["runtime"].update(
        state="listening", kids_mode={"active": True, "locked": False, "profile": {"activity": "quiz"}}
    )
    page.evaluate("s => updateStatus({config:s.config,runtime:s.runtime})", state)
    assert _scene(page, "hero-presence")["phase"] == "paused"
    # Private text that leaks into status is never rendered by the figure.
    _apply(page, state, "speaking", transcript="secret question", response_preview="private reply")
    assert not any(secret in _scene(page, "hero-presence")["text"] for secret in ("secret question", "private reply"))
    before = page.evaluate("presenceStage.snapshot().renders")
    for _ in range(5):
        _apply(page, state, "speaking", transcript="secret question", response_preview="private reply")
    assert page.evaluate("presenceStage.snapshot().renders") == before
    assert posts == [] and not errors


def test_home_presence_pauses_when_hidden_and_drops_to_offline(ui):
    page, state, posts, errors = ui
    _apply(page, state, "speaking")
    page.wait_for_timeout(250)
    assert _scene(page, "hero-presence")["motion"] == "on" and _running(page, "#hero-presence") > 0
    page.click("#tab-settings")
    page.wait_for_timeout(250)
    assert _scene(page, "hero-presence")["motion"] == "off" and _running(page, "#hero-presence") == 0
    page.click("#tab-dashboard")
    page.wait_for_timeout(250)
    assert _scene(page, "hero-presence")["motion"] == "on"
    page.evaluate(HIDE)
    assert _scene(page, "hero-presence")["motion"] == "off" and _running(page, "#hero-presence") == 0
    page.evaluate(SHOW)
    page.wait_for_timeout(300)
    # Unreachable status: neutral, motionless offline still.
    page.route("**/api/status", lambda route: route.fulfill(status=503, json={}))
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.querySelector('#hero-presence .hb-scene').dataset.phase === 'offline'")
    assert _scene(page, "hero-presence")["motion"] == "off"
    page.unroute("**/api/status")
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.querySelector('#hero-presence .hb-scene').dataset.phase === 'speaking'")
    # Owner loss wins over an already-admitted status result.
    _owner_loss(page)
    page.wait_for_function("document.querySelector('#hero-presence .hb-scene').dataset.phase === 'offline'")
    _apply(page, state, "listening")
    assert _scene(page, "hero-presence")["phase"] == "offline"
    assert page.evaluate("[...document.querySelectorAll('.hb-scene')].every(s => s.dataset.motion === 'off')")
    assert not errors


@pytest.mark.parametrize("width", [320, 390, 1280])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_home_hero_fits_and_power_controls_stay_hit_testable(ui, width, scheme):
    page, state, posts, errors = ui
    page.emulate_media(color_scheme=scheme)
    page.set_viewport_size({"width": width, "height": 900})
    _apply(page, state, "listening")
    page.wait_for_timeout(700)
    geometry = page.evaluate("""() => {
        const c = document.querySelector('#hero-presence .hb-scene-canvas').getBoundingClientRect();
        const h = document.querySelector('.status-card').getBoundingClientRect();
        return {left: c.left, right: c.right, w: c.width, hl: h.left, hr: h.right,
                doc: document.documentElement.scrollWidth}; }""")
    assert geometry["left"] >= geometry["hl"] and geometry["right"] <= geometry["hr"]
    assert geometry["doc"] <= width and geometry["w"] >= 140
    for selector in ('.power-actions [data-power="standby"]', '.power-actions [data-power="sleep"]'):
        page.locator(selector).focus()
        assert page.evaluate(HIT_TEST, selector)["ok"], selector
    page.evaluate("window.scrollTo(0, 0)")
    _shot(page, f"home-hero-listening-{width}-{scheme}", clip=page.locator(".status-card").bounding_box())
    assert not errors


# ---------- Workspace: bench ----------

BENCH = "document.querySelector('#workspace-native-scene .hb-scene').dataset.phase"


def _native_fixture(page, runtime):
    runtime["agent"] = {"profile": "agent", "activity": [], "pending_approval": False}
    fixture = {
        "targets": [
            {
                "target_id": "photo",
                "backend": "hermes",
                "title": "Photo project",
                "session_id": "native-secret-session",
            }
        ],
        "binding": None,
        "messages": [],
        "events": [],
        "pending": None,
        "run": {},
    }
    held = {"actions": set(), "routes": []}

    def native(route):
        action = route.request.url.rsplit("/", 1)[-1]
        body = json.loads(route.request.post_data or "{}")
        if action in held["actions"]:
            held["routes"].append(route)
            return
        response = fixture
        if action == "bind":
            fixture["binding"] = {"backend": "hermes", "project_id": "photo", "session_id": "native-secret-session"}
        elif action == "prepare":
            fixture["pending"] = {
                "approval_id": "exact-once",
                "request": body["text"],
                "session_id": "native-secret-session",
            }
            response = fixture["pending"]
        elif action == "approve":
            fixture.update(
                pending=None,
                run={"status": "running", "run_id": "run-secret", "ready_to_test": False, "receipt": None},
            )
            response = fixture["run"]
        route.fulfill(json=response)

    page.route("**/api/agent/workspace/native/*", native)
    return fixture, held


def _open_native(page):
    page.click("#agent-view-workspace")
    page.wait_for_function("!document.getElementById('workspace-start').disabled")
    page.click("#workspace-start")
    page.wait_for_function("!document.getElementById('workspace-native-bind').disabled")


def _bench_shot(page, name):
    page.locator("#workspace-native-scene").scroll_into_view_if_needed()
    _shot(page, name, clip=page.locator(".workspace-native").bounding_box())


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_workspace_bench_follows_native_state_without_inventing_progress(workspace_browser, width):
    page, _display, runtime, errors = workspace_browser
    page.set_viewport_size({"width": width, "height": 900})
    fixture, held = _native_fixture(page, runtime)
    page.wait_for_function(f"{BENCH} === 'idle'")
    _open_native(page)
    assert _scene(page, "workspace-native-scene")["title"] == "No session selected"
    # Button feedback is tied to the real request: pending while held, confirmed when accepted.
    held["actions"].add("bind")
    page.click("#workspace-native-bind")
    page.wait_for_function("document.getElementById('workspace-native-bind').dataset.hbMotion === 'pending'")
    height = page.evaluate("document.getElementById('workspace-native-bind').getBoundingClientRect().height")
    held["actions"].clear()
    fixture["binding"] = {"backend": "hermes", "project_id": "photo", "session_id": "native-secret-session"}
    held["routes"].pop().fulfill(json=fixture)
    page.wait_for_function("document.getElementById('workspace-native-bind').dataset.hbMotion === 'confirmed'")
    assert page.evaluate("document.getElementById('workspace-native-bind').getBoundingClientRect().height") == height
    page.wait_for_function(f"{BENCH} === 'discussing'")
    page.wait_for_function("!document.getElementById('workspace-native-bind').dataset.hbMotion", timeout=3000)
    page.fill("#workspace-native-request", "Implement the private contract")
    page.click("#workspace-native-prepare")
    page.wait_for_function(f"{BENCH} === 'approval'")
    # Bounded attention: three swells on the gate, then a steady still.
    gate = (
        "getComputedStyle(document.querySelector('#workspace-native-scene .hb-wb-gate path')).animationIterationCount"
    )
    assert page.evaluate(gate) == "3"
    _bench_shot(page, f"workspace-approval-{width}")
    page.click("#workspace-native-approve")
    page.wait_for_function(f"{BENCH} === 'working'")
    page.locator("#workspace-native-scene").scroll_into_view_if_needed()
    page.wait_for_function("document.querySelector('#workspace-native-scene .hb-scene').dataset.motion === 'on'")
    # Hidden (another view), the loop pauses; it is decoration, not a progress signal.
    page.click("#agent-view-companion")
    page.wait_for_function("document.querySelector('#workspace-native-scene .hb-scene').dataset.motion === 'off'")
    assert _running(page, "#workspace-native-scene .hb-art") == 0
    page.click("#agent-view-workspace")
    page.wait_for_function(f"{BENCH} === 'working'")
    _bench_shot(page, f"workspace-running-{width}")
    # "completed" text returned is not success; only the exact receipt check is "Ready to test".
    fixture["run"].update(status="completed", ready_to_test=True)
    page.evaluate("HomebodyNativeWorkspace.update(true)")
    page.wait_for_function(f"{BENCH} === 'finished'")
    assert _scene(page, "workspace-native-scene")["motion"] == "off"
    fixture["run"]["receipt"] = {
        "verified": True,
        "native_run_id": "run-secret",
        "native_session_id": "native-secret-session",
    }
    page.evaluate("HomebodyNativeWorkspace.update(true)")
    page.wait_for_function(f"{BENCH} === 'ready'")
    _bench_shot(page, f"workspace-ready-{width}")
    fixture["run"]["receipt"]["native_run_id"] = "someone-else"
    page.evaluate("HomebodyNativeWorkspace.update(true)")
    page.wait_for_function(f"{BENCH} === 'finished'")
    text = _scene(page, "workspace-native-scene")["text"]
    assert not any(
        secret in text for secret in ("native-secret-session", "run-secret", "private contract", "Photo project")
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    persisted = page.evaluate(STORAGE)
    assert "native" not in persisted and "private" not in persisted
    assert not errors


def test_workspace_bench_late_reads_lost_connection_and_privacy_win(workspace_browser):
    page, _display, runtime, errors = workspace_browser
    fixture, held = _native_fixture(page, runtime)
    _open_native(page)
    # A read admitted before reset cannot repaint a running bench afterwards.
    fixture.update(
        binding={"backend": "hermes", "project_id": "photo", "session_id": "s"},
        run={"status": "running", "run_id": "r"},
    )
    held["actions"].add("read")
    page.evaluate("HomebodyNativeWorkspace.update(true)")
    page.wait_for_timeout(150)
    assert held["routes"]
    page.evaluate("HomebodyNativeWorkspace.reset()")
    held["routes"].pop().fulfill(json=fixture)
    page.wait_for_timeout(150)
    assert page.evaluate(BENCH) in {"idle", "offline"}
    held["actions"].clear()
    # Connection lost: "unconfirmed", never "stopped" or "done", and Stop stays available.
    page.unroute("**/api/agent/workspace/native/*")
    page.route("**/api/agent/workspace/native/*", lambda route: route.fulfill(status=502, json={"detail": "down"}))
    page.evaluate("HomebodyNativeWorkspace.update(true)")
    page.wait_for_function(f"{BENCH} === 'uncertain'")
    assert page.locator("#workspace-native-stop").is_enabled()
    _bench_shot(page, "workspace-uncertain-390")
    # Kids or privacy mode clears the display; the bench goes neutral and shows nothing private.
    runtime["kids_mode"] = {"active": True, "locked": True}
    page.evaluate("refreshStatus()")
    page.wait_for_function(f"{BENCH} === 'idle'")
    assert _scene(page, "workspace-native-scene")["motion"] == "off"
    runtime["kids_mode"] = {}
    # Owner loss: the bench reads "hidden" even if a later update arrives.
    _owner_loss(page)
    page.wait_for_function(f"{BENCH} === 'offline'")
    page.evaluate("HomebodyNativeWorkspace.update(true)")
    assert page.evaluate(BENCH) == "offline"
    assert not errors


# ---------- Camera: lens ----------

FAKE_CAMERA = """() => {
    const canvas = document.createElement('canvas'); canvas.width = 640; canvas.height = 360;
    const ctx = canvas.getContext('2d');
    window.pauseVideoFrames = false;
    window.syntheticFrames = setInterval(() => {
        if (window.pauseVideoFrames) return;
        ctx.fillStyle = `hsl(${Date.now() % 360}, 35%, 30%)`; ctx.fillRect(0, 0, 640, 360);
    }, 60);
    window.GstWebRTCAPI = class {
        registerConnectionListener() {} unregisterConnectionListener() {} unregisterProducersListener() {}
        registerProducersListener(listener) { listener.producerAdded({ id: 'sim', meta: { name: 'reachymini' } }); }
        createConsumerSession() {
            const session = new EventTarget();
            session.streams = [canvas.captureStream()];
            session.connect = () => session.dispatchEvent(new Event('streamsChanged'));
            session.close = () => {};
            return session;
        }
    };
}"""
LENS = "document.querySelector('#camera-lens .hb-scene').dataset.phase"


def _camera_page(ui, width=390):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 844})
    camera_requests = []
    page.on("request", lambda request: camera_requests.append(request.url) if "/api/camera" in request.url else None)
    state["config"].update(camera_feed_enabled=True, camera_controls_enabled=True)
    _apply(page, state, "waiting_for_wake_word", "awake", motors_enabled=True)
    page.evaluate(FAKE_CAMERA)
    page.click("#tab-robot")
    return page, state, errors, camera_requests


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_camera_lens_claims_live_only_for_fresh_decoded_video(ui, width):
    page, state, errors, camera_requests = _camera_page(ui, width)
    assert page.evaluate(LENS) == "ready"
    page.click("#camera-live-start")
    assert page.evaluate(LENS) in {"connecting", "live"}
    page.wait_for_function(f"{LENS} === 'live'", timeout=10000)
    assert page.locator("#camera-live-status").text_content() == "Live"
    assert page.evaluate("document.getElementById('camera-viewer').dataset.lens") == "live"
    # The lens sits beside the badge: never over the video, joystick, Exit or Stop.
    overlap = page.evaluate("""() => { const a = document.querySelector('#camera-lens').getBoundingClientRect();
        const b = document.querySelector('#camera-viewer').getBoundingClientRect();
        return !(a.right <= b.left || a.left >= b.right || a.bottom <= b.top || a.top >= b.bottom); }""")
    assert not overlap
    for selector in ("#camera-control-stop", "#camera-live-stop"):
        page.locator(selector).scroll_into_view_if_needed()
        assert page.evaluate(HIT_TEST, selector)["ok"], selector
    page.locator(".camera-card .section-heading").scroll_into_view_if_needed()
    _shot(page, f"camera-live-{width}", clip=page.locator(".camera-card").bounding_box())
    # Frames stop arriving: delayed/held, not live.
    page.evaluate("window.pauseVideoFrames = true")
    page.wait_for_function(f"{LENS} === 'held'", timeout=5000)
    page.evaluate("window.pauseVideoFrames = false")
    page.wait_for_function(f"{LENS} === 'live'", timeout=5000)
    page.click("#camera-live-stop")
    # Stopped: "off", or "ready" once the next status poll re-applies an allowed policy. Never live.
    assert page.evaluate(LENS) in {"off", "ready"}
    assert page.evaluate("document.getElementById('reachy-camera-video').srcObject") is None
    # Privacy mode: closed, slashed lens; the scene requests nothing of its own.
    _apply(page, state, "meeting", "meeting", motors_enabled=False)
    assert page.evaluate(LENS) == "privacy"
    assert all("/api/camera/connectivity" in url for url in camera_requests)
    assert not errors


def test_camera_lens_fullscreen_hidden_reduced_and_owner_loss(ui):
    page, state, errors, camera_requests = _camera_page(ui, 390)
    page.evaluate(
        "() => { document.getElementById('camera-viewer').requestFullscreen = "
        "async () => { throw new Error('unsupported'); }; }"
    )
    page.click("#camera-live-start")
    page.wait_for_function(f"{LENS} === 'live'", timeout=10000)
    page.click("#camera-live-fullscreen")
    page.wait_for_function("document.body.classList.contains('camera-app-fullscreen-active')")
    # The fallback fullscreen viewer carries no decoration and keeps its own controls on top.
    assert page.evaluate("getComputedStyle(document.getElementById('camera-viewer')).boxShadow") == "none"
    for selector in ("#camera-control-fullscreen-exit", "#camera-control-stop", "#camera-joystick"):
        assert page.evaluate(HIT_TEST, selector)["ok"], selector
    _shot(page, "camera-fullscreen-fallback-390")
    page.click("#camera-control-fullscreen-exit")
    page.wait_for_function("!document.body.classList.contains('camera-app-fullscreen-active')")
    # Reduced motion: the lens keeps a clear static open aperture without looping.
    page.emulate_media(reduced_motion="reduce")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    assert _scene(page, "camera-lens")["reduced"] == "true" and _running(page, "#camera-lens") == 0
    assert page.evaluate("getComputedStyle(document.querySelector('#camera-lens .hb-ln-iris path')).opacity") == "0"
    page.emulate_media(reduced_motion="no-preference")
    # Backgrounding stops the camera (existing privacy rule); the lens follows and stops moving.
    page.evaluate(HIDE)
    assert page.evaluate(LENS) in {"ready", "off"}
    assert _scene(page, "camera-lens")["motion"] == "off"
    page.evaluate(SHOW)
    page.click("#camera-live-start")
    page.wait_for_function(f"{LENS} === 'live'", timeout=10000)
    # Owner loss stops the camera and the lens stays "offline" for any later camera callback.
    _owner_loss(page)
    page.wait_for_function(f"{LENS} === 'offline'")
    page.evaluate("ReachyCamera.setPolicy({enabled: true, powerMode: 'awake', motorsEnabled: true})")
    assert page.evaluate(LENS) == "offline"
    assert page.evaluate("document.getElementById('reachy-camera-video').srcObject") is None
    assert not errors


# ---------- Updates: rail ----------

RAIL = "document.querySelector('#update-rail .hb-scene').dataset.phase"


def _updates_page(ui, width=390):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 844})
    catalog = {
        "current": {"version": "0.5.6"},
        "supported": True,
        "checked_at": time.time(),
        "available": None,
        "history": [],
        "job": None,
        "notes": [],
    }
    control = {"status": 200, "hold": False, "held": []}

    def updates(route):
        if control["hold"]:
            control["held"].append(route)
        elif control["status"] != 200:
            route.fulfill(status=control["status"], body="Restarting")
        else:
            route.fulfill(json=json.loads(json.dumps(catalog)))

    page.route("**/api/updates", updates)
    page.click("#tab-settings")
    return page, catalog, control, errors


def _refresh(page):
    page.evaluate("HomebodyUpdates.refresh()")


def test_update_rail_follows_durable_job_states_only(ui):
    page, catalog, control, errors = _updates_page(ui)
    _refresh(page)
    page.wait_for_function(f"{RAIL} === 'idle'")
    assert _scene(page, "update-rail")["title"] == "Up to date"
    page.evaluate(
        "() => { window.scrollTo(0, document.body.scrollHeight); document.getElementById('update-check').focus(); }"
    )
    page.wait_for_timeout(150)
    hit = page.evaluate(HIT_TEST, "#update-check")
    assert hit["ok"] and hit["height"] >= 48
    catalog["available"] = {"version": "0.5.7", "release_id": 7}
    _refresh(page)
    page.wait_for_function(f"{RAIL} === 'available'")
    expected = {
        "queued": "queued",
        "downloading": "downloading",
        "installing": "installing",
        "stopping": "restarting",
        "starting": "restarting",
        "verifying": "verifying",
        "rolling_back": "rolling_back",
    }
    for job_state, phase in expected.items():
        catalog["job"] = {"id": "a" * 32, "state": job_state, "acknowledged": False}
        _refresh(page)
        page.wait_for_function(f"{RAIL} === '{phase}'")
        if job_state == "installing":
            page.locator("#updates-card").scroll_into_view_if_needed()
            _shot(page, "updates-installing-390", clip=page.locator("#updates-card").bounding_box())
    # No timer advances the rail: without a new durable answer it stays put.
    catalog["job"]["state"] = "verifying"
    _refresh(page)
    page.wait_for_function(f"{RAIL} === 'verifying'")
    page.wait_for_timeout(1200)
    assert page.evaluate(RAIL) == "verifying"
    # The app restarting mid-update reads as "reconnecting", never as success.
    control["status"] = 502
    _refresh(page)
    page.wait_for_function(f"{RAIL} === 'unreachable'")
    assert "not confirmed" in _scene(page, "update-rail")["text"]
    control["status"] = 200
    for terminal, phase in [("rolled_back", "rolled_back"), ("rollback_failed", "failed"), ("failed", "failed")]:
        catalog["job"] = {"id": "b" * 32, "state": terminal, "acknowledged": False, "error": "x"}
        _refresh(page)
        page.wait_for_function(f"{RAIL} === '{phase}'")
    _shot(page, "updates-failed-390", clip=page.locator("#updates-card").bounding_box())
    assert not errors


@pytest.mark.parametrize("width", [320, 390, 1280])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_update_success_needs_the_journal_and_dialog_stays_readable(ui, width, scheme):
    page, catalog, control, errors = _updates_page(ui, width)
    page.emulate_media(color_scheme=scheme)
    catalog["current"] = {"version": "0.5.7"}
    job = {
        "id": "c" * 32,
        "state": "succeeded",
        "acknowledged": False,
        "from_version": "0.5.6",
        "manifest": {"version": "0.5.7"},
        "notes": [{"version": "0.5.7", "notes": "Motion"}],
    }
    catalog.update(job=job, history=[job])
    _refresh(page)
    page.wait_for_function(f"{RAIL} === 'succeeded'")
    page.wait_for_function("document.getElementById('update-dialog').open")
    page.wait_for_timeout(400)
    for selector in ("#update-ack", "#update-close"):
        hit = page.evaluate(HIT_TEST, selector)
        assert hit["ok"] and hit["right"] <= width, selector
        if width < 650:
            assert hit["height"] >= 48, selector
    assert "Android app (APK): unchanged" in page.locator("#update-scope").inner_text()
    _shot(page, f"updates-success-dialog-{width}-{scheme}")
    page.click("#update-close")
    page.locator("#updates-card").scroll_into_view_if_needed()
    _shot(page, f"updates-succeeded-card-{width}-{scheme}", clip=page.locator("#updates-card").bounding_box())
    # Acknowledged success is history, not a fresh "installed" claim.
    job["acknowledged"] = True
    catalog.update(job=job, history=[job])
    _refresh(page)
    page.wait_for_function(f"{RAIL} === 'idle'")
    assert not errors


def test_update_rail_ignores_late_reads_and_owner_loss_wins(ui):
    page, catalog, control, errors = _updates_page(ui)
    catalog["job"] = {"id": "d" * 32, "state": "installing", "acknowledged": False}
    control["hold"] = True
    _refresh(page)  # older read, held in flight
    page.wait_for_timeout(100)
    control["hold"] = False
    catalog["job"] = {"id": "d" * 32, "state": "verifying", "acknowledged": False}
    _refresh(page)
    page.wait_for_function(f"{RAIL} === 'verifying'")
    stale = {**catalog, "job": {"id": "d" * 32, "state": "downloading", "acknowledged": False}}
    control["held"].pop().fulfill(json=stale)
    page.wait_for_timeout(200)
    assert page.evaluate(RAIL) == "verifying"
    _owner_loss(page)
    page.wait_for_function(f"{RAIL} === 'offline'")
    _refresh(page)
    page.wait_for_timeout(200)
    assert page.evaluate(RAIL) == "offline"
    assert not errors


# ---------- Shared motion primitives ----------


def test_motion_feedback_is_token_bound_and_reset_releases_everything(ui):
    page, state, posts, errors = ui
    storage = page.evaluate(STORAGE)
    result = page.evaluate("""async () => {
        const b = document.getElementById('update-check');
        const first = HomebodyMotion.pending(b);
        const second = HomebodyMotion.pending(b);
        const staleSettled = HomebodyMotion.settle(first, 'confirmed');
        const afterStale = b.dataset.hbMotion;
        const ok = HomebodyMotion.settle(second, 'confirmed');
        const confirmed = b.dataset.hbMotion;
        await new Promise(r => setTimeout(r, 1600));
        const cleared = b.dataset.hbMotion || '';
        const third = HomebodyMotion.pending(b);
        HomebodyMotion.reset();
        return {staleSettled, afterStale, ok, confirmed, cleared, afterReset: b.dataset.hbMotion || '',
                late: HomebodyMotion.settle(third, 'confirmed'), active: HomebodyMotion.activeCount()};
    }""")
    assert result == {
        "staleSettled": False,
        "afterStale": "pending",
        "ok": True,
        "confirmed": "confirmed",
        "cleared": "",
        "afterReset": "",
        "late": False,
        "active": 0,
    }
    assert page.evaluate(STORAGE) == storage
    assert posts == [] and not errors


def test_disclosure_entrances_are_opt_in_and_skip_reduced_motion(ui):
    page, state, posts, errors = ui
    page.click("#tab-settings")
    assert 3 <= page.evaluate("document.querySelectorAll('details[data-hb-disclosure]').length") <= 8
    page.locator("#updates-card details summary").click()
    page.wait_for_function("document.getElementById('update-history').classList.contains('hb-enter')")
    page.wait_for_timeout(400)
    assert not page.evaluate("document.getElementById('update-history').classList.contains('hb-enter')")
    # Not opted in: no entrance class is ever added.
    page.locator("#global-power-controls summary").click()
    assert page.evaluate("document.querySelectorAll('#global-power-controls .hb-enter').length") == 0
    page.emulate_media(reduced_motion="reduce")
    page.locator("#updates-card details summary").click()
    page.locator("#updates-card details summary").click()
    assert not page.evaluate("document.getElementById('update-history').classList.contains('hb-enter')")
    assert page.evaluate("HomebodyMotion.enter(document.getElementById('updates-card'))") is False
    assert not errors


REDUCED_VIEWS = """() => {
    const out = {};
    const host = document.createElement('div'); document.body.appendChild(host);
    const rail = HomebodyScene.mount(host, {variant: 'rail', label: 'reduced'});
    rail.update(HomebodyScene.updaterView({job: 'installing'}));
    out.fill = getComputedStyle(host.querySelector('.hb-up-fill')).strokeDashoffset;
    out.rail = rail.snapshot().motion; rail.destroy();
    const host2 = document.createElement('div'); document.body.appendChild(host2);
    const bench = HomebodyScene.mount(host2, {variant: 'strip', label: 'reduced'});
    bench.update(HomebodyScene.workbenchView({showing: true, pending: true}));
    out.gate = getComputedStyle(host2.querySelector('.hb-wb-gate')).opacity;
    out.bench = bench.snapshot().motion; bench.destroy();
    return out;
}"""


def test_reduced_motion_keeps_static_meaning_on_every_adult_surface(ui):
    page, state, posts, errors = ui
    page.emulate_media(reduced_motion="reduce")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    _apply(page, state, "listening")
    page.wait_for_timeout(300)
    hero = _scene(page, "hero-presence")
    assert hero["phase"] == "listening" and hero["reduced"] == "true" and hero["motion"] == "off"
    assert page.evaluate("getComputedStyle(document.querySelector('#hero-presence .hb-fx-listening')).opacity") == "1"
    assert page.evaluate(REDUCED_VIEWS) == {"fill": "153.6px", "rail": "off", "gate": "1", "bench": "off"}
    assert _running(page, ".hb-scene") == 0
    assert not errors


@pytest.mark.parametrize("width", [320, 390])
def test_scrolled_controls_clear_sticky_bars_and_keep_48px_targets(workspace_browser, width):
    page, _display, runtime, errors = workspace_browser
    page.set_viewport_size({"width": width, "height": 700})
    _native_fixture(page, runtime)
    _open_native(page)
    for selector in ("#workspace-start", "#workspace-native-stop", "#workspace-native-prepare"):
        height = page.evaluate("s => document.querySelector(s).getBoundingClientRect().height", selector)
        assert height >= 48, (selector, height)
    # Keyboard focus from far below lands each enabled control clear of the sticky top bar and the
    # fixed phone tab bar.
    for selector in ("#workspace-clear", "#workspace-native-bind", "#agent-view-companion", "#agent-view-workspace"):
        page.evaluate(
            "s => { window.scrollTo(0, document.body.scrollHeight); document.querySelector(s).focus(); }", selector
        )
        page.wait_for_timeout(150)
        assert page.evaluate("s => document.activeElement === document.querySelector(s)", selector), selector
        hit = page.evaluate(HIT_TEST, selector)
        assert hit["ok"], selector
        assert hit["height"] >= 48, (selector, hit["height"])
        assert hit["right"] <= width
    assert not errors


CYCLE = """async () => {
    const base = HomebodyScene.liveCount();
    const host = document.createElement('div'); document.body.appendChild(host);
    const views = [HomebodyScene.presenceView({state: 'speaking'}),
                   HomebodyScene.workbenchView({showing: true, status: 'running'}),
                   HomebodyScene.lensView({phase: 'live'}), HomebodyScene.updaterView({job: 'installing'})];
    for (let i = 0; i < 24; i++) {
        const s = HomebodyScene.mount(host, {variant: ['figure', 'strip', 'glyph', 'rail'][i % 4], label: 'cycle'});
        s.update(views[i % 4]); s.update(views[(i + 1) % 4]);
        s.destroy(); s.destroy();
        if (s.update(views[0]) !== false) return {error: 'updated after destroy'};
    }
    await new Promise(r => setTimeout(r, 700));
    return {base, after: HomebodyScene.liveCount(), nodes: host.childElementCount};
}"""


def test_adult_scene_mount_destroy_cycles_release_everything(ui):
    page, state, posts, errors = ui
    before = page.evaluate(STORAGE)
    result = page.evaluate(CYCLE)
    assert result["after"] == result["base"] and result["nodes"] == 0
    assert page.evaluate(STORAGE) == before
    assert posts == [] and not errors
