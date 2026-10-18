"""Owner-only repository picker, simulated GitHub/native responses; no robot or provider."""

import json
from pathlib import Path

import pytest
import test_workspace_browser as fixtures

repo_browser = fixtures.workspace_browser


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_repositories_select_without_launch_and_unconfigured_blocks_wrong_project(repo_browser, width):
    page, voice, runtime, errors = repo_browser
    runtime["agent"] = {"profile": "agent", "activity": [], "pending_approval": False}
    page.set_viewport_size({"width": width, "height": 1000})
    native = {
        "targets": [
            {"target_id": "homebody-hermes", "title": "Homebody", "backend": "hermes", "session_id": "native-fixture"}
        ],
        "binding": None,
        "run": {},
        "pending": None,
        "messages": [],
        "events": [],
    }
    catalog = {
        "available": True,
        "owner": "Timverhoogt",
        "checked_at": 1,
        "repositories": [
            {
                "repository": "Timverhoogt/homebody",
                "private": False,
                "archived": False,
                "target_ids": ["homebody-hermes"],
            },
            {"repository": "Timverhoogt/private-project", "private": True, "archived": False, "target_ids": []},
        ],
    }
    calls = []

    def reply(route):
        action = route.request.url.rsplit("/", 1)[-1]
        body = json.loads(route.request.post_data or "{}")
        calls.append((action, body))
        result = catalog if action == "repositories" else native
        if action == "bind":
            assert body == {"target_id": "homebody-hermes"}
            native["binding"] = {
                "target_id": "homebody-hermes",
                "project_id": "homebody",
                "backend": "hermes",
                "session_id": "native-fixture",
            }
        route.fulfill(json=result)

    page.route("**/api/agent/workspace/native/*", reply)
    assert page.locator("#tab-agent").inner_text() == "Workspace"
    page.click("#workspace-start")
    page.wait_for_function("document.getElementById('workspace-repository').options.length === 3")
    page.select_option("#workspace-repository", "Timverhoogt/private-project")
    assert "no project session is configured" in page.inner_text("#workspace-repository-state")
    assert page.locator("#workspace-native-bind").is_disabled()
    assert page.locator("#workspace-native-prepare").is_disabled()
    assert not any(c[0] in {"bind", "prepare", "approve"} for c in calls)
    page.select_option("#workspace-repository", "Timverhoogt/homebody")
    page.locator("#workspace-native-bind").scroll_into_view_if_needed()
    page.click("#workspace-native-bind")
    page.wait_for_function("!document.getElementById('workspace-native-prepare').disabled")
    page.evaluate("HomebodyWorkspace.pane('project')")  # opening a project lands on Chat on phones
    page.select_option("#workspace-repository", "Timverhoogt/private-project")
    assert page.locator("#workspace-native-prepare").is_disabled()
    assert page.locator("#workspace-native-request").is_disabled()
    assert page.locator("#workspace-native-approve").is_disabled()
    stored = page.evaluate("JSON.stringify([localStorage, sessionStorage])")
    assert "private-project" not in stored and "native-fixture" not in stored
    assert not any(c[0] in {"prepare", "approve"} for c in calls)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.select_option("#workspace-repository", "Timverhoogt/homebody")
    page.locator("#workspace-project-heading").scroll_into_view_if_needed()
    Path(".artifacts/repositories").mkdir(parents=True, exist_ok=True)
    page.screenshot(path=f".artifacts/repositories/picker-{width}.png")
    voice["enabled"] = False
    page.evaluate("HomebodyNativeWorkspace.reset()")
    assert page.locator("#workspace-repository option").count() == 0
    assert "private-project" not in page.inner_text("#workspace-repository-state")
    assert not errors


def test_late_private_catalog_cannot_reappear_after_clear(repo_browser):
    page, voice, runtime, errors = repo_browser
    runtime["agent"] = {"profile": "agent", "activity": [], "pending_approval": False}
    held = []

    def reply(route):
        if route.request.url.endswith("/repositories"):
            held.append(route)
        else:
            route.fulfill(
                json={"targets": [], "binding": None, "messages": [], "events": [], "run": {}, "pending": None}
            )

    page.route("**/api/agent/workspace/native/*", reply)
    with page.expect_request("**/api/agent/workspace/native/repositories"):
        page.click("#workspace-start")
    page.wait_for_timeout(100)
    assert held
    voice["enabled"] = False
    page.evaluate("HomebodyNativeWorkspace.reset()")
    held[0].fulfill(
        json={
            "available": True,
            "owner": "Timverhoogt",
            "repositories": [
                {"repository": "Timverhoogt/secret-project", "private": True, "archived": False, "target_ids": []}
            ],
        }
    )
    page.wait_for_timeout(100)
    assert page.locator("#workspace-repository option").count() == 0
    assert "secret-project" not in page.inner_text("body")
    assert not errors
