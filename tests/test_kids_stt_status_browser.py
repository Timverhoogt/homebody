# ruff: noqa: F811 -- imported fixture is intentionally a test parameter
"""Real Chromium: transcribing vs thinking vs speaking, and 'Local transcription' only when confirmed."""

from pathlib import Path

import pytest
from test_assistant_modes_browser import ui  # noqa: F401

SHOTS = Path(__file__).resolve().parents[1] / ".artifacts" / "kids-stt-status"
LOCAL_COPY = "Turning speech into text on your Hermes host"


def show(page, state, runtime):
    state["runtime"] = {"power_mode": "awake", "agent": {"profile": "conversation", "recent_activity": []}, **runtime}
    page.evaluate("refreshStatus()")


def kids_runtime(phase, locality):
    payload = {
        "state": phase,
        "kids_mode": {"active": True, "locked": True, "profile": {"activity": "quiz", "language": "nl"}},
    }
    if locality is not None:
        payload["stt_locality"] = locality
    return payload


@pytest.mark.parametrize("width", [320, 390])
@pytest.mark.parametrize("locality", ["local", "unknown", "cloud", None])
def test_kids_stage_names_local_transcription_only_when_confirmed(ui, width, locality):
    page, state, _posts, errors = ui
    page.set_viewport_size({"width": width, "height": 900})
    page.click("#tab-kids")
    show(page, state, kids_runtime("transcribing", locality))
    stage = page.locator("#kids-stage")
    title = stage.locator(".hb-scene-title")
    expected = "Local transcription" if locality == "local" else "Transcribing"
    page.wait_for_function(
        "expected => document.querySelector('#kids-stage .hb-scene-title').textContent === expected",
        arg=expected,
    )
    detail = stage.locator(".hb-scene-detail").inner_text()
    if locality == "local":
        assert LOCAL_COPY in detail
    else:
        assert "Hermes host" not in detail and "Local" not in title.inner_text()
        assert "local" not in detail.casefold()
    if locality == "cloud":
        assert "cloud" in detail
    # The calm thinking visuals stay; only the words distinguish transcription from reply work.
    assert stage.locator("[data-phase]").first.get_attribute("data-phase") == "thinking"
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    SHOTS.mkdir(parents=True, exist_ok=True)
    stage.scroll_into_view_if_needed()
    stage.screenshot(path=str(SHOTS / f"kids-transcribing-{locality or 'absent'}-{width}.png"))
    assert not errors


def test_kids_phases_progress_transcribing_thinking_speaking(ui):
    page, state, _posts, errors = ui
    page.click("#tab-kids")
    titles = []
    for phase in ("transcribing", "thinking", "speaking"):
        show(page, state, kids_runtime(phase, "local"))
        page.wait_for_function(
            "phase => document.querySelector('#kids-stage [data-phase]')?.dataset.phase !== undefined",
            arg=phase,
        )
        page.wait_for_timeout(150)
        titles.append(page.locator("#kids-stage .hb-scene-title").inner_text())
    assert titles == ["Local transcription", "Thinking", "Speaking"]
    assert not errors


@pytest.mark.parametrize("locality", ["local", "unknown", "cloud"])
def test_hero_headline_and_presence_for_adult_transcribing(ui, locality):
    page, state, _posts, errors = ui
    show(page, state, {"state": "transcribing", "stt_locality": locality, "kids_mode": {}})
    expected = "Local transcription…" if locality == "local" else "Transcribing …"
    page.wait_for_function(
        "expected => document.querySelector('#runtime-state').textContent === expected",
        arg=expected,
    )
    presence = page.locator("#hero-presence .hb-scene-detail").inner_text()
    assert (LOCAL_COPY in presence) == (locality == "local")
    show(page, state, {"state": "thinking", "stt_locality": locality, "kids_mode": {}})
    page.wait_for_function("document.querySelector('#runtime-state').textContent === 'Thinking …'")
    assert "transcri" not in page.locator("#hero-presence .hb-scene-title").inner_text().casefold()
    assert not errors


def test_owner_loss_hides_transcription_claim(ui):
    page, state, _posts, errors = ui
    page.click("#tab-kids")
    show(page, state, kids_runtime("transcribing", "local"))
    page.wait_for_function(
        "document.querySelector('#kids-stage .hb-scene-title').textContent === 'Local transcription'"
    )
    page.evaluate("HomebodyOwner.isOwner = () => false; refreshStatus()")
    page.wait_for_function(
        "document.querySelector('#kids-stage .hb-scene-title').textContent !== 'Local transcription'"
    )
    assert LOCAL_COPY not in page.locator("#kids-stage").inner_text()
    assert not errors


@pytest.mark.parametrize("width", [320, 390])
def test_receipt_sequence_never_leaves_a_stale_local_claim(ui, width):
    """Runtime enums after a cloud receipt, a custom provider and an STT error (pipeline-tested)."""
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 900})
    page.click("#tab-kids")
    stage = page.locator("#kids-stage")
    for phase, locality, title in [
        ("transcribing", "local", "Local transcription"),
        ("transcribing", "cloud", "Transcribing"),  # next turn after a cloud receipt
        ("transcribing", "unknown", "Transcribing"),  # custom provider receipt
        ("error", "unknown", None),  # transcription error clears provenance
    ]:
        show(page, state, kids_runtime(phase, locality))
        if title:
            page.wait_for_function(
                "t => document.querySelector('#kids-stage .hb-scene-title').textContent === t",
                arg=title,
            )
        else:
            page.wait_for_function(
                "document.querySelector('#kids-stage .hb-scene-title').textContent !== 'Transcribing'"
            )
        if locality != "local":
            assert "Hermes host" not in stage.inner_text()
    SHOTS.mkdir(parents=True, exist_ok=True)
    stage.scroll_into_view_if_needed()
    stage.screenshot(path=str(SHOTS / f"kids-after-error-{width}.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors and not [path for path, _ in posts if path.startswith("/api/kids")]
