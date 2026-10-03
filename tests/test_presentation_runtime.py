from __future__ import annotations

import io
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from homebody.config import AppConfig
from homebody.runtime import HermesVoiceRuntime


def jpeg(level: int) -> bytes:
    pixels = np.full((120, 160, 3), 30, dtype=np.uint8)
    pixels[30:90, 40:120] = level
    output = io.BytesIO()
    Image.fromarray(pixels, "RGB").save(output, format="JPEG", quality=90)
    return output.getvalue()


class Robot:
    media = SimpleNamespace()


def runtime(**overrides: object) -> HermesVoiceRuntime:
    config = AppConfig(
        camera_enabled=True,
        shared_physical_context_enabled=True,
        contextual_offers_enabled=True,
        initiative_policy_enabled=True,
        initiative_quiet_hours_enabled=False,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    config.validate()
    result = HermesVoiceRuntime(Robot(), threading.Event(), config_loader=lambda: config)
    result._power_mode = "awake"
    result._motors_enabled = True
    result._capability_profile = "agent"
    result._control_ready.set()
    result._audio_ready = True
    result._status.state = "waiting_for_wake_word"
    return result


def test_presentation_window_is_explicit_bounded_and_status_is_redacted(monkeypatch) -> None:
    target = runtime()
    monkeypatch.setattr(target, "_capture_camera_jpeg", lambda: jpeg(30))
    started: list[tuple[int, object, float]] = []
    monkeypatch.setattr(
        target,
        "_start_presentation_worker",
        lambda generation, gate, duration: started.append((generation, gate, duration)),
    )

    status = target.start_presentation_window()

    assert status["state"] == "watching"
    assert status["visible_indicator"] is True
    assert status["expires_seconds_remaining"] == 20
    assert len(started) == 1
    assert "jpeg" not in repr(status).lower()
    assert "image" not in status
    assert "baseline" not in status


def test_wake_word_microphone_capture_does_not_block_explicit_presentation(monkeypatch) -> None:
    target = runtime()
    target._recording = True
    monkeypatch.setattr(target, "_capture_camera_jpeg", lambda: jpeg(30))
    monkeypatch.setattr(target, "_start_presentation_worker", lambda *_args: None)

    assert target.start_presentation_window()["state"] == "watching"


def test_active_voice_mutex_still_blocks_presentation() -> None:
    target = runtime()
    target._voice_activity_lock.acquire()

    with pytest.raises(RuntimeError, match="voice activity"):
        target.start_presentation_window()


def test_repeated_presented_change_queues_one_bounded_offer(monkeypatch) -> None:
    target = runtime()
    monkeypatch.setattr(target, "_capture_camera_jpeg", lambda: jpeg(30))
    started: list[tuple[int, object, float]] = []
    monkeypatch.setattr(
        target,
        "_start_presentation_worker",
        lambda generation, gate, duration: started.append((generation, gate, duration)),
    )
    offers = []
    monkeypatch.setattr(
        target,
        "submit_contextual_offer",
        lambda offer: offers.append(offer) or {"ok": True, "queued": True, "token": 9},
    )
    target.start_presentation_window()
    generation = started[0][0]

    assert target._observe_presentation_frame(generation, jpeg(220)) is False
    assert target._observe_presentation_frame(generation, jpeg(220)) is False
    assert target._observe_presentation_frame(generation, jpeg(220)) is True
    assert len(offers) == 1
    assert offers[0].source == "presentation"
    assert offers[0].topic == "presented_context"
    assert "say Hey Hermes" in offers[0].accepted_text
    assert target._observe_presentation_frame(generation, jpeg(220)) is False
    assert len(offers) == 1


def test_stop_invalidates_stale_frames_and_clears_ephemeral_gate(monkeypatch) -> None:
    target = runtime()
    monkeypatch.setattr(target, "_capture_camera_jpeg", lambda: jpeg(30))
    started: list[tuple[int, object, float]] = []
    monkeypatch.setattr(
        target,
        "_start_presentation_worker",
        lambda generation, gate, duration: started.append((generation, gate, duration)),
    )
    target.start_presentation_window()
    generation = started[0][0]

    stopped = target.stop_presentation_window("user_stopped")

    assert stopped["state"] == "cancelled"
    assert stopped["reason"] == "user_stopped"
    assert stopped["visible_indicator"] is False
    assert target._observe_presentation_frame(generation, jpeg(220)) is False
    assert target._presentation_gate is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"shared_physical_context_enabled": False}, "disabled"),
        ({"camera_enabled": False}, "camera"),
        ({"contextual_offers_enabled": False}, "contextual offers"),
    ],
)
def test_presentation_start_fails_closed_for_disabled_prerequisites(overrides, message, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    target = runtime(**overrides)
    monkeypatch.setattr(target, "_capture_camera_jpeg", lambda: jpeg(30))

    with pytest.raises(RuntimeError, match=message):
        target.start_presentation_window()


def test_presentation_requires_awake_agent_and_idle_camera(monkeypatch) -> None:
    target = runtime()
    monkeypatch.setattr(target, "_capture_camera_jpeg", lambda: jpeg(30))
    target._power_mode = "standby"
    with pytest.raises(RuntimeError, match="Awake"):
        target.start_presentation_window()
    target._power_mode = "awake"
    target._capability_profile = "conversation"
    with pytest.raises(RuntimeError, match="Agent"):
        target.start_presentation_window()
    target._capability_profile = "agent"
    target._camera_control_session_id = "camera-active"
    target._camera_control_last_activity = time.monotonic()
    with pytest.raises(RuntimeError, match="camera control"):
        target.start_presentation_window()
