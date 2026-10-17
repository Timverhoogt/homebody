"""Runtime-level Look now handoff bounds: one queued request per conversation owner, dropped on
conversation end, expiry and ineligible sessions; plus its interaction with local face following.

Route-level reproduction lives in ``test_owner_show_tell_look_now.py``. Loopback fakes only.
"""

import pytest
from test_show_and_tell_runtime import make_runtime

from homebody import show_and_tell, show_and_tell_runtime
from homebody.kids_mode import KidsProfile


class TrackingMedia:
    def __init__(self, events):
        self.events = events

    def get_frame_jpeg(self):
        self.events.append(("capture", self.tracking[0]))
        return b"\xff\xd8synthetic-frame-not-a-child-image\xff\xd9"

    def push_audio_sample(self, _sample):
        self.events.append("pcm")


class TrackingRobot:
    def __init__(self, events):
        self.tracking = [False]
        self.media = TrackingMedia(events)
        self.media.tracking = self.tracking

    def cancel_move(self):
        pass

    def start_head_tracking(self, weight=1.0):
        self.tracking[0] = True

    def stop_head_tracking(self):
        self.tracking[0] = False


@pytest.fixture
def runtime(monkeypatch):
    events: list[object] = []
    saved = {"api_key": "test-key", "camera_enabled": True, "kids_show_and_tell_enabled": True,
             "face_tracking_enabled": True}
    monkeypatch.setattr(show_and_tell, "MIN_LOOK_INTERVAL", 0.0)
    runtime = make_runtime(monkeypatch, saved, events, {})
    runtime.robot = TrackingRobot(events)
    runtime._notify_bridge_kids_session_async = lambda *_a, **_k: None
    runtime._motors_enabled, runtime._head_safely_folded = True, False
    runtime.start_kids_mode(KidsProfile(duration_minutes=15, motion_enabled=True), greet=False)
    runtime._accept_wake_turn()
    runtime.events = events
    yield runtime
    runtime.stop_kids_mode(fold=False)


def test_conversation_end_and_ttl_drop_an_unconsumed_request(runtime, monkeypatch):
    with runtime._voice_activity_lock:
        key = runtime._show_tell_register_consumer(runtime._kids_session_id)
        assert key is not None
        assert runtime.show_tell_look_now()["queued"] is True
        assert runtime.show_tell_look_now()["queued"] is True
        assert runtime.status()["kids_mode"]["show_and_tell"]["queued"] is True
        runtime._show_tell_unregister_consumer(key)  # the conversation ended (silence, error, ...)
        assert runtime._show_tell_pending is None and not runtime._show_tell_take_pending(key)
        assert runtime.status()["kids_mode"]["show_and_tell"]["queued"] is False
        monkeypatch.setattr(show_and_tell_runtime, "_QUEUED_LOOK_TTL", 0.0)
        key = runtime._show_tell_register_consumer(runtime._kids_session_id)
        runtime.show_tell_look_now()
        assert not runtime._show_tell_take_pending(key), "an expired request is dropped"
        runtime._show_tell_unregister_consumer(key)
    assert not [item for item in runtime.events if isinstance(item, tuple) and item[0] in {"permit", "capture"}]


def test_stale_consumer_after_stop_or_replacement_cannot_queue(runtime):
    with runtime._voice_activity_lock:
        key = runtime._show_tell_register_consumer(runtime._kids_session_id)
        runtime._request_conversation_stop()  # the owning turn was stopped
        assert runtime._show_tell_queue_look() == ""
        runtime._show_tell_unregister_consumer(key)
        runtime._accept_wake_turn()
        key = runtime._show_tell_register_consumer(runtime._kids_session_id)
        runtime.start_kids_mode(KidsProfile(duration_minutes=15), greet=False)  # replacement
        runtime._accept_wake_turn()
        assert runtime._show_tell_queue_look() == ""
        assert not runtime._show_tell_take_pending(key)


def test_hosted_and_non_buddy_conversations_register_no_consumer(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "_admit_kids_session_voice", lambda _language: None)
    for profile in (KidsProfile(activity="story", duration_minutes=15),
                    KidsProfile(host=True, duration_minutes=15)):
        runtime.start_kids_mode(profile, greet=False)
        runtime._accept_wake_turn()
        assert runtime._show_tell_register_consumer(runtime._kids_session_id) is None


def test_queued_look_yields_face_following_only_for_the_capture(runtime):
    config = runtime._kids_voice_config(runtime.config_loader())
    with runtime._conversation_stop_generation_lock:
        stop_generation = runtime._turn_stop_generation
    lease = runtime._begin_face_follow(config, stop_generation)
    assert runtime.robot.tracking[0] is True, "Kids Chat follows under the saved Owner setting"
    with runtime._voice_activity_lock:
        key = runtime._show_tell_register_consumer(runtime._kids_session_id)
        assert runtime.show_tell_look_now()["queued"] is True
        assert runtime._show_tell_take_pending(key)
        assert runtime._show_tell_voice_turn() is True
        runtime._show_tell_unregister_consumer(key)
    captures = [item for item in runtime.events if isinstance(item, tuple) and item[0] == "capture"]
    assert captures == [("capture", False)], "the head held still (no tracker) for the one frame"
    assert runtime.robot.tracking[0] is True, "following resumed for the still-current turn"
    runtime.stop_kids_mode(fold=False)
    assert runtime.robot.tracking[0] is False
    runtime._end_face_follow(lease)
