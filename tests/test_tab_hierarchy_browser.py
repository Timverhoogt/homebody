"""Tab identity and global controls against real DOM/JS; no robot access."""
from pathlib import Path

import pytest
from test_notifications_browser import browser_page as browser_page


@pytest.mark.parametrize("width", [390, 1440])
def test_page_identity_home_only_hero_and_safe_navigation(browser_page, width):
    page, errors = browser_page
    page.set_viewport_size({"width": width, "height": 844})
    page.clock.resume()
    dest = Path(".pytest_cache/tab-hierarchy")
    dest.mkdir(parents=True, exist_ok=True)
    for tab, label in [("dashboard", "Everyday"), ("agent", "Workspace"), ("kids", "Kids"),
                       ("announce", "Announce"), ("robot", "Robot"), ("settings", "Settings")]:
        if tab == "announce":  # Announce is an Everyday action (and the #announce deep link), not a tab
            page.locator("#tab-dashboard").click()
            page.locator("#everyday-announce-link").click()
        else:
            page.locator(f"#tab-{tab}").click()  # four-button row plus the Settings gear, no More menu
        assert page.locator("#tab-more").is_hidden()
        assert page.locator(".page-heading h1:visible").inner_text() == label
        assert page.title() == f"{label} · Homebody"
        assert page.locator(".status-card").is_visible() == (tab == "dashboard")
        # One status bar on every destination (the hero no longer carries its own power buttons).
        assert page.locator("#global-robot-status").is_visible()
        assert page.locator(".hero [data-power]").count() == 0
        assert page.locator(f"#tab-{tab}").get_attribute("aria-selected") == "true"
        box = page.locator(".page-heading h1:visible").bounding_box()
        assert 0 <= box["y"] < 240
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if tab in {"dashboard", "robot", "settings"}:
            page.evaluate("() => new Promise(requestAnimationFrame)")
            page.screenshot(path=str(dest / f"{tab}-{width}.png"), animations="disabled")
    page.locator("#tab-dashboard").focus()  # Settings may be folded behind More on phones
    page.keyboard.press("Home")
    assert page.locator("#panel-dashboard").is_visible()
    page.keyboard.press("End")
    assert page.locator("#panel-settings").is_visible()
    page.go_back()
    assert page.locator("#panel-dashboard").is_visible()
    page.evaluate("document.getElementById('tab-settings').click()")
    page.clock.resume()
    page.evaluate("() => new Promise(requestAnimationFrame)")
    dest = Path(".pytest_cache/tab-hierarchy")
    dest.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(dest / f"settings-{width}.png"), animations="disabled")
    assert not errors


def test_compact_status_and_kids_lock_are_authoritative(browser_page):
    page, errors = browser_page
    page.evaluate("document.getElementById('tab-settings').click()")
    page.evaluate("updateStatus({config:{camera_feed_enabled:true},runtime:{power_mode:'awake',motors_enabled:true,state:'listening'}})")
    assert page.locator("#compact-power-mode").inner_text() == "Awake"
    for name in ["mic", "camera", "motors"]:
        assert page.locator(f"#compact-{name}").inner_text() == page.locator(f"#tele-{name}").inner_text()
    page.evaluate("activateTab('robot')")  # power controls live on Robot now
    page.locator("#robot-power #global-power-controls summary").click()
    assert page.locator('#global-power-controls [data-power="sleep"]').is_visible()
    page.evaluate("updateStatus({runtime:{kids_mode:{locked:true,active:true}}})")
    assert page.locator(".page-heading h1:visible").inner_text() == "Kids"
    assert page.locator("#global-adult-controls").is_hidden()
    assert not page.locator("#global-power-controls").evaluate("el => el.open")
    page.evaluate("activateTab('settings')")
    assert page.locator("#panel-settings").is_hidden()
    assert page.locator("#panel-dashboard").is_hidden()
    assert not errors


@pytest.mark.parametrize("outcome", ["success", "unconfirmed", "network"])
def test_shared_power_and_stop_use_visible_feedback(browser_page, outcome):
    page, errors = browser_page
    page.evaluate("document.getElementById('tab-robot').click()")  # power controls live on Robot now
    # Suppress toast cards to exercise the actual inline fallback.
    page.evaluate("HomebodyNotifications.show = () => false")
    if outcome == "network":
        page.route("**/api/power", lambda route: route.abort())
    else:
        runtime = (
            {"power_mode": "awake", "motors_enabled": True}
            if outcome == "success" else {"power_mode": "standby"}
        )
        page.route("**/api/power", lambda route: route.fulfill(json={"runtime": runtime}))
    page.locator('#global-power-awake').click()
    page.wait_for_function("!powerTransitionPending")
    message = page.locator("#global-power-message")
    assert message.is_visible()
    assert ("Reachy awake" in message.inner_text()) == (outcome == "success")
    if outcome != "success":
        assert "error" in message.get_attribute("class")
    page.route(
        "**/api/robot/stop",
        lambda route: route.fulfill(json={"robot_stopped": True, "active_cancelled": True}),
    )
    page.locator("#global-robot-stop").click()
    page.wait_for_function("document.querySelector('#global-stop-message').textContent === 'Movement stopped'")
    assert page.locator("#global-stop-message").is_visible()
    assert not errors
