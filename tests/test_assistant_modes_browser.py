"""Real Chromium: Everyday/Workspace, separate Stop controls and Advanced voice-engine settings.

Every API reply is an explicit fixture; nothing contacts Reachy, Hermes or a model provider.
"""

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

SHOTS = Path("/home/timv/.hermes/cache/scratch/hb-ew/screens")
CAPABILITIES = {
    "available": True, "error": "",
    "text_provider": {"name": "openrouter", "label": "OpenRouter", "region": "global", "model": "openai/gpt-4.1-mini"},
    "weather": {"source": "home_assistant", "label": "Home Assistant weather entity", "location_leaves_home": False},
    "devices": {"controls": ["desk light"], "status": ["desk light", "hall sensor"]},
    "time_source": "hermes_host_clock",
}


@pytest.fixture
def ui():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        errors, posts = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        state = {
            "config": {"conversation_mode": "realtime", "realtime_model": "gpt-4o-realtime-preview",
                       "assistant_mode": "everyday", "assistant_mode_notice": "migrated", "live_voice": "marin",
                       "live_session_timeout_seconds": 300, "realtime_semantic_eagerness": "auto",
                       "bridge_url": "http://hermes.test:8643", "api_key": "********"},
            "runtime": {"state": "waiting_for_wake_word", "power_mode": "awake", "kids_mode": {},
                        "agent": {"profile": "conversation", "recent_activity": []}},
            "badge": {"status": "running", "running": True},
            "capabilities": CAPABILITIES,
        }

        def respond(route):
            request = route.request
            path = urlparse(request.url).path
            if request.method == "POST" and path.startswith("/api/"):
                posts.append((path, json.loads(request.post_data or "{}")))
            if path.startswith("/static/"):
                target = Path("homebody") / path.lstrip("/")
                route.fulfill(path=str(target)) if target.exists() else route.fulfill(status=404)
            elif path == "/":
                route.fulfill(path="homebody/static/index.html", content_type="text/html")
            elif path == "/api/owner/session":
                route.fulfill(json={"owner": True, "name": "Browser fixture", "csrf": "fixture"})
            elif path == "/api/status":
                route.fulfill(json={"config": state["config"], "runtime": state["runtime"]})
            elif path == "/api/everyday/capabilities":
                route.fulfill(json=state["capabilities"])
            elif path == "/api/agent/workspace/native-badge":
                route.fulfill(json=state["badge"])
            elif path == "/api/agent/profile":
                profile = json.loads(request.post_data)["profile"]
                state["runtime"]["agent"]["profile"] = profile
                route.fulfill(json={"ok": True, "agent": state["runtime"]["agent"]})
            elif path == "/api/settings":
                route.fulfill(json={"ok": True, "config": state["config"], "note": "Saved"})
            elif path.startswith("/api/agent/workspace"):
                route.fulfill(json={"enabled": False, "generation": 1, "events": [], "targets": [], "messages": []})
            else:
                route.fulfill(json={})

        page.route("**/*", respond)
        page.goto("http://homebody.test/")
        page.wait_for_function("!document.querySelector('main').hidden && typeof updateStatus === 'function'")
        yield page, state, posts, errors
        browser.close()


@pytest.mark.parametrize("width", [390, 1440])
def test_modes_stop_controls_and_everyday_disclosure(ui, width):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 900})
    page.click("#tab-agent")
    page.wait_for_function("document.getElementById('everyday-badge').textContent === 'Tools ready'")
    assert page.locator("#agent-conversation-button").get_attribute("aria-pressed") == "true"
    assert "OpenAI Realtime" in page.locator("#everyday-voice").inner_text()
    assert "gpt-4o-realtime-preview" in page.locator("#everyday-voice").inner_text()
    assert "openai/gpt-4.1-mini" in page.locator("#everyday-model").text_content()
    assert "retrieved through your home system" in page.locator("#everyday-weather").inner_text()
    assert "1 permitted control" in page.locator("#everyday-devices").inner_text()
    assert "desk light" in page.locator("#everyday-device-ids").text_content()
    assert not page.locator("#everyday-technical-details").get_attribute("open")
    assert page.locator("#native-work-badge").is_visible()  # durable work visible as a badge in Everyday
    assert page.locator("#assistant-mode-notice").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"agent-everyday-{width}.png"), full_page=True)

    page.click("#voice-stop-button")
    page.wait_for_function("document.getElementById('agent-message').textContent.includes('Stopped speaking')")
    assert ("/api/voice/stop", {}) in posts and not any(p == "/api/agent/stop" for p, _ in posts)

    page.click("#agent-enable-button")
    page.wait_for_function("document.getElementById('agent-enable-button').getAttribute('aria-pressed') === 'true'")
    assert ("/api/agent/profile", {"profile": "agent", "preserve_work": False}) in posts
    page.click("#agent-conversation-button")
    page.wait_for_function("document.getElementById('agent-message').textContent.includes('Everyday active')")
    assert ("/api/agent/profile", {"profile": "conversation", "preserve_work": True}) in posts

    page.click("#assistant-mode-notice-ack")
    page.wait_for_function("document.getElementById('assistant-mode-notice').hidden")
    assert ("/api/settings", {"assistant_mode_notice": ""}) in posts
    assert not errors


def test_unavailable_state_kids_exclusion_and_late_reply_after_owner_loss(ui):
    page, state, _posts, errors = ui
    state["capabilities"] = {**CAPABILITIES, "available": False, "error": "Everyday text provider key is missing"}
    state["badge"] = {"status": "idle", "running": False}
    page.click("#tab-agent")
    page.evaluate("HomebodyAssistantModes.owner(false); HomebodyAssistantModes.owner(true); refreshStatus()")
    page.wait_for_function("document.getElementById('everyday-badge').textContent === 'Unavailable'")
    assert "key is missing" in page.locator("#everyday-message").inner_text()
    assert not page.locator("#native-work-badge").is_visible()

    held = []
    page.route("**/api/everyday/capabilities", lambda route: held.append(route))
    page.evaluate("HomebodyAssistantModes.owner(false)")
    page.evaluate("HomebodyAssistantModes.owner(true); updateStatus({config: {}, runtime: {state: 'listening', "
                  "power_mode: 'awake', kids_mode: {}, agent: {profile: 'conversation'}}})")
    page.wait_for_timeout(100)
    assert held
    page.evaluate("HomebodyAssistantModes.owner(false)")  # owner lost while the reply is in flight
    held[0].fulfill(json={**CAPABILITIES, "text_provider": {**CAPABILITIES["text_provider"], "model": "late-model"}})
    page.wait_for_timeout(150)
    assert "late-model" not in page.locator("#everyday-card").inner_text()

    state["runtime"]["kids_mode"] = {"active": True}
    page.evaluate("refreshStatus()")
    page.wait_for_function("document.getElementById('everyday-card').hidden")
    assert not errors


@pytest.mark.parametrize("engine, label", [("realtime", "OpenAI Realtime"), ("live", "OpenAI GPT-Live"),
                                           ("pipeline", "Pipeline")])
def test_everyday_primary_label_follows_saved_voice_not_text_backend(ui, engine, label):
    page, state, posts, _ = ui
    page.click("#tab-agent")
    state["config"].update(conversation_mode=engine, realtime_model="gpt-realtime-2.1", live_model="gpt-live-1")
    before = len(posts)
    page.evaluate("payload => HomebodyAssistantModes.update(payload)",
                  {"config": state["config"], "runtime": state["runtime"]})
    assert label in page.locator("#everyday-voice").inner_text()
    assert "gpt-4.1-mini" not in page.locator("#everyday-voice").inner_text()
    assert not page.locator("#everyday-technical-details").get_attribute("open")
    assert len(posts) == before  # rendering never changes the saved engine


@pytest.mark.parametrize("width", [390, 1440])
def test_advanced_engine_settings_save_exact_values_and_keep_existing_controls(ui, width):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 900})
    page.click("#tab-settings")
    page.wait_for_function("document.getElementById('conversation_mode').value === 'realtime'")
    # A saved non-default Realtime model is preserved as a selectable value, not silently replaced.
    assert page.locator("#realtime_model").input_value() == "gpt-4o-realtime-preview"
    assert page.locator("#realtime-settings").is_visible() and not page.locator("#live-settings").is_visible()
    page.select_option("#conversation_mode", "live")
    assert page.locator("#live-settings").is_visible()
    assert not page.locator("#realtime-settings").is_visible() and not page.locator("#pipeline-settings").is_visible()
    assert "Preview" in page.locator("#engine-disclosure").inner_text()
    assert "billed" in page.locator("#engine-disclosure").inner_text()
    page.select_option("#live_voice", "cedar")
    page.fill("#live_session_timeout_seconds", "600")
    page.locator("#settings-form button[type=submit]").first.click()
    page.wait_for_function("document.getElementById('form-message').textContent === 'Saved'")
    saved = [body for path, body in posts if path == "/api/settings"][-1]
    assert (saved["conversation_mode"], saved["live_model"], saved["live_voice"]) == ("live", "gpt-live-1", "cedar")
    assert saved["live_session_timeout_seconds"] == 600 and saved["realtime_model"] == "gpt-4o-realtime-preview"
    assert "assistant_mode" not in saved  # purpose changes only through the authority route
    page.select_option("#conversation_mode", "pipeline")
    assert page.locator("#pipeline-settings").is_visible()
    assert page.locator("#speaker-card").count() == 1 and page.locator("#updates-card").count() == 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.select_option("#conversation_mode", "live")
    page.locator("#conversation_mode").scroll_into_view_if_needed()
    page.screenshot(path=str(SHOTS / f"settings-live-{width}.png"), full_page=False)
    assert not errors
