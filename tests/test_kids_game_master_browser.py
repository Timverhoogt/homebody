# ruff: noqa: F811 -- imported fixture is intentionally a test parameter
"""Real Chromium, explicit status fixtures: the Play together stage on the integrated Kids page.

Nothing here contacts Reachy, Hermes or a provider; screenshots are fixture renders, not a demo.
"""

from pathlib import Path

import pytest
from test_assistant_modes_browser import ui  # noqa: F401

SHOTS = Path(__file__).resolve().parents[1] / ".artifacts" / "kids-game-master"


def hosted(phase: str, *, activity: str = "", notice: str = "", state: str = "") -> dict:
    state = state or ("listening" if phase == "playing" else "waiting_for_wake_word")
    return {
        "state": state, "power_mode": "awake", "stt_locality": "unknown", "motors_enabled": True,
        "head_safely_folded": False,
        "kids_mode": {
            "active": True, "locked": True, "remaining_seconds": 600, "turns_completed": 2,
            "profile": {"age_band": "7-9", "activity": activity or "buddy", "language": "en",
                        "duration_minutes": 15, "motion_enabled": True, "camera_active": False, "host": True},
            "last_end_reason": "", "last_fold_succeeded": None, "tool_policy": "voice-state-motion-only",
            "camera_enabled": False, "camera_active": False,
            "host": {"enabled": True, "phase": phase, "activity": activity,
                     "choices": ["again", "riddles"] if phase == "offer" else ["story", "riddles"],
                     "notice": notice, "available": ["story", "riddles", "quiz", "buddy", "calm"]},
        },
    }


def show(page, state, runtime) -> dict:
    page.set_default_timeout(4000)
    state["runtime"] = runtime
    page.evaluate("refreshStatus()")
    page.wait_for_timeout(50)
    page.evaluate("refreshStatus()")
    expected = {"lobby": "lobby", "transition": "switching", "offer": "offer", "waiting": "waiting"}.get(
        runtime["kids_mode"]["host"]["phase"], "listening")
    probe = """() => { const s = document.querySelector('#kids-stage .hb-scene');
      return s ? [s.dataset.scene, s.dataset.phase, s.querySelector('figcaption').innerText] : null; }"""
    for _ in range(40):
        current = page.evaluate(probe)
        if current and current[1] == expected:
            break
        page.wait_for_timeout(50)
    assert current and current[1] == expected, (expected, current)
    return page.evaluate("""() => {
      const scene = document.querySelector('#kids-stage .hb-scene');
      return {scene: scene.dataset.scene, phase: scene.dataset.phase, motion: scene.dataset.motion,
              caption: scene.querySelector('figcaption').innerText};
    }""")


def end_is_hit_testable(page) -> bool:
    return page.evaluate("""() => {
      const button = document.getElementById('kids-stop-button');
      button.scrollIntoView({block: 'center'});
      const box = button.getBoundingClientRect();
      const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
      return !button.disabled && (hit === button || button.contains(hit));
    }""")


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_play_together_stage_phases_and_controls(ui, width, scheme):
    page, state, posts, errors = ui
    page.emulate_media(color_scheme=scheme)
    page.set_viewport_size({"width": width, "height": 900})
    page.click("#tab-kids")
    # Idle: Play together is the primary start; direct activity Start remains available.
    assert page.locator("#kids-host-start-button").is_visible()
    assert page.locator("#kids-start-button").is_visible()
    SHOTS.mkdir(parents=True, exist_ok=True)

    seen = {}
    for phase, activity, notice in [
        ("lobby", "", ""), ("lobby", "", "camera_consent_needed"), ("transition", "story", ""),
        ("playing", "story", ""), ("offer", "story", ""),
    ]:
        view = show(page, state, hosted(phase, activity=activity, notice=notice))
        seen[(phase, notice)] = view
        assert end_is_hit_testable(page), phase
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        stage = page.locator("#kids-stage")
        stage.scroll_into_view_if_needed()
        stage.screenshot(path=str(SHOTS / f"{phase}{'-camera' if notice else ''}-{width}-{scheme}.png"))
    assert seen[("lobby", "")]["scene"] == "host" and seen[("lobby", "")]["motion"] == "on"
    assert "Choosing a game" in seen[("lobby", "")]["caption"]
    assert "grown-up" in seen[("lobby", "camera_consent_needed")]["caption"]
    assert seen[("transition", "")]["phase"] == "switching"
    assert seen[("playing", "")]["scene"] == "story"
    assert seen[("offer", "")]["phase"] == "offer" and "stop always finishes" in seen[("offer", "")]["caption"]
    # Captions never contain invented scores, targets or transcripts.
    for view in seen.values():
        assert "score" not in view["caption"].lower() and "points" not in view["caption"].lower()

    # Caregiver direct selection posts exactly the owner route; the child page renders but never fetches it.
    assert page.locator("#kids-host-controls").is_visible()
    assert page.locator("#kids-ispy-guess-form").is_visible()
    assert page.locator("#kids-ispy-guess-label").inner_text() == "Type to Reachy"
    page.click('[data-kids-host-activity="riddles"]')
    page.wait_for_function("document.getElementById('kids-host-message').textContent.length > 0")
    assert ("/api/kids/host/select", {"activity": "riddles", "camera_consent": False}) in posts
    assert not errors


def test_play_together_reduced_motion_hidden_page_and_end_cleanup(ui):
    page, state, posts, errors = ui
    page.click("#tab-kids")
    view = show(page, state, hosted("lobby"))
    assert view["motion"] == "on"
    # Background tab: loops pause, caption stays.
    page.evaluate("""() => { Object.defineProperty(document, 'hidden', {value: true, configurable: true});
                             document.dispatchEvent(new Event('visibilitychange')); }""")
    assert page.evaluate("document.querySelector('#kids-stage .hb-scene').dataset.motion") == "off"
    page.evaluate("""() => { Object.defineProperty(document, 'hidden', {value: false, configurable: true});
                             document.dispatchEvent(new Event('visibilitychange')); }""")
    page.emulate_media(reduced_motion="reduce")
    page.wait_for_function("document.querySelector('#kids-stage .hb-scene').dataset.reduced === 'true'")
    scene = page.evaluate("""() => { const s = document.querySelector('#kids-stage .hb-scene');
      return {motion: s.dataset.motion, caption: s.querySelector('figcaption').innerText}; }""")
    assert scene["motion"] == "off" and "Choosing a game" in scene["caption"]
    page.locator("#kids-stage").screenshot(path=str(SHOTS / "lobby-reduced-motion.png"))
    # Identical status polls do not touch the stage DOM.
    show(page, state, hosted("offer", activity="quiz"))
    page.wait_for_timeout(700)  # let the scene crossfade finish
    page.evaluate("""() => { window.__stageMutations = 0;
      new MutationObserver(list => { window.__stageMutations += list.length; })
        .observe(document.querySelector('#kids-stage'),
                 {subtree: true, childList: true, attributes: true, characterData: true}); }""")
    page.evaluate("refreshStatus()")
    page.wait_for_timeout(200)
    page.evaluate("refreshStatus()")
    page.wait_for_timeout(200)
    assert page.evaluate("window.__stageMutations") == 0
    # End: the stage leaves host phases and a stale active poll fenced by End cannot re-show it.
    page.route("**/api/kids/stop", lambda route: route.fulfill(json={"ok": True, "kids_mode": {"active": False}}))
    state["runtime"] = {"state": "waiting_for_wake_word", "power_mode": "standby",
                        "kids_mode": {"active": False, "locked": False, "last_end_reason": "parent"}}
    page.click("#kids-stop-button")
    page.wait_for_function("document.querySelector('#kids-stage .hb-scene').dataset.phase === 'ended'")
    assert page.locator("#kids-host-controls").is_hidden()
    assert not errors


def test_play_together_real_401_owner_loss_disables_host_controls(ui):
    page, state, posts, errors = ui
    page.click("#tab-kids")
    show(page, state, hosted("lobby"))
    assert page.locator("#kids-host-controls").is_visible()
    page.route(
        "**/api/status",
        lambda route: route.fulfill(status=401, json={"detail": "Pair this device as Owner first"}),
    )
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('kids-host-controls').hidden")
    assert page.evaluate("[...document.querySelectorAll('[data-kids-host-activity]')].every(b => b.disabled)")
    assert page.evaluate("document.querySelector('#kids-stage .hb-scene').dataset.phase") == "offline"
    assert not [path for path, _ in posts if path == "/api/kids/host/select"]


def test_held_active_poll_after_end_cannot_resurrect_stage_or_controls(ui):
    page, state, posts, errors = ui
    page.click("#tab-kids")
    show(page, state, hosted("lobby"))
    page.wait_for_timeout(300)
    page.wait_for_function("!document.getElementById('kids-stop-button').disabled")
    held = []
    page.route("**/api/status", lambda route: held.append(route))
    page.evaluate("() => { refreshStatus(); }")  # started before End; its reply is held (not awaited)
    for _ in range(40):
        if held:
            break
        page.wait_for_timeout(25)
    assert held
    disabled = page.evaluate("document.getElementById('kids-stop-button').disabled")
    assert not disabled, page.evaluate("document.getElementById('kids-message').textContent")
    page.route("**/api/kids/stop", lambda route: route.fulfill(json={"ok": True, "kids_mode": {"active": False}}))
    page.click("#kids-stop-button")
    page.wait_for_timeout(200)
    stale = {"config": state["config"], "runtime": hosted("offer", activity="story")}
    held[0].fulfill(json=stale)
    page.wait_for_timeout(300)
    assert page.locator("#kids-host-controls").is_hidden()
    assert page.evaluate("document.querySelector('#kids-stage .hb-scene').dataset.scene") != "host" or \
        page.evaluate("document.querySelector('#kids-stage .hb-scene').dataset.phase") in {"ending", "ended"}
    # Normal polling still works once the held request is gone.
    state["runtime"] = {"state": "waiting_for_wake_word", "power_mode": "standby",
                        "kids_mode": {"active": False, "locked": False, "last_end_reason": "parent"}}
    page.unroute("**/api/status")
    for route in held[1:]:  # later polls held by the test route now reach the normal fixture
        route.fallback()
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.querySelector('#kids-stage .hb-scene').dataset.phase === 'ended'")
    assert page.locator("#kids-host-controls").is_hidden()
    assert not errors


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_play_together_full_panel_screenshots(ui, width, scheme):
    page, state, posts, errors = ui
    page.emulate_media(color_scheme=scheme)
    page.set_viewport_size({"width": width, "height": 900})
    page.click("#tab-kids")
    for phase, activity in [("lobby", ""), ("transition", "story"), ("offer", "story")]:
        show(page, state, hosted(phase, activity=activity))
        page.wait_for_timeout(600)
        assert end_is_hit_testable(page)
        assert page.locator("#kids-ispy-guess-form").is_visible()
        page.locator("#panel-kids").screenshot(path=str(SHOTS / f"panel-{phase}-{width}-{scheme}.png"))
    assert not errors


def test_play_together_owner_loss_hides_host_controls(ui):
    page, state, posts, errors = ui
    page.click("#tab-kids")
    show(page, state, hosted("lobby"))
    assert page.locator("#kids-host-controls").is_visible()
    state["runtime"] = {"state": "waiting_for_wake_word", "power_mode": "standby", "kids_mode": {}}
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('kids-host-controls').hidden")
    assert not errors
