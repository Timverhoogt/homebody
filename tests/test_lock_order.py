from __future__ import annotations

import threading

from test_camera_joystick import ready_runtime

from reachy_mini_hermes.runtime import HermesVoiceRuntime


class OrderedLock:
    """RLock that records any _kids_lock acquisition followed by _motor_transition_lock."""

    held = threading.local()

    def __init__(self, name: str, violations: list[str]) -> None:
        self._lock = threading.RLock()
        self._name = name
        self._violations = violations

    def _stack(self) -> list[str]:
        if not hasattr(self.held, "names"):
            self.held.names = []
        return self.held.names

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        stack = self._stack()
        if self._name == "motor" and "kids" in stack and "motor" not in stack:
            self._violations.append("motor acquired while holding kids")
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            stack.append(self._name)
        return acquired

    def release(self) -> None:
        stack = self._stack()
        stack.reverse()
        stack.remove(self._name)
        stack.reverse()
        self._lock.release()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_args: object) -> None:
        self.release()


def install_ordered_locks(runtime: HermesVoiceRuntime) -> list[str]:
    violations: list[str] = []
    runtime._kids_lock = OrderedLock("kids", violations)  # type: ignore[assignment]
    runtime._motor_transition_lock = OrderedLock("motor", violations)  # type: ignore[assignment]
    return violations


def test_manual_and_camera_controls_take_motor_lock_before_kids_lock() -> None:
    runtime, _actions = ready_runtime()
    violations = install_ordered_locks(runtime)

    runtime.queue_manual_robot_action("look", "left")
    runtime.queue_precision_robot_action("x", 1.0)
    started = runtime.start_camera_control(camera_feed_enabled=True, controls_enabled=True, adult_ui_unlocked=True)
    session_id = str(started["session_id"])
    runtime.queue_camera_control(session_id, 1, 0.5, 0.0)
    runtime.center_camera_control(session_id, 2)

    assert violations == []


def test_kids_lock_state_is_readable_while_kids_lock_is_held_elsewhere() -> None:
    runtime, _actions = ready_runtime()
    runtime._kids_locked = True
    holding = threading.Event()
    release = threading.Event()

    def hold_kids_lock() -> None:
        with runtime._kids_lock:
            holding.set()
            release.wait(timeout=5.0)

    holder = threading.Thread(target=hold_kids_lock)
    holder.start()
    try:
        assert holding.wait(timeout=1.0)
        result: list[bool] = []
        reader = threading.Thread(target=lambda: result.append(runtime.kids_controls_locked))
        reader.start()
        reader.join(timeout=1.0)
        assert result == [True], "the HTTP middleware must not block on _kids_lock"
    finally:
        release.set()
        holder.join(timeout=5.0)
