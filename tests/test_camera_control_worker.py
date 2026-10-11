"""Real worker concurrency regressions; no hardware or camera clients."""
from __future__ import annotations

import threading

import pytest
from test_camera_joystick import ready_runtime
from test_robot_tools import FakeRobot

from homebody.robot_tools import ReachyRobotActions


def worker_runtime(monkeypatch, *, gate=None):
    runtime, _ = ready_runtime()
    robot = FakeRobot()
    monkeypatch.setattr("homebody.robot_tools.create_head_pose", lambda **kwargs: kwargs)
    monkeypatch.setattr("homebody.robot_tools.interpolate_head_pose", lambda start, target, ratio: target)

    def before():
        if gate is not None:
            gate()
        runtime._before_robot_action()  # Includes the real Kids/privacy safety probe.

    actions = ReachyRobotActions(robot, threading.Event(), before_action=before)
    runtime._actions = actions
    actions.start()
    session = runtime.start_camera_control(
        camera_feed_enabled=True, controls_enabled=True, adult_ui_unlocked=True,
    )["session_id"]
    return runtime, actions, robot, session


def test_center_real_worker_finishes_through_safety_gate(monkeypatch):
    runtime, actions, robot, session = worker_runtime(monkeypatch)
    try:
        result = runtime.center_camera_control(session, 1)
        assert result["result"]["ok"] is True
        assert result["result"]["axis"] == "center_all"
        assert robot.body_samples[-1] == 0.0
        assert robot.head_samples[-1]["roll"] == 0.0
    finally:
        actions.close()


def test_center_worker_sends_combined_sdk_head_base_targets(monkeypatch):
    runtime, actions, robot, session = worker_runtime(monkeypatch)
    combined = []
    monkeypatch.setattr(robot, "set_target", lambda **kwargs: combined.append(kwargs), raising=False)
    try:
        result = runtime.center_camera_control(session, 1)
        assert result["result"]["ok"] is True
        assert combined
        assert combined[-1]["head"]["roll"] == 0.0
        assert combined[-1]["body_yaw"] == 0.0
    finally:
        actions.close()


def test_center_reports_execution_failure_not_idle(monkeypatch):
    runtime, actions, robot, session = worker_runtime(monkeypatch)
    try:
        def fail(**kwargs):
            raise RuntimeError("fake target failed")
        monkeypatch.setattr(actions, "_run_precision_interpolation", fail)
        with pytest.raises(RuntimeError, match="fake target failed"):
            runtime.center_camera_control(session, 1)
        assert actions.wait_idle(1)
        assert all(value == 0.0 for value in robot.body_samples)  # Joystick only.
    finally:
        actions.close()


@pytest.mark.parametrize("transition", ["stop", "privacy", "end"])
def test_center_concurrent_invalidation_cannot_execute_later(monkeypatch, transition):
    entered, release = threading.Event(), threading.Event()
    calls = 0

    def gate():
        nonlocal calls
        calls += 1
        if calls == 2:  # Let the joystick finish; block the admitted Center.
            entered.set()
            assert release.wait(2)

    runtime, actions, robot, session = worker_runtime(monkeypatch, gate=gate)
    errors = []

    def center():
        try:
            runtime.center_camera_control(session, 1)
        except RuntimeError as exc:
            errors.append(str(exc))

    requester = threading.Thread(target=center)
    requester.start()
    try:
        assert entered.wait(2)
        body_before = list(robot.body_samples)
        moving_heads_before = sum(isinstance(head, dict) for head in robot.head_samples)
        if transition == "privacy":
            with runtime._motor_transition_lock, runtime._kids_lock:
                runtime._privacy_requested.set()
                runtime.invalidate_camera_control()
                actions.cancel(stop_media=False)
        elif transition == "stop":
            token = runtime._camera_center_cancel
            stopper = threading.Thread(target=runtime.stop_manual_robot_action)
            stopper.start()
            assert token.wait(1)
            release.set()
            stopper.join(2)
            assert not stopper.is_alive()
        else:
            # End normally waits for worker idle; do not let that mask this race.
            monkeypatch.setattr(actions, "wait_idle", lambda timeout: True)
            runtime.end_camera_control(session, 2)
        release.set()
        requester.join(2)
        assert not requester.is_alive()
        assert errors
        assert robot.body_samples == body_before  # No Center interpolation.
        assert sum(isinstance(head, dict) for head in robot.head_samples) == moving_heads_before
    finally:
        release.set()
        requester.join(2)
        actions.close()


def test_center_timeout_token_cannot_cancel_newer_work(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = 0

    def gate():
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            assert release.wait(2)

    runtime, actions, robot, session = worker_runtime(monkeypatch, gate=gate)
    monkeypatch.setattr("homebody.manual_control._CAMERA_CENTER_TIMEOUT_SECONDS", 0.08)
    try:
        with pytest.raises(RuntimeError, match="did not complete in time"):
            runtime.center_camera_control(session, 1)
        assert entered.is_set()
        heads_before = sum(isinstance(head, dict) for head in robot.head_samples)
        # A delayed failed Center must not clear this subsequent action.
        done = threading.Event()
        outcomes = []
        actions.enqueue("move_reachy_head", {"direction": "left"}, hold_pose=True,
                        on_complete=lambda result: (outcomes.append(result), done.set()))
        release.set()
        assert done.wait(2)
        assert outcomes[0]["ok"] is True
        assert len(robot.targets) == 1
        assert sum(isinstance(head, dict) for head in robot.head_samples) == heads_before
        with pytest.raises(RuntimeError, match="stale or replayed"):
            runtime.center_camera_control(session, 1)
    finally:
        release.set()
        actions.close()


def test_stop_between_center_stream_exit_and_admission_preserves_new_owner(monkeypatch):
    runtime, actions, robot, session = worker_runtime(monkeypatch)
    original_wait = actions.wait_idle
    entered, release = threading.Event(), threading.Event()
    errors = []

    def wait_idle(timeout):
        result = original_wait(timeout)
        entered.set()
        assert release.wait(2)
        return result

    monkeypatch.setattr(actions, "wait_idle", wait_idle)

    def center():
        try:
            runtime.center_camera_control(session, 1)
        except RuntimeError as exc:
            errors.append(str(exc))

    requester = threading.Thread(target=center)
    requester.start()
    try:
        assert entered.wait(2)
        runtime.invalidate_camera_control()
        new_session = runtime.start_camera_control(
            camera_feed_enabled=True, controls_enabled=True, adult_ui_unlocked=True,
        )["session_id"]
        new_stream = runtime._camera_control_stream
        release.set()
        requester.join(2)
        assert errors and not requester.is_alive()
        assert runtime._camera_control_session_id == new_session
        with pytest.raises(RuntimeError, match="session is not active"):
            runtime.center_camera_control(session, 2)
        assert new_stream is not None
        assert new_stream.snapshot().active
        assert robot.targets == []
    finally:
        release.set()
        runtime.invalidate_camera_control()
        actions.close()


@pytest.mark.parametrize("transition", ["stop", "privacy", "revoke", "end"])
def test_active_center_interpolation_stops_without_late_targets(monkeypatch, transition):
    runtime, actions, robot, session = worker_runtime(monkeypatch)
    moving = threading.Event()
    original_interpolate = actions._run_precision_interpolation
    errors = []

    def interpolate(**kwargs):
        original_body = robot.set_target_body_yaw

        def body_sample(value):
            original_body(value)
            moving.set()

        monkeypatch.setattr(robot, "set_target_body_yaw", body_sample)
        return original_interpolate(**kwargs)

    monkeypatch.setattr(actions, "_run_precision_interpolation", interpolate)

    def center():
        try:
            runtime.center_camera_control(session, 1)
        except RuntimeError as exc:
            errors.append(str(exc))

    requester = threading.Thread(target=center)
    requester.start()
    try:
        assert moving.wait(2)
        if transition == "stop":
            runtime.stop_manual_robot_action()
        elif transition == "privacy":
            with runtime._motor_transition_lock, runtime._kids_lock:
                runtime._privacy_requested.set()
                runtime.invalidate_camera_control()
                actions.cancel(stop_media=False)
        elif transition == "end":
            runtime.end_camera_control(session, 2)
        else:
            with runtime._motor_transition_lock, runtime._kids_lock:
                runtime.revoke_camera_control()
        assert actions.wait_idle(1)
        requester.join(2)
        assert errors and not requester.is_alive()
        samples = len(robot.body_samples)
        threading.Event().wait(0.08)
        assert len(robot.body_samples) == samples
    finally:
        runtime.invalidate_camera_control()
        requester.join(2)
        actions.close()


def test_timed_out_center_cannot_publish_deferred_interpolation(monkeypatch):
    runtime, actions, robot, session = worker_runtime(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr("homebody.manual_control._CAMERA_CENTER_TIMEOUT_SECONDS", 0.08)

    def delayed_pose(start, target, ratio):
        entered.set()
        assert release.wait(2)
        return target

    monkeypatch.setattr("homebody.robot_tools.interpolate_head_pose", delayed_pose)
    try:
        with pytest.raises(RuntimeError, match="did not complete in time"):
            runtime.center_camera_control(session, 1)
        assert entered.is_set()
        body_before = list(robot.body_samples)
        heads_before = len(robot.head_samples)
        release.set()
        assert actions.wait_idle(1)
        assert robot.body_samples == body_before
        assert len(robot.head_samples) == heads_before
    finally:
        release.set()
        actions.close()
