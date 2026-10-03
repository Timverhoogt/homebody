from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from homebody.audio import EndpointResult
from homebody.config import AppConfig
from homebody.hermes_client import SpeechAudio
from homebody.runtime import HermesVoiceRuntime


class FakeMedia:
    def __init__(self) -> None:
        self.pushed_audio: list[np.ndarray] = []
        self.clear_calls = 0
        self.audio = SimpleNamespace(clear_player=self._clear_player)

    def play_sound(self, path: str) -> None:
        pass

    def push_audio_sample(self, samples: np.ndarray) -> None:
        self.pushed_audio.append(samples)

    def _clear_player(self) -> None:
        self.clear_calls += 1


class FakeRobot:
    def __init__(self) -> None:
        self.media = FakeMedia()


def make_runtime() -> HermesVoiceRuntime:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._audio_ready = True
    runtime._power_mode = "awake"
    runtime._play_asset = lambda name: None  # type: ignore[method-assign]
    runtime._discard_audio = lambda seconds: None  # type: ignore[method-assign]
    return runtime


def test_session_change_invalidates_agent_generation_without_requesting_voice_stop() -> None:
    runtime = make_runtime()
    generation = int(runtime.status()["agent"]["session_generation"])  # type: ignore[index]

    runtime._accept_wake_turn()
    runtime.cancel_agent_work("session_changed")

    assert runtime.agent_session_is_current(generation) is False
    assert not runtime._conversation_stop_requested.is_set()


@pytest.mark.parametrize(
    "reason",
    [
        "stopped",
        "profile_changed",
        "power_standby",
        "power_meeting",
        "power_sleep",
        "kids_mode",
        "privacy",
        "emergency_stop",
        "unknown_reason",
    ],
)
def test_safety_cancellations_still_request_voice_teardown(reason: str) -> None:
    runtime = make_runtime()

    runtime.cancel_agent_work(reason)

    assert runtime._conversation_stop_requested.is_set()


def test_session_change_cannot_erase_a_racing_safety_stop() -> None:
    runtime = make_runtime()

    runtime.cancel_agent_work("privacy")
    runtime.cancel_agent_work("session_changed")

    assert runtime._conversation_stop_requested.is_set()


def test_realtime_wake_session_accepts_audio_after_agent_generation_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = make_runtime()
    sessions: list[object] = []

    class Session:
        def __init__(
            self, config: AppConfig, *, agent_context: object, agent_request_id: str
        ) -> None:
            self.sent_audio = 0
            self.closed = False
            sessions.append(self)

        def start(self) -> None:
            pass

        def send_audio(self, samples: np.ndarray) -> None:
            self.sent_audio += 1

        def events(self) -> list[object]:
            runtime.stop_event.set()
            return [SimpleNamespace(type="response.done", payload={})]

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("homebody.runtime.RealtimeBridgeSession", Session)
    runtime._read_16k_frame = lambda: np.ones(160, dtype=np.float32)  # type: ignore[method-assign]

    runtime.cancel_agent_work("session_changed")
    runtime._run_realtime_conversation(AppConfig(conversation_mode="realtime"))

    assert len(sessions) == 1
    assert sessions[0].sent_audio == 1  # type: ignore[attr-defined]
    assert sessions[0].closed is True  # type: ignore[attr-defined]
    assert runtime.status()["turns_completed"] == 1


def test_pipeline_wake_session_completes_first_turn_after_agent_generation_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = make_runtime()
    calls: list[str] = []

    class Client:
        last_stt_provider = "test-stt"

        def __init__(self, config: AppConfig) -> None:
            self.config = config

        def health(self) -> dict[str, str]:
            calls.append("health")
            return {"status": "ok"}

        def transcribe(self, audio: bytes) -> str:
            calls.append("transcribe")
            return "hello"

        def chat(self, transcript: str) -> str:
            calls.append("chat")
            return "Hi there"

        def synthesize(self, text: str) -> SpeechAudio:
            calls.append("synthesize")
            return SpeechAudio(b"audio", "audio/wav", ".wav", "test-tts")

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr("homebody.runtime.HermesBridgeClient", Client)
    # This test exercises local generation invalidation and the first voice turn,
    # not the best-effort remote lease publisher's daemon thread.
    runtime._publish_remote_agent_session = lambda: None  # type: ignore[method-assign]
    runtime._record_utterance = lambda config: EndpointResult(  # type: ignore[method-assign]
        np.ones(160, dtype=np.float32), True, "end_silence", 0.01
    )
    runtime._play_response = lambda speech, text, barge_in: False  # type: ignore[method-assign]

    runtime.cancel_agent_work("session_changed")
    runtime._run_conversation(AppConfig(continuous_conversation=False))

    assert calls == ["health", "transcribe", "chat", "synthesize", "close"]
    assert runtime.status()["turns_completed"] == 1


def test_pipeline_no_speech_timeout_returns_to_wake_word_without_bridge_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = make_runtime()
    signalled: list[bool] = []

    class Client:
        def __init__(self, config: AppConfig) -> None:
            pass

        def health(self) -> dict[str, str]:
            return {"status": "ok"}

        def close(self) -> None:
            pass

    monkeypatch.setattr("homebody.runtime.HermesBridgeClient", Client)
    runtime._record_utterance = lambda config: EndpointResult(  # type: ignore[method-assign]
        np.empty(0, dtype=np.float32), False, "initial_timeout", 0.01
    )
    runtime._signal_error = lambda: signalled.append(True)  # type: ignore[method-assign]

    runtime.cancel_agent_work("session_changed")
    runtime._run_conversation(AppConfig(continuous_conversation=False))

    status = runtime.status()
    assert status["state"] == "waiting_for_wake_word"
    assert status["detail"] == "No speech detected"
    assert signalled == [True]


def test_realtime_follow_up_inactivity_closes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = make_runtime()
    sessions: list[object] = []

    class Clock:
        now = -0.6

        def monotonic(self) -> float:
            self.now += 0.6
            return self.now

    class Session:
        def __init__(
            self, config: AppConfig, *, agent_context: object, agent_request_id: str
        ) -> None:
            self.closed = False
            sessions.append(self)

        def start(self) -> None:
            pass

        def send_audio(self, samples: np.ndarray) -> None:
            pass

        def events(self) -> list[object]:
            return []

        def close(self) -> None:
            self.closed = True

    clock = Clock()
    monkeypatch.setattr("homebody.runtime.RealtimeBridgeSession", Session)
    monkeypatch.setattr("homebody.runtime.time.monotonic", clock.monotonic)
    runtime._read_16k_frame = lambda: None  # type: ignore[method-assign]

    runtime.cancel_agent_work("session_changed")
    runtime._run_realtime_conversation(
        AppConfig(conversation_mode="realtime", conversation_timeout_seconds=1.0)
    )

    assert len(sessions) == 1
    assert sessions[0].closed is True  # type: ignore[attr-defined]
    assert not runtime.stop_event.is_set()


def test_privacy_cancellation_closes_realtime_session_and_flushes_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = make_runtime()
    sessions: list[object] = []

    class Session:
        def __init__(
            self, config: AppConfig, *, agent_context: object, agent_request_id: str
        ) -> None:
            self.closed = False
            sessions.append(self)

        def start(self) -> None:
            pass

        def send_audio(self, samples: np.ndarray) -> None:
            pass

        def events(self) -> list[object]:
            runtime.cancel_agent_work("privacy")
            return []

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("homebody.runtime.RealtimeBridgeSession", Session)
    runtime._read_16k_frame = lambda: None  # type: ignore[method-assign]

    runtime.cancel_agent_work("session_changed")
    runtime._run_realtime_conversation(AppConfig(conversation_mode="realtime"))

    assert sessions[0].closed is True  # type: ignore[attr-defined]
    assert runtime.robot.media.clear_calls == 1  # type: ignore[attr-defined]


def test_realtime_agent_session_binds_request_id_and_privacy_cancels_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = make_runtime()
    runtime._publish_remote_agent_session = lambda: None  # type: ignore[method-assign]
    runtime._establish_remote_agent_session = lambda _context: None  # type: ignore[method-assign]
    runtime.set_capability_profile("agent", adult_ui_unlocked=True)
    runtime._accept_wake_turn()
    cancelled = threading.Event()
    captured: dict[str, str] = {}

    class Session:
        def __init__(
            self, config: AppConfig, *, agent_context: object, agent_request_id: str
        ) -> None:
            captured["request_id"] = agent_request_id

        def start(self) -> None:
            pass

        def send_audio(self, samples: np.ndarray) -> None:
            pass

        def events(self) -> list[object]:
            runtime.cancel_agent_work("privacy")
            return []

        def close(self) -> None:
            pass

    def cancel_remote(request_id: str) -> None:
        captured["cancelled"] = request_id
        cancelled.set()

    runtime._cancel_remote_agent_request = cancel_remote  # type: ignore[method-assign]
    runtime._read_16k_frame = lambda: None  # type: ignore[method-assign]
    monkeypatch.setattr("homebody.runtime.RealtimeBridgeSession", Session)

    runtime._run_realtime_conversation(AppConfig(conversation_mode="realtime"))

    assert captured["request_id"].startswith("agent-")
    assert cancelled.wait(timeout=1.0)
    assert captured["cancelled"] == captured["request_id"]
