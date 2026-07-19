from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from reachy_mini_hermes.config import AppConfig
from reachy_mini_hermes.hermes_client import SpeechAudio
from reachy_mini_hermes.runtime import Announcement, HermesVoiceRuntime

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "reachy_mini_hermes" / "static"


class FakeAudio:
    def clear_player(self) -> None:
        pass


class FakeMedia:
    def __init__(self) -> None:
        self.audio = FakeAudio()
        self.played: list[str] = []

    def play_sound(self, path: str) -> None:
        self.played.append(path)


class FakeRobot:
    def __init__(self) -> None:
        self.media = FakeMedia()


def ready_runtime() -> HermesVoiceRuntime:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._audio_ready = True
    runtime._announcement_worker = object()  # type: ignore[assignment]
    return runtime


def test_announcement_queue_validates_and_reports_depth() -> None:
    runtime = ready_runtime()

    result = runtime.queue_announcement(
        "  Dinner   is ready.  ",
        behavior="voice_only",
        repeat=2,
        pause_seconds=0.5,
    )

    assert result == {"ok": True, "queued": True, "queue_depth": 1}
    item = runtime._announcement_queue.get_nowait()
    assert item.text == "Dinner   is ready."
    assert item.behavior == "voice_only"
    assert item.repeat == 2


@pytest.mark.parametrize("behavior", ["invalid", "sleep", "wake"])
def test_announcement_queue_rejects_unknown_behavior(behavior: str) -> None:
    with pytest.raises(ValueError, match="behavior"):
        ready_runtime().queue_announcement("Test", behavior=behavior)


def test_announcements_are_blocked_in_privacy_modes() -> None:
    runtime = ready_runtime()
    runtime._power_mode = "sleep"

    with pytest.raises(RuntimeError, match="Meeting and Sleep"):
        runtime.queue_announcement("Do not play")


def test_stop_announcements_clears_the_bounded_queue() -> None:
    runtime = ready_runtime()
    runtime.queue_announcement("One")
    runtime.queue_announcement("Two")

    result = runtime.stop_announcements(clear_queue=True)

    assert result == {"ok": True, "active_cancelled": False, "queued_cleared": 2}
    assert runtime.status()["announcement_queue_depth"] == 0


def test_stop_waiting_announcement_does_not_interrupt_conversation_audio() -> None:
    runtime = ready_runtime()
    item = Announcement("Waiting", cancellation_generation=runtime._announcement_cancellation_generation)
    runtime._announcement_current = item
    runtime._announcement_active.set()

    result = runtime.stop_announcements(clear_queue=False)

    assert result["active_cancelled"] is True
    assert item.cancel_event.is_set()
    assert runtime.robot.media.played == []


def test_stop_generation_invalidates_item_already_dequeued(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = ready_runtime()
    runtime.queue_announcement("Must not play")
    item = runtime._announcement_queue.get_nowait()
    runtime._announcement_queue.task_done()
    runtime.stop_announcements(clear_queue=False)
    runtime._announcement_queue.put_nowait(item)
    stop_event = runtime.stop_event
    synthesized = threading.Event()

    class FailingClient:
        def __init__(self, config: AppConfig) -> None:
            pass

        def synthesize(self, text: str) -> SpeechAudio:
            synthesized.set()
            raise AssertionError("cancelled item reached synthesis")

        def close(self) -> None:
            pass

    monkeypatch.setattr("reachy_mini_hermes.runtime.HermesBridgeClient", FailingClient)
    worker = threading.Thread(target=runtime._run_announcement_worker, daemon=True)
    runtime._announcement_worker = worker
    worker.start()
    deadline = time.monotonic() + 1
    while runtime._announcement_queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)
    stop_event.set()
    worker.join(timeout=1)

    assert not synthesized.is_set()
    assert runtime.status()["announcements_completed"] == 0


def test_shutdown_cancellation_prevents_post_shutdown_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    stop_event = threading.Event()
    runtime = HermesVoiceRuntime(
        FakeRobot(), stop_event, config_loader=lambda: AppConfig(api_key="test-key")
    )
    runtime._audio_ready = True
    synthesis_started = threading.Event()
    release_synthesis = threading.Event()
    power_calls: list[str] = []

    class BlockingClient:
        def __init__(self, config: AppConfig) -> None:
            pass

        def synthesize(self, text: str) -> SpeechAudio:
            synthesis_started.set()
            release_synthesis.wait(2)
            return SpeechAudio(b"audio", "audio/mpeg", ".mp3", "fake-tts")

        def close(self) -> None:
            pass

    def fake_power(mode: str, *, duration_seconds: float = 0.0) -> dict[str, object]:
        power_calls.append(mode)
        runtime._power_mode = mode
        return runtime.status()

    monkeypatch.setattr("reachy_mini_hermes.runtime.HermesBridgeClient", BlockingClient)
    runtime.set_power_mode = fake_power  # type: ignore[method-assign]
    worker = threading.Thread(target=runtime._run_announcement_worker, daemon=True)
    runtime._announcement_worker = worker
    worker.start()
    runtime.queue_announcement("Shutdown race", behavior="wake_and_return")
    assert synthesis_started.wait(2)

    stop_event.set()
    runtime._audio_ready = False
    runtime._cancel_announcements(clear_queue=True)
    release_synthesis.set()
    worker.join(timeout=2)

    assert power_calls == ["awake"]
    assert runtime.robot.media.played == []
    assert not worker.is_alive()


def test_long_announcement_duration_is_not_truncated_to_short_voice_limit(tmp_path: Path) -> None:
    missing_audio = tmp_path / "missing.mp3"
    duration = HermesVoiceRuntime._announcement_audio_duration(
        missing_audio, fallback_text="x" * 15_000
    )

    assert duration > 1_000


def test_wake_and_return_restoration_preserves_following_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    stop_event = threading.Event()
    runtime = HermesVoiceRuntime(
        FakeRobot(), stop_event, config_loader=lambda: AppConfig(api_key="test-key")
    )
    runtime._audio_ready = True
    played: list[str] = []

    class FakeClient:
        def __init__(self, config: AppConfig) -> None:
            pass

        def synthesize(self, text: str) -> SpeechAudio:
            return SpeechAudio(b"audio", "audio/mpeg", ".mp3", "fake-tts")

        def close(self) -> None:
            pass

    def fake_power(
        mode: str,
        *,
        duration_seconds: float = 0.0,
        cancel_announcements: bool = True,
    ) -> dict[str, object]:
        runtime._power_mode = mode
        return runtime.status()

    monkeypatch.setattr("reachy_mini_hermes.runtime.HermesBridgeClient", FakeClient)
    runtime.set_power_mode = fake_power  # type: ignore[method-assign]
    runtime._play_announcement_audio = lambda item, speech, text: played.append(text)  # type: ignore[method-assign]
    runtime._announcement_worker = object()  # type: ignore[assignment]
    runtime.queue_announcement("First", behavior="wake_and_return")
    runtime.queue_announcement("Second", behavior="wake_and_return")
    worker = threading.Thread(target=runtime._run_announcement_worker, daemon=True)
    runtime._announcement_worker = worker
    worker.start()

    deadline = time.monotonic() + 2
    while runtime.status()["announcements_completed"] < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop_event.set()
    worker.join(timeout=1)

    assert played == ["First", "Second"]
    assert runtime.status()["announcements_completed"] == 2
    assert runtime._power_mode == "standby"


def test_worker_synthesizes_once_and_repeats_serially(monkeypatch: pytest.MonkeyPatch) -> None:
    played: list[str] = []
    stop_event = threading.Event()
    runtime = HermesVoiceRuntime(
        FakeRobot(),
        stop_event,
        config_loader=lambda: AppConfig(api_key="test-key"),
    )
    runtime._audio_ready = True

    class FakeClient:
        def __init__(self, config: AppConfig) -> None:
            self.config = config

        def synthesize(self, text: str) -> SpeechAudio:
            assert text == "Repeat me"
            return SpeechAudio(b"audio", "audio/mpeg", ".mp3", "fake-tts")

        def close(self) -> None:
            pass

    monkeypatch.setattr("reachy_mini_hermes.runtime.HermesBridgeClient", FakeClient)
    runtime._play_announcement_audio = lambda item, speech, text: played.append(text)  # type: ignore[method-assign]
    worker = threading.Thread(target=runtime._run_announcement_worker, daemon=True)
    runtime._announcement_worker = worker
    worker.start()

    runtime.queue_announcement("Repeat me", behavior="voice_only", repeat=3, pause_seconds=0)
    deadline = time.monotonic() + 2
    while runtime.status()["announcements_completed"] < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop_event.set()
    worker.join(timeout=1)

    assert played == ["Repeat me", "Repeat me", "Repeat me"]
    assert runtime.status()["announcement_provider"] == "fake-tts"
    assert runtime.status()["announcement_last_text"] == "Repeat me"
    assert runtime.status()["announcement_busy"] is False


def test_privacy_transition_wins_over_wake_and_return(monkeypatch: pytest.MonkeyPatch) -> None:
    stop_event = threading.Event()
    runtime = HermesVoiceRuntime(
        FakeRobot(), stop_event, config_loader=lambda: AppConfig(api_key="test-key")
    )
    runtime._audio_ready = True

    class FakeClient:
        def __init__(self, config: AppConfig) -> None:
            pass

        def synthesize(self, text: str) -> SpeechAudio:
            return SpeechAudio(b"audio", "audio/mpeg", ".mp3", "fake-tts")

        def close(self) -> None:
            pass

    def fake_power(mode: str, *, duration_seconds: float = 0.0) -> dict[str, object]:
        runtime._power_mode = mode
        if mode in {"meeting", "sleep"}:
            runtime._privacy_requested.set()
        return runtime.status()

    entered_meeting = threading.Event()

    def enter_meeting(item: object, speech: SpeechAudio, text: str) -> None:
        runtime._power_mode = "meeting"
        runtime._meeting_until = time.monotonic() + 60
        runtime._privacy_requested.set()
        entered_meeting.set()

    monkeypatch.setattr("reachy_mini_hermes.runtime.HermesBridgeClient", FakeClient)
    runtime.set_power_mode = fake_power  # type: ignore[method-assign]
    runtime._play_announcement_audio = enter_meeting  # type: ignore[method-assign]
    worker = threading.Thread(target=runtime._run_announcement_worker, daemon=True)
    runtime._announcement_worker = worker
    worker.start()

    runtime.queue_announcement("Privacy wins", behavior="wake_and_return")
    assert entered_meeting.wait(2)
    deadline = time.monotonic() + 2
    while runtime.status()["announcement_busy"] and time.monotonic() < deadline:
        time.sleep(0.01)
    stop_event.set()
    worker.join(timeout=1)

    assert runtime._power_mode == "meeting"
    assert runtime._privacy_requested.is_set()


def test_announcement_ui_exposes_full_tts_controls_and_private_routes() -> None:
    html = (STATIC / "index.html").read_text()
    main = (STATIC / "main.js").read_text()
    worker = (STATIC / "service-worker.js").read_text()

    for element_id in (
        "tab-announce",
        "announcement-text",
        "announcement-provider",
        "announcement-model",
        "announcement-voice",
        "announcement-behavior",
        "announcement-repeat",
        "announcement-pause",
        "announcement-send",
        "announcement-stop",
        "announcement-current",
        "announcement-last",
    ):
        assert f'id="{element_id}"' in html
    assert 'maxlength="15000"' in html
    assert 'value="voice_only"' in html
    assert 'value="wake_and_return"' in html
    assert 'value="wake_and_stay"' in html
    assert 'fetch("/api/announcements"' in main
    assert 'fetch("/api/announcements/stop"' in main
    assert "reachy-hermes-announcement-draft" in main
    assert "window.sessionStorage" in main
    assert "window.localStorage.setItem(\"reachy-hermes-announcement-draft\"" not in main
    assert 'id="announcement-live"' in html
    assert 'role="alert"' in html
    assert "Voice only · do not change power state" in html
    assert "reachy-hermes-shell-v22" in worker
    assert "/static/main.js?v=22" in html
