"""Failed quiescence cannot pretend it discharged a Kids audio flush obligation."""
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from homebody.kids_acknowledgements import KidsAckPlayer
from homebody.playback_gate import PlaybackGate


def setup_player():
    entered, release, current = threading.Event(), threading.Event(), threading.Event()
    current.set()
    queued, calls = [], []

    def push(samples):
        entered.set()
        assert release.wait(8)
        queued.append(samples)
        calls.append("late-push")

    def clear():
        queued.clear()
        calls.append("clear")

    media = SimpleNamespace(push_audio_sample=push, audio=SimpleNamespace(clear_player=clear))
    gate = PlaybackGate(lambda: media)
    clip = SimpleNamespace(name="test", samples=np.zeros(10), sample_rate=24000)
    player = KidsAckPlayer(
        clips=(clip,), selector=SimpleNamespace(choose=lambda clips, now: clips[0]),
        push=gate.push_audio_sample, clear=gate.clear, is_current=current.is_set,
        resample=lambda samples, old, new: samples, output_rate=24000, clock=time.monotonic, delay=0,
    ).start()
    assert entered.wait(2)
    return gate, player, media, release, current, queued, calls


def test_timeout_then_late_push_settlement_flushes_before_wake():
    gate, player, media, release, current, queued, calls = setup_player()
    try:
        gate.clear()  # Ordinary Stop is still responsive during the opaque push.
        with pytest.raises(RuntimeError, match="playback is still in flight"):
            gate.suspend()
        assert gate.blocked
        current.clear()
    finally:
        release.set()
        player._thread.join(2)
    assert not player._thread.is_alive()
    assert player.settle()
    assert not queued, calls
    assert not player.inflight
    assert calls.index("late-push") < len(calls) - 1 and calls[-1] == "clear"
    gate.awake()
    assert not queued and not gate.blocked


def test_failed_post_timeout_clear_retains_quarantine_and_refuses_wake():
    gate, player, media, release, current, queued, calls = setup_player()
    try:
        with pytest.raises(RuntimeError, match="playback is still in flight"):
            gate.suspend()
        def fail_clear():
            raise RuntimeError("SDK flush failed")
        media.audio.clear_player = fail_clear
        current.clear()
    finally:
        release.set()
        player._thread.join(2)
    assert not player.settle()
    assert player.inflight and queued and gate.blocked
    with pytest.raises(RuntimeError, match="SDK flush failed"):
        gate.awake()
    assert gate.blocked and queued
    media.audio.clear_player = queued.clear
    gate.awake()
    assert not queued and not gate.blocked
    assert player.inflight  # Runtime recovery still owns the player's recorded failure.


def test_wake_flushes_late_output_even_without_a_player_cleanup():
    entered, release = threading.Event(), threading.Event()
    queued = []
    def push(samples):
        entered.set()
        assert release.wait(8)
        queued.append(samples)
    media = SimpleNamespace(push_audio_sample=push, audio=SimpleNamespace(clear_player=queued.clear))
    gate = PlaybackGate(lambda: media)
    producer = threading.Thread(target=lambda: gate.push_audio_sample("old"))
    producer.start()
    assert entered.wait(2)
    try:
        with pytest.raises(RuntimeError, match="playback is still in flight"):
            gate.suspend()
        with pytest.raises(RuntimeError, match="playback is still in flight"):
            gate.awake()
    finally:
        release.set()
        producer.join(2)
    assert queued == ["old"]
    gate.awake()
    assert not queued and not gate.blocked


def test_drained_but_unconfirmed_stop_does_not_claim_cleanup_success():
    calls = []
    gate = PlaybackGate(lambda: SimpleNamespace(audio=SimpleNamespace(clear_player=lambda: calls.append("clear"))))
    gate.suspend()
    gate.clear()  # late cleanup yields to the fold, without confirming SDK stop
    assert not gate._stop_confirmed
    with pytest.raises(RuntimeError, match="not confirmed"):
        gate.awake()
    assert gate.blocked and not calls
    gate.confirm_stopped()
    gate.clear()  # Only a proven SDK stop can discharge cleanup without resurrecting PLAYING.
    assert not calls
    gate.awake()
    gate.clear()
    assert calls == ["clear"]
