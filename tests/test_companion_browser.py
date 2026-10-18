"""Real Chromium: the Companion card in Everyday shows truthful state, looks once, and drops late replies.

Every API reply is an explicit fixture; nothing contacts Reachy, Hermes, a camera or a model.
"""

import json
from pathlib import Path

import pytest
from test_assistant_modes_browser import ui  # noqa: F401  (shared real-page fixture)

SHOTS = Path(__file__).resolve().parents[1] / ".artifacts" / "browser-screens" / "companion"


def companion_state(state, **overrides):
    state["config"].update(
        camera_enabled=True,
        proactive_presence_enabled=True,
        initiative_policy_enabled=False,
        contextual_offers_enabled=False,
    )
    state["runtime"].update(
        power_mode="awake",
        motors_enabled=True,
        presence={
            "enabled": True,
            "level": "present",
            "occupancy": "present",
            "source_health": "fresh",
            "source": "home_assistant",
            "last_outcome": "acknowledged_silently",
        },
        companion={
            "activity": "quiet",
            "presence": {"occupancy": "present", "source_health": "fresh"},
            "camera": {"permitted": True, "in_use": False},
            "microphone": {"permitted": True, "in_use": False},
            "look": {
                "available": True,
                "blocked_reason": "",
                "context_live": False,
                "context_seconds_remaining": 0,
                "follow_ups_remaining": 0,
                "last_failure": "",
            },
            "speech": {"last_announcement_failed": False, "route": ""},
            "suggestions": {"level": "off", "paused_reason": "Suggestions are off until a source is enrolled"},
            "receipts": [],
        },
    )
    state["runtime"]["companion"].update(overrides)


def quiesce(page):
    page.wait_for_function("!statusRefreshPending && (clearTimeout(statusPollTimer), true)")


def open_card(page, state):
    companion_state(state)
    page.click("#tab-dashboard")
    page.evaluate("refreshStatus()")
    page.wait_for_function("!document.getElementById('companion-look').disabled")


def hold_look(page, held):
    page.route("**/api/companion/look", lambda route: held.append(route))
    page.fill("#companion-question", "read this")
    with page.expect_request("**/api/companion/look"):
        page.click("#companion-look")


@pytest.mark.parametrize("width", [390, 1440])
def test_companion_card_in_everyday_shows_truth_and_looks_once(ui, width):  # noqa: F811
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 900})
    SHOTS.mkdir(parents=True, exist_ok=True)
    open_card(page, state)
    assert page.locator("#agent-enable-button").is_visible()  # Workspace picker preserved
    assert page.locator("#companion-presence").inner_text() == "Someone is here"
    assert "not in use" in page.locator("#companion-camera").inner_text()
    assert "permitted" in page.locator("#companion-camera").inner_text()
    assert page.locator("#companion-suggestions-level").input_value() == "off"
    assert "enrolled" in page.locator("#companion-why").inner_text()
    page.locator("#companion-card").screenshot(path=str(SHOTS / f"companion-{width}.png"))

    looks = []

    def answer_look(route):
        looks.append(json.loads(route.request.post_data or "{}"))
        # The real runtime reports a live follow-up context after a successful look.
        state["runtime"]["companion"]["look"].update(
            context_live=True, context_seconds_remaining=120, follow_ups_remaining=3
        )
        route.fulfill(
            json={
                "ok": True,
                "answer": "It is a dentist letter for Friday.",
                "captures": 1,
                "processing": "host_provider",
            }
        )

    page.route("**/api/companion/look", answer_look)
    page.fill("#companion-question", "What does this say?")
    with page.expect_response("**/api/companion/look"):
        page.click("#companion-look")  # real hit test at this viewport
    page.wait_for_function("document.getElementById('companion-answer').textContent.includes('dentist')")
    assert looks == [{"question": "What does this say?"}]  # exactly one explicit look request
    assert "1 capture" in page.locator("#companion-answer-meta").inner_text()
    assert not page.locator("#companion-stop").is_disabled()
    with page.expect_response("**/api/companion/stop"):
        page.click("#companion-stop")
    assert ("/api/companion/stop", {}) in posts
    # Stop is a teardown on the page too: the private answer text is gone immediately.
    assert page.locator("#companion-answer").is_hidden()
    assert "dentist" not in page.locator("#companion-card").inner_text()
    assert not errors


def test_unknown_offline_and_disabled_reasons_are_truthful(ui):  # noqa: F811
    page, state, _posts, errors = ui
    companion_state(
        state,
        look={
            "available": False,
            "blocked_reason": "Camera access is disabled in Reachy settings",
            "context_live": False,
            "context_seconds_remaining": 0,
            "follow_ups_remaining": 0,
            "last_failure": "",
        },
    )
    state["runtime"]["companion"]["presence"] = {"occupancy": "unknown", "source_health": "stale"}
    state["runtime"]["companion"]["camera"] = {"permitted": False, "in_use": False}
    page.click("#tab-dashboard")
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('companion-presence').textContent.includes('Unknown')")
    assert "sensor not reporting" in page.locator("#companion-presence").inner_text()
    assert page.locator("#companion-look").is_disabled()
    assert "Camera access is disabled" in page.locator("#companion-look-reason").inner_text()
    page.route("**/api/status", lambda route: route.fulfill(status=503, json={"detail": "unavailable"}))
    quiesce(page)
    assert page.evaluate("refreshStatus()") is False
    assert "unavailable" in page.locator("#companion-activity").inner_text().lower()
    assert page.locator("#companion-look").is_disabled()
    assert not errors


@pytest.mark.parametrize("teardown", ["owner_loss", "stop", "privacy", "offline", "kids", "profile"])
def test_held_look_reply_after_teardown_never_repaints_private_text_or_controls(ui, teardown):  # noqa: F811
    page, state, _posts, errors = ui
    open_card(page, state)
    held = []
    hold_look(page, held)  # the draft question "read this" is private text too
    if teardown == "owner_loss":
        page.route("**/api/agent/activity", lambda route: route.fulfill(status=401, json={"detail": "revoked"}))
        assert page.evaluate("fetch('/api/agent/activity').then(r => r.status)") == 401
        assert page.evaluate("window.HomebodyOwner.isOwner()") is False
    elif teardown == "stop":
        page.route("**/api/companion/stop", lambda route: route.fulfill(json={"ok": True, "stopped": ["look"]}))
        with page.expect_response("**/api/companion/stop"):
            page.click("#companion-stop")
    elif teardown == "privacy":
        state["runtime"]["power_mode"] = "sleep"
        quiesce(page)
        assert page.evaluate("refreshStatus()") is True
    elif teardown == "offline":
        page.route("**/api/status", lambda route: route.fulfill(status=503, json={"detail": "unavailable"}))
        quiesce(page)
        assert page.evaluate("refreshStatus()") is False
    elif teardown == "kids":
        state["runtime"]["kids_mode"] = {"active": True, "locked": False}
        quiesce(page)
        assert page.evaluate("refreshStatus()") is True
    else:
        state["runtime"]["agent"]["profile"] = "agent"
        quiesce(page)
        assert page.evaluate("refreshStatus()") is True
    # Release the immutable, successful reply admitted before the teardown.
    held.pop().fulfill(json={"ok": True, "answer": "SECRET LETTER", "captures": 1})
    page.wait_for_timeout(300)
    assert "SECRET" not in page.content()
    assert page.locator("#companion-answer").is_hidden()
    assert page.locator("#companion-question").input_value() == ""
    if teardown in {"owner_loss", "offline", "privacy", "kids"}:
        assert page.locator("#companion-look").is_disabled()
    assert not errors
    # Deliberate positive control: a fresh current-generation status may restore the controls.
    if teardown not in {"owner_loss", "offline"}:
        page.unroute("**/api/companion/look")
        state["runtime"]["power_mode"] = "awake"
        state["runtime"]["kids_mode"] = {}
        state["runtime"]["agent"]["profile"] = "conversation"
        quiesce(page)
        assert page.evaluate("refreshStatus()") is True
        page.wait_for_function("!document.getElementById('companion-look').disabled")

        def fresh_look(route):
            state["runtime"]["companion"]["look"].update(context_live=True, context_seconds_remaining=120)
            route.fulfill(json={"ok": True, "answer": "Fresh answer.", "captures": 1})

        page.route("**/api/companion/look", fresh_look)
        page.fill("#companion-question", "read this again")
        with page.expect_response("**/api/companion/look"):
            page.click("#companion-look")
        page.wait_for_function("document.getElementById('companion-answer').textContent.includes('Fresh')")


def test_owner_loss_does_not_let_a_later_status_poll_re_enable_controls(ui):  # noqa: F811
    page, state, _posts, errors = ui
    open_card(page, state)
    page.route("**/api/agent/activity", lambda route: route.fulfill(status=401, json={"detail": "revoked"}))
    assert page.evaluate("fetch('/api/agent/activity').then(r => r.status)") == 401
    assert page.locator("#companion-look").is_disabled()
    # A status payload applied while unpaired (e.g. a stale poll) must not resurrect the controls.
    payload = json.dumps({"config": state["config"], "runtime": state["runtime"]})
    page.evaluate(f"updateStatus({payload})")
    assert page.locator("#companion-look").is_disabled()
    assert page.locator("#companion-suggestions-level").is_disabled()
    assert not errors


def test_enrollment_panel_shows_setup_needed_then_offers_only_catalog_identifiers(ui):  # noqa: F811
    page, state, posts, errors = ui
    open_card(page, state)
    catalog = {
        "ok": True,
        "available": False,
        "setup_needed": ["Set REACHY_COMPANION_SOURCES_FILE on the Hermes host."],
        "devices": [],
        "calendars": [],
        "enrollment": {"enabled": False},
    }
    page.route("**/api/companion/sources", lambda route: route.fulfill(json=catalog))
    with page.expect_response("**/api/companion/sources"):
        page.click("#companion-details summary")
    page.wait_for_function("document.querySelectorAll('#companion-setup-needed li').length === 1")
    assert page.locator("#companion-enroll").is_hidden()
    assert "Setup needed" in page.locator("#companion-sources-status").inner_text()
    # Host now ready: only exact already-authorized identifiers are offered; saving posts the selection.
    ready = {
        "ok": True,
        "available": True,
        "setup_needed": [],
        "devices": [{"entity_id": "binary_sensor.washer_done", "label": "Washing machine"}],
        "calendars": [{"calendar_entity_id": "calendar.family", "label": "family"}],
        "enrollment": {"enabled": False, "home_assistant": [], "calendar": []},
    }
    saved = []

    def catalog_route(route):
        if route.request.method == "POST":
            saved.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(json=ready)

    page.unroute("**/api/companion/sources")
    page.route("**/api/companion/sources", catalog_route)
    page.evaluate("window.HomebodyCompanion.loadCatalog()")
    page.wait_for_function("!document.getElementById('companion-enroll').hidden")
    options = page.eval_on_selector_all("#companion-device option", "o => o.map(x => x.value)")
    assert options == ["", "binary_sensor.washer_done"]
    page.select_option("#companion-calendar", "calendar.family")
    page.select_option("#companion-device", "binary_sensor.washer_done")
    page.fill("#companion-device-value", "on")
    page.fill("#companion-device-offer", "The washing machine seems finished; would you like to know when it stopped?")
    page.fill("#companion-device-accepted", "Okay. The washer reported done just now; nothing else was changed.")
    with page.expect_response("**/api/companion/sources"):
        page.click("#companion-enroll-save")
    assert saved and saved[0]["calendar"][0]["calendar_entity_id"] == "calendar.family"
    assert posts == [post for post in posts if post[0] != "/api/companion/sources"]  # only the intercepted POST
    assert saved[0]["calendar"][0]["disclosure"] == "private"
    assert saved[0]["home_assistant"][0]["entity_id"] == "binary_sensor.washer_done"
    assert not errors
