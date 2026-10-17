"""Local face following in Kids Mode: the saved Owner setting, one lease per admitted turn, yields to
exclusive motion/camera owners, and immediate stops that stale callbacks cannot undo.

The daemon-local tracker is faked at the SDK edge (start/stop_head_tracking); no robot, camera,
daemon or network is contacted and no face data exists anywhere. NOT physical acceptance.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from test_kids_end_single_fold import Rig
from test_show_and_tell_runtime import make_runtime

from homebody.config import AppConfig
from homebody.kids_mode import KidsProfile
from homebody.kids_runtime import ISpyInputUnavailable


class Media:
    def get_frame_jpeg(self):
        return b"\xff\xd8x\xff\xd9"

    def push_audio_sample(self, _sample):
        pass

    def start_recording(self):
        pass

    def stop_recording(self):
        pass

    def stop_playing(self):
        pass


class TrackingRobot:
    def __init__(self):
        self.media = Media()
        self.tracking = False
        self.starts = 0
        self.moves: list[bool] = []

    def cancel_move(self):
        pass

    def start_head_tracking(self, weight=1.0):
        self.tracking = True
        self.starts += 1

    def stop_head_tracking(self):
        self.tracking = False

    def goto_target(self, **_kwargs):
        self.moves.append(self.tracking)  # the tracker must never command the head during a gesture


@pytest.fixture
def saved():
    # Camera access for AI is off: local following is a separate, daemon-local permission.
    return {"api_key": "test-key", "camera_enabled": False, "face_tracking_enabled": True}


@pytest.fixture
def runtime(monkeypatch, saved):
    runtime = make_runtime(monkeypatch, saved, [], {})
    runtime.robot = TrackingRobot()
    runtime._notify_bridge_kids_session_async = lambda *_a, **_k: None
    runtime._admit_kids_session_voice = lambda _language: None
    runtime._prepare_owned_ispy_search = lambda *_args: None
    runtime._motors_enabled, runtime._head_safely_folded = True, False
    yield runtime
    runtime.stop_kids_mode(fold=False)


def begin(runtime, profile):
    runtime.start_kids_mode(profile, greet=False)
    runtime._accept_wake_turn()
    config = runtime._kids_voice_config(runtime.config_loader())
    with runtime._conversation_stop_generation_lock:
        stop_generation = runtime._turn_stop_generation
    return config, runtime._begin_face_follow(config, stop_generation)


PROFILES = [
    KidsProfile(activity="buddy", duration_minutes=15),
    KidsProfile(activity="story", duration_minutes=15),
    KidsProfile(activity="quiz", duration_minutes=15),
    KidsProfile(activity="riddles", duration_minutes=15),
    KidsProfile(activity="calm", duration_minutes=15),
    KidsProfile(activity="ispy", camera_consent=True, duration_minutes=15),
    KidsProfile(host=True, duration_minutes=15),
    KidsProfile(host=True, camera_consent=True, duration_minutes=15),
    KidsProfile(language="es", duration_minutes=15),
    KidsProfile(age_band="4-6", activity="story", duration_minutes=15),
]


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: f"{p.activity}-host{p.host}-{p.language}-{p.age_band}")
def test_every_kids_activity_follows_under_the_saved_setting_without_unlocking_camera_or_tools(runtime, profile):
    config, lease = begin(runtime, profile)
    assert config.face_tracking_enabled is True
    assert not config.camera_enabled and not config.camera_feed_enabled and not config.doa_enabled
    assert not (config.robot_tools_enabled or config.agent_tools_enabled or config.power_tools_enabled)
    assert runtime.robot.tracking is True and runtime.robot.starts == 1
    status = runtime.status()
    assert status["kids_mode"]["camera_enabled"] is False  # AI-camera disclosure is unchanged
    runtime._end_face_follow(lease)
    assert runtime.robot.tracking is False


@pytest.mark.parametrize(("setting", "motion"), [(False, True), (True, False), (False, False)])
def test_saved_off_or_kids_motion_off_never_follows(runtime, saved, setting, motion):
    saved["face_tracking_enabled"] = setting
    config, _lease = begin(runtime, KidsProfile(duration_minutes=15, motion_enabled=motion))
    assert config.face_tracking_enabled is False
    runtime._sync_face_follow()
    runtime._after_robot_action()
    assert runtime.robot.starts == 0 and runtime.robot.tracking is False


def _stop_event(runtime, event, monkeypatch):
    if event == "stop":
        runtime._request_conversation_stop()
    elif event == "end":
        runtime.stop_kids_mode(fold=False)
    elif event == "expiry":
        with runtime._kids_lock:
            generation = runtime._kids_generation
        runtime._expire_kids_mode(generation)
    elif event == "privacy":
        runtime._privacy_requested.set()
        runtime._request_conversation_stop()
    elif event == "sleep":
        monkeypatch.setattr(runtime, "_fold_head_before_torque_release", lambda: (True, ""))
        runtime._power_mode = "sleep"
        runtime._apply_power_mode()
    elif event == "standby_fold":
        monkeypatch.setattr("homebody.power.httpx.post",
                            lambda url, **_k: httpx.Response(200, request=httpx.Request("POST", url)))
        runtime._run_native_sleep = lambda: None
        runtime._read_head_safely_folded = lambda: True
        assert runtime._fold_head_before_torque_release() == (True, "")
    elif event == "disconnect":
        runtime._motor_backend_unavailable(RuntimeError("unplugged"))
    elif event == "replacement":
        runtime.start_kids_mode(KidsProfile(duration_minutes=15), greet=False)
        runtime._accept_wake_turn()  # even a fresh turn cannot donate authority to the old lease
    elif event == "expired_clock":
        with runtime._kids_lock:
            runtime._kids_ends_at = 0.0


@pytest.mark.parametrize("event", ["stop", "end", "expiry", "privacy", "sleep", "standby_fold", "disconnect",
                                   "replacement", "expired_clock"])
def test_every_stop_path_stops_now_and_stale_callbacks_cannot_restart(runtime, monkeypatch, event):
    _config, _lease = begin(runtime, KidsProfile(duration_minutes=15))
    assert runtime.robot.tracking is True
    _stop_event(runtime, event, monkeypatch)
    if event == "expired_clock":
        runtime._sync_face_follow()  # the next boundary sees the deadline
    assert runtime.robot.tracking is False
    starts = runtime.robot.starts
    runtime._after_robot_action()  # a late action callback
    runtime._resume_face_follow("robot_action")
    runtime._resume_face_follow("ispy_guess")
    runtime._sync_face_follow()
    assert runtime.robot.tracking is False and runtime.robot.starts == starts
    runtime._privacy_requested.clear()


def test_hosted_game_switch_invalidates_the_old_lease(runtime):
    _config, _lease = begin(runtime, KidsProfile(host=True, duration_minutes=15))
    runtime.queue_announcement = lambda *_a, **_k: {"queued": True}  # no announcement worker here
    with runtime._kids_lock:
        generation = runtime._kids_generation
    runtime._kids_host_transition("story", generation)
    assert runtime.robot.tracking is False
    runtime._sync_face_follow()
    assert runtime.robot.tracking is False


def test_ispy_search_owns_head_and_camera_then_following_resumes(runtime, monkeypatch):
    profile = KidsProfile(activity="ispy", camera_consent=True, duration_minutes=15)
    _config, _lease = begin(runtime, profile)
    seen = []

    def scan(*_args, **_kwargs):
        seen.append(runtime.robot.tracking)
        with runtime._kids_lock:
            runtime._kids_camera_active = True
        runtime._sync_face_follow()  # a concurrent sync during the search must not restart it
        seen.append(runtime.robot.tracking)
        with runtime._kids_lock:
            runtime._kids_camera_active = False
        return None

    monkeypatch.setattr(runtime, "_scan_ispy_scene_owned", scan)
    with runtime._kids_lock:
        generation = runtime._kids_generation
    runtime._scan_ispy_scene(generation, profile)
    assert seen == [False, False]
    assert runtime.robot.tracking is True


def test_ispy_guess_gesture_holds_until_the_next_listen(runtime):
    _config, _lease = begin(runtime, KidsProfile(activity="ispy", camera_consent=True, duration_minutes=15))
    with runtime._kids_lock:
        generation = runtime._kids_generation
    runtime._perform_ispy_player_guess_motion(generation)
    assert runtime.robot.moves == [False]
    runtime._after_robot_action()
    assert runtime.robot.tracking is False, "the spoken guess keeps its pose"
    runtime._resume_face_follow("ispy_guess")  # what the conversation does before listening again
    assert runtime.robot.tracking is True


def test_manual_control_and_robot_actions_yield(runtime, monkeypatch):
    _config, _lease = begin(runtime, KidsProfile(duration_minutes=15))
    runtime._yield_face_follow("robot_action")
    assert runtime.robot.tracking is False
    runtime._after_robot_action()
    assert runtime.robot.tracking is True
    monkeypatch.setattr(runtime, "_camera_control_session_live_locked", lambda: True)
    runtime._sync_face_follow()
    assert runtime.robot.tracking is False


def test_stop_between_authority_sample_and_apply_wins(runtime, monkeypatch):
    _config, _lease = begin(runtime, KidsProfile(duration_minutes=15))
    runtime._yield_face_follow("robot_action")
    original = runtime._face_follow_eligible

    def racing(lease):
        allowed = original(lease)
        runtime.stop_kids_mode(fold=False)  # End lands after the sample, before the apply
        return allowed

    monkeypatch.setattr(runtime, "_face_follow_eligible", racing)
    runtime._resume_face_follow("robot_action")
    assert runtime.robot.tracking is False and runtime.robot.starts == 1


def test_wake_loop_follows_during_the_kids_turn_and_never_during_the_end_fold(monkeypatch):
    rig = Rig(monkeypatch, wakes_at={1: "hey"})
    runtime = rig.runtime
    runtime.config_loader = lambda: AppConfig(
        api_key="test-key", wake_cooldown_seconds=0.0, face_tracking_enabled=True,
    )
    tracker = {"on": False, "starts": 0}

    def start(weight=1.0):
        tracker["on"], tracker["starts"] = True, tracker["starts"] + 1

    rig.robot.start_head_tracking = start
    rig.robot.stop_head_tracking = lambda: tracker.update(on=False)
    during_turn, during_sleep = [], []
    conversation = runtime._run_selected_voice_conversation

    def observed(config, keyword):
        during_turn.append(tracker["on"])
        conversation(config, keyword)

    runtime._run_selected_voice_conversation = observed
    native_sleep = runtime._run_native_sleep

    def observed_sleep():
        during_sleep.append(tracker["on"])
        native_sleep()

    runtime._run_native_sleep = observed_sleep
    rig.start_kids()
    rig.thread.start()
    assert rig.in_turn.wait(5)
    runtime.stop_kids_mode(reason="parent")
    assert tracker["on"] is False
    rig.finish()

    assert during_turn == [True], "Kids Chat follows during its active turn"
    assert during_sleep == [False], "the safety fold is the single motion owner"
    assert tracker["starts"] == 1 and tracker["on"] is False
    assert rig.daemon.sleeps == 1


# --- Announcement lines (greeting, I Spy clue, host lobby) through the real worker -------------


def _start_worker(runtime, monkeypatch, on_play):
    played: list[tuple[str, bool]] = []

    def play(item, _speech, text):
        played.append((text, runtime.robot.tracking))
        on_play(item)

    monkeypatch.setattr(runtime, "_synthesize_kids_fixed_speech", lambda _client, _item: SimpleNamespace(provider="x"))
    monkeypatch.setattr(runtime, "_play_announcement_audio", play)
    worker = threading.Thread(target=runtime._run_announcement_worker, name="test-announcements", daemon=True)
    runtime._announcement_worker = worker
    worker.start()
    return worker, played


def _wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.005)


LINE_PROFILES = [
    ("greeting", KidsProfile(activity="story", duration_minutes=15)),
    ("ispy_clue", KidsProfile(activity="ispy", camera_consent=True, duration_minutes=15)),
    ("host_lobby", KidsProfile(host=True, duration_minutes=15)),
]


@pytest.mark.parametrize(("line", "profile"), LINE_PROFILES, ids=[name for name, _ in LINE_PROFILES])
def test_kids_lines_follow_while_they_play_and_hand_back_cleanly(runtime, monkeypatch, line, profile):
    target = SimpleNamespace(clue=lambda _language: "I spy something blue.")
    runtime._prepare_owned_ispy_search = lambda *_args: target
    worker, played = _start_worker(runtime, monkeypatch, lambda _item: None)
    runtime.start_kids_mode(profile, greet=True)
    _wait_for(lambda: played and runtime._kids_first_listen is not None)

    assert [tracking for _text, tracking in played] == [True], f"{line} follows during the line"
    if line == "ispy_clue":
        assert played[0][0] == "I spy something blue."
    assert runtime.robot.tracking is False and runtime._face_follow_lease is None  # ended with the line
    runtime.stop_event.set()
    worker.join(3)


def test_end_during_a_line_stops_following_and_the_line_lease_cannot_resume(runtime, monkeypatch):
    starts: list[int] = []

    def end_mid_line(_item):
        runtime.stop_kids_mode(fold=False)
        starts.append(runtime.robot.starts)
        assert runtime.robot.tracking is False  # immediately, while the line's lease still exists
        runtime._after_robot_action()
        runtime._sync_face_follow()
        assert runtime.robot.tracking is False

    worker, played = _start_worker(runtime, monkeypatch, end_mid_line)
    runtime.start_kids_mode(KidsProfile(duration_minutes=15), greet=True)
    _wait_for(lambda: starts and not runtime._announcement_active.is_set())

    assert played and played[0][1] is True
    assert runtime.robot.starts == starts[0] == 1 and runtime.robot.tracking is False
    assert runtime._kids_first_listen is None, "an ended session never arms a listen"
    runtime.stop_event.set()
    worker.join(3)


def test_saved_off_line_never_follows(runtime, saved, monkeypatch):
    saved["face_tracking_enabled"] = False
    worker, played = _start_worker(runtime, monkeypatch, lambda _item: None)
    runtime.start_kids_mode(KidsProfile(duration_minutes=15), greet=True)
    _wait_for(lambda: played and not runtime._announcement_active.is_set())
    assert played[0][1] is False and runtime.robot.starts == 0
    runtime.stop_event.set()
    worker.join(3)


# --- Typed I Spy turn ---------------------------------------------------------------------------


def _typed_rig(runtime, monkeypatch, *, motion=True, during=None):
    profile = KidsProfile(activity="ispy", camera_consent=True, duration_minutes=15, motion_enabled=motion)
    runtime.start_kids_mode(profile, greet=False)
    seen: list[bool] = []

    def typed_turn(config, *, typed_input):
        assert typed_input == "a cup"
        seen.append(runtime.robot.tracking)
        if during is not None:
            during()
        seen.append(runtime.robot.tracking)

    monkeypatch.setattr(runtime, "_run_conversation", typed_turn)
    token = runtime.ispy_input_context()["token"]
    assert token
    return token, seen


def test_typed_ispy_turn_follows_only_for_its_own_turn(runtime, monkeypatch):
    token, seen = _typed_rig(runtime, monkeypatch)
    assert runtime.submit_ispy_guess("a cup", token) == {"accepted": True}
    assert seen == [True, True]
    assert runtime.robot.tracking is False and runtime._face_follow_lease is None
    runtime._after_robot_action()  # a late callback after the typed turn
    assert runtime.robot.tracking is False and runtime.robot.starts == 1


def test_typed_ispy_turn_stopped_mid_reply_stays_off(runtime, monkeypatch):
    def stop_mid_reply():
        runtime._request_conversation_stop()
        runtime._resume_face_follow("ispy_guess")
        runtime._sync_face_follow()

    token, seen = _typed_rig(runtime, monkeypatch, during=stop_mid_reply)
    with pytest.raises(ISpyInputUnavailable):
        runtime.submit_ispy_guess("a cup", token)  # the typed turn truthfully reports it ended
    assert seen == [True, False] and runtime.robot.starts == 1 and runtime.robot.tracking is False


def test_typed_ispy_turn_with_kids_motion_off_never_follows(runtime, monkeypatch):
    token, seen = _typed_rig(runtime, monkeypatch, motion=False)
    runtime.submit_ispy_guess("a cup", token)
    assert seen == [False, False] and runtime.robot.starts == 0
