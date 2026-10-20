# ruff: noqa: F811 -- imported fixture is intentionally a test parameter
"""Real Chromium: 'Transcribing on Reachy' vs 'on Hermes host' from the per-turn location enum."""

from pathlib import Path

import pytest
from settings_ui_helpers import choose
from test_assistant_modes_browser import ui  # noqa: F401
from test_kids_stt_status_browser import show

SHOTS = Path(__file__).resolve().parents[1] / ".artifacts" / "robot-stt-status"


def kids(phase, location, verified=False, locality="unknown"):
    return {
        "state": phase,
        "stt_locality": locality,
        "stt_processing_location": location,
        "stt_processing_verified": verified,
        "kids_mode": {"active": True, "locked": True, "profile": {"activity": "quiz", "language": "nl"}},
    }


def title_is(page, text):
    page.wait_for_function(
        "t => document.querySelector('#kids-stage .hb-scene-title').textContent === t", arg=text
    )


@pytest.mark.parametrize("width", [390, 1280])
def test_kids_stage_names_reachy_then_host_fallback(ui, width):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 900})
    page.click("#tab-kids")
    stage = page.locator("#kids-stage")
    show(page, state, kids("transcribing", "robot"))  # pending primary intent
    title_is(page, "Transcribing on Reachy")
    assert "Hermes host" not in stage.inner_text()
    SHOTS.mkdir(parents=True, exist_ok=True)
    stage.scroll_into_view_if_needed()
    stage.screenshot(path=str(SHOTS / f"kids-robot-{width}.png"))
    show(page, state, kids("transcribing", "host"))  # robot failed; one local host fallback
    title_is(page, "Transcribing on Hermes host")
    assert "Reachy itself" not in stage.inner_text()
    stage.screenshot(path=str(SHOTS / f"kids-host-fallback-{width}.png"))
    # Late status after the turn ended: the location was reset, so no stale claim remains.
    show(page, state, kids("transcribing", "unknown"))
    title_is(page, "Transcribing")
    assert "Reachy itself" not in stage.inner_text() and "Hermes host" not in stage.inner_text()
    # Unexpected values are never rendered.
    show(page, state, kids("transcribing", "/opt/models/parakeet.gguf"))
    title_is(page, "Transcribing")
    assert "parakeet" not in page.content()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors and not [path for path, _ in posts if path.startswith("/api/kids")]


@pytest.mark.parametrize("width", [390, 1280])
def test_hero_headline_for_adult_robot_transcription(ui, width):
    page, state, _posts, errors = ui
    page.set_viewport_size({"width": width, "height": 900})
    show(page, state, {"state": "transcribing", "stt_processing_location": "robot", "kids_mode": {}})
    page.wait_for_function("document.querySelector('#runtime-state').textContent === 'Transcribing on Reachy…'")
    show(page, state, {"state": "transcribing", "stt_processing_location": "host", "stt_locality": "local",
                       "kids_mode": {}})
    page.wait_for_function(
        "document.querySelector('#runtime-state').textContent === 'Transcribing on Hermes host…'"
    )
    show(page, state, {"state": "thinking", "stt_processing_location": "unknown", "kids_mode": {}})
    page.wait_for_function("document.querySelector('#runtime-state').textContent === 'Thinking …'")
    assert not errors


def test_owner_loss_hides_reachy_claim(ui):
    page, state, _posts, errors = ui
    page.click("#tab-kids")
    show(page, state, kids("transcribing", "robot", verified=True, locality="local"))
    title_is(page, "Transcribing on Reachy")
    # Exercise actual owner revocation and teardown, not a getter override that leaves authority live.
    page.route("**/api/agent/activity", lambda route: route.fulfill(status=401, json={"detail": "owner required"}))
    page.evaluate("fetch('/api/agent/activity')")
    page.wait_for_function("HomebodyOwner.isOwner() === false")
    page.wait_for_function(
        "document.querySelector('#kids-stage .hb-scene-title').textContent !== 'Transcribing on Reachy'"
    )
    assert "Reachy itself" not in page.locator("#kids-stage").inner_text()
    assert not errors


@pytest.mark.parametrize("provisioned", [False, True])
def test_settings_offer_robot_provider_with_explicit_fallback(ui, provisioned):
    page, state, _posts, errors = ui
    page.route("**/api/voice-options", lambda route: route.fulfill(json={
        "stt": [{"id": "configured", "label": "Hermes configured STT", "models": ["base"]},
                {"id": "robot", "label": "On Reachy (on-device)", "models": [],
                 "provisioned": provisioned}],
        "tts": [{"id": "configured", "label": "Hermes configured TTS", "models": [], "voices": []}],
    }))
    page.evaluate("document.getElementById('tab-settings').click()")  # behind More on phones
    choose(page,"voice")
    page.locator("#speech-provider-details > summary").click()
    page.evaluate("loadVoiceOptions()")
    page.wait_for_function("[...document.querySelectorAll('#stt_provider option')].some(o => o.value === 'robot')")
    # The saved/default selection is preserved; the fallback control stays hidden for host STT.
    assert page.locator("#stt_provider").input_value() == "configured"
    assert page.locator("#stt_robot_fallback").is_hidden()
    assert page.locator("#robot-stt-note").is_hidden()
    page.select_option("#stt_provider", "robot")
    assert page.locator("#stt_robot_fallback").is_visible()
    assert page.locator("#stt_robot_fallback").input_value() == "none"
    assert page.locator("#stt_model").is_hidden()
    note = page.locator("#robot-stt-note").inner_text()
    if provisioned:
        # A provisioned manifest is a selection, not proof that recognition ran on Reachy.
        assert "set to run on Reachy" in note and "Live/Realtime adult speech is unchanged" in note
    else:
        assert "not provisioned" in note
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.locator("#pipeline-settings").screenshot(path=str(SHOTS / f"settings-robot-provider-{provisioned}.png"))
    page.select_option("#stt_provider", "configured")
    assert page.locator("#robot-stt-note").is_hidden() and page.locator("#stt_model").is_visible()
    assert not errors
