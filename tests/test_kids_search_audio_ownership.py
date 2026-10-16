"""Search ownership must outlive filler PCM and prevent later cleanup cutting newer speech."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from test_kids_interaction_fixes import profile_for
from test_kids_stt_acknowledgements import wait_for
from test_kids_waiting_speech import hosted, initial_start, transition

from homebody import kids_acknowledgements as acks
from homebody.config import AppConfig


@pytest.fixture(autouse=True)
def quick_waits(monkeypatch):
    monkeypatch.setattr(acks, "SEARCH_WAIT_DELAY_SECONDS", 0.02)
    monkeypatch.setattr(acks, "REPLY_WAIT_DELAY_SECONDS", 0.02)


def held_search(monkeypatch, tmp_path, kind):
    h = (hosted(monkeypatch, tmp_path, search_seconds=0.0) if kind == "hosted"
         else initial_start(monkeypatch, tmp_path, search_seconds=0.0))
    h.stt_seconds = 0.01
    entered, release = threading.Event(), threading.Event()
    errors = []

    def search(*_args, **_kwargs):
        entered.set()
        assert release.wait(5), "test search was not released"
        return SimpleNamespace(clue=lambda _language: "Ik zie iets blauws")

    h.runtime._prepare_ispy_round = search

    def run():
        try:
            if kind == "hosted":
                transition(h)
            else:
                h.runtime.start_kids_mode(profile_for("ispy", "nl"))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert entered.wait(2)
    assert wait_for(lambda: bool(h.media.pushes()), 2)
    return h, release, worker, errors


@pytest.mark.parametrize("kind", ["initial", "hosted"])
@pytest.mark.parametrize("window", ["queued_pcm", "thread_finished"])
def test_search_excludes_concurrent_wake_and_microphone_until_final_settle(monkeypatch, tmp_path, kind, window):
    h, release, worker, errors = held_search(monkeypatch, tmp_path, kind)
    runtime = h.runtime
    try:
        player = runtime._kids_ack_producer
        if window == "thread_finished":
            assert wait_for(lambda: not player.running, 2)
        else:
            assert player.queued_until > time.monotonic()
        assert not player.inflight  # reproduce the gap beyond the old push-only quarantine
        runtime._accept_wake_turn()
        cfg = runtime._kids_voice_config(h.config)
        runtime._run_selected_voice_conversation(cfg, "button")
        assert not any(label in {"record", "answer"} for label, _ in h.calls)
        assert acks.output_quarantined(runtime), "search still owns a future audio clear"
    finally:
        release.set()
        worker.join(3)
    assert not errors and not worker.is_alive()
    assert not acks.output_quarantined(runtime)
    runtime._kids_host_handle_transcript = lambda *_a, **_k: False
    runtime._kids_host_after_reply = lambda *_a, **_k: False
    runtime._accept_wake_turn()
    runtime._run_selected_voice_conversation(runtime._kids_voice_config(h.config), "button")
    assert any(label == "record" for label, _ in h.calls)  # healthy later microphone admission
    assert any(label == "answer" for label, _ in h.calls)


@pytest.mark.parametrize("kind", ["initial", "hosted"])
def test_finished_filler_cannot_clear_a_newer_approved_answer(monkeypatch, tmp_path, kind):
    h, release, worker, errors = held_search(monkeypatch, tmp_path, kind)
    runtime = h.runtime
    answer_entered, answer_release = threading.Event(), threading.Event()
    bad_clears = []

    def clear():
        if answer_entered.is_set() and not answer_release.is_set():
            bad_clears.append(time.monotonic())
        h.media.events.append(("clear", time.monotonic(), 0))

    def answer(*_a, **_k):
        answer_entered.set()
        assert answer_release.wait(3)
        return False

    h.media.audio.clear_player = clear
    runtime._play_kids_stream = answer
    runtime._kids_host_handle_transcript = lambda *_a, **_k: False
    runtime._kids_host_after_reply = lambda *_a, **_k: False
    newer = None
    try:
        assert wait_for(lambda: not runtime._kids_ack_producer.running, 2)
        runtime._accept_wake_turn()
        cfg = runtime._kids_voice_config(h.config)
        newer = threading.Thread(target=runtime._run_selected_voice_conversation, args=(cfg, "button"), daemon=True)
        newer.start()
        answer_entered.wait(0.8)  # rejected, not merely delayed until the old producer exits
        release.set()
        worker.join(2)
        assert not bad_clears, "original search cleared a newer active answer"
        assert not answer_entered.is_set(), "new answer was admitted before search relinquished its clear ownership"
    finally:
        release.set()
        answer_release.set()
        worker.join(3)
        if newer is not None:
            newer.join(3)
    assert not errors and not worker.is_alive()


@pytest.mark.parametrize("kind", ["initial", "hosted"])
def test_search_lease_blocks_adult_and_direct_pipeline_while_proof_is_pending(monkeypatch, tmp_path, kind):
    h = (hosted(monkeypatch, tmp_path, search_seconds=0.0) if kind == "hosted"
         else initial_start(monkeypatch, tmp_path, search_seconds=0.0))
    runtime = h.runtime
    proof_entered, proof_release, search_release = threading.Event(), threading.Event(), threading.Event()
    original = runtime._new_bridge_client

    def factory(cfg):
        client = original(cfg)
        read = client.kids_voice_proof

        def proof(**kwargs):
            proof_entered.set()
            assert proof_release.wait(3)
            return read(**kwargs)

        client.kids_voice_proof = proof
        return client

    runtime._new_bridge_client = factory
    runtime._prepare_ispy_round = lambda *_a, **_k: (
        search_release.wait(4), SimpleNamespace(clue=lambda _language: "Ik zie iets blauws")
    )[1]
    errors = []

    def start():
        try:
            if kind == "hosted":
                transition(h)
            else:
                runtime.start_kids_mode(profile_for("ispy", "nl"))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=start, daemon=True)
    worker.start()
    try:
        assert proof_entered.wait(2)
        assert acks.output_quarantined(runtime)
        assert runtime._voice_activity_lock.locked()
        assert h.media.pushes() == []
        runtime._accept_wake_turn()
        runtime._run_conversation(runtime._kids_voice_config(h.config))
        assert not any(label in {"record", "answer"} for label, _ in h.calls)
        adult_calls = []
        runtime._run_live_conversation = lambda *_a: adult_calls.append("live")
        runtime._run_selected_voice_conversation(AppConfig(api_key="k", conversation_mode="live"), "button")
        assert adult_calls == []
    finally:
        proof_release.set()
        search_release.set()
        worker.join(3)
    assert not errors and not worker.is_alive()


def test_stop_while_waiting_for_previous_voice_owner_never_starts_search(monkeypatch, tmp_path):
    h = initial_start(monkeypatch, tmp_path, search_seconds=0.0)
    runtime = h.runtime
    searches, errors = [], []
    runtime._prepare_ispy_round = lambda *_a, **_k: searches.append("search")
    owned_entered = threading.Event()
    original = runtime._prepare_owned_ispy_search

    def owned(*args, **kwargs):
        owned_entered.set()  # the Start receipt has been captured before voice-lock admission
        return original(*args, **kwargs)

    runtime._prepare_owned_ispy_search = owned
    runtime._voice_activity_lock.acquire()

    def start():
        try:
            runtime.start_kids_mode(profile_for("ispy", "nl"))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=start, daemon=True)
    worker.start()
    try:
        assert owned_entered.wait(2)
        runtime._request_conversation_stop()
    finally:
        runtime._voice_activity_lock.release()
        worker.join(3)
    assert not worker.is_alive() and errors
    assert searches == [] and h.media.pushes() == []


@pytest.mark.parametrize("kind", ["initial", "hosted"])
def test_outstanding_cancel_clear_stays_quarantined_after_search_settle(monkeypatch, tmp_path, kind):
    h, release, worker, errors = held_search(monkeypatch, tmp_path, kind)
    runtime = h.runtime
    first_clear_entered, old_clear_release = threading.Event(), threading.Event()
    answer_entered, answer_release = threading.Event(), threading.Event()
    clear_lock = threading.Lock()
    clear_count = 0
    bad_clears = []

    def clear():
        nonlocal clear_count
        with clear_lock:
            clear_count += 1
            index = clear_count
        if index == 1:  # cancellation watcher: opaque SDK clear remains outstanding
            first_clear_entered.set()
            assert old_clear_release.wait(5)
            if answer_entered.is_set() and not answer_release.is_set():
                bad_clears.append("old clear completed during newer answer")
        h.media.events.append(("clear", time.monotonic(), 0))

    def answer(*_a, **_k):
        answer_entered.set()
        assert answer_release.wait(3)
        return False

    h.media.audio.clear_player = clear
    runtime._play_kids_stream = answer
    runtime._kids_host_handle_transcript = lambda *_a, **_k: False
    runtime._kids_host_after_reply = lambda *_a, **_k: False
    newer = None
    try:
        runtime._request_conversation_stop()
        assert first_clear_entered.wait(2)
        release.set()  # settle's second clear returns, but the first is still held
        worker.join(2)
        assert not worker.is_alive() and clear_count >= 2
        runtime._accept_wake_turn()
        cfg = runtime._kids_voice_config(h.config)
        newer = threading.Thread(target=runtime._run_selected_voice_conversation, args=(cfg, "button"), daemon=True)
        newer.start()
        answer_entered.wait(0.8)
        old_clear_release.set()
        assert wait_for(lambda: not runtime._kids_ack_producer.running, 2)
        assert not bad_clears, "bounded settle released quarantine while an old SDK clear survived"
        assert not answer_entered.is_set(), "newer speech admitted during an outstanding clear"
        assert not acks.output_quarantined(runtime)
        answer_release.set()
        runtime._run_selected_voice_conversation(runtime._kids_voice_config(h.config), "button")
        assert answer_entered.is_set()  # a deliberately later turn succeeds after the clear is done
    finally:
        release.set()
        old_clear_release.set()
        answer_release.set()
        worker.join(3)
        if newer is not None:
            newer.join(3)
    assert not errors
