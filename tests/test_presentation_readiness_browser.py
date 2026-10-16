"""Real Chromium: greyed presentation Start explains what it needs and navigates, never enables.

Every API reply is an explicit fixture; nothing contacts Reachy, Hermes, a camera or a model.
"""

import json
from pathlib import Path

import pytest
from test_assistant_modes_browser import ui  # noqa: F401  (shared real-page fixture)

SHOTS = Path(__file__).resolve().parents[1] / ".artifacts" / "browser-screens" / "workspace-live-repair"


@pytest.fixture(autouse=True)
def open_legacy_companion_diagnostics(request):
    # These cases test the retained advanced detector, not the new primary one-look flow.
    page, *_ = request.getfixturevalue("ui")
    page.evaluate("document.getElementById('companion-advanced').open = true")


def needs(page):
    return page.eval_on_selector_all("#presentation-requirements li", "rows => rows.map(r => r.dataset.need)")


def refresh(page):
    page.evaluate("refreshStatus()")


def quiesce(page):
    """Stop the page's background poll at a moment no poll is in flight (one atomic JS check), so
    each later status request is one the test issued through the real ``refreshStatus``."""
    page.wait_for_function("!statusRefreshPending && (clearTimeout(statusPollTimer), true)")


def user_reported_state(state):
    """Shared physical context on (20 s), everything else at its default; Hermes reachable."""
    state["config"].update(
        shared_physical_context_enabled=True,
        presentation_window_seconds=20,
        proactive_presence_enabled=False,
        camera_enabled=False,
        initiative_policy_enabled=False,
        contextual_offers_enabled=False,
    )
    state["runtime"].update(
        power_mode="standby", motors_enabled=False, shared_physical_context={"state": "disabled", "reason": "none"}
    )


@pytest.mark.parametrize("width", [390, 1440])
def test_greyed_start_lists_unmet_requirements_and_navigates_without_writes(ui, width):  # noqa: F811
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 900})
    SHOTS.mkdir(parents=True, exist_ok=True)
    user_reported_state(state)
    page.click("#tab-agent")
    refresh(page)
    page.wait_for_function("document.querySelectorAll('#presentation-requirements li').length > 0")
    assert page.locator("#presentation-start").is_disabled()
    assert needs(page) == ["camera", "initiative", "offers", "workspace", "awake"]
    summary = page.locator("#presentation-readiness").inner_text()
    assert "5 more things" in summary and "Hermes backend does not make it ready" in summary
    assert "only while a window is running" in page.locator("#presentation-stop-help").inner_text()
    assert page.locator("#presentation-stop").is_disabled()
    rows = page.locator("#presentation-requirements").inner_text()
    assert "On-demand camera" in rows and "Use initiative policy" in rows and "Contextual offers" in rows
    assert "Workspace" in rows and "Wake up" in rows  # visible control names from the page source
    assert page.locator("#agent-enable-button strong").inner_text() == "Workspace"
    page.locator(".presentation-card").scroll_into_view_if_needed()
    page.locator(".presentation-card").screenshot(path=str(SHOTS / f"7-presentation-needs-{width}.png"))

    before = list(posts)
    # Navigation only: the exact existing control is shown and focused; nothing is saved or started.
    page.click("#presentation-requirements li[data-need='camera'] button")
    page.wait_for_function("document.activeElement && document.activeElement.id === 'camera_enabled'")
    assert not page.locator("#panel-settings").is_hidden()
    assert page.locator("#camera_enabled").evaluate("el => el.closest('details').open")
    assert page.locator("#camera_enabled").is_checked() is False
    page.screenshot(path=str(SHOTS / f"8-presentation-nav-camera-{width}.png"))
    page.click("#tab-agent")
    page.click("#presentation-requirements li[data-need='workspace'] button")
    page.wait_for_function("document.activeElement && document.activeElement.id === 'agent-enable-button'")
    page.click("#presentation-requirements li[data-need='initiative'] button")
    page.wait_for_function("document.activeElement && document.activeElement.id === 'initiative-policy-enabled'")
    page.click("#presentation-requirements li[data-need='awake'] button")
    page.wait_for_function("document.activeElement && document.activeElement.id === 'global-power-awake'")
    assert posts == before, "navigation must not save settings, wake Reachy or start a window"

    # Every gate satisfied: Start becomes available; the server still re-checks on press.
    state["config"].update(camera_enabled=True, initiative_policy_enabled=True, contextual_offers_enabled=True)
    state["runtime"].update(power_mode="awake", motors_enabled=True)
    state["runtime"]["agent"]["profile"] = "agent"
    refresh(page)
    page.wait_for_function("!document.getElementById('presentation-start').disabled")
    assert needs(page) == [] and "Ready to start" in page.locator("#presentation-readiness").inner_text()
    page.locator(".presentation-card").screenshot(path=str(SHOTS / f"9-presentation-ready-{width}.png"))
    assert not errors


def test_face_following_blocks_and_unreachable_status_is_unknown(ui):  # noqa: F811
    page, state, _posts, errors = ui
    user_reported_state(state)
    state["config"].update(camera_enabled=True, initiative_policy_enabled=True, contextual_offers_enabled=True)
    state["runtime"].update(power_mode="awake", motors_enabled=True, face_tracking_active=True)
    state["runtime"]["agent"]["profile"] = "agent"
    page.click("#tab-agent")
    refresh(page)
    page.wait_for_function("document.querySelectorAll('#presentation-requirements li').length === 1")
    assert needs(page) == ["face"] and page.locator("#presentation-start").is_disabled()

    state["runtime"]["face_tracking_active"] = False
    quiesce(page)
    assert page.evaluate("refreshStatus()") is True
    assert not page.locator("#presentation-start").is_disabled()

    # Reachy unreachable (not an owner loss): nothing is claimed ready and Start/Stop are off.
    page.route("**/api/status", lambda route: route.fulfill(status=503, json={"detail": "unavailable"}))
    assert page.evaluate("refreshStatus()") is False
    assert page.evaluate("window.HomebodyOwner.isOwner()") is True
    assert "unknown" in page.locator("#presentation-readiness").inner_text()
    assert page.locator("#presentation-start").is_disabled() and page.locator("#presentation-stop").is_disabled()
    assert needs(page) == []
    assert "unavailable while live status is unknown" in page.locator("#presentation-stop-help").inner_text()

    # Same owner generation: polls are serialized (no overlapping request) and a fresh authorized
    # reply may restore readiness.
    held = []
    page.unroute("**/api/status")
    page.route("**/api/status", lambda route: held.append(route))
    with page.expect_request("**/api/status"):
        page.evaluate("window.__firstRefresh = refreshStatus(); window.__secondRefresh = refreshStatus();")
    assert page.evaluate("window.__secondRefresh") is False  # returned without a second request
    assert len(held) == 1 and page.locator("#presentation-start").is_disabled()
    held[0].fulfill(json={"config": state["config"], "runtime": state["runtime"]})
    assert page.evaluate("window.__firstRefresh") is True
    assert not page.locator("#presentation-start").is_disabled()
    assert not errors


def test_initial_and_unknown_configuration_never_implies_ready(ui):  # noqa: F811
    page, state, _posts, errors = ui
    # The fixture's initial config carries no presentation keys: unknown, not ready.
    page.click("#tab-agent")
    quiesce(page)
    assert page.evaluate("refreshStatus()") is True
    assert "unknown" in page.locator("#presentation-readiness").inner_text()
    assert page.locator("#presentation-start").is_disabled() and needs(page) == []
    state["config"] = {}
    state["runtime"].update(power_mode="awake", motors_enabled=True)
    assert page.evaluate("refreshStatus()") is True
    assert "unknown" in page.locator("#presentation-readiness").inner_text()
    assert page.locator("#presentation-start").is_disabled()
    assert not errors


def test_owner_revocation_discards_admitted_ready_reply_even_after_pairing_again(ui):  # noqa: F811
    page, state, _posts, errors = ui
    user_reported_state(state)
    state["config"].update(camera_enabled=True, initiative_policy_enabled=True, contextual_offers_enabled=True)
    state["runtime"].update(power_mode="awake", motors_enabled=True)
    state["runtime"]["agent"]["profile"] = "agent"
    page.click("#tab-agent")
    quiesce(page)
    assert page.evaluate("refreshStatus()") is True
    assert not page.locator("#presentation-start").is_disabled()  # genuinely ready before revocation
    # Immutable, successful, ready reply; the page's own 5 s request timeout must not be what drops it.
    old_ready = json.loads(json.dumps({"config": state["config"], "runtime": state["runtime"]}))
    page.route("**/api/agent/activity", lambda route: route.fulfill(status=401, json={"detail": "revoked"}))
    holding = {"on": True}
    held = []

    def status(route):
        if holding["on"]:
            holding["on"] = False
            held.append(route)
        else:
            route.fulfill(json={"config": state["config"], "runtime": state["runtime"]})

    page.route("**/api/status", status)

    def admit_old_reply():
        holding["on"] = True
        with page.expect_request("**/api/status"):
            # Trailing null: do not let evaluate await the held refresh itself.
            page.evaluate("window.__heldAt = performance.now(); window.__oldRefresh = refreshStatus(); null")

    def revoke():
        # Authority is revoked through a different real endpoint answering 401 via the owner transport.
        assert page.evaluate("fetch('/api/agent/activity').then(r => r.status)") == 401
        # Immediately after that fetch resolves: no wait, no poll.
        assert page.evaluate("window.HomebodyOwner.isOwner()") is False
        assert "unknown" in page.locator("#presentation-readiness").inner_text()
        assert page.locator("#presentation-start").is_disabled()
        assert page.locator("#presentation-stop").is_disabled()

    def release_old_reply():
        held.pop().fulfill(json=old_ready)
        assert page.evaluate("window.__oldRefresh") is False
        # Discarded as stale, not aborted by the 5 s timeout (which would make this test vacuous).
        assert page.evaluate("performance.now() - window.__heldAt") < 4500
        assert "No response from Reachy" not in page.locator("#runtime-detail").inner_text()

    # Phase A: release while still unpaired -> no revival.
    admit_old_reply()
    revoke()
    release_old_reply()
    assert page.evaluate("window.HomebodyOwner.isOwner()") is False
    assert "unknown" in page.locator("#presentation-readiness").inner_text()
    assert page.locator("#presentation-start").is_disabled()

    # Phase B: pair again through the real form, then release an older admitted reply.
    page.fill("#owner-pair-code", "123456")
    with page.expect_response("**/api/owner/session"):
        page.click("#owner-pair-button")
    page.wait_for_function("window.HomebodyOwner.isOwner() === true")
    page.wait_for_function("!document.getElementById('presentation-start').disabled")  # fresh current reply
    quiesce(page)
    admit_old_reply()
    revoke()
    state["config"]["camera_enabled"] = False  # the server's current state after pairing again
    page.fill("#owner-pair-code", "123456")
    with page.expect_response("**/api/owner/session"):
        page.click("#owner-pair-button")
    page.wait_for_function("document.querySelector('#presentation-requirements li[data-need=\"camera\"]')")
    release_old_reply()
    assert needs(page) == ["camera"] and page.locator("#presentation-start").is_disabled()

    # A fresh, current-generation reply may restore readiness.
    state["config"]["camera_enabled"] = True
    quiesce(page)
    assert page.evaluate("refreshStatus()") is True
    assert needs(page) == [] and not page.locator("#presentation-start").is_disabled()
    assert not errors
