"""Independent regressions for the actual Everyday callback camera bypass.

Synthetic SDK/model/media only; genuine client, local RPC and bridge routes.
The pre-fix independent receipt captured one image/inference on a negated request.
"""

import asyncio
import json
import socket
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestServer
from test_companion import make_runtime
from test_everyday_local_callbacks import Provider, call
from test_everyday_local_transport import run_host
from test_everyday_workspace_bridge import HEADERS, ctx
from test_live_transport import FakeLive, collect, delta

from companion import live_transport
from homebody.look_intent import looks_like_look_request

out = {}


@pytest.fixture(autouse=True)
def fixture_network_only(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        if (
            not isinstance(address, tuple)
            or address[0] not in {"127.0.0.1", "::1"}
            or address[1] in {8000, 8042, 6053, 8642, 8643}
        ):
            raise AssertionError("Only isolated fixture network is permitted")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    out.clear()


class VisionProvider(Provider):
    """Only model output is synthetic; real HTTPX image submission and bridge inference route."""

    def __init__(self, batches):
        super().__init__(batches)
        self.inferences = 0

    async def complete(self, body, **kwargs):
        if isinstance(body["messages"][-1]["content"], list):
            self.inferences += 1
            assert "tools" not in body
            return {"choices": [{"message": {"content": "Fixture private letter"}}]}
        return await super().complete(body, **kwargs)


def live_generic_camera_probe(words):
    async def scenario(bridge, server, module):
        runtime, look_client, captures = make_runtime(conversation_mode="live")
        config = runtime.config_loader()
        config.bridge_url = str(server.make_url("/")).rstrip("/")
        config.api_key = "bridge-secret"
        config.instance_id = "reachy-1"
        runtime._agent_session_generation = 27
        runtime._accept_wake_turn()
        fence = runtime.everyday_tool_fence()
        assert not looks_like_look_request(words)
        provider = VisionProvider(
            [
                [call("model-camera", "capture_reachy_camera", {"purpose": "Read this letter"})],
                [call("after-visual-status", "get_reachy_status", {})],
            ]
        )
        bridge.everyday_llm = provider
        bridge._everyday_key = lambda: "fixture-only"

        async def script(ws, received):
            await ws.send_json(delta("user", 0, 500, words))
            await ws.send_json(
                {
                    "type": "session.delegation.created",
                    "offset_ms": 500,
                    "delegation": {"id": "negated-1", "target": "client"},
                }
            )

        fake = FakeLive(script)
        upstream = TestServer(fake.app())
        await upstream.start_server()
        original_session = live_transport.ClientSession

        class Redirect(original_session):
            def ws_connect(self, target, **kwargs):
                assert target == live_transport.LIVE_URL
                return super().ws_connect(
                    str(upstream.make_url("/v1/live/sessions")).replace("http", "ws", 1), **kwargs
                )

        live_transport.ClientSession = Redirect
        events, calls = [], []
        try:
            async with server.ws_connect("/v1/live", headers=HEADERS) as ws:
                await ws.send_json(
                    {
                        "type": "session.start",
                        "model": "gpt-live-1",
                        "voice": "marin",
                        "timeout_seconds": 60,
                        "assistant_mode": "everyday",
                        "agent_context": ctx(27, "conversation"),
                        "local_tools": runtime.everyday_local_tools(),
                    }
                )
                async with asyncio.timeout(5):
                    while True:
                        event = await ws.receive_json()
                        events.append(event)
                        if event.get("type") == "everyday.tool_call":
                            calls.append(event["name"])
                            result = await asyncio.to_thread(
                                runtime.execute_everyday_tool,
                                event["name"],
                                event["arguments"],
                                fence=fence,
                                call_id=event["call_id"],
                            )
                            await ws.send_json(
                                {"type": "everyday.tool_result", "call_id": event["call_id"], "result": result}
                            )
                        if event.get("type") == "bridge.delegation" and event.get("state") in {"answered", "failed"}:
                            break
                await ws.send_json({"type": "session.close"})
                await collect(ws, events, lambda p: p.get("type") == "session.closed")
            out["live_negated_camera"] = {
                "transcript": words,
                "classifier_consents": False,
                "rpc_calls": calls,
                "captures": len(captures),
                "inferences": provider.inferences,
                "bounded_companion_looks": len(look_client.looks),
                "tool_results": [
                    {"call_id": m["tool_call_id"], "ok": json.loads(m["content"]).get("ok")}
                    for m in provider.requests[-1]["messages"]
                    if m.get("role") == "tool"
                ],
                "provider_tools": [x["function"]["name"] for x in provider.requests[0]["tools"]],
            }
        finally:
            live_transport.ClientSession = original_session
            await upstream.close()
            runtime._companion_teardown("review_cleanup")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("OPENAI_API_KEY", "fixture-only")
        run_host(scenario)


def pipeline_generic_camera_probe(transcript):
    import numpy as np

    from homebody.audio import EndpointResult
    from homebody.hermes_client import HermesBridgeClient

    async def scenario(bridge, server, module):
        runtime, _, captures = make_runtime(conversation_mode="pipeline", continuous_conversation=False)
        config = runtime.config_loader()
        config.bridge_url = str(server.make_url("/")).rstrip("/")
        config.api_key = "bridge-secret"
        config.instance_id = "reachy-1"
        runtime._agent_session_generation = 27
        runtime._accept_wake_turn()
        runtime._motion = None
        runtime._play_asset = lambda _: None
        runtime._discard_audio = lambda _: None
        words = [transcript]
        runtime._record_utterance = lambda _: EndpointResult(np.ones(160, dtype=np.float32), True, "end_silence", 0.01)
        realclient = HermesBridgeClient(config)
        realclient.health = lambda: {"status": "ok"}
        realclient.transcribe = lambda _: words.pop(0)
        realclient.last_stt_provider = "fixture"
        realclient.synthesize = lambda _: SimpleNamespace(provider="fixture")
        runtime._new_bridge_client = lambda _: realclient
        runtime._play_response = lambda *a, **k: False
        provider = VisionProvider(
            [[call("pipeline-camera", "capture_reachy_camera", {"purpose": "Read this letter"})]]
        )
        bridge.everyday_llm = provider
        bridge._everyday_key = lambda: "fixture-only"

        def run():
            with runtime._voice_activity_lock:
                runtime._run_conversation(config)

        try:
            await asyncio.wait_for(asyncio.to_thread(run), 8)
            out["pipeline_negated_camera"] = {
                "classifier_consents": False,
                "captures": len(captures),
                "inferences": provider.inferences,
                "provider_rounds": len(provider.requests),
            }
        finally:
            realclient.close()
            runtime._companion_teardown("review_cleanup")

    run_host(scenario)


@pytest.mark.parametrize(
    "words", ["Do not look at this letter", "Look at the repository", 'Explain the phrase "Look at this"']
)
def test_live_model_camera_choice_is_not_actual_user_consent(words):
    live_generic_camera_probe(words)
    proof = out["live_negated_camera"]
    assert not looks_like_look_request(words)
    assert proof["captures"] == 0
    assert proof["inferences"] == 0
    assert proof["bounded_companion_looks"] == 0


@pytest.mark.parametrize(
    "words", ["Do not look at this letter", "Look at the repository", 'Explain the phrase "Look at this"']
)
def test_pipeline_model_camera_choice_is_not_actual_user_consent(words):
    pipeline_generic_camera_probe(words)
    proof = out["pipeline_negated_camera"]
    assert not looks_like_look_request(words)
    assert proof["captures"] == 0
    assert proof["inferences"] == 0


def test_image_analysis_fences_same_batch_and_later_rpc_tools():
    """Even a provider ignoring removed schemas cannot dispatch after OCR data."""
    from homebody.hermes_client import HermesBridgeClient

    async def scenario(bridge, server, module):
        runtime, _, _ = make_runtime(conversation_mode="pipeline")
        cfg = runtime.config_loader()
        cfg.bridge_url = str(server.make_url("/")).rstrip("/")
        cfg.api_key = "bridge-secret"
        cfg.instance_id = "reachy-1"
        runtime._agent_session_generation = 27
        runtime._accept_wake_turn()
        provider = VisionProvider(
            [
                [
                    call("image", "capture_reachy_camera", {"purpose": "Read this letter"}),
                    call("same-batch", "get_reachy_status", {}),
                ],
                [call("next-round", "get_reachy_status", {})],
            ]
        )
        bridge.everyday_llm = provider
        bridge._everyday_key = lambda: "fixture-only"
        admitted = []

        def execute(name, arguments, call_id):
            admitted.append(name)
            return {"ok": True, "text": "Untrusted fixture image analysis"}

        client = HermesBridgeClient(cfg)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    client.everyday_turn,
                    "Look at this letter",
                    context=runtime.agent_broker_context(explicit_private_intent=True),
                    local_tools=runtime.everyday_local_tools(),
                    local_executor=execute,
                    is_current=lambda: True,
                ),
                8,
            )
            assert admitted == ["capture_reachy_camera"]
            assert len(provider.requests) >= 2
            for body in provider.requests[1:]:
                assert not body.get("tools")
        finally:
            client.close()
            runtime._companion_teardown("test_cleanup")

    run_host(scenario)
