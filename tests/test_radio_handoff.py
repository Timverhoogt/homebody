"""Live radio can become ready after its spoken preface already drained."""

import threading

import pytest
from test_live_voice import FakeLiveSession, LiveHarness, audio

from homebody.config import AppConfig
from homebody.realtime_client import RealtimeEvent


@pytest.mark.parametrize("after_result_audio", [False, True])
def test_live_radio_ready_after_preface_does_not_wait_for_another_reply(monkeypatch, after_result_audio):
    clock = {"now": 100.0}
    ready = threading.Event()
    monkeypatch.setattr("homebody.voice_live.time.monotonic", lambda: clock["now"])
    session = FakeLiveSession(
        [
            [audio(2400)],
            [],
            [],
            [],
            [RealtimeEvent("bridge.delegation", {"state": "answered"})],
            *([[audio(2400)]] if after_result_audio else [[]]),
        ]
    )
    r = LiveHarness(session, stop_after=30)
    r._radio_handoff_ready = ready.is_set
    original_read = r._read_16k_frame

    def advance():
        clock["now"] += 0.2
        if r.loops == 4:
            ready.set()  # Exact production ordering: prior speech ended before the tool result.
        return original_read()

    r._read_16k_frame = advance
    r._run_live_conversation(AppConfig(api_key="synthetic", barge_in_enabled=True))
    assert r.loops < 20, "Radio remained paused until user Stop / conversation timeout"
    assert session.order[-2:] == ["clear_audio", "close"]
    assert r.pushed == ([2400, 2400] if after_result_audio else [2400])


def test_live_radio_ready_before_result_does_not_close_over_inflight_callback(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr("homebody.voice_live.time.monotonic", lambda: clock["now"])
    session = FakeLiveSession(
        [[audio(2400)], [], [], [], [], [], [], [], [RealtimeEvent("bridge.delegation", {"state": "answered"})]]
    )
    r = LiveHarness(session, stop_after=40)
    r._radio_handoff_ready = lambda: True
    original_read = r._read_16k_frame

    def advance():
        clock["now"] += 0.2
        return original_read()

    r._read_16k_frame = advance
    r._run_live_conversation(AppConfig(api_key="synthetic", barge_in_enabled=True))
    assert 9 <= r.loops < 25
    assert session.order[-1] == "close"


def test_realtime_radio_text_only_completion_closes_without_waiting_for_audio(monkeypatch):
    from test_voice_workspace import runtime_fixture

    r = runtime_fixture()
    r._radio_handoff_ready = lambda: True
    closed = []

    class Session:
        def __init__(self, *args, **kwargs):
            self.reads = 0

        def start(self):
            pass

        def events(self):
            self.reads += 1
            assert self.reads == 1, "Text-only completed response did not hand off to radio"
            return [RealtimeEvent("response.done", {"response": {"id": "r1", "status": "completed"}})]

        def close(self):
            closed.append(True)

    monkeypatch.setattr("homebody.runtime.RealtimeBridgeSession", Session)
    r._run_realtime_conversation(AppConfig(conversation_mode="realtime"))
    assert closed == [True]


def test_new_speech_during_radio_grace_keeps_voice_owner(monkeypatch):
    clock = {"now": 100.0}
    ready = threading.Event()
    ready.set()
    monkeypatch.setattr("homebody.voice_live.time.monotonic", lambda: clock["now"])
    session = FakeLiveSession(
        [
            [RealtimeEvent("bridge.delegation", {"state": "answered"})],
            [RealtimeEvent("session.input_transcript.delta", {"delta": "Actually wait"})],
            [],
            [],
            [],
            [],
            [RealtimeEvent("session.closed", {})],
        ]
    )
    r = LiveHarness(session, stop_after=20)
    r._radio_handoff_ready = ready.is_set
    r._radio_defer_handoff = ready.clear
    original_read = r._read_16k_frame

    def advance():
        clock["now"] += 0.3
        return original_read()

    r._read_16k_frame = advance
    r._run_live_conversation(AppConfig(api_key="synthetic", barge_in_enabled=True))
    assert not ready.is_set()
    assert not session.script  # New speech prevented the old pending radio handoff.
