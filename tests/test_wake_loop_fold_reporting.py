"""M1: the wake loop's automatic return-to-Standby fold must report failures like set_power_mode.

The real ``_listen_for_wake_word`` loop, fold wrapper and ``_set_motor_mode`` run here; only
the daemon HTTP edge, the spotter, the conversation and the native sleep are fixtures.
"""

from __future__ import annotations

import threading

import httpx
import numpy as np
import pytest

from homebody.config import AppConfig
from homebody.daemon_sleep import DaemonSleepError
from homebody.runtime import HermesVoiceRuntime


class _Media:
    def __init__(self) -> None:
        self.recording = False
        self.played: list[str] = []

    def start_playing(self) -> None:
        pass

    def stop_playing(self) -> None:
        pass

    def play_sound(self, path: str) -> None:
        self.played.append(path)

    def start_recording(self) -> None:
        self.recording = True

    def stop_recording(self) -> None:
        self.recording = False

    def get_DoA(self) -> tuple[float, bool]:
        return 0.0, False


class _Robot:
    def __init__(self) -> None:
        self.media = _Media()
        self.wake_calls = 0

    def wake_up(self) -> None:
        self.wake_calls += 1

    def stop_head_tracking(self) -> None:
        pass

    def start_head_tracking(self, weight: float = 1.0) -> None:
        pass


class _Spotter:
    """Return a keyword on chosen frame numbers; every other frame is silence."""

    def __init__(self, keywords_at: dict[int, str]) -> None:
        self.keywords_at = dict(keywords_at)
        self.frames = 0

    def accept(self, frame: np.ndarray, rate: int) -> str:
        self.frames += 1
        return self.keywords_at.get(self.frames, "")

    def reset(self) -> None:
        pass


class WakeLoopHarness:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        keywords_at: dict[int, str],
        total_frames: int,
    ) -> None:
        self.stop_event = threading.Event()
        self.robot = _Robot()
        runtime = HermesVoiceRuntime(
            self.robot,
            self.stop_event,
            config_loader=lambda: AppConfig(api_key="test-key", wake_cooldown_seconds=0.0),
        )
        self.runtime = runtime
        runtime._audio_ready = True
        runtime._control_ready.set()
        runtime._power_mode = "standby"
        runtime._spotter = _Spotter(keywords_at)  # type: ignore[assignment]
        runtime._take_button_wake = lambda: False  # type: ignore[method-assign]
        runtime._orient_to_voice = lambda _config: None  # type: ignore[method-assign]
        runtime.observe_presence = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        runtime._set_face_tracking = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        self.posts: list[str] = []

        def post(url: str, **_kwargs: object) -> httpx.Response:
            self.posts.append(url.rsplit("/", 1)[-1])
            return httpx.Response(200, request=httpx.Request("POST", url))

        monkeypatch.setattr("homebody.power.httpx.post", post)
        # Only the duplicate-wake pose read uses GET here; the fold measurement is stubbed below.
        monkeypatch.setattr(
            "homebody.power.httpx.get",
            lambda url, **_kwargs: httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={"control_mode": "enabled", "head_pose": {"z": 0.0, "pitch": 0.0}},
            ),
        )
        self.turns: list[str] = []
        self.turn_errors: dict[int, Exception] = {}

        def conversation(config: AppConfig, keyword: str) -> None:
            self.turns.append(keyword)
            error = self.turn_errors.get(len(self.turns))
            if error is not None:
                raise error

        runtime._run_selected_voice_conversation = conversation  # type: ignore[method-assign]
        self.native_sleeps = 0
        self.sleep_errors: dict[int, Exception] = {}

        def native_sleep() -> None:
            self.native_sleeps += 1
            error = self.sleep_errors.get(self.native_sleeps)
            if error is not None:
                raise error

        runtime._run_native_sleep = native_sleep  # type: ignore[method-assign]
        self.measurements = 0
        self.measurement_errors: dict[int, Exception] = {}

        def read_folded() -> bool:
            self.measurements += 1
            error = self.measurement_errors.get(self.measurements)
            if error is not None:
                raise runtime._motor_backend_unavailable(error) from error
            return True

        runtime._read_head_safely_folded = read_folded  # type: ignore[method-assign]
        self.frames_read = 0

        def read_frame() -> np.ndarray | None:
            self.frames_read += 1
            if self.frames_read > total_frames:
                self.stop_event.set()
                return None
            return np.zeros(1600, dtype=np.float32)

        runtime._read_16k_frame = read_frame  # type: ignore[method-assign]

    def run(self) -> dict[str, object]:
        thread = threading.Thread(target=self.runtime._listen_for_wake_word, daemon=True)
        thread.start()
        thread.join(10)
        assert not thread.is_alive(), "wake loop did not finish"
        return self.runtime.status()


def test_successful_post_turn_fold_returns_to_idle_without_error(monkeypatch) -> None:
    harness = WakeLoopHarness(monkeypatch, keywords_at={1: "hey"}, total_frames=6)

    status = harness.run()

    assert harness.turns == ["hey"]
    assert harness.native_sleeps == 1
    assert status["state"] == "waiting_for_wake_word"
    assert status["detail"] == "Local wake detection only"
    assert status["last_error"] == ""
    assert status["motors_enabled"] is False
    assert status["head_safely_folded"] is True
    assert harness.robot.media.recording is True


def test_post_turn_sleep_failure_is_reported_and_survives_idle_iterations(monkeypatch) -> None:
    harness = WakeLoopHarness(monkeypatch, keywords_at={1: "hey"}, total_frames=8)
    harness.sleep_errors[1] = RuntimeError("movement blocked")

    status = harness.run()

    # Seven silent frames followed the turn; the loop must not have reset the fault.
    assert harness.frames_read == 9
    assert status["state"] == "power_transition_error"
    assert status["power_mode"] == "standby"
    assert "motors remain enabled" in str(status["last_error"])
    assert "movement blocked" in str(status["last_error"])
    assert "torque remains enabled" in str(status["detail"])
    assert status["motors_enabled"] is True
    assert status["head_safely_folded"] is False
    # Torque was enabled for the fold and reconfirmed after the failure; nothing released it.
    assert harness.posts == ["enabled", "enabled", "enabled"]


def test_unavailable_measurement_reports_unknown_torque_not_enabled_torque(monkeypatch) -> None:
    harness = WakeLoopHarness(monkeypatch, keywords_at={1: "hey"}, total_frames=6)
    harness.measurement_errors[1] = httpx.ConnectError(
        "unplugged", request=httpx.Request("GET", "http://127.0.0.1:8000/api/state/full")
    )

    status = harness.run()

    assert status["state"] == "power_transition_error"
    assert "hardware connection is unavailable" in str(status["last_error"])
    assert "unverified" in str(status["detail"])
    assert "torque remains enabled" not in str(status["detail"])
    assert status["motors_enabled"] is None
    assert status["head_safely_folded"] is False
    assert harness.posts == ["enabled", "enabled"], "an unavailable measurement must not trigger another motor write"


def test_uncertain_sleep_latches_and_refuses_the_next_wake_without_retry(monkeypatch) -> None:
    harness = WakeLoopHarness(monkeypatch, keywords_at={1: "hey", 4: "hey"}, total_frames=8)
    harness.sleep_errors[1] = DaemonSleepError("completion unknown", uncertain=True)

    status = harness.run()

    assert harness.turns == ["hey"], "a second turn must not start after an uncertain sleep"
    assert harness.native_sleeps == 1
    assert harness.runtime._native_sleep_uncertain is True
    assert status["state"] == "power_transition_error"
    assert "unknown" in str(status["last_error"])
    assert status["motors_enabled"] is None
    assert harness.posts == ["enabled", "enabled"], "no motor write may follow the uncertain latch"


def test_privacy_race_after_wake_reports_fold_failure(monkeypatch) -> None:
    harness = WakeLoopHarness(monkeypatch, keywords_at={1: "hey"}, total_frames=6)
    harness.sleep_errors[1] = RuntimeError("movement blocked")
    runtime = harness.runtime

    def wake_then_privacy() -> None:
        harness.robot.wake_calls += 1
        runtime._privacy_requested.set()

    harness.robot.wake_up = wake_then_privacy  # type: ignore[method-assign]

    status = harness.run()

    assert harness.turns == [], "the turn must be abandoned when privacy arrives after the wake"
    assert harness.native_sleeps == 1
    assert status["state"] == "power_transition_error"
    assert "motors remain enabled" in str(status["last_error"])
    assert status["motors_enabled"] is True


def test_later_successful_fold_clears_fold_fault_but_keeps_turn_error(monkeypatch) -> None:
    harness = WakeLoopHarness(monkeypatch, keywords_at={1: "hey", 4: "hey", 7: "hey"}, total_frames=10)
    harness.sleep_errors[1] = RuntimeError("movement blocked")
    harness.turn_errors[3] = RuntimeError("bridge timeout")
    runtime = harness.runtime
    seen_states: list[tuple[str, str]] = []
    original_fold = runtime._fold_head_before_torque_release

    def record_then_fold() -> tuple[bool, str]:
        with runtime._status_lock:
            seen_states.append((runtime._status.state, runtime._status.last_error))
        return original_fold()

    runtime._fold_head_before_torque_release = record_then_fold  # type: ignore[method-assign]

    status = harness.run()

    assert harness.turns == ["hey", "hey", "hey"]
    assert harness.native_sleeps == 3
    # Turn 1's fault was still visible when turn 2's fold started, through the wake and the turn.
    assert seen_states[1][0] == "power_transition_error"
    assert "movement blocked" in seen_states[1][1]
    # Turn 2's verified fold cleared the fold fault; turn 3's turn error is a separate path.
    assert "movement blocked" not in str(status["last_error"])
    assert status["last_error"] == "bridge timeout"
    assert status["state"] == "waiting_for_wake_word"
    assert status["motors_enabled"] is False
    assert status["head_safely_folded"] is True
