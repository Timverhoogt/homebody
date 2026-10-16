"""Kids waiting speech: one cached clip during slow I Spy searches and backend reply waits.

Real runtime entry points (``_run_conversation``, ``start_kids_mode``, ``_continue_ispy_reachy_turn``)
with synthetic PCM, held proofs and held SDK pushes; no real voice, network or hardware.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from types import SimpleNamespace

import pytest
from test_kids_interaction_fixes import profile_for
from test_kids_stt_acknowledgements import VOICE_PROOF, Harness, ack_threads, cancel, wait_for

from homebody import kids_acknowledgements as acks
from homebody.config import AppConfig
from homebody.ispy import ISpyCancelled
from homebody.runtime import HermesVoiceRuntime

# Synthetic catalog: nl-0 "Even denken…" (think), nl-1 "Eens kijken…" (see); en has no "see" clip.
THINK, SEE = "nl-0.wav", "nl-1.wav"


@pytest.fixture(autouse=True)
def short_waits(monkeypatch):
    monkeypatch.setattr(acks, "REPLY_WAIT_DELAY_SECONDS", 0.2)
    monkeypatch.setattr(acks, "SEARCH_WAIT_DELAY_SECONDS", 0.2)


def with_bridge(harness: Harness, *, chat_seconds: float = 0.6, proof=None, proof_gate=None) -> None:
    """Give the harness client a slow Kids reply and a (possibly held) fresh voice proof."""
    base = harness.runtime._new_bridge_client
    harness.chat_release = threading.Event()

    def factory(cfg):
        client = base(cfg)

        def chat(_text):
            harness.calls.append(("chat", time.monotonic()))
            harness.chat_release.wait(chat_seconds)
            harness.calls.append(("chat_end", time.monotonic()))
            return "Goed zo!"

        def kids_voice_proof(**_kwargs):
            harness.calls.append(("proof", time.monotonic()))
            if proof_gate is not None:
                proof_gate()
            return dict(VOICE_PROOF if proof is None else proof)

        client.chat = chat
        client.kids_voice_proof = kids_voice_proof
        client.ispy_clue = lambda _sid: "Ik zie iets blauws"
        return client

    harness.runtime._new_bridge_client = factory  # type: ignore[method-assign]


def chosen(runtime: HermesVoiceRuntime) -> list[str]:
    picks: list[str] = []
    selector = runtime._kids_ack_selector
    original = selector.choose

    def choose(clips, now):
        clip = original(clips, now)
        if clip is not None:
            picks.append(clip.name)
        return clip

    selector.choose = choose  # type: ignore[method-assign]
    return picks


BOUNDARIES = ["stop", "end", "privacy", "new_start", "standby", "shutdown", "expiry"]


def cancel_any(runtime: HermesVoiceRuntime, boundary: str) -> None:
    """The shared matrix plus process shutdown and the outer session deadline passing."""
    if boundary == "shutdown":
        runtime.stop_event.set()
    elif boundary == "expiry":
        with runtime._kids_lock:
            runtime._kids_ends_at = time.monotonic() - 0.01
    else:
        cancel(runtime, boundary)


def clears_between(harness: Harness, start: float, end: float) -> bool:
    return any(kind == "clear" and start <= when <= end for kind, when, _ in harness.media.events)


# --- Backend reply waits (noncamera Kids replies and shared games) -------------------------------


@pytest.mark.parametrize("host", [False, True])
def test_slow_reply_plays_one_think_clip_and_answer_is_not_delayed(monkeypatch, tmp_path, host) -> None:
    harness = Harness(monkeypatch, tmp_path, stt_seconds=0.02, clip_seconds=1.5)
    if host:  # shared game: the game master hands the transcript to the normal Kids reply route
        harness.config = dataclasses.replace(harness.config, kids_host_enabled=True)
        harness.runtime._kids_host_handle_transcript = lambda *_a, **_k: False  # type: ignore[method-assign]
        harness.runtime._kids_host_after_reply = lambda *_a, **_k: False  # type: ignore[method-assign]
    with_bridge(harness, chat_seconds=0.6)
    picks = chosen(harness.runtime)
    harness.run()
    pushes = harness.media.pushes()
    assert picks == [THINK]  # reply waits use the thinking phrase, never "Eens kijken"
    chat, chat_end, answer = harness.at("chat"), harness.at("chat_end"), harness.at("answer")
    assert chat + 0.15 < pushes[0][1] - harness.t0 < chat_end  # after the delay, while still waiting
    assert answer - chat_end < 0.3  # the 1.5 s clip never delays the real answer
    assert clears_between(harness, pushes[-1][1], answer + harness.t0)  # flushed before reply speech
    assert ack_threads() == []


def test_fast_reply_skips_filler_and_never_reads_proof(monkeypatch, tmp_path) -> None:
    harness = Harness(monkeypatch, tmp_path, stt_seconds=0.02)
    with_bridge(harness, chat_seconds=0.05)
    harness.run()
    assert harness.media.pushes() == []
    assert not any(label == "proof" for label, _ in harness.calls)
    assert ack_threads() == []


def test_slow_stt_and_slow_reply_speak_only_once(monkeypatch, tmp_path) -> None:
    harness = Harness(monkeypatch, tmp_path, stt_seconds=0.5, clip_seconds=0.6)
    with_bridge(harness, chat_seconds=0.6)
    picks = chosen(harness.runtime)
    harness.run()
    assert len(picks) == 1  # the recognition clip already spoke: no second reply filler
    pushes = harness.media.pushes()
    # The reply wait neither replaced nor cut the recognition clip that was already speaking.
    assert not clears_between(harness, pushes[0][1], harness.at("chat_end") + harness.t0)


def test_cooldown_keeps_consecutive_slow_replies_sparse(monkeypatch, tmp_path) -> None:
    harness = Harness(
        monkeypatch, tmp_path, stt_seconds=0.02, transcripts=("een", "twee"), speech=[True, True, False, False, False],
    )
    with_bridge(harness, chat_seconds=0.5)
    picks = chosen(harness.runtime)
    harness.run()
    assert [label for label, _ in harness.calls].count("answer") == 2
    assert len(picks) == 1  # second wait is inside the cooldown: silent


@pytest.mark.parametrize("case", ["calm", "wrong_proof", "wrong_model", "no_catalog"])
def test_ineligible_reply_waits_stay_silent(monkeypatch, tmp_path, case) -> None:
    harness = Harness(
        monkeypatch, tmp_path, activity="calm" if case == "calm" else "quiz", stt_seconds=0.02,
        catalog=case != "no_catalog",
    )
    proof = {
        "wrong_proof": {**VOICE_PROOF, "kids_voice_sha256": "0" * 64},
        "wrong_model": {**VOICE_PROOF, "kids_tts_model": "eleven_multilingual_v2"},
    }.get(case)
    with_bridge(harness, chat_seconds=0.6, proof=proof)
    harness.run()
    assert harness.media.pushes() == []
    assert harness.at("answer") - harness.at("chat_end") < 0.3


def test_adult_and_unsupported_language_never_arm(monkeypatch, tmp_path) -> None:
    harness = Harness(monkeypatch, tmp_path)
    runtime = harness.runtime
    refresh = lambda: dict(VOICE_PROOF)  # noqa: E731
    assert runtime._start_kids_ack(AppConfig(api_key="k"), refresh=refresh, stage="think", delay=0.0) is None
    assert runtime._arm_kids_wait_speech("es", lambda: True, refresh=refresh, stage="think") is None
    assert runtime._arm_kids_wait_speech("en", lambda: True, refresh=refresh, stage="see") is None  # no EN "see"


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_cancel_during_held_reply_proof_prevents_late_audio(monkeypatch, tmp_path, boundary) -> None:
    harness = Harness(monkeypatch, tmp_path, stt_seconds=0.02)
    gate = threading.Event()
    with_bridge(harness, chat_seconds=1.0, proof_gate=lambda: gate.wait(3))

    def cancel_in_proof() -> None:
        assert wait_for(lambda: any(label == "proof" for label, _ in harness.calls))
        cancel_any(harness.runtime, boundary)
        gate.set()

    threading.Thread(target=cancel_in_proof, daemon=True).start()
    harness.run()
    time.sleep(0.2)
    assert any(label == "proof" for label, _ in harness.calls)  # the race really hit the proof read
    assert harness.media.pushes() == []
    assert "answer" not in [label for label, _ in harness.calls]
    assert ack_threads() == []


def test_reply_finishing_during_held_proof_stays_silent(monkeypatch, tmp_path) -> None:
    harness = Harness(monkeypatch, tmp_path, stt_seconds=0.02)
    gate = threading.Event()
    with_bridge(harness, chat_seconds=0.4, proof_gate=lambda: gate.wait(3))
    threading.Thread(
        target=lambda: (wait_for(lambda: any(label == "chat_end" for label, _ in harness.calls)), gate.set()),
        daemon=True,
    ).start()
    harness.run()
    time.sleep(0.2)
    assert any(label == "proof" for label, _ in harness.calls)
    assert harness.media.pushes() == []
    assert "answer" in [label for label, _ in harness.calls]


def test_held_sdk_push_quarantines_output_and_blocks_new_producers(monkeypatch, tmp_path) -> None:
    harness = Harness(monkeypatch, tmp_path, stt_seconds=0.02, clip_seconds=1.5)
    with_bridge(harness, chat_seconds=0.5)
    runtime = harness.runtime
    release = threading.Event()
    harness.media.on_push = lambda: release.wait(5)  # the first PCM push hangs inside the SDK
    harness.run()
    labels = [label for label, _ in harness.calls]
    assert "answer" not in labels and labels.count("record") == 1  # no speech, no relisten
    assert acks.output_quarantined(runtime)
    held = runtime._kids_ack_producer
    assert runtime._start_kids_ack(harness.config, harness.proof) is None  # never overwrites the owner
    assert runtime._arm_kids_wait_speech("nl", lambda: True, voice_proof=harness.proof, stage="think") is None
    assert runtime._kids_ack_producer is held
    clears_before = sum(1 for kind, *_ in harness.media.events if kind == "clear")
    released_at = time.monotonic()
    release.set()
    assert wait_for(lambda: not held.running, 2)
    assert not acks.output_quarantined(runtime)
    assert len(harness.media.pushes()) == 1  # no chunk after the settle
    assert sum(1 for kind, *_ in harness.media.events if kind == "clear") > clears_before
    assert clears_between(harness, released_at, time.monotonic())  # producer flushed its own chunk
    # Once the old push returned and flushed, a healthy later producer is admitted and plays.
    harness.media.on_push = None
    runtime._kids_ack_selector = acks.AckSelector()  # outside the 8 s cooldown of the old clip
    later = runtime._arm_kids_wait_speech("nl", lambda: True, voice_proof=harness.proof, stage="see", delay=0.0)
    assert later is not None and runtime._kids_ack_producer is later
    assert wait_for(lambda: len(harness.media.pushes()) > 1, 2)
    later.settle()
    assert wait_for(lambda: not later.running, 2)
    assert not acks.output_quarantined(runtime)


# --- I Spy continuation search -----------------------------------------------------------------


def continuation(monkeypatch, tmp_path, *, search_seconds: float, language: str = "nl", proof_gate=None):
    monkeypatch.setattr(HermesVoiceRuntime, "_prepare_ispy_round", lambda self, *_a, **_k: SimpleNamespace())
    harness = Harness(monkeypatch, tmp_path, activity="ispy", language=language)
    with_bridge(harness, proof_gate=proof_gate)
    runtime = harness.runtime
    search_done = threading.Event()

    def search(generation, _profile, *, client=None, turn_bound=False):
        harness.calls.append(("search", time.monotonic()))
        deadline = time.monotonic() + search_seconds
        while time.monotonic() < deadline:
            runtime._assert_ispy_current(generation, turn_bound=turn_bound)
            time.sleep(0.02)
        search_done.set()
        return SimpleNamespace()

    runtime._prepare_ispy_round = search  # type: ignore[method-assign]
    client = runtime._new_bridge_client(harness.config)
    runtime._accept_wake_turn()
    harness.t0 = time.monotonic()
    return harness, client


def test_continuation_search_plays_one_see_clip_flushed_before_clue(monkeypatch, tmp_path) -> None:
    harness, client = continuation(monkeypatch, tmp_path, search_seconds=0.6)
    picks = chosen(harness.runtime)
    harness.runtime._continue_ispy_reachy_turn(client, barge_in=False)
    pushes = harness.media.pushes()
    assert picks == [SEE]
    assert clears_between(harness, pushes[-1][1], harness.at("answer") + harness.t0)
    assert "record" not in [label for label, _ in harness.calls]  # never opens the microphone
    assert ack_threads() == []


def test_fast_continuation_search_is_silent(monkeypatch, tmp_path) -> None:
    harness, client = continuation(monkeypatch, tmp_path, search_seconds=0.02)
    harness.runtime._continue_ispy_reachy_turn(client, barge_in=False)
    assert harness.media.pushes() == []
    assert "answer" in [label for label, _ in harness.calls]


def test_language_without_see_clip_keeps_search_silent(monkeypatch, tmp_path) -> None:
    harness, client = continuation(monkeypatch, tmp_path, search_seconds=0.6, language="en")
    harness.runtime._continue_ispy_reachy_turn(client, barge_in=False)
    assert harness.media.pushes() == []  # no wrong-phrase or wrong-language substitute


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_cancel_during_continuation_search_flushes_and_never_speaks_clue(monkeypatch, tmp_path, boundary) -> None:
    harness, client = continuation(monkeypatch, tmp_path, search_seconds=3.0)
    harness.runtime._clear_streamed_audio = lambda: harness.media.events.append(  # type: ignore[method-assign]
        ("clear", time.monotonic(), 0)
    )
    cancelled_at: list[float] = []

    def on_push() -> None:
        if len(harness.media.pushes()) == 2 and not cancelled_at:
            cancelled_at.append(time.monotonic())
            cancel_any(harness.runtime, boundary)

    harness.media.on_push = on_push
    with pytest.raises(ISpyCancelled):
        harness.runtime._continue_ispy_reachy_turn(client, barge_in=False)
    time.sleep(0.2)
    assert len(harness.media.pushes()) == 2
    assert clears_between(harness, cancelled_at[0], time.monotonic())
    assert "answer" not in [label for label, _ in harness.calls]
    assert ack_threads() == []


# --- Initial I Spy Start search ----------------------------------------------------------------


def initial_start(monkeypatch, tmp_path, *, search_seconds: float, proof_gate=None, on_search=None):
    harness = Harness(monkeypatch, tmp_path)  # an earlier quiz session; Start replaces it
    with_bridge(harness, proof_gate=proof_gate)
    runtime = harness.runtime

    def search(generation, _profile, **_kwargs):
        harness.calls.append(("search", time.monotonic()))
        if on_search is not None:
            on_search()
        time.sleep(search_seconds)
        return SimpleNamespace(clue=lambda _language: "Ik zie iets blauws")

    runtime._prepare_ispy_round = search  # type: ignore[method-assign]
    runtime.queue_announcement = lambda *a, **k: harness.calls.append(  # type: ignore[method-assign]
        ("announce", time.monotonic())
    )
    return harness


def test_initial_start_search_plays_see_clip_despite_its_own_stop_flag(monkeypatch, tmp_path) -> None:
    harness = initial_start(monkeypatch, tmp_path, search_seconds=0.7)
    picks = chosen(harness.runtime)
    harness.t0 = time.monotonic()
    harness.runtime.start_kids_mode(profile_for("ispy", "nl"))
    pushes = harness.media.pushes()
    assert picks == [SEE] and pushes
    announce = harness.at("announce") + harness.t0
    assert pushes[-1][1] <= announce and clears_between(harness, pushes[-1][1], announce)
    assert "record" not in [label for label, _ in harness.calls]  # no microphone during the search
    assert ack_threads() == []


def test_fast_initial_search_is_silent(monkeypatch, tmp_path) -> None:
    harness = initial_start(monkeypatch, tmp_path, search_seconds=0.02)
    harness.runtime.start_kids_mode(profile_for("ispy", "nl"))
    assert harness.media.pushes() == []
    assert not any(label == "proof" for label, _ in harness.calls)


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_cancel_during_held_initial_proof_prevents_late_audio(monkeypatch, tmp_path, boundary) -> None:
    gate = threading.Event()
    harness = initial_start(monkeypatch, tmp_path, search_seconds=0.8, proof_gate=lambda: gate.wait(3))

    def cancel_in_proof() -> None:
        assert wait_for(lambda: any(label == "proof" for label, _ in harness.calls))
        cancel_any(harness.runtime, boundary)
        gate.set()

    threading.Thread(target=cancel_in_proof, daemon=True).start()
    try:
        harness.runtime.start_kids_mode(profile_for("ispy", "nl"))
    except Exception:
        pass  # End/replacement make the clue stale; that failure is the runtime's, not ours
    time.sleep(0.2)
    assert any(label == "proof" for label, _ in harness.calls)  # the race really hit the proof read
    assert harness.media.pushes() == []
    assert ack_threads() == []


@pytest.mark.parametrize("boundary", ["stop", "end", "privacy", "shutdown", "expiry"])
def test_stop_while_initial_clip_plays_flushes_and_stays_silent(monkeypatch, tmp_path, boundary) -> None:
    harness = initial_start(monkeypatch, tmp_path, search_seconds=1.0)
    cancelled_at: list[float] = []

    def on_push() -> None:
        if len(harness.media.pushes()) == 2 and not cancelled_at:
            cancelled_at.append(time.monotonic())
            cancel_any(harness.runtime, boundary)

    harness.media.on_push = on_push
    try:
        harness.runtime.start_kids_mode(profile_for("ispy", "nl"))
    except Exception:
        pass
    time.sleep(0.2)
    assert cancelled_at and len(harness.media.pushes()) == 2
    assert clears_between(harness, cancelled_at[0], time.monotonic())
    assert ack_threads() == []


# --- Shared play (game master) -> I Spy search ----------------------------------------------------


def hosted(monkeypatch, tmp_path, *, search_seconds: float, proof_gate=None, real_queue: bool = False):
    harness = Harness(monkeypatch, tmp_path)
    with_bridge(harness, proof_gate=proof_gate)
    runtime = harness.runtime
    runtime.start_kids_mode(dataclasses.replace(profile_for("ispy", "nl"), host=True), greet=False)

    def search(generation, _profile, **_kwargs):
        harness.calls.append(("search", time.monotonic()))
        time.sleep(search_seconds)
        return SimpleNamespace(clue=lambda _language: "Ik zie iets blauws")

    runtime._prepare_ispy_round = search  # type: ignore[method-assign]
    if not real_queue:
        runtime.queue_announcement = lambda *a, **k: harness.calls.append(  # type: ignore[method-assign]
            ("announce", time.monotonic())
        )
    return harness


def transition(harness: Harness) -> None:
    runtime = harness.runtime
    harness.t0 = time.monotonic()
    runtime._kids_host_transition("ispy", runtime._kids_generation)


def test_hosted_ispy_search_plays_see_clip_flushed_before_clue(monkeypatch, tmp_path) -> None:
    harness = hosted(monkeypatch, tmp_path, search_seconds=0.7)
    picks = chosen(harness.runtime)
    transition(harness)
    pushes = harness.media.pushes()
    assert picks == [SEE] and pushes
    announce = harness.at("announce") + harness.t0
    assert clears_between(harness, pushes[-1][1], announce)  # settled before the clue handoff
    assert "record" not in [label for label, _ in harness.calls]
    assert ack_threads() == []


def test_fast_hosted_search_is_silent(monkeypatch, tmp_path) -> None:
    harness = hosted(monkeypatch, tmp_path, search_seconds=0.02)
    transition(harness)
    assert harness.media.pushes() == []
    assert not any(label == "proof" for label, _ in harness.calls)
    assert "announce" in [label for label, _ in harness.calls]


@pytest.mark.parametrize("boundary", [*BOUNDARIES, "switch"])
def test_cancel_during_held_hosted_proof_prevents_late_audio(monkeypatch, tmp_path, boundary) -> None:
    gate = threading.Event()
    harness = hosted(monkeypatch, tmp_path, search_seconds=0.8, proof_gate=lambda: gate.wait(3))
    runtime = harness.runtime

    def cancel_in_proof() -> None:
        assert wait_for(lambda: any(label == "proof" for label, _ in harness.calls))
        if boundary == "switch":  # the game master replaces I Spy with another game
            runtime._kids_host_transition("story", runtime._kids_generation)
        else:
            cancel_any(runtime, boundary)
        gate.set()

    threading.Thread(target=cancel_in_proof, daemon=True).start()
    transition(harness)
    time.sleep(0.3)
    assert any(label == "proof" for label, _ in harness.calls)  # the race really hit the proof read
    assert harness.media.pushes() == []
    assert ack_threads() == []


def test_hung_push_in_hosted_search_blocks_clue_at_common_quarantine(monkeypatch, tmp_path) -> None:
    harness = hosted(monkeypatch, tmp_path, search_seconds=0.8, real_queue=True)
    runtime = harness.runtime
    release = threading.Event()
    harness.media.on_push = lambda: release.wait(10)
    runtime._announcement_worker = object()  # real queue_announcement; worker driven below
    worker = threading.Thread(target=runtime._run_announcement_worker, name="test-announce", daemon=True)
    try:
        transition(harness)
        assert acks.output_quarantined(runtime)
        worker.start()
        assert wait_for(
            lambda: runtime._status.announcement_last_error == "Audio output is busy; announcement not played", 3,
        )
        assert len(harness.media.pushes()) == 1 and "answer" not in [label for label, _ in harness.calls]
    finally:
        release.set()
        runtime.stop_event.set()
    assert wait_for(lambda: not acks.output_quarantined(runtime), 2)
    worker.join(2)


def test_typed_ispy_guess_stays_silent_even_when_reply_is_slow(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(HermesVoiceRuntime, "_prepare_ispy_round", lambda self, *_a, **_k: SimpleNamespace())
    harness = Harness(monkeypatch, tmp_path, activity="ispy")
    with_bridge(harness, chat_seconds=0.6)
    harness.runtime._invalidate_ispy_input = lambda: None  # type: ignore[method-assign]
    harness.runtime._ispy_typed_session_is_current = lambda _sid: True  # type: ignore[method-assign]
    harness.runtime._accept_wake_turn()
    harness.runtime._run_conversation(harness.config, typed_input="een stoel")
    assert "chat_end" in [label for label, _ in harness.calls]
    assert harness.media.pushes() == []  # explicit choice: typed guesses get no waiting speech
