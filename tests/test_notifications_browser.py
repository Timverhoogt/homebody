"""Optional Chromium checks of actual DOM/CSS and app wiring, with all APIs intercepted.

Run: python -m pip install playwright && python -m playwright install chromium
     python -m pytest tests/test_notifications_browser.py -o addopts='' -q
No requests reach a robot; responses below are explicit test fixtures.
"""

from pathlib import Path
from urllib.parse import urlparse

import pytest


@pytest.fixture
def browser_page():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        try:
            browser = driver.chromium.launch()
        except playwright.Error as error:
            if "Executable doesn't exist" in str(error):
                pytest.skip("Install Chromium with python -m playwright install chromium")
            raise
        page = browser.new_page(viewport={"width": 390, "height": 844})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.clock.install(time=0)
        page.clock.pause_at(1000)
        Path(".pytest_cache/notification-browser").mkdir(parents=True, exist_ok=True)

        def respond(route):
            path = urlparse(route.request.url).path
            if path.startswith("/static/"):
                target = Path("homebody") / path.lstrip("/")
                if target.exists():
                    route.fulfill(path=str(target))
                else:
                    route.fulfill(status=404)
            elif path == "/":
                route.fulfill(path="homebody/static/index.html", content_type="text/html")
            elif path == "/api/owner/session":
                route.fulfill(json={"owner": True, "name": "Browser fixture", "csrf": "test-only"})
            elif path == "/api/status":
                route.fulfill(json={"config": {}, "runtime": {"state": "standby", "power_mode": "standby"}})
            elif path == "/api/settings" and route.request.method == "POST":
                route.fulfill(status=503, json={"detail": "Fixture: settings server unavailable"})
            else:
                route.fulfill(json={})

        page.route("**/*", respond)
        page.goto("http://homebody.test/")
        page.wait_for_function("typeof notifyFeedback === 'function' && !document.querySelector('main').hidden")
        yield page, errors
        browser.close()


def test_app_notification_wiring_and_phone_layout(browser_page):
    page, errors = browser_page
    page.evaluate("notifyFeedback(document.getElementById('agent-message'), 'ok')")
    page.evaluate("HomebodyNotifications.clear()")
    for message, kind, text in [
        ("form-message", "ok", "Saved"),
        ("agent-message", "error", "Action failed"),
        ("presence-message", "pending", "Updating presence…"),
    ]:
        page.evaluate(
            "([id,kind,text]) => {const el=document.getElementById(id);el.textContent=text;notifyFeedback(el,kind)}",
            [message, kind, text],
        )
    assert page.locator(".notification:visible").count() == 3
    box = page.locator("#notifications").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 390
    assert box["y"] >= 0 and box["y"] + box["height"] <= 844 - 80
    for button in page.locator(".notification-dismiss:visible").all():
        bounds = button.bounding_box()
        assert bounds["width"] >= 44 and bounds["height"] >= 44
    page.clock.resume()
    page.evaluate("() => new Promise(requestAnimationFrame)")
    page.screenshot(path=".pytest_cache/notification-browser/phone.png", animations="disabled")
    page.locator(".notification-dismiss").first.focus()
    page.keyboard.press("Enter")
    assert page.locator(".notification:visible").count() == 2
    assert page.locator(".notification-dismiss").first.evaluate("el => el === document.activeElement")
    page.evaluate("HomebodyNotifications.show('<img src=x onerror=alert(1)>', {kind:'error'})")
    assert page.locator(".notification img").count() == 0
    page.evaluate("HomebodyNotifications.clear()")
    page.locator("#tab-dashboard").focus()
    page.evaluate("HomebodyNotifications.show('Saved', {kind:'ok'})")
    page.locator(".notification-dismiss").focus()
    page.keyboard.press("Enter")
    assert page.locator("#tab-dashboard").evaluate("el => el === document.activeElement")
    assert not errors


def test_real_browser_timer_queue_and_hover(browser_page):
    page, errors = browser_page
    page.evaluate("for(let i=0;i<4;i++) HomebodyNotifications.show('Saved '+i, {kind:'ok'})")
    assert page.locator(".notification:visible").count() == 3
    assert page.locator(".notification-queued").inner_text().startswith("1 more")
    page.locator(".notification").first.hover()
    page.clock.fast_forward(6000)
    assert page.locator(".notification:visible").count() == 2
    page.mouse.move(0, 0)
    page.clock.fast_forward(5999)
    assert page.locator(".notification:visible").count() == 2
    page.clock.fast_forward(1)
    assert page.locator(".notification:visible").count() == 0
    page.evaluate("HomebodyNotifications.show('Still pending', {kind:'pending'})")
    page.clock.fast_forward(60000)
    assert page.locator(".notification:visible").count() == 1
    assert not errors


def test_actual_settings_failure_and_tab_switch(browser_page):
    page, errors = browser_page
    page.locator("#tab-settings").click()
    # Submit the real form handler, with a fixture 503 response; not a robot write.
    page.locator("#settings-form").evaluate(
        "el => { el.noValidate=true; el.requestSubmit(el.querySelector('button[type=submit]')); }"
    )
    page.wait_for_function("document.querySelector('.notification-error') !== null")
    assert "Fixture: settings server unavailable" in page.locator(".notification-error").inner_text()
    page.locator("#tab-dashboard").click()
    assert page.locator(".notification-error").is_visible()
    page.clock.fast_forward(60000)
    assert page.locator(".notification-error").is_visible()
    assert not errors


def test_private_feedback_clears_and_late_callbacks_stay_quiet(browser_page):
    page, errors = browser_page
    page.clock.fast_forward(12000)
    assert page.locator(".notification").count() == 0, "routine polling must stay quiet"
    page.evaluate("HomebodyNotifications.show('Private action detail', {kind:'error'})")
    page.evaluate("updateStatus({runtime:{kids_mode:{locked:true}}})")
    assert page.locator(".notification").count() == 0
    page.evaluate("HomebodyNotifications.show('Late result while Kids locked', {kind:'error'})")
    assert page.locator(".notification").count() == 0
    page.evaluate("updateStatus({runtime:{kids_mode:{locked:false}}})")
    page.evaluate("HomebodyNotifications.show('Private owner detail', {kind:'error'})")
    page.route("**/api/status", lambda route: route.fulfill(status=401, json={"detail": "Fixture: session expired"}))
    page.evaluate("fetch('/api/status')")
    assert page.locator("main").is_hidden()
    assert page.locator(".notification").count() == 0
    page.evaluate("HomebodyNotifications.show('Late private result', {kind:'error'})")
    assert page.locator(".notification").count() == 0
    assert not errors


def test_desktop_long_text_reduced_motion_and_fullscreen(browser_page):
    page, errors = browser_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    page.evaluate(
        "HomebodyNotifications.show('Long error '.repeat(150), {kind:'error'}); "
        "HomebodyNotifications.show('Loading', {kind:'pending'})"
    )
    assert page.locator(".notification").first.evaluate("el => getComputedStyle(el).animationName") == "none"
    assert (
        page.locator(".notification").last.evaluate(
            "el => getComputedStyle(el.querySelector('.notification-text'),'::before').animationName"
        )
        == "none"
    )
    box = page.locator("#notifications").bounding_box()
    assert box["y"] >= 0 and box["x"] + box["width"] <= 1440
    page.locator(".notification-dismiss").first.click()
    page.clock.resume()
    page.evaluate("() => new Promise(requestAnimationFrame)")
    page.screenshot(path=".pytest_cache/notification-browser/desktop.png")
    page.locator("#panel-dashboard").evaluate(
        "el => el.addEventListener('click', () => el.requestFullscreen(), {once:true})"
    )
    page.locator("#panel-dashboard").click(position={"x": 5, "y": 5})
    page.wait_for_function(
        "document.fullscreenElement && "
        "document.getElementById('notifications').parentElement === document.fullscreenElement"
    )
    assert page.locator("#notifications").evaluate("el => el.parentElement === document.fullscreenElement")
    page.evaluate("document.exitFullscreen()")
    page.wait_for_function(
        "!document.fullscreenElement && "
        "document.getElementById('notifications').parentElement === document.body"
    )
    assert page.locator("#notifications").evaluate("el => el.parentElement === document.body")
    assert not errors
