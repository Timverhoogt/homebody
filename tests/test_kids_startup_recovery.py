"""Startup regressions: fake hardware only, including stale SDK IPC readers."""
from __future__ import annotations

import inspect
import threading
from types import SimpleNamespace

import pytest

from homebody.config import AppConfig
from homebody.ispy import validate_ispy_target
from homebody.kids_mode import KidsProfile
from homebody.runtime import HermesVoiceRuntime


class Camera:
    def __init__(self, ready=False):
        self.ready = ready
        self.camera_specs = object()
        self.closed = 0

    def close(self):
        self.closed += 1


class Media:
    backend = SimpleNamespace(value="local")

    def __init__(self, ready=True):
        self.camera = Camera(ready)
        self.reconnects = 0
        self.captures = 0
        self.reconnect_allowed = True

    def get_frame_jpeg(self):
        self.captures += 1
        return b"jpeg" if self.camera and self.camera.ready else None

    def _init_camera(self, log_level, camera_specs=None):
        self.reconnects += 1
        self.camera = Camera(self.reconnect_allowed)
        self.camera.camera_specs = camera_specs

    def start_recording(self):
        pass

    def stop_recording(self):
        pass

    def play_sound(self, _path):
        pass


class Robot:
    def __init__(self, ready=True):
        self.media = Media(ready)
        self.wakes = 0
        self.folds = 0
        self.targets = []
        self.unfolded = False
        self.waking = threading.Event()
        self.continue_wake = threading.Event()
        self.block_wake = False

    def wake_up(self):
        self.wakes += 1
        if self.block_wake and self.wakes == 1:
            self.waking.set()
            assert self.continue_wake.wait(2)
        self.unfolded = True

    def goto_sleep(self):
        self.folds += 1
        self.unfolded = False

    def goto_target(self, **kwargs):
        self.targets.append(kwargs)

    def cancel_move(self):
        pass


class Selector:
    def select_ispy_target(self, frames, **_kwargs):
        assert frames == [b"jpeg"] * 5
        return validate_ispy_target({
            "object_name": "chair", "colour": "blue", "category": "furniture",
            "location": "beside the table", "frame_index": 1, "bbox": [0.2, 0.2, 0.3, 0.4],
            "confidence": 0.91, "stable": True, "visible_frame_count": 3,
            "hints_en": ["You can sit on it"], "hints_nl": ["Je kunt erop zitten"],
        }, frame_count=5)

    def close(self):
        pass


@pytest.fixture
def make_runtime(monkeypatch):
    runtimes = []
    monkeypatch.setattr("homebody.power.httpx.post", lambda *_args, **_kwargs: SimpleNamespace(
        raise_for_status=lambda: None))
    monkeypatch.setattr("homebody.vision_runtime.time", SimpleNamespace(sleep=lambda _seconds: None))

    def make(*, ready=True):
        robot = Robot(ready)
        runtime = HermesVoiceRuntime(robot, threading.Event(), config_loader=lambda: AppConfig())
        runtime._audio_ready = True
        runtime._head_safely_folded = True
        runtime._motors_enabled = False
        monkeypatch.setattr(runtime, "_read_head_safely_folded", lambda: not robot.unfolded)
        monkeypatch.setattr(runtime, "_wake_pose_is_ready", lambda: robot.unfolded)
        monkeypatch.setattr(runtime, "_notify_bridge_kids_session_async", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(runtime, "_cancel_ispy_bridge_session", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(runtime, "_new_bridge_client", lambda _config: Selector())
        runtimes.append(runtime)
        return runtime, robot

    yield make
    for runtime in runtimes:
        runtime.stop_kids_mode(fold=False)


def test_repeated_awake_does_not_repeat_wake_motion(make_runtime):
    runtime, robot = make_runtime()
    runtime.set_power_mode("awake")
    runtime.set_power_mode("awake")
    runtime._apply_power_mode()
    assert robot.wakes == 1


def test_inflight_awake_and_stale_runtime_reconciliation_wake_only_once(make_runtime):
    runtime, robot = make_runtime()
    robot.block_wake = True
    failures = []

    def run(action):
        try:
            action()
        except Exception as exc:
            failures.append(exc)

    first = threading.Thread(target=lambda: run(lambda: runtime.set_power_mode("awake")))
    second = threading.Thread(target=lambda: run(runtime._apply_power_mode))
    first.start()
    assert robot.waking.wait(1)
    assert runtime._effective_power_mode() == "awake"
    assert runtime._status.power_mode == "standby"
    second.start()
    robot.continue_wake.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive() and not failures
    assert robot.wakes == 1


def test_camera_rebuilds_only_local_reader_once_and_preserves_specs(make_runtime):
    runtime, robot = make_runtime(ready=False)
    old = robot.media.camera
    assert runtime.camera_snapshot() == b"jpeg"
    assert old.closed == 1
    assert robot.media.reconnects == 1
    assert robot.media.camera.camera_specs is old.camera_specs
    assert robot.wakes == robot.folds == 0


def test_camera_never_rebuilds_remote_backend(make_runtime):
    runtime, robot = make_runtime(ready=False)
    robot.media.backend = SimpleNamespace(value="webrtc")
    with pytest.raises(RuntimeError, match="did not return a frame"):
        runtime.camera_snapshot()
    assert robot.media.reconnects == 0


def test_privacy_during_camera_rebuild_closes_replacement_without_capture(make_runtime, monkeypatch):
    runtime, robot = make_runtime(ready=False)
    rebuild = robot.media._init_camera
    before = []

    def enter_privacy(*args, **kwargs):
        before.append(robot.media.captures)
        rebuild(*args, **kwargs)
        runtime._privacy_requested.set()
        runtime._power_mode = "sleep"

    monkeypatch.setattr(robot.media, "_init_camera", enter_privacy)
    with pytest.raises(RuntimeError, match="privacy"):
        runtime.camera_snapshot()
    assert robot.media.captures == before[0]
    assert robot.media.camera is None or robot.media.camera.closed == 1
    assert robot.wakes == 0


def test_failed_preflight_does_not_wake_move_or_fold(make_runtime):
    runtime, robot = make_runtime(ready=False)
    robot.media.reconnect_allowed = False
    try:
        with pytest.raises(RuntimeError, match="did not return a frame"):
            runtime.start_kids_mode(KidsProfile(activity="ispy", camera_consent=True), greet=False)
        assert robot.wakes == robot.folds == 0
        assert robot.targets == []
        assert runtime._effective_power_mode() == "standby"
        assert runtime.status()["kids_mode"]["active"] is False
        assert robot.media.reconnects == 1
    finally:
        runtime.stop_kids_mode(fold=False)


def test_ispy_camera_ready_before_wake_and_no_extra_wake_while_awake(make_runtime):
    runtime, robot = make_runtime()
    runtime.set_power_mode("awake")
    seen = []
    wake = runtime.set_power_mode

    def awake(*args, **kwargs):
        seen.append(robot.media.captures)
        return wake(*args, **kwargs)

    runtime.set_power_mode = awake
    try:
        runtime.start_kids_mode(KidsProfile(activity="ispy", camera_consent=True), greet=False)
        assert seen == [1]
        assert robot.wakes == 1
        assert robot.media.captures == 6
        assert len(robot.targets) == 9
    finally:
        runtime.stop_kids_mode(fold=False)


def test_stop_during_preflight_cannot_wake_or_start_search(make_runtime, monkeypatch):
    runtime, robot = make_runtime()

    def stop():
        runtime.stop_kids_mode(reason="parent", fold=False)
        return b"late-jpeg"

    monkeypatch.setattr(robot.media, "get_frame_jpeg", stop)
    with pytest.raises(RuntimeError, match="cancelled"):
        runtime.start_kids_mode(KidsProfile(activity="ispy", camera_consent=True), greet=False)
    assert robot.wakes == 0 and robot.targets == []
    assert runtime.status()["kids_mode"]["active"] is False


def test_ispy_consent_required_before_capture_or_movement(make_runtime):
    runtime, robot = make_runtime()
    with pytest.raises((RuntimeError, ValueError), match="Camera opt-in"):
        runtime.start_kids_mode(KidsProfile(activity="ispy", camera_consent=False), greet=False)
    assert robot.media.captures == 0
    assert robot.wakes == robot.folds == 0


def test_repeat_wake_rechecks_physical_pose_instead_of_cached_app_state(make_runtime):
    runtime, robot = make_runtime()
    runtime.set_power_mode("awake")
    robot.unfolded = False  # external hardware reset: cached motor/app state is insufficient
    runtime.set_power_mode("awake")
    assert robot.wakes == 2


# Independent safety-review regressions, retained in the normal suite.
def test_failed_wake_must_not_continue_camera_search(make_runtime):
    runtime, robot = make_runtime()

    def failing_wake():
        robot.wakes += 1
        raise RuntimeError('fake wake blocked')

    robot.wake_up = failing_wake
    error = None
    try:
        runtime.start_kids_mode(KidsProfile(activity='ispy', camera_consent=True), greet=False)
    except RuntimeError as exc:
        error = str(exc)
    print({'probe': 'wake failure', 'raised': error, 'targets': len(robot.targets),
           'power': runtime._effective_power_mode(), 'motors': runtime._motors_enabled,
           'active': runtime._kids_active, 'internal_error': runtime._status.last_error})
    assert error is not None, 'Failed wake was hidden by locked public status and search continued'
    assert robot.targets == []


def test_failed_greeting_after_search_must_fold(make_runtime):
    runtime, robot = make_runtime()
    # The real queue method rejects the absent announcement worker, not a fake exception.
    with pytest.raises(RuntimeError, match='Announcement audio is not ready'):
        runtime.start_kids_mode(KidsProfile(activity='ispy', camera_consent=True))
    print({'probe': 'greeting failure', 'targets': len(robot.targets), 'folds': robot.folds,
           'power': runtime._effective_power_mode(), 'motors': runtime._motors_enabled,
           'folded': runtime._head_safely_folded, 'active': runtime._kids_active})
    assert runtime._motors_enabled is False and runtime._head_safely_folded


def test_shutdown_during_wake_must_not_command_search(make_runtime):
    runtime, robot = make_runtime()
    wake = robot.wake_up

    def shutdown_during_wake():
        wake()
        runtime.stop_event.set()

    robot.wake_up = shutdown_during_wake
    with pytest.raises(RuntimeError, match='cancelled'):
        runtime.start_kids_mode(KidsProfile(activity='ispy', camera_consent=True), greet=False)
    print({'probe': 'shutdown during wake', 'targets_after_stop': len(robot.targets),
           'folds': robot.folds, 'motors': runtime._motors_enabled})
    assert robot.targets == [], 'Search movement was issued after shutdown won'


def test_queued_sleep_must_interrupt_search_and_prevent_selection(make_runtime, monkeypatch):
    runtime, robot = make_runtime()
    privacy_requested = threading.Event()
    sleep_finished = threading.Event()
    transition_errors = []
    sleep_thread = None
    cancellations = runtime._cancel_announcements
    goto_target = robot.goto_target
    selection_states = []

    def cancel_announcements(**kwargs):
        result = cancellations(**kwargs)
        if threading.current_thread().name == 'review-sleep':
            privacy_requested.set()  # Actual Sleep caller reached the pre-motor-lock boundary.
        return result

    monkeypatch.setattr(runtime, '_cancel_announcements', cancel_announcements)

    def sleep():
        try:
            runtime.set_power_mode('sleep')
        except Exception as exc:
            transition_errors.append(repr(exc))
        finally:
            sleep_finished.set()

    def target(**kwargs):
        nonlocal sleep_thread
        goto_target(**kwargs)
        if sleep_thread is None:
            sleep_thread = threading.Thread(target=sleep, name='review-sleep')
            sleep_thread.start()
            assert privacy_requested.wait(2)

    robot.goto_target = target

    class ObservedSelector(Selector):
        def select_ispy_target(self, frames, **kwargs):
            selection_states.append(runtime._effective_power_mode())
            return super().select_ispy_target(frames, **kwargs)

    def selector(_config):
        # The existing search has released its motor lock before opening its selector.
        assert sleep_finished.wait(2)
        return ObservedSelector()

    monkeypatch.setattr(runtime, '_new_bridge_client', selector)
    error = None
    try:
        runtime.start_kids_mode(KidsProfile(activity='ispy', camera_consent=True), greet=False)
    except RuntimeError as exc:
        error = str(exc)
    finally:
        if sleep_thread is not None:
            sleep_thread.join(2)
            assert not sleep_thread.is_alive()
    print({'probe': 'Sleep during search', 'raised': error, 'targets': len(robot.targets),
           'captures': robot.media.captures, 'selected_in_modes': selection_states,
           'transition_errors': transition_errors})
    assert error is not None and selection_states == [], 'Privacy failed to revoke capture/selection'


def test_stale_start_failure_must_not_stop_replacement(make_runtime, monkeypatch):
    runtime, robot = make_runtime(ready=False)
    robot.media.reconnect_allowed = False
    checked_old_generation = threading.Event()
    release_old_failure = threading.Event()
    is_current = runtime._kids_callback_is_current
    errors = []

    def delayed_current(generation):
        result = is_current(generation)
        caller = inspect.currentframe().f_back.f_code.co_name
        if threading.current_thread().name == 'review-old-start' and caller == 'start_kids_mode':
            checked_old_generation.set()
            assert release_old_failure.wait(2)
        return result

    monkeypatch.setattr(runtime, '_kids_callback_is_current', delayed_current)

    def old_start():
        try:
            runtime.start_kids_mode(KidsProfile(activity='ispy', camera_consent=True), greet=False)
        except Exception as exc:
            errors.append(str(exc))

    old_thread = threading.Thread(target=old_start, name='review-old-start')
    old_thread.start()
    try:
        assert checked_old_generation.wait(2)
        runtime.start_kids_mode(KidsProfile(activity='story'), greet=False)
        replacement_id = runtime._kids_session_id
    finally:
        release_old_failure.set()
        old_thread.join(2)
        assert not old_thread.is_alive()
    print({'probe': 'stale failure', 'replacement_still_active': runtime._kids_session_is_current(replacement_id),
           'last_end_reason': runtime._kids_last_end_reason, 'old_error': errors})
    assert runtime._kids_session_is_current(replacement_id), 'Old failure stopped the new child session'


@pytest.mark.parametrize(('control_mode', 'z', 'pitch', 'ready'), [
    ('enabled', 0.0, 0.0, True),
    ('disabled', 0.0, 0.0, False),
    ('enabled', -0.04, 0.30, False),
    ('enabled', float('nan'), 0.0, False),
])
def test_real_readiness_helper_requires_measured_enabled_unfolded_finite_pose(
        make_runtime, monkeypatch, control_mode, z, pitch, ready):
    from homebody.power import PowerMixin
    runtime, _robot = make_runtime()
    monkeypatch.setattr('homebody.power.httpx.get', lambda *_args, **_kwargs: SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {'control_mode': control_mode, 'head_pose': {'z': z, 'pitch': pitch}},
    ))
    assert PowerMixin._wake_pose_is_ready(runtime) is ready


def test_reader_rebuild_keeps_audio_and_never_releases_daemon_media(make_runtime):
    runtime, robot = make_runtime(ready=False)
    audio = object()
    robot.media.audio = audio
    robot.media.release = lambda: (_ for _ in ()).throw(AssertionError('Daemon media release forbidden'))
    assert runtime.camera_snapshot() == b'jpeg'
    assert robot.media.audio is audio
    assert robot.media.reconnects == 1


def test_search_frame_failure_after_motion_folds_safely(make_runtime):
    runtime, robot = make_runtime()
    goto_target = robot.goto_target

    def kill_frames_after_motion(**kwargs):
        goto_target(**kwargs)
        robot.media.camera.ready = False
        robot.media.reconnect_allowed = False

    robot.goto_target = kill_frames_after_motion
    with pytest.raises(RuntimeError, match='did not return a frame'):
        runtime.start_kids_mode(KidsProfile(activity='ispy', camera_consent=True), greet=False)
    assert robot.wakes == 1 and robot.folds == 1
    assert runtime._head_safely_folded and runtime._motors_enabled is False
