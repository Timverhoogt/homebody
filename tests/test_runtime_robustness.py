from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from test_voice_session_lifecycle import make_runtime

from homebody import runtime as runtime_module
from homebody import voice_ha
from homebody.audio import EndpointResult
from homebody.config import AppConfig
from homebody.hermes_client import HermesBridgeClient, SpeechAudio


def test_realtime_agent_lease_is_released_when_session_setup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = make_runtime()
    runtime._publish_remote_agent_session = lambda: None  # type: ignore[method-assign]
    runtime._establish_remote_agent_session = lambda _context: None  # type: ignore[method-assign]
    runtime.set_capability_profile("agent", adult_ui_unlocked=True)

    class BrokenSession:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise OSError("bridge unreachable")

    monkeypatch.setattr("homebody.runtime.RealtimeBridgeSession", BrokenSession)

    with pytest.raises(OSError):
        runtime._run_realtime_conversation(AppConfig(conversation_mode="realtime"))

    assert runtime._agent_active_request_id == ""
    assert runtime._agent_activity[-1]["event"] == "request_failed"


def test_realtime_agent_request_reports_failure_when_the_session_loop_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = make_runtime()
    runtime._publish_remote_agent_session = lambda: None  # type: ignore[method-assign]
    runtime._establish_remote_agent_session = lambda _context: None  # type: ignore[method-assign]
    runtime.set_capability_profile("agent", adult_ui_unlocked=True)
    runtime._accept_wake_turn()

    class FailingSession:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

        def events(self) -> list[object]:
            raise RuntimeError("socket dropped")

        def send_audio(self, _samples: np.ndarray) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("homebody.runtime.RealtimeBridgeSession", FailingSession)
    runtime._read_16k_frame = lambda: None  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="socket dropped"):
        runtime._run_realtime_conversation(AppConfig(conversation_mode="realtime"))

    assert runtime._agent_active_request_id == ""
    assert runtime._agent_activity[-1]["event"] == "request_failed"


def test_failed_voice_turn_error_expires_after_grace_period() -> None:
    runtime = make_runtime()
    runtime._set_status("error", "bridge timeout", last_error="bridge timeout")
    runtime._turn_error = ("bridge timeout", time.monotonic() + 60.0)

    runtime._expire_turn_error()
    assert runtime.status()["last_error"] == "bridge timeout"

    runtime._turn_error = ("bridge timeout", time.monotonic() - 1.0)
    runtime._expire_turn_error()
    assert runtime.status()["last_error"] == ""


def test_turn_error_expiry_keeps_a_newer_unrelated_error() -> None:
    runtime = make_runtime()
    runtime._turn_error = ("bridge timeout", time.monotonic() - 1.0)
    runtime._set_status("power_transition_error", "wake failed", last_error="wake failed")

    runtime._expire_turn_error()

    assert runtime.status()["last_error"] == "wake failed"


def test_home_assistant_media_gives_up_when_the_voice_slot_stays_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = make_runtime()
    monkeypatch.setattr(voice_ha, "_HA_MEDIA_VOICE_WAIT_SECONDS", 0.05)
    played: list[str] = []
    provider = SimpleNamespace(validate_media_url=lambda url: url)
    runtime._home_assistant_provider = lambda: provider  # type: ignore[method-assign]
    runtime._play_home_assistant_media = lambda _provider, url: played.append(url)  # type: ignore[method-assign]

    with runtime._voice_activity_lock:
        runtime.queue_home_assistant_media("http://ha.local/tts.mp3", announcement=False)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and "busy" not in runtime.status()["last_error"]:
            time.sleep(0.01)

    assert played == []
    assert "stayed busy" in runtime.status()["last_error"]


def test_startup_failure_tears_down_started_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class Actions:
        busy = False
        pending_count = 0

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            events.append("actions_started")

        def close(self) -> None:
            events.append("actions_closed")

    def fail_model() -> None:
        raise RuntimeError("wake model download failed")

    monkeypatch.setattr(runtime_module, "ReachyRobotActions", Actions)
    monkeypatch.setattr(runtime_module, "ensure_kws_model", fail_model)
    runtime = make_runtime()
    runtime.robot.media.stop_recording = lambda: events.append("recording_stopped")  # type: ignore[attr-defined]
    runtime.robot.media.stop_playing = lambda: events.append("playing_stopped")  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="wake model download failed"):
        runtime.run()

    assert events[:2] == ["actions_started", "actions_closed"]
    assert runtime._runtime_started is False
    assert runtime.status()["state"] == "stopping"


def test_empty_transcript_is_a_no_speech_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = make_runtime()
    calls: list[str] = []

    class Client:
        last_stt_provider = "test-stt"

        def __init__(self, _config: AppConfig) -> None:
            pass

        def health(self) -> dict[str, str]:
            return {"status": "ok"}

        def transcribe(self, _audio: bytes) -> str:
            calls.append("transcribe")
            return ""

        def chat(self, _transcript: str) -> str:
            calls.append("chat")
            return "unused"

        def close(self) -> None:
            pass

    monkeypatch.setattr("homebody.runtime.HermesBridgeClient", Client)
    runtime._record_utterance = lambda config: EndpointResult(  # type: ignore[method-assign]
        np.ones(160, dtype=np.float32), True, "end_silence", 0.01
    )
    runtime.cancel_agent_work("session_changed")
    runtime._accept_wake_turn()

    runtime._run_conversation(AppConfig(continuous_conversation=False))

    assert calls == ["transcribe"]
    status = runtime.status()
    assert status["detail"] == "No speech detected"
    assert status["last_error"] == ""


def test_bridge_client_returns_empty_transcript_instead_of_raising() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "  "})

    client = HermesBridgeClient(
        AppConfig(bridge_url="http://bridge.test", api_key="secret"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.transcribe(b"RIFF") == ""


def test_stop_during_pipeline_playback_silences_the_response_file() -> None:
    runtime = make_runtime()
    played: list[str] = []
    runtime.robot.media.play_sound = played.append  # type: ignore[method-assign]
    runtime._audio_duration = lambda _path, fallback_text: 5.0  # type: ignore[method-assign]
    checks = iter([False, True])
    runtime._turn_stop_requested = lambda: next(checks, True)  # type: ignore[method-assign]

    runtime._play_response(SpeechAudio(b"audio", "audio/wav", ".wav", "test"), "Hello", barge_in=False)

    assert played[-1].endswith("silence.wav")
    assert len(played) == 2


def test_play_response_stop_check_runs_before_any_audio() -> None:
    runtime = make_runtime()
    played: list[str] = []
    runtime.robot.media.play_sound = played.append  # type: ignore[method-assign]
    stop = threading.Event()
    stop.set()
    runtime._turn_stop_requested = stop.is_set  # type: ignore[method-assign]

    assert runtime._play_response(SpeechAudio(b"audio", "audio/wav", ".wav", "test"), "Hi", barge_in=False) is False
    assert played == []
