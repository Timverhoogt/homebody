"""Independent final authorization regressions. All media/transports are fixture-only."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from test_companion import make_runtime


class Authority:
    def __init__(self, identity):
        self.identity = identity
        self.active = True

    def __call__(self):
        return self.active


@pytest.mark.parametrize("revoke", [False, True])
def test_completed_private_look_cannot_be_adopted_by_a_different_owner(revoke):
    runtime, client, captures = make_runtime()
    original = Authority("owner-a")
    runtime.look_at_this("Read this letter", authority=original)
    original.active = not revoke
    try:
        with pytest.raises(RuntimeError):
            runtime.follow_up_look("What day?", authority=Authority("owner-b"))
        assert not client.follow_ups
        assert captures == [1]
    finally:
        runtime._companion_teardown("test_cleanup")


def test_camera_revocation_prevents_uploading_existing_analysis_for_follow_up():
    runtime, client, _ = make_runtime()
    runtime.look_at_this("Read this letter")
    runtime.config_loader().camera_enabled = False
    try:
        with pytest.raises(RuntimeError):
            runtime.follow_up_look("What day?")
        assert not client.follow_ups
        assert runtime._look_context is None
    finally:
        runtime._companion_teardown("test_cleanup")


def test_stop_during_follow_up_preflight_is_not_rebased_into_new_authorization():
    runtime, client, _ = make_runtime()
    runtime.look_at_this("Read this letter")
    take = runtime._take_follow_up

    def held_take(question):
        context = take(question)
        runtime._request_conversation_stop()
        return context

    runtime._take_follow_up = held_take
    try:
        with pytest.raises(RuntimeError):
            runtime.follow_up_look("What day?")
        assert not client.follow_ups
        assert runtime._look_context is None
    finally:
        runtime._companion_teardown("test_cleanup")


def test_follow_up_does_not_detach_context_from_original_expiry_timer():
    runtime, _, _ = make_runtime()
    runtime.look_at_this("Read this letter")
    original_token = runtime._look_context.ticket_token
    runtime.follow_up_look("What day?")
    try:
        runtime._expire_look_context(original_token)
        assert runtime._look_context is None
    finally:
        runtime._companion_teardown("test_cleanup")


@pytest.mark.parametrize("engine", ["live", "realtime"])
def test_queued_visual_worker_cannot_rebase_after_stop_and_new_wake(monkeypatch, engine):
    runtime, client, captures = make_runtime(local_vision_enabled=True)
    pending, sent = [], []
    real_thread = threading.Thread

    class DeferredThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            pending.append(self.target)

    session = SimpleNamespace(
        send_look_result=lambda *a, **k: sent.append((a, k)),
        send_camera_description=lambda *a, **k: sent.append((a, k)),
        send_camera_error=lambda *a, **k: sent.append((a, k)),
    )
    runtime._voice_activity_lock.acquire()
    try:
        monkeypatch.setattr(threading, "Thread", DeferredThread)
        if engine == "live":
            runtime._start_live_look(session, "look-1", "Read this letter")
        else:
            runtime._describe_for_realtime(session, "look-1", "Read this letter", runtime.config_loader())
        monkeypatch.setattr(threading, "Thread", real_thread)
        assert len(pending) == 1
        runtime._request_conversation_stop()
        runtime._accept_wake_turn()
        pending[0]()
        assert captures == []
        assert client.looks == []
        assert sent == []
    finally:
        runtime._voice_activity_lock.release()
        runtime._companion_teardown("test_cleanup")
