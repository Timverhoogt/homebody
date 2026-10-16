"""Real robot client → host model loop → runtime dispatch → verified model result.

Only the model responses and hardware are fixtures. All HTTP/WS, authentication,
local RPC correlation, manifest validation and runtime lifecycle guards are real.
"""

import asyncio
import copy
import io
import json
import threading
from types import SimpleNamespace

import pytest
from PIL import Image
from test_everyday_local_transport import VALID, run_host
from test_everyday_workspace_bridge import ctx

from homebody.config import AppConfig
from homebody.hermes_client import AgentBrokerContext, HermesBridgeClient
from homebody.runtime import HermesVoiceRuntime


@pytest.mark.parametrize("name,args", VALID)
def test_real_robot_client_and_host_roundtrip(name, args):
    async def main(bridge, server, module):
        config = AppConfig(
            bridge_url=str(server.make_url("/")).rstrip("/"),
            api_key="bridge-secret",
            instance_id="reachy-1",
            everyday_camera_enabled=True,
        )
        runtime = HermesVoiceRuntime(
            SimpleNamespace(media=SimpleNamespace()), threading.Event(), config_loader=lambda: config
        )
        runtime._control_ready.set()
        runtime._audio_ready = True
        runtime._power_mode = "awake"
        runtime._motors_enabled = True
        runtime._agent_session_generation = 27
        runtime._accept_wake_turn()
        runtime._speaker = SimpleNamespace(
            read=lambda: {"volume": 24, "verified": True}, set=lambda **kw: {**kw, "verified": True}
        )
        applied = []

        def enqueue(action, arguments, *, on_complete, **kw):
            applied.append((action, arguments))
            on_complete({"ok": True, "action": action})
            return {"accepted": True}

        runtime._actions = SimpleNamespace(enqueue=enqueue, pending_count=0)

        def power(mode, **kw):
            applied.append((mode, kw))
            runtime._request_conversation_stop()
            return {"power_mode": mode}

        runtime.set_power_mode = power
        image = io.BytesIO()
        Image.new("RGB", (32, 32), "white").save(image, "JPEG")
        captures = []
        runtime._capture_camera_jpeg = lambda **kw: captures.append(1) or image.getvalue()
        requests = []

        class Provider:
            name = "openai-codex"
            model = staticmethod(lambda role: "fixture")
            request = staticmethod(lambda body: body)

            async def complete(self, body, **kw):
                requests.append(copy.deepcopy(body))
                content = body["messages"][-1]["content"]
                if isinstance(content, list):
                    assert "tools" not in body
                    message = {"content": "The fixture image is white."}
                elif body["messages"][-1]["role"] == "tool":
                    result = json.loads(content)
                    assert result["ok"] is True
                    if name == "capture_reachy_camera" and result.get("camera_session_active"):
                        message = {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "model-look",
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(args)},
                                }
                            ],
                        }
                    else:
                        message = {"content": "Verified fixture reply."}
                else:
                    message = {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "model-tool",
                                "type": "function",
                                "function": {
                                    "name": "start_reachy_camera" if name == "capture_reachy_camera" else name,
                                    "arguments": json.dumps(
                                        {"duration_seconds": 30} if name == "capture_reachy_camera" else args
                                    ),
                                },
                            }
                        ],
                    }
                return {"choices": [{"message": message}]}

        bridge.everyday_llm = Provider()
        bridge._everyday_key = lambda: "fixture-only"
        fence = runtime.everyday_tool_fence()

        def request():
            client = HermesBridgeClient(config)
            try:
                return client.everyday_turn(
                    "perform the explicit fixture action",
                    context=AgentBrokerContext(**ctx(27, "conversation")),
                    local_tools=runtime.everyday_local_tools(),
                    local_executor=lambda tool, arguments, identifier: runtime.execute_everyday_tool(
                        tool, arguments, call_id=identifier, fence=fence
                    ),
                    is_current=lambda: not runtime._turn_stop_requested(),
                )
            finally:
                client.close()

        answer = await asyncio.wait_for(asyncio.to_thread(request), 5)
        assert answer == ("" if name == "set_reachy_power_mode" else "Verified fixture reply.")
        if name in {"start_reachy_camera", "capture_reachy_camera"}:
            assert len(captures) == (2 if name == "capture_reachy_camera" else 1) and not config.camera_enabled
        if name == "capture_reachy_camera":
            assert len(requests) == 4  # start, capture, no-tools vision, grounded final text
        if name in {"move_reachy_head", "express_reachy_emotion", "dance_reachy"}:
            assert applied == [(name, args)]
        assert not bridge.agent_broker.actions._scheduled_tasks

    run_host(main)
