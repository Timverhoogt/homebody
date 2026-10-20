# ruff: noqa: F811 -- imported fixture is intentionally a test parameter
"""Real Chromium, explicit status fixtures: caregiver Show and Tell controls on the Kids page.

Nothing here contacts Reachy, Hermes or a provider; no camera is used.
"""

import json

from test_assistant_modes_browser import ui  # noqa: F401


def show_tell(**changes) -> dict:
    value = {"camera_approved": True, "enabled": True, "session": True, "available": True, "looking": False}
    value.update(changes)
    return value


def kids(*, state=None, host=False, active=True, camera=False, activity="buddy") -> dict:
    return {
        "state": "waiting_for_wake_word", "power_mode": "awake", "stt_locality": "unknown",
        "motors_enabled": True, "head_safely_folded": False,
        "kids_mode": {
            "active": active, "locked": active, "remaining_seconds": 600, "turns_completed": 1,
            "profile": {"age_band": "7-9", "activity": activity, "language": "en", "duration_minutes": 15,
                        "motion_enabled": True, "camera_active": False, "host": host},
            "last_end_reason": "", "last_fold_succeeded": None, "tool_policy": "voice-state-motion-only",
            "camera_enabled": False, "camera_active": camera,
            "host": {"enabled": host, "phase": "lobby" if host else "", "activity": "", "choices": [],
                     "notice": "", "available": []},
            "show_and_tell": state or show_tell(),
        },
    }


def show(page, state, runtime) -> None:
    state["runtime"] = runtime
    page.evaluate("refreshStatus()")
    page.wait_for_timeout(120)
    page.evaluate("refreshStatus()")
    page.wait_for_timeout(120)


def show_tell_posts(posts):
    return [(path, body) for path, body in posts if path.startswith("/api/kids/show-and-tell/")]


def label(page) -> str:
    return page.locator("#kids-show-tell-state").text_content()


def test_on_by_default_within_camera_approval_with_disclosure_and_no_approval_step(ui):
    page, state, posts, errors = ui
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
    page.click("#tab-kids")
    show(page, state, kids())
    assert page.locator("#kids-show-tell").is_visible()
    assert label(page) == "On · ready in this Kids Chat"
    disclosure = page.locator("#kids-show-tell-disclosure").inner_text()
    assert "external service" in disclosure and "not stored" in disclosure and "never looks by itself" in disclosure
    assert page.locator("#kids-show-tell-toggle").text_content() == "Turn off"
    assert not page.is_disabled("#kids-show-tell-look")
    assert page.locator("#kids-show-tell-allow").count() == 0  # no per-session Allow control exists
    # Settings discloses that the AI camera approval also covers Show and Tell.
    assert "Show and Tell" in page.locator("label:has(#camera_enabled)").text_content()
    # Outside an eligible session the setting is still visible; Look now is not usable.
    for runtime in (kids(active=False, state=show_tell(session=False, available=False)),
                    kids(activity="story", state=show_tell(session=False, available=False))):
        show(page, state, runtime)
        assert page.locator("#kids-show-tell").is_visible() and label(page) == "On"
        assert page.is_disabled("#kids-show-tell-look")
    show(page, state, kids(host=True))
    assert page.locator("#kids-show-tell").is_hidden()  # Play together never offers it
    assert show_tell_posts(posts) == [] and dialogs == [] and not errors


def test_existing_camera_off_stays_off_and_cannot_be_turned_on_here(ui):
    page, state, posts, errors = ui
    page.click("#tab-kids")
    show(page, state, kids(state=show_tell(camera_approved=False, available=False)))
    assert label(page) == "Off · Camera access for AI is off in Settings"
    assert page.locator("#kids-show-tell-toggle").text_content() == "Turn off"
    assert not page.is_disabled("#kids-show-tell-toggle")  # opting out always stays possible
    show(page, state, kids(state=show_tell(camera_approved=False, enabled=False, available=False)))
    assert page.locator("#kids-show-tell-toggle").text_content() == "Turn on"
    assert page.is_disabled("#kids-show-tell-toggle") and page.is_disabled("#kids-show-tell-look")
    assert show_tell_posts(posts) == [] and not errors


def test_turn_off_and_on_are_single_clicks_and_store_nothing_in_the_browser(ui):
    page, state, posts, errors = ui
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
    page.click("#tab-kids")
    show(page, state, kids())
    def save_setting(route):
        body = json.loads(route.request.post_data)
        posts.append(("/api/kids/show-and-tell/setting", body))
        saved = show_tell(enabled=body["enabled"], available=body["enabled"])
        # Persist before acknowledging, as the real endpoint does. Otherwise a
        # subsequent status poll can truthfully return the fixture's stale "on".
        state["runtime"] = kids(state=saved)
        route.fulfill(json=saved)

    page.route("**/api/kids/show-and-tell/setting", save_setting)
    page.click("#kids-show-tell-toggle")
    page.wait_for_function("document.getElementById('kids-show-tell-message').textContent.includes('is off')")
    state["runtime"] = kids(state=show_tell(enabled=False, available=False))
    page.wait_for_function("document.getElementById('kids-show-tell-toggle').textContent === 'Turn on'")
    assert label(page) == "Off" and page.is_disabled("#kids-show-tell-look")
    page.click("#kids-show-tell-toggle")
    page.wait_for_function("document.getElementById('kids-show-tell-message').textContent.includes('is on')")
    assert show_tell_posts(posts) == [
        ("/api/kids/show-and-tell/setting", {"enabled": False}),
        ("/api/kids/show-and-tell/setting", {"enabled": True}),
    ]
    assert dialogs == []  # no repetitive confirmation
    storage = page.evaluate("JSON.stringify({...localStorage}) + JSON.stringify({...sessionStorage})")
    assert "show" not in storage.lower() and "camera" not in storage.lower()
    assert not errors


def test_status_reply_held_across_turn_off_cannot_reshow_on(ui):
    page, state, posts, errors = ui
    page.click("#tab-kids")
    show(page, state, kids())
    held = []
    page.route("**/api/status", lambda route: held.append(route))
    page.evaluate("() => { refreshStatus(); }")  # started before Turn off; its reply is held
    for _ in range(40):
        if held:
            break
        page.wait_for_timeout(25)
    assert held
    page.route("**/api/kids/show-and-tell/setting",
               lambda route: route.fulfill(json=show_tell(enabled=False, available=False)))
    page.click("#kids-show-tell-toggle")
    page.wait_for_function("document.getElementById('kids-show-tell-message').textContent.includes('is off')")
    held[0].fulfill(json={"config": state["config"], "runtime": kids()})  # stale "on" snapshot
    page.wait_for_timeout(300)
    assert label(page) == "Off" and page.locator("#kids-show-tell-toggle").text_content() == "Turn on"
    page.unroute("**/api/status")
    for route in held[1:]:
        route.fallback()
    state["runtime"] = kids(state=show_tell(enabled=False, available=False))
    page.evaluate("refreshStatus()")
    page.wait_for_timeout(200)
    assert label(page) == "Off" and not errors


def test_look_now_posts_once_and_turn_off_stays_available_mid_look(ui):
    page, state, posts, errors = ui
    page.click("#tab-kids")
    show(page, state, kids())
    page.click("#kids-show-tell-look")
    page.wait_for_function("document.getElementById('kids-show-tell-message').textContent.includes('one photo')")
    assert show_tell_posts(posts) == [("/api/kids/show-and-tell/look-now", {})]
    show(page, state, kids(state=show_tell(looking=True), camera=True))
    assert page.is_disabled("#kids-show-tell-look") and not page.is_disabled("#kids-show-tell-toggle")
    assert page.locator("#kids-status-badge").text_content() == "One photo"
    assert "one announced photo" in page.locator("#kids-session-detail").inner_text()
    assert not errors


def test_owner_loss_hides_and_disables_show_and_tell(ui):
    page, state, posts, errors = ui
    page.click("#tab-kids")
    show(page, state, kids())
    assert page.locator("#kids-show-tell").is_visible()
    page.route("**/api/status", lambda route: route.fulfill(status=401, json={"detail": "Pair this device first"}))
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('kids-show-tell').hidden")
    assert page.is_disabled("#kids-show-tell-look") and page.is_disabled("#kids-show-tell-toggle")
    assert show_tell_posts(posts) == [] and not errors
