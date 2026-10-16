"""Adversarial SDK callback interleavings; synthetic adapters, never hardware."""

import runpy
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from homebody.motion import VoiceMotion
from homebody.runtime import HermesVoiceRuntime

SDK = runpy.run_path(str(Path(__file__).parent / "fixtures" / "sdk_19_sleep_callbacks.py"), init_globals={
    "np": np, "time": time, "Gst": SimpleNamespace(FlowReturn=SimpleNamespace(OK="ok")),
})
OFFSETS = (0.001, 0.002, 0.003, 0.01, 0.02, 0.03)


def fixture():
    events, callbacks = [], []
    media = SimpleNamespace(
        enable_wobbling=callbacks.append,
        stop_playing=lambda: events.append("stopped"),
        play_sound=lambda path: events.append("play"),
        push_audio_sample=lambda data: events.append("push"),
        start_playing=lambda: events.append("start"),
        audio=SimpleNamespace(clear_player=lambda: events.append("clear")),
    )

    def send(command):
        if hasattr(command, "offsets"):
            events.append(tuple(command.offsets))

    robot = SimpleNamespace(
        media=media, client=SimpleNamespace(send_command=send),
        goto_target=lambda **kwargs: None,
        disable_wobbling=lambda: events.append("disabled"),
    )
    rt = HermesVoiceRuntime(robot, threading.Event())
    motion = VoiceMotion(robot, playback=rt._playback)
    rt._motion = motion
    motion.speaking()
    return rt, motion, robot, events, callbacks


def test_actual_sdk_callback_held_after_generation_check_cannot_emit_after_fold_or_new_wake():
    rt, motion, robot, events, callbacks = fixture()
    entered, release = threading.Event(), threading.Event()
    old_callback = callbacks[-1]

    def held_apply(offsets):
        entered.set()  # HeadWobbler._fire has already released its generation lock.
        assert release.wait(2)
        old_callback(offsets)

    wobbler = SimpleNamespace(_lock=threading.Lock(), _generation=7, _apply_offsets=held_apply)
    worker = threading.Thread(target=lambda: SDK["_fire"](wobbler, OFFSETS, 7))
    worker.start()
    assert entered.wait(2)
    rt._playback.suspend()
    motion.prepare_sleep(robot.media.stop_playing)
    events.append("native-fold-start")
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert events == ["stopped", "disabled", "native-fold-start"]

    rt._playback.awake()
    motion.awake()
    motion.speaking()
    old_callback(OFFSETS)  # Still obsolete, even after a later legitimate wake.
    assert OFFSETS not in events
    callbacks[-1](OFFSETS)
    assert events[-1] == OFFSETS


def test_sleep_waits_for_an_offset_emission_already_inside_its_send_boundary():
    rt, motion, robot, events, callbacks = fixture()
    entered, release, folded = threading.Event(), threading.Event(), threading.Event()
    send = robot.client.send_command

    def held_send(command):
        if hasattr(command, "offsets"):
            entered.set()
            assert release.wait(2)
        send(command)

    robot.client.send_command = held_send
    worker = threading.Thread(target=lambda: callbacks[-1](OFFSETS))
    worker.start()
    assert entered.wait(2)

    def fold():
        rt._playback.suspend()
        motion.prepare_sleep(robot.media.stop_playing)
        folded.set()

    sleeper = threading.Thread(target=fold)
    sleeper.start()
    assert not folded.wait(0.05)
    release.set()
    worker.join(2)
    sleeper.join(2)
    assert not worker.is_alive() and not sleeper.is_alive()
    assert events == [OFFSETS, "stopped", "disabled"]


def test_late_real_runtime_clear_cannot_restart_playback_between_stop_and_remove():
    rt, motion, robot, events, callbacks = fixture()
    stopped, finish_stop = threading.Event(), threading.Event()

    def held_stop():
        events.append("NULL")
        stopped.set()
        assert finish_stop.wait(2)

    # Wire the real SDK clear_player body to synthetic Gst primitives. If it
    # runs during sleep, its state transitions would resurrect PLAYING.
    gst = SimpleNamespace(
        State=SimpleNamespace(PAUSED="PAUSED", PLAYING="PLAYING"),
        Event=SimpleNamespace(new_flush_start=lambda: None, new_flush_stop=lambda **kw: None),
    )
    clear = SDK["clear_player"]
    clear.__globals__["Gst"] = gst
    sdk_audio = SimpleNamespace(
        _head_wobbler=None, _appsrc=SimpleNamespace(send_event=lambda e: None),
        _pipeline=SimpleNamespace(set_state=events.append),
        logger=SimpleNamespace(info=lambda s: None, warning=lambda s: None),
    )
    robot.media.audio.clear_player = lambda: clear(sdk_audio)
    robot.media.stop_playing = held_stop

    def fold():
        rt._playback.suspend()
        motion.prepare_sleep(held_stop)

    sleeper = threading.Thread(target=fold)
    sleeper.start()
    assert stopped.wait(2)
    cleanup = threading.Thread(target=rt._clear_streamed_audio)
    cleanup.start()
    assert cleanup.is_alive()
    finish_stop.set()
    sleeper.join(2)
    cleanup.join(2)
    assert not sleeper.is_alive() and not cleanup.is_alive()
    assert events == ["NULL", "disabled"]
    assert "PLAYING" not in events

    # No restarted audio can pass the real callback's non-None wobbler check.
    SDK["_on_wobbler_sample"].__globals__["Gst"] = SimpleNamespace(FlowReturn=SimpleNamespace(OK="ok"))
    assert SDK["_on_wobbler_sample"](sdk_audio, SimpleNamespace(pull_sample=lambda: object())) == "ok"


def test_every_app_playback_producer_is_blocked_until_confirmed_wake():
    rt, motion, robot, events, callbacks = fixture()
    rt._playback.suspend()
    motion.prepare_sleep(robot.media.stop_playing)
    saved_push = rt._playback.push_audio_sample
    for _ in range(2):
        rt._playback.play_sound("late.wav")
        saved_push(np.zeros(10))
        rt._playback.start_playing()
        rt._clear_streamed_audio()
        motion.resume()
        motion.speaking()
    assert events == ["stopped", "disabled"]
    rt._playback.awake()
    motion.awake()
    rt._playback.play_sound("new.wav")
    saved_push(np.zeros(10))
    rt._clear_streamed_audio()
    assert events[-3:] == ["play", "push", "clear"]


def test_hung_sdk_push_keeps_ordinary_clear_responsive_but_refuses_sleep():
    rt, motion, robot, events, callbacks = fixture()
    entered, release = threading.Event(), threading.Event()

    def held_push(samples):
        entered.set()
        assert release.wait(5)

    robot.media.push_audio_sample = held_push
    producer = threading.Thread(target=lambda: rt._playback.push_audio_sample(np.zeros(10)))
    producer.start()
    assert entered.wait(2)
    cleanup = threading.Thread(target=rt._clear_streamed_audio)
    cleanup.start()
    cleanup.join(0.5)
    assert not cleanup.is_alive() and events == ["clear"]
    try:
        with pytest.raises(RuntimeError, match="playback is still in flight"):
            rt._playback.suspend()
        assert rt._playback.blocked is True
        assert "stopped" not in events and "disabled" not in events
        with pytest.raises(RuntimeError, match="playback is still in flight"):
            rt._playback.awake()
    finally:
        release.set()
        producer.join(2)
    assert not producer.is_alive()
    rt._playback.awake()
