# ruff: noqa: F811 -- imported fixture is intentionally a test parameter
"""Everyday structure on a phone: one primary action from the live state, small secondaries, power on
Robot, one Stop per context, header that never bisects the hero, a bottom row that never covers the last
control. Real Chromium with explicit fixtures; states are set through the page's own updateStatus.

Screenshots: HOMEBODY_TEST_SCREENSHOTS (default .artifacts/screenshots) / structure.
"""

import os
from pathlib import Path

import pytest
from test_assistant_modes_browser import ui  # noqa: F401

SHOTS = Path(os.getenv("HOMEBODY_TEST_SCREENSHOTS", ".artifacts/screenshots")) / "structure"
STATES = {
    "standby-empty": {"power_mode": "standby", "motors_enabled": False, "head_safely_folded": True,
                      "state": "waiting_for_wake_word"},
    "awake-populated": {"power_mode": "awake", "motors_enabled": True, "state": "waiting_for_wake_word",
                        "transcript": "How long is the oven on?", "response_preview": "Eleven minutes."},
    "listening": {"power_mode": "awake", "motors_enabled": True, "state": "listening"},
    "speaking": {"power_mode": "awake", "motors_enabled": True, "state": "speaking",
                 "transcript": "Set a timer", "response_preview": "Ten minutes, starting now."},
    "sleep": {"power_mode": "sleep", "motors_enabled": False, "head_safely_folded": True, "state": "idle"},
}
EXPECT = {  # primary label, forwarded control, Stop all work visible
    "standby-empty": ("Wake up", "global-power-awake"),
    "awake-populated": ("Standby", "global-power-standby"),
    "listening": ("Stop", "voice-stop-button"),
    "speaking": ("Stop speaking", "voice-stop-button"),
    "sleep": ("Wake up", "global-power-awake"),
}
BOX = ("s => { const r = document.querySelector(s).getBoundingClientRect();"
       " return [r.top, r.bottom, r.height, r.width]; }")


def _show(page, runtime):
    page.evaluate("r => updateStatus({config: {}, runtime: Object.assign("
                  "{kids_mode: {}, agent: {profile: 'conversation'}}, r)})", runtime)


def _shot(page, name, full_page=False):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.evaluate("document.querySelectorAll('.notification-dismiss').forEach(b => b.click())")
    page.evaluate("() => new Promise(requestAnimationFrame)")
    page.screenshot(path=str(SHOTS / f"{name}.png"), animations="disabled", full_page=full_page)


def _row_top(page):
    return page.evaluate("""() => { const row = document.querySelector('.tabs-primary');
      return getComputedStyle(row).position === 'fixed' ? row.getBoundingClientRect().top : innerHeight; }""")


def _overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("width", [320, 390, 1440])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_everyday_states_have_one_primary_and_tidy_layout(ui, width, scheme):
    page, state, posts, errors = ui
    page.emulate_media(color_scheme=scheme)
    page.set_viewport_size({"width": width, "height": 844})
    page.click("#tab-dashboard")
    # Power controls are not on Everyday at all: they live in the Robot page's power section.
    assert page.locator("#panel-dashboard [data-power]").count() == 0
    assert page.locator("#robot-power #global-power-awake").count() == 1
    assert page.locator("#robot-power #global-power-controls").count() == 1
    for name, runtime in STATES.items():
        _show(page, runtime)
        label, target = EXPECT[name]
        primary = page.locator("#everyday-primary")
        assert primary.is_visible() and primary.inner_text().strip() == label, (name, primary.inner_text())
        assert primary.get_attribute("data-target") == target
        assert not primary.is_disabled()
        assert page.locator("#everyday-primary-hint").inner_text().strip() != ""
        # One Stop per context on Everyday: the strip's Stop speaking is not shown here (the primary is it
        # while a turn is active); Stop all work only appears with real work.
        assert page.locator("#voice-stop-button").is_hidden()
        # The fixture's work badge reports project work running, so Stop all work is truthfully shown.
        assert page.locator("#agent-stop-button").is_visible() == page.locator("#native-work-badge").is_visible()
        assert page.locator("#agent-stop-button").count() == 1
        # Secondary chips are small and the whole hero fits in the first screen on phones.
        for chip in ("#everyday-look-link", "#everyday-announce-link", "#everyday-robot-link"):
            top, bottom, height, chip_width = page.evaluate(BOX, chip)
            assert 48 <= height <= 56 and chip_width < 200, (chip, height, chip_width)
        page.evaluate("window.scrollTo(0, 0)")
        top, bottom, _h, _w = page.evaluate(BOX, "#everyday-primary")
        assert 0 <= top and bottom <= _row_top(page), (name, top, bottom)
        face_top = page.evaluate(BOX, "#hero-presence")[0]
        header_bottom = page.evaluate(BOX, ".topbar")[1] if width < 900 else 0
        assert face_top >= header_bottom - 1, (face_top, header_bottom)  # the header never bisects the face
        _overflow(page)
        if name in ("standby-empty", "awake-populated", "speaking"):
            _shot(page, f"{name}-{width}-{scheme}")
    assert not posts and not errors


@pytest.mark.parametrize("width", [320, 390])
def test_primary_forwards_one_click_to_the_real_control(ui, width):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 844})
    page.click("#tab-dashboard")
    power = []
    page.route("**/api/power", lambda route: (power.append(route.request.post_data_json), route.fulfill(
        json={"runtime": {"power_mode": "awake", "motors_enabled": True}})))
    _show(page, STATES["standby-empty"])
    page.click("#everyday-primary")
    page.wait_for_function("!powerTransitionPending")
    assert power == [{"mode": "awake", "duration_minutes": 60}]
    assert page.locator("#global-power-message").inner_text().strip() != ""  # the real control's feedback
    stops = []
    page.route("**/api/voice/stop", lambda route: (stops.append(1), route.fulfill(json={"ok": True})))
    _show(page, STATES["speaking"])
    page.click("#everyday-primary")
    page.wait_for_function("document.getElementById('agent-message').textContent.includes('Stopped')")
    assert stops == [1]
    # Restore / programmatic navigation never acts.
    before = len(posts) + len(power) + len(stops)
    page.evaluate("activateTab('robot', false, true); activateTab('dashboard', false, true)")
    page.wait_for_timeout(300)
    assert len(posts) + len(power) + len(stops) == before
    assert not errors


def test_no_background_work_shows_no_work_badge_or_stop_all_work(ui):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": 390, "height": 844})
    page.click("#tab-dashboard")
    # The default fixture reports running project work; a genuinely idle robot reports none, and then
    # nothing on Everyday claims work: no badge, no Stop all work, only the primary power step.
    state["badge"] = {"status": "idle", "running": False}
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('native-work-badge').hidden")
    assert page.locator("#agent-stop-button").is_hidden()
    assert page.locator("#native-work-badge").is_hidden()
    assert "Project work running" not in page.locator("#panel-dashboard").inner_text()
    _show(page, STATES["standby-empty"])
    assert page.locator("#everyday-primary").inner_text().strip() == "Wake up"
    assert page.locator("#conversation-empty").is_visible()
    _shot(page, "standby-empty-no-work-390")
    assert not errors


def test_workspace_active_and_disconnected_are_truthful(ui):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": 390, "height": 844})
    page.click("#tab-dashboard")
    # Workspace route active with project work: the route is a status line, Stop all work appears once,
    # the bottom row still shows Everyday as the page (navigation), not the route.
    state["runtime"]["agent"]["profile"] = "agent"
    state["runtime"].update(power_mode="awake", motors_enabled=True, state="waiting_for_wake_word")
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('panel-agent').dataset.assistantMode === 'workspace'")
    assert page.locator("#assistant-mode-current").inner_text() == "Workspace"
    assert page.locator("#tab-dashboard").get_attribute("aria-selected") == "true"
    assert page.locator("#agent-stop-button").is_visible() and page.locator("#agent-stop-button").count() == 1
    assert page.locator("#everyday-primary").inner_text().strip() == "Standby"
    _overflow(page)
    _shot(page, "workspace-active-390-dark" if False else "workspace-active-390")
    # Disconnected: the primary says so and is disabled; nothing claims a route, a reply or a mode.
    page.route("**/api/status", lambda route: route.fulfill(status=503, json={"detail": "unavailable"}))
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.body.dataset.runtimeState === 'disconnected'")
    primary = page.locator("#everyday-primary")
    assert primary.is_disabled() and "Reconnecting" in primary.inner_text()
    assert "out of reach" in page.locator("#everyday-primary-hint").inner_text()
    assert "Workspace" not in page.locator("#assistant-mode-current").inner_text()
    _shot(page, "disconnected-390")
    assert not errors


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_scroll_top_middle_bottom_never_hides_controls(ui, scheme):
    page, state, posts, errors = ui
    page.emulate_media(color_scheme=scheme)
    page.set_viewport_size({"width": 390, "height": 844})
    page.click("#tab-dashboard")
    _show(page, STATES["awake-populated"])
    assert page.evaluate("getComputedStyle(document.querySelector('.topbar')).position") == "static"
    # Middle: the companion Look row is reachable and clear of the fixed row.
    page.evaluate("document.getElementById('companion-look').scrollIntoView({block: 'center'})")
    for selector in ("#companion-look", "#companion-stop", "#companion-question"):
        top, bottom, _h, _w = page.evaluate(BOX, selector)
        assert bottom <= _row_top(page) or top >= page.evaluate("innerHeight"), selector
    _shot(page, f"scrolled-middle-390-{scheme}")
    # Bottom: the last Everyday element ends above the row.
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(200)
    last_bottom = page.evaluate("""() => { const items = [...document.querySelectorAll('#panel-dashboard > *')]
      .filter(e => e.getBoundingClientRect().height > 0);
      return items[items.length - 1].getBoundingClientRect().bottom; }""")
    assert last_bottom <= _row_top(page), (last_bottom, _row_top(page))
    _shot(page, f"scrolled-bottom-390-{scheme}")
    # Robot: power section with the moved controls, Stop movement there, Meeting · Sleep folded.
    page.click("#tab-robot")
    page.evaluate("window.scrollTo(0, 0)")
    for selector in ("#robot-power #global-power-awake", "#robot-power #global-power-standby",
                     "#robot-power #global-robot-stop", "#robot-power #global-power-controls > summary"):
        assert page.locator(selector).is_visible(), selector
    assert not page.locator("#global-power-controls").evaluate("el => el.open")
    _overflow(page)
    _shot(page, f"robot-power-390-{scheme}")
    assert not errors
