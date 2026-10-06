"""Actual Chromium UI with explicit API fixtures; never contacts Reachy."""

from pathlib import Path
from urllib.parse import urlparse

import pytest


@pytest.fixture
def workspace_browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        fixture = {"enabled": False, "generation": 1, "events": [], "seconds_remaining": 0}
        runtime = {"state": "waiting_for_wake_word", "power_mode": "standby", "kids_mode": {}}

        def respond(route):
            path = urlparse(route.request.url).path
            if path.startswith("/static/"):
                target = Path("homebody") / path.lstrip("/")
                route.fulfill(path=str(target)) if target.exists() else route.fulfill(status=404)
            elif path == "/":
                route.fulfill(path="homebody/static/index.html", content_type="text/html")
            elif path == "/api/owner/session":
                route.fulfill(json={"owner": True, "name": "Browser fixture", "csrf": "fixture"})
            elif path == "/api/status":
                route.fulfill(json={"config": {}, "runtime": runtime})
            elif path.startswith("/api/agent/workspace/native/"):
                route.fulfill(json={"targets": [], "messages": [], "events": [], "pending": None})
            elif path.startswith("/api/agent/workspace"):
                if path.endswith("/start"):
                    fixture.update(enabled=True, seconds_remaining=3600)
                elif path.endswith("/clear"):
                    fixture.update(enabled=False, events=[], generation=fixture["generation"] + 1)
                route.fulfill(json=fixture)
            else:
                route.fulfill(json={})

        page.route("**/*", respond)
        page.goto("http://homebody.test/")
        page.wait_for_function("!document.querySelector('main').hidden && typeof updateStatus === 'function'")
        page.click("#tab-agent")
        yield page, fixture, runtime, errors
        browser.close()


@pytest.mark.parametrize("width", [390, 1440])
def test_companion_default_and_conversation_layout(workspace_browser, width):
    page, fixture, _runtime, errors = workspace_browser
    page.set_viewport_size({"width": width, "height": 900})
    assert page.locator("#agent-companion-view").is_visible()
    assert not page.locator("#agent-workspace-view").is_visible()
    page.click("#agent-view-workspace")
    page.wait_for_function("!document.getElementById('workspace-start').disabled")
    assert not page.locator(".agent-run-sheet").is_visible()
    page.click("#workspace-start")
    page.wait_for_function("document.getElementById('workspace-badge').textContent === 'Showing live'")
    fixture["events"] = [
        {"id": 1, "role": "user", "text": "Hello Homebody"},
        {"id": 2, "role": "activity", "text": "Hermes is responding"},
        {"id": 3, "role": "assistant", "text": "Hello Tim. What would you like to work on?"},
    ]
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.querySelectorAll('.workspace-event').length === 3")
    assert page.locator(".workspace-user strong").text_content() == "You"
    assert page.locator(".workspace-user p").text_content() == "Hello Homebody"
    assert "What would you like to work on?" in page.locator(".workspace-assistant").inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator(".agent-session-details summary").is_visible()
    Path(".pytest_cache/workspace-browser").mkdir(parents=True, exist_ok=True)
    page.screenshot(path=f".pytest_cache/workspace-browser/workspace-{width}.png", full_page=True)
    page.click("#workspace-clear")
    page.wait_for_function("document.querySelectorAll('.workspace-event').length === 0")
    assert fixture["events"] == []
    assert not fixture["enabled"]
    assert not errors


def test_text_only_and_privacy_clears_dom(workspace_browser):
    page, fixture, runtime, errors = workspace_browser
    page.click("#agent-view-workspace")
    page.wait_for_function("!document.getElementById('workspace-start').disabled")
    page.click("#workspace-start")
    page.wait_for_function("document.getElementById('workspace-badge').textContent === 'Showing live'")
    fixture["events"] = [{"id": 1, "role": "assistant", "text": "<img src=x onerror=alert(1)> adult text"}]
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.querySelectorAll('.workspace-event').length === 1")
    assert page.locator("#workspace-timeline img").count() == 0
    runtime.update(power_mode="sleep")
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.querySelectorAll('.workspace-event').length === 0")
    assert page.locator("#workspace-start").is_disabled()
    assert "privacy" in page.locator("#workspace-state").inner_text()
    assert not errors


def test_late_poll_cannot_repaint_after_privacy(workspace_browser):
    page, _fixture, _runtime, errors = workspace_browser
    held = []
    page.route("**/api/agent/workspace", lambda route: held.append(route))
    page.click("#agent-view-workspace")
    page.wait_for_function("document.getElementById('workspace-start').disabled")
    page.wait_for_timeout(100)
    assert held
    page.evaluate("HomebodyWorkspace.update({power_mode:'sleep'})")
    held[0].fulfill(json={
        "enabled": True, "generation": 1,
        "events": [{"id": 1, "role": "user", "text": "late private data"}],
    })
    page.wait_for_timeout(100)
    assert page.locator(".workspace-event").count() == 0
    assert "late private data" not in page.locator("#workspace-timeline").inner_text()
    assert not errors


def test_tool_activity_and_pending_approval_reveal_workspace(workspace_browser):
    page, _fixture, runtime, errors = workspace_browser
    runtime["agent"] = {"profile": "agent", "activity": [], "pending_approval": False}
    page.route("**/api/agent/activity", lambda route: route.fulfill(json={"activity": [{
        "timestamp": 1, "event": "capability_finished", "capability_id": "get_reachy_status",
        "result_class": "success",
    }]}))
    page.route("**/api/agent/pending-approval", lambda route: route.fulfill(json={"pending_approval": {
        "capability_id": "set_timer", "arguments": {"seconds": 60}, "expires_in_seconds": 30,
    }}))
    page.evaluate("refreshStatus()")
    page.evaluate("refreshAgentActivity()")
    page.wait_for_function("!document.getElementById('agent-workspace-view').hidden")
    assert page.locator("#agent-approval-sheet").is_visible()
    page.click(".agent-session-details summary")
    assert "get reachy status · capability finished · success" in page.locator("#agent-activity").inner_text()
    assert page.locator("#agent-approval-arguments").text_content() == '{\n  "seconds": 60\n}'
    assert not errors


def test_clear_does_not_wait_for_slow_poll(workspace_browser):
    page, fixture, _runtime, errors = workspace_browser
    held = []
    page.route("**/api/agent/workspace", lambda route: held.append(route))
    page.click("#agent-view-workspace")
    page.wait_for_timeout(100)
    assert held
    assert page.locator("#workspace-clear").is_enabled()
    page.click("#workspace-clear")
    page.wait_for_function(
        "document.getElementById('workspace-state').textContent.includes('Nothing is being retained')"
    )
    assert fixture["enabled"] is False
    assert fixture["events"] == []
    assert page.locator(".workspace-event").count() == 0
    assert not errors
