"""Kids turn deadlines, I Spy at the subscription-adapter boundary, fixed-line lifecycle and copy drift.

Synthetic fixtures only: fake model/moderation/vision transports, a fake subscription client and the
real announcement worker/wake loop with hardware edges faked. No provider, robot or media access.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from test_ispy_target_variety import CHAIR, CUSHION, LAMP, SESSION, Harness, Request, candidate
from test_kids_interaction_fixes import StartHarness, _SpeechClient, profile_for, wait_for
from test_kids_story_ispy_speech import ModelHttp, chat, story_bridge

from companion import kids_fixed_copy as catalog
from companion import subscription_reasoning as sub
from homebody.hermes_client import KIDS_CHAT_TIMEOUT_SECONDS, HermesBridgeError
from homebody.kids_game_master import host_line
from homebody.kids_mode import KidsProfile, kids_greeting, kids_message


async def _hang(*_args: object, **_kwargs: object) -> bool:
    await asyncio.Event().wait()
    return False


async def _safe(*_args: object, **_kwargs: object) -> bool:
    return False


# --- Whole-turn and per-stage deadlines --------------------------------------------------------


def test_budgets_are_aligned_and_stage_ceilings_fit_the_whole_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    module, _bridge = story_bridge(monkeypatch, ModelHttp())
    stages = 2 * module._KIDS_MODERATION_TIMEOUT_SECONDS + module._KIDS_MODEL_TIMEOUT_SECONDS
    assert module._KIDS_TURN_BUDGET_SECONDS <= stages  # the whole-turn budget, not the sum, is the bound
    assert module._KIDS_TURN_BUDGET_SECONDS < KIDS_CHAT_TIMEOUT_SECONDS  # the bridge answers before Reachy gives up


@pytest.mark.parametrize("stage", ["input", "output"])
def test_hanging_moderation_gives_retry_without_model_output_or_history(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    module, bridge = story_bridge(monkeypatch, ModelHttp(content="A secret model answer."))
    monkeypatch.setattr(module, "_KIDS_MODERATION_TIMEOUT_SECONDS", 0.05)
    calls: list[str] = []

    async def moderation(text: str, _key: str) -> bool:
        calls.append(text)
        if stage == "input" or len(calls) == 2:
            await asyncio.Event().wait()
        return False

    bridge._moderation_flagged = moderation  # type: ignore[assignment]
    started = time.monotonic()
    payload = json.loads(asyncio.run(bridge.kids_chat(chat())).text)
    assert time.monotonic() - started < 2.0
    assert payload["retry"] is True and payload["text"] == module._KIDS_MODEL_RETRY_REPLY["nl"]
    assert bridge._kids_sessions[SESSION]["history"] == []
    assert len(bridge.http.model_kwargs) == (0 if stage == "input" else 1)  # unscreened input never sent
    approved = {value["text_digest"] for value in bridge._kids_speech_approvals.values()}
    assert len(approved) == 1  # only the fixed retry line, never the unscreened model answer


def test_whole_turn_budget_bounds_a_hanging_model_below_its_own_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    module, bridge = story_bridge(monkeypatch, ModelHttp())
    monkeypatch.setattr(module, "_KIDS_TURN_BUDGET_SECONDS", 0.1)
    bridge.http.gate = asyncio.Event()  # never released
    started = time.monotonic()
    payload = json.loads(asyncio.run(bridge.kids_chat(chat())).text)
    assert time.monotonic() - started < 2.0 and payload["retry"] is True


def test_moderation_failure_after_end_issues_no_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    module, bridge = story_bridge(monkeypatch, ModelHttp())
    calls: list[str] = []

    async def moderation(text: str, _key: str) -> bool:
        calls.append(text)
        if len(calls) == 2:  # output screening fails after the session ended
            bridge._end_kids_live(SESSION)
            raise module.web.HTTPServiceUnavailable(text="Kids Mode safety screening is unavailable")
        return False

    bridge._moderation_flagged = moderation  # type: ignore[assignment]
    with pytest.raises(module.web.HTTPConflict):
        asyncio.run(bridge.kids_chat(chat()))
    assert not bridge._kids_speech_approvals


def test_cancellation_is_not_turned_into_a_spoken_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    _module, bridge = story_bridge(monkeypatch, ModelHttp())
    bridge.http.gate = asyncio.Event()

    async def scenario() -> None:
        task = asyncio.create_task(bridge.kids_chat(chat()))
        for _ in range(50):
            await asyncio.sleep(0)
            if bridge.http.model_kwargs:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not bridge._kids_speech_approvals and bridge._kids_sessions[SESSION]["history"] == []


def ispy_chat(text: str = "a sofa") -> Request:
    return Request({"input": text, "session_id": SESSION, "profile": {
        "age_band": "7-9", "activity": "ispy", "language": "en"}})


def test_ispy_judge_timeout_retries_without_spending_a_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(monkeypatch, [CHAIR])
    harness.run()
    monkeypatch.setattr(harness.module, "_KIDS_ISPY_MODEL_TIMEOUT_SECONDS", 0.05)
    harness.bridge._judge_ispy_guess = _hang  # type: ignore[assignment]
    payload = json.loads(asyncio.run(harness.bridge.kids_chat(ispy_chat())).text)
    assert payload["retry"] is True
    assert (payload["ispy_role"], payload["ispy_phase"], payload["ispy_next_action"]) == (
        "reachy_picker", "reachy_guessing", "")
    session = harness.bridge._kids_sessions[SESSION]
    assert session["ispy_guess_count"] == 0 and session["ispy_target"]["object_name"] == "chair"


def test_ispy_output_screening_failure_rolls_back_the_game_step(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(monkeypatch, [CHAIR])
    harness.run()
    monkeypatch.setattr(harness.module, "_KIDS_MODERATION_TIMEOUT_SECONDS", 0.05)
    harness.bridge._judge_ispy_guess = _safe  # type: ignore[assignment]
    calls: list[str] = []

    async def moderation(text: str, _key: str) -> bool:
        calls.append(text)
        if len(calls) == 2:
            await asyncio.Event().wait()
        return False

    harness.bridge._moderation_flagged = moderation  # type: ignore[assignment]
    before = dict(harness.bridge._kids_sessions[SESSION])
    payload = json.loads(asyncio.run(harness.bridge.kids_chat(ispy_chat())).text)
    assert payload["retry"] is True
    after = harness.bridge._kids_sessions[SESSION]
    assert {k: v for k, v in after.items() if k != "updated_at"} == {
        k: v for k, v in before.items() if k != "updated_at"}


def test_ispy_player_guess_timeout_asks_for_another_clue(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(monkeypatch, [CHAIR])
    harness.run()
    monkeypatch.setattr(harness.module, "_KIDS_ISPY_MODEL_TIMEOUT_SECONDS", 0.05)
    harness.bridge._kids_sessions[SESSION].update({
        "ispy_role": "player_picker", "ispy_player_phase": "awaiting_clue",
        "ispy_player_clues": [], "ispy_player_guesses": [],
    })
    harness.bridge._guess_player_ispy_object = _hang  # type: ignore[assignment]
    payload = json.loads(asyncio.run(harness.bridge.kids_chat(ispy_chat("It is round and blue"))).text)
    assert payload["text"] == "I'm not sure yet. Please give me a different clue."
    assert harness.bridge._kids_sessions[SESSION]["ispy_player_phase"] == "awaiting_clue"


# --- I Spy at the real subscription adapter boundary -------------------------------------------


def subscription_harness(monkeypatch: pytest.MonkeyPatch, reply: object):
    """Real provider/transport and the REAL installed Codex adapter over a fake Responses transport.

    Skips (with the exact missing dependency) when the installed Hermes source is not importable.
    """
    from test_subscription_structured_output import codex_client, provider

    harness = Harness(monkeypatch, [CHAIR])
    content = reply if isinstance(reply, str) else json.dumps(reply)
    _client, transport = codex_client(monkeypatch, [content])
    harness.bridge.llm = provider()
    harness.bridge.http = sub.SubscriptionHttp(harness.vision, harness.bridge.llm)
    return harness, transport.responses.calls, None


def test_subscription_ispy_schema_is_native_strict_output_on_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    harness, calls, _ = subscription_harness(monkeypatch, {"candidates": [CHAIR]})
    assert harness.run()["target"]["object_name"] == "chair"
    [wire] = calls
    text_format = wire["text"]["format"]
    assert text_format["name"] == "kids_ispy_candidates" and text_format["strict"] is True
    assert text_format["schema"]["properties"]["candidates"]["maxItems"] == 4
    assert "minLength" not in json.dumps(text_format)  # host-only; the bridge validator enforces lengths
    assert harness.vision.requests == []  # no paid API-key HTTP request was made


def test_subscription_ispy_fails_closed_when_the_schema_cannot_be_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs everywhere: an adapter that is not the installed Codex adapter never gets a schema request."""
    harness = Harness(monkeypatch, [CHAIR])
    created: list[object] = []
    client = SimpleNamespace(base_url="https://chatgpt.com/backend-api/codex/", chat=SimpleNamespace(
        completions=SimpleNamespace(
            create=lambda **kwargs: created.append(kwargs),
            _build_responses_kwargs=lambda _kwargs: ({"reasoning": {"effort": "none"}}, None, None),
        )))
    monkeypatch.setattr(sub, "_resolve_client", lambda _model: client)
    harness.bridge.llm = sub.SubscriptionTextProvider("m", models={"agent": "m", "kids": "m", "ispy": "m"})
    harness.bridge.http = sub.SubscriptionHttp(harness.vision, harness.bridge.llm)
    with pytest.raises(harness.module.web.HTTPServiceUnavailable):
        harness.run()
    assert created == [] and SESSION not in harness.bridge._kids_sessions and harness.moderated == []


@pytest.mark.parametrize(
    "reply,outcome,metadata",
    [
        ({"candidates": []}, "no candidates", "candidates=0"),
        ({"items": [CHAIR]}, "invalid candidate pool", "candidates=-1"),
        ({"candidates": [candidate("person", confidence=True), candidate("phone", frame_index=999),
                         candidate("knife", bbox=[0, 0, 0, 0]),
                         candidate("vase", visible_frame_count=0)]}, "no usable candidate", "candidates=4"),
        ({"candidates": [CHAIR] * 17}, "oversized candidate pool", "candidates=17"),
    ],
)
def test_subscription_pool_outcomes_fail_closed_with_content_free_diagnostics(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, reply: object, outcome: str, metadata: str
) -> None:
    harness, _created, _wire = subscription_harness(monkeypatch, reply)
    caplog.set_level(logging.INFO)
    with pytest.raises(harness.module.web.HTTPBadGateway):
        harness.run()  # provider 200 is not schema acceptance
    assert SESSION not in harness.bridge._kids_sessions and harness.moderated == []
    assert outcome in caplog.text and metadata in caplog.text and "provider=openai-codex" in caplog.text
    for word in ("chair", "person", "phone", "knife", "vase", "not json"):
        assert word not in caplog.text


def test_subscription_oversized_pool_is_rejected_without_truncation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pool = [CHAIR, LAMP, CUSHION, candidate("bookshelf", colour="brown"), candidate("mug", colour="red")]
    harness, _created, _wire = subscription_harness(monkeypatch, {"candidates": pool})
    caplog.set_level(logging.INFO)
    with pytest.raises(harness.module.web.HTTPBadGateway):
        harness.run()
    assert harness.rng.seen == [] and harness.moderated == []
    assert "candidates=5" in caplog.text and "oversized candidate pool" in caplog.text
    assert "chair" not in caplog.text


def test_subscription_non_json_fails_closed_before_the_pool_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    harness, calls, _ = subscription_harness(monkeypatch, "not json at all")
    with pytest.raises(harness.module.web.HTTPServiceUnavailable):
        harness.run()
    assert len(calls) == 1
    assert SESSION not in harness.bridge._kids_sessions and harness.moderated == []


# --- Robot: fixed-line approval failures never strand a silent Kids session --------------------


def test_greeting_sends_only_catalog_selectors(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = StartHarness(monkeypatch)
    try:
        harness.start_threads()
        harness.runtime.start_kids_mode(profile_for("story", "nl", nickname="Sam"), greet=True)
        assert harness.playback_started.wait(2)
        assert _SpeechClient.approved_lines == [({"kind": "greeting"},)]
    finally:
        harness.close()


@pytest.mark.parametrize("error", [HermesBridgeError("HTTP 503"), HermesBridgeError("HTTP 404")])
def test_failed_greeting_approval_ends_the_session_without_listening(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    harness = StartHarness(monkeypatch)
    _SpeechClient.approve_error = error
    try:
        harness.start_threads()
        harness.runtime.start_kids_mode(profile_for("story", "nl"), greet=True)
        assert wait_for(lambda: not harness.runtime._kids_active)
        assert harness.runtime._kids_last_end_reason.startswith("speech_failed")
        harness.settle()
        assert harness.conversations == [] and harness.runtime._kids_first_listen is None
        assert not harness.playback_started.is_set()
    finally:
        harness.close()


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_held_approval_across_end_never_plays_listens_or_touches_later_state(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    harness = StartHarness(monkeypatch)
    _SpeechClient.approve_entered, _SpeechClient.approve_release = threading.Event(), threading.Event()
    if outcome == "failure":
        _SpeechClient.approve_error = HermesBridgeError("HTTP 503")
    try:
        harness.start_threads()
        harness.runtime.start_kids_mode(profile_for("story", "en"), greet=True)
        assert _SpeechClient.approve_entered.wait(2)
        harness.runtime.stop_kids_mode(reason="parent", fold=False)
        _SpeechClient.approve_release.set()
        harness.settle()
        assert not harness.playback_started.is_set() and harness.conversations == []
        assert harness.runtime._kids_first_listen is None
        assert harness.runtime._kids_last_end_reason == "parent"  # the late result changed nothing
    finally:
        harness.close()


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_held_approval_across_replacement_never_ends_or_listens_for_the_new_session(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    harness = StartHarness(monkeypatch)
    _SpeechClient.approve_entered, _SpeechClient.approve_release = threading.Event(), threading.Event()
    if outcome == "failure":
        _SpeechClient.approve_error = HermesBridgeError("HTTP 503")
    try:
        harness.start_threads()
        harness.runtime.start_kids_mode(profile_for("quiz", "en"), greet=True)
        assert _SpeechClient.approve_entered.wait(2)
        harness.runtime.start_kids_mode(profile_for("riddles", "en"), greet=False)
        replacement = harness.runtime._kids_session_id
        _SpeechClient.approve_release.set()
        harness.settle()
        assert harness.runtime._kids_active and harness.runtime._kids_session_id == replacement
        assert not harness.playback_started.is_set() and harness.conversations == []
        assert harness.runtime._kids_first_listen is None
    finally:
        harness.close()


def test_stop_after_approval_but_before_first_listen_arm_never_listens(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = StartHarness(monkeypatch)
    try:
        harness.start_threads()
        harness.runtime.start_kids_mode(profile_for("story", "en"), greet=True)
        assert harness.playback_started.wait(2)  # approved and playing; arming happens only after playback
        harness.runtime._request_conversation_stop()  # a Stop, not an End, before the arm
        harness.release_playback.set()
        harness.settle()
        assert harness.conversations == [] and harness.runtime._kids_first_listen is None
        assert harness.runtime._kids_active  # Stop is not End; the session waits for a wake
    finally:
        harness.close()


# --- Robot copy and the bridge catalog cannot drift ---------------------------------------------


@pytest.mark.parametrize("language", catalog.LANGUAGES)
def test_robot_preview_copy_matches_the_bridge_catalog(language: str) -> None:
    for activity in catalog.ACTIVITIES:
        profile = KidsProfile(activity=activity, language=language, camera_consent=activity == "ispy")
        assert kids_greeting(profile) == catalog.render_segment(
            {"kind": "greeting"}, activity=activity, language=language)
        for key, slots in catalog.HOST_SLOTS.items():
            for a in catalog.ACTIVITIES:
                args = dict(zip(slots, (a, "story"), strict=False))
                rendered = catalog.render_segment({"kind": "host", "key": key, **args}, activity=activity,
                                                  language=language)
                assert host_line(language, key, **args) == rendered
    assert kids_message(language, "five_minutes") == catalog.render_segment(
        {"kind": "notice", "key": "five_minutes"}, activity="story", language=language)
