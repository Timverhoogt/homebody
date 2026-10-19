"""Real Chromium: the confirmed assistant mode decides which Agent surface exists.

Everyday never shows project pickers, request forms or advanced run panels; actual or uncertain
preserved work keeps a truthful badge and a reachable global Stop. Workspace explains connection,
display and voice separately; Clear states its full scope; late Agent replies cannot repaint after
a mode change. Every API reply is an explicit fixture; nothing contacts Reachy, Hermes, GitHub or a
model provider.
"""

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

SHOTS = Path(__file__).resolve().parents[1] / ".artifacts" / "agent-ui-boundaries"
CAPABILITIES = {
    "available": True, "error": "",
    "text_provider": {"name": "openai", "label": "OpenAI", "region": "global", "model": "gpt-4.1-mini"},
    "weather": {"source": "home_assistant", "label": "Home Assistant weather entity", "location_leaves_home": False},
    "devices": {"controls": ["desk light"], "status": ["desk light"]},
    "time_source": "hermes_host_clock",
}
TARGET = {"target_id": "homebody-hermes", "title": "Homebody", "backend": "hermes", "session_id": "native-fixture"}
BINDING = {"target_id": "homebody-hermes", "project_id": "homebody", "backend": "hermes",
           "session_id": "native-fixture"}
CATALOG = {
    "available": True, "owner": "Timverhoogt", "checked_at": 1,
    "repositories": [
        {"repository": "Timverhoogt/homebody", "private": False, "archived": False, "target_ids": ["homebody-hermes"]},
        {"repository": "Timverhoogt/unset-project", "private": True, "archived": False, "target_ids": []},
    ],
}
AGENT_POLLS = ("/api/agent/activity", "/api/agent/run/current", "/api/agent/run/status", "/api/agent/pending-approval")
# Everything that belongs to the Workspace project surface; none of it may be visible outside Workspace.
PROJECT_SURFACE = ["#agent-workspace-view", ".workspace-segments", "#workspace-mode-card", "#workspace-repository",
                   "#workspace-native-request", ".agent-advanced-actions", "#agent-approval-sheet",
                   ".agent-session-details", "#workspace-start"]


@pytest.fixture
def agent():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        state = {
            "config": {"conversation_mode": "pipeline", "assistant_mode": "everyday",
                       "bridge_url": "http://hermes.test:8643", "api_key": "********"},
            "runtime": {"state": "waiting_for_wake_word", "power_mode": "awake", "kids_mode": {},
                        "agent": {"profile": "conversation", "recent_activity": []}},
            "status_code": 200,
            "badge": {"status": "idle", "running": False}, "badge_code": 200,
            "display": {"enabled": False, "generation": 1, "events": []},
            "native": {"targets": [TARGET], "binding": None, "run": {}, "pending": None, "messages": [], "events": []},
            "approval": None, "run": None,
            "requests": [], "hold": set(), "held": [],
        }

        def respond(route):
            request = route.request
            path = urlparse(request.url).path
            if path.startswith("/api/"):
                state["requests"].append((request.method, path))
            if path in state["hold"]:
                state["held"].append((path, route))
                return
            if path.startswith("/static/"):
                target = Path("homebody") / path.lstrip("/")
                route.fulfill(path=str(target)) if target.exists() else route.fulfill(status=404)
            elif path == "/":
                route.fulfill(path="homebody/static/index.html", content_type="text/html")
            elif path == "/api/owner/session":
                route.fulfill(json={"owner": True, "name": "Browser fixture", "csrf": "fixture"})
            elif path == "/api/status":
                route.fulfill(status=state["status_code"],
                              json={"config": state["config"], "runtime": state["runtime"]})
            elif path == "/api/everyday/capabilities":
                route.fulfill(json=CAPABILITIES)
            elif path == "/api/agent/workspace/native-badge":
                route.fulfill(status=state["badge_code"], json=state["badge"])
            elif path == "/api/agent/profile":
                state["runtime"]["agent"]["profile"] = json.loads(request.post_data)["profile"]
                route.fulfill(json={"ok": True, "agent": state["runtime"]["agent"]})
            elif path in ("/api/agent/stop", "/api/voice/stop"):
                route.fulfill(json={"ok": True, "agent": state["runtime"]["agent"]})
            elif path.startswith("/api/agent/workspace/native/"):
                action = path.rsplit("/", 1)[-1]
                if action == "repositories":
                    route.fulfill(json=CATALOG)
                    return
                if action == "bind":
                    state["native"]["binding"] = dict(BINDING)
                route.fulfill(json=state["native"])
            elif path.startswith("/api/agent/workspace"):
                if path.endswith("/start"):
                    state["display"]["enabled"] = True
                elif path.endswith("/clear"):  # backend: clear display, stop and detach native work
                    state["display"].update(enabled=False, events=[], generation=state["display"]["generation"] + 1)
                    state["native"]["binding"] = None
                route.fulfill(json=state["display"])
            elif path == "/api/agent/activity":
                route.fulfill(json={"activity": []})
            elif path == "/api/agent/pending-approval":
                route.fulfill(json={"pending_approval": state["approval"]})
            elif path.startswith("/api/agent/run/"):
                route.fulfill(json={"run": state["run"]})
            else:
                route.fulfill(json={})

        page.route("**/*", respond)
        page.goto("http://homebody.test/")
        page.wait_for_function("!document.querySelector('main').hidden && typeof updateStatus === 'function'")
        page.evaluate("activateTab('agent', false, true)")  # navigation only; tab taps also enter Workspace
        page.wait_for_function("document.getElementById('panel-agent').dataset.assistantMode === 'everyday'")
        yield page, state, errors
        browser.close()


def _mode(page, mode):
    page.wait_for_function(f"document.getElementById('panel-agent').dataset.assistantMode === '{mode}'")


def _enter_workspace(page, state):
    page.evaluate("document.getElementById('agent-enable-button').click()")  # the route call behind the Workspace tab
    _mode(page, "workspace")
    page.locator("#agent-workspace-view").wait_for(state="visible")


def _visible_project_surface(page):
    return [selector for selector in PROJECT_SURFACE if page.locator(selector).first.is_visible()]


def _try_to_reveal(page):
    """Every path that previously revealed project content, including synthetic clicks on hidden buttons."""
    page.evaluate("""() => {
      HomebodyWorkspace.show();
      activateTab('agent');
      currentAgentRun = {status: 'waiting_approval', steps: [], active_step_id: ''};
      renderAgentRun();
      currentAgentRun = null;
    }""")
    page.wait_for_timeout(100)


def _shot(page, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.evaluate("document.querySelectorAll('.notification-dismiss').forEach(b => b.click())")
    page.wait_for_timeout(300)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True, animations="disabled")


@pytest.mark.parametrize("width", [390, 1440])
def test_workspace_to_everyday_leaves_only_the_everyday_surface(agent, width):
    page, state, errors = agent
    page.set_viewport_size({"width": width, "height": 900})
    _enter_workspace(page, state)
    page.click("#workspace-start")
    page.wait_for_function("!document.getElementById('workspace-repository').disabled")
    assert page.locator("#workspace-repository").is_visible()
    page.evaluate("document.getElementById('agent-conversation-button').click()")  # behind the Everyday tab
    _mode(page, "everyday")
    page.locator("#agent-workspace-view").wait_for(state="hidden")
    assert _visible_project_surface(page) == []
    assert page.locator("#workspace-gate").is_visible()  # the destination offers one way back in
    page.click("#tab-dashboard")
    assert _visible_project_surface(page) == []
    assert page.locator("#everyday-card").is_visible()
    assert page.locator("#agent-companion-view").is_visible()  # existing Companion presence stays
    assert page.locator("#voice-stop-button").is_visible() and page.locator("#voice-stop-button").is_enabled()
    _try_to_reveal(page)
    assert _visible_project_surface(page) == []
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    _shot(page, f"everyday-after-workspace-{width}")
    assert not errors


@pytest.mark.parametrize("boundary", ["pending", "offline", "owner", "kids"])
def test_unconfirmed_states_fail_closed_on_project_content(agent, boundary):
    page, state, errors = agent
    _enter_workspace(page, state)
    assert page.locator("#workspace-start").is_visible()
    if boundary == "pending":
        state["hold"].add("/api/agent/profile")
        page.evaluate("document.getElementById('agent-conversation-button').click()")  # behind the Everyday tab
        _mode(page, "pending")
    elif boundary == "offline":
        state["status_code"] = 503
        page.evaluate("refreshStatus()")
        _mode(page, "unknown")
    elif boundary == "owner":
        page.evaluate("HomebodyAssistantModes.owner(false)")
        _mode(page, "unknown")
    else:
        state["runtime"]["kids_mode"] = {"active": True}
        page.evaluate("refreshStatus()")
        _mode(page, "unknown")
    assert _visible_project_surface(page) == []
    _try_to_reveal(page)
    assert _visible_project_surface(page) == []
    assert page.locator("#agent-workspace-view").is_hidden()
    assert not errors


def test_pending_entry_reveals_projects_only_after_confirmation(agent):
    page, state, errors = agent
    state["hold"].add("/api/agent/profile")
    page.evaluate("document.getElementById('agent-enable-button').click()")  # the route call behind the Workspace tab
    _mode(page, "pending")
    _try_to_reveal(page)
    assert _visible_project_surface(page) == []
    state["hold"].clear()
    path, route = state["held"].pop()
    state["runtime"]["agent"]["profile"] = "agent"
    route.fulfill(json={"ok": True, "agent": state["runtime"]["agent"]})
    _mode(page, "workspace")
    page.locator("#agent-workspace-view").wait_for(state="visible")
    # Mode entry opens no session and no display: those stay explicit owner actions.
    assert not any(p.startswith("/api/agent/workspace/") and m == "POST" for m, p in state["requests"]
                   if p.rsplit("/", 1)[-1] in {"start", "bind"})
    assert not errors


def test_everyday_keeps_a_truthful_badge_and_global_stop_for_real_or_uncertain_work(agent):
    page, state, errors = agent

    def rebadge():
        page.evaluate("HomebodyAssistantModes.owner(false); HomebodyAssistantModes.owner(true); refreshStatus()")

    rebadge()
    page.wait_for_function("document.getElementById('agent-stop-button').hidden")
    assert page.locator("#native-work-badge").is_hidden()  # confirmed idle: no idle work controls
    state["badge"] = {"status": "running", "running": True}
    rebadge()
    page.wait_for_function("!document.getElementById('native-work-badge').hidden")
    assert page.text_content("#native-work-badge") == "Project work running"
    assert page.locator("#agent-stop-button").is_visible() and page.locator("#agent-stop-button").is_enabled()
    page.click("#agent-stop-button")
    page.wait_for_function("document.getElementById('agent-message').textContent.includes('stopped')")
    assert ("POST", "/api/agent/stop") in state["requests"]
    state["badge_code"] = 502
    rebadge()
    page.wait_for_function("document.getElementById('native-work-badge').textContent.includes('unconfirmed')")
    assert page.locator("#native-work-badge").is_visible()
    assert page.locator("#agent-stop-button").is_visible() and page.locator("#agent-stop-button").is_enabled()
    assert _visible_project_surface(page) == []
    _shot(page, "everyday-uncertain-work-390")
    assert not errors


def _open_project(page):
    page.click("#workspace-start")
    page.wait_for_function("document.getElementById('workspace-repository').options.length === 3")
    page.select_option("#workspace-repository", "Timverhoogt/homebody")
    page.click("#workspace-native-bind")
    page.wait_for_function("!document.getElementById('workspace-native-request').disabled")


def test_clear_is_confirmed_names_its_scope_and_asks_to_reopen(agent):
    page, state, errors = agent
    _enter_workspace(page, state)
    _open_project(page)
    page.click("#ws-seg-project")  # phones show one Workspace pane at a time
    page.click("#workspace-clear")
    confirm = page.locator("#workspace-clear-confirm")
    assert confirm.is_visible()
    scope = confirm.inner_text()
    assert "stop" in scope.lower() and "disconnect" in scope.lower()
    assert not any(p.endswith("/clear") for _m, p in state["requests"])
    page.click("#workspace-clear-cancel")
    assert confirm.is_hidden()
    assert "native-fixture" in page.inner_text("#workspace-native-identity")
    assert not any(p.endswith("/clear") for _m, p in state["requests"])
    page.click("#workspace-clear")
    page.click("#workspace-clear-confirm-button")
    page.wait_for_function("document.getElementById('workspace-state').textContent.includes('Open project again')")
    assert [p for _m, p in state["requests"] if p.endswith("/clear")] == ["/api/agent/workspace/clear"]
    assert confirm.is_hidden()
    assert "native-fixture" not in page.inner_text("#workspace-native-identity")
    assert not errors


@pytest.mark.parametrize("width", [390, 1440])
def test_workspace_guidance_separates_display_connection_and_voice(agent, width):
    page, state, errors = agent
    page.set_viewport_size({"width": width, "height": 900})
    _enter_workspace(page, state)
    # Display, project connection and voice are separate header facts; Connect is the one display step.
    # Display, route and voice facts sit in the header's Details disclosure: read their content.
    assert page.text_content("#workspace-mode-display") == "Not connected"
    assert page.inner_text("#workspace-start") == "Connect"
    assert "Not connected" in page.inner_text("#workspace-mode-session")
    voice = page.text_content("#workspace-mode-voice")
    assert "Hey Homebody" in voice and "Listening" not in voice  # waiting for wake word is not listening
    page.click("#workspace-start")
    page.wait_for_function("document.getElementById('workspace-repository').options.length === 3")
    assert "Connected" in page.text_content("#workspace-mode-display")
    session = page.inner_text("#workspace-mode-session")
    assert "Not open" in session and "Open ·" not in session  # display consent is not a connection
    # Unconfigured repository: the disabled action explains the exact setup that is missing.
    page.select_option("#workspace-repository", "Timverhoogt/unset-project")
    assert page.locator("#workspace-native-bind").is_disabled()
    help_text = page.inner_text("#workspace-native-bind-help")
    assert "Setup needed" in help_text and "Timverhoogt/unset-project" in help_text
    assert page.get_attribute("#workspace-native-bind", "aria-describedby") == "workspace-native-bind-help"
    assert "Connection unavailable" not in page.inner_text("#workspace-native-state")
    _shot(page, f"workspace-setup-needed-{width}")
    page.select_option("#workspace-repository", "Timverhoogt/homebody")
    assert page.locator("#workspace-native-bind").is_enabled()
    assert "Setup needed" not in page.inner_text("#workspace-native-bind-help")
    page.click("#workspace-native-bind")
    page.wait_for_function("document.getElementById('workspace-mode-session').textContent.startsWith('Open ·')")
    state["runtime"]["state"] = "listening"
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('workspace-mode-voice').textContent.startsWith('Listening')")
    _shot(page, f"workspace-open-{width}")
    assert not errors


def test_labels_name_homebody_not_full_hermes(agent):
    page, state, errors = agent
    assert "Hermes" not in page.inner_text(".voice-route")
    _enter_workspace(page, state)
    assert "talk to Hermes" not in page.inner_text("#agent-workspace-view")
    assert "with Hermes" not in page.inner_text("#workspace-mode-card")
    page.click("#workspace-start")
    state["display"]["events"] = [{"id": 1, "role": "user", "text": "Hi"},
                                  {"id": 2, "role": "assistant", "text": "Hello"}]
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.querySelectorAll('#workspace-timeline .workspace-event').length === 2")
    assert page.text_content("#workspace-timeline .workspace-assistant strong") == "Homebody"
    page.evaluate("document.getElementById('agent-conversation-button').click()")  # behind the Everyday tab
    _mode(page, "everyday")
    assert "Hermes" not in page.inner_text("#assistant-mode-detail")
    assert not errors


def test_agent_polls_stop_during_switch_and_late_replies_cannot_repaint(agent):
    page, state, errors = agent
    _enter_workspace(page, state)
    state["hold"].add("/api/agent/run/current")
    page.evaluate("void refreshAgentActivity()")
    page.wait_for_timeout(100)
    assert state["held"], "a Workspace poll is genuinely in flight"
    state["hold"] = {"/api/agent/profile"}
    page.evaluate("document.getElementById('agent-conversation-button').click()")  # behind the Everyday tab
    _mode(page, "pending")
    mark = len(state["requests"])
    page.evaluate("void refreshAgentActivity()")  # the interval firing during the switch
    page.wait_for_timeout(150)
    assert not [p for _m, p in state["requests"][mark:] if p in AGENT_POLLS]
    state["hold"] = set()
    state["approval"] = {"capability_id": "set_timer", "arguments": {"seconds": 60}, "expires_in_seconds": 30}
    state["runtime"]["agent"]["profile"] = "conversation"
    for path, route in list(state["held"]):
        if path == "/api/agent/profile":
            route.fulfill(json={"ok": True, "agent": state["runtime"]["agent"]})
    _mode(page, "everyday")
    late = {"run_id": "late", "status": "waiting_approval", "active_step_id": "step-1",
            "steps": [{"step_id": "step-1", "status": "waiting_approval", "capability_id": "set_timer"}]}
    for path, route in list(state["held"]):
        if path == "/api/agent/run/current":
            route.fulfill(json={"run": late})
    page.wait_for_timeout(200)
    page.evaluate("void refreshAgentActivity()")
    page.wait_for_timeout(150)
    assert not [p for _m, p in state["requests"][mark:] if p in AGENT_POLLS]
    assert _visible_project_surface(page) == []
    assert page.text_content("#agent-run-badge") != "waiting approval"
    assert not errors


def test_project_connection_copy_does_not_promise_backend_tool_permissions(agent):
    page, state, errors = agent
    _enter_workspace(page, state)
    page.click("#workspace-start")
    page.wait_for_function("document.getElementById('workspace-native-target').options.length === 1")
    help_text = page.inner_text("#workspace-native-bind-help")
    assert "does not submit work" in help_text
    assert "Nothing runs until" not in help_text
    assert "not enforce read-only" in page.inner_text("#workspace-native-permissions-note")
    assert "Discuss" not in page.get_attribute("#workspace-native-request", "placeholder")
    assert not errors


@pytest.mark.parametrize("boundary", ["owner", "offline", "kids"])
def test_late_agent_run_cannot_repaint_after_display_authority_is_lost(agent, boundary):
    page, state, errors = agent
    _enter_workspace(page, state)
    state["hold"].add("/api/agent/run/current")
    page.evaluate("void refreshAgentRun()")
    for _ in range(100):
        if any(path == "/api/agent/run/current" for path, _ in state["held"]):
            break
        page.wait_for_timeout(10)
    assert any(path == "/api/agent/run/current" for path, _ in state["held"])
    if boundary == "owner":
        state["status_code"] = 401
        page.evaluate("void refreshStatus()")
        page.wait_for_function("window.HomebodyOwner.isOwner() === false")
    elif boundary == "offline":
        page.evaluate("HomebodyAssistantModes.offline()")
    else:
        state["runtime"]["kids_mode"] = {"active": True}
        page.evaluate("payload => updateStatus(payload)", {"config": state["config"], "runtime": state["runtime"]})
    for path, route in list(state["held"]):
        if path == "/api/agent/run/current":
            route.fulfill(json={"run": {"run_id": "late-private", "status": "waiting_approval",
                                        "steps": [], "goal": "late private project data"}})
    page.wait_for_timeout(100)
    assert "late-private" not in page.text_content("#agent-run-budget")
    assert page.text_content("#agent-run-badge") != "waiting approval"
    assert not errors


def test_older_idle_badge_cannot_hide_stop_after_newer_running_badge(agent):
    page, state, errors = agent
    # Workspace keeps polling; unused Everyday intentionally performs discovery only.
    state["runtime"]["agent"]["profile"] = "agent"
    state["hold"].add("/api/agent/workspace/native-badge")
    page.evaluate("HomebodyAssistantModes.owner(false); HomebodyAssistantModes.owner(true); void refreshStatus()")
    for _ in range(100):
        if any(path.endswith("native-badge") for path, _ in state["held"]):
            break
        page.wait_for_timeout(10)
    assert any(path.endswith("native-badge") for path, _ in state["held"])
    older = [route for path, route in state["held"] if path.endswith("native-badge")][0]
    page.clock.install()
    page.clock.fast_forward(6000)
    page.evaluate("void refreshStatus()")
    for _ in range(100):
        if len([r for p, r in state["held"] if p.endswith("native-badge")]) >= 2:
            break
        page.wait_for_timeout(10)
    held = [r for p, r in state["held"] if p.endswith("native-badge")]
    assert len(held) >= 2
    held[-1].fulfill(json={"status": "running", "running": True})
    page.wait_for_function("document.getElementById('native-work-badge').textContent === 'Project work running'")
    older.fulfill(json={"status": "idle", "running": False})
    page.wait_for_timeout(100)
    assert page.locator("#agent-stop-button").is_visible()
    assert page.text_content("#native-work-badge") == "Project work running"
    assert not errors
