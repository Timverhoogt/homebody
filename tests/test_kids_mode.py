from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from reachy_mini_hermes.config import AppConfig
from reachy_mini_hermes.kids_mode import (
    KidsProfile,
    build_kids_prompt,
    hash_parent_pin,
    kids_greeting,
    verify_parent_pin,
)
from reachy_mini_hermes.runtime import HermesVoiceRuntime

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "reachy_mini_hermes" / "static"


class FakeMedia:
    pass


class FakeRobot:
    def __init__(self) -> None:
        self.media = FakeMedia()


def test_kids_profile_is_bounded_and_prompt_has_core_safeguards() -> None:
    profile = KidsProfile(
        nickname="  Sam  ",
        age_band="4-6",
        activity="story",
        language="nl",
        duration_minutes=15,
        motion_enabled=False,
    )

    prompt = build_kids_prompt(profile)
    assert profile.nickname == "Sam"
    assert "Dutch" in prompt
    assert "aged 4-6" in prompt
    assert "Never ask for or repeat a full name" in prompt
    assert "Do not suggest" in prompt and "keeping secrets" in prompt
    assert "Camera access is disabled" in prompt
    assert "trusted grown-up" in prompt
    assert "Current activity:" in prompt
    assert kids_greeting(profile).startswith("Hi Sam.")


def test_parent_pin_uses_salted_scrypt_and_never_round_trips_plaintext() -> None:
    first = hash_parent_pin("482614")
    second = hash_parent_pin("482614")
    assert first.startswith("scrypt$")
    assert first != second
    assert "482614" not in first
    assert verify_parent_pin("482614", first) is True
    assert verify_parent_pin("482715", first) is False
    with pytest.raises(ValueError, match="6 to 8 digits"):
        hash_parent_pin("123")


def test_pin_verifier_is_removed_from_redacted_status() -> None:
    config = AppConfig(kids_parent_pin_hash=hash_parent_pin("482614"))
    redacted = config.redacted_dict()
    assert "kids_parent_pin_hash" not in redacted
    assert redacted["kids_parent_pin_configured"] is True


def test_locked_child_status_exposes_only_readiness_and_clears_private_runtime_text() -> None:
    config = AppConfig(
        bridge_url="http://private-bridge.example:8643",
        api_key="secret",
        system_prompt="private normal-mode prompt",
        instance_id="private-instance",
        kids_parent_pin_hash=hash_parent_pin("482614"),
    )
    assert config.child_status_dict() == {
        "configured": True,
        "api_key_configured": True,
        "kids_parent_pin_configured": True,
    }

    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._kids_locked = True
    with runtime._status_lock:
        runtime._status.detail = "private detail with bridge URL"
        runtime._status.transcript = "private child words"
        runtime._status.response_preview = "private answer"
        runtime._status.stt_provider = "private-stt"
        runtime._status.tts_provider = "private-tts"
        runtime._status.camera_last_error = "private camera error"
        runtime._status.last_robot_action = "private prior action"
        runtime._status.robot_action_last_error = "private robot error"
        runtime._status.announcement_current_preview = "Hi Sam"
        runtime._status.announcement_last_text = "Hi Sam"
        runtime._status.announcement_last_error = "private provider error"
        runtime._status.announcement_provider = "private-provider"
        runtime._status.last_error = "private bridge URL"
    status = runtime.status()
    assert set(status) == {
        "state",
        "power_mode",
        "motors_enabled",
        "head_safely_folded",
        "kids_mode",
    }
    serialized = str(status)
    for private_value in (
        "private detail",
        "private child words",
        "private answer",
        "private-stt",
        "private-tts",
        "private camera error",
        "private prior action",
        "private robot error",
        "Hi Sam",
        "private provider error",
        "private-provider",
        "private bridge URL",
    ):
        assert private_value not in serialized


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"age_band": "13-15"}, "age band"),
        ({"activity": "internet"}, "activity"),
        ({"language": "de"}, "language"),
        ({"duration_minutes": 10}, "duration"),
        ({"duration_minutes": 90}, "duration"),
        ({"nickname": "x" * 33}, "nickname"),
    ],
)
def test_kids_profile_rejects_unbounded_values(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=f"(?i){message}"):
        KidsProfile(**kwargs)


def test_runtime_kids_mode_forces_moderated_pipeline_and_removes_private_tools() -> None:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._audio_ready = True
    profile = KidsProfile(activity="quiz", duration_minutes=15, motion_enabled=True)
    config = AppConfig(
        conversation_mode="realtime",
        camera_enabled=True,
        camera_feed_enabled=True,
        face_tracking_enabled=True,
        doa_enabled=True,
    )

    status = runtime.start_kids_mode(profile, greet=False)
    effective = runtime._kids_voice_config(config)

    assert status["active"] is True
    assert status["locked"] is True
    public_profile = status["profile"]
    assert isinstance(public_profile, dict)
    assert "nickname" not in public_profile
    assert status["tool_policy"] == "voice-state-motion-only"
    assert effective.conversation_mode == "pipeline"
    assert effective.kids_mode_enabled is True
    assert effective.kids_session_id.startswith("kids-")
    session_token = effective.kids_session_id.removeprefix("kids-")
    assert len(session_token) == 32
    assert set(session_token) <= set("0123456789abcdef")
    assert effective.kids_age_band == "7-9"
    assert effective.kids_activity == "quiz"
    assert effective.camera_enabled is False
    assert effective.camera_feed_enabled is False
    assert effective.face_tracking_enabled is False
    assert effective.doa_enabled is False
    assert effective.agent_tools_enabled is False
    assert effective.power_tools_enabled is False
    assert effective.robot_tools_enabled is False
    assert effective.continuous_conversation is True
    assert effective.system_prompt != config.system_prompt
    assert runtime._kids_session_is_current(effective.kids_session_id) is True
    assert runtime._kids_session_is_current("kids-stale") is False
    current_generation = runtime._kids_generation
    runtime._expire_kids_mode(current_generation - 1)
    assert runtime.status()["kids_mode"]["active"] is True  # type: ignore[index]

    stopped = runtime.stop_kids_mode(fold=False)
    assert stopped["active"] is False
    assert stopped["locked"] is True
    assert stopped["last_end_reason"] == "parent"
    assert stopped["last_fold_succeeded"] is None
    unlocked = runtime.unlock_kids_controls()
    assert unlocked["locked"] is False


def test_runtime_generated_kids_session_id_passes_real_bridge_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._audio_ready = True
    runtime.start_kids_mode(KidsProfile(activity="quiz", duration_minutes=15), greet=False)
    effective = runtime._kids_voice_config(AppConfig())

    bridge_path = ROOT / "companion" / "hermes_reachy_bridge.py"
    spec = importlib.util.spec_from_file_location("runtime_session_bridge", bridge_path)
    assert spec and spec.loader
    bridge_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge_module)
    bridge = bridge_module.Bridge(api_key="secret", hermes_url="http://127.0.0.1:8642", profile=None)

    async def moderation_clear(_text: str, _key: str) -> bool:
        return False

    monkeypatch.setattr(bridge, "_moderation_flagged", moderation_clear)

    class Upstream:
        status = 200

        async def json(self, **_kwargs) -> dict[str, object]:
            return {"choices": [{"message": {"content": "Bridge accepted this session."}}]}

    class Context:
        async def __aenter__(self) -> Upstream:
            return Upstream()

        async def __aexit__(self, *_args) -> None:
            return None

    class Http:
        def post(self, *_args, **_kwargs) -> Context:
            return Context()

    class Request:
        headers = {"Authorization": "Bearer secret"}

        async def json(self) -> dict[str, object]:
            return {
                "input": "Hello",
                "session_id": effective.kids_session_id,
                "profile": {"age_band": "7-9", "activity": "quiz", "language": "en"},
            }

    bridge.http = Http()
    response = asyncio.run(bridge.kids_chat(Request()))
    payload = json.loads(response.text)
    assert payload["text"] == "Bridge accepted this session."
    assert payload["speech_approval"]
    assert payload["fallback_speech_approval"]
    runtime.stop_kids_mode(fold=False)


def test_kids_fold_failure_is_reported_without_false_success(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._audio_ready = True
    runtime.start_kids_mode(KidsProfile(duration_minutes=15), greet=False)

    def fail_fold(*_args, **_kwargs):
        raise RuntimeError("motor controller unavailable")

    monkeypatch.setattr(runtime, "set_power_mode", fail_fold)
    with pytest.raises(RuntimeError, match="motor controller unavailable"):
        runtime.stop_kids_mode(reason="time_limit", fold=True)
    status = runtime.status()["kids_mode"]
    assert status["active"] is False  # type: ignore[index]
    assert status["last_fold_succeeded"] is False  # type: ignore[index]
    assert "fold_failed" in status["last_end_reason"]  # type: ignore[operator,index]


def test_runtime_pushes_kids_pcm_chunks_directly_to_reachy_audio() -> None:
    pushed: list[np.ndarray] = []

    class StreamingMedia:
        def push_audio_sample(self, sample: np.ndarray) -> None:
            pushed.append(sample.copy())

    robot = SimpleNamespace(media=StreamingMedia())
    runtime = HermesVoiceRuntime(robot, threading.Event())
    runtime._output_sample_rate = 24000

    class StreamingClient:
        last_tts_provider = "elevenlabs-flash-stream"
        config = SimpleNamespace(kids_mode_enabled=False, kids_session_id="")

        @staticmethod
        def iter_kids_speech(_text: str, *, should_stop=None):
            assert should_stop is not None
            yield b"\x00"
            yield b"\x00\xff\x7f"

    interrupted = runtime._play_kids_stream(StreamingClient(), "Hello", barge_in=False)  # type: ignore[arg-type]

    assert interrupted is False
    assert pushed
    samples = np.concatenate(pushed)
    assert samples.shape == (2,)
    assert samples[0] == pytest.approx(0.0)
    assert samples[1] > 0.99


def test_kids_stream_stop_cancels_while_network_producer_is_active() -> None:
    import time

    class StreamingMedia:
        def push_audio_sample(self, _sample: np.ndarray) -> None:
            pass

        def clear_player(self) -> None:
            pass

    runtime = HermesVoiceRuntime(SimpleNamespace(media=StreamingMedia()), threading.Event())
    runtime._output_sample_rate = 24000

    class SlowClient:
        last_tts_provider = "elevenlabs-flash-stream"
        config = SimpleNamespace(kids_mode_enabled=False, kids_session_id="")

        @staticmethod
        def iter_kids_speech(_text: str, *, should_stop=None):
            assert should_stop is not None
            yield b"\x00\x00" * 1200
            while not should_stop():
                time.sleep(0.01)

    timer = threading.Timer(0.05, runtime._conversation_stop_requested.set)
    timer.start()
    started = time.monotonic()
    try:
        assert runtime._play_kids_stream(SlowClient(), "Hello", barge_in=False) is False  # type: ignore[arg-type]
    finally:
        timer.cancel()
    assert time.monotonic() - started < 1.0


def test_kids_stream_rejects_work_from_replaced_session() -> None:
    runtime = HermesVoiceRuntime(SimpleNamespace(media=SimpleNamespace()), threading.Event())
    with runtime._kids_lock:
        runtime._kids_active = True
        runtime._kids_session_id = "kids-current"

    class StaleClient:
        config = SimpleNamespace(kids_mode_enabled=True, kids_session_id="kids-old")
        last_tts_provider = ""

        @staticmethod
        def iter_kids_speech(_text: str, *, should_stop=None):
            raise AssertionError("stale session must be rejected before opening the stream")
            yield b""  # pragma: no cover

    assert runtime._play_kids_stream(StaleClient(), "Hello", barge_in=False) is False  # type: ignore[arg-type]


def test_kids_tab_has_activities_parent_controls_disclosures_and_end_button() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    main = (STATIC / "main.js").read_text(encoding="utf-8")
    style = (STATIC / "style.css").read_text(encoding="utf-8")
    backend = (ROOT / "reachy_mini_hermes" / "main.py").read_text(encoding="utf-8")

    assert 'data-tab="kids"' in html
    assert 'data-panel="kids"' in html
    for activity in ("buddy", "story", "quiz", "riddles", "calm"):
        assert f'data-kids-activity="{activity}"' in html
    assert 'id="kids-age-band"' in html
    assert 'id="kids-duration"' in html
    assert 'id="kids-motion-enabled"' in html
    assert 'id="kids-stop-button"' in html
    assert 'id="kids-parent-pin"' in html
    assert 'id="kids-pin-setup-button"' in html
    assert 'id="kids-parent-unlock-button"' in html
    assert "Adult supervision" in html
    assert "No private tools" in html
    assert 'fetch("/api/kids/start"' in main
    assert 'fetch("/api/kids/stop"' in main
    assert '"/api/kids/parent/setup"' in main
    assert '"/api/kids/parent/unlock"' in main
    assert "reachy-hermes-kids-profile" in main
    persisted_profile = main.split(
        'window.localStorage.setItem("reachy-hermes-kids-profile"',
        1,
    )[1].split(";", 1)[0]
    assert "parent_pin" not in persisted_profile
    assert "enabled: !kidsActive" in main
    assert ".kids-activities" in style
    assert 'post("/api/kids/start")' in backend
    assert 'post("/api/kids/stop")' in backend


def test_kids_static_assets_advance_pwa_cache_together() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    worker = (STATIC / "service-worker.js").read_text(encoding="utf-8")
    assert "reachy-hermes-shell-v22" in worker
    for asset in ("style.css", "camera.js", "main.js"):
        assert f"/static/{asset}?v=22" in html
        assert f'"/static/{asset}?v=22"' in worker
