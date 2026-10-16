"""Session-language target names: request contract, native adapter and retained/revealed name.

Synthetic transports only. These tests establish the contract, not model language accuracy.
"""
from __future__ import annotations

import asyncio
import copy
import json

import pytest
from test_ispy_target_variety import FRAMES, SESSION, Harness, Request, candidate
from test_kids_turn_deadlines_lifecycle import subscription_harness

from homebody.ispy import validate_ispy_target

NAMES = {
    "en": ("English", "chair"), "nl": ("Dutch", "stoel"),
    "es": ("Spanish", "silla"), "de": ("German", "Stuhl"),
    "fr": ("French", "chaise"), "it": ("Italian", "sedia"),
    "pt": ("Portuguese", "cadeira"),
}


def target(language):
    value = candidate(NAMES[language][1], hints_en=["You can sit on it."], hints_nl=["Je kunt erop zitten."])
    if language not in {"en", "nl"}:
        value[f"hints_{language}"] = [NAMES[language][1]]
    return value


def select(harness, language):
    # Production deliberately clears frame/prompt containers after the request. Snapshot the wire,
    # not FakeVision's mutable reference, without changing the real adapter harness.
    if harness.bridge.http is harness.vision:
        post = harness.vision.post

        def snapshot(url, *, json, **kwargs):
            return post(url, json=copy.deepcopy(json), **kwargs)

        harness.vision.post = snapshot
    return json.loads(asyncio.run(harness.bridge.kids_ispy_select(Request({
        "session_id": SESSION, "age_band": "7-9", "language": language, "frames_jpeg": list(FRAMES),
    }))).text)


def assert_contract(wire, language, *, native=False):
    language_name = NAMES[language][0]
    if native:
        schema = wire["text"]["format"]["schema"]
        prompt = wire["extra_body"]["input"][0]["content"][0]["text"]
    else:
        schema = wire["response_format"]["json_schema"]["schema"]
        prompt = wire["messages"][0]["content"][0]["text"]
    props = schema["properties"]["candidates"]["items"]["properties"]
    assert f"Write object_name in {language_name}" in prompt
    assert language_name in props["object_name"]["description"]
    assert "spoken verbatim" in props["object_name"]["description"]
    # Machine colour keys remain English, while names/hints are child-facing copy.
    assert props["colour"]["enum"] == [
        "red", "orange", "yellow", "green", "blue", "purple", "pink", "brown", "black", "white", "grey",
    ]
    if not native:  # Native adapter strips unsupported lengths; local validators retain this bound.
        assert props["object_name"]["maxLength"] == 60


@pytest.mark.parametrize("language", NAMES)
def test_selection_explicitly_requests_session_language_name(monkeypatch, language):
    harness = Harness(monkeypatch, [target(language)])
    selected = select(harness, language)["target"]
    assert_contract(harness.vision.requests[0], language)
    name = NAMES[language][1]
    assert selected["object_name"] == name
    assert harness.bridge._kids_sessions[SESSION]["ispy_target"] == selected
    assert validate_ispy_target(selected, frame_count=5, language=language).object_name == name
    assert name in harness.moderated[0]


@pytest.mark.parametrize("language", NAMES)
def test_language_contract_reaches_real_subscription_adapter(monkeypatch, language):
    harness, calls, _ = subscription_harness(monkeypatch, {"candidates": [target(language)]})
    assert select(harness, language)["target"]["object_name"] == NAMES[language][1]
    assert_contract(calls[0], language, native=True)


def test_request_local_schema_never_leaks_language_between_sessions(monkeypatch):
    harness = Harness(monkeypatch, [target("nl")])
    module = harness.module
    before = copy.deepcopy(module._ISPY_RESPONSE_SCHEMA)
    dutch = module._ispy_response_schema("nl")
    english = module._ispy_response_schema("en")
    german = module._ispy_response_schema("de")
    for schema, expected in [(dutch, "Dutch"), (english, "English"), (german, "German")]:
        assert expected in schema["properties"]["candidates"]["items"]["properties"]["object_name"]["description"]
    assert module._ISPY_RESPONSE_SCHEMA == before


@pytest.mark.parametrize("text,matched,guesses,hints", [
    ("stoel", True, 0, 0),
    ("ik weet het niet", False, 0, 0),
    ("tafel", False, 2, 0),
    ("hint", False, 0, 2),
])
def test_dutch_name_survives_all_reveal_paths_and_speech_approval(monkeypatch, text, matched, guesses, hints):
    harness = Harness(monkeypatch, [target("nl")])
    selected = select(harness, "nl")["target"]
    session = harness.bridge._kids_sessions[SESSION]
    session.update(ispy_guess_count=guesses, ispy_hints_given=hints)

    async def judge(guess, stored, *, language, openai_key):
        assert stored == selected and stored["object_name"] == "stoel" and language == "nl"
        return matched

    monkeypatch.setattr(harness.bridge, "_judge_ispy_guess", judge)
    payload = json.loads(asyncio.run(harness.bridge.kids_chat(Request({
        "input": text, "session_id": SESSION,
        "profile": {"age_band": "7-9", "activity": "ispy", "language": "nl"},
    }))).text)
    assert "stoel" in payload["text"] and "chair" not in payload["text"]
    assert payload["ispy_phase"] == "awaiting_clue"
    assert "ispy_target" not in session
    assert payload["text"] in harness.moderated
    assert harness.bridge._kids_speech_approvals[payload["speech_approval"]]["language"] == "nl"
