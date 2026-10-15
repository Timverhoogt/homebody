"""Robot speech-recognition routing through the real Pipeline/Kids loop and contextual offers.

The on-robot worker is replaced by an explicit fake client (no subprocess here; the protocol is
covered by test_asr_worker.py). A fake success is not device proof. No network or hardware.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from test_announcements import ready_runtime
from test_kids_stt_acknowledgements import Harness

from homebody import stt_routing, voice_pipeline
from homebody.announcements import Announcement
from homebody.config import AppConfig
from homebody.contextual_offers import ContextualOffer
from homebody.robot_stt import RobotReceipt, RobotSttCancelled, RobotSttUnavailable


class FakeRobotStt:
    def __init__(self, *, texts=("een stoel",), error: str | None = None, on_call=None, hang=False):
        self.texts = list(texts)
        self.error = error
        self.on_call = on_call
        self.hang = hang
        self.calls: list[dict] = []

    def transcribe(self, samples, *, session, generation, language, cancelled):
        self.calls.append({"session": session, "generation": generation, "language": language,
                           "samples": int(np.asarray(samples).size)})
        if self.on_call is not None:
            self.on_call()
        if self.hang:
            while not cancelled():
                threading.Event().wait(0.01)
            raise RobotSttCancelled("cancelled")
        if self.error:
            raise RobotSttUnavailable(self.error)
        return RobotReceipt(self.texts.pop(0) if self.texts else "", "gpu", 12)

    def close(self):
        pass


def robot_harness(monkeypatch, tmp_path, *, fallback="none", language="nl", robot=None, receipt="local",
                  health_locality="local", **kwargs):
    harness = Harness(monkeypatch, tmp_path, language=language, stt_seconds=0.0, catalog=False, **kwargs)
    harness.config = replace(harness.config, stt_provider="robot", stt_robot_fallback=fallback)
    harness.robot = robot or FakeRobotStt()
    harness.runtime._robot_stt = harness.robot
    harness.host_calls = []
    harness.statuses = []
    harness.localities = []
    client_class = harness.runtime._new_bridge_client

    class Client(client_class):  # type: ignore[misc, valid-type]
        last_stt_receipt = ""

        def health(self):
            return {**super().health(), "stt_locality": health_locality}

        def transcribe(self, audio, *, provider=None):
            harness.host_calls.append(provider)
            self.last_stt_receipt = receipt
            return "host words"

    harness.runtime._new_bridge_client = Client
    original = harness.runtime._set_status

    def record_status(state, detail="", **updates):
        original(state, detail, **updates)
        status = harness.runtime._status
        harness.statuses.append((state, detail, status.stt_processing_location, status.stt_processing_verified))
        harness.localities.append((state, status.stt_locality, status.stt_provider))

    harness.runtime._set_status = record_status
    return harness


def chats(harness) -> int:
    return sum(1 for label, _ in harness.calls if label == "chat")


def records(harness) -> int:
    return sum(1 for label, _ in harness.calls if label == "record")


def test_robot_success_never_calls_host_and_shows_reachy(monkeypatch, tmp_path):
    harness = robot_harness(monkeypatch, tmp_path, language="nl")
    harness.run()
    assert harness.host_calls == []
    assert chats(harness) == 1
    call = harness.robot.calls[0]
    assert call["language"] == "nl" and call["session"] == harness.config.kids_session_id
    assert ("transcribing", "Transcribing on Reachy", "robot", False) in harness.statuses
    assert any(loc == "robot" and verified for _s, _d, loc, verified in harness.statuses)
    status = harness.runtime._status
    assert (status.stt_processing_location, status.stt_processing_verified) == ("unknown", False)  # reset at end
    # The host's own health says "local", but a pending robot turn never borrows that claim.
    assert all(loc == "unknown" for state, loc, _p in harness.localities if state == "transcribing")
    assert any(state == "thinking" and provider == "robot" for state, _l, provider in harness.localities)
    marks = harness.runtime._voice_timings.snapshot()[0]["marks"]
    assert {"endpoint_final", "stt_submitted", "stt_final", "local_speech_end"} <= set(marks)
    assert marks["endpoint_final"] <= marks["stt_submitted"] <= marks["stt_final"]


def test_english_profile_language_is_passed_per_turn(monkeypatch, tmp_path):
    harness = robot_harness(monkeypatch, tmp_path, language="en", robot=FakeRobotStt(texts=("a chair",)))
    harness.run()
    assert harness.robot.calls[0]["language"] == "en"


def test_adult_turns_autodetect_language():
    assert stt_routing.robot_language(AppConfig(stt_provider="robot", language="nl")) == "auto"
    assert stt_routing.robot_language(SimpleNamespace(kids_mode_enabled=True, language="nl")) == "nl"
    assert stt_routing.robot_language(SimpleNamespace(kids_mode_enabled=True, language="sv")) == "auto"
    for language in ("es", "de", "fr", "it", "pt"):
        assert stt_routing.robot_language(SimpleNamespace(kids_mode_enabled=True, language=language)) == language
        assert stt_routing.robot_language(AppConfig(stt_provider="robot", language=language)) == "auto"


def test_infrastructure_failure_is_not_silence_and_does_not_retry(monkeypatch, tmp_path):
    harness = robot_harness(monkeypatch, tmp_path, robot=FakeRobotStt(error="unprovisioned"))
    errors = []
    harness.runtime._signal_error = lambda: errors.append(1)
    harness.run()
    assert harness.host_calls == [] and chats(harness) == 0
    assert records(harness) == 1  # no no-speech retry loop, no reload
    assert errors == [1]
    # Not provisioned: an owner setup notice, never a fake "ready" state.
    assert harness.runtime._status.last_error == "On-device speech recognition is not set up on Reachy yet"
    assert len(harness.robot.calls) == 1


def test_runtime_failure_notice_differs_from_setup_notice(monkeypatch, tmp_path):
    harness = robot_harness(monkeypatch, tmp_path, robot=FakeRobotStt(error="decode_timeout"))
    harness.runtime._signal_error = lambda: None
    harness.run()
    assert harness.runtime._status.last_error == "Speech recognition on Reachy is unavailable"
    assert chats(harness) == 0 and records(harness) == 1


def test_robot_silence_keeps_kids_empty_retry(monkeypatch, tmp_path):
    harness = robot_harness(monkeypatch, tmp_path, robot=FakeRobotStt(texts=("", "een stoel")),
                            speech=[True, True])
    harness.run()
    # The empty robot final is ordinary silence: the bounded Kids re-listen runs, then one answer.
    assert len(harness.robot.calls) == 2 and chats(harness) == 1 and harness.host_calls == []


def test_host_fallback_once_requests_only_local(monkeypatch, tmp_path):
    harness = robot_harness(monkeypatch, tmp_path, fallback="host", robot=FakeRobotStt(error="load_timeout"))
    harness.run()
    assert harness.host_calls == ["local"]
    assert chats(harness) == 1
    assert ("transcribing", "Transcribing on the Hermes host", "host", False) in harness.statuses
    assert any(loc == "host" and verified for _s, _d, loc, verified in harness.statuses)
    assert len(harness.robot.calls) == 1


def test_host_fallback_with_cloud_receipt_is_discarded(monkeypatch, tmp_path):
    harness = robot_harness(monkeypatch, tmp_path, fallback="host", receipt="openai",
                            robot=FakeRobotStt(error="decode_timeout"))
    harness.run()
    assert harness.host_calls == ["local"] and chats(harness) == 0


def test_host_fallback_refused_when_host_stt_not_local(monkeypatch, tmp_path):
    harness = robot_harness(monkeypatch, tmp_path, fallback="host", health_locality="cloud",
                            robot=FakeRobotStt(error="decode_timeout"))
    harness.run()
    assert harness.host_calls == [] and chats(harness) == 0  # no audio sent anywhere


def test_stop_during_robot_decode_cancels_without_fallback(monkeypatch, tmp_path):
    harness = robot_harness(monkeypatch, tmp_path, fallback="host")
    harness.robot.hang = True
    harness.robot.on_call = lambda: threading.Timer(0.1, harness.runtime._request_conversation_stop).start()
    harness.run()
    assert harness.host_calls == [] and chats(harness) == 0


def test_late_robot_final_after_stop_is_rejected(monkeypatch, tmp_path):
    harness = robot_harness(monkeypatch, tmp_path)
    harness.robot.on_call = harness.runtime._request_conversation_stop
    harness.run()
    assert len(harness.robot.calls) == 1 and chats(harness) == 0 and harness.host_calls == []


def test_session_switch_during_decode_discards_final(monkeypatch, tmp_path):
    harness = robot_harness(monkeypatch, tmp_path)
    harness.robot.on_call = lambda: harness.runtime.stop_kids_mode(fold=False)
    harness.run()
    assert chats(harness) == 0


def test_robot_route_never_borrows_host_health_locality():
    config = AppConfig(stt_provider="robot")
    assert voice_pipeline.stt_locality(config, {"stt_locality": "local"}) == "unknown"
    assert voice_pipeline.stt_locality(AppConfig(), {"stt_locality": "local"}) == "local"
    assert voice_pipeline.receipt_locality("local_command") == "local"


def test_settings_default_unchanged_and_fallback_validated():
    config = AppConfig()
    assert config.stt_provider == "configured" and config.stt_robot_fallback == "none"
    assert AppConfig(stt_provider="robot", stt_robot_fallback="host").stt_robot_fallback == "host"
    with pytest.raises(ValueError, match="robot STT fallback"):
        AppConfig(stt_robot_fallback="cloud")


def test_child_locked_status_exposes_only_categorical_location(monkeypatch, tmp_path):
    harness = robot_harness(monkeypatch, tmp_path)
    runtime = harness.runtime
    with runtime._kids_lock:
        runtime._kids_locked = True
    runtime._status.stt_processing_location = "robot"
    runtime._status.stt_processing_verified = True
    runtime._status.transcript = "secret words"
    locked = runtime.status()
    assert locked["stt_processing_location"] == "robot" and locked["stt_processing_verified"] is True
    assert "transcript" not in locked and "stt_provider" not in locked
    runtime._status.stt_processing_location = "/opt/model.gguf"
    assert runtime.status()["stt_processing_location"] == "unknown"


def offer_runtime():
    runtime = ready_runtime()
    runtime._power_mode = "awake"
    runtime._motors_enabled = True
    runtime._control_ready.set()
    runtime._status.state = "waiting_for_wake_word"
    token = runtime._contextual_offers.queue(
        ContextualOffer(source="timer", topic="active_timer", confidence=0.95, fingerprint="timer-1",
                        text="Your timer is active; would you like the remaining time?",
                        accepted_text="There are five minutes remaining."),
        response_window_seconds=10,
    )
    item = Announcement("Your timer is active; would you like the remaining time?", contextual_offer_token=token)
    runtime._announcement_current = item
    runtime._play_asset = lambda _name: None
    runtime._discard_audio = lambda _seconds: None
    runtime._record_utterance = lambda _config, **_kwargs: type(
        "Endpoint", (), {"speech_detected": True, "samples": np.ones(160, dtype=np.float32)}
    )()
    responses: list = []
    runtime.respond_to_contextual_offer = lambda t, r: responses.append((t, r)) or {"ok": True}
    return runtime, item, token, responses


class HostMustNotBeCalled:
    def transcribe(self, *_args, **_kwargs):
        raise AssertionError("robot route must not call the host")

    def health(self):
        raise AssertionError("robot route must not call the host")


def test_contextual_offer_uses_robot_route(monkeypatch):
    runtime, item, token, responses = offer_runtime()
    runtime._robot_stt = FakeRobotStt(texts=("yes",))
    config = AppConfig(stt_provider="robot")
    runtime._capture_contextual_offer_response(item, HostMustNotBeCalled(), config)
    assert responses == [(token, "yes")]
    assert runtime._robot_stt.calls[0]["language"] == "auto"


def test_contextual_offer_robot_failure_cancels_without_host():
    runtime, item, token, responses = offer_runtime()
    runtime._robot_stt = FakeRobotStt(error="unprovisioned")
    runtime._capture_contextual_offer_response(item, HostMustNotBeCalled(), AppConfig(stt_provider="robot"))
    assert responses == [] and not runtime._contextual_offers.is_queued(token)
