"""Regressions for Kids language, Start-to-first-turn, empty-turn retries, STT latency and I Spy rules.

All audio, transcripts and targets here are synthetic fixtures.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from companion import local_stt_command
from homebody.audio import EndpointResult
from homebody.config import AppConfig
from homebody.hermes_client import HermesBridgeClient, SpeechAudio
from homebody.kids_mode import ACTIVITIES, KidsProfile, kids_greeting, kids_message
from homebody.runtime import HermesVoiceRuntime

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "companion" / "hermes_reachy_bridge.py"
WAKE_PHRASES = ("Hey Homebody", "Hey Hermes", "Okay Nabu", "Hey Reachy")


def load_bridge_module():
    spec = importlib.util.spec_from_file_location("hermes_reachy_bridge_kids_fixes", BRIDGE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def profile_for(activity: str, language: str, nickname: str = "") -> KidsProfile:
    return KidsProfile(
        nickname=nickname,
        activity=activity,
        language=language,
        camera_consent=activity == "ispy",
    )


# --- 1. Deterministic language-selected fixed copy -------------------------------------------


def test_dutch_story_greeting_is_dutch_not_english() -> None:
    greeting = kids_greeting(KidsProfile(language="nl", activity="story"))
    assert greeting == "Verhaaltijd staat klaar. Over wie zal ons verhaal gaan?"
    assert "Story time" not in greeting


@pytest.mark.parametrize("activity", sorted(ACTIVITIES))
def test_every_greeting_is_localized_and_invites_an_immediate_answer(activity: str) -> None:
    english = kids_greeting(profile_for(activity, "en"))
    dutch = kids_greeting(profile_for(activity, "nl"))
    assert english != dutch
    for greeting in (english, dutch):
        assert not any(phrase.casefold() in greeting.casefold() for phrase in WAKE_PHRASES)
        if activity != "ispy":
            assert greeting.endswith("?")
    assert kids_greeting(profile_for(activity, "nl", "Sam")).startswith("Hoi Sam. ")
    assert kids_greeting(profile_for(activity, "en", "Sam")).startswith("Hi Sam. ")
    # Deterministic: the same profile always yields the same copy (no model involvement).
    assert kids_greeting(profile_for(activity, "nl")) == dutch


def test_five_minute_warning_uses_the_session_language() -> None:
    runtime = HermesVoiceRuntime(SimpleNamespace(media=SimpleNamespace()), threading.Event())
    runtime._audio_ready = True
    runtime._notify_bridge_kids_session_async = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    queued: list[str] = []
    runtime.queue_announcement = lambda text, **_kwargs: queued.append(text)  # type: ignore[method-assign]
    runtime._request_conversation_stop = lambda: None  # type: ignore[method-assign]
    runtime._clear_streamed_audio = lambda: None  # type: ignore[method-assign]
    runtime.start_kids_mode(profile_for("quiz", "nl"), greet=False)
    runtime._warn_kids_mode(runtime._kids_generation)
    assert queued == [kids_message("nl", "five_minutes")]
    assert queued[0].startswith("Nog vijf minuten")
    runtime._warn_kids_mode(runtime._kids_generation - 1)  # stale timer: ignored
    assert len(queued) == 1


def test_bridge_safety_and_fallback_copy_is_localized() -> None:
    source = BRIDGE_PATH.read_text(encoding="utf-8")
    assert "Daar kan ik niet mee helpen. Vertel het nu aan een volwassene" in source
    assert "Daar heb ik geen antwoord op." in source


# --- 2. Parent Start opens exactly one first listening turn -----------------------------------


class _Media:
    def __init__(self) -> None:
        self.audio = SimpleNamespace(clear_player=lambda: None)
        self.played: list[str] = []

    def play_sound(self, path: str) -> None:
        self.played.append(path)


class _Robot:
    def __init__(self) -> None:
        self.media = _Media()

    def cancel_move(self) -> None:
        pass


class _Spotter:
    def accept(self, _frame: object, _rate: int) -> str:
        return ""

    def reset(self) -> None:
        pass


class _SpeechClient:
    fail = False

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def synthesize(self, _text: str) -> SpeechAudio:
        if _SpeechClient.fail:
            raise RuntimeError("synthetic TTS failure")
        return SpeechAudio(b"audio", "audio/mpeg", ".mp3", "fake-tts")

    # Kids greetings/host lines: exact-text approval, then the Kids voice stream (no generic TTS).
    last_tts_provider = "fake-kids-stream"

    # Optional hold point for lifecycle tests: entered is set, then the approval waits for release.
    approve_entered: threading.Event | None = None
    approve_release: threading.Event | None = None
    approve_error: Exception | None = None
    approved_lines: list[object] = []

    def approve_kids_fixed_speech(self, lines) -> str:
        assert self.config.kids_mode_enabled and lines
        _SpeechClient.approved_lines.append(lines)
        if _SpeechClient.approve_entered is not None and _SpeechClient.approve_release is not None:
            _SpeechClient.approve_entered.set()
            assert _SpeechClient.approve_release.wait(5)
        if _SpeechClient.approve_error is not None:
            raise _SpeechClient.approve_error
        if _SpeechClient.fail:
            raise RuntimeError("synthetic TTS failure")
        return "Server-rendered fixed line."

    def iter_kids_speech(self, _text: str, *, should_stop=None):
        yield b"\x00\x00" * 2400

    def close(self) -> None:
        pass


class StartHarness:
    """Real announcement worker + real wake loop; only hardware/network edges are faked."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, fail_tts: bool = False) -> None:
        _SpeechClient.fail = fail_tts
        _SpeechClient.approve_entered = _SpeechClient.approve_release = None
        _SpeechClient.approve_error = None
        _SpeechClient.approved_lines = []
        monkeypatch.setattr("homebody.runtime.HermesBridgeClient", _SpeechClient)
        self.stop_event = threading.Event()
        runtime = HermesVoiceRuntime(
            _Robot(),
            self.stop_event,
            config_loader=lambda: AppConfig(api_key="test-key"),
        )
        self.runtime = runtime
        runtime._audio_ready = True
        runtime._power_mode = "standby"
        runtime._spotter = _Spotter()  # type: ignore[assignment]
        runtime._notify_bridge_kids_session_async = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        runtime._apply_power_mode = lambda: None  # type: ignore[method-assign]
        runtime._orient_to_voice = lambda _config: None  # type: ignore[method-assign]
        runtime.observe_presence = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        runtime._set_face_tracking = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        runtime._fold_head_before_torque_release = lambda: None  # type: ignore[method-assign]
        runtime._take_button_wake = lambda: False  # type: ignore[method-assign]

        def read_frame() -> None:
            self.stop_event.wait(0.01)
            return None

        runtime._read_16k_frame = read_frame  # type: ignore[method-assign]

        def power(mode: str, **_kwargs: object) -> dict[str, object]:
            runtime._power_mode = mode
            runtime._motors_enabled = mode == "awake"
            return {}

        runtime.set_power_mode = power  # type: ignore[method-assign]

        def motor(enabled: bool, **_kwargs: object) -> None:
            runtime._motors_enabled = enabled
            runtime._power_mode = "awake"

        runtime._set_motor_mode = motor  # type: ignore[method-assign]
        self.release_playback = threading.Event()
        self.playback_started = threading.Event()
        self.played: list[str] = []

        def play(_item: object, _speech: object, text: str) -> None:
            self.playback_started.set()
            assert self.release_playback.wait(5)
            self.played.append(text)

        runtime._play_announcement_audio = play  # type: ignore[method-assign]
        self.conversations: list[tuple[str, str]] = []

        def conversation(config: AppConfig, keyword: str) -> None:
            self.conversations.append((config.kids_session_id, keyword))

        runtime._run_selected_voice_conversation = conversation  # type: ignore[method-assign]
        runtime._announcement_worker = object()  # type: ignore[assignment]
        self.threads: list[threading.Thread] = []

    def start_threads(self) -> None:
        worker = threading.Thread(target=self.runtime._run_announcement_worker, daemon=True)
        self.runtime._announcement_worker = worker
        wake = threading.Thread(target=self.runtime._listen_for_wake_word, daemon=True)
        self.threads = [worker, wake]
        for thread in self.threads:
            thread.start()

    def settle(self, seconds: float = 0.4) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(0.02)

    def close(self) -> None:
        self.release_playback.set()
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=2)
            assert not thread.is_alive()


def wait_for(predicate, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_start_greeting_transitions_into_exactly_one_first_listening_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = StartHarness(monkeypatch)
    try:
        harness.start_threads()
        harness.runtime.start_kids_mode(profile_for("story", "nl"), greet=True)
        session_id = harness.runtime._kids_session_id
        assert harness.playback_started.wait(2)
        harness.settle(0.2)
        assert harness.conversations == []  # never listens over its own greeting
        harness.release_playback.set()
        assert wait_for(lambda: harness.conversations)
        harness.settle()
        assert harness.played == ["Verhaaltijd staat klaar. Over wie zal ons verhaal gaan?"]
        assert harness.conversations == [(session_id, "kids_start")]  # single-use ticket
    finally:
        harness.close()


def test_end_while_greeting_plays_never_starts_listening(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = StartHarness(monkeypatch)
    try:
        harness.start_threads()
        harness.runtime.start_kids_mode(profile_for("quiz", "en"), greet=True)
        assert harness.playback_started.wait(2)
        harness.runtime.stop_kids_mode(fold=False)
        harness.release_playback.set()
        harness.settle()
        assert harness.conversations == []
        assert harness.runtime._kids_first_listen is None
    finally:
        harness.close()


def test_replacement_while_old_greeting_plays_does_not_listen_for_either_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = StartHarness(monkeypatch)
    try:
        harness.start_threads()
        harness.runtime.start_kids_mode(profile_for("quiz", "en"), greet=True)
        assert harness.playback_started.wait(2)
        harness.runtime.start_kids_mode(profile_for("riddles", "en"), greet=False)
        harness.release_playback.set()
        harness.settle()
        assert harness.conversations == []
    finally:
        harness.close()


def test_failed_greeting_never_admits_listening(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = StartHarness(monkeypatch, fail_tts=True)
    try:
        harness.start_threads()
        harness.runtime.start_kids_mode(profile_for("buddy", "nl"), greet=True)
        harness.release_playback.set()
        assert wait_for(lambda: harness.runtime._status.announcement_last_error != "")
        harness.settle()
        assert harness.conversations == []
    finally:
        harness.close()


def test_first_listen_ticket_rejects_privacy_expiry_shutdown_and_other_sessions() -> None:
    runtime = HermesVoiceRuntime(_Robot(), threading.Event())
    runtime._audio_ready = True
    runtime._notify_bridge_kids_session_async = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    runtime.start_kids_mode(profile_for("calm", "en"), greet=False)
    session_id = runtime._kids_session_id

    assert runtime._arm_kids_first_listen("kids-" + "0" * 32) is False
    assert runtime._arm_kids_first_listen(session_id) is True
    assert runtime._take_kids_first_listen("kids-" + "0" * 32) is None  # consumed, wrong session
    assert runtime._take_kids_first_listen(session_id) is None  # single use

    runtime._privacy_requested.set()
    assert runtime._arm_kids_first_listen(session_id) is False
    runtime._privacy_requested.clear()

    assert runtime._arm_kids_first_listen(session_id) is True
    ticket = runtime._kids_first_listen
    assert ticket is not None
    runtime._kids_first_listen = (*ticket[:3], time.monotonic() - 1)
    assert runtime._take_kids_first_listen(session_id) is None  # expired ticket

    assert runtime._arm_kids_first_listen(session_id) is True
    runtime._kids_ends_at = time.monotonic() - 1  # session expired after arming
    assert runtime._take_kids_first_listen(session_id) is None
    runtime._kids_ends_at = time.monotonic() + 60

    # Any conversation Stop after Start invalidates the automatic turn, armed or not.
    assert runtime._arm_kids_first_listen(session_id) is True
    runtime.stop_speaking()
    assert runtime._take_kids_first_listen(session_id) is None
    assert runtime._arm_kids_first_listen(session_id) is False

    runtime.stop_event.set()
    assert runtime._arm_kids_first_listen(session_id) is False


# --- 3. Bounded empty/silent Kids turns ---------------------------------------------------------


def kids_pipeline_runtime(monkeypatch: pytest.MonkeyPatch, transcripts: list[str], endpoints: list[bool]):
    runtime = HermesVoiceRuntime(_Robot(), threading.Event())
    runtime._audio_ready = True
    runtime._power_mode = "awake"
    runtime._play_asset = lambda _name: None  # type: ignore[method-assign]
    runtime._discard_audio = lambda _seconds: None  # type: ignore[method-assign]
    runtime._notify_bridge_kids_session_async = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    runtime.start_kids_mode(profile_for("quiz", "nl"), greet=False)
    config = runtime._kids_voice_config(AppConfig(api_key="test-key"))
    calls: list[str] = []

    class Client:
        last_stt_provider = "local"

        def __init__(self, client_config: AppConfig) -> None:
            self.config = client_config

        def health(self) -> dict[str, str]:
            return {"status": "ok"}

        def transcribe(self, _audio: bytes) -> str:
            calls.append("transcribe")
            return transcripts.pop(0) if transcripts else ""

        def chat(self, text: str) -> str:
            calls.append(f"chat:{text}")
            return "Goed zo!"

        def close(self) -> None:
            pass

    def record(_config: AppConfig) -> EndpointResult:
        calls.append("record")
        speech = endpoints.pop(0) if endpoints else False
        samples = np.ones(160, dtype=np.float32) if speech else np.empty(0, dtype=np.float32)
        return EndpointResult(samples, speech, "end_silence" if speech else "initial_timeout", 0.01)

    monkeypatch.setattr("homebody.runtime.HermesBridgeClient", Client)
    runtime._record_utterance = record  # type: ignore[method-assign]
    runtime._play_kids_stream = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
    runtime._accept_wake_turn()
    return runtime, config, calls


def test_empty_kids_transcripts_retry_then_answer_without_a_new_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, config, calls = kids_pipeline_runtime(monkeypatch, ["", "", "een stoel"], [True, True, True])
    runtime._run_conversation(config)
    assert calls[:6] == ["record", "transcribe", "record", "transcribe", "record", "transcribe"]
    assert calls[6] == "chat:een stoel"
    # Misses reset after the answered turn; then three silent listens end the session's mic.
    assert calls[7:] == ["record", "record", "record"]
    assert runtime._status.detail == "No speech detected"


def test_empty_kids_turn_retries_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, config, calls = kids_pipeline_runtime(monkeypatch, [], [True, True, True, True])
    runtime._run_conversation(config)
    assert calls == ["record", "transcribe"] * 3
    assert not any(call.startswith("chat") for call in calls)


def test_end_during_empty_turn_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, config, calls = kids_pipeline_runtime(monkeypatch, [], [True, True])
    original_record = runtime._record_utterance

    def record_then_end(record_config: AppConfig) -> EndpointResult:
        result = original_record(record_config)
        runtime.stop_kids_mode(fold=False)
        return result

    runtime._record_utterance = record_then_end  # type: ignore[method-assign]
    runtime._run_conversation(config)
    assert calls == ["record", "transcribe"]


def test_adult_empty_transcript_still_returns_to_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, _config, calls = kids_pipeline_runtime(monkeypatch, [], [True, True])
    runtime.stop_kids_mode(fold=False)
    runtime._accept_wake_turn()
    runtime._run_conversation(AppConfig(api_key="test-key", continuous_conversation=True))
    assert calls == ["record", "transcribe"]


# --- 4. Local STT latency fix --------------------------------------------------------------------


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def transcribe(self, path: str, **kwargs: object):
        self.calls.append({"path": path, **kwargs})
        return iter(
            [
                SimpleNamespace(text="De gele bal.", no_speech_prob=0.1, avg_logprob=-0.2),
                SimpleNamespace(text="hallucinated", no_speech_prob=0.95, avg_logprob=-2.0),
            ]
        ), None


def test_kids_language_hint_skips_detection_and_other_inputs_keep_autodetect(tmp_path: Path) -> None:
    model = _FakeModel()
    assert local_stt_command.transcribe(model, str(tmp_path / "homebody-stt-nl-abc.wav")) == "De gele bal."
    assert local_stt_command.transcribe(model, str(tmp_path / "homebody-stt-abc.wav")) == "De gele bal."
    assert local_stt_command.transcribe(model, str(tmp_path / "homebody-stt-sv-abc.wav")) == "De gele bal."
    for language in ("de", "es", "fr", "it", "pt"):
        hinted = str(tmp_path / f"homebody-stt-{language}-abc.wav")
        assert local_stt_command.transcribe(model, hinted) == "De gele bal."
    assert [call["language"] for call in model.calls] == ["nl", None, None, "de", "es", "fr", "it", "pt"]
    for call in model.calls:
        assert call["vad_filter"] is True and call["condition_on_previous_text"] is False


def test_worker_reuses_one_model_and_exits_when_idle(tmp_path: Path) -> None:
    loads: list[_FakeModel] = []

    def factory() -> _FakeModel:
        loads.append(_FakeModel())
        return loads[-1]

    short = tempfile.TemporaryDirectory(prefix="hbs", dir="/tmp")  # AF_UNIX paths are length-limited
    socket_path = Path(short.name) / "w" / "worker.sock"
    audio = tmp_path / "homebody-stt-en-x.wav"
    audio.write_bytes(b"synthetic")
    served: list[int] = []
    server = threading.Thread(
        target=lambda: served.append(local_stt_command.serve(socket_path, idle_seconds=0.5, model_factory=factory)),
    )
    server.start()
    assert wait_for(socket_path.exists)
    assert local_stt_command.request(socket_path, audio) == "De gele bal."
    assert local_stt_command.request(socket_path, audio) == "De gele bal."
    with pytest.raises(RuntimeError):
        local_stt_command.request(socket_path, tmp_path / "missing.wav")
    server.join(timeout=5)
    assert not server.is_alive()  # bounded: exits after the idle window
    assert served == [3]
    assert len(loads) == 1 and len(loads[0].calls) == 2
    assert (socket_path.parent.stat().st_mode & 0o077) == 0
    assert not socket_path.exists()
    assert local_stt_command.worker_running(socket_path) == 0
    short.cleanup()


def test_command_falls_back_in_process_when_worker_cannot_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _FakeModel()
    monkeypatch.setattr(local_stt_command, "load_model", lambda: model)

    def broken_spawn(*_args: object) -> object:
        raise OSError("no worker")

    monkeypatch.setattr(local_stt_command, "_spawn_worker", broken_spawn)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "local_stt_command",
            "--input",
            str(tmp_path / "homebody-stt-en-x.wav"),
            "--output-dir",
            str(tmp_path),
            "--socket",
            str(tmp_path / "s" / "worker.sock"),
        ],
    )
    local_stt_command.main()
    assert (tmp_path / "transcript.txt").read_text() == "De gele bal."
    assert model.calls[0]["language"] == "en"


def test_runtime_marks_only_kids_transcriptions() -> None:
    captured: list[bytes] = []

    def handler(request):
        import httpx

        captured.append(request.content)
        return httpx.Response(200, json={"text": "ok", "provider": "local"})

    import httpx

    for kids in (True, False):
        client = HermesBridgeClient(
            AppConfig(
                api_key="k",
                kids_mode_enabled=kids,
                kids_age_band="7-9",
                kids_activity="quiz",
                language="nl",
            )
        )
        client._client = httpx.Client(transport=httpx.MockTransport(handler))
        assert client.transcribe(b"RIFF") == "ok"
        client.close()
    assert b'name="kids"' in captured[0]
    assert b'name="kids"' not in captured[1]


@pytest.mark.parametrize(("kids", "expected_prefix"), [("1", "homebody-stt-nl-"), ("", "homebody-stt-")])
def test_bridge_names_only_kids_uploads_with_the_profile_language(
    monkeypatch: pytest.MonkeyPatch,
    kids: str,
    expected_prefix: str,
) -> None:
    bridge_module = load_bridge_module()
    bridge = bridge_module.Bridge(api_key="secret", hermes_url="http://127.0.0.1:8642", profile=None)
    bridge._require_hermes_speech = lambda: None  # type: ignore[method-assign]
    monkeypatch.setattr(bridge_module, "_ensure_hermes_imports", lambda: None)
    seen: list[str] = []

    def transcribe_audio(path: str) -> dict[str, object]:
        seen.append(Path(path).name)
        assert Path(path).is_file()
        return {"success": True, "transcript": "de stoel", "provider": "local_command"}

    tools = SimpleNamespace(transcribe_audio=transcribe_audio)
    monkeypatch.setitem(sys.modules, "tools", SimpleNamespace(transcription_tools=tools))
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", tools)

    async def scenario() -> dict[str, object]:
        app = web.Application()
        app.router.add_post("/v1/audio/transcriptions", bridge.transcribe)
        async with TestClient(TestServer(app)) as client:
            form = FormData()
            form.add_field("file", b"RIFFsynthetic", filename="utterance.wav", content_type="audio/wav")
            form.add_field("language", "nl")  # after the file: order must not matter
            if kids:
                form.add_field("kids", kids)
            response = await client.post(
                "/v1/audio/transcriptions",
                data=form,
                headers={"Authorization": "Bearer secret"},
            )
            assert response.status == 200
            return await response.json()

    payload = asyncio.run(scenario())
    assert payload["text"] == "de stoel"
    assert seen[0].startswith(expected_prefix)
    if not kids:
        assert not seen[0].startswith("homebody-stt-nl-")


# --- 5. Finite I Spy rounds with explicit give-up and hint handling ---------------------------


TARGET = {
    "object_name": "chair",
    "colour": "blue",
    "category": "furniture",
    "hints_en": ["You can sit on it", "It has legs"],
    "hints_nl": ["Je kunt erop zitten", "Het heeft poten"],
}


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("I don't know", "give_up"),
        ("um, I dont know.", "give_up"),
        ("Ik weet het niet", "give_up"),
        ("weet ik niet", "give_up"),
        ("Geen idee!", "give_up"),
        ("I give up", "give_up"),
        ("Ik geef het op", "give_up"),
        ("hint please", "hint"),
        ("Mag ik een hint?", "hint"),
        ("Nog een tip", "hint"),
        ("help", "hint"),
        ("a chair", "answer"),
        ("Is it a chair? I don't know", "answer"),
        ("No", "answer"),
        ("Nee", "answer"),
        ("It is not a ball", "answer"),
    ],
)
def test_ispy_intent_is_explicit_and_bilingual(text: str, intent: str) -> None:
    assert load_bridge_module()._ispy_intent(text) == intent


def test_ispy_reply_caps_guesses_at_three_with_non_repeating_hints() -> None:
    bridge = load_bridge_module()
    answer, count, hints, complete = bridge._ispy_reply(TARGET, language="en", matched=False, previous_count=0)
    assert answer == "Nice guess, but not yet. New hint: You can sit on it. You have 2 guesses left."
    assert (count, hints, complete) == (1, 1, False)
    answer, count, hints, complete = bridge._ispy_reply(
        TARGET,
        language="en",
        matched=False,
        previous_count=count,
        hints_given=hints,
    )
    assert answer == "Nice guess, but not yet. New hint: It has legs. You have one guess left."
    assert (count, hints, complete) == (2, 2, False)
    answer, count, hints, complete = bridge._ispy_reply(
        TARGET,
        language="nl",
        matched=False,
        previous_count=count,
        hints_given=hints,
    )
    assert answer == "Goed geprobeerd! Het was de chair."
    assert (count, complete) == (3, True)


def test_ispy_hint_request_does_not_spend_a_guess_and_exhaustion_reveals() -> None:
    bridge = load_bridge_module()
    seen: list[str] = []
    hints = 0
    for _ in range(3):
        answer, count, hints, complete = bridge._ispy_reply(
            TARGET,
            language="nl",
            matched=False,
            previous_count=0,
            intent="hint",
            hints_given=hints,
        )
        assert count == 0 and complete is False
        seen.append(answer)
    assert len(set(seen)) == 3  # approved hints, then the colour reminder; never repeated
    assert seen[2] == "Hier is een hint: Zoek iets dat blauw is. Je mag nog 3 keer raden."
    answer, _count, _hints, complete = bridge._ispy_reply(
        TARGET,
        language="nl",
        matched=False,
        previous_count=0,
        intent="hint",
        hints_given=hints,
    )
    assert complete is True and answer == "Dat waren al mijn hints. Het was de chair."


def test_ispy_give_up_reveals_immediately_in_both_languages() -> None:
    bridge = load_bridge_module()
    assert bridge._ispy_reply(TARGET, language="nl", matched=False, previous_count=1, intent="give_up") == (
        "Dat geeft niet! Het was de chair.",
        1,
        0,
        True,
    )
    assert bridge._ispy_reply(TARGET, language="en", matched=False, previous_count=0, intent="give_up")[0] == (
        "That's okay! It was the chair."
    )


class IspyBridge:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, language: str = "nl", *, flag: str = "") -> None:
        self.module = load_bridge_module()
        self.bridge = self.module.Bridge(api_key="bridge-secret", hermes_url="http://127.0.0.1:8642", profile=None)
        self.bridge.http = object()
        monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
        self.judged: list[str] = []
        self.robot_guesses = ["lamp", "table", "book", "cup"]

        async def moderation(text: str, _key: str) -> bool:
            return bool(flag) and flag in text

        async def judge(guess: str, _target: dict[str, object], **_kwargs) -> bool:
            self.judged.append(guess)
            return guess == "stoel"

        async def robot_guess(_clues: list[str], previous: list[str], **_kwargs) -> str:
            return self.robot_guesses[len(previous)]

        monkeypatch.setattr(self.bridge, "_moderation_flagged", moderation)
        monkeypatch.setattr(self.bridge, "_judge_ispy_guess", judge)
        monkeypatch.setattr(self.bridge, "_guess_player_ispy_object", robot_guess)
        self.session_id = "kids-" + "a" * 32
        self.profile = {"age_band": "7-9", "activity": "ispy", "language": language}
        self.bridge._kids_sessions[self.session_id] = {
            "profile": ("7-9", "ispy", language),
            "history": [],
            "updated_at": time.monotonic(),
            "ispy_target": dict(TARGET),
            "ispy_role": "reachy_picker",
            "ispy_guess_count": 0,
            "ispy_hints_given": 0,
        }

    def say(self, text: str) -> dict[str, object]:
        outer = self

        class Request:
            headers = {"Authorization": "Bearer bridge-secret"}

            async def json(self) -> dict[str, object]:
                return {"input": text, "session_id": outer.session_id, "profile": outer.profile}

        payload = json.loads(asyncio.run(self.bridge.kids_chat(Request())).text)
        # Every spoken answer still carries exact-text, single-use approvals.
        self.bridge._consume_kids_speech_approval(payload["speech_approval"], self.session_id, payload["text"])
        with pytest.raises(web.HTTPException):
            self.bridge._consume_kids_speech_approval(payload["speech_approval"], self.session_id, payload["text"])
        return payload

    @property
    def state(self) -> dict[str, object]:
        return self.bridge._kids_sessions[self.session_id]


def test_child_guessing_round_ends_after_three_wrong_guesses(monkeypatch: pytest.MonkeyPatch) -> None:
    game = IspyBridge(monkeypatch)
    first = game.say("een bal")
    second = game.say("een tafel")
    assert "Je kunt erop zitten" in first["text"] and "Het heeft poten" in second["text"]
    third = game.say("een kast")
    assert third["text"].startswith("Goed geprobeerd! Het was de chair.")
    assert "Nu ben jij aan de beurt" in third["text"]
    assert third["ispy_role"] == "player_picker" and third["ispy_next_action"] == "prepare_player_view"
    assert third["ispy_phase"] == "awaiting_view" and third["ispy_view_token"] == game.state["ispy_view_token"]
    assert "ispy_target" not in game.state
    assert game.judged == ["een bal", "een tafel", "een kast"]


def test_child_give_up_and_hint_are_not_judged_as_guesses(monkeypatch: pytest.MonkeyPatch) -> None:
    game = IspyBridge(monkeypatch)
    hint = game.say("Mag ik een hint?")
    assert hint["text"].startswith("Hier is een hint: Je kunt erop zitten.")
    assert game.state["ispy_guess_count"] == 0
    give_up = game.say("Ik weet het niet")
    assert give_up["text"].startswith("Dat geeft niet! Het was de chair.")
    assert give_up["ispy_role"] == "player_picker" and give_up["ispy_next_action"] == "prepare_player_view"
    assert give_up["ispy_phase"] == "awaiting_view"
    assert game.judged == []


def test_robot_guessing_role_is_capped_and_give_up_never_starts_camera(monkeypatch: pytest.MonkeyPatch) -> None:
    game = IspyBridge(monkeypatch, language="en")
    game.state.pop("ispy_target")
    game.state.update(
        {
            "ispy_role": "player_picker",
            "ispy_player_phase": "awaiting_clue",
            "ispy_player_clues": [],
            "ispy_player_guesses": [],
        }
    )
    for clue in ("It is round", "It is soft", "It is small"):
        assert game.say(clue)["text"].startswith("Is it a ")
        reply = game.say("No")
    assert reply["text"] == "You beat me! What object did you choose?"
    assert reply["ispy_phase"] == "awaiting_reveal"
    give_up = game.say("I don't know")
    assert give_up["ispy_role"] == "reachy_pending" and give_up["ispy_next_action"] == ""
    still_no_camera = game.say("help")
    assert still_no_camera["ispy_next_action"] == ""
    play_again = game.say("play again")
    assert play_again["ispy_next_action"] == "prepare_robot_round"


def test_robot_guessing_dont_know_is_not_a_generic_no(monkeypatch: pytest.MonkeyPatch) -> None:
    game = IspyBridge(monkeypatch)
    game.state.pop("ispy_target")
    game.state.update(
        {
            "ispy_role": "player_picker",
            "ispy_player_phase": "awaiting_clue",
            "ispy_player_clues": [],
            "ispy_player_guesses": [],
        }
    )
    hint = game.say("hint")
    assert hint["text"].startswith("Vertel me iets over jouw voorwerp")
    assert game.state["ispy_player_guesses"] == []
    assert game.say("Het is rond")["text"] == "Is het een lamp?"
    answer = game.say("Ik weet het niet")
    assert answer["ispy_role"] == "reachy_pending" and answer["ispy_next_action"] == ""
    assert answer["text"].startswith("Dat geeft niet")


def test_flagged_give_up_input_still_returns_the_localized_safety_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    game = IspyBridge(monkeypatch, flag="weet")
    reply = game.say("Ik weet het niet")
    assert reply["text"].startswith("Daar kan ik niet mee helpen.")
    assert reply["ispy_next_action"] == ""
    assert game.state["ispy_guess_count"] == 0 and "ispy_target" in game.state


def test_flagged_output_is_replaced_and_cannot_start_camera(monkeypatch: pytest.MonkeyPatch) -> None:
    game = IspyBridge(monkeypatch, language="en", flag="look around")
    game.state.pop("ispy_target")
    game.state.update({"ispy_role": "reachy_pending", "ispy_player_phase": "complete"})
    reply = game.say("play again")
    assert reply["text"].startswith("I can't help with that.")
    assert reply["ispy_next_action"] == ""
