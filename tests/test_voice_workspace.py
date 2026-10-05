"""RAM retention and actual voice hooks; no robot or provider requests."""

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from homebody.audio import EndpointResult
from homebody.config import AppConfig
from homebody.hermes_client import SpeechAudio
from homebody.runtime import HermesVoiceRuntime
from homebody.voice_workspace import VoiceWorkspace


def runtime_fixture():
    media = SimpleNamespace(audio=SimpleNamespace(clear_player=lambda: None))
    runtime = HermesVoiceRuntime(SimpleNamespace(media=media), threading.Event(), config_loader=AppConfig)
    runtime._power_mode = "awake"
    runtime._play_asset = lambda _name: None
    runtime._discard_audio = lambda _seconds: None
    runtime._publish_remote_agent_session = lambda: None
    runtime._read_16k_frame = lambda: None
    return runtime


def test_opt_in_bounds_redaction_and_late_callback():
    workspace = VoiceWorkspace()
    workspace.append(None, "user", "not retained")
    assert workspace.snapshot()["events"] == []
    workspace.start()
    lease = workspace.lease()
    for i in range(130):
        workspace.append(lease, "user", f"message {i}")
    assert len(workspace.snapshot()["events"]) == 120
    workspace.append(lease, "assistant", "api_key=privatevalue " + "a" * 4500)
    event = workspace.snapshot()["events"][-1]
    assert len(event["text"]) == 4000 and event["truncated"]
    assert "privatevalue" not in event["text"]
    assert "[redacted]" in event["text"]
    workspace.clear()
    workspace.start()
    workspace.append(lease, "assistant", "late result")
    assert workspace.snapshot()["events"] == []


def test_hard_expiry_and_deduplication():
    now = [0.0]
    workspace = VoiceWorkspace(clock=lambda: now[0])
    workspace.start()
    lease = workspace.lease()
    workspace.append(lease, "assistant", "reply", key="response-1")
    workspace.append(lease, "assistant", "reply", key="response-1")
    assert len(workspace.snapshot()["events"]) == 1
    now[0] = 3601
    assert workspace.snapshot()["events"] == []
    assert not workspace.snapshot()["enabled"]
    workspace.append(lease, "assistant", "expired")
    assert workspace.snapshot()["events"] == []


@pytest.mark.parametrize("reason", ["kids_mode", "privacy", "power_sleep", "power_meeting", "stopped"])
def test_safety_teardown_clears_and_new_wake_does_not(reason):
    runtime = runtime_fixture()
    runtime.workspace("start")
    lease = runtime._workspace_lease()
    runtime._workspace_event(lease, "user", "private adult question")
    runtime.cancel_agent_work("session_changed")
    assert runtime.workspace()["enabled"]
    assert len(runtime.workspace()["events"]) == 1
    runtime.cancel_agent_work(reason)
    runtime.workspace("start")
    runtime._workspace_event(lease, "assistant", "late result")
    assert runtime.workspace()["events"] == []


@pytest.mark.parametrize("boundary", ["kids_active", "kids_locked", "meeting", "sleep", "privacy"])
def test_capture_and_reads_fail_closed(boundary):
    runtime = runtime_fixture()
    runtime.workspace("start")
    lease = runtime._workspace_lease()
    if boundary.startswith("kids_"):
        setattr(runtime, "_" + boundary, True)
    elif boundary == "privacy":
        runtime._privacy_requested.set()
    else:
        runtime._power_mode = boundary
        runtime._meeting_until = float("inf")
    runtime._workspace_event(lease, "user", "must not survive")
    with pytest.raises(RuntimeError):
        runtime.workspace()
    assert runtime._voice_workspace.snapshot()["events"] == []


@pytest.mark.parametrize("clear_during_chat", [False, True])
def test_pipeline_hooks_record_real_accepted_messages(monkeypatch, clear_during_chat):
    runtime = runtime_fixture()
    runtime.workspace("start")
    runtime._record_utterance = lambda _config: EndpointResult(
        np.ones(160, dtype=np.float32), True, "end_silence", 0.01,
    )
    runtime._play_response = lambda _speech, _text, barge_in: False

    class Client:
        last_stt_provider = "fixture-stt"

        def __init__(self, config):
            self.config = config

        def health(self):
            return {"status": "ok"}

        def transcribe(self, _audio):
            return "Tell me about the roadmap"

        def chat(self, _transcript):
            if clear_during_chat:
                runtime.workspace("clear")
                runtime.workspace("start")
            return "This is the actual fixture reply, not a synthesized progress report."

        def synthesize(self, _text):
            return SpeechAudio(b"audio", "audio/wav", ".wav", "fixture-tts")

        def close(self):
            pass

    monkeypatch.setattr("homebody.runtime.HermesBridgeClient", Client)
    runtime._run_conversation(AppConfig(continuous_conversation=False))
    events = runtime.workspace()["events"]
    if clear_during_chat:
        assert events == []
    else:
        assert [(e["role"], e["text"]) for e in events if e["role"] != "activity"] == [
            ("user", "Tell me about the roadmap"),
            ("assistant", "This is the actual fixture reply, not a synthesized progress report."),
        ]
        assert events[-2]["text"] == "Voice turn finished"
        assert events[-1]["text"] == "Voice session ended"
        assert "workspace" not in runtime.status()  # Never added to public status.


def test_realtime_completed_transcripts_and_duplicates(monkeypatch):
    runtime = runtime_fixture()
    runtime.workspace("start")

    class Session:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def events(self):
            if getattr(self, "sent", False):
                runtime._conversation_stop_requested.set()
                return []
            self.sent = True
            return [
                SimpleNamespace(type="conversation.item.input_audio_transcription.delta",
                                payload={"delta": "partial"}),
                SimpleNamespace(type="conversation.item.input_audio_transcription.completed",
                                payload={"transcript": "Accepted question", "item_id": "user-1"}),
                SimpleNamespace(type="response.created", payload={"response": {"id": "reply-1"}}),
                SimpleNamespace(type="response.output_audio_transcript.delta", payload={"delta": "Actual reply"}),
                SimpleNamespace(type="response.output_audio_transcript.done",
                                payload={"transcript": "Actual reply", "response_id": "reply-1"}),
                SimpleNamespace(type="response.done", payload={"response": {"id": "reply-1", "status": "completed"}}),
            ]

        def close(self):
            pass

    monkeypatch.setattr("homebody.runtime.RealtimeBridgeSession", Session)
    runtime._run_realtime_conversation(AppConfig(conversation_mode="realtime"))
    runtime._conversation_stop_requested.clear()
    messages = [e for e in runtime.workspace()["events"] if e["role"] != "activity"]
    assert [(e["role"], e["text"]) for e in messages] == [
        ("user", "Accepted question"), ("assistant", "Actual reply"),
    ]
