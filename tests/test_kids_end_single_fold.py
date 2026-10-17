"""One Kids End must admit exactly one native sleep, even when the wake loop's post-turn fold overlaps.

Reproduces the retained-log shape (one POST /api/kids/stop, then two /play/goto_sleep with torque
re-enabled between them) with the real ``_listen_for_wake_word`` loop, real ``stop_kids_mode``,
real fold/torque code and a simulated daemon state behind the HTTP edge. Event barriers force each
overlap order. No robot, daemon or network is contacted; this is NOT physical acceptance.
"""

from __future__ import annotations

import threading
import time

import httpx
import numpy as np
import pytest

from homebody.config import AppConfig
from homebody.kids_mode import KidsProfile
from homebody.runtime import HermesVoiceRuntime


class _Media:
    def __init__(self) -> None:
        self.recording = False

    def start_playing(self) -> None:
        pass

    def stop_playing(self) -> None:
        pass

    def play_sound(self, _path: str) -> None:
        pass

    def start_recording(self) -> None:
        self.recording = True

    def stop_recording(self) -> None:
        self.recording = False


class _Daemon:
    """The daemon's measured state, driven only by the commands Homebody sends."""

    def __init__(self) -> None:
        self.control_mode = "enabled"
        self.folded = False
        self.posts: list[str] = []
        self.sleeps = 0
        self.get_error: Exception | None = None

    def state(self) -> dict[str, object]:
        pose = {"z": -0.05, "pitch": 0.4} if self.folded else {"z": 0.0, "pitch": 0.0}
        return {"control_mode": self.control_mode, "head_pose": pose}


class _Robot:
    def __init__(self, daemon: _Daemon) -> None:
        self.media = _Media()
        self.daemon = daemon

    def wake_up(self) -> None:
        self.daemon.folded = False

    def start_head_tracking(self, weight: float = 1.0) -> None:
        pass

    def stop_head_tracking(self) -> None:
        pass


class Rig:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, wakes_at: dict[int, str]) -> None:
        self.daemon = daemon = _Daemon()
        self.stop_event = threading.Event()
        self.robot = _Robot(daemon)
        runtime = HermesVoiceRuntime(
            self.robot, self.stop_event,
            config_loader=lambda: AppConfig(api_key="test-key", wake_cooldown_seconds=0.0),
        )
        self.runtime = runtime
        runtime._audio_ready = True
        runtime._control_ready.set()
        # The Kids greeting (wake_and_stay) already woke Reachy: Awake, torque on, unfolded.
        runtime._power_mode = "awake"
        runtime._motors_enabled = True
        runtime._head_safely_folded = False
        runtime._recording = True
        self.keywords = keywords = dict(wakes_at)
        self.frames = frames = {"n": 0}

        class Spotter:
            def accept(self, _frame, _rate):
                return keywords.pop(frames["n"], "")

            def reset(self):
                pass

        runtime._spotter = Spotter()  # type: ignore[assignment]
        runtime._take_button_wake = lambda: False  # type: ignore[method-assign]
        runtime._orient_to_voice = lambda _config: None  # type: ignore[method-assign]
        runtime.observe_presence = lambda *_a, **_k: None  # type: ignore[method-assign]
        runtime._notify_bridge_kids_session_async = lambda *_a, **_k: None  # type: ignore[method-assign]
        self.loop_done = threading.Event()

        def read_frame():
            frames["n"] += 1
            if self.loop_done.is_set() and not keywords:
                self.stop_event.set()
                return None
            time.sleep(0.002)
            return np.zeros(1600, dtype=np.float32)

        runtime._read_16k_frame = read_frame  # type: ignore[method-assign]

        def post(url: str, **_kwargs: object) -> httpx.Response:
            mode = url.rsplit("/", 1)[-1]
            daemon.posts.append(mode)
            daemon.control_mode = mode
            return httpx.Response(200, request=httpx.Request("POST", url))

        def get(url: str, **_kwargs: object) -> httpx.Response:
            if daemon.get_error is not None:
                raise daemon.get_error
            return httpx.Response(200, request=httpx.Request("GET", url), json=daemon.state())

        monkeypatch.setattr("homebody.power.httpx.post", post)
        monkeypatch.setattr("homebody.power.httpx.get", get)
        self.sleep_entered = threading.Event()
        self.release_sleep = threading.Event()
        self.release_sleep.set()

        def native_sleep() -> None:
            daemon.sleeps += 1
            self.sleep_entered.set()
            assert self.release_sleep.wait(5), "test barrier never released the native sleep"
            daemon.folded = True

        runtime._run_native_sleep = native_sleep  # type: ignore[method-assign]
        self.turns: list[str] = []
        self.in_turn = threading.Event()
        self.turn_exit_barrier: threading.Event | None = None

        def conversation(config: AppConfig, _keyword: str) -> None:
            self.turns.append(config.kids_session_id)
            self.in_turn.set()
            if not config.kids_mode_enabled:
                return
            deadline = time.monotonic() + 5
            # The continuous Kids loop runs until End makes its session stale.
            while runtime._kids_session_is_current(config.kids_session_id):
                assert time.monotonic() < deadline
                time.sleep(0.002)
            if self.turn_exit_barrier is not None:
                assert self.turn_exit_barrier.wait(5)

        runtime._run_selected_voice_conversation = conversation  # type: ignore[method-assign]
        self.fold_attempts = threading.Event()
        self.fold_gate: threading.Event | None = None  # parks the post-turn fold before the motor lock
        self.fold_results: list[tuple[bool, str]] = []
        original = runtime._fold_unless_released

        def fold_unless_released():
            self.fold_attempts.set()
            if self.fold_gate is not None:
                assert self.fold_gate.wait(5), "test barrier never released the post-turn fold"
            result = original()
            self.fold_results.append(result)
            return result

        runtime._fold_unless_released = fold_unless_released  # type: ignore[method-assign]
        self.thread = threading.Thread(target=runtime._listen_for_wake_word, daemon=True)

    def start_kids(self) -> None:
        self.runtime.start_kids_mode(KidsProfile(duration_minutes=15, motion_enabled=True), greet=False)
        # Start's own conversation stop must not leave the simulated greeting state behind.
        self.runtime._power_mode = "awake"

    def finish(self) -> dict[str, object]:
        self.loop_done.set()
        self.thread.join(10)
        assert not self.thread.is_alive(), "wake loop did not finish"
        return self.runtime.status()

    def enables_after_first_release(self) -> list[str]:
        posts = self.daemon.posts
        return [mode for mode in posts[posts.index("disabled") + 1:] if mode == "enabled"]


def test_end_fold_then_overlapping_post_turn_fold_sleeps_exactly_once(monkeypatch) -> None:
    rig = Rig(monkeypatch, wakes_at={1: "hey"})
    rig.start_kids()
    rig.release_sleep.clear()
    rig.turn_exit_barrier = rig.sleep_entered  # the turn ends while End's native sleep is running
    rig.thread.start()
    assert rig.in_turn.wait(5)
    end_result: dict[str, object] = {}
    end = threading.Thread(target=lambda: end_result.update(rig.runtime.stop_kids_mode(reason="parent")))
    end.start()
    assert rig.sleep_entered.wait(5)  # End's fold is inside the native sleep, holding the motor lock
    assert rig.fold_attempts.wait(5)  # the wake loop's finally already wants its own fold (the log race)
    time.sleep(0.05)
    rig.release_sleep.set()
    end.join(10)
    status = rig.finish()

    assert rig.daemon.sleeps == 1, "one End admits exactly one native sleep"
    assert rig.enables_after_first_release() == [], "no torque re-enable after the verified release"
    assert rig.daemon.posts.count("disabled") == 1
    assert end_result["last_fold_succeeded"] is True and end_result["active"] is False
    assert status["motors_enabled"] is False and status["head_safely_folded"] is True
    assert status["state"] == "waiting_for_wake_word" and not status["last_error"]


def test_baseline_unconditional_post_turn_fold_reproduces_the_logged_double_sleep(monkeypatch) -> None:
    """Mechanism proof: with the 0.5.33 post-turn fold, the same overlap sleeps twice."""
    rig = Rig(monkeypatch, wakes_at={1: "hey"})
    rig.runtime._fold_unless_released = rig.runtime._fold_head_before_torque_release  # type: ignore[method-assign]
    rig.start_kids()
    rig.turn_exit_barrier = rig.sleep_entered  # the turn ends while End's native sleep is running
    rig.thread.start()
    assert rig.in_turn.wait(5)
    rig.runtime.stop_kids_mode(reason="parent")
    rig.finish()

    assert rig.daemon.sleeps == 2
    assert rig.enables_after_first_release() == ["enabled"], "torque was re-enabled between the sleeps"


def test_post_turn_fold_first_then_end_reuses_the_verified_release(monkeypatch) -> None:
    rig = Rig(monkeypatch, wakes_at={1: "hey"})
    rig.start_kids()
    rig.runtime._power_mode = "standby"  # no greeting woke it: the turn's own finally folds first
    end_paused, end_continue = threading.Event(), threading.Event()
    original_revoke = rig.runtime._revoke_show_tell

    def revoke_then_wait() -> None:  # between End's Kids-lock invalidation and its motor-lock teardown
        original_revoke()
        end_paused.set()
        assert end_continue.wait(5)

    rig.runtime._revoke_show_tell = revoke_then_wait  # type: ignore[method-assign]
    rig.thread.start()
    assert rig.in_turn.wait(5)
    end_result: dict[str, object] = {}
    end = threading.Thread(target=lambda: end_result.update(rig.runtime.stop_kids_mode(reason="parent")))
    end.start()
    assert end_paused.wait(5)
    deadline = time.monotonic() + 5
    while "disabled" not in rig.daemon.posts:  # the turn's fold verified its release first
        assert time.monotonic() < deadline
        time.sleep(0.005)
    end_continue.set()
    end.join(10)
    status = rig.finish()

    assert rig.daemon.sleeps == 1
    assert rig.enables_after_first_release() == []
    assert end_result["last_fold_succeeded"] is True
    assert status["motors_enabled"] is False and status["head_safely_folded"] is True


def test_fresh_wake_and_next_end_still_fold_once_each(monkeypatch) -> None:
    rig = Rig(monkeypatch, wakes_at={1: "hey"})
    rig.start_kids()
    rig.thread.start()
    assert rig.in_turn.wait(5)
    rig.runtime.stop_kids_mode(reason="parent")
    deadline = time.monotonic() + 5
    while rig.runtime._voice_activity_lock.locked() or rig.runtime._motors_enabled is not False:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert rig.daemon.sleeps == 1
    # A later legitimate Start + wake re-enables torque; that End must fold again, exactly once.
    rig.in_turn.clear()
    rig.runtime.start_kids_mode(KidsProfile(duration_minutes=15, motion_enabled=True), greet=False)
    rig.runtime.set_power_mode("awake")
    enables_before = rig.daemon.posts.count("enabled")
    rig.keywords[rig.frames["n"] + 3] = "hey"
    assert rig.in_turn.wait(5)
    second = rig.runtime.stop_kids_mode(reason="parent")
    status = rig.finish()

    assert rig.daemon.posts.count("enabled") >= enables_before
    assert rig.daemon.sleeps == 2, "each End folds exactly once"
    assert second["last_fold_succeeded"] is True
    assert status["motors_enabled"] is False and status["head_safely_folded"] is True


def test_stale_post_turn_fold_never_folds_a_fresh_legitimate_wake(monkeypatch) -> None:
    """Parent-found race (3 sleeps): turn 1's finally sampled Standby off the motor lock, then a fresh
    Start + Awake won the lock; the stale fold must not re-fold the newly woken robot."""
    rig = Rig(monkeypatch, wakes_at={1: "hey"})
    rig.start_kids()
    rig.turn_exit_barrier = threading.Event()
    rig.fold_gate = threading.Event()
    rig.thread.start()
    assert rig.in_turn.wait(5)
    rig.runtime.stop_kids_mode(reason="parent")
    assert rig.daemon.sleeps == 1
    rig.turn_exit_barrier.set()
    assert rig.fold_attempts.wait(5)  # the finally saw Standby and is parked before the motor lock
    rig.runtime.start_kids_mode(KidsProfile(duration_minutes=15, motion_enabled=True), greet=False)
    rig.runtime.set_power_mode("awake")  # what a fresh Kids greeting (wake_and_stay) does
    posts_after_wake = list(rig.daemon.posts)
    rig.fold_gate.set()
    wait_until = time.monotonic() + 5
    while not rig.fold_results:
        assert time.monotonic() < wait_until
        time.sleep(0.005)

    assert rig.daemon.sleeps == 1, "a stale post-turn fold never folds another owner's fresh wake"
    assert rig.daemon.posts == posts_after_wake, "no motor write from the stale fold"
    assert rig.runtime._motors_enabled is True and rig.runtime._power_mode == "awake"
    second = rig.runtime.stop_kids_mode(reason="parent")  # the fresh session's End still folds once
    status = rig.finish()
    assert rig.daemon.sleeps == 2 and second["last_fold_succeeded"] is True
    assert status["motors_enabled"] is False and status["head_safely_folded"] is True


@pytest.mark.parametrize("baseline", [False, True])
def test_end_while_wake_loop_idles_in_awake_folds_once(monkeypatch, baseline) -> None:
    """The conversation already ended (e.g. silence); End's Standby must not be re-applied by the
    idle wake loop's off-lock mode comparison. ``baseline`` reinstates the 0.5.33 re-apply."""
    rig = Rig(monkeypatch, wakes_at={})
    runtime = rig.runtime
    rig.start_kids()
    observed = threading.Event()
    reconcile = runtime._apply_power_mode if baseline else runtime._reconcile_power_mode

    def observe_then_reconcile() -> None:
        observed.set()
        reconcile()

    runtime._reconcile_power_mode = observe_then_reconcile  # type: ignore[method-assign]
    with runtime._status_lock:
        runtime._status.power_mode = "awake"
    rig.release_sleep.clear()
    rig.thread.start()
    end = threading.Thread(target=runtime.stop_kids_mode, kwargs={"reason": "parent"})
    end.start()
    assert rig.sleep_entered.wait(5)
    assert observed.wait(5)  # the idle loop saw Standby before End recorded it as applied
    rig.release_sleep.set()
    end.join(10)
    status = rig.finish()

    assert rig.daemon.sleeps == (2 if baseline else 1)
    if not baseline:
        assert rig.enables_after_first_release() == []
        assert status["motors_enabled"] is False and status["head_safely_folded"] is True


def test_measured_torque_back_on_is_never_skipped_by_stale_booleans(monkeypatch) -> None:
    rig = Rig(monkeypatch, wakes_at={1: "hey"})
    rig.start_kids()
    rig.turn_exit_barrier = threading.Event()
    rig.thread.start()
    assert rig.in_turn.wait(5)
    rig.runtime.stop_kids_mode(reason="parent")
    assert rig.daemon.sleeps == 1
    # Something outside this process re-enabled torque and lifted the head after the release.
    rig.daemon.control_mode, rig.daemon.folded = "enabled", False
    rig.turn_exit_barrier.set()
    status = rig.finish()

    assert rig.daemon.sleeps == 2, "a measured unreleased state requires the native fold"
    assert status["motors_enabled"] is False and status["head_safely_folded"] is True


def test_unknown_measurement_after_release_fails_closed_without_motor_write(monkeypatch) -> None:
    rig = Rig(monkeypatch, wakes_at={1: "hey"})
    rig.start_kids()
    rig.turn_exit_barrier = threading.Event()
    rig.thread.start()
    assert rig.in_turn.wait(5)
    rig.runtime.stop_kids_mode(reason="parent")
    posts_after_end = list(rig.daemon.posts)
    rig.daemon.get_error = httpx.ConnectError(
        "unplugged", request=httpx.Request("GET", "http://127.0.0.1:8000/api/state/full"),
    )
    rig.turn_exit_barrier.set()
    status = rig.finish()

    assert rig.daemon.sleeps == 1
    assert rig.daemon.posts == posts_after_end, "an unknown pose never triggers another motor write"
    assert status["state"] == "power_transition_error"
    assert status["motors_enabled"] is None and status["head_safely_folded"] is False
    assert "hardware connection is unavailable" in str(status["last_error"])


def test_failed_end_fold_is_not_hidden_and_the_turn_fold_retries_normally(monkeypatch) -> None:
    rig = Rig(monkeypatch, wakes_at={1: "hey"})
    rig.start_kids()
    rig.turn_exit_barrier = threading.Event()
    rig.thread.start()
    assert rig.in_turn.wait(5)
    original = rig.runtime._run_native_sleep
    calls = {"n": 0}

    def fail_first() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            rig.daemon.sleeps += 1
            raise RuntimeError("movement blocked")
        original()

    rig.runtime._run_native_sleep = fail_first  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        rig.runtime.stop_kids_mode(reason="parent")
    assert rig.runtime.status()["kids_mode"]["last_fold_succeeded"] is False
    rig.turn_exit_barrier.set()
    status = rig.finish()

    assert rig.daemon.sleeps == 2, "an unverified fold is never treated as a release to reuse"
    assert status["motors_enabled"] is False and status["head_safely_folded"] is True
