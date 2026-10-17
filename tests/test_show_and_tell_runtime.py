"""Synthetic runtime integration: real HermesVoiceRuntime gate, Owner permission, camera fence,
Kids PCM path and the real pipeline conversation loop.

Loopback fakes only. No robot, camera, speaker, microphone, bridge or provider is contacted;
this is NOT physical acceptance.
"""

import threading
import time

import numpy as np
import pytest

from homebody import show_and_tell
from homebody.audio import EndpointResult
from homebody.config import AppConfig
from homebody.ispy import ISpyCancelled
from homebody.kids_mode import KidsProfile
from homebody.runtime import HermesVoiceRuntime
from homebody.show_and_tell_runtime import ShowAndTellUnavailable

FRAME = b"\xff\xd8synthetic-frame-not-a-child-image\xff\xd9"
RESULT = "I saw a drawing with blue. What would you like to tell me about it?"


class Media:
    def __init__(self, events, hooks):
        self.events, self.hooks = events, hooks

    def get_frame_jpeg(self):
        self.events.append("capture")
        self.hooks.get("capture", lambda: None)()
        return FRAME

    def push_audio_sample(self, _sample):
        self.events.append("pcm")


class Robot:
    def __init__(self, events, hooks):
        self.media = Media(events, hooks)

    def cancel_move(self):
        pass


class Bridge:
    """Loopback stand-in for HermesBridgeClient's Show and Tell, Kids chat and exact speech routes."""

    last_tts_provider = "fake"
    last_stt_provider = "fake"

    def __init__(self, events, hooks, config):
        self.events, self.hooks, self.config = events, hooks, config

    def close(self):
        pass

    def health(self):
        return {"status": "ok"}

    def set_kids_session_state(self, *_args, **_kwargs):
        pass

    def transcribe(self, _audio, **_kwargs):
        heard = self.hooks["transcripts"].pop(0) if self.hooks.get("transcripts") else ""
        self.events.append(("heard", heard))
        return heard

    def chat(self, text):
        self.events.append(("chat", text))
        return "What a fun idea!"

    def show_tell_permit(self, grant, serial, ttl_seconds):
        assert len(grant) == 64 and serial >= 1 and 1 <= ttl_seconds <= 3600
        self.events.append(("permit", serial))
        self.hooks.get("permit", lambda: None)()
        return "permit-token", "LOOKING"

    def show_tell_look(self, permit, frame):
        assert permit == "permit-token" and frame == FRAME
        self.events.append("look")
        self.hooks.get("look", lambda: None)()
        return RESULT, True

    def show_tell_cancel(self):
        self.events.append("cancel")

    def show_tell_revoke(self, serial):
        self.events.append(("revoke", serial))

    def iter_kids_speech(self, text, *, should_stop=None):
        self.events.append(("speak", text))
        self.hooks.get(("speak", text), lambda: None)()
        yield b"\x00\x10" * 240


@pytest.fixture
def saved():
    """The Owner's saved settings as the runtime's config loader sees them."""
    return {"api_key": "test-key", "camera_enabled": True, "kids_show_and_tell_enabled": True}


def make_runtime(monkeypatch, saved, events, hooks):
    runtime = HermesVoiceRuntime(Robot(events, hooks), threading.Event(), config_loader=lambda: AppConfig(**saved))
    runtime._audio_ready = True
    runtime._power_mode = "awake"
    runtime._output_sample_rate = 24000
    monkeypatch.setattr("homebody.runtime.HermesBridgeClient", lambda config: Bridge(events, hooks, config))
    monkeypatch.setattr(runtime, "set_power_mode", lambda *_args, **_kwargs: None)  # no motors here
    runtime._play_asset = lambda _name: None
    runtime._discard_audio = lambda _seconds: None
    return runtime


@pytest.fixture
def rig(monkeypatch, saved):
    events, hooks = [], {}
    monkeypatch.setattr(show_and_tell, "MIN_LOOK_INTERVAL", 0.0)  # the rate limit has its own core test

    def never_listen(*_args, **_kwargs):
        raise AssertionError("Show and Tell must never open the microphone")

    runtime = make_runtime(monkeypatch, saved, events, hooks)
    monkeypatch.setattr(runtime, "_record_utterance", never_listen)
    runtime.start_kids_mode(KidsProfile(duration_minutes=15), greet=False)
    yield runtime, events, hooks
    runtime._privacy_requested.clear()
    runtime.stop_kids_mode(fold=False)


def wait_idle(runtime):
    deadline = time.monotonic() + 5
    while runtime._voice_activity_lock.locked():
        assert time.monotonic() < deadline, "look did not finish"
        time.sleep(0.01)


def wait_for(predicate, seconds=3.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def state(runtime):
    return runtime.status()["kids_mode"]["show_and_tell"]


def look_now(runtime):
    assert runtime.show_tell_look_now()["accepted"] is True
    wait_idle(runtime)


def test_startup_and_enabling_never_capture_wake_or_listen(rig, saved):
    runtime, events, _ = rig
    assert state(runtime) == {
        "camera_approved": True, "enabled": True, "session": True, "available": True, "looking": False,
        "queued": False,
    }
    saved["kids_show_and_tell_enabled"] = False
    runtime.show_tell_sync_permission(revoke=True)
    saved["kids_show_and_tell_enabled"] = True
    runtime.show_tell_sync_permission()
    runtime.start_kids_mode(KidsProfile(duration_minutes=15), greet=False)
    for _ in range(3):
        runtime.status()
    assert not [item for item in events if item in ("capture", "pcm", "look") or isinstance(item, tuple)]
    assert runtime._kids_session_id not in repr(runtime.status())


@pytest.mark.parametrize("settings", [
    {"camera_enabled": False},  # existing camera-off stays off, even with the Show and Tell default
    {"camera_enabled": False, "kids_show_and_tell_enabled": True},
    {"kids_show_and_tell_enabled": False},  # persistent opt-out
])
def test_owner_camera_off_or_opt_out_disables_everything(rig, saved, settings):
    runtime, events, _ = rig
    saved.update(settings)
    runtime.show_tell_sync_permission(revoke=True)
    assert state(runtime)["available"] is False
    with pytest.raises(ShowAndTellUnavailable):
        runtime.show_tell_look_now()
    config = runtime._kids_voice_config(runtime.config_loader())
    assert not runtime._show_tell_requested(config, "Look at my drawing!")
    assert "capture" not in events and not [item for item in events if item[0] == "permit"]


def test_fresh_install_defaults_keep_show_and_tell_disabled(monkeypatch):
    events, hooks = [], {}
    runtime = HermesVoiceRuntime(Robot(events, hooks), threading.Event(), config_loader=lambda: AppConfig())
    runtime._audio_ready = True
    monkeypatch.setattr(runtime, "_new_bridge_client", lambda config: Bridge(events, hooks, config))
    runtime.start_kids_mode(KidsProfile(duration_minutes=15), greet=False)
    try:
        assert AppConfig().camera_enabled is False and AppConfig().kids_show_and_tell_enabled is True
        assert state(runtime) == {
            "camera_approved": False, "enabled": True, "session": True, "available": False, "looking": False,
            "queued": False,
        }
    finally:
        runtime.stop_kids_mode(fold=False)


def test_opt_out_persists_into_a_new_runtime_instance(monkeypatch, saved):
    saved["kids_show_and_tell_enabled"] = False  # what the Owner route saved before a restart
    events, hooks = [], {}
    runtime = make_runtime(monkeypatch, saved, events, hooks)
    runtime.start_kids_mode(KidsProfile(duration_minutes=15), greet=False)
    try:
        assert state(runtime)["enabled"] is False and state(runtime)["available"] is False
    finally:
        runtime.stop_kids_mode(fold=False)


@pytest.mark.parametrize("profile", [
    KidsProfile(activity="ispy", camera_consent=True, duration_minutes=15),
    KidsProfile(activity="story", duration_minutes=15),
    KidsProfile(host=True, camera_consent=True, duration_minutes=15),
    KidsProfile(language="es", duration_minutes=15),
])
def test_other_activities_hosts_and_languages_stay_denied(rig, monkeypatch, profile):
    runtime, _, _ = rig
    monkeypatch.setattr(runtime, "_admit_kids_session_voice", lambda _language: None)
    monkeypatch.setattr(runtime, "_prepare_owned_ispy_search", lambda *_args: None)
    runtime.start_kids_mode(profile, greet=False)
    assert state(runtime)["available"] is False
    with pytest.raises(ShowAndTellUnavailable):
        runtime.show_tell_look_now()


def test_ispy_consent_never_admits_show_and_tell_capture(rig, monkeypatch):
    runtime, _, _ = rig
    monkeypatch.setattr(runtime, "_prepare_owned_ispy_search", lambda *_args: None)
    runtime.start_kids_mode(KidsProfile(activity="ispy", camera_consent=True, duration_minutes=15), greet=False)
    with runtime._kids_lock:
        generation = runtime._kids_generation
        runtime._kids_camera_active = True
    with pytest.raises(ISpyCancelled):
        runtime._capture_camera_jpeg(kids_generation=generation, show_tell=lambda: None)


def test_owner_look_now_needs_no_session_approval_and_never_listens(rig):
    runtime, events, _ = rig
    look_now(runtime)
    assert events.count("capture") == 1 and events.count("look") == 1
    assert events.index(("speak", "LOOKING")) < events.index("pcm") < events.index("capture")
    assert events.index("capture") < events.index("look") < events.index(("speak", RESULT))
    assert "pcm" in events[events.index(("speak", RESULT)):]
    assert "cancel" not in events
    assert runtime._kids_camera_active is False
    assert state(runtime)["available"] is True and state(runtime)["looking"] is False
    assert RESULT not in repr(runtime.status())


def test_fresh_session_derives_new_authority_without_approval(rig):
    runtime, events, _ = rig
    look_now(runtime)
    runtime.stop_kids_mode(fold=False)
    runtime.start_kids_mode(KidsProfile(duration_minutes=15), greet=False)
    look_now(runtime)
    permits = [item[1] for item in events if isinstance(item, tuple) and item[0] == "permit"]
    assert len(permits) == 2 and permits[1] > permits[0]  # a new grant, never the old one
    wait_for(lambda: ("revoke", permits[0]) in events)  # End retired the first session's grant


def test_voice_turn_uses_the_existing_turn_owner(rig):
    runtime, events, _ = rig
    config = runtime._kids_voice_config(runtime.config_loader())
    assert runtime._show_tell_requested(config, "Look at my drawing!")
    assert not runtime._show_tell_requested(config, "do not look at my drawing")
    assert not runtime._show_tell_requested(config, "look at my drawing and read the screen")
    runtime._accept_wake_turn()
    with runtime._voice_activity_lock:  # the pipeline conversation owns the voice slot
        with pytest.raises(ShowAndTellUnavailable):
            runtime.show_tell_look_now()  # a second owner is refused
        assert runtime._show_tell_voice_turn() is True
    assert events.count("capture") == 1


STAGES = [("permit_hook", "permit"), ("speak", "LOOKING"), "capture", "look", ("speak", RESULT)]


def install(hooks, stage, action):
    hooks["permit" if stage == ("permit_hook", "permit") else stage] = action


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("event", ["stop", "end", "privacy", "opt_out", "camera_revoke", "replacement"])
def test_cancellation_at_every_boundary_prevents_later_capture_and_speech(rig, saved, stage, event):
    runtime, events, hooks = rig

    def cancel():
        events.append("CANCELLED")
        if event == "stop":
            runtime._request_conversation_stop()
        elif event == "end":
            runtime.stop_kids_mode(fold=False)
        elif event == "privacy":
            runtime._privacy_requested.set()
        elif event == "opt_out":
            saved["kids_show_and_tell_enabled"] = False
            runtime.show_tell_sync_permission(revoke=True)
        elif event == "camera_revoke":
            saved["camera_enabled"] = False  # the global Owner camera revoke, as /api/settings saves it
            runtime.show_tell_sync_permission(revoke=True)
        else:
            runtime.start_kids_mode(KidsProfile(duration_minutes=15), greet=False)

    install(hooks, stage, cancel)
    runtime.show_tell_look_now()
    wait_idle(runtime)
    start = events.index("CANCELLED") + 1
    after = events[start:]
    assert "pcm" not in after and "look" not in after
    assert "capture" not in after and ("speak", RESULT) not in after
    assert events.count("capture") <= 1
    assert runtime._kids_camera_active is False and state(runtime)["looking"] is False
    # The bridge is told to drop the in-flight look/context (cancel) or the whole grant (revoke).
    wait_for(lambda: any(item == "cancel" or (isinstance(item, tuple) and item[0] == "revoke")
                         for item in events[start:]))
    if event == "stop":
        # Stop cancels and clears context but never the Owner's saved preference.
        assert saved["kids_show_and_tell_enabled"] is True and state(runtime)["enabled"] is True
        assert state(runtime)["available"] is True


def test_saved_revocation_cancels_even_if_a_later_save_re_enabled(rig, saved):
    runtime, events, hooks = rig

    def flip():
        events.append("CANCELLED")
        saved["camera_enabled"] = False
        saved["camera_enabled"] = True  # saved again before this sync ran: the revoke still wins
        runtime.show_tell_sync_permission(revoke=True)

    hooks["capture"] = flip
    look_now(runtime)
    after = events[events.index("CANCELLED") + 1:]
    assert "look" not in after and ("speak", RESULT) not in after
    assert state(runtime)["available"] is True  # a later explicit look may derive fresh authority


def test_stale_slow_settings_read_cannot_restore_an_older_permission(rig, saved, monkeypatch):
    runtime, _, _ = rig
    entered, release = threading.Event(), threading.Event()
    reads = {"count": 0}

    def loader():
        reads["count"] += 1
        if reads["count"] == 1:
            snapshot = dict(saved)  # an old "enabled" read that stalls before applying
            entered.set()
            assert release.wait(3)
            return AppConfig(**snapshot)
        return AppConfig(**saved)

    monkeypatch.setattr(runtime, "config_loader", loader)
    slow = threading.Thread(target=runtime.show_tell_sync_permission)
    slow.start()
    assert entered.wait(3)
    saved["kids_show_and_tell_enabled"] = False  # the Owner's newer save
    newer = threading.Thread(target=runtime.show_tell_sync_permission, kwargs={"revoke": True})
    newer.start()
    time.sleep(0.05)
    release.set()
    slow.join(3)
    newer.join(3)
    assert state(runtime)["enabled"] is False and state(runtime)["available"] is False


def test_rate_and_busy_owner_deny_before_capture(rig, monkeypatch):
    runtime, events, _ = rig
    monkeypatch.setattr(show_and_tell, "MIN_LOOK_INTERVAL", 5.0)
    look_now(runtime)
    look_now(runtime)  # admitted by the owner route, refused by the gate's rate limit
    assert events.count("capture") == 1


# --- The real pipeline conversation loop ---------------------------------------------------------


def pipeline(monkeypatch, saved, transcripts):
    events, hooks = [], {"transcripts": list(transcripts)}
    monkeypatch.setattr(show_and_tell, "MIN_LOOK_INTERVAL", 0.0)
    runtime = make_runtime(monkeypatch, saved, events, hooks)
    runtime._notify_bridge_kids_session_async = lambda *_args, **_kwargs: None
    runtime.start_kids_mode(KidsProfile(duration_minutes=15), greet=False)
    config = runtime._kids_voice_config(AppConfig(api_key="test-key"))
    speech = list(transcripts)

    def record(_config):
        voiced = bool(speech) and speech.pop(0) != ""
        events.append("record")
        samples = np.ones(160, dtype=np.float32) if voiced else np.empty(0, dtype=np.float32)
        return EndpointResult(samples, voiced, "end_silence" if voiced else "initial_timeout", 0.01)

    runtime._record_utterance = record
    runtime._accept_wake_turn()
    return runtime, config, events, hooks


def test_conversation_loop_explicit_request_looks_once_then_follow_up_chats(monkeypatch, saved):
    runtime, config, events, _ = pipeline(monkeypatch, saved, ["Look at my drawing!", "It is a dragon"])
    try:
        runtime._run_conversation(config)
        assert ("chat", "Look at my drawing!") not in events  # the request never became a chat turn
        assert events.count("capture") == 1
        order = [events.index(item) for item in (
            ("heard", "Look at my drawing!"), ("speak", "LOOKING"), "capture", "look", ("speak", RESULT),
            ("heard", "It is a dragon"), ("chat", "It is a dragon"), ("speak", "What a fun idea!"),
        )]
        assert order == sorted(order)  # the same continuous turn carries on with a moderated chat
        assert events.count("record") >= 3 and events[-1] == "record"
    finally:
        runtime.stop_kids_mode(fold=False)


def test_conversation_loop_with_camera_off_routes_request_to_ordinary_chat(monkeypatch, saved):
    saved["camera_enabled"] = False
    runtime, config, events, _ = pipeline(monkeypatch, saved, ["Look at my drawing!"])
    try:
        runtime._run_conversation(config)
        assert ("chat", "Look at my drawing!") in events
        assert "capture" not in events and not [item for item in events if item[0] == "permit"]
    finally:
        runtime.stop_kids_mode(fold=False)


def test_conversation_loop_stop_during_look_ends_turn_without_result_or_listening(monkeypatch, saved):
    runtime, config, events, hooks = pipeline(monkeypatch, saved, ["Look at my drawing!", "More"])
    hooks["capture"] = runtime._request_conversation_stop
    try:
        runtime._run_conversation(config)
        after = events[events.index("capture") + 1:]
        assert "look" not in after and ("speak", RESULT) not in after
        assert "record" not in after and ("chat", "More") not in events  # no implicit relisten
        assert state(runtime)["enabled"] is True  # Stop never changes the saved preference
    finally:
        runtime.stop_kids_mode(fold=False)
