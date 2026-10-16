"""Compact Everyday permissions in real Chromium, entirely intercepted local API fixtures."""
# ruff: noqa: F811 -- imported pytest fixture is intentionally used as a parameter.

from copy import deepcopy
from pathlib import Path

import pytest
from test_assistant_modes_browser import CAPABILITIES, ui  # noqa: F401, F811

SHOTS = Path(__file__).resolve().parents[1] / ".artifacts/browser-screens/everyday-capabilities"


def snapshot(**changes):
    body = deepcopy(CAPABILITIES)
    body.update(
        {
            "local": {
                "permissions": {
                    "power": True,
                    "movement": False,
                    "camera": True,
                    "camera_on_request": True,
                    "volume": True,
                },
                "activity": {"camera_session_active": False, "camera_remaining_seconds": 0},
                "tools": ["get_reachy_status", "get_reachy_volume", "start_reachy_camera"],
            },
            "groups": {"timers": {"available": True}, "public_lookup": {"available": False}},
        }
    )
    body.update(changes)
    return body


def refresh(page, state, body):
    state["capabilities"] = body
    page.evaluate(
        "payload => {HomebodyAssistantModes.owner(false); HomebodyAssistantModes.owner(true); "
        "HomebodyAssistantModes.update(payload)}",
        {"config": state["config"], "runtime": state["runtime"]},
    )
    page.wait_for_function("document.getElementById('everyday-badge').textContent === 'Tools ready'")


def wait_text(page, selector, fragment):
    page.wait_for_function(
        "([selector, fragment]) => document.querySelector(selector).textContent.includes(fragment)",
        arg=[selector, fragment],
    )


@pytest.mark.parametrize("width", [320, 390, 1440])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("scale", [1, 1.25])
def test_compact_permissions_geometry_and_navigation(ui, width, scheme, scale):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 1000})
    page.emulate_media(color_scheme=scheme)
    page.evaluate("scale => document.documentElement.style.fontSize = `${16 * scale}px`", scale)
    page.click("#tab-agent")
    refresh(page, state, snapshot())
    assert "OpenAI Realtime" in page.locator("#everyday-voice").inner_text()
    assert "not proof of an active" in page.locator("#everyday-voice-note").inner_text()
    assert "timers & reminders" in page.locator("#everyday-summary").inner_text()
    assert page.locator("#everyday-permissions").get_attribute("open") is None
    assert page.locator("#everyday-technical-details").get_attribute("open") is None
    assert page.locator("#everyday-card input").count() == 0  # not a second settings form
    SHOTS.mkdir(parents=True, exist_ok=True)
    # Isolated card crops hide fixed chrome only during capture; real hit-testing below retains it.
    page.locator("#everyday-card").screenshot(
        path=str(SHOTS / f"compact-{width}-{scheme}-{scale}.png"),
        style=".topbar, .tabs { visibility: hidden !important; }",
    )
    if scale == 1:
        page.evaluate("scrollTo(0,0)")
        page.screenshot(path=str(SHOTS / f"page-{width}-{scheme}.png"), full_page=True)
    page.locator("#everyday-permissions > summary").click()
    assert "movement: off" in page.locator("#everyday-robot-permissions").inner_text()
    assert "Camera access: allowed" in page.locator("#everyday-camera-permission").inner_text()
    assert "Public lookup: unavailable" in page.locator("#everyday-search").inner_text()
    page.locator("#everyday-technical-details > summary").click()
    assert "desk light" in page.locator("#everyday-device-ids").inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    targets = [
        "#everyday-permissions > summary",
        "#everyday-technical-details > summary",
        "[data-everyday-setting=robot_tools_enabled]",
        "[data-everyday-setting=camera_enabled]",
    ]
    for selector in targets:
        target = page.locator(selector)
        target.scroll_into_view_if_needed()
        geometry = target.evaluate("""e => {
          const r = e.getBoundingClientRect();
          const hit = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
          return {height:r.height, fits:e.scrollWidth <= e.clientWidth + 1,
                  hit:hit === e || e.contains(hit)};
        }""")
        assert round(geometry["height"], 2) >= 48 and geometry["fits"] and geometry["hit"], geometry
    page.locator("#everyday-card").screenshot(
        path=str(SHOTS / f"permissions-{width}-{scheme}-{scale}.png"),
        style=".topbar, .tabs { visibility: hidden !important; }",
    )
    before = len(posts)
    page.click("[data-everyday-setting=camera_enabled]")
    checkbox = page.locator("#camera_enabled")
    assert checkbox.is_visible() and not checkbox.is_checked()
    assert page.evaluate("document.activeElement.id") == "camera_enabled"
    assert checkbox.locator("..").bounding_box()["height"] >= 48
    assert page.locator("#camera_enabled").count() == 1
    assert page.locator("#everyday_camera_enabled").count() == 0
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert len(posts) == before and not errors


def test_old_unknown_and_malformed_fields_do_not_invent_permissions(ui):
    page, state, posts, errors = ui
    page.click("#tab-agent")
    refresh(page, state, deepcopy(CAPABILITIES))
    assert "not reported" in page.locator("#everyday-robot-permissions").text_content()
    assert "status not reported" in page.locator("#everyday-camera-activity").text_content()
    malformed = snapshot(
        groups={"public_lookup": {"available": "yes", "status": "future"}},
        local={
            "permissions": {"camera": "yes", "movement": 1},
            "activity": {"camera_session_active": "true"},
            "tools": "not-an-array",
        },
        devices={"controls": "future", "status": None},
    )
    refresh(page, state, malformed)
    assert "movement: not reported" in page.locator("#everyday-robot-permissions").text_content()
    assert "Camera access: not reported" in page.locator("#everyday-camera-permission").text_content()
    assert "not reported by the host" in page.locator("#everyday-search").text_content()
    assert "undefined" not in page.locator("#everyday-card").text_content()
    assert not posts and not errors


def test_temporary_session_expiry_is_not_persistent_camera_or_voice_activity(ui):
    page, state, posts, errors = ui
    page.click("#tab-agent")
    body = snapshot()
    body["local"]["activity"] = {"camera_session_active": True, "camera_remaining_seconds": 1}
    refresh(page, state, body)
    wait_text(page, "#everyday-camera-activity", "Temporary camera active")
    assert "Camera access: allowed" in page.locator("#everyday-camera-permission").text_content()
    wait_text(page, "#everyday-camera-activity", "expired")
    assert "active" not in page.locator("#everyday-camera-activity").text_content()
    body["local"]["activity"] = {"camera_session_active": True, "camera_remaining_seconds": None}
    refresh(page, state, body)
    assert "unconfirmed" in page.locator("#everyday-camera-activity").text_content()
    assert not posts and not errors


@pytest.mark.parametrize("transition", ["owner", "kids", "offline"])
def test_private_copies_and_held_permissions_cannot_return_after_teardown(ui, transition):
    page, state, _posts, errors = ui
    page.click("#tab-agent")
    body = snapshot()
    body["text_provider"]["model"] = "private-model-name"
    body["devices"]["controls"] = ["private-room-device"]
    refresh(page, state, body)
    page.locator("#everyday-permissions > summary").click()
    page.locator("#everyday-technical-details > summary").click()
    held = []
    page.route("**/api/everyday/capabilities", lambda route: held.append(route))
    page.evaluate(
        "payload => {HomebodyAssistantModes.owner(false); HomebodyAssistantModes.owner(true); "
        "HomebodyAssistantModes.update(payload)}",
        {"config": state["config"], "runtime": state["runtime"]},
    )
    page.wait_for_function("document.getElementById('everyday-badge').textContent === 'Unavailable'")
    page.wait_for_timeout(100)
    assert held
    if transition == "owner":
        # Real fetch wrapper observes cross-endpoint 401 before the immutable old body arrives.
        page.route(
            "**/api/agent/workspace/native-badge",
            lambda route: route.fulfill(status=401, json={"detail": "Owner required"}),
        )
        page.evaluate("fetch('/api/agent/workspace/native-badge')")
        page.wait_for_function("HomebodyOwner.isOwner() === false")
    elif transition == "kids":
        state["runtime"]["kids_mode"] = {"active": True}
        page.evaluate(
            "payload => HomebodyAssistantModes.update(payload)",
            {"config": state["config"], "runtime": state["runtime"]},
        )
    else:
        page.evaluate("HomebodyAssistantModes.offline()")
    held[0].fulfill(json=body)
    page.wait_for_timeout(100)
    raw = page.locator("#everyday-card").text_content()  # includes hidden/nested copies
    assert "private-model-name" not in raw and "private-room-device" not in raw
    assert "Camera access: allowed" not in raw
    assert page.locator("#everyday-card").is_hidden()
    assert page.locator("#everyday-permissions").get_attribute("open") is None
    stored = page.evaluate("JSON.stringify({local:{...localStorage},session:{...sessionStorage}})")
    assert "private-model-name" not in stored and "private-room-device" not in stored
    assert not errors


def test_host_configuration_is_not_live_provider_or_audible_delivery_proof(ui):
    page, state, posts, errors = ui
    page.click("#tab-agent")
    refresh(
        page,
        state,
        snapshot(
            groups={
                "timers": {"configured": True, "scope": "conversation_session", "persistent": False},
                "public_lookup": {"configured": True, "provider": "brave", "query_leaves_host": True},
            }
        ),
    )
    assert "configured · not verified live" in page.locator("#everyday-search").text_content()
    assert "public queries leave your home" in page.locator("#everyday-search").text_content()
    assert "Session-only; audible delivery unverified" in page.locator("#everyday-household").text_content()
    assert not posts and not errors


def test_late_active_camera_snapshot_cannot_extend_its_reported_expiry(ui):
    page, state, posts, errors = ui
    page.click("#tab-agent")
    held = []
    page.route("**/api/everyday/capabilities", lambda route: held.append(route))
    page.evaluate(
        "payload => {HomebodyAssistantModes.owner(false); HomebodyAssistantModes.owner(true); "
        "HomebodyAssistantModes.update(payload)}",
        {"config": state["config"], "runtime": state["runtime"]},
    )
    page.wait_for_timeout(1200)
    assert held
    body = snapshot()
    body["local"]["activity"] = {"camera_session_active": True, "camera_remaining_seconds": 1}
    held[0].fulfill(json=body)
    wait_text(page, "#everyday-camera-activity", "expired")
    assert "active" not in page.locator("#everyday-camera-activity").text_content()
    assert not posts and not errors


def test_single_camera_permission_uses_existing_settings_without_activating_viewer_or_tracking(ui):
    page, state, posts, errors = ui
    state["config"].update(camera_enabled=False)
    page.click("#tab-agent")
    refresh(page, state, snapshot())
    page.locator("#everyday-permissions > summary").click()
    page.click("[data-everyday-setting=camera_enabled]")
    assert not page.locator("#camera_enabled").is_checked()
    page.check("#camera_enabled")
    page.locator("#settings-form button[type=submit]").first.click()
    wait_text(page, "#form-message", "Saved")
    saved = [body for path, body in posts if path == "/api/settings"][-1]
    assert saved["camera_enabled"] is True
    assert "everyday_camera_enabled" not in saved
    assert not saved["camera_feed_enabled"] and not saved["face_tracking_enabled"]
    assert all(path == "/api/settings" for path, _ in posts)
    assert not errors
