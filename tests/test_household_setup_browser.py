"""Household setup card in real Chromium against the real server-side status/apply logic.

The page is served from the repository; every API reply is computed by ``homebody.household`` from an
in-memory config, so nothing contacts Reachy, Hermes or a provider and nothing captures.
"""
# ruff: noqa: F811 -- imported pytest fixture is intentionally used as a parameter.

import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import pytest
from test_assistant_modes_browser import ui  # noqa: F401

from homebody.config import AppConfig, merge_config
from homebody.household import HouseholdSetupRequest, household_revocation, household_status, household_updates

SHOTS = Path(__file__).resolve().parents[1] / ".artifacts/browser-screens/household"


def serve(page, config, *, apply_status=200):
    state = {"config": config, "posts": []}

    def respond(route):
        request = route.request
        path = urlparse(request.url).path
        if request.method == "GET":
            return route.fulfill(json=household_status(state["config"]))
        body = json.loads(request.post_data or "{}")
        state["posts"].append((path, body))
        if path == "/api/household/setup":
            if apply_status != 200:
                detail = "Household setup is unavailable while Kids Mode is active"
                return route.fulfill(status=apply_status, json={"detail": detail})
            updates = household_updates(HouseholdSetupRequest(**body), now=1_700_000_000)
        elif path == "/api/household/revoke":
            updates = household_revocation()
        else:
            updates = {"camera_enabled": False}
        state["config"] = merge_config(state["config"], updates)
        route.fulfill(json=household_status(state["config"]))

    page.route("**/api/household/**", respond)
    page.click("#tab-agent")
    page.evaluate("HomebodyHousehold.reload()")
    page.wait_for_function("document.getElementById('household-badge').textContent !== 'Checking…'")
    return state


def hit_testable(page, selector):
    target = page.locator(selector)
    target.scroll_into_view_if_needed()
    return target.evaluate("""e => {
      const r = e.getBoundingClientRect();
      const hit = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
      return {height: r.height, hit: hit === e || e.contains(hit), fits: e.scrollWidth <= e.clientWidth + 1};
    }""")


@pytest.mark.parametrize("width", [320, 390, 1440])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_first_setup_discloses_cloud_use_and_saves_only_explicit_choices(ui, width, scheme):
    page, _state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(color_scheme=scheme)
    state = serve(page, AppConfig(camera_enabled=False))
    card = page.locator("#household-card")
    assert card.is_visible()
    assert page.locator("#household-badge").text_content() == "Not set up"
    assert "did not approve anything" in page.locator("#household-state").text_content()
    assert "Camera access: off" in page.locator("#household-camera-state").text_content()
    page.locator("#household-review > summary").click()
    disclosure = page.locator("#household-disclosure").text_content()
    assert "cloud service" in disclosure and "one still frame" in disclosure and "never identified" in disclosure
    assert "purchases, messages, locks" in page.locator("#household-excluded").text_content()
    assert page.locator("#household-approve").is_disabled()
    assert page.locator("#household-kids_ispy_camera").is_disabled()
    assert page.locator("#household-revoke").is_disabled() and page.locator("#household-camera-off").is_disabled()
    page.check("#household-camera_on_request")
    assert page.locator("#household-kids_ispy_camera").is_enabled()
    page.check("#household-voice_power_controls")
    assert page.locator("#household-approve").is_disabled()  # still needs the acknowledgement
    page.check("#household-ack")
    for selector in ("#household-review > summary", "#household-approve", "label:has(#household-ack)"):
        geometry = hit_testable(page, selector)
        assert geometry["height"] >= 48 and geometry["hit"] and geometry["fits"], (selector, geometry)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    SHOTS.mkdir(parents=True, exist_ok=True)
    card.screenshot(
        path=str(SHOTS / f"review-{width}-{scheme}.png"), style=".topbar, .tabs { visibility: hidden !important; }"
    )
    page.click("#household-approve")
    page.wait_for_function("document.getElementById('household-badge').textContent === 'Approved'")
    assert state["posts"] == [(
        "/api/household/setup",
        {"disclosure_version": 1, "acknowledged": True, "camera_on_request": True,
         "following_and_movement": False, "voice_power_controls": True, "kids_ispy_camera": False},
    )]
    assert "No picture was taken" in page.locator("#household-message").text_content()
    assert "Camera access: on" in page.locator("#household-camera-state").text_content()
    assert page.locator("#household-revoke").is_enabled() and page.locator("#household-camera-off").is_enabled()
    # The persisted current state is what the server reports, shown after a reload too.
    page.evaluate("HomebodyHousehold.reload()")
    page.wait_for_function("document.getElementById('household-camera_on_request').checked === true")
    assert not any(path.startswith(("/api/companion", "/api/camera", "/api/settings")) for path, _ in posts)
    assert not errors


def test_camera_off_and_revoke_controls_reduce_and_persist(ui):
    page, _state, _posts, errors = ui
    approved = merge_config(AppConfig(), household_updates(HouseholdSetupRequest(
        disclosure_version=1, acknowledged=True, camera_on_request=True, following_and_movement=True,
        voice_power_controls=True, kids_ispy_camera=True), now=1_700_000_000))
    state = serve(page, approved)
    assert page.locator("#household-badge").text_content() == "Approved"
    page.click("#household-camera-off")
    page.wait_for_function("document.getElementById('household-camera-state').textContent.includes('off')")
    assert state["config"].camera_enabled is False and page.locator("#household-camera-off").is_disabled()
    page.click("#household-revoke")
    page.wait_for_function("document.getElementById('household-badge').textContent === 'Not set up'")
    assert state["config"].camera_natural_requests_enabled is False
    assert state["config"].kids_ispy_camera_preapproved is False
    assert [path for path, _ in state["posts"]] == ["/api/household/camera-off", "/api/household/revoke"]
    assert not errors


def test_refusal_kids_mode_and_owner_loss_never_show_or_keep_an_approval(ui):
    page, _state, _posts, errors = ui
    serve(page, replace(AppConfig(), camera_enabled=True), apply_status=423)
    page.locator("#household-review > summary").click()
    page.check("#household-camera_on_request")
    page.check("#household-ack")
    page.click("#household-approve")
    page.wait_for_function("document.getElementById('household-message').textContent.includes('Kids Mode')")
    assert page.locator("#household-badge").text_content() == "Not set up"
    page.evaluate("HomebodyHousehold.update({runtime: {kids_mode: {active: true}}})")
    assert page.locator("#household-card").is_hidden()
    page.evaluate("HomebodyHousehold.update({runtime: {kids_mode: {}}}); HomebodyHousehold.owner(false)")
    assert page.locator("#household-card").is_hidden()
    assert page.locator("#household-disclosure li").count() == 0
    assert not page.locator("#household-camera_on_request").is_checked()
    assert not errors


def test_connection_card_sends_only_explicit_settings_and_clears_secret(ui):
    page, _state, posts, errors = ui
    serve(page, AppConfig())
    captured = []
    def respond(route):
        if route.request.method == "GET":
            return route.fulfill(json={"url": "", "enabled": False, "connected": False})
        captured.append((urlparse(route.request.url).path, json.loads(route.request.post_data or "{}")))
        route.fulfill(json={"saved": True, "ok": True})
    page.route("**/api/household/tools/**", respond)
    page.locator("#household-connection > summary").click()
    page.fill("#household-url", "https://house.invalid")
    page.fill("#household-credential", "synthetic-secret")
    page.check("#household-durable")
    page.check("#household-connection-ack")
    page.click("#household-connection-save")
    page.wait_for_function("document.getElementById('household-connection-message').textContent.includes('saved')")
    assert captured == [("/api/household/tools/configure", {
        "url": "https://house.invalid", "robot_credential": "synthetic-secret", "enabled": True,
        "acknowledged": True,
    })]
    assert page.input_value("#household-credential") == ""
    assert "synthetic-secret" not in page.locator("body").inner_text()
    page.click("#household-connection-validate")
    page.wait_for_function("document.getElementById('household-connection-message').textContent.includes('validated')")
    assert captured[-1] == ("/api/household/tools/validate", {})
    assert not errors
