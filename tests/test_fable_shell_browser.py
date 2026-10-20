# ruff: noqa: F811 -- imported fixtures are intentionally test parameters
"""Fable shell in real Chromium with explicit fixtures: four-button row, Settings gear, presence hero,
Everyday actions, Kids play-first screen, Robot and Settings composition, legacy deep links.

Screenshots go to HOMEBODY_TEST_SCREENSHOTS (default .artifacts/screenshots/fable). Every API reply is
a fixture; nothing contacts Reachy, Hermes or a provider.
"""

import os
from pathlib import Path

import pytest
from test_assistant_modes_browser import ui  # noqa: F401
from test_ui_shell_browser import _connect_and_open, _pane, native, shell  # noqa: F401

SHOTS = Path(os.getenv("HOMEBODY_TEST_SCREENSHOTS", ".artifacts/screenshots")) / "fable"
OVERFLOW = """() => [...document.querySelectorAll('body *')].filter(e => {
  const r = e.getBoundingClientRect(); return r.width && (r.right > innerWidth + 1 || r.left < -1);
}).slice(0, 10).map(e => ({tag: e.tagName, id: e.id, cls: String(e.className).slice(0, 40)}))"""


def _shot(page, name, full_page=False):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.evaluate("() => new Promise(requestAnimationFrame)")
    page.screenshot(path=str(SHOTS / f"{name}.png"), animations="disabled", full_page=full_page)


def _no_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), page.evaluate(OVERFLOW)


def _nav_clear(page, selector):
    """The fixed phone row never covers the element (its bottom edge stays above the row)."""
    return page.evaluate("""(selector) => {
      const el = document.querySelector(selector); if (!el) return true;
      const tabs = document.querySelector('.tabs-primary');
      if (getComputedStyle(tabs).position !== 'fixed') return true;
      const r = el.getBoundingClientRect(), n = tabs.getBoundingClientRect();
      return r.bottom <= n.top || r.top >= n.bottom || r.right <= n.left || r.left >= n.right;
    }""", selector)


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_shell_row_gear_and_every_destination(ui, width, scheme):
    page, state, posts, errors = ui
    page.emulate_media(color_scheme=scheme)
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("updateStatus({runtime:{power_mode:'awake',motors_enabled:true,state:'waiting_for_wake_word',kids_mode:{}}})")
    # Four destinations, in order, and Settings reachable as the gear; More is gone.
    row = [t.strip() for t in page.locator(".tabs-primary button:visible").all_inner_texts()]
    assert row == ["Everyday", "Workspace", "Kids", "Robot"], row
    assert page.locator("#tab-more").is_hidden() and page.locator("#tab-announce").is_hidden()
    assert page.locator("#tab-settings").is_visible()
    rail = page.evaluate("getComputedStyle(document.querySelector('.topbar')).position")
    assert rail == ("fixed" if width >= 900 else "static")  # phones: the header scrolls away, never over the hero
    # Workspace last: tapping it switches the voice route for real, so every earlier screenshot shows
    # one coherent idle state and no "Workspace active" notice over Kids.
    for tab in ["dashboard", "kids", "robot", "settings", "agent"]:
        page.click(f"#tab-{tab}")
        assert page.locator(f"#panel-{tab}").is_visible()
        assert page.locator("#global-robot-status").is_visible()
        _no_overflow(page)
        heading = page.locator(".page-heading h1:visible").bounding_box()
        assert heading and 0 <= heading["y"] < 240, heading
        for stop in ("#global-robot-stop", "#voice-stop-button", "#agent-stop-button"):
            assert page.locator(stop).count() == 1  # Stop exactly once per surface
        _shot(page, f"{tab}-{width}-{scheme}")
    # Only the explicit tap on Workspace switches the voice route; nothing else posts.
    paths = {path for path, _ in posts}
    assert "/api/agent/profile" in paths and paths <= {"/api/agent/profile", "/api/agent/run/current"}, posts
    assert not errors


@pytest.mark.parametrize("width", [390, 1440])
def test_everyday_presence_is_live_state_and_actions_only_navigate(ui, width):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 900})
    page.click("#tab-dashboard")
    # The hero is the live scene plus the runtime sentence; no fixture copy, no power buttons.
    assert page.locator("#hero-presence .hb-scene").is_visible()
    assert page.locator(".hero [data-power]").count() == 0
    page.evaluate("updateStatus({runtime:{power_mode:'awake',motors_enabled:true,state:'listening',kids_mode:{}}})")
    page.wait_for_function("document.querySelector('#hero-presence .hb-scene').dataset.phase === 'listening'")
    assert page.locator("#runtime-state").inner_text().strip() != ""
    assert page.locator("#last-response").inner_text().strip() == "—"  # nothing invented
    # Everyday actions open existing controls and never post.
    actions = ("everyday-look-link", "everyday-announce-link", "everyday-robot-link")
    assert all(_nav_clear(page, f"#{i}") for i in actions)
    page.click("#everyday-look-link")
    assert page.evaluate("document.activeElement.id") == "companion-question"
    page.click("#everyday-announce-link")
    assert page.locator("#panel-announce").is_visible()
    assert page.locator(".page-heading h1:visible").inner_text() == "Announce"
    assert page.evaluate("location.hash") == "#announce"
    _shot(page, f"announce-{width}-light")
    page.click("#announce-back")
    assert page.locator("#panel-dashboard").is_visible()
    page.click("#everyday-robot-link")
    assert page.locator("#panel-robot").is_visible() and page.evaluate("document.activeElement.id") == "tab-robot"
    assert not posts and not errors


@pytest.mark.parametrize("fragment", ["announce", "robot", "settings", "agent", "kids"])
def test_legacy_deep_links_still_open_their_panel(ui, fragment):
    page, state, posts, errors = ui
    page.goto(f"http://homebody.test/#{fragment}")
    page.wait_for_function("!document.querySelector('main').hidden && typeof updateStatus === 'function'")
    assert page.locator(f"#panel-{fragment}").is_visible()
    assert page.locator(f"#tab-{fragment}").get_attribute("aria-selected") == "true"
    # Restoring a hash never enters Workspace or starts anything on its own.
    assert not [p for p in posts if p[0] in ("/api/agent/profile", "/api/agent/workspace/start", "/api/kids/start")]
    assert not errors


@pytest.mark.parametrize("width", [390, 1440])
def test_kids_play_first_with_folded_grownup_detail(ui, width):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("updateStatus({runtime:{power_mode:'standby',motors_enabled:false,head_safely_folded:true,state:'waiting_for_wake_word',kids_mode:{}}})")
    page.click("#tab-kids")
    page.evaluate("window.scrollTo(0, 0)")
    hero = page.locator(".kids-hero")
    stage = hero.locator("#kids-stage .hb-scene")
    assert stage.is_visible() and stage.bounding_box()["height"] > 120  # the robot is drawn, not blank
    assert stage.bounding_box()["y"] < hero.locator("h2").bounding_box()["y"]
    assert not page.locator("#kids-guide").evaluate("el => el.open")
    assert "How Kids Mode works" not in page.locator("#panel-kids").inner_text()
    # The idea cloud keeps the six activities as starters; choosing one never starts a session.
    assert page.locator(".kids-activities [data-kids-activity]").count() == 6
    page.click('[data-kids-activity="story"]')
    assert not [p for p in posts if p[0] == "/api/kids/start"]
    assert _nav_clear(page, "#kids-host-setup-link")
    _no_overflow(page)
    _shot(page, f"kids-lobby-{width}-light")
    # Start → grown-up check → Play together posts once; End posts a stop; Look now stays wired.
    page.click("#kids-host-setup-link")
    assert page.locator("#kids-host-start-button").is_visible()
    starts = []
    page.route("**/api/kids/start", lambda route: (starts.append(route.request.post_data_json),
                                                   route.fulfill(json={"ok": True, "kids_mode": {"active": True}})))
    page.click("#kids-host-start-button")
    page.wait_for_function("!kidsRequestPending")
    assert len(starts) == 1 and starts[0]["host"] is True, starts
    state["runtime"]["kids_mode"] = {"active": True, "locked": False, "remaining_seconds": 1500, "activity": "story",
                                     "host": {"enabled": True, "phase": "lobby"}}
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('panel-kids').dataset.kidsSession === 'active'")
    assert not page.locator("#kids-stop-button").is_disabled()
    assert page.locator("#kids-show-tell-look").count() == 1
    _shot(page, f"kids-active-{width}-light")
    stops = []
    page.route("**/api/kids/stop", lambda route: (stops.append(1), route.fulfill(
        json={"ok": True, "kids_mode": {"active": False}})))
    page.click("#kids-stop-button")
    page.wait_for_function("!kidsRequestPending")
    assert stops == [1]
    assert not errors


@pytest.mark.parametrize("width", [390, 1440])
def test_robot_and_settings_keep_every_control_reachable(ui, width):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 900})
    # A realistic pose catalogue (the real backend serves one); the UI hides poses it is not offered.
    page.route("**/api/robot/options", lambda route: route.fulfill(json={
        "look": ["up_left", "up", "up_right", "left", "center", "right", "down_left", "down", "down_right"],
        "dance": ["short", "groovy", "energetic"], "emotion": ["happy", "thinking", "surprised", "yes", "no"]}))
    page.click("#tab-robot")
    page.evaluate("loadRobotOptions()")  # the app loads the catalogue once at start-up
    page.wait_for_function("document.getElementById('emotion-select').options.length === 5")
    assert page.locator("#look-d-pad button:visible").count() == 9
    pad = page.evaluate("""() => [...document.querySelectorAll('#look-d-pad button, .dance-group .preset-button')]
      .map(b => { const r = b.getBoundingClientRect(); return [b.textContent.trim().slice(0, 8), Math.round(r.width),
        Math.round(r.height)]; })""")
    assert len(pad) == 12 and all(entry[1] >= 48 and entry[2] >= 48 for entry in pad), pad
    for selector in ("#robot-power #global-power-awake", "#robot-power #global-power-standby", "#robot-stop-button",
                     "#emotion-button", "#camera-live-start"):
        assert page.locator(selector).is_visible(), selector
    # Precision, gamepad and buttons stay one disclosure away.
    for selector in (".precision-group", ".bluetooth-card", ".gpio-card"):
        assert page.locator(f"{selector} > summary").is_visible()
        assert not page.locator(selector).evaluate("el => el.open")
    _no_overflow(page)
    _shot(page, f"robot-{width}-light", full_page=True)
    page.click("#tab-settings")
    links = page.locator(".settings-index a")
    assert links.count() == 7
    assert page.locator('.settings-index a[href="#household-card"]').count() == 1
    for i in range(links.count()):
        target = links.nth(i).get_attribute("href").lstrip("#")
        assert page.locator(f"#{target}").count() == 1, target
    for selector in ("#settings-form", "#speaker-card", "#updates-card", "#agent-setup-card", "#app-off-button",
                     "#household-card, #everyday-card"):
        assert page.locator(selector).count() >= 1, selector
    assert page.locator("#camera_enabled").count() == 1 and page.locator("#home_assistant_enabled").count() == 1
    _no_overflow(page)
    _shot(page, f"settings-{width}-light", full_page=True)
    assert not posts and not errors


@pytest.mark.parametrize("width", [390, 1440])
def test_workspace_thread_evidence_and_project_chip(native, width):
    page, voice, _runtime, state, errors = native
    page.set_viewport_size({"width": width, "height": 900})
    assert page.locator("#workspace-start").inner_text() == "Connect"
    _shot(page, f"workspace-connect-{width}-light")
    _connect_and_open(page, voice)
    page.wait_for_function("document.getElementById('workspace-live-status').textContent === 'Speaking the reply'")
    assert page.inner_text("#workspace-mode-repository") == "Timverhoogt/photo"
    # Reachy's turns read as serif prose, yours as bubbles; evidence stays mono in the Work pane.
    assistant = page.locator("#workspace-timeline .workspace-assistant").first
    user = page.locator("#workspace-timeline .workspace-user").first
    assert "serif" in page.evaluate("el => getComputedStyle(el).fontFamily.toLowerCase()", assistant.element_handle())
    assert page.evaluate("el => getComputedStyle(el).backgroundColor", user.element_handle()) != "rgba(0, 0, 0, 0)"
    _no_overflow(page)
    _shot(page, f"workspace-thread-{width}-light", full_page=True)
    _pane(page, "work")
    page.fill("#workspace-native-request", "Add an export button.")
    page.click("#workspace-native-prepare")
    page.wait_for_function("!document.getElementById('workspace-native-approval').hidden")
    assert state["calls"].count("approve") == 0
    _shot(page, f"workspace-review-{width}-light", full_page=True)
    page.click("#workspace-native-approve")
    page.wait_for_function("document.getElementById('workspace-native-state').dataset.status === 'running'")
    assert state["calls"].count("approve") == 1
    page.click("#workspace-native-stop")
    assert not errors
