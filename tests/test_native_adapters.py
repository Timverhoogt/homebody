"""Real loopback HTTP/SSE and WebSocket fixtures; never starts a model or robot."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from companion.native_adapters import HermesNativeAdapter, OpenClawNativeAdapter, public_messages


def target(url):
    return SimpleNamespace(
        url=url, token_env="HOST_SECRET", session_id="native-project", project_root="/registered/project"
    )


def test_public_messages_never_exposes_tools_reasoning_or_credentials():
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "thinking", "text": "hidden"}, {"type": "text", "text": "visible"}],
                "reasoning": "hidden",
            },
            {"role": "tool", "content": "private tool result"},
            {"role": "user", "content": "Bearer secret_long_token"},
        ]
    }
    result = public_messages(payload)
    assert result[0] == {"role": "assistant", "text": "visible"}
    assert "hidden" not in str(result) and "private tool" not in str(result)
    assert "secret_long_token" not in str(result)


def test_hermes_native_uses_exact_session_and_native_run_sse():
    async def scenario():
        seen = []

        async def route(request):
            assert request.headers["Authorization"] == "Bearer host-only"
            seen.append((request.method, request.path))
            if request.path.endswith("/messages"):
                return web.json_response(
                    {"session_id": "native-project", "data": [{"role": "user", "content": "persisted native turn"}]}
                )
            if request.path == "/v1/runs":
                assert request.headers["Idempotency-Key"] == "submission-exact"
                assert await request.json() == {"session_id": "native-project", "input": "approved request"}
                return web.json_response({"run_id": "run-native"}, status=202)
            if request.path.endswith("/events"):
                stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await stream.prepare(request)
                await stream.write(b": keepalive\n\n")
                for frame in [
                    {"type": "reasoning.delta", "text": "hidden"},
                    {"type": "tool.started", "tool": "terminal", "preview": "password=private"},
                    {"type": "tool.completed", "tool": "terminal", "error": True},
                    {"type": "approval.request", "secret": "private"},
                    {"type": "run.completed", "output": "actual response"},
                ]:
                    await stream.write(f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n".encode())
                await stream.write_eof()
                return stream
            if request.path.endswith("/stop"):
                return web.json_response({"status": "stopping"})
            if request.path.startswith("/v1/runs/"):
                return web.json_response({"session_id": "native-project", "status": "running"})
            return web.json_response({"object": "hermes.session", "session": {"id": "native-project"}})

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", route)
        async with TestServer(app) as server, ClientSession() as http:
            adapter = HermesNativeAdapter(http, lambda _name: "host-only")
            config = target(str(server.make_url("/")).rstrip("/"))
            assert (await adapter.history(config))[0]["text"] == "persisted native turn"
            run = await adapter.submit(config, "approved request", "submission-exact")
            events = [event async for event in adapter.events(config, run)]
            assert [e["event"] for e in events] == ["tool.started", "tool.failed", "approval.request", "run.completed"]
            assert "private" not in str(events) and "hidden" not in str(events)
            assert await adapter.stop(config, run) == "stopping"
            assert ("GET", "/api/sessions/native-project/messages") in seen
            await adapter.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("observe_approvals", [False, True])
def test_openclaw_native_handshake_history_events_and_exact_abort(observe_approvals):
    async def scenario():
        calls = []

        async def gateway(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"type": "event", "event": "connect.challenge", "payload": {"nonce": "n", "ts": 1}})
            async for message in ws:
                frame = json.loads(message.data)
                method, params = frame["method"], frame["params"]
                calls.append((method, params))
                if method == "connect":
                    assert params["auth"]["token"] == "host-only"
                    assert params["client"]["id"] == "gateway-client"
                    assert ("operator.approvals" in params["scopes"]) is observe_approvals
                    payload = {
                        "type": "hello-ok",
                        "features": {
                            "methods": [
                                "sessions.resolve",
                                "chat.history",
                                "chat.send",
                                "chat.abort",
                                "sessions.messages.subscribe",
                            ]
                        },
                    }
                elif method == "sessions.messages.subscribe":
                    assert params.get("includeApprovals", False) is observe_approvals
                    payload = {"ok": True}
                elif method == "sessions.resolve":
                    payload = {"ok": True, "key": params["key"]}
                elif method == "chat.history":
                    payload = {
                        "sessionKey": params["sessionKey"],
                        "sessionId": "existing-native",
                        "messages": [{"role": "assistant", "content": [{"type": "text", "text": "native history"}]}],
                    }
                elif method == "chat.send":
                    assert params["deliver"] is False and params["suppressCommandInterpretation"] is True
                    assert params["sessionKey"] == "native-project"
                    assert params["idempotencyKey"] == "submission-exact"
                    payload = {"runId": "run-native", "status": "started"}
                    await ws.send_json(
                        {
                            "type": "event",
                            "event": "session.approval",
                            "payload": {
                                "sessionKey": "native-project",
                                "phase": "pending",
                                "approval": {"presentation": {"details": {"command": "private"}}},
                            },
                        }
                    )
                    await ws.send_json(
                        {
                            "type": "event",
                            "event": "chat",
                            "payload": {
                                "runId": "run-native",
                                "sessionKey": "unregistered-private-session",
                                "state": "final",
                                "message": {"role": "assistant", "content": "do-not-cache-other-session"},
                            },
                        }
                    )
                    # An event arriving before admission must be retained, not attributed to another run.
                    await ws.send_json(
                        {
                            "type": "event",
                            "event": "agent",
                            "payload": {
                                "runId": "run-foreign",
                                "sessionKey": "native-project",
                                "stream": "tool",
                                "data": {"phase": "start", "name": "foreign"},
                            },
                        }
                    )
                    await ws.send_json(
                        {
                            "type": "event",
                            "event": "agent",
                            "payload": {
                                "runId": "run-native",
                                "sessionKey": "native-project",
                                "stream": "tool",
                                "data": {"phase": "start", "name": "read", "args": {"secret": "private"}},
                            },
                        }
                    )
                    await ws.send_json(
                        {
                            "type": "event",
                            "event": "chat",
                            "payload": {
                                "runId": "run-native",
                                "sessionKey": "native-project",
                                "state": "final",
                                "message": {
                                    "role": "assistant",
                                    "content": [
                                        {"type": "text", "text": "done"},
                                        {"type": "thinking", "text": "hidden"},
                                    ],
                                },
                            },
                        }
                    )
                else:
                    payload = {"ok": True}
                await ws.send_json({"type": "res", "id": frame["id"], "ok": True, "payload": payload})
            return ws

        app = web.Application()
        app.router.add_get("/", gateway)
        async with TestServer(app) as server, ClientSession() as http:
            adapter = OpenClawNativeAdapter(http, lambda _name: "host-only")
            config = target(str(server.make_url("/")).replace("http:", "ws:").rstrip("/"))
            config.observe_approvals = observe_approvals
            assert (await adapter.history(config))[0]["text"] == "native history"
            run = await adapter.submit(config, "approved request", "submission-exact")
            events = [event async for event in adapter.events(config, run)]
            expected = [{"event": "session.approval.request"}] if observe_approvals else []
            expected += [{"event": "tool.started", "tool": "read"}, {"event": "run.completed", "output": "done"}]
            assert events == expected
            assert "private" not in str(list(adapter.connections.values())[0].events)
            assert "do-not-cache-other-session" not in str(list(adapter.connections.values())[0].events)
            assert "private" not in str(events) and "hidden" not in str(events) and "foreign" not in str(events)
            await adapter.stop(config, run)
            assert ("chat.abort", {"sessionKey": "native-project", "runId": "run-native"}) in calls
            await adapter.close()

    asyncio.run(scenario())
