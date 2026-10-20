"""Real Chromium: display consent, project connection, spoken-request routing and listening stay distinct.

Every API reply is an explicit fixture; nothing contacts Reachy, Hermes, a camera or a model.
"""

import json
from pathlib import Path

import pytest
import test_workspace_browser as browser_fixtures
from settings_ui_helpers import choose

route_browser = browser_fixtures.workspace_browser
SHOTS = Path(__file__).resolve().parents[1] / ".artifacts" / "browser-screens" / "workspace-live-repair"


def text(page, element_id):
    # Route and voice facts live in the header's Details disclosure, so read content, not rendering.
    return page.locator(f"#{element_id}").text_content()


def wait_text(page, element_id, fragment):
    page.wait_for_function(
        f"document.getElementById({json.dumps(element_id)}).textContent.includes({json.dumps(fragment)})")


@pytest.mark.parametrize("width", [390, 1440])
def test_show_bind_voice_route_clear_reopen_and_stale_replies(route_browser, width):
    page, display, runtime, errors = route_browser
    page.set_viewport_size({"width": width, "height": 900})
    SHOTS.mkdir(parents=True, exist_ok=True)
    native = {
        "targets": [{"target_id": "photo", "backend": "hermes", "title": "Raw photo project",
                     "project_id": "photo", "session_id": "native-existing"}],
        "binding": None, "messages": [], "events": [], "pending": None, "run": {},
    }
    voice_discussion = {"enabled": False}
    held = []

    def respond(route):
        action = route.request.url.rsplit("/", 1)[-1]
        if action == "bind":
            native["binding"] = {"backend": "hermes", "project_id": "photo", "session_id": "native-existing",
                                 "target_id": "photo", "voice_discussion": voice_discussion["enabled"]}
        if action == "read" and held is not None and len(held) < 1 and native.get("_hold"):
            held.append(route)
            return
        route.fulfill(json={k: v for k, v in native.items() if not k.startswith("_")})

    page.route("**/api/agent/workspace/native/*", respond)
    # 1. Workspace confirmed, nothing shown: no project route; Awake/standby is not listening.
    assert "Not sent to a project" in text(page, "workspace-mode-route")
    assert "Not listening" in text(page, "workspace-mode-voice")

    # 2. Connect is the conversation display only, not a project connection.
    page.wait_for_function("!document.getElementById('workspace-start').disabled")
    page.click("#workspace-start:visible, #workspace-project-connect:visible")
    page.wait_for_function("document.getElementById('workspace-badge').textContent === 'Live'")
    page.wait_for_function("!document.getElementById('workspace-native-bind').disabled")
    assert text(page, "workspace-mode-display") == "Connected"
    assert "Not open" in text(page, "workspace-mode-session")
    assert "Not sent to a project" in text(page, "workspace-mode-route")
    page.locator("#workspace-mode-card").screenshot(path=str(SHOTS / f"1-shown-unbound-{width}.png"))

    # 3. Bound, but the host has voice discussion off: connected, yet spoken requests are not sent.
    page.click("#workspace-native-bind")
    wait_text(page, "workspace-mode-session", "Open · hermes · photo")
    wait_text(page, "workspace-mode-route", "voice discussion is off")
    assert "spoken requests are not sent to it" in text(page, "workspace-native-bind-help")
    page.locator("#workspace-mode-card").screenshot(path=str(SHOTS / f"2-bound-voice-off-{width}.png"))

    # 4. Bound with host voice discussion: spoken requests go to the session; listening is separate.
    voice_discussion["enabled"] = True
    page.evaluate("HomebodyWorkspace.pane('project')")  # opening a project lands on Chat on phones
    page.click("#workspace-native-bind")
    wait_text(page, "workspace-mode-route", "Your project session · hermes · photo")
    assert "Spoken Workspace requests go to this session." in text(page, "workspace-native-bind-help")
    assert "Not listening" in text(page, "workspace-mode-voice")
    runtime["state"] = "listening"
    page.evaluate("refreshStatus()")
    wait_text(page, "workspace-mode-voice", "Listening now")
    page.locator("#workspace-mode-card").screenshot(path=str(SHOTS / f"3-bound-voice-listening-{width}.png"))
    page.evaluate("HomebodyWorkspace.pane('project')")
    page.locator("#workspace-pane-project").screenshot(path=str(SHOTS / f"4-native-help-{width}.png"))
    runtime["state"] = "waiting_for_wake_word"

    # 5. Clear disconnects; a late read answered after Clear cannot repaint a bound route.
    native["_hold"] = True
    page.evaluate("HomebodyNativeWorkspace.update(true)")
    page.wait_for_timeout(80)
    assert held, "read poll was not issued"
    page.evaluate("HomebodyWorkspace.pane('project')")
    page.click("#workspace-clear")
    with page.expect_response(lambda response: response.url.endswith('/api/agent/workspace/clear')
                              and response.request.method == 'POST') as cleared:
        page.click("#workspace-clear-confirm-button")
    assert cleared.value.ok
    # A normal display poll may replace the one-shot Clear explanatory message.
    # Assert the acknowledged operation and durable disconnected state instead.
    page.wait_for_function("document.getElementById('panel-agent').dataset.connection === 'off'")
    held[0].fulfill(json={k: v for k, v in native.items() if not k.startswith("_")})
    page.wait_for_timeout(80)
    assert "Not sent to a project" in text(page, "workspace-mode-route")
    assert "native-existing" not in text(page, "workspace-native-identity")
    page.locator("#workspace-mode-card").screenshot(path=str(SHOTS / f"5-after-clear-{width}.png"))

    # 6. Reopen: the server cleared the binding, so the route stays "not sent" until Open project.
    native.update(binding=None, _hold=False)
    page.click("#workspace-start:visible, #workspace-project-connect:visible")
    page.wait_for_function("document.getElementById('workspace-badge').textContent === 'Live'")
    page.wait_for_function("!document.getElementById('workspace-native-bind').disabled")
    assert "Not sent to a project" in text(page, "workspace-mode-route")

    # 7. Owner loss hides project facts; no private identity reaches browser storage.
    page.evaluate("HomebodyAssistantModes && HomebodyNativeWorkspace.reset()")
    persisted = page.evaluate("JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})")
    assert "native-existing" not in persisted and "photo" not in persisted
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


def test_face_status_is_requested_not_running_and_failure_is_explicit(route_browser):
    page, _display, runtime, errors = route_browser
    runtime.update(face_tracking_state="requested", face_model_app_cache="missing")
    page.evaluate("refreshStatus()")
    wait_text(page, "face-tracking-status", "not confirmed running")
    status = page.locator("#face-tracking-status").text_content()
    assert "No face in view is not a failure" in status and "not in this app's cache" in status
    assert "running." not in status.replace("not confirmed running", "")
    page.evaluate("document.getElementById('tab-settings').click()")  # behind More on phones
    choose(page,"everyday")
    assert page.locator("#settings-everyday").is_visible()
    target = page.locator("#face-tracking-status")
    target.wait_for(state="visible")
    for width in (390, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        target.scroll_into_view_if_needed()
        page.screenshot(path=str(SHOTS / f"6-face-requested-{width}.png"))
    runtime.update(face_tracking_state="command_failed")
    page.evaluate("refreshStatus()")
    wait_text(page, "face-tracking-status", "Could not reach the robot daemon")
    assert "error" in page.locator("#face-tracking-status").get_attribute("class")
    runtime.update(face_tracking_state="off", face_model_app_cache="verified")
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('face-tracking-status').textContent === 'Off.'")
    assert not errors
