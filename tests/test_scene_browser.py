# ruff: noqa: F811 -- imported pytest fixture is intentionally used as a test parameter
"""Real Chromium: the shared scene component on the Kids stage and the Everyday companion.

Every API reply is an explicit fixture; nothing contacts Reachy, Hermes or a provider. Screenshots
land in .artifacts/kids-motion/ (untracked) for visual review.
"""

import json
from pathlib import Path

import pytest
from test_assistant_modes_browser import ui  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".artifacts" / "kids-motion"
ACTIVITIES = ["buddy", "story", "quiz", "riddles", "calm", "ispy"]
TOKEN = "b" * 32


def _shot(page, name, **kwargs):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), **kwargs)


def _kids(activity="buddy", **extra):
    return {"active": True, "locked": True, "remaining_seconds": 1500,
            "profile": {"activity": activity, "age_band": "7-9", "duration_minutes": 30}, **extra}


def _apply(page, state, runtime_state="waiting_for_wake_word", kids=None, power="awake"):
    state["runtime"]["state"] = runtime_state
    state["runtime"]["power_mode"] = power
    state["runtime"]["kids_mode"] = kids if kids is not None else {}
    page.evaluate("s => updateStatus({config:s.config,runtime:s.runtime})", state)


def _stage(page):
    return page.evaluate("""() => {
        const root = document.querySelector('#kids-stage .hb-scene');
        return {scene: root.dataset.scene, phase: root.dataset.phase, motion: root.dataset.motion,
                reduced: root.dataset.reduced, title: root.querySelector('.hb-scene-title').textContent,
                chip: root.querySelector('.hb-scene-chip').textContent,
                arts: root.querySelectorAll('svg.hb-art').length};
    }""")


def _running_scene_animations(page, selector=".hb-scene"):
    # Loops only: finite CSS transitions between phase poses are allowed to finish.
    return page.evaluate("""selector => document.getAnimations().filter(a => a.playState === 'running'
        && a instanceof CSSAnimation && a.effect && a.effect.target && a.effect.target.closest
        && a.effect.target.closest(selector)).length""", selector)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_showcase_renders_every_scene_and_phase(scheme):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900}, color_scheme=scheme)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        page.goto((ROOT / "docs" / "scenes" / "showcase.html").as_uri())
        page.wait_for_timeout(700)
        assert "SIMULATED STATES" in page.inner_text(".simulated")
        for activity in ACTIVITIES:
            row = page.locator(f'[data-activity="{activity}"]')
            assert row.locator(f"svg.hb-art-{activity}").count() >= 8
            row.scroll_into_view_if_needed()
            page.wait_for_timeout(250)
            _shot(page, f"showcase-{activity}-{scheme}", clip=row.bounding_box())
        page.locator("#companions").scroll_into_view_if_needed()
        _shot(page, f"showcase-companion-{scheme}", clip=page.locator("#companions").bounding_box())
        # Only local files: the component never initiates a request of its own.
        assert all(url.startswith("file://") for url in requests)
        assert not errors
        browser.close()


@pytest.mark.parametrize("width", [320, 390, 1280])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_kids_stage_switches_scenes_and_fits(ui, width, scheme):
    page, state, posts, errors = ui
    page.emulate_media(color_scheme=scheme)
    page.set_viewport_size({"width": width, "height": 900})
    page.click("#tab-kids")
    signatures = set()
    for activity in ACTIVITIES:
        page.click(f'[data-kids-activity="{activity}"]')
        page.wait_for_timeout(600)
        stage = _stage(page)
        assert stage["scene"] == activity and stage["phase"] == "preview" and stage["chip"] == "Preview"
        assert stage["arts"] == 1  # the previous scene has finished leaving
        # Preview is a composed still: no loop runs before a session starts.
        assert stage["motion"] == "off" and _running_scene_animations(page, "#kids-stage .hb-art *") == 0
        signatures.add(page.evaluate("document.querySelector('#kids-stage svg.hb-art').innerHTML.length"))
    assert len(signatures) == len(ACTIVITIES)
    geometry = page.evaluate("""() => {
        const r = document.querySelector('#kids-stage .hb-scene-canvas').getBoundingClientRect();
        return {left: r.left, right: r.right, width: r.width, doc: document.documentElement.scrollWidth};}""")
    assert geometry["left"] >= 0 and geometry["right"] <= width and geometry["doc"] <= width
    # Fable's compact phone stage intentionally leaves room for play controls.
    assert geometry["width"] >= min(200, width - 64)
    _apply(page, state, "listening", _kids("story"))
    page.wait_for_timeout(700)
    page.locator(".kids-hero").scroll_into_view_if_needed()
    _shot(page, f"kids-stage-story-listening-{width}-{scheme}", clip=page.locator(".kids-hero").bounding_box())
    if width == 1280:
        _shot(page, f"kids-panel-{width}-{scheme}", full_page=False)
    assert posts == [] and not errors


def test_phases_follow_categorical_status_and_ignore_repeats(ui):
    page, state, posts, errors = ui
    page.click("#tab-kids")
    expected = [
        ("waiting_for_wake_word", "waiting"),
        ("listening", "listening"),
        ("transcribing", "thinking"),
        ("thinking", "thinking"),
        ("synthesizing", "thinking"),
        ("speaking", "speaking"),
        ("announcing", "speaking"),
        ("starting", "waiting"),
        ("some_future_state", "waiting"),
        ("error", "trouble"),
    ]
    for runtime_state, phase in expected:
        _apply(page, state, runtime_state, _kids("quiz"))
        assert _stage(page)["phase"] == phase, runtime_state
    assert _stage(page)["title"] == "Something went wrong"
    _apply(page, state, "listening", _kids("quiz"), power="meeting")
    assert _stage(page)["phase"] == "paused"
    # camera_active outside I Spy never claims a search; inside I Spy it is the only search signal.
    _apply(page, state, "speaking", _kids("quiz", camera_active=True))
    assert _stage(page)["phase"] == "speaking"
    _apply(page, state, "speaking", _kids("ispy", camera_active=True))
    stage = _stage(page)
    assert stage["scene"] == "ispy" and stage["phase"] == "searching" and stage["chip"] == "Camera on"
    _apply(page, state, "speaking", _kids("ispy", camera_active=False))
    assert _stage(page)["phase"] == "speaking"
    # Identical polls do not re-render, swap art or replay the phase cue.
    before = page.evaluate("kidsStage.snapshot().renders")
    for _ in range(5):
        _apply(page, state, "speaking", _kids("ispy"))
    assert page.evaluate("kidsStage.snapshot().renders") == before
    page.wait_for_timeout(600)
    assert page.evaluate("document.querySelectorAll('#kids-stage svg.hb-art').length") == 1
    # The stage text never echoes a transcript, reply, nickname or guess even if one leaked into status.
    leaky = _kids("ispy", nickname="Sam", transcript="blue cup", response_preview="It is the lamp")
    _apply(page, state, "speaking", leaky)
    text = page.inner_text("#kids-stage")
    assert not any(secret in text for secret in ("Sam", "blue cup", "lamp"))
    assert posts == [] and not errors


def test_motion_runs_only_when_live_visible_and_not_reduced(ui):
    page, state, posts, errors = ui
    page.click("#tab-kids")
    # Unlocked fixture so the adult tabs stay reachable for the visibility check.
    _apply(page, state, "speaking", _kids("buddy", locked=False))
    page.wait_for_timeout(300)
    assert _stage(page)["motion"] == "on"
    assert _running_scene_animations(page, "#kids-stage") > 0
    # Another tab hides the panel: no animation remains running for the stage.
    page.click("#tab-dashboard")
    page.wait_for_timeout(300)
    assert _stage(page)["motion"] == "off"
    assert _running_scene_animations(page, "#kids-stage") == 0
    page.click("#tab-kids")
    page.wait_for_timeout(300)
    assert _stage(page)["motion"] == "on"
    # Page hidden (backgrounded phone) pauses too, and resumes on return.
    page.evaluate("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
        document.dispatchEvent(new Event('visibilitychange')); }""")
    assert _stage(page)["motion"] == "off"
    assert _running_scene_animations(page, "#kids-stage") == 0
    page.evaluate("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
        document.dispatchEvent(new Event('visibilitychange')); }""")
    assert _stage(page)["motion"] == "on"
    page.wait_for_timeout(400)  # let the poll that visibility resumes settle first
    # Ending the session leaves a still.
    _apply(page, state, "waiting_for_wake_word", {"active": False, "locked": False, "last_end_reason": "parent_stop"})
    stage = _stage(page)
    assert stage["phase"] == "ended" and stage["motion"] == "off"
    page.wait_for_timeout(200)
    assert _running_scene_animations(page, "#kids-stage .hb-art *") == 0
    assert not errors


def test_reduced_motion_keeps_meaningful_static_states(ui):
    page, state, posts, errors = ui
    page.emulate_media(reduced_motion="reduce")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.click("#tab-kids")
    for runtime_state, kids, phase, visible_layer in [
        ("listening", _kids("calm"), "listening", ".hb-fx-listening"),
        ("thinking", _kids("riddles"), "thinking", ".hb-fx-thinking"),
        ("speaking", _kids("story"), "speaking", ".hb-fx-speaking"),
    ]:
        _apply(page, state, runtime_state, kids)
        stage = _stage(page)
        assert stage["phase"] == phase and stage["reduced"] == "true" and stage["motion"] == "off"
        opacity = page.evaluate(f"getComputedStyle(document.querySelector('#kids-stage {visible_layer}')).opacity")
        assert opacity == "1"
        assert _running_scene_animations(page, "#kids-stage") == 0
        assert stage["title"]
    _apply(page, state, "speaking", _kids("ispy", camera_active=True))
    assert _stage(page)["phase"] == "searching"
    assert page.evaluate("getComputedStyle(document.querySelector('#kids-stage .hb-iris path')).opacity") == "0"
    page.set_viewport_size({"width": 1280, "height": 900})
    page.locator(".kids-hero").scroll_into_view_if_needed()
    _shot(page, "kids-stage-ispy-searching-reduced", clip=page.locator("#kids-stage").bounding_box())
    _apply(page, state, "speaking", _kids("ispy", camera_active=False))
    assert page.evaluate("getComputedStyle(document.querySelector('#kids-stage .hb-iris path')).opacity") == "1"
    assert not errors


HIT_TEST = """selector => {
    const el = document.querySelector(selector); const r = el.getBoundingClientRect();
    const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return {ok: top === el || el.contains(top), right: r.right};
}"""


@pytest.mark.parametrize("width", [320, 390])
def test_end_and_typed_guess_stay_hit_testable_beside_the_stage(ui, width):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 760})
    page.route("**/api/kids/ispy/input", lambda r: r.fulfill(json={"available": True, "token": TOKEN}))
    sent = []

    def guess(route):
        sent.append(json.loads(route.request.post_data))
        route.fulfill(json={"accepted": True})

    page.route("**/api/kids/ispy/guess", guess)
    _apply(page, state, "listening", _kids("ispy"))
    page.click("#tab-kids")
    page.wait_for_function("!document.querySelector('#kids-ispy-guess').disabled")
    for selector in ("#kids-stop-button", "#kids-ispy-guess", "#kids-ispy-guess-send"):
        page.locator(selector).scroll_into_view_if_needed()
        hit = page.evaluate(HIT_TEST, selector)
        assert hit["ok"], selector
        assert hit["right"] <= width
    # While a session runs, the disabled single-activity Start steps aside with its picker.
    assert page.locator("#kids-start-button").is_hidden()
    assert not page.evaluate("document.querySelector('#kids-stop-button').disabled")
    page.fill("#kids-ispy-guess", "red ball")
    page.press("#kids-ispy-guess", "Enter")
    page.wait_for_function("document.querySelector('#kids-ispy-guess-message').textContent.includes('Guess checked')")
    assert sent == [{"text": "red ball", "token": TOKEN}]
    # The stage is never focusable and never captures keyboard focus.
    focusable = "#kids-stage [tabindex], #kids-stage a, #kids-stage button, #kids-stage input"
    assert page.evaluate("s => document.querySelectorAll(s).length", focusable) == 0
    page.locator("#kids-ispy-guess-form").scroll_into_view_if_needed()
    _shot(page, f"kids-ispy-guess-with-stage-{width}")
    assert not errors


def _phase_is(page, phase):
    page.wait_for_function("p => document.querySelector('#kids-stage .hb-scene').dataset.phase === p", arg=phase)


def test_end_fences_late_polls_and_offline_and_owner_loss_reset(ui):
    page, state, posts, errors = ui
    page.click("#tab-kids")
    _apply(page, state, "speaking", _kids("riddles"))
    held = []
    page.route("**/api/kids/stop", lambda route: held.append(route))
    page.click("#kids-stop-button")
    _phase_is(page, "ending")
    # A poll that still reports the old session cannot bring it back while End is in flight...
    _apply(page, state, "listening", _kids("riddles"))
    assert _stage(page)["phase"] == "ending"
    state["runtime"]["kids_mode"] = {"active": False, "locked": False, "last_end_reason": "parent_stop"}
    page.route("**/api/status", lambda r: r.fulfill(json={"config": state["config"], "runtime": state["runtime"]}))
    held[0].fulfill(json={"ok": True})
    _phase_is(page, "ended")
    # ...or after End returned, for any status request that started before then.
    _apply(page, state, "speaking", _kids("riddles"))
    assert _stage(page)["phase"] in {"ending", "speaking"}
    _apply(page, state, "waiting_for_wake_word", {"active": False, "locked": False, "last_end_reason": "parent_stop"})
    stage = _stage(page)
    assert stage["phase"] == "ended" and stage["motion"] == "off"
    page.unroute("**/api/status")
    # A fresh, later status poll is believed again (for example a new caregiver start).
    state["runtime"]["kids_mode"] = _kids("calm")
    state["runtime"]["state"] = "listening"
    page.evaluate("refreshStatus()")
    _phase_is(page, "listening")
    # Offline: neutral still, no claims about listening.
    page.route("**/api/status", lambda route: route.fulfill(status=503, json={}))
    page.evaluate("refreshStatus()")
    _phase_is(page, "offline")
    assert _stage(page)["motion"] == "off"
    assert page.evaluate("document.querySelector('#everyday-companion .hb-scene').dataset.phase") == "offline"
    page.unroute("**/api/status")
    page.evaluate("refreshStatus()")
    _phase_is(page, "listening")
    # Owner loss (a 401 on any API call) drops every scene to a motionless neutral state.
    page.route("**/api/agent/activity", lambda route: route.fulfill(status=401, json={}))
    page.evaluate("fetch('/api/agent/activity')")
    _phase_is(page, "offline")
    assert page.evaluate("[...document.querySelectorAll('.hb-scene')].every(s => s.dataset.motion === 'off')")
    # A status result admitted before owner loss must not resurrect live animation.
    _apply(page, state, "speaking", _kids("buddy"))
    assert _stage(page)["phase"] == "offline"
    assert page.evaluate("document.querySelector('#everyday-companion .hb-scene').dataset.phase") == "offline"
    assert not errors


def test_late_status_started_before_end_cannot_revive_the_scene(ui):
    page, state, posts, errors = ui
    page.click("#tab-kids")
    _apply(page, state, "speaking", _kids("story"))
    held_status, held_stop = [], []
    page.route("**/api/status", lambda route: held_status.append(route))
    page.route("**/api/kids/stop", lambda route: held_stop.append(route))
    page.evaluate("() => { window.lateStatus = refreshStatus(); }")  # in flight before End
    page.wait_for_function("() => true")
    page.click("#kids-stop-button")
    _phase_is(page, "ending")
    held_stop[0].fulfill(json={"ok": True})
    page.wait_for_timeout(150)
    # The older poll now answers with the session that has just ended.
    held_status[0].fulfill(json={"config": state["config"], "runtime": {**state["runtime"], "state": "listening"}})
    page.evaluate("window.lateStatus")
    page.wait_for_timeout(150)
    assert _stage(page)["phase"] == "ending"
    assert not errors


def test_everyday_companion_reuses_the_component(ui):
    page, state, posts, errors = ui
    page.click("#tab-dashboard")  # the companion lives in Everyday
    for runtime_state, power, phase, title in [
        ("waiting_for_wake_word", "awake", "waiting", "Waiting"),
        ("listening", "awake", "listening", "Listening"),
        ("thinking", "awake", "thinking", "Thinking"),
        ("looking", "awake", "thinking", "Taking a look"),
        ("speaking", "awake", "speaking", "Speaking"),
        ("meeting", "meeting", "paused", "In a meeting"),
    ]:
        _apply(page, state, runtime_state, {}, power=power)
        result = page.evaluate("""() => { const s = document.querySelector('#everyday-companion .hb-scene');
            return {variant: s.dataset.variant, scene: s.dataset.scene, phase: s.dataset.phase,
                    title: s.querySelector('.hb-scene-title').textContent}; }""")
        assert result == {"variant": "compact", "scene": "companion", "phase": phase, "title": title}
    _apply(page, state, "speaking", {})
    page.wait_for_timeout(500)
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        page.locator("#everyday-card").scroll_into_view_if_needed()
        _shot(page, f"everyday-companion-{scheme}", clip=page.locator("#everyday-card").bounding_box())
    # Same component, one implementation: every mount, Kids and adult, comes from HomebodyScene.
    mounts = page.evaluate("""() => [...document.querySelectorAll('.hb-scene')]
        .map(s => `${s.parentElement.id}:${s.dataset.variant}:${s.dataset.scene}`).sort()""")
    assert mounts == sorted([
        "kids-stage:stage:host", "everyday-companion:compact:companion", "hero-presence:figure:presence",
        "camera-lens:glyph:lens", "workspace-native-scene:strip:workbench", "update-rail:rail:updater",
    ])
    assert page.evaluate("HomebodyScene.liveCount()") == len(mounts)
    assert not errors


CYCLE = """async () => {
    const base = HomebodyScene.liveCount();
    const host = document.createElement('div'); document.body.appendChild(host);
    const pick = i => HomebodyScene.ACTIVITIES[i % 6];
    for (let i = 0; i < 25; i++) {
        const s = HomebodyScene.mount(host, {label: 'cycle'});
        s.update(HomebodyScene.kidsView({kids: {active: true, profile: {activity: pick(i)}}, state: 'speaking'}));
        s.update(HomebodyScene.kidsView({kids: {active: true, profile: {activity: pick(i + 1)}}, state: 'listening'}));
        s.destroy(); s.destroy();
        if (s.update({scene: 'buddy', phase: 'speaking'}) !== false) return {error: 'updated after destroy'};
    }
    await new Promise(r => setTimeout(r, 700));
    return {base, after: HomebodyScene.liveCount(), nodes: host.childElementCount};
}"""
STORAGE = "JSON.stringify([Object.keys(localStorage).sort(), Object.keys(sessionStorage).sort()])"


def test_mount_destroy_cycles_release_everything_and_touch_no_storage(ui):
    page, state, posts, errors = ui
    storage_before = page.evaluate(STORAGE)
    result = page.evaluate(CYCLE)
    assert result["after"] == result["base"] and result["nodes"] == 0
    assert page.evaluate(STORAGE) == storage_before
    assert posts == [] and not errors
