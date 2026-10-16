"""Independent regressions for the final release review's five adverse boundaries."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_bridge import load_bridge_module
from test_companion import calendar_offer, make_runtime, offers_runtime, someone_present
from test_companion_bridge import FakeHttp, FakeRequest
from test_companion_realtime_consent import camera, transcript

from homebody.realtime_client import RealtimeBridgeSession, RealtimeEvent


def test_queued_live_look_cannot_adopt_a_stop_arriving_during_input_read(monkeypatch):
    runtime, client, captures = make_runtime(conversation_mode="live")
    runtime._motion = None
    runtime._read_16k_frame = lambda: (runtime._request_conversation_stop(), None)[1]
    pending, sent = [], []

    class DeferredThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            pending.append(self.target)

    monkeypatch.setattr(threading, "Thread", DeferredThread)
    session = SimpleNamespace(events=lambda: [], send_look_result=lambda *a, **k: sent.append((a, k)))
    try:
        with runtime._voice_activity_lock:
            runtime._live_loop(
                runtime.config_loader(),
                session,
                [RealtimeEvent("bridge.look_request", {"delegation_id": "old-look", "question": "Read this letter"})],
                "everyday",
                None,
            )
            assert runtime._turn_stop_requested()
            for work in pending:
                work()
        assert captures == []
        assert client.looks == []
        assert sent == []
    finally:
        runtime._companion_teardown("test_cleanup")


@pytest.mark.parametrize("operation", ["image", "follow_up"])
def test_stop_during_client_preparation_prevents_private_submission(operation):
    runtime, client, captures = make_runtime()
    if operation == "follow_up":
        runtime.look_at_this("Read this letter")

    def prepared_after_stop(_config):
        runtime._request_conversation_stop()
        return client

    runtime._new_bridge_client = prepared_after_stop
    try:
        with pytest.raises(RuntimeError):
            if operation == "image":
                runtime.look_at_this("Read this letter")
            else:
                runtime.follow_up_look("What day?")
        assert len(client.looks) == (1 if operation == "follow_up" else 0)
        assert client.follow_ups == []
        assert runtime._look_context is None
        assert captures == [1]
    finally:
        runtime._companion_teardown("test_cleanup")


def test_stop_during_accepted_calendar_bookkeeping_cannot_restore_speech_or_private_ram():
    runtime, _, _ = offers_runtime()
    someone_present(runtime)
    try:
        queued = runtime.submit_contextual_offer(calendar_offer())
        token = queued["token"]
        runtime._announcement_queue.get_nowait()
        runtime._contextual_offers.mark_spoken(token)
        runtime._initiative.record_welcomed = lambda *_a, **_kw: runtime.companion_stop()
        runtime.respond_to_contextual_offer(token, "yes")
        assert runtime._turn_stop_requested()
        assert runtime._announcement_queue.empty()
        assert runtime.companion_owner_status()["private_details"] == []
    finally:
        runtime._companion_teardown("test_cleanup")


def test_realtime_visual_worker_cannot_emit_a_continuation_after_barge_in(monkeypatch):
    runtime, client, captures = make_runtime(conversation_mode="realtime")
    runtime._motion = None
    runtime._play_asset = lambda _name: None
    runtime._discard_audio = lambda _seconds: None
    runtime._read_16k_frame = lambda: None
    client.release.clear()
    wire, phases = [], [0]
    done = threading.Event()
    real_thread = threading.Thread

    class ObservedThread(real_thread):
        def __init__(self, *, target, **kwargs):
            def run_target():
                try:
                    target()
                finally:
                    done.set()

            super().__init__(target=run_target, **kwargs)

    monkeypatch.setattr(threading, "Thread", ObservedThread)
    session = SimpleNamespace(
        start=lambda: None,
        close=lambda: None,
        _socket=SimpleNamespace(send=lambda text: wire.append(json.loads(text))),
    )
    session.send_tool_result = lambda *a, **k: RealtimeBridgeSession.send_tool_result(session, *a, **k)

    def events():
        phases[0] += 1
        if phases[0] == 1:
            return [transcript("Read this letter"), camera()]
        if phases[0] == 2:
            for _ in range(500):
                if client.looks:
                    break
                threading.Event().wait(0.005)
            assert client.looks, "vision worker did not reach the held request"
            return [RealtimeEvent("input_audio_buffer.speech_started", {})]
        if phases[0] == 3:
            return [RealtimeEvent("input_audio_buffer.speech_stopped", {})]
        client.release.set()
        assert done.wait(3), "held worker did not settle"
        runtime._request_conversation_stop()
        return []

    session.events = events
    runtime._new_realtime_session = lambda *_a, **_k: session
    try:
        with runtime._voice_activity_lock:
            runtime._run_realtime_conversation(runtime.config_loader())
        assert captures == [1]
        assert not [m for m in wire if m["type"] in {"conversation.item.create", "response.create"}]
    finally:
        client.release.set()
        runtime._companion_teardown("test_cleanup")


def test_timed_out_offer_cannot_be_admitted_after_successful_source_removal(tmp_path, monkeypatch):
    import homebody.main as main

    runtime, _, _ = offers_runtime()
    someone_present(runtime)
    old_revision, new_revision = "a" * 32, "b" * 32
    runtime.config_loader().companion_sources_revision = old_revision
    monkeypatch.setattr(main, "default_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(main, "load_config", runtime.config_loader)
    robot = main.Homebody()
    robot._runtime = runtime
    api = TestClient(robot.settings_app)
    module = load_bridge_module()
    monkeypatch.setattr(
        module,
        "_resolve_secret",
        lambda name, *_: {
            "HASS_URL": "http://fake-ha.invalid",
            "HASS_TOKEN": "fixture",
        }.get(name, ""),
    )
    bridge = module.Bridge(api_key="bridge-secret", hermes_url="http://127.0.0.1:1", profile=None)
    bridge._sources_path = str(tmp_path / "enrollment.json")
    bridge._offer_url = "https://fake-robot.invalid/api/initiative/offers"
    document = {
        "source_revision": old_revision,
        "home_assistant": [
            {
                "id": "washer",
                "label": "Washer",
                "entity_id": "binary_sensor.washer_done",
                "condition": "state_equals",
                "value": "on",
                "offer_text": "The washer is finished; would you like its status?",
                "accepted_text": "It reports done. Nothing was changed.",
                "min_interval_seconds": 1800,
            }
        ],
        "calendar": [],
    }
    from pathlib import Path

    Path(bridge._sources_path).write_text(json.dumps(document))
    http = FakeHttp()
    http.source_revision = old_revision
    http.states["binary_sensor.washer_done"] = {"state": "on", "last_changed": "fixture-moment"}
    bridge.http = http
    pending = []

    class TimedOutResponse:
        async def __aenter__(self):
            raise TimeoutError("request sent; response timeout")

        async def __aexit__(self, *_args):
            pass

    def post(_url, **kwargs):
        pending.append(json.loads(json.dumps(kwargs["json"])))
        return TimedOutResponse()

    http.post = post

    async def scenario():
        with pytest.raises(TimeoutError):
            await bridge._poll_sources_once()
        # Owner enrollment rotates durable robot authority BEFORE contacting the host.
        runtime.config_loader().companion_sources_revision = new_revision
        runtime.cancel_contextual_offer("sources_changed")
        removed = await bridge.companion_sources_enroll(
            FakeRequest(
                {
                    "source_revision": new_revision,
                    "home_assistant": [],
                    "calendar": [],
                }
            )
        )
        runtime.cancel_contextual_offer("sources_changed")
        assert removed.status == 200 and runtime._announcement_queue.empty()
        response = api.post(
            "/api/initiative/offers",
            json=pending[0],
            headers={
                "Authorization": "Bearer " + runtime.config_loader().api_key,
            },
        )
        assert response.status_code == 409
        assert runtime._announcement_queue.empty()
        assert runtime.companion_owner_status()["private_details"] == []

    try:
        asyncio.run(scenario())
    finally:
        runtime._companion_teardown("test_cleanup")
