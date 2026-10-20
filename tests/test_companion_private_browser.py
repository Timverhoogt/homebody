"""Owner-only details and primary companion controls, exercised in real Chromium with HTTP fixtures."""

import json
from pathlib import Path

import pytest
from test_assistant_modes_browser import ui  # noqa: F401
from test_companion_browser import open_card


@pytest.mark.parametrize("width", [320, 390, 1440])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_primary_companion_is_compact_and_advanced_is_optional(ui, width, theme):  # noqa: F811
    page, state, _posts, errors = ui
    page.set_viewport_size({"width": width, "height": 1000})
    page.emulate_media(color_scheme=theme)
    open_card(page, state)
    assert page.locator("#presence-enabled").is_hidden()
    assert page.locator("#presence-acknowledgement-enabled").is_hidden()
    assert page.locator("#panel-settings #presence-enabled").count() == 1
    assert not page.locator("#companion-advanced").evaluate("el => el.open")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for selector in ("#companion-look", "#companion-follow-up", "#companion-stop"):
        box = page.locator(selector).bounding_box()
        assert box and box["height"] >= 48
    shots = Path(__file__).resolve().parents[1] / ".artifacts" / "browser-screens" / "companion-final"
    shots.mkdir(parents=True, exist_ok=True)
    # Fit the entire component for inspection; phone geometry above was tested at height 1000.
    height = page.locator("#companion-card").bounding_box()["height"]
    page.set_viewport_size({"width": width, "height": int(height + 360)})
    page.locator("#companion-card").screenshot(path=str(shots / f"{theme}-{width}.png"))
    assert not errors


def test_private_calendar_details_render_as_text_and_leave_on_real_owner_loss(ui):  # noqa: F811
    page, state, _posts, errors = ui
    open_card(page, state)
    secret = "<img src=x onerror=alert(1)> Dentist Friday at 15:00"
    page.route(
        "**/api/companion/status",
        lambda r: r.fulfill(
            json={
                "ok": True,
                "private_details": [{"source": "calendar", "detail": secret, "seconds_remaining": 30}],
            }
        ),
    )
    page.evaluate("HomebodyCompanion.loadPrivateDetails()")
    assert page.locator("#companion-private-list").inner_text() == secret
    assert page.locator("#companion-private-list img").count() == 0
    page.route("**/api/agent/activity", lambda r: r.fulfill(status=401, json={"detail": "revoked"}))
    assert page.evaluate("fetch('/api/agent/activity').then(r => r.status)") == 401
    assert page.evaluate("HomebodyOwner.isOwner()") is False
    assert "Dentist" not in page.content()
    assert page.locator("#companion-private-details").is_hidden()
    assert not errors


@pytest.mark.parametrize("teardown", ["camera_off", "server_stop"])
def test_held_look_does_not_survive_camera_revocation_or_an_external_stop(ui, teardown):  # noqa: F811
    page, state, _posts, errors = ui
    open_card(page, state)
    held = []

    def hold(route):
        held.append(route)
        page.evaluate("window.__lookActuallyHeld = true")

    page.route("**/api/companion/look", hold)
    page.fill("#companion-question", "Read this letter")
    page.click("#companion-look")
    page.wait_for_function("window.__lookActuallyHeld === true")
    if teardown == "camera_off":
        state["runtime"]["companion"]["camera"]["permitted"] = False
        state["config"]["camera_enabled"] = False
    else:
        state["runtime"]["companion"]["look"]["generation"] = 1
    payload = json.dumps({"config": state["config"], "runtime": state["runtime"]})
    page.evaluate(f"updateStatus({payload})")
    with page.expect_response("**/api/companion/look"):
        held.pop().fulfill(json={"ok": True, "answer": "SECRET LETTER", "captures": 1})
    page.wait_for_timeout(100)
    assert "SECRET LETTER" not in page.content()
    assert page.locator("#companion-question").input_value() == ""
    assert not errors
