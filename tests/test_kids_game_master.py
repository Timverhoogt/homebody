"""Kids game master: deterministic host commands, session lifecycle, adapters and boundaries."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from homebody.config import AppConfig
from homebody.hermes_client import HermesBridgeClient, HermesBridgeError
from homebody.kids_game_master import (
    ROUND_TURN_LIMITS,
    host_line,
    parse_host_command,
)
from homebody.kids_mode import KidsProfile
from homebody.runtime import HermesVoiceRuntime

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "homebody" / "static"


class FakeRobot:
    def __init__(self) -> None:
        self.media = SimpleNamespace()
        self.cancel_calls = 0

    def cancel_move(self) -> None:
        self.cancel_calls += 1


def wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


@pytest.fixture
def host(monkeypatch):
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._audio_ready = True
    spoken: list[tuple[str, dict[str, Any]]] = []
    reported: list[tuple[str, bool]] = []
    stops: list[dict[str, Any]] = []
    runtime.queue_announcement = lambda text, **kw: spoken.append((text, kw)) or {"ok": True}  # type: ignore[method-assign]
    runtime._notify_bridge_kids_session_async = lambda sid, active: reported.append((sid, active))  # type: ignore[method-assign]
    runtime._cancel_ispy_bridge_session = lambda sid: None  # type: ignore[method-assign]
    original_stop = runtime.stop_kids_mode

    def stop_without_fold(**kwargs):
        stops.append(kwargs)
        return original_stop(**{**kwargs, "fold": False})

    runtime.stop_kids_mode = stop_without_fold  # type: ignore[method-assign]
    yield SimpleNamespace(runtime=runtime, spoken=spoken, reported=reported, stops=stops)
    with runtime._kids_lock:
        for timer in (runtime._kids_timer, runtime._kids_warning_timer):
            if timer is not None:
                timer.cancel()


def start(host, **kwargs) -> dict[str, Any]:
    profile = KidsProfile(host=True, duration_minutes=15, **kwargs)
    return host.runtime.start_kids_mode(profile)


def host_state(runtime) -> dict[str, Any]:
    return runtime.status()["kids_mode"]["host"]


# ---- Deterministic command recognition (NL + EN, ambiguous inputs) ---------------------------

@pytest.mark.parametrize(
    ("text", "phase", "kind", "activity"),
    [
        ("Stop", "playing", "stop", ""),
        ("I'm done!", "playing", "stop", ""),
        ("ik wil stoppen", "playing", "stop", ""),
        ("Klaar.", "offer", "stop", ""),
        ("what can I say?", "playing", "help", ""),
        ("Wat kan ik zeggen", "playing", "help", ""),
        ("help", "lobby", "help", ""),
        ("ik weet het niet", "offer", "help", ""),
        ("again", "offer", "again", ""),
        ("nog een keer", "offer", "again", ""),
        ("let's do riddles", "playing", "switch", "riddles"),
        ("mogen we een verhaaltje", "playing", "switch", "story"),
        ("ik zie ik zie", "lobby", "switch", "ispy"),
        ("a story please", "offer", "switch", "story"),
        ("something else", "offer", "different", ""),
        ("iets anders", "playing", "different", ""),
        ("nee", "offer", "no", ""),
    ],
)
def test_parse_direct_child_commands_nl_en(text, phase, kind, activity) -> None:
    command = parse_host_command(text, phase=phase)
    assert (command.kind, command.activity) == (kind, activity)


@pytest.mark.parametrize(
    "text",
    [
        "the bus had to stop at the station and then everyone got out",
        'the dragon shouted "stop"',
        "“stop the game” said the knight",
        "help",  # mid-game help is a game move (hint), not a host command
        "ik weet het niet",  # I Spy / quiz give-up stays with the game
        "I don't know",
        "I give up",
        "play again",  # mid-game "again" follows the game's own rules
        "again",  # mid-game again belongs to the game
        "no",
        "I see a blue cup",
        "the princess wants to play riddles",  # narrated switch words are not child commands
        "de prinses wil nu raadsels spelen",
        "the princess liked her story about a cat in the garden",
        "",
    ],
)
def test_ambiguous_or_game_local_inputs_stay_with_the_game(text) -> None:
    assert parse_host_command(text, phase="playing").kind == "none"


def test_host_copy_is_short_bilingual_and_never_coercive() -> None:
    for language in ("en", "nl"):
        for key in ("lobby", "offer", "help", "camera_needed", "waiting"):
            line = host_line(language, key, a="story", b="riddles")
            assert len(line) < 200
            lowered = line.lower()
            assert "please stay" not in lowered and "don't go" not in lowered and "blijf" not in lowered
    assert "verhaal" in host_line("nl", "offer", a="story", b="riddles")
    assert "stop" in host_line("en", "offer", a="story", b="riddles")


# ---- Session lifecycle -----------------------------------------------------------------------

def test_host_start_opens_lobby_in_selected_language_with_two_choices(host) -> None:
    status = start(host, language="nl", nickname="Sam")
    assert status["host"] == {
        "enabled": True, "phase": "lobby", "activity": "", "choices": ["story", "riddles"], "notice": "",
        "available": ["story", "riddles", "quiz", "buddy", "calm"],
    }
    text, kwargs = host.spoken[-1]
    assert text.startswith("Hoi Sam!") and "verhaal" in text and "raadsels" in text
    assert kwargs["kids_listen_session_id"] == host.runtime._kids_session_id
    assert host.runtime._kids_voice_config(AppConfig()).kids_host_enabled is True


def test_profile_consent_rules_stay_strict_outside_host_sessions() -> None:
    with pytest.raises(ValueError, match="only valid for I Spy"):
        KidsProfile(activity="story", camera_consent=True)
    assert KidsProfile(host=True, camera_consent=True).camera_consent is True
    with pytest.raises(ValueError):
        KidsProfile(host="yes")  # type: ignore[arg-type]


def test_switch_invalidates_old_game_and_preserves_monotonic_deadline(host) -> None:
    runtime = host.runtime
    start(host)
    old_session, old_generation, deadline = runtime._kids_session_id, runtime._kids_generation, runtime._kids_ends_at
    runtime._kids_first_listen = (
        old_session, old_generation, runtime._kids_start_stop_generation, time.monotonic() + 5,
    )
    assert runtime._kids_host_handle_transcript(old_session, "let's do riddles") is True
    wait_for(lambda: host_state(runtime)["phase"] == "playing")

    assert runtime._kids_ends_at == deadline
    assert runtime._kids_session_id != old_session
    assert runtime._kids_generation > old_generation
    assert runtime._kids_session_is_current(old_session) is False
    assert runtime._kids_callback_is_current(old_generation) is False
    assert runtime._take_kids_first_listen(old_session) is None
    assert (old_session, False) in host.reported and (runtime._kids_session_id, True) in host.reported
    assert host_state(runtime)["activity"] == "riddles"
    assert runtime._kids_profile.activity == "riddles"
    text, kwargs = host.spoken[-1]
    assert text.startswith("Okay, riddles!") and kwargs["kids_listen_session_id"] == runtime._kids_session_id
    # Stale speech from the old generation can no longer be queued.
    before = len(host.spoken)
    assert runtime._kids_host_speak("stale", old_generation, phase="offer") is False
    assert len(host.spoken) == before
    # A stale transition callback is a no-op.
    runtime._kids_host_transition("quiz", old_generation)
    assert host_state(runtime)["activity"] == "riddles"


def test_switch_rearms_timers_without_extending_the_session(host) -> None:
    runtime = host.runtime
    start(host)
    with runtime._kids_lock:
        runtime._kids_ends_at = time.monotonic() + 200.0  # past the five-minute warning
        generation = runtime._kids_generation
    runtime._kids_host_choose("quiz", generation)
    wait_for(lambda: host_state(runtime)["phase"] == "playing")
    assert runtime._kids_warning_timer is None
    assert runtime._kids_timer is not None and runtime._kids_timer.interval <= 200.0
    assert runtime.status()["kids_mode"]["remaining_seconds"] <= 200


def test_voice_cannot_authorize_ispy_camera(host) -> None:
    runtime = host.runtime
    start(host)
    session = runtime._kids_session_id
    assert runtime._kids_host_handle_transcript(session, "I spy") is True
    assert host_state(runtime)["notice"] == "camera_consent_needed"
    assert host_state(runtime)["phase"] == "lobby"
    assert runtime._kids_session_id == session  # no transition, no camera
    assert runtime._kids_camera_active is False
    assert "grown-up" in host.spoken[-1][0]
    assert "ispy" not in host_state(runtime)["available"]


def test_caregiver_select_grants_ispy_and_successful_round_offers_before_search(host, monkeypatch) -> None:
    runtime = host.runtime
    start(host)
    searches: list[int] = []

    def fake_round(generation, profile, **_kw):
        assert profile.camera_consent is True
        searches.append(generation)
        return SimpleNamespace(clue=lambda language: "I spy something red.")

    runtime._prepare_ispy_round = fake_round  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="Camera opt-in"):
        runtime.kids_host_select("ispy")
    runtime.kids_host_select("ispy", camera_consent=True)
    wait_for(lambda: host_state(runtime)["phase"] == "playing")
    assert searches and host.spoken[-1][0].endswith("I spy something red.")
    session = runtime._kids_session_id
    # The bridge's hosted round outcome finishes: offer comes first, no automatic new search.
    assert runtime._kids_host_after_reply(session, "finished") is True
    assert len(searches) == 1
    state = host_state(runtime)
    assert state["phase"] == "offer" and state["choices"][0] == "again"
    assert "Another round of I Spy" in host.spoken[-1][0]
    # "again" runs a fresh, consented round in a new bridge session.
    assert runtime._kids_host_handle_transcript(session, "play again") is True
    wait_for(lambda: len(searches) == 2 and host_state(runtime)["phase"] == "playing")
    assert runtime._kids_session_id != session


def test_switching_away_from_ispy_revokes_capture_and_cancels_motion(host) -> None:
    runtime = host.runtime
    start(host, camera_consent=True)
    entered, release = threading.Event(), threading.Event()
    captured: list[bool] = []

    def slow_round(generation, profile, **_kw):
        with runtime._kids_lock:
            runtime._kids_camera_active = True
        entered.set()
        release.wait(2)
        captured.append(runtime._kids_callback_is_current(generation))
        runtime._assert_ispy_current(generation)
        return SimpleNamespace(clue=lambda language: "clue")

    runtime._prepare_ispy_round = slow_round  # type: ignore[method-assign]
    runtime._kids_host_choose("ispy", runtime._kids_generation)
    assert entered.wait(2)
    with runtime._kids_lock:
        generation = runtime._kids_generation
        runtime._kids_host_phase = "playing"
    runtime._kids_host_choose("story", generation)
    wait_for(lambda: host_state(runtime)["activity"] == "story")
    release.set()
    wait_for(lambda: captured)
    assert captured == [False]
    assert runtime._kids_camera_active is False
    assert runtime.robot.cancel_calls >= 1
    time.sleep(0.05)
    assert all("clue" not in text for text, _ in host.spoken)


def test_stop_during_switch_wins_over_greeting(host) -> None:
    runtime = host.runtime
    start(host, camera_consent=True)

    def stop_mid_search(generation, profile, **_kw):
        host.runtime.stop_kids_mode(reason="parent")
        runtime._assert_ispy_current(generation)
        return SimpleNamespace(clue=lambda language: "clue")

    runtime._prepare_ispy_round = stop_mid_search  # type: ignore[method-assign]
    before = len(host.spoken)
    runtime._kids_host_choose("ispy", runtime._kids_generation)
    wait_for(lambda: runtime.status()["kids_mode"]["active"] is False)
    time.sleep(0.05)
    assert len(host.spoken) == before
    assert host_state(runtime) == {
        "enabled": False, "phase": "", "activity": "", "choices": [], "notice": "", "available": [],
    }


def test_child_stop_ends_session_immediately_without_persuasion(host) -> None:
    runtime = host.runtime
    start(host, language="nl")
    before = len(host.spoken)
    receipt = runtime._conversation_stop_generation
    assert runtime._kids_host_handle_transcript(runtime._kids_session_id, "stop") is True
    assert runtime._conversation_stop_generation > receipt  # current turn cancelled synchronously
    wait_for(lambda: runtime.status()["kids_mode"]["active"] is False)
    assert host.stops[-1]["reason"] == "child_stop"
    assert len(host.spoken) == before  # no "are you sure" or invitation to keep playing


def test_offer_choices_again_different_no_and_bounded_reprompts(host) -> None:
    runtime = host.runtime
    start(host)
    runtime._kids_host_choose("story", runtime._kids_generation)
    wait_for(lambda: host_state(runtime)["phase"] == "playing")
    session = runtime._kids_session_id
    assert runtime._kids_host_after_reply(session, "playing") is False
    assert runtime._kids_host_after_reply(session, "finished") is True
    other = host_state(runtime)["choices"][1]
    assert other != "story"
    # Unrecognised replies are re-asked at most twice, then the host waits without listening.
    for _ in range(2):
        assert runtime._kids_host_handle_transcript(session, "banana") is True
        assert host.spoken[-1][1]["kids_listen_session_id"] == session
    assert runtime._kids_host_handle_transcript(session, "banana") is True
    assert host_state(runtime)["phase"] == "waiting"
    assert host.spoken[-1][1]["kids_listen_session_id"] == ""
    assert runtime._kids_host_handle_transcript(session, "something else") is True
    wait_for(lambda: host_state(runtime)["activity"] == other and host_state(runtime)["phase"] == "playing")
    session = runtime._kids_session_id
    runtime._kids_host_after_reply(session, "finished")
    assert runtime._kids_host_handle_transcript(session, "no thanks") is True
    wait_for(lambda: runtime.status()["kids_mode"]["active"] is False)


@pytest.mark.parametrize("activity", ["buddy", "story", "quiz", "riddles", "calm"])
def test_every_model_adapter_has_a_bounded_finish(host, activity) -> None:
    runtime = host.runtime
    start(host)
    runtime._kids_host_choose(activity, runtime._kids_generation)
    wait_for(lambda: host_state(runtime)["phase"] == "playing")
    session = runtime._kids_session_id
    for _ in range(ROUND_TURN_LIMITS[activity] - 1):
        assert runtime._kids_host_after_reply(session, "playing") is False
    assert runtime._kids_host_after_reply(session, "playing") is True
    assert host_state(runtime)["phase"] == "offer"


def test_mid_game_help_and_same_game_name_do_not_hijack_the_game(host) -> None:
    runtime = host.runtime
    start(host, camera_consent=True)
    runtime._prepare_ispy_round = lambda g, p, **k: SimpleNamespace(clue=lambda language: "clue")  # type: ignore[method-assign]
    runtime._kids_host_choose("ispy", runtime._kids_generation)
    wait_for(lambda: host_state(runtime)["phase"] == "playing")
    session = runtime._kids_session_id
    assert runtime._kids_host_handle_transcript(session, "I spy") is False
    assert runtime._kids_host_handle_transcript(session, "help") is False
    assert runtime._kids_host_handle_transcript(session, "is it the red cup") is False
    assert runtime._kids_host_handle_transcript(session, "what can I say") is True
    assert host_state(runtime)["phase"] == "playing"


def test_old_session_inputs_are_ignored_after_switch_end_and_privacy(host) -> None:
    runtime = host.runtime
    start(host)
    first = runtime._kids_session_id
    runtime._kids_host_choose("quiz", runtime._kids_generation)
    wait_for(lambda: host_state(runtime)["phase"] == "playing")
    assert runtime._kids_host_handle_transcript(first, "stop") is False
    assert runtime._kids_host_after_reply(first, "finished") is False
    second, generation = runtime._kids_session_id, runtime._kids_generation
    runtime._privacy_requested.set()
    try:
        assert runtime._kids_host_offer(generation, finished_activity="quiz") is False
    finally:
        runtime._privacy_requested.clear()
    runtime.stop_kids_mode(reason="parent")
    assert runtime._kids_host_handle_transcript(second, "stop") is False
    assert runtime._kids_host_after_reply(second, "finished") is False


def test_status_is_categorical_and_contains_no_private_text(host) -> None:
    runtime = host.runtime
    start(host, nickname="Secretname")
    runtime._kids_host_handle_transcript(runtime._kids_session_id, "my secret answer is purple")
    payload = json.dumps(runtime.status())
    assert "Secretname" not in payload and "purple" not in payload and "kids-" not in payload


# ---- Spoken and typed integration through the real conversation loop -------------------------

class FakeKidsClient:
    def __init__(self, config: AppConfig, replies: list[tuple[str, str]], calls: list[str]) -> None:
        self.config = config
        self._replies = replies
        self.calls = calls
        self.last_stt_provider = ""
        self.last_kids_ispy_role = ""
        self.last_kids_ispy_phase = ""
        self.last_kids_next_action = ""
        self.last_kids_game_state = ""

    def health(self) -> dict[str, object]:
        return {}

    def chat(self, transcript: str) -> str:
        self.calls.append(transcript)
        text, self.last_kids_game_state = self._replies.pop(0)
        return text

    def close(self) -> None:
        pass


def test_typed_and_conversation_loop_route_host_commands_and_finish(host, monkeypatch) -> None:
    runtime = host.runtime
    start(host)
    calls: list[str] = []
    replies = [("The end of our story.", "finished")]
    runtime._new_bridge_client = lambda config: FakeKidsClient(config, replies, calls)  # type: ignore[method-assign]
    runtime._play_kids_stream = lambda client, text, **kw: False  # type: ignore[method-assign]
    runtime._start_kids_ack = lambda *a, **k: None  # type: ignore[method-assign]

    config = runtime._kids_voice_config(AppConfig())
    runtime._kids_host_typed_handled = False
    runtime._accept_wake_turn()  # as submit_ispy_guess admits the typed turn
    runtime._run_conversation(config, typed_input="let's do a story")
    assert runtime._kids_host_typed_handled is True and calls == []
    wait_for(lambda: host_state(runtime)["phase"] == "playing")

    config = runtime._kids_voice_config(AppConfig())
    assert config.kids_activity == "story"
    runtime._accept_wake_turn()
    runtime._run_conversation(config, typed_input="a brave turtle")
    assert calls == ["a brave turtle"]
    assert host_state(runtime)["phase"] == "offer"
    assert "Another round of a story" in host.spoken[-1][0]


def test_spoken_transcript_reaches_host_before_model(host, monkeypatch) -> None:
    runtime = host.runtime
    start(host, language="nl")
    calls: list[str] = []
    runtime._new_bridge_client = lambda config: FakeKidsClient(config, [], calls)  # type: ignore[method-assign]
    runtime._start_kids_ack = lambda *a, **k: None  # type: ignore[method-assign]
    runtime._play_asset = lambda *_a, **_k: None  # type: ignore[method-assign]
    runtime._discard_audio = lambda *_a, **_k: None  # type: ignore[method-assign]
    import numpy as np

    runtime._record_utterance = lambda config: SimpleNamespace(  # type: ignore[method-assign]
        speech_detected=True, samples=np.ones(1600, dtype=np.float32),
    )

    class SpokenClient(FakeKidsClient):
        def transcribe(self, _wav: bytes) -> str:
            return "Raadsels!"

    runtime._new_bridge_client = lambda config: SpokenClient(config, [], calls)  # type: ignore[method-assign]
    runtime._accept_wake_turn()
    runtime._run_conversation(runtime._kids_voice_config(AppConfig()))
    assert calls == []
    wait_for(lambda: host_state(runtime)["activity"] == "riddles")
    assert host.spoken[-1][0].startswith("Oké, raadsels!")


# ---- Client and bridge: strict hosted finish schema with moderation intact -------------------

def hosted_client(handler) -> HermesBridgeClient:
    client = HermesBridgeClient(AppConfig(
        bridge_url="http://bridge", api_key="k", kids_mode_enabled=True, kids_session_id="kids-" + "a" * 32,
        kids_age_band="7-9", kids_activity="story", kids_host_enabled=True,
    ))
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_client_sends_hosted_and_rejects_non_categorical_outcomes() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "text": "Hi", "speech_approval": "a", "fallback_speech_approval": "b", "game_state": "done-ish",
        })

    client = hosted_client(handler)
    with pytest.raises(HermesBridgeError, match="game outcome"):
        client.chat("hello")
    assert seen[0]["hosted"] is True


def load_bridge(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    spec = importlib.util.spec_from_file_location("gm_bridge", ROOT / "companion" / "hermes_reachy_bridge.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, module.Bridge(api_key="secret", hermes_url="http://127.0.0.1:8642", profile=None)


def run_kids_chat(bridge, body, content, flagged=()):
    requests: list[dict[str, Any]] = []

    async def moderation(text: str, _key: str) -> bool:
        return text in flagged

    bridge._moderation_flagged = moderation

    class Upstream:
        status = 200

        async def json(self, **_kwargs):
            return {"choices": [{"message": {"content": content}}]}

    class Context:
        async def __aenter__(self):
            return Upstream()

        async def __aexit__(self, *_args):
            return None

    class Http:
        def post(self, *_args, json=None, **_kwargs):
            requests.append(json)
            return Context()

    class Request:
        headers = {"Authorization": "Bearer secret"}

        async def json(self):
            return body

    bridge.http = Http()
    return asyncio.run(bridge.kids_chat(Request())), requests


def body(activity="story", **extra):
    return {
        "input": "the end", "session_id": "kids-" + "b" * 32,
        "profile": {"age_band": "7-9", "activity": activity, "language": "en"}, **extra,
    }


def test_bridge_hosted_turn_uses_strict_schema_and_reports_finish(monkeypatch) -> None:
    module, bridge = load_bridge(monkeypatch)
    response, requests = run_kids_chat(
        bridge, body(hosted=True), json.dumps({"reply": "And they all slept well.", "game_state": "finished"}),
    )
    payload = json.loads(response.text)
    assert payload["game_state"] == "finished" and payload["text"] == "And they all slept well."
    fmt = requests[0]["response_format"]
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["properties"]["game_state"]["enum"] == ["playing", "finished"]
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False


def test_bridge_hosted_turn_rejects_prose_and_moderation_still_wins(monkeypatch) -> None:
    from aiohttp import web

    _module, bridge = load_bridge(monkeypatch)
    with pytest.raises(web.HTTPBadGateway):
        run_kids_chat(bridge, body(hosted=True), "The story is finished now.")
    with pytest.raises(web.HTTPBadGateway):
        run_kids_chat(bridge, body(hosted=True, input="x") | {"session_id": "kids-" + "c" * 32},
                      json.dumps({"reply": "x", "game_state": "finished", "extra": 1}))
    response, _ = run_kids_chat(
        bridge, body() | {"session_id": "kids-" + "d" * 32, "hosted": True},
        json.dumps({"reply": "unsafe words", "game_state": "finished"}), flagged=("unsafe words",),
    )
    payload = json.loads(response.text)
    assert payload["game_state"] == "playing" and "trusted grown-up" in payload["text"]
    with pytest.raises(web.HTTPBadRequest):
        run_kids_chat(bridge, body(hosted="yes") | {"session_id": "kids-" + "e" * 32}, "{}")


def test_bridge_unhosted_chat_is_unchanged(monkeypatch) -> None:
    _module, bridge = load_bridge(monkeypatch)
    response, requests = run_kids_chat(bridge, body(), "Plain prose reply.")
    payload = json.loads(response.text)
    assert payload["text"] == "Plain prose reply." and "game_state" not in payload
    assert "response_format" not in requests[0]


def test_bridge_hosted_ispy_player_round_finishes_without_search(monkeypatch) -> None:
    _module, bridge = load_bridge(monkeypatch)
    session_id = "kids-" + "f" * 32
    bridge._kids_sessions[session_id] = {
        "profile": ("7-9", "ispy", "en", True), "history": [], "updated_at": time.monotonic(),
        "ispy_role": "player_picker", "ispy_player_phase": "awaiting_confirmation",
        "ispy_player_clues": ["red"], "ispy_player_guesses": ["apple"],
    }
    response, _ = run_kids_chat(
        bridge, {**body("ispy", hosted=True), "input": "yes", "session_id": session_id}, "",
    )
    payload = json.loads(response.text)
    assert payload["ispy_next_action"] == "" and payload["game_state"] == "finished"
    assert "look around" not in payload["text"]
    # Hosted and unhosted fingerprints cannot be mixed within one bridge session.
    from aiohttp import web

    with pytest.raises(web.HTTPConflict):
        run_kids_chat(bridge, {**body("ispy"), "input": "yes", "session_id": session_id}, "")


# ---- Static UI contract ----------------------------------------------------------------------

def test_kids_page_makes_play_together_primary_and_keeps_direct_controls() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert html.index('id="kids-host-start-button"') < html.index('id="kids-start-button"')
    assert 'id="kids-stop-button"' in html and "data-kids-activity=\"ispy\"" in html
    assert 'data-kids-host-activity="ispy"' in html
    scene = (STATIC / "scene.js").read_text(encoding="utf-8")
    assert "host: {" in scene and '"switching"' in scene and '"offer"' in scene
    for forbidden in ("fetch(", "XMLHttpRequest", "localStorage", "getUserMedia"):
        assert forbidden not in scene
