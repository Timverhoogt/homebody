# ruff: noqa: F811 -- imported fixture is intentionally a test parameter
"""Real Chromium, isolated fixtures: Kids Play together hierarchy and the shared power card.

Every request is answered by the in-page route mock; nothing reaches Reachy, Hermes or a provider.
Screenshots are fixture renders written to the repository's untracked .artifacts directory.
"""

from pathlib import Path

import pytest
from test_assistant_modes_browser import ui  # noqa: F401
from test_kids_game_master_browser import hosted, show

SHOTS = Path(__file__).resolve().parents[1] / ".artifacts" / "kids-power-ui"

HIT = """(id) => {
  const el = document.getElementById(id);
  el.scrollIntoView({block: 'center'});
  const box = el.getBoundingClientRect();
  const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
  return {hit: hit === el || el.contains(hit), disabled: el.disabled, x: box.left + box.width / 2,
          y: box.top + box.height / 2, w: box.width, h: box.height};
}"""


def press(page, element_id):
    """Click at the element's centre only if a real hit-test reaches it (not hidden by the nav)."""
    box = page.evaluate(HIT, element_id)
    assert box["hit"] and not box["disabled"], (element_id, box)
    page.mouse.click(box["x"], box["y"])


def starts(posts):
    return [body for path, body in posts if path == "/api/kids/start"]


def small_targets(page, scope):
    return page.evaluate("""(scope) => [...document.querySelectorAll(
        ['button', 'summary', 'select', 'input[type=text]'].map(tag => `${scope} ${tag}`).join(','))]
      .filter(el => el.offsetParent !== null)
      .map(el => [el.id || el.dataset.power || el.textContent.trim().slice(0, 24), el.getBoundingClientRect()])
      .filter(([, r]) => r.height < 47.5 || r.width < 47.5)
      .map(([name, r]) => [name, Math.round(r.width), Math.round(r.height)])""", scope)


@pytest.mark.parametrize("width", [360, 390, 1440])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_first_kids_screen_leads_with_play_together_and_reviewed_setup(ui, width, scheme):
    page, state, posts, errors = ui
    page.emulate_media(color_scheme=scheme)
    page.set_viewport_size({"width": width, "height": 800})
    page.evaluate("updateStatus({runtime:{power_mode:'standby',motors_enabled:false,head_safely_folded:true,state:'waiting_for_wake_word',kids_mode:{}}})")
    page.click("#tab-kids")
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)
    page.screenshot(path=str(SHOTS / f"kids-first-{width}-{scheme}.png"))
    hero = page.locator(".kids-hero")
    assert hero.locator("h2").inner_text() == "What shall we play?"
    assert "stories, games and make-believe" in hero.inner_text().lower()
    assert hero.locator("#kids-stage .hb-scene").is_visible()  # Reachy's stage leads, above the question
    assert "Choose an activity below" not in page.locator("#panel-kids").inner_text()
    # The idle stage previews the host world, still (no loop) until a session really starts.
    stage = page.evaluate("""() => { const s = document.querySelector('#kids-stage .hb-scene');
      return [s.dataset.scene, s.dataset.phase, s.dataset.motion]; }""")
    assert stage[:2] == ["host", "preview"] and stage[2] != "on", stage
    # The route to the supervised setup is on the first screen, above the bottom nav, and not a start.
    cta = page.locator("#kids-host-setup-link")
    box = cta.bounding_box()
    nav_top = page.evaluate("""() => { const tabs = document.querySelector('.tabs');
      return getComputedStyle(tabs).position === 'fixed' ? tabs.getBoundingClientRect().top : innerHeight; }""")
    assert box and box["y"] + box["height"] <= nav_top, (box, nav_top)
    order = page.evaluate("""() => {
      const el = (id) => document.getElementById(id);
      const pos = (a, b) => Boolean(el(a).compareDocumentPosition(el(b)) & Node.DOCUMENT_POSITION_FOLLOWING);
      const reviewed = ['kids-age-band', 'kids-language', 'kids-duration', 'kids-host-camera-consent'];
      return {setupBeforePicker: pos('kids-host-start-button', 'kids-activity-picker'),
              reviewBeforeStart: reviewed.every(id => pos(id, 'kids-host-start-button'))};
    }""")
    assert order == {"setupBeforePicker": True, "reviewBeforeStart": True}
    press(page, "kids-host-setup-link")
    page.wait_for_timeout(400)
    assert page.evaluate("document.activeElement.id") == "kids-age-band"
    assert not starts(posts), "the setup route must never auto-start"
    assert not page.locator("#kids-host-camera-consent").is_checked()
    assert small_targets(page, "#panel-kids") == []
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#panel-kids").screenshot(path=str(SHOTS / f"kids-full-{width}-{scheme}.png"))
    assert not errors


def test_shared_start_payload_and_standalone_start_are_preserved(ui):
    page, state, posts, errors = ui
    page.click("#tab-kids")
    page.select_option("#kids-age-band", "4-6")
    page.select_option("#kids-language", "nl")
    page.select_option("#kids-duration", "15")
    press(page, "kids-host-start-button")
    page.wait_for_function("!kidsRequestPending")
    host = starts(posts)[-1]
    assert host["host"] is True and host["camera_consent"] is False and host["activity"] == "buddy"
    assert (host["age_band"], host["language"], host["duration_minutes"]) == ("4-6", "nl", 15)
    # A single chosen activity is still possible, from its own secondary card.
    page.click('[data-kids-activity="story"]')
    press(page, "kids-start-button")
    page.wait_for_function("!kidsRequestPending")
    single = starts(posts)[-1]
    assert single["activity"] == "story" and single["host"] is False and single["camera_consent"] is False
    # I Spy alone still demands its own per-start consent before posting.
    count = len(starts(posts))
    page.click('[data-kids-activity="ispy"]')
    press(page, "kids-start-button")
    assert len(starts(posts)) == count
    assert not errors


def test_pending_start_suppresses_both_start_buttons(ui):
    page, state, posts, errors = ui
    page.click("#tab-kids")
    held = []
    page.route("**/api/kids/start", lambda route: held.append(route))
    press(page, "kids-host-start-button")
    page.wait_for_function("kidsRequestPending")
    page.evaluate("['kids-host-start-button', 'kids-start-button'].forEach(id => document.getElementById(id).click())")
    page.wait_for_timeout(150)
    assert len(held) == 1
    assert page.evaluate("document.getElementById('kids-start-button').disabled")
    held[0].fulfill(status=409, json={"detail": "Kids Mode is already starting"})
    page.wait_for_function("!kidsRequestPending")
    assert "already starting" in page.locator("#kids-message").inner_text()
    assert not errors


@pytest.mark.parametrize("width", [360, 390, 1440])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_active_play_together_shows_phase_time_and_end_together(ui, width, scheme):
    page, state, posts, errors = ui
    page.emulate_media(color_scheme=scheme)
    page.set_viewport_size({"width": width, "height": 800})
    page.click("#tab-kids")
    for phase, activity, expected in [("lobby", "", "Choosing"), ("transition", "story", "Switching"),
                                      ("playing", "riddles", "Riddle")]:
        show(page, state, hosted(phase, activity=activity))
        summary = page.locator("#kids-live-summary")
        assert summary.is_visible() and expected in summary.inner_text(), summary.inner_text()
        assert "10:00" in summary.inner_text()
        # Management is locked: setup review and the single-activity picker step aside; End stays.
        assert page.locator("#kids-age-band").is_hidden()
        assert page.locator("#kids-activity-picker").is_hidden()
        assert page.locator("#kids-host-setup-link").is_hidden()
        assert page.locator("#global-adult-controls").is_hidden()
        gap = page.evaluate("""() => document.getElementById('kids-stop-button').getBoundingClientRect().top
            - document.getElementById('kids-live-summary').getBoundingClientRect().bottom""")
        assert 0 <= gap < 160, gap
        end = page.evaluate(HIT, "kids-stop-button")
        assert end["hit"] and 48 <= end["h"] <= 72, end
        assert small_targets(page, "#panel-kids") == []
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / f"kids-active-{phase}-{width}-{scheme}.png"))
    stops = []
    page.route("**/api/kids/stop", lambda route: (stops.append(route.request.method),
                                                  route.fulfill(json={"ok": True, "kids_mode": {"active": False}})))
    press(page, "kids-stop-button")
    page.wait_for_function("!kidsRequestPending")
    assert stops == ["POST"]
    assert not errors


@pytest.mark.parametrize("width", [360, 390, 1440])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_shared_power_card_names_mode_and_exposes_common_actions(ui, width, scheme):
    page, state, posts, errors = ui
    page.emulate_media(color_scheme=scheme)
    page.set_viewport_size({"width": width, "height": 800})
    state["config"]["camera_feed_enabled"] = True
    state["runtime"].update(power_mode="standby", motors_enabled=False, head_safely_folded=True)
    page.evaluate("document.getElementById('tab-robot').click()")  # power lives in the Robot page's power section
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('compact-power-mode').textContent === 'Standby'")
    card = page.locator("#robot-power")
    assert "wake word" in page.locator("#compact-power-detail").inner_text().lower()
    camera = page.locator("#compact-camera").inner_text()
    assert "allowed" in camera.lower() and "not open" in camera.lower(), camera
    for mode in ["awake", "standby"]:
        assert card.locator(f'#global-adult-controls > .global-power-primary [data-power="{mode}"]').is_visible()
    summary = page.locator("#global-power-controls > summary").inner_text()
    assert "Meeting" in summary and "Sleep" in summary
    assert page.locator('#global-power-controls [data-power="meeting"]').is_hidden()
    assert card.locator('[data-power="standby"]').get_attribute("aria-pressed") == "true"
    assert small_targets(page, "#robot-power") == []
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    card.screenshot(path=str(SHOTS / f"power-standby-{width}-{scheme}.png"))
    page.locator("#global-power-controls > summary").click()
    assert page.locator('#global-power-controls [data-power="sleep"]').is_visible()
    card.screenshot(path=str(SHOTS / f"power-more-{width}-{scheme}.png"))
    assert not errors


def test_power_pending_is_not_rendered_as_confirmed_and_stop_stays_reachable(ui):
    page, state, posts, errors = ui
    page.evaluate("HomebodyNotifications.show = () => false")
    state["runtime"].update(power_mode="standby", motors_enabled=False, head_safely_folded=True)
    page.evaluate("document.getElementById('tab-robot').click()")
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('compact-power-mode').textContent === 'Standby'")
    held = []
    page.route("**/api/power", lambda route: held.append(route))
    press(page, "global-power-awake")
    page.wait_for_function("powerTransitionPending")
    page.evaluate("refreshStatus()")  # an ordinary poll during the transition still reports Standby
    page.wait_for_timeout(150)
    assert page.locator("#compact-power-mode").inner_text() == "Standby"
    assert page.locator("#global-power-awake").get_attribute("aria-busy") == "true"
    assert page.locator("#global-power-awake").get_attribute("aria-pressed") == "false"
    page.locator("#global-robot-status").screenshot(path=str(SHOTS / "power-pending-390-light.png"))
    stop = page.evaluate(HIT, "global-robot-stop")
    assert stop["hit"] and not stop["disabled"]
    # Fetch success with an unconfirmed mode is an error, not a completed change.
    held[0].fulfill(json={"runtime": {"power_mode": "standby"}})
    page.wait_for_function("!powerTransitionPending")
    assert "error" in page.get_attribute("#global-power-message", "class")
    assert page.locator("#compact-power-mode").inner_text() == "Standby"
    page.locator("#global-robot-status").screenshot(path=str(SHOTS / "power-error-390-light.png"))
    # Confirmed by the runtime and the next poll: only now does the card say Awake.
    page.unroute("**/api/power")
    sent = []
    page.route("**/api/power", lambda route: (sent.append(route.request.post_data_json), route.fulfill(
        json={"runtime": {"power_mode": "awake", "motors_enabled": True}})))
    state["runtime"].update(power_mode="awake", motors_enabled=True, head_safely_folded=False)
    press(page, "global-power-awake")
    page.wait_for_function("!powerTransitionPending")
    page.wait_for_function("refreshStatus() && document.getElementById('compact-power-mode').textContent === 'Awake'")
    assert "Reachy awake" in page.locator("#global-power-message").inner_text()
    assert sent == [{"mode": "awake", "duration_minutes": 60}]
    assert held[0].request.post_data_json == {"mode": "awake", "duration_minutes": 60}
    assert not errors


def test_offline_power_card_fails_closed(ui):
    page, state, posts, errors = ui
    page.evaluate("document.getElementById('tab-announce').click()")
    page.route("**/api/status", lambda route: route.fulfill(status=503, json={"detail": "unavailable"}))
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('compact-power-mode').textContent.includes('Unknown')")
    assert "unavailable" in page.locator("#compact-power-detail").inner_text().lower()
    assert page.evaluate("[...document.querySelectorAll('#global-robot-status [data-power]')].every(b => b.disabled)")
    page.locator("#global-robot-status").screenshot(path=str(SHOTS / "power-offline-390-light.png"))
    assert not errors


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_enlarged_text_and_reduced_motion_keep_targets_and_layout(ui, scheme):
    page, state, posts, errors = ui
    page.emulate_media(color_scheme=scheme, reduced_motion="reduce")
    page.set_viewport_size({"width": 360, "height": 800})
    page.evaluate("document.documentElement.style.fontSize = '125%'")
    page.click("#tab-kids")
    page.evaluate("window.scrollTo(0, 0)")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert small_targets(page, "#panel-kids") == []
    page.screenshot(path=str(SHOTS / f"kids-first-360-{scheme}-125-reduced.png"))
    show(page, state, hosted("lobby"))
    assert page.evaluate("document.querySelector('#kids-stage .hb-scene').dataset.motion") == "off"
    assert page.evaluate(HIT, "kids-stop-button")["hit"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(SHOTS / f"kids-active-360-{scheme}-125-reduced.png"))
    assert not errors
