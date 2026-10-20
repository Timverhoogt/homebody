# ruff: noqa: F811 -- imported fixture is intentionally a test parameter
"""Field cleanup after 0.5.40: the Everyday flow in its empty, populated, disconnected and scrolled states.

Real Chromium, explicit fixtures only. Covers: the tabs drive the voice route (no visible duplicate switch,
no silent switch on restore), the conversation empty state, Settings reachability of the relocated
shortcut and device controls, the install prompt inside the Android WebView, and 390 / 1440 layout.
Screenshots: HOMEBODY_TEST_SCREENSHOTS (default .artifacts/screenshots) / field-cleanup.
"""

import os
from pathlib import Path

import pytest
from settings_ui_helpers import choose
from test_assistant_modes_browser import ui  # noqa: F401

SHOTS = Path(os.getenv("HOMEBODY_TEST_SCREENSHOTS", ".artifacts/screenshots")) / "field-cleanup"
WEBVIEW_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/UP1A; wv) AppleWebKit/537.36 (KHTML, like Gecko) " \
             "Version/4.0 Chrome/124.0.0.0 Mobile Safari/537.36"


def _shot(page, name, full_page=False):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.evaluate("document.querySelectorAll('.notification-dismiss').forEach(b => b.click())")
    page.evaluate("() => new Promise(requestAnimationFrame)")
    page.screenshot(path=str(SHOTS / f"{name}.png"), animations="disabled", full_page=full_page)


def _profiles(posts):
    return [body for path, body in posts if path == "/api/agent/profile"]


def _clear_of_row(page, selector):
    return page.evaluate("""(selector) => {
      const el = document.querySelector(selector), row = document.querySelector('.tabs-primary');
      if (!el || getComputedStyle(row).position !== 'fixed') return true;
      const r = el.getBoundingClientRect(), n = row.getBoundingClientRect();
      return r.bottom <= n.top || r.top >= n.bottom;
    }""", selector)


@pytest.mark.parametrize("width", [390, 1440])
def test_tabs_drive_the_voice_route_and_restore_never_switches(ui, width):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 900})
    page.click("#tab-dashboard")
    # No duplicate switch in the flow; the confirmed route is still named for the user.
    assert page.evaluate("getComputedStyle(document.querySelector('.assistant-mode-switch')).display") == "none"
    assert page.locator("#assistant-mode-state").is_visible()
    assert page.locator("#assistant-mode-current").inner_text() == "Everyday"
    assert _profiles(posts) == []
    # Programmatic navigation (hash restore, Android restore) never switches the route.
    page.evaluate("activateTab('agent', false, true)")
    page.evaluate("activateTab('dashboard', false, true)")
    assert _profiles(posts) == []
    # A real tap on Workspace switches to the agent profile through the existing API, with feedback.
    page.click("#tab-agent")
    page.wait_for_function("document.getElementById('panel-agent').dataset.assistantMode === 'workspace'")
    assert _profiles(posts) == [{"profile": "agent", "preserve_work": False}]
    assert "Workspace route active" in page.locator("#agent-message").inner_text()
    assert page.locator("#assistant-mode-current").inner_text() == "Workspace"
    # A real tap on Everyday brings it back, preserving approved project work; repeated taps post once.
    page.click("#tab-dashboard")
    page.wait_for_function("document.getElementById('panel-agent').dataset.assistantMode === 'everyday'")
    page.click("#tab-dashboard")
    assert _profiles(posts)[1:] == [{"profile": "conversation", "preserve_work": True}]
    assert "Everyday active" in page.locator("#agent-message").inner_text()
    # Stop controls stay exactly once.
    for stop in ("#global-robot-stop", "#voice-stop-button", "#agent-stop-button"):
        assert page.locator(stop).count() == 1
    assert not errors


def test_route_switch_failure_is_reported_and_route_stays(ui):
    page, state, posts, errors = ui
    page.route("**/api/agent/profile", lambda route: route.fulfill(status=502, json={"detail": "Bridge unavailable"}))
    page.click("#tab-agent")
    page.wait_for_function("document.getElementById('agent-message').textContent.includes('Bridge unavailable')")
    assert "error" in page.locator("#agent-message").get_attribute("class")
    page.wait_for_function("document.getElementById('panel-agent').dataset.assistantMode === 'everyday'")
    assert page.locator("#workspace-gate").is_visible()  # still Everyday, and the gate says so
    assert not errors


@pytest.mark.parametrize("width", [390, 1440])
def test_conversation_is_one_line_until_something_is_said(ui, width):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 900})
    page.click("#tab-dashboard")
    card = page.locator(".conversation-card")
    assert card.get_attribute("data-empty") == "true"
    assert page.locator("#conversation-empty").is_visible()
    assert page.locator("#last-transcript").is_hidden() and page.locator("#last-response").is_hidden()
    assert card.bounding_box()["height"] < 120, card.bounding_box()
    _shot(page, f"everyday-empty-{width}")
    page.evaluate("updateStatus({runtime:{power_mode:'awake',state:'speaking',transcript:'How long is the oven on?',"
                  "response_preview:'Eleven minutes. The kitchen light is on.',kids_mode:{}}})")
    assert card.get_attribute("data-empty") == "false"
    assert page.locator("#conversation-empty").is_hidden()
    assert page.locator("#last-transcript").inner_text() == "How long is the oven on?"
    assert page.locator("#last-response").inner_text() == "Eleven minutes. The kitchen light is on."
    _shot(page, f"everyday-populated-{width}")
    # A transcript alone (reply still coming) is also shown, never hidden.
    page.evaluate("updateStatus({runtime:{power_mode:'awake',state:'thinking',transcript:'Set a timer',"
                  "kids_mode:{}}})")
    assert page.locator("#last-transcript").is_visible() and page.locator("#last-response").inner_text() == "—"
    assert not errors


@pytest.mark.parametrize("width", [390, 1440])
def test_shortcut_and_device_controls_live_in_settings_and_webview_hides_install(ui, width):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 900})
    page.click("#tab-dashboard")
    assert page.locator("#panel-dashboard #install-card").count() == 0
    assert page.locator("#panel-settings #install-card").count() == 1
    assert page.locator("#panel-settings #owner-device-controls").count() == 1
    page.click("#tab-settings")
    choose(page,'system')
    card = page.locator("#install-card")
    assert card.is_visible()  # ordinary Chromium: the shortcut card is available in Settings
    assert page.locator("#install-help").count() == 1
    _shot(page, f"settings-shortcut-{width}", full_page=True)
    # Inside the Android app's WebView the home-screen install is meaningless and hidden; real UA signal.
    page.evaluate("ua => { Object.defineProperty(navigator, 'userAgent', {get: () => ua}); updateInstallUi(); }",
                  WEBVIEW_UA)
    assert card.is_hidden()
    assert page.evaluate("runningInWrapper()") is True
    assert not errors


def test_phone_scrolled_and_disconnected_states_are_tidy(ui):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": 390, "height": 844})
    page.click("#tab-dashboard")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    # The connection badge is a small dot with an accessible name and a 44 px target.
    badge = page.locator("#agent-connection-badge")
    box = badge.bounding_box()
    assert box["height"] >= 44 and box["width"] <= 56, box
    assert page.locator("#agent-connection-label").text_content().strip() != ""
    # Companion verbosity: the static suggestion paragraph is gone, the "why" hint stays short and real.
    assert page.locator(".companion-suggestions > p.compact-note:not(#companion-why)").first.is_hidden()
    assert page.locator("#companion-why").is_hidden()
    assert page.locator("#panel-settings #companion-why").count() == 1
    # Scrolled: the Look row is reachable and never under the fixed row.
    page.locator("#companion-look").scroll_into_view_if_needed()
    page.evaluate("document.getElementById('companion-look').scrollIntoView({block: 'center'})")
    for selector in ("#companion-look", "#companion-stop"):
        assert _clear_of_row(page, selector), selector
    _shot(page, "everyday-scrolled-390")
    # Disconnected: status unavailable → the hero and strip say so, nothing claims a route or a reply.
    page.route("**/api/status", lambda route: route.fulfill(status=503, json={"detail": "unavailable"}))
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('panel-agent').dataset.assistantMode === 'unknown'")
    page.evaluate("window.scrollTo(0, 0)")
    assert page.locator("#conversation-empty").is_visible() or page.locator("#last-transcript").is_visible()
    assert "Workspace" not in page.locator("#assistant-mode-current").inner_text()
    _shot(page, "everyday-disconnected-390")
    assert not errors
