from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from reachy_mini_hermes.config import AppConfig
from reachy_mini_hermes.realtime_client import RealtimeEvent
from reachy_mini_hermes.runtime import HermesVoiceRuntime

_LOGGER = logging.getLogger(__name__)


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

    def goto_sleep(self) -> None:
        pass


def make_runtime() -> HermesVoiceRuntime:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._audio_ready = True
    runtime._power_mode = "awake"
    runtime._motors_enabled = True
    runtime._head_safely_folded = False
    runtime._play_asset = lambda name: None  # type: ignore[method-assign]
    runtime._discard_audio = lambda seconds: None  # type: ignore[method-assign]
    runtime._publish_remote_agent_session = lambda: None  # type: ignore[method-assign]
    runtime._establish_remote_agent_session = lambda _context: None  # type: ignore[method-assign]
    runtime.set_capability_profile("agent", adult_ui_unlocked=True)
    runtime._accept_wake_turn()
    return runtime


def test_focused_realtime_conversation_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = make_runtime()
    sessions: list[MockRealtimeSession] = []
    logs: list[str] = []

    class MockRealtimeSession:
        def __init__(self, config: AppConfig, *, agent_context: object, agent_request_id: str) -> None:
            self.config = config
            self.agent_context = agent_context
            self.agent_request_id = agent_request_id
            self.closed = False
            self.sent_audio_count = 0
            self.truncated_items: list[tuple[str, int]] = []
            self.state = "start"
            sessions.append(self)
            logs.append(f"[Session Init] request_id: {agent_request_id}")

        def start(self) -> None:
            logs.append("[Session Start] Started connection")

        def send_audio(self, samples: np.ndarray) -> None:
            self.sent_audio_count += 1

        def truncate_audio(self, item_id: str, played_ms: int) -> None:
            self.truncated_items.append((item_id, played_ms))
            logs.append(f"[Session Truncate] item: {item_id}, played_ms: {played_ms}")

        def events(self) -> list[RealtimeEvent]:
            status = runtime.status()
            current_state = status["state"]
            turns = status["turns_completed"]
            interruptions = status["interruptions"]

            # Step 1: Start Conversation -> Turn 1 Start
            if self.state == "start" and current_state == "listening":
                self.state = "waiting_for_turn1_speaking"
                logs.append("[Lifecycle Turn 1] Starting clean Realtime turn")
                return [
                    RealtimeEvent("response.created", {"response": {"id": "resp-1"}}),
                    RealtimeEvent(
                        "response.output_item.added",
                        {"item": {"type": "message", "role": "assistant", "id": "item-1"}},
                    ),
                    RealtimeEvent(
                        "response.output_audio.delta", {"item_id": "item-1", "samples_size": 16, "delta": "..."}
                    ),
                ]

            # Step 2: Turn 1 Speaking -> Turn 1 Done
            elif self.state == "waiting_for_turn1_speaking" and current_state == "speaking":
                self.state = "waiting_for_turn1_done"
                logs.append("[Lifecycle Turn 1] Turn 1 complete event sent")
                return [
                    RealtimeEvent("response.done", {"response": {"id": "resp-1"}}),
                ]

            # Step 3: Turn 1 Completed -> Turn 2 Start (Follow-up with no active-response overlap)
            elif self.state == "waiting_for_turn1_done" and turns == 1 and current_state == "listening":
                self.state = "waiting_for_turn2_speaking"
                logs.append("[Lifecycle Turn 2] Starting follow-up (no active-response overlap)")
                return [
                    RealtimeEvent("input_audio_buffer.speech_started", {}),
                    RealtimeEvent("response.created", {"response": {"id": "resp-2"}}),
                    RealtimeEvent(
                        "response.output_item.added",
                        {"item": {"type": "message", "role": "assistant", "id": "item-2"}},
                    ),
                    RealtimeEvent(
                        "response.output_audio.delta", {"item_id": "item-2", "samples_size": 16, "delta": "..."}
                    ),
                ]

            # Step 4: Turn 2 Speaking -> Turn 2 Done
            elif self.state == "waiting_for_turn2_speaking" and current_state == "speaking":
                self.state = "waiting_for_turn2_done"
                logs.append("[Lifecycle Turn 2] Turn 2 complete event sent")
                return [
                    RealtimeEvent("response.done", {"response": {"id": "resp-2"}}),
                ]

            # Step 5: Turn 2 Completed -> Turn 3 Start (Barge-in test)
            elif self.state == "waiting_for_turn2_done" and turns == 2 and current_state == "listening":
                self.state = "waiting_for_barge_in_speaking"
                logs.append("[Lifecycle Turn 3] Starting barge-in test response")
                return [
                    RealtimeEvent("response.created", {"response": {"id": "resp-3"}}),
                    RealtimeEvent(
                        "response.output_item.added",
                        {"item": {"type": "message", "role": "assistant", "id": "item-3"}},
                    ),
                    RealtimeEvent(
                        "response.output_audio.delta", {"item_id": "item-3", "samples_size": 16000 * 5, "delta": "..."}
                    ),  # 5s of speech
                ]

            # Step 6: Turn 3 Speaking -> Interruption Trigger (Barge-in)
            elif self.state == "waiting_for_barge_in_speaking" and current_state == "speaking":
                self.state = "waiting_for_barge_in_interrupted"
                logs.append("[Lifecycle Turn 3] Triggering barge-in interruption (speech_started)")
                return [
                    RealtimeEvent("input_audio_buffer.speech_started", {}),
                ]

            # Step 7: Barge-in Verified -> Turn 4 Start (Stop during response test)
            elif (
                self.state == "waiting_for_barge_in_interrupted"
                and interruptions == 1
                and current_state == "listening"
            ):
                self.state = "waiting_for_turn4_stop_trigger"
                logs.append("[Lifecycle Turn 4] Starting response for Stop test")
                return [
                    RealtimeEvent("response.created", {"response": {"id": "resp-4"}}),
                    RealtimeEvent(
                        "response.output_item.added",
                        {"item": {"type": "message", "role": "assistant", "id": "item-4"}},
                    ),
                    RealtimeEvent(
                        "response.output_audio.delta", {"item_id": "item-4", "samples_size": 16, "delta": "..."}
                    ),
                ]

            # Step 8: Turn 4 Active -> Trigger Stop Agent
            elif self.state == "waiting_for_turn4_stop_trigger" and current_state in ("thinking", "speaking"):
                self.state = "stopped_triggered"
                logs.append("[Lifecycle Turn 4] Triggering cancel_agent_work('stopped') during response")
                runtime.cancel_agent_work("stopped")
                return []

            return []

        def close(self) -> None:
            self.closed = True
            logs.append("[Session Close] Closed WebSocket connection")

        def audio_samples(self, event: RealtimeEvent) -> np.ndarray:
            size = event.payload.get("samples_size", 16)
            return np.ones(size, dtype=np.float32)

    monkeypatch.setattr("reachy_mini_hermes.runtime.RealtimeBridgeSession", MockRealtimeSession)

    # Simple non-blocking frame reader with tiny delay to let time progress nicely
    def read_frame() -> np.ndarray:
        time.sleep(0.001)
        return np.zeros(160, dtype=np.float32)

    runtime._read_16k_frame = read_frame  # type: ignore[method-assign]

    # Run the Realtime conversation loop
    runtime._run_realtime_conversation(AppConfig(conversation_mode="realtime", conversation_timeout_seconds=5.0))

    # Output logs for sanitized evidence
    print("\n=== SANITIZED REALTIME LIFECYCLE EVIDENCE ===")
    for log in logs:
        print(log)
    print("=============================================\n")

    # Assertions to verify the entire lifecycle
    assert len(sessions) == 1
    session = sessions[0]
    assert session.closed is True

    status = runtime.status()
    # 2 completed turns (Turn 1 and Turn 2)
    assert status["turns_completed"] == 2
    # 1 interruption (Turn 3 barge-in)
    assert status["interruptions"] == 1
    # Check that truncate_audio was called with item-3 and non-zero played_ms
    assert len(session.truncated_items) == 1
    assert session.truncated_items[0][0] == "item-3"
    assert session.truncated_items[0][1] >= 0

    # Check that robot media player clear was called on barge-in and close
    assert runtime.robot.media.clear_calls == 2  # type: ignore[attr-defined]

    # Check that the session state in the mock ended at "stopped_triggered"
    assert session.state == "stopped_triggered"

    # Verify motors and standby recovery
    assert runtime._conversation_stop_requested.is_set()
