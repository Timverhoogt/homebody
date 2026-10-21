"""Real radio worker and runtime, synthetic decoder and silent playback only."""

import threading
import time

import numpy as np
import pytest
from test_everyday_runtime import runtime

from companion.everyday_host import MUTATIONS
from companion.everyday_manifest import local_tool_specs, validate_local_arguments
from companion.everyday_tools import TurnBudget
from companion.look_intent import direct_action_intent
from homebody import radio


class Decoder:
    def __init__(self, rate):
        self.rate = rate
        self.closed = False

    def read(self):
        time.sleep(0.005)
        return np.ones(self.rate // 100, dtype=np.float32) * 0.2

    def close(self):
        self.closed = True


class Output:
    def awake(self):
        pass

    def __init__(self):
        self.samples = []
        self.clears = 0
        self.received = threading.Event()

    def push_audio_sample(self, sample):
        self.samples.append(sample.copy())
        self.received.set()

    def clear(self):
        self.clears += 1


def player(output=None, **kwargs):
    return radio.RadioPlayer(output or Output(), 48000, allowed=lambda: True, decoder_factory=Decoder, **kwargs)


def finish(p):
    p.stop()
    p.thread.join(2)
    assert not p.thread.is_alive()


def test_decode_ready_is_silent_until_resume_and_stop_closes_worker():
    out = Output()
    p = player(out)
    try:
        status = p.start()
        assert status["state"] == "ready" and status["audible_verified"] is False
        assert not out.samples
        p.resume()
        assert out.received.wait(1)
        assert np.max(out.samples[0]) == pytest.approx(0.1)
        p.pause()
        count = len(out.samples)
        time.sleep(0.05)
        assert len(out.samples) == count and out.clears == 1
        p.resume()
    finally:
        finish(p)
    count = len(out.samples)
    time.sleep(0.03)
    assert len(out.samples) == count


def test_stopped_or_paused_decoder_failure_cannot_clear_new_voice_audio():
    release = threading.Event()

    class Failing(Decoder):
        def read(self):
            if release.is_set():
                raise RuntimeError("station offline")
            return super().read()

    out = Output()
    p = radio.RadioPlayer(out, 48000, allowed=lambda: True, decoder_factory=Failing)
    try:
        p.start()
        p.resume()
        assert out.received.wait(1)
        p.pause()
        clears = out.clears
        release.set()
        p.thread.join(1)
        assert p.status()["state"] == "failed"
        assert out.clears == clears
    finally:
        finish(p)


def test_stop_during_held_sdk_push_fences_late_output_and_new_voice():
    entered, release = threading.Event(), threading.Event()
    out = Output()

    def push(samples):
        entered.set()
        assert release.wait(5)
        out.samples.append(samples)

    out.push_audio_sample = push
    p = player(out)
    try:
        p.start()
        p.resume()
        assert entered.wait(1)
        p.stop()
        assert p.cancelled.is_set() and p.thread.is_alive()
        with pytest.raises(RuntimeError, match="in flight"):
            p.pause()
        release.set()
        p.thread.join(1)
        assert len(out.samples) == 1 and out.clears == 1
    finally:
        release.set()
        finish(p)


@pytest.mark.parametrize("field", ["url", "station", "volume", "command"])
def test_radio_cannot_accept_arbitrary_urls_or_commands(field):
    for name in ("start_reachy_radio", "stop_reachy_radio", "get_reachy_radio"):
        assert validate_local_arguments(name, {}) == {}
        with pytest.raises(ValueError):
            validate_local_arguments(name, {field: "anything"})


@pytest.mark.parametrize(
    "phrase, name",
    [
        ("turn on the radio", "start_reachy_radio"),
        ("turn the radio on", "start_reachy_radio"),
        ("play lo-fi radio", "start_reachy_radio"),
        ("zet de radio aan", "start_reachy_radio"),
        ("turn off the radio", "stop_reachy_radio"),
        ("stop the radio", "stop_reachy_radio"),
        ("zet de radio uit", "stop_reachy_radio"),
    ],
)
def test_direct_radio_commands_and_negated_or_quoted_refusals(phrase, name):
    assert direct_action_intent(phrase) == (name, {})
    assert direct_action_intent('"' + phrase + '"') is None
    assert direct_action_intent("don't " + phrase) is None


def install_fake_decoder(monkeypatch):
    real = radio.RadioPlayer
    monkeypatch.setattr(radio, "RadioPlayer", lambda *a, **k: real(*a, **k, decoder_factory=Decoder))


def test_runtime_device_scoped_playback_survives_transport_close_and_new_wake(monkeypatch):
    install_fake_decoder(monkeypatch)
    r, c = runtime()
    r._playback = Output()
    closed = threading.Event()
    r._radio_voice_begin()
    try:
        result = r.execute_everyday_tool("start_reachy_radio", {}, fence=r.everyday_tool_fence(closed))
        assert result["ok"] and r._radio_handoff_ready() and not r._playback.samples
        closed.set()  # Ordinary voice teardown is not a radio Stop.
        r._radio_voice_end()
        assert r._playback.received.wait(1)
        r._radio_voice_begin()
        r._agent_session_generation += 1  # A new normal wake retires callbacks, not device radio.
        r._radio_voice_end()
        assert not r._radio.cancelled.is_set()
        r._request_conversation_stop()
        assert r._radio.cancelled.is_set()
    finally:
        finish(r._radio)


@pytest.mark.parametrize("block", ["kids", "locked", "privacy", "sleep", "workspace", "not_ready"])
def test_radio_runtime_permission_guards_do_not_construct_decoder(monkeypatch, block):
    r, c = runtime()
    if block == "kids":
        r._kids_active = True
    elif block == "locked":
        r._kids_locked = True
    elif block == "privacy":
        r._privacy_requested.set()
    elif block == "sleep":
        r._power_mode = "sleep"
    elif block == "workspace":
        r._capability_profile = "agent"
    else:
        r._control_ready.clear()
    monkeypatch.setattr(radio, "RadioPlayer", lambda *a, **k: pytest.fail("No decoder admission"))
    assert not r.execute_everyday_tool("start_reachy_radio", {})["ok"]


def test_standby_radio_resumes_audio_only_after_safe_fold(monkeypatch):
    install_fake_decoder(monkeypatch)
    r, c = runtime()
    r._playback = Output()
    audio = []
    r._resume_playback_after_wake = lambda: audio.append("audio only")
    r._radio_voice_begin()
    try:
        assert r.execute_everyday_tool("start_reachy_radio", {})["ok"]
        r._power_mode = "standby"
        r._radio_voice_end()
        assert r._radio.paused and not audio
        r._motors_enabled, r._head_safely_folded = False, True
        r._radio_resume_idle()
        assert audio == ["audio only"] and not r._radio.paused
    finally:
        finish(r._radio)


def test_station_unavailable_returns_honest_error(monkeypatch):
    class Offline:
        def __init__(self, rate):
            raise radio.RadioFailure("stream_decode_failed")

    p = radio.RadioPlayer(Output(), 48000, allowed=lambda: True, decoder_factory=Offline)
    with pytest.raises(RuntimeError, match="could not be decoded"):
        p.start()
    finish(p)


def test_radio_uses_same_cross_engine_specs_and_mutation_budget():
    names = {s["name"] for s in local_tool_specs()}
    assert {"start_reachy_radio", "stop_reachy_radio", "get_reachy_radio"} <= names
    assert {"start_reachy_radio", "stop_reachy_radio"} <= MUTATIONS
    b = TurnBudget()
    assert not b.admit("one", "start_reachy_radio")
    assert b.admit("two", "home_control")


def test_stop_during_decoder_connection_never_commits_start(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class Held(Decoder):
        def __init__(self, rate):
            entered.set()
            assert release.wait(2)
            super().__init__(rate)

    real = radio.RadioPlayer
    monkeypatch.setattr(radio, "RadioPlayer", lambda *a, **k: real(*a, **k, decoder_factory=Held))
    r, c = runtime()
    r._playback = Output()
    result = []
    t = threading.Thread(target=lambda: result.append(r.execute_everyday_tool("start_reachy_radio", {})))
    t.start()
    try:
        assert entered.wait(1)
        r._request_conversation_stop()
        release.set()
        t.join(2)
        assert not t.is_alive() and not result[0]["ok"]
        assert not r._playback.samples
    finally:
        release.set()
        t.join(2)
        finish(r._radio)
