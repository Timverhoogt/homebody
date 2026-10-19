# ruff: noqa: F811 -- imported fixtures are intentionally test parameters
"""Workspace › Project flow as a person walks it: disconnected → Connect → repositories → Open project →
work, plus connecting, connection error, empty catalogue, refused route switch, and Clear & disconnect.
Real taps only (no JS clicks for user actions). Fixtures are local; nothing reaches a robot or GitHub.

Screenshots and a manifest (screen, state, theme, viewport, scroll, inspected) go to
HOMEBODY_TEST_SCREENSHOTS (default .artifacts/screenshots) / workspace-flow.
"""

import json
import os
from pathlib import Path

import pytest
from test_ui_shell_browser import CATALOG, native, shell  # noqa: F401

SHOTS = Path(os.getenv("HOMEBODY_TEST_SCREENSHOTS", ".artifacts/screenshots")) / "workspace-flow"
MANIFEST = SHOTS / "manifest.json"


def _record(page, screen, state, theme, scroll="top"):
    SHOTS.mkdir(parents=True, exist_ok=True)
    size = page.viewport_size
    name = f"{screen}-{state}-{size['width']}-{theme}-{scroll}"
    page.evaluate("() => new Promise(requestAnimationFrame)")
    page.screenshot(path=str(SHOTS / f"{name}.png"), animations="disabled")
    entries = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else []
    entries = [e for e in entries if e["file"] != f"{name}.png"]
    # A new capture is new evidence: never inherit inspection of older pixels.
    entries.append({"file": f"{name}.png", "screen": screen, "state": state, "theme": theme,
                    "viewport": f"{size['width']}x{size['height']}", "scroll": scroll,
                    "inspected": False})
    MANIFEST.write_text(json.dumps(sorted(entries, key=lambda e: e["file"]), indent=1))


def _no_overlap_with_segments(page):
    """Nothing but the segment bar is drawn at the bar's own centre line (it has a solid backdrop)."""
    return page.evaluate("""() => { const bar = document.querySelector('.workspace-segments');
      if (getComputedStyle(bar).position !== 'sticky') return true;
      const r = bar.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + 4, r.top + r.height / 2);
      return bar.contains(hit) || hit === bar; }""")


def _project(page):
    if page.locator("#ws-seg-project").is_visible():
        page.click("#ws-seg-project")


def _posts(page_requests, suffix):
    return [url for method, url in page_requests if method == "POST" and url.endswith(suffix)]


@pytest.mark.parametrize("width,theme", [(390, "dark"), (390, "light"), (1440, "light")])
def test_project_pane_walks_from_disconnected_to_open_project_with_single_requests(native, width, theme):
    page, voice, runtime, state, errors = native
    page.emulate_media(color_scheme=theme)
    page.set_viewport_size({"width": width, "height": 844})
    requests = []
    page.on("request", lambda request: requests.append((request.method, request.url)))
    # Disconnected: one sentence, a working Connect, no picker, no Open project, no Clear.
    _project(page)
    assert page.locator("#panel-agent").get_attribute("data-connection") == "off"
    connect = page.locator("#workspace-project-connect")
    assert connect.is_visible() and connect.is_enabled()
    assert page.locator("#workspace-start").is_hidden()
    assert page.locator("#workspace-mode-help").is_hidden()
    assert "Workspace" in page.locator("#assistant-mode-current").inner_text()
    # CSS display:grid used to revive this hidden gate, contradicting the confirmed route.
    assert page.locator("#workspace-gate").is_hidden()
    assert page.locator("#workspace-repository").is_hidden() and page.locator("#workspace-native-bind").is_hidden()
    # Clear keeps its cancellation scope in every state but reads as a quiet link while nothing is connected.
    clear = page.locator("#workspace-clear")
    assert clear.is_visible() and clear.evaluate("el => getComputedStyle(el).textDecorationLine") == "underline"
    assert page.locator("#workspace-native-identity").is_hidden()
    assert _no_overlap_with_segments(page)
    if width == 390:
        assert page.locator('.workspace-segments').evaluate(
            'el => getComputedStyle(el).position') not in ('sticky', 'fixed')
    _record(page, "project", "disconnected", theme)
    page.click(".workspace-header-more > summary")
    assert "Not sent to a project" in page.locator("#workspace-mode-route").inner_text()
    _record(page, "project", "disconnected-details", theme)
    page.click(".workspace-header-more > summary")
    page.click(".workspace-about > summary")
    _record(page, "project", "about", theme)
    page.click(".workspace-about > summary")
    for pane in ("chat", "work"):
        if page.locator(f"#ws-seg-{pane}").is_visible():
            page.click(f"#ws-seg-{pane}")
            assert page.locator("#workspace-start").is_visible()
            _record(page, pane, "disconnected", theme)
    _project(page)
    # Connect (real tap on the pane's button) → exactly one start request; repositories load.
    page.click("#workspace-project-connect")
    page.wait_for_function("document.getElementById('panel-agent').dataset.connection === 'on'")
    page.wait_for_function("document.getElementById('workspace-repository').options.length === 2")
    assert len(_posts(requests, "/api/agent/workspace/start")) == 1
    assert page.locator("#panel-agent").get_attribute("data-project") == "ready"
    assert page.locator("#workspace-project-connect").is_hidden()
    assert page.locator("#workspace-repository").is_visible()
    assert page.locator("#workspace-clear").evaluate("el => getComputedStyle(el).textDecorationLine") == "none"
    _record(page, "project", "repo-loaded", theme)
    # Choose and open: exactly one bind; the chip names the open project; Open project steps aside.
    page.select_option("#workspace-repository", "Timverhoogt/photo")
    page.select_option("#workspace-native-target", "photo")
    page.click("#workspace-native-bind")
    page.wait_for_function("document.getElementById('panel-agent').dataset.project === 'open'")
    assert state["calls"].count("bind") == 1
    _project(page)
    assert page.locator("#workspace-native-identity").is_visible()
    assert "photo" in page.locator("#workspace-native-identity").inner_text()
    assert page.locator("#workspace-native-bind").is_visible()  # still the way to switch sessions
    _record(page, "project", "project-open", theme)
    # Chat and Work in the open state.
    if page.locator("#ws-seg-chat").is_visible():
        page.click("#ws-seg-chat")
    page.wait_for_function("document.querySelectorAll('#workspace-timeline .workspace-event').length > 0")
    _record(page, "chat", "project-open", theme)
    if page.locator("#ws-seg-work").is_visible():
        page.click("#ws-seg-work")
    page.fill("#workspace-native-request", "Add an export button.")
    page.click("#workspace-native-prepare")
    page.wait_for_function("!document.getElementById('workspace-native-approval').hidden")
    _record(page, "work", "review", theme)
    page.click("#workspace-native-approve")
    page.wait_for_function("document.getElementById('workspace-native-state').dataset.status === 'running'")
    assert state["calls"].count("approve") == 1
    _record(page, "work", "active-work", theme)
    # Scrolled middle / bottom: the segment bar keeps its backdrop, the row never covers the last control.
    page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
    assert _no_overlap_with_segments(page)
    _record(page, "work", "active-work", theme, scroll="middle")
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    _record(page, "work", "active-work", theme, scroll="bottom")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    # Clear & disconnect from Project: confirm dialog, exactly one clear, back to disconnected.
    _project(page)
    page.click("#workspace-clear")
    assert page.locator("#workspace-clear-confirm").is_visible()
    _record(page, "project", "clear-dialog", theme)
    page.click("#workspace-clear-cancel")
    assert page.locator("#workspace-clear-confirm").is_hidden()
    page.click("#workspace-clear")
    page.click("#workspace-clear-confirm-button")
    page.wait_for_function("document.getElementById('panel-agent').dataset.connection === 'off'")
    assert len(_posts(requests, "/api/agent/workspace/clear")) == 1
    assert page.locator("#workspace-project-connect").is_visible()
    _record(page, "project", "disconnected-after-clear", theme)
    page.click("#agent-connection-badge")
    assert page.locator("#agent-connection-details").is_visible()
    _record(page, "settings", "agent-connection", theme, scroll="details")
    assert not errors


def test_connection_error_offers_retry_and_pending_sends_once(native):
    page, voice, runtime, state, errors = native
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(color_scheme="dark")
    _project(page)
    requests = []
    page.on("request", lambda request: requests.append((request.method, request.url)))
    page.route("**/api/agent/workspace/start",
               lambda route: route.fulfill(status=502, json={"detail": "Bridge unavailable"}))
    page.click("#workspace-project-connect")
    page.wait_for_function("document.getElementById('panel-agent').dataset.connection === 'error'")
    assert "Bridge unavailable" in page.locator("#workspace-state").inner_text()
    assert page.locator("#workspace-project-connect").inner_text().strip() == "Retry Connect"
    assert page.locator("#workspace-repository").is_hidden()
    _record(page, "project", "connect-error", "dark")
    # Pending: a held request shows Connecting… and a second tap does not send again.
    held = []
    page.unroute("**/api/agent/workspace/start")
    page.route("**/api/agent/workspace/start", lambda route: held.append(route))
    page.click("#workspace-project-connect")
    page.wait_for_function("document.getElementById('panel-agent').dataset.connection === 'pending'")
    assert page.locator("#workspace-project-connect").is_disabled()
    assert "Connecting" in page.locator("#workspace-project-connect").inner_text()
    _record(page, "project", "connecting", "dark")
    page.click("#workspace-project-connect", force=True)
    page.wait_for_timeout(200)
    assert len(_posts(requests, "/api/agent/workspace/start")) == 2  # the failed one and the held one
    held[0].fulfill(json={"enabled": True, "generation": 2, "events": [], "seconds_remaining": 3600})
    page.wait_for_function("document.getElementById('panel-agent').dataset.connection === 'on'")
    assert not errors


def test_connected_with_no_repositories_and_catalogue_error_are_explicit(native):
    page, voice, runtime, state, errors = native
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(color_scheme="light")
    _project(page)
    page.route("**/api/agent/workspace/native/repositories", lambda route: route.fulfill(
        json={**CATALOG, "repositories": []}))
    page.click("#workspace-project-connect")
    page.wait_for_function("document.getElementById('panel-agent').dataset.project === 'empty'")
    assert page.locator("#workspace-repository").is_hidden()
    assert page.locator("#workspace-repositories-reload").is_visible()
    assert "0 repositories" in page.locator("#workspace-repository-state").inner_text()
    assert page.locator("#workspace-native-bind").is_visible()  # sessions can exist without a repository filter
    _record(page, "project", "connected-empty", "light")
    page.unroute("**/api/agent/workspace/native/repositories")
    page.route("**/api/agent/workspace/native/repositories",
               lambda route: route.fulfill(status=502, json={"detail": "GitHub down"}))
    page.click("#workspace-repositories-reload")
    page.wait_for_function("document.getElementById('panel-agent').dataset.project === 'error'")
    assert "unavailable" in page.locator("#workspace-repository-state").inner_text()
    assert page.locator("#workspace-repositories-reload").is_visible()
    _record(page, "project", "catalogue-error", "light")
    page.unroute("**/api/agent/workspace/native/repositories")
    page.click("#workspace-repositories-reload")
    page.wait_for_function("document.getElementById('panel-agent').dataset.project === 'ready'")
    assert not errors


def test_refused_route_switch_stays_in_everyday_with_the_error_inline(native):
    page, voice, runtime, state, errors = native
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(color_scheme="dark")
    runtime["agent"]["profile"] = "conversation"
    page.route("**/api/agent/profile",
               lambda route: route.fulfill(status=409, json={"detail": "Stop the current turn first"}))
    page.evaluate("activateTab('dashboard'); refreshStatus()")
    page.wait_for_function("document.getElementById('panel-agent').dataset.assistantMode === 'everyday'")
    page.click("#tab-agent")
    page.wait_for_function("document.getElementById('agent-message').textContent.includes('Stop the current')")
    assert page.locator("#workspace-gate").is_visible()
    assert page.locator("#panel-agent").get_attribute("data-connection") == "off"
    _record(page, "gate", "refused-switch", "dark")
    assert not errors


def test_route_switch_success_is_inline_not_a_toast(native):
    page, voice, runtime, state, errors = native
    page.set_viewport_size({"width": 390, "height": 844})
    runtime["agent"]["profile"] = "conversation"
    page.route("**/api/agent/profile", lambda route: (runtime["agent"].update(profile="agent"),
                                                      route.fulfill(json={"ok": True, "agent": runtime["agent"]})))
    page.evaluate("activateTab('dashboard'); refreshStatus()")
    page.wait_for_function("document.getElementById('panel-agent').dataset.assistantMode === 'everyday'")
    page.click("#tab-agent")
    page.wait_for_function("document.getElementById('agent-message').textContent.includes('route active')")
    assert "Connect" in page.locator("#agent-message").inner_text()
    toasts = page.evaluate("[...document.querySelectorAll('.notifications .notification-text')]"
                           ".filter(n => n.textContent.includes('route active')).length")
    assert toasts == 0  # the route switch is not a connection: inline only, no toast over the controls
    assert not errors
