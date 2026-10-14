"""Chromium fixtures for marketplace-first setup; no live services or hardware."""

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest


@pytest.fixture
def setup_ui():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        errors, posts = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        state = {
            "owner": True,
            "required": True,
            "step": "welcome",
            "entry_route": "unknown",
            "connection_configured": False,
            "connection_checked": False,
            "speech_tested": False,
            "host": {"label": "NVIDIA Jetson", "kind": "jetson", "system": "Linux", "machine": "aarch64"},
            "python": "3.11",
            "sdk": "1.9.0",
            "recommendation": "GPU models are optional and share system RAM.",
            "language": "en",
            "capability_note": "Host detection is not a hardware acceptance test.",
        }

        def respond(route):
            request = route.request
            path = urlparse(request.url).path
            payload = json.loads(request.post_data or "{}") if request.method == "POST" else {}
            if request.method == "POST":
                posts.append((path, payload))
            if path.startswith("/static/"):
                target = Path("homebody") / path.lstrip("/")
                route.fulfill(path=str(target)) if target.exists() else route.fulfill(status=404)
            elif path == "/":
                route.fulfill(path="homebody/static/index.html", content_type="text/html")
            elif path == "/api/owner/session":
                route.fulfill(json={"owner": state["owner"], "name": "Fixture", "csrf": "fixture"})
            elif path == "/api/status":
                route.fulfill(
                    json={"config": {"setup_pending": True}, "runtime": {"state": "setup_required", "kids_mode": {}}}
                )
            elif path == "/api/onboarding/progress":
                state.update(payload)
                route.fulfill(json=state)
            elif path == "/api/onboarding/check":
                state["connection_checked"] = True
                route.fulfill(json=state)
            elif path == "/api/onboarding/finish":
                state["required"] = False
                route.fulfill(
                    json={
                        **state,
                        "restart_required": True,
                        "note": "Stop then Start Homebody in Reachy Mini Control.",
                    }
                )
            elif path == "/api/onboarding":
                route.fulfill(json=state)
            else:
                route.fulfill(json={})

        page.route("**/*", respond)
        page.goto("http://homebody.test/")
        page.wait_for_function("!document.querySelector('main').hidden")
        yield page, state, posts, errors
        browser.close()


@pytest.mark.parametrize("width", [320, 390, 1440])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_marketplace_first_run_is_visible_and_does_not_install_or_activate(setup_ui, width, scheme):
    page, state, posts, errors = setup_ui
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(color_scheme=scheme)
    page.wait_for_selector("#setup-open")
    page.click("#setup-open")
    page.wait_for_selector("#setup-dialog")
    assert page.locator("#setup-dialog").is_visible()
    assert "Reachy Mini Control" in page.locator("#setup-welcome").inner_text()
    assert "NVIDIA Jetson" in page.locator("#setup-host").inner_text()
    assert "Off" in page.locator("#tele-mic").inner_text()
    page.select_option("#setup-entry", "marketplace")
    page.click("#setup-next")
    page.wait_for_function("!document.getElementById('setup-connection').hidden")
    assert "already installed" in page.locator("#setup-route-note").inner_text()
    assert state["step"] == "connection" and state["entry_route"] == "marketplace"
    page.click("#setup-close")
    page.reload()
    page.wait_for_selector("#setup-open")
    page.click("#setup-open")
    page.wait_for_function("!document.getElementById('setup-connection').hidden")
    assert not any(
        path.startswith(("/api/camera", "/api/voice/", "/api/power", "/api/updates/install")) for path, _ in posts
    )
    assert not errors
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    output = Path(".artifacts/onboarding")
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"marketplace-{width}-{scheme}.png"), full_page=True)


def test_backend_check_then_review_never_claims_speech_verified(setup_ui):
    page, state, posts, errors = setup_ui
    page.wait_for_selector("#setup-open")
    page.click("#setup-open")
    page.click("#setup-next")
    page.wait_for_function("!document.getElementById('setup-connection').hidden")
    page.click("#setup-check")
    page.wait_for_function("!document.getElementById('setup-review').hidden")
    assert "Speech has not been tested" in page.locator("#setup-review").inner_text()
    assert page.locator("#setup-finish").is_disabled()
    page.check("#setup-restart-consent")
    page.click("#setup-finish")
    page.wait_for_function("document.getElementById('setup-message').textContent.includes('Stop then Start')")
    assert ("/api/onboarding/finish", {"confirm": "restart"}) in posts
    assert not any(path.startswith(("/api/camera", "/api/voice/", "/api/power")) for path, _ in posts)
    assert not errors


def test_late_private_setup_reply_is_discarded_on_owner_loss(setup_ui):
    page, state, posts, errors = setup_ui
    page.wait_for_selector("#setup-open")
    page.click("#setup-open")
    held = []
    page.route("**/api/onboarding", lambda route: held.append(route))
    with page.expect_request("**/api/onboarding"):
        page.evaluate("void HomebodyOnboarding.refresh()")
    page.wait_for_function("document.getElementById('setup-refresh').disabled")
    page.evaluate("HomebodyOnboarding.owner(false)")
    assert page.locator("#setup-dialog").is_hidden()
    assert page.locator("#setup-intro").is_hidden()
    for route in held:
        route.fulfill(json={**state, "host": {"label": "PRIVATE OLD HOST"}})
    page.wait_for_timeout(80)
    assert "PRIVATE OLD HOST" not in page.locator("body").inner_text()
    assert not errors
