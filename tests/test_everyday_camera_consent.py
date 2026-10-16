"""Camera consent is the person's own words, never the model's tool choice.

Real Pipeline/Live loops, the real host Everyday model loop, authenticated loopback HTTP/WebSocket
and local RPC correlation, and the real runtime callback gate. Only model output, SDK capture,
STT/TTS/playback and the vision answer are synthetic; no provider, camera or credential is used.
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from test_companion import make_runtime as companion_runtime
from test_everyday_local_transport import run_host
from test_everyday_workspace_bridge import HEADERS, ctx
from test_live_transport import FakeLive, delta

from companion import live_transport
from companion.everyday_manifest import local_tool_specs
from homebody.audio import EndpointResult
from homebody.config import AppConfig
from homebody.everyday_runtime import EverydayCallbackScope
from homebody.hermes_client import AgentBrokerContext, HermesBridgeClient
from homebody.look_intent import looks_like_look_request, requests_camera_session
from homebody.realtime_client import RealtimeEvent
from homebody.runtime import HermesVoiceRuntime

NON_CONSENTING = [
    "Do not look at this letter",
    "What does 'look at this' mean?",
    "Read the project roadmap",
    "How tall is the Eiffel tower?",
]
MODEL_CAMERA_BATCH = [
    ("capture_reachy_camera", {"purpose": "Read this letter"}),
    ("start_reachy_camera", {"duration_seconds": 30}),
    ("get_reachy_volume", {}),
]


class Model:
    """Synthetic fast-model output: it selects camera tools whatever the person said."""

    name = "openai-codex"
    model = staticmethod(lambda role: "fixture")
    request = staticmethod(lambda body: body)

    def __init__(self, rounds):
        self.rounds = [list(batch) for batch in rounds]
        self.requests: list[dict] = []

    async def complete(self, body, **kwargs):
        self.requests.append(copy.deepcopy(body))
        batch = self.rounds.pop(0) if self.rounds else []
        if not batch:
            return {"choices": [{"message": {"content": "Fixture reply."}}]}
        calls = [
            {
                "id": f"call-{len(self.requests)}-{index}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for index, (name, args) in enumerate(batch)
        ]
        return {"choices": [{"message": {"content": None, "tool_calls": calls}}]}

    def tool_results(self) -> dict[str, dict]:
        """Results the host returned to the model, by tool name (latest wins)."""
        names = {}
        results = {}
        for body in self.requests:
            for message in body["messages"]:
                for call in message.get("tool_calls") or []:
                    names[call["id"]] = call["function"]["name"]
                if message.get("role") == "tool":
                    results[names[message["tool_call_id"]]] = json.loads(message["content"])
        return results

    def saw_image(self) -> bool:
        return any(isinstance(message.get("content"), list) for body in self.requests for message in body["messages"])


def robot_runtime(**settings) -> tuple[HermesVoiceRuntime, AppConfig, list[int]]:
    config = AppConfig(**settings)
    runtime = HermesVoiceRuntime(
        SimpleNamespace(media=SimpleNamespace()), threading.Event(), config_loader=lambda: config
    )
    runtime._control_ready.set()
    runtime._power_mode = "awake"
    runtime._motors_enabled = True
    runtime._agent_session_generation = 27
    runtime._accept_wake_turn()
    runtime._speaker = SimpleNamespace(
        read=lambda: {"volume": 24, "verified": True}, set=lambda **kw: {**kw, "verified": True}
    )
    captures: list[int] = []
    runtime._capture_camera_jpeg = lambda **kw: captures.append(1) or b"synthetic jpeg"
    runtime._everyday_describe_frame = lambda *a, **k: pytest.fail("no generic image analysis")
    return runtime, config, captures


# --- the person's own words ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Turn on the camera for 30 seconds",
        "Start the camera",
        "Hey Reachy, can you switch the camera on for a minute?",
        "Zet de camera aan",
        "camera aan voor 2 minuten",
    ],
)
def test_camera_session_request_is_narrow_and_never_a_look(text):
    assert requests_camera_session(text)
    assert not looks_like_look_request(text)


@pytest.mark.parametrize(
    "text",
    [
        *NON_CONSENTING,
        "Don't turn on the camera",
        "Do not start the camera",
        "What does 'turn on the camera' mean?",
        "Turn on the camera and read the project plan",
        "Turn on the kitchen light",
        "Look at this letter",
        "Never switch the camera on",
    ],
)
def test_negated_quoted_nonphysical_and_ordinary_words_do_not_start_the_camera(text):
    assert not requests_camera_session(text)


# --- Pipeline: actual conversation loop ----------------------------------------------------------


def pipeline_runtime(server, words, **settings):
    runtime, client, captures = companion_runtime(
        conversation_mode="pipeline", continuous_conversation=True, **settings
    )
    runtime._motion = None
    runtime._agent_session_generation = 27
    config = runtime.config_loader()
    client.config = config
    client.last_stt_provider = "fixture"
    client.health = lambda: {"status": "ok"}
    client.transcribe = lambda _audio: words.pop(0)
    client.synthesize = lambda _text: SimpleNamespace(provider="fixture")
    bridge_config = AppConfig(
        bridge_url=str(server.make_url("/")).rstrip("/"), api_key="bridge-secret", instance_id="reachy-1"
    )

    def everyday_turn(transcript, *, context=None, local_tools=None, local_executor=None, is_current=lambda: True):
        # The real robot bridge client and local RPC; the wire context matches the host fixture lease.
        client.turns.append(transcript)
        real = HermesBridgeClient(bridge_config)
        try:
            return real.everyday_turn(
                transcript,
                context=AgentBrokerContext(**ctx(27, "conversation")),
                local_tools=local_tools,
                local_executor=local_executor,
                is_current=is_current,
            )
        finally:
            real.close()

    client.everyday_turn = everyday_turn
    runtime._play_asset = lambda _name: None
    runtime._discard_audio = lambda _seconds: None
    runtime._record_utterance = lambda _config: EndpointResult(
        np.ones(160, dtype=np.float32) if words else np.empty(0, dtype=np.float32),
        bool(words),
        "end_silence" if words else "initial_timeout",
        0.01,
    )
    spoken: list[str] = []
    runtime._play_response = lambda _speech, text, **kwargs: spoken.append(text) or False

    def converse():
        with runtime._voice_activity_lock:
            runtime._run_conversation(config)

    return runtime, client, captures, config, spoken, converse


@pytest.mark.parametrize("transcript", NON_CONSENTING)
def test_pipeline_model_selected_capture_or_start_never_captures(transcript):
    async def main(bridge, server, module):
        model = Model([MODEL_CAMERA_BATCH])
        bridge.everyday_llm = model
        bridge._everyday_key = lambda: "fixture-only"
        words = [transcript]
        runtime, client, captures, config, spoken, converse = pipeline_runtime(server, words, camera_enabled=True)
        runtime._speaker = SimpleNamespace(read=lambda: {"volume": 24, "verified": True})
        await asyncio.wait_for(asyncio.to_thread(converse), 10)
        assert words == [] and client.turns == [transcript]
        assert captures == [] and runtime._status.camera_captures == 0
        assert client.looks == [] and not model.saw_image()
        results = model.tool_results()
        assert "nothing was captured" in results["capture_reachy_camera"]["error"]
        assert "nothing was started" in results["start_reachy_camera"]["error"]
        # Ordinary Everyday tools outside visual scope keep working in the same batch.
        assert results["get_reachy_volume"] == {"ok": True, "volume": 24, "verified": True}
        assert not runtime.everyday_local_capabilities()["activity"]["camera_session_active"]
        assert spoken == ["Fixture reply."]

    run_host(main)


def test_pipeline_robot_refuses_model_camera_callbacks_even_if_a_host_dispatches_them():
    runtime, client, captures = companion_runtime(conversation_mode="pipeline", continuous_conversation=False)
    runtime._motion = None
    config = runtime.config_loader()
    client.config = config
    client.last_stt_provider = "fixture"
    client.health = lambda: {"status": "ok"}
    words = ["Do not look at this letter"]
    client.transcribe = lambda _audio: words.pop(0)
    client.synthesize = lambda _text: SimpleNamespace(provider="fixture")
    results = []

    def hostile_host(transcript, *, context=None, local_tools=None, local_executor=None, is_current=lambda: True):
        assert {"capture_reachy_camera", "start_reachy_camera"} <= set(local_tools)
        results.append(local_executor("capture_reachy_camera", {"purpose": "Read this letter"}, "c1"))
        results.append(local_executor("start_reachy_camera", {"duration_seconds": 30}, "c2"))
        return "Host reply."

    client.everyday_turn = hostile_host
    runtime._play_asset = lambda _name: None
    runtime._discard_audio = lambda _seconds: None
    runtime._record_utterance = lambda _config: EndpointResult(
        np.ones(160, dtype=np.float32), True, "end_silence", 0.01
    )
    runtime._play_response = lambda _speech, text, **kwargs: False
    with runtime._voice_activity_lock:
        runtime._run_conversation(config)
    assert [r["ok"] for r in results] == [False, False]
    assert "nothing was captured" in results[0]["error"] and "nothing was started" in results[1]["error"]
    assert captures == [] and not runtime.everyday_local_capabilities()["activity"]["camera_session_active"]


def test_pipeline_explicit_window_then_one_frame_look_and_tool_free_follow_up():
    async def main(bridge, server, module):
        model = Model([[("start_reachy_camera", {"duration_seconds": 30})]])
        bridge.everyday_llm = model
        bridge._everyday_key = lambda: "fixture-only"
        words = ["Turn on the camera for 30 seconds", "Look at this letter", "And what day?"]
        runtime, client, captures, config, spoken, converse = pipeline_runtime(server, words, camera_enabled=True)
        await asyncio.wait_for(asyncio.to_thread(converse), 10)
        assert words == []
        assert model.tool_results()["start_reachy_camera"]["camera_session_active"] is True
        assert len(model.requests) == 2 and not model.saw_image()
        # Window start is a local readiness probe only; exactly one look frame was analysed.
        assert captures == [1, 1] and len(client.looks) == 1 and len(client.follow_ups) == 1
        assert client.turns == ["Turn on the camera for 30 seconds"]
        assert spoken == ["Fixture reply.", client.answer, "Friday at ten, according to what I read."]
        assert config.camera_enabled is True

    run_host(main)


# --- Live: actual authenticated relay and host model loop ----------------------------------------


@pytest.mark.parametrize("transcript", NON_CONSENTING[:2])
def test_live_model_selected_capture_never_reaches_robot_or_vision(monkeypatch, transcript):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")

    async def main(bridge, client, module):
        async def script(ws, received):
            await ws.send_json(delta("user", 0, 500, transcript))
            await ws.send_json(
                {
                    "type": "session.delegation.created",
                    "offset_ms": 500,
                    "delegation": {"id": "del-1", "target": "client"},
                }
            )

        fake = FakeLive(script)
        model = Model([MODEL_CAMERA_BATCH])
        bridge.everyday_llm = model
        bridge._everyday_key = lambda: "fixture-only"
        runtime, _config, captures = robot_runtime(camera_enabled=True)
        fence = runtime.everyday_tool_fence()
        async with live_server(monkeypatch, bridge, fake):
            events = []
            async with client.ws_connect("/v1/live", headers=HEADERS) as ws:
                await ws.send_json(live_start([name for name, _args in MODEL_CAMERA_BATCH]))
                async with asyncio.timeout(5):
                    while True:
                        payload = await ws.receive_json()
                        events.append(payload)
                        if payload["type"] == "everyday.tool_call":
                            # The robot side: the real callback gate with this turn's actual words.
                            result = await asyncio.to_thread(
                                runtime.execute_everyday_callback,
                                payload["name"],
                                payload["arguments"],
                                scope=EverydayCallbackScope(transcript=transcript),
                                fence=fence,
                                call_id=payload["call_id"],
                            )
                            await ws.send_json(
                                {"type": "everyday.tool_result", "call_id": payload["call_id"], "result": result}
                            )
                        elif payload.get("state") in {"answered", "failed"}:
                            break
                await ws.send_json({"type": "session.close"})
        assert [p["name"] for p in events if p["type"] == "everyday.tool_call"] == ["get_reachy_volume"]
        assert not any(p["type"].startswith("bridge.look") for p in events)
        assert captures == [] and not model.saw_image()
        results = model.tool_results()
        assert results["capture_reachy_camera"]["ok"] is False and results["start_reachy_camera"]["ok"] is False
        assert results["get_reachy_volume"]["ok"] is True

    run_host(main)


def live_start(names):
    return {
        "type": "session.start",
        "assistant_mode": "everyday",
        "model": "gpt-live-1",
        "voice": "marin",
        "agent_context": ctx(27, "conversation"),
        "local_tools": names,
    }


class live_server:
    """Point the real bridge Live route at the scripted loopback provider."""

    def __init__(self, monkeypatch, bridge, fake):
        self.monkeypatch, self.bridge, self.fake = monkeypatch, bridge, fake

    async def __aenter__(self):
        from aiohttp.test_utils import TestServer

        self.upstream = TestServer(self.fake.app())
        await self.upstream.start_server()
        real_serve = live_transport.serve_live
        url = str(self.upstream.make_url("/v1/live/sessions")).replace("http", "ws", 1)

        async def relay(request, **kwargs):
            return await real_serve(request, upstream_url=url, **kwargs)

        self.monkeypatch.setitem(self.bridge.live.__func__.__globals__, "serve_live", relay)
        return self

    async def __aexit__(self, *exc):
        await self.upstream.close()


class LiveRobotSession:
    """Robot-side Live session fixture: a look request, then model callbacks in the same session."""

    def __init__(self, runtime, calls):
        self.runtime, self.calls = runtime, calls
        self.started = time.monotonic()
        self.emitted = False
        self.closed = False
        self.results: list[tuple[str, dict]] = []
        self.looks: list[dict] = []

    def start(self):
        return None

    def wait_started(self):
        return []

    def send_audio(self, _):
        return None

    def send_everyday_result(self, identifier, result):
        self.results.append((identifier, result))

    def send_look_result(self, delegation_id, **result):
        self.looks.append({"delegation_id": delegation_id, **result})

    def events(self):
        done = self.looks and len(self.results) == len(self.calls)
        if done or time.monotonic() - self.started > 3:
            self.runtime._request_conversation_stop()
            return []
        if self.emitted:
            return []
        self.emitted = True
        look = RealtimeEvent(
            "bridge.look_request",
            {"type": "bridge.look_request", "delegation_id": "d1", "question": "Look at this letter"},
        )
        callbacks = [
            RealtimeEvent(
                "everyday.tool_call",
                {"type": "everyday.tool_call", "call_id": f"after-look-{i}", "name": name, "arguments": args},
            )
            for i, (name, args) in enumerate(self.calls)
        ]
        return [RealtimeEvent("session.input_transcript.delta", {"delta": "Look at this letter"}), look, *callbacks]

    def close(self):
        self.closed = True


def test_live_robot_refuses_every_callback_after_a_look_entered_the_session():
    runtime, client, captures = companion_runtime(conversation_mode="live")
    runtime._motion = None
    runtime._accept_wake_turn()
    runtime._play_asset = lambda *_: None
    runtime._discard_audio = lambda *_: None
    runtime._clear_streamed_audio = lambda: None
    runtime._speaker = SimpleNamespace(
        read=lambda: pytest.fail("status/volume after visual analysis"),
        set=lambda **kw: pytest.fail("volume change after visual analysis"),
    )
    calls = [
        ("set_reachy_volume", {"volume": 10}),
        ("get_reachy_status", {}),
        ("capture_reachy_camera", {"purpose": "Read it again"}),
    ]
    session = LiveRobotSession(runtime, calls)
    runtime._new_live_session = lambda *a, **k: session

    def read():
        time.sleep(0.005)
        return np.zeros(160, dtype=np.float32)

    runtime._read_16k_frame = read
    with runtime._voice_activity_lock:
        runtime._run_live_conversation(runtime.config_loader())
    assert session.closed
    assert [identifier for identifier, _ in session.results] == ["after-look-0", "after-look-1", "after-look-2"]
    assert all(r["ok"] is False and "tool-free" in r["error"] for _, r in session.results)
    # The explicit look itself still works: one frame, one bounded text answer.
    assert session.looks == [{"delegation_id": "d1", "text": client.answer}]
    assert captures == [1] and len(client.looks) == 1


# --- host model loop: same batch and later rounds after image analysis ---------------------------


@pytest.mark.parametrize(
    "earlier,current,admitted",
    [
        ("user: Do not look at anything", "Turn on the camera for 30 seconds", True),
        ("user: Turn on the camera for 30 seconds", "Do not turn on the camera", False),
    ],
)
def test_host_camera_window_consent_is_the_current_words_not_earlier_context(earlier, current, admitted):
    async def main(bridge, server, module):
        model = Model([[("start_reachy_camera", {"duration_seconds": 30})]])
        dispatched = []

        async def executor(name, arguments):
            dispatched.append(name)
            return {"ok": True, "camera_session_active": True}

        await bridge.everyday.answer(
            f"Earlier conversation (context only, may be incomplete):\n{earlier}\n\nCurrent request:\n{current}",
            http=None,
            provider=model,
            key="fixture-only",
            device_id="reachy-1",
            generation=27,
            local_tools=local_tool_specs(["start_reachy_camera"]),
            local_executor=executor,
            consent_text=current,
        )
        assert dispatched == (["start_reachy_camera"] if admitted else [])

    run_host(main)


@pytest.mark.parametrize("executor_kind", ["synthetic_analysis", "real_robot"])
def test_host_loop_is_tool_free_after_camera_analysis_in_same_batch_and_later_rounds(executor_kind):
    async def main(bridge, server, module):
        model = Model(
            [
                [
                    ("capture_reachy_camera", {"purpose": "Read this letter"}),
                    ("get_reachy_status", {}),
                    ("set_reachy_volume", {"volume": 10}),
                ],
                [("set_reachy_power_mode", {"mode": "sleep"})],
            ]
        )
        dispatched = []
        runtime, _config, captures = robot_runtime(camera_enabled=True)
        fence = runtime.everyday_tool_fence()

        async def executor(name, arguments):
            dispatched.append(name)
            if executor_kind == "synthetic_analysis":
                return {"ok": True, "text": "A dentist letter about Friday."}
            return await asyncio.to_thread(
                runtime.execute_everyday_callback,
                name,
                arguments,
                scope=EverydayCallbackScope(transcript="Look at this letter"),
                fence=fence,
            )

        names = ["capture_reachy_camera", "get_reachy_status", "set_reachy_volume", "set_reachy_power_mode"]
        result = await bridge.everyday.answer(
            "Look at this letter",
            http=None,
            provider=model,
            key="fixture-only",
            device_id="reachy-1",
            generation=27,
            local_tools=local_tool_specs(names),
            local_executor=executor,
        )
        assert result["text"] == "Fixture reply."
        assert dispatched == ["capture_reachy_camera"]
        assert captures == [] and runtime._status.camera_captures == 0
        assert "tools" in model.requests[0]
        assert all("tools" not in body and "tool_choice" not in body for body in model.requests[1:])
        results = model.tool_results()
        for name in ("get_reachy_status", "set_reachy_volume", "set_reachy_power_mode"):
            assert results[name]["ok"] is False and "tool-free" in results[name]["error"]
        if executor_kind == "real_robot":
            assert "nothing was captured" in results["capture_reachy_camera"]["error"]

    run_host(main)
