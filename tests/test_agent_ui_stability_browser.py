"""Real Chromium: repository picker stability under normal polling, and unambiguous Agent mode.

Every API reply is an explicit fixture; nothing contacts Reachy, Hermes, GitHub or a model provider.
"""

import json
from pathlib import Path

import pytest
import test_assistant_modes_browser as mode_fixtures
import test_workspace_browser as fixtures

picker_browser = fixtures.workspace_browser
ui = mode_fixtures.ui
SHOTS = Path(".artifacts/agent-ui-fix")
TARGET = {"target_id": "homebody-hermes", "title": "Homebody", "backend": "hermes", "session_id": "native-fixture"}
BINDING = {
    "target_id": "homebody-hermes",
    "project_id": "homebody",
    "backend": "hermes",
    "session_id": "native-fixture",
}


def _catalog(*names):
    return {
        "available": True,
        "owner": "Timverhoogt",
        "checked_at": 1,
        "repositories": [
            {
                "repository": f"Timverhoogt/{name}",
                "private": name != "homebody",
                "archived": False,
                "target_ids": ["homebody-hermes"] if name == "homebody" else [],
            }
            for name in names
        ],
    }


# Records every DOM mutation (option replacement, disabled/value flips) on the two native selects and
# tags each option node so identity can be compared across polls. Screenshots cannot prove this.
WATCH = """() => {
  window.__pickerLog = [];
  let n = 0;
  for (const id of ['workspace-repository', 'workspace-native-target']) {
    const select = document.getElementById(id);
    [...select.options].forEach(option => { option.__tag = `${id}:${++n}`; });
    new MutationObserver(records => records.forEach(r => window.__pickerLog.push(
      `${id}:${r.type}:${r.attributeName || ''}`))).observe(select,
      {attributes: true, childList: true, subtree: true, characterData: true});
  }
  window.__pickerSamples = [];
  window.__pickerSampler = setInterval(() => {
    const s = document.getElementById('workspace-repository');
    window.__pickerSamples.push({disabled: s.disabled, value: s.value, focused: document.activeElement === s});
  }, 5);
}"""
TAGS = "id => [...document.getElementById(id).options].map(o => o.__tag || 'replaced')"


@pytest.fixture
def picker(picker_browser):
    page, voice, runtime, errors = picker_browser
    runtime["agent"] = {"profile": "agent", "activity": [], "pending_approval": False}
    state = {
        "native": {"targets": [TARGET], "binding": None, "run": {}, "pending": None, "messages": [], "events": []},
        "catalog": _catalog("homebody", "private-project"),
        "calls": [],
        "hold": set(),
        "held": [],
        "fail": set(),
    }

    def reply(route):
        action = route.request.url.rsplit("/", 1)[-1]
        body = json.loads(route.request.post_data or "{}")
        state["calls"].append((action, body))
        if action in state["hold"]:
            state["held"].append((action, route))
            return
        if action in state["fail"]:
            route.fulfill(status=502, json={"detail": "Native connection unavailable"})
            return
        if action == "bind":
            state["native"]["binding"] = dict(BINDING)
        route.fulfill(json=state["catalog"] if action == "repositories" else state["native"])

    page.route("**/api/agent/workspace/native/*", reply)
    page.click("#workspace-start")
    page.wait_for_function("document.getElementById('workspace-repository').options.length === 3")
    page.wait_for_function("!document.getElementById('workspace-repository').disabled")
    yield page, voice, runtime, state, errors


def _reads(state):
    return sum(1 for action, _ in state["calls"] if action == "read")


def _poll(page, state, count=4):
    """Drive status polls plus explicit native refreshes until `count` more native reads answer.

    The native lane now polls on its own slower cadence, so each round also requests a read directly."""
    goal = _reads(state) + count
    for _ in range(400):
        if _reads(state) >= goal:
            break
        page.evaluate("refreshStatus(); HomebodyNativeWorkspace.update(true)")
        page.wait_for_timeout(20)
    assert _reads(state) >= goal
    page.wait_for_timeout(60)


def test_identical_polls_never_touch_open_repository_select(picker):
    page, _voice, _runtime, state, errors = picker
    page.select_option("#workspace-repository", "Timverhoogt/homebody")
    page.focus("#workspace-repository")
    page.evaluate(WATCH)
    before = {sel: page.evaluate(TAGS, sel) for sel in ["workspace-repository", "workspace-native-target"]}
    _poll(page, state, 6)
    log = page.evaluate("window.__pickerLog")
    samples = page.evaluate("clearInterval(window.__pickerSampler), window.__pickerSamples")
    assert log == [], log  # no option replacement and no disabled/enabled oscillation
    assert samples and all(not s["disabled"] and s["value"] == "Timverhoogt/homebody" for s in samples), samples
    assert all(s["focused"] for s in samples)
    after = {sel: page.evaluate(TAGS, sel) for sel in ["workspace-repository", "workspace-native-target"]}
    assert after == before
    assert page.locator("#workspace-native-bind").is_enabled()
    assert not any(action in {"bind", "prepare", "approve"} for action, _ in state["calls"])
    assert not errors


def test_slow_read_keeps_picker_usable_and_write_supersedes_it(picker):
    page, _voice, _runtime, state, errors = picker
    page.select_option("#workspace-repository", "Timverhoogt/homebody")
    page.evaluate(WATCH)
    state["hold"].add("read")
    held_before = len(state["held"])
    for _ in range(200):
        if len(state["held"]) > held_before:
            break
        page.evaluate("refreshStatus(); HomebodyNativeWorkspace.update(true)")
        page.wait_for_timeout(20)
    assert len(state["held"]) > held_before  # a read is genuinely in flight
    assert page.locator("#workspace-repository").is_enabled()
    assert page.locator("#workspace-native-target").is_enabled()
    assert page.locator("#workspace-native-stop").is_disabled()  # a poll is not cancellable work
    assert page.evaluate("window.__pickerLog") == []
    # A real write is not silently dropped behind the poll; the poll's late answer cannot repaint over it.
    page.click("#workspace-native-bind")
    page.wait_for_function("!document.getElementById('workspace-native-prepare').disabled")
    assert "native-fixture" in page.inner_text("#workspace-native-identity")
    stale = {**state["native"], "binding": None}
    for action, route in state["held"]:
        route.fulfill(json=stale) if action == "read" else route.abort()
    page.wait_for_timeout(100)
    assert "native-fixture" in page.inner_text("#workspace-native-identity")
    assert page.locator("#workspace-native-prepare").is_enabled()
    assert [action for action, _ in state["calls"]].count("bind") == 1
    assert not errors


def test_failed_read_is_unconfirmed_not_bound_and_recovers(picker):
    page, _voice, _runtime, state, errors = picker
    page.select_option("#workspace-repository", "Timverhoogt/homebody")
    page.click("#workspace-native-bind")
    page.wait_for_function("!document.getElementById('workspace-native-prepare').disabled")
    state["fail"].add("read")
    page.evaluate("HomebodyNativeWorkspace.update(true)")
    page.wait_for_function(
        "document.getElementById('workspace-native-state').textContent.includes('may still be running')"
    )
    assert page.locator("#workspace-native-stop").is_enabled()
    assert page.locator("#workspace-native-prepare").is_disabled()
    assert page.locator("#workspace-native-bind").is_disabled()  # no write against an unconfirmed binding
    # A transient failure keeps the last confirmed content, visibly marked stale, instead of blanking it.
    assert page.locator("#workspace-stale").is_visible()
    assert page.locator("#workspace-timeline").get_attribute("data-stale") is not None
    assert page.locator("#workspace-repository").input_value() == "Timverhoogt/homebody"
    state["fail"].clear()
    _poll(page, state, 1)
    page.wait_for_function("!document.getElementById('workspace-native-prepare').disabled")
    assert "native-fixture" in page.inner_text("#workspace-native-identity")
    assert page.locator("#workspace-stale").is_hidden()
    assert not any(action in {"prepare", "approve"} for action, _ in state["calls"])
    assert not errors


def test_changed_catalog_preserves_selection_and_reload_is_truthful(picker):
    page, _voice, _runtime, state, errors = picker
    page.select_option("#workspace-repository", "Timverhoogt/private-project")
    state["catalog"] = _catalog("homebody", "private-project", "new-repo")
    state["hold"].add("repositories")
    page.click("#workspace-repositories-reload")
    page.wait_for_function("document.getElementById('workspace-repository').disabled")
    assert page.locator("#workspace-repositories-reload").is_disabled()
    reads = _reads(state)
    for _ in range(3):  # status polls during the refresh neither re-enable, repaint nor race it
        page.evaluate("refreshStatus()")
        page.wait_for_timeout(40)
        assert page.locator("#workspace-repository").is_disabled()
    assert _reads(state) == reads
    state["hold"].clear()
    state["held"].pop()[1].fulfill(json=state["catalog"])
    page.wait_for_function("document.getElementById('workspace-repository').options.length === 4")
    page.wait_for_function("!document.getElementById('workspace-repository').disabled")
    assert page.locator("#workspace-repository").input_value() == "Timverhoogt/private-project"
    assert "no project session is configured" in page.inner_text("#workspace-repository-state")
    assert page.locator("#workspace-native-bind").is_disabled()
    # The selected repository disappears from GitHub: the choice is cleared, never silently re-pointed.
    state["catalog"] = _catalog("homebody")
    page.click("#workspace-repositories-reload")
    page.wait_for_function("document.getElementById('workspace-repository').options.length === 2")
    assert page.locator("#workspace-repository").input_value() == ""
    assert not any(action in {"bind", "prepare", "approve"} for action, _ in state["calls"])
    assert not errors


def test_repository_switch_under_polls_never_reaches_old_binding(picker):
    page, _voice, _runtime, state, errors = picker
    page.select_option("#workspace-repository", "Timverhoogt/homebody")
    page.click("#workspace-native-bind")
    page.wait_for_function("!document.getElementById('workspace-native-prepare').disabled")
    page.evaluate("HomebodyWorkspace.pane('project')")  # opening a project lands on Chat on phones
    page.select_option("#workspace-repository", "Timverhoogt/private-project")
    _poll(page, state, 3)
    for control in ["prepare", "request", "approve", "bind"]:
        assert page.locator(f"#workspace-native-{control}").is_disabled(), control
    assert page.locator("#workspace-repository").input_value() == "Timverhoogt/private-project"
    assert page.locator("#workspace-native-target option").count() == 0
    assert "None open for this repository" in page.inner_text("#workspace-mode-session")
    assert not any(action in {"prepare", "approve"} for action, _ in state["calls"])
    assert not errors


def test_mode_switch_owner_loss_and_late_catalog_clear_picker(picker):
    page, voice, runtime, state, errors = picker
    state["hold"].add("repositories")
    page.click("#workspace-repositories-reload")
    page.wait_for_timeout(50)
    assert state["held"]
    runtime["agent"]["profile"] = "conversation"  # left Workspace elsewhere; the next poll confirms it
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('workspace-repository').options.length === 0")
    state["held"].pop()[1].fulfill(json=_catalog("homebody", "secret-project"))
    page.wait_for_timeout(100)
    assert page.locator("#workspace-repository option").count() == 0
    assert "secret-project" not in page.inner_text("body")
    assert page.locator("#workspace-mode-card").is_hidden()
    # Back in Workspace, then the owner session ends while a catalog reply is still in flight.
    runtime["agent"]["profile"] = "agent"
    page.evaluate("refreshStatus()")
    page.wait_for_timeout(100)
    # Back in Workspace the destination shows its project surface again (this fixture's display lease
    # stays on, so it reads again without a new Connect).
    page.locator("#agent-workspace-view").wait_for(state="visible")
    for _ in range(100):
        if any(action == "repositories" for action, _ in state["held"]):
            break
        page.evaluate("HomebodyNativeWorkspace.update(true)")
        page.wait_for_timeout(20)
    page.evaluate("HomebodyWorkspace.reset('Pair an owner device to see conversations.')")
    for _action, route in state["held"]:
        route.fulfill(json=_catalog("late-secret"))
    page.wait_for_timeout(100)
    assert page.locator("#workspace-repository option").count() == 0
    assert "late-secret" not in page.inner_text("body")
    stored = page.evaluate("JSON.stringify([{...localStorage}, {...sessionStorage}])")
    assert "secret" not in stored and "native-fixture" not in stored
    assert not errors


MODE = """() => ({
  mode: document.getElementById('panel-agent').dataset.assistantMode,
  current: document.getElementById('assistant-mode-current').textContent,
  badge: document.getElementById('agent-profile-badge').textContent,
  everyday: !document.getElementById('everyday-card').hidden,
  workspace: !document.getElementById('workspace-mode-card').hidden,
})"""


def _shot(page, name, below):
    """Settled shot of the mode card and the content beneath it; toasts are dismissed, not hidden by CSS."""
    size = page.viewport_size
    page.set_viewport_size({"width": size["width"], "height": 2600})  # whole card stack fits; no scrolling
    page.evaluate("scrollTo(0, 0)")
    page.wait_for_timeout(700)
    page.evaluate("document.querySelectorAll('.notification-dismiss').forEach(b => b.click())")
    page.wait_for_timeout(400)
    top = page.locator(".agent-card").bounding_box()
    bottom = page.locator(below).bounding_box()
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(SHOTS / f"{name}.png"),
        animations="disabled",
        clip={
            "x": 0,
            "y": top["y"] - 8,
            "width": size["width"],
            "height": bottom["y"] + bottom["height"] - top["y"] + 16,
        },
    )
    page.set_viewport_size(size)


@pytest.mark.parametrize("width", [320, 390, 1440])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_current_mode_is_unambiguous_and_shows_only_its_content(ui, width, scheme):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(color_scheme=scheme)
    page.evaluate("activateTab('agent', false, true)")  # navigation only; tab taps also enter Workspace
    page.wait_for_function("document.getElementById('panel-agent').dataset.assistantMode === 'everyday'")
    everyday = page.evaluate(MODE)
    assert everyday == {
        "mode": "everyday",
        "current": "Everyday",
        "badge": "Everyday",
        "everyday": True,
        "workspace": False,
    }, everyday
    assert page.locator(".assistant-mode-switch").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    SHOTS.mkdir(parents=True, exist_ok=True)
    _shot(page, f"everyday-{width}-{scheme}", "#workspace-gate")

    held = []
    page.route("**/api/agent/profile", lambda route: held.append(route))
    page.click("#agent-enable-button")
    page.wait_for_timeout(100)
    assert held
    pending = page.evaluate(MODE)
    # Not yet confirmed: still says Everyday is what is running, and never claims Workspace.
    assert pending["mode"] == "pending" and pending["workspace"] is False and pending["everyday"] is False, pending
    assert "Switching to Workspace" in pending["current"]
    page.evaluate("refreshStatus()")
    page.wait_for_timeout(100)
    assert page.evaluate(MODE)["mode"] == "pending"  # an ordinary poll does not end the pending switch
    page.unroute("**/api/agent/profile")
    body = json.loads(held[0].request.post_data)
    state["runtime"]["agent"]["profile"] = body["profile"]
    held[0].fulfill(json={"ok": True, "agent": state["runtime"]["agent"]})
    page.wait_for_function("document.getElementById('panel-agent').dataset.assistantMode === 'workspace'")
    workspace = page.evaluate(MODE)
    assert workspace == {
        "mode": "workspace",
        "current": "Workspace",
        "badge": "Workspace",
        "everyday": False,
        "workspace": True,
    }, workspace
    facts = page.locator("#workspace-mode-card").text_content()  # includes the collapsed Details
    assert page.locator("#agent-workspace-view").is_visible()  # entering Workspace shows the project surface
    assert "Not connected" in page.text_content("#workspace-mode-display")  # display is separate from mode
    assert "No project open" in page.inner_text("#workspace-mode-repository")
    # Display is off, so the binding was never read: not connected, rather than "none" or "open".
    assert "Not connected" in page.inner_text("#workspace-mode-session")
    assert "Project work running." in facts  # from the status-only badge fixture, not from selection
    assert page.locator(".assistant-mode-switch").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    _shot(page, f"workspace-{width}-{scheme}", "#workspace-mode-card")

    page.click("#tab-dashboard")
    assert page.evaluate(MODE)["mode"] == "workspace"  # navigation is not the mode switch
    page.evaluate("activateTab('agent', false, true)")  # navigation only; tab taps also enter Workspace

    page.route("**/api/status", lambda route: route.fulfill(status=503, json={}))
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('panel-agent').dataset.assistantMode === 'unknown'")
    offline = page.evaluate(MODE)
    assert offline["everyday"] is False and offline["workspace"] is False, offline
    assert "Workspace" not in offline["current"] and "Workspace" not in offline["badge"]
    # The last pressed button is no longer presented as a confirmed mode.
    assert page.locator(".assistant-mode-switch").get_attribute("data-confirmed") == "false"
    # Unconfirmed mode fails closed: no projects view switch, only Companion beneath.
    assert page.locator(".workspace-jump").is_hidden() and page.locator("#agent-workspace-view").is_hidden()
    _shot(page, f"offline-{width}-{scheme}", "#workspace-gate")
    assert body == {"profile": "agent", "preserve_work": False}
    assert not any(path.startswith("/api/agent/workspace") for path, _ in posts)  # mode entry starts no work
    assert not errors


def test_kids_and_owner_loss_never_claim_a_mode(ui):
    page, state, _posts, errors = ui
    page.evaluate("activateTab('agent', false, true)")  # navigation only; tab taps also enter Workspace
    state["runtime"]["agent"]["profile"] = "agent"
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('panel-agent').dataset.assistantMode === 'workspace'")
    page.evaluate("HomebodyAssistantModes.owner(false)")
    assert page.evaluate(MODE)["mode"] == "unknown"
    assert page.locator("#workspace-mode-card").is_hidden()
    page.evaluate("HomebodyAssistantModes.owner(true)")
    state["runtime"]["kids_mode"] = {"active": True}
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('panel-agent').dataset.assistantMode === 'unknown'")
    assert page.locator("#workspace-mode-card").is_hidden() and page.locator("#everyday-card").is_hidden()
    assert not errors


def test_owner_loss_clears_private_mode_summary_even_in_hidden_dom(picker):
    page, _voice, _runtime, _state, errors = picker
    page.select_option("#workspace-repository", "Timverhoogt/private-project")
    assert "private-project" in page.text_content("#workspace-mode-repository")
    page.route("**/api/agent/activity", lambda route: route.fulfill(status=401, json={}))
    page.evaluate("fetch('/api/agent/activity')")
    page.wait_for_function("HomebodyOwner.isOwner() === false")
    assert "private-project" not in page.text_content("#workspace-mode-card")
    assert page.evaluate(MODE)["mode"] == "unknown"
    assert not errors


def test_successful_switch_waits_for_status_not_an_old_inflight_read(ui):
    page, state, _posts, errors = ui
    page.evaluate("activateTab('agent', false, true)")  # navigation only; tab taps also enter Workspace
    page.wait_for_function("document.getElementById('panel-agent').dataset.assistantMode === 'everyday'")
    old = json.loads(json.dumps(state))
    status_reads, writes = [], []
    page.route("**/api/status", lambda route: status_reads.append(route))
    page.route("**/api/agent/profile", lambda route: writes.append(route))
    page.evaluate("void refreshStatus()")
    page.wait_for_timeout(100)
    assert status_reads
    page.click("#agent-enable-button")
    page.wait_for_timeout(100)
    assert writes
    state["runtime"]["agent"]["profile"] = "agent"
    writes[0].fulfill(json={"ok": True, "agent": state["runtime"]["agent"]})
    page.wait_for_timeout(100)
    assert page.evaluate(MODE)["mode"] == "pending"
    status_reads[0].fulfill(json=old)
    page.wait_for_timeout(100)
    assert page.evaluate(MODE)["mode"] == "pending"
    page.unroute("**/api/status")
    for route in status_reads[1:]:
        route.fulfill(json=state)
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('panel-agent').dataset.assistantMode === 'workspace'")
    assert page.locator("#everyday-card").is_hidden()
    assert not errors


@pytest.mark.parametrize("failure", [502, 200])
def test_catalog_failure_cannot_restore_old_binding_actions(picker, failure):
    page, _voice, _runtime, state, errors = picker
    page.select_option("#workspace-repository", "Timverhoogt/homebody")
    page.click("#workspace-native-bind")
    page.wait_for_function("!document.getElementById('workspace-native-request').disabled")
    page.evaluate("HomebodyWorkspace.pane('project')")  # opening a project lands on Chat on phones
    page.select_option("#workspace-repository", "Timverhoogt/private-project")
    page.route(
        "**/api/agent/workspace/native/repositories",
        lambda route: route.fulfill(
            status=failure, json={"available": False, "repositories": [], "error": "Unavailable"}
        ),
    )
    page.click("#workspace-repositories-reload")
    page.wait_for_function("!document.getElementById('workspace-repositories-reload').disabled")
    _poll(page, state, 2)
    for control in ["bind", "prepare", "request", "approve"]:
        assert page.locator(f"#workspace-native-{control}").is_disabled(), control
    assert page.locator("#workspace-native-target option").count() == 0
    assert page.evaluate("HomebodyNativeWorkspace.summary().session") != "bound"
    # Even a synthetic click cannot prepare work for the previously bound project.
    page.evaluate("document.getElementById('workspace-native-prepare').dispatchEvent(new Event('click'))")
    page.wait_for_timeout(100)
    assert not any(action in {"prepare", "approve"} for action, _ in state["calls"])
    state["native"]["run"] = {"status": "running", "run_id": "old-bound-run"}
    _poll(page, state, 1)
    assert page.locator("#workspace-native-stop").is_enabled()
    assert not errors


def test_removed_selected_repository_cannot_restore_old_binding_actions(picker):
    page, _voice, _runtime, state, errors = picker
    page.select_option("#workspace-repository", "Timverhoogt/homebody")
    page.click("#workspace-native-bind")
    page.wait_for_function("!document.getElementById('workspace-native-request').disabled")
    page.evaluate("HomebodyWorkspace.pane('project')")  # opening a project lands on Chat on phones
    page.select_option("#workspace-repository", "Timverhoogt/private-project")
    state["catalog"] = _catalog("homebody")
    page.click("#workspace-repositories-reload")
    page.wait_for_function("!document.getElementById('workspace-repositories-reload').disabled")
    assert page.locator("#workspace-repository").input_value() == ""
    assert page.locator("#workspace-native-target option").count() == 0
    assert page.locator("#workspace-native-prepare").is_disabled()
    assert page.evaluate("HomebodyNativeWorkspace.summary().session") != "bound"
    page.select_option("#workspace-repository", "Timverhoogt/homebody")
    assert page.locator("#workspace-native-prepare").is_enabled()
    assert not errors
