"""Actual browser badge and polling behavior; all API requests are intercepted."""

from pathlib import Path

from test_notifications_browser import browser_page as browser_page


def status(page, state, label="Hermes", detail="Fixture connection status", fresh=25000):
    page.route(
        "**/api/status",
        lambda route: route.fulfill(
            json={
                "config": {},
                "runtime": {"state": "standby", "power_mode": "standby"},
                "agent_connection": {"state": state, "label": label, "detail": detail, "fresh_for_ms": fresh},
            }
        ),
    )
    page.evaluate("refreshStatus()")


def test_live_states_navigation_retry_and_safe_text(browser_page):
    page, errors = browser_page
    badge = page.locator("#agent-connection-badge")
    for state, label, text in [
        ("connected", "Hermes", "Hermes connected"),
        ("checking", "Hermes", "Checking connection…"),
        ("unavailable", "Hermes", "Hermes unavailable"),
        ("unconfigured", "Agent", "No agent connected"),
        ("connected", "OpenClaw", "OpenClaw connected"),
    ]:
        status(page, state, label)
        assert badge.get_attribute("data-state") == state
        assert badge.inner_text() == text
    status(page, "unavailable", detail="<img src=x onerror=alert(1)>")
    badge.click()
    assert page.locator("#panel-settings").is_visible()
    assert page.locator("#agent-connection-details").evaluate("el => el === document.activeElement")
    assert page.locator("#agent-connection-detail").inner_text() == "<img src=x onerror=alert(1)>"
    assert page.locator("#agent-connection-details img").count() == 0
    page.route(
        "**/api/agent-connection/retry",
        lambda route: route.fulfill(
            json={
                "state": "checking",
                "label": "Hermes",
                "detail": "Retrying fixture",
                "fresh_for_ms": 0,
            }
        ),
    )
    page.locator("#agent-connection-retry").click()
    assert badge.get_attribute("data-state") == "checking"
    assert page.locator("#agent-connection-retry").is_disabled()
    assert not errors


def test_stale_health_and_robot_disconnection_clear_green(browser_page):
    page, errors = browser_page
    badge = page.locator("#agent-connection-badge")
    status(page, "connected", fresh=100)
    page.clock.fast_forward(100)
    assert badge.get_attribute("data-state") == "checking"
    status(page, "connected", fresh=0)
    assert badge.get_attribute("data-state") == "checking"
    status(page, "connected")
    page.route("**/api/status", lambda route: route.fulfill(status=503, json={"detail": "Fixture app offline"}))
    page.evaluate("refreshStatus()")
    assert badge.get_attribute("data-state") == "unavailable"
    status(page, "connected")
    assert badge.get_attribute("data-state") == "connected"
    assert not errors


def test_phone_and_desktop_header_layout(browser_page):
    page, errors = browser_page
    status(page, "connected")
    dest = Path(".pytest_cache/agent-connection-browser")
    dest.mkdir(parents=True, exist_ok=True)
    page.clock.resume()
    for name, width in [("phone", 390), ("desktop", 1440)]:
        page.set_viewport_size({"width": width, "height": 844})
        page.evaluate("() => new Promise(requestAnimationFrame)")
        box = page.locator("#agent-connection-badge").bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width
        assert box["height"] >= 44
        assert page.locator("#agent-connection-badge").evaluate(
            "el => {const r=el.getBoundingClientRect();"
            "return el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}"
        )
        page.screenshot(path=str(dest / f"{name}.png"), animations="disabled")
    assert not errors
