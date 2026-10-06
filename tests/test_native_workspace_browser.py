"""Real Chromium, explicitly simulated native host responses: no robot or model."""

import json
from pathlib import Path

import pytest
import test_workspace_browser as browser_fixtures

native_browser = browser_fixtures.workspace_browser


@pytest.mark.parametrize("width", [390, 1440])
def test_native_session_exact_approval_and_verified_receipt(native_browser, width):
    page, _voice, _runtime, errors = native_browser
    _runtime["agent"] = {"profile": "agent", "activity": [], "pending_approval": False}
    page.set_viewport_size({"width": width, "height": 900})
    fixture = {
        "targets": [
            {"target_id": "photo", "backend": "hermes", "title": "Raw photo project", "session_id": "native-existing"}
        ],
        "binding": None,
        "messages": [],
        "events": [],
        "pending": None,
        "run": {},
    }
    calls = []

    def native(route):
        action = route.request.url.rsplit("/", 1)[-1]
        body = json.loads(route.request.post_data or "{}")
        assert route.request.headers.get("x-homebody-csrf") == "fixture"
        calls.append((action, body))
        response = fixture
        if action == "bind":
            assert body == {"target_id": "photo"}
            fixture["binding"] = {"backend": "hermes", "project_id": "photo", "session_id": "native-existing"}
            fixture["messages"] = [{"role": "assistant", "text": "<img src=x onerror=alert(1)> native previous turn"}]
        elif action == "prepare":
            assert body == {"text": "Implement the discussed contract"}
            fixture["pending"] = {
                "approval_id": "exact-once",
                "request": body["text"],
                "session_id": "native-existing",
                "project_root": "/registered/project",
            }
            response = fixture["pending"]
        elif action == "approve":
            assert body == {"approval_id": "exact-once"}
            fixture.update(
                pending=None,
                run={"status": "running", "run_id": "real-native-fixture", "ready_to_test": False, "receipt": None},
            )
            response = fixture["run"]
        route.fulfill(json=response)

    page.route("**/api/agent/workspace/native/*", native)
    page.click("#agent-view-workspace")
    page.wait_for_function("!document.getElementById('workspace-start').disabled")
    page.click("#workspace-start")
    page.wait_for_function("!document.getElementById('workspace-native-bind').disabled")
    assert not any(action == "approve" for action, _body in calls)
    page.click("#workspace-native-bind")
    page.wait_for_function("!document.getElementById('workspace-native-request').disabled")
    assert "native-existing" in page.locator("#workspace-native-identity").inner_text()
    assert page.locator("#workspace-native-timeline img").count() == 0
    assert "native previous turn" in page.locator("#workspace-native-timeline").inner_text()
    page.fill("#workspace-native-request", "Implement the discussed contract")
    page.click("#workspace-native-prepare")
    page.wait_for_function("!document.getElementById('workspace-native-approval').hidden")
    assert "/registered/project" in page.locator("#workspace-native-scope").text_content()
    assert not any(action == "approve" for action, _body in calls)
    page.click("#workspace-native-approve")
    page.wait_for_function("document.getElementById('workspace-native-state').textContent.includes('running')")
    assert page.locator("#workspace-native-stop").is_enabled()
    assert len([c for c in calls if c[0] == "approve"]) == 1
    fixture["run"].update(status="completed", ready_to_test=True)
    page.evaluate("HomebodyNativeWorkspace.update(true)")
    page.wait_for_function("document.getElementById('workspace-native-state').textContent.includes('completed')")
    assert "Not verified" in page.locator("#workspace-native-state").inner_text()
    fixture["run"]["receipt"] = {
        "verified": True,
        "native_run_id": "real-native-fixture",
        "native_session_id": "native-existing",
    }
    page.evaluate("HomebodyNativeWorkspace.update(true)")
    page.wait_for_function("document.getElementById('workspace-native-state').textContent.startsWith('Ready to test')")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    Path(".pytest_cache/workspace-browser").mkdir(parents=True, exist_ok=True)
    page.screenshot(path=f".pytest_cache/workspace-browser/native-{width}.png", full_page=True)
    persisted = page.evaluate("JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})")
    assert all(text not in persisted for text in ["native-existing", "native previous turn", "discussed contract"])

    held = []
    page.route("**/api/agent/workspace/native/read", lambda route: held.append(route))
    page.evaluate("HomebodyNativeWorkspace.update(true)")
    page.wait_for_timeout(50)
    assert held
    page.evaluate("HomebodyNativeWorkspace.reset()")
    held[0].fulfill(json=fixture)
    page.wait_for_timeout(50)
    assert page.locator("#workspace-native-timeline li").count() == 0
    assert "native-existing" not in page.locator("#workspace-native-identity").inner_text()
    assert page.locator("#workspace-native-approve").is_disabled()
    assert not errors


def test_native_connection_unknown_keeps_stop_available(native_browser):
    page, _fixture, _runtime, errors = native_browser
    calls = []

    def fail(route):
        action = route.request.url.rsplit("/", 1)[-1]
        calls.append(action)
        if action == "cancel":
            route.fulfill(json={"status": "stopping", "run_id": "uncertain-native", "ready_to_test": False})
        else:
            route.fulfill(status=502, json={"detail": "Native connection unavailable"})

    page.route("**/api/agent/workspace/native/*", fail)
    page.click("#agent-view-workspace")
    page.wait_for_function("!document.getElementById('workspace-start').disabled")
    page.click("#workspace-start")
    page.wait_for_function(
        "document.getElementById('workspace-native-state').textContent.includes('may still be running')"
    )
    assert page.locator("#workspace-native-stop").is_enabled()
    page.click("#workspace-native-stop")
    page.wait_for_function("document.getElementById('workspace-native-state').textContent.includes('stopping')")
    assert "cancel" in calls
    assert not errors
