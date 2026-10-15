"""Loopback REAL Hermes API server (isolated source tree) with a scripted fake model; test-only.

Usage: python hermes_discussion_server.py HERMES_SOURCE STATE_DIR
Prints one JSON line {"port": ...} once listening, then serves until stdin closes.

Everything between the HTTP request and the model transport is the unmodified Hermes code under
test: /v1/capabilities, POST /v1/runs admission, _create_agent, AIAgent.run_conversation, the tool
round and every dispatch boundary. Only the provider transport (and the auxiliary title call) is
replaced, so no provider is contacted. GET /test/evidence reports what actually happened.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SOURCE, STATE = Path(sys.argv[1]), Path(sys.argv[2])
os.environ["HERMES_HOME"] = str(STATE / "home")
(STATE / "home").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(SOURCE))

try:
    from aiohttp import web

    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from hermes_state import SessionDB
except ImportError as exc:  # an interpreter without Hermes' dependencies: report, don't fake
    print(json.dumps({"missing": exc.name or str(exc)}), flush=True)
    sys.exit(0)

KEY = "native-token"
PWNED = STATE / "pwned.txt"
HOSTILE = ["terminal", "write_file", "patch", "memory", "delegate_task", "mcp_github_create_issue",
           "tool_call", "execute_code", "skill_manage", "send_message"]
evidence = {"model_calls": [], "resolved": [], "agents": [], "direct": {}, "plugin_fired": []}
slow_release = threading.Event()


def response(content="", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning=None, reasoning_content=None,
                          reasoning_details=None, refusal=None, role="assistant")
    finish = "tool_calls" if tool_calls else "stop"
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)], model="fake", usage=None)


def tool_call(i, name):
    args = json.dumps({"path": str(PWNED), "content": "x", "command": f"touch {PWNED}"})
    return SimpleNamespace(id=f"call_{i}", type="function", function=SimpleNamespace(name=name, arguments=args))


def fake_model(agent, api_kwargs, *args, **kwargs):
    messages = api_kwargs["messages"]
    user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
    evidence["model_calls"].append({
        "tools": len(api_kwargs.get("tools") or []),
        "roles": [m["role"] for m in messages],
        "history": [str(m.get("content")) for m in messages if m["role"] in {"user", "assistant"}],
        "tool_results": [str(m.get("content")) for m in messages if m["role"] == "tool"],
        "system": str(messages[0].get("content"))[:4000],
    })
    if "SLOW" in user:
        slow_release.wait(10)
        return response("late answer that must never be published")
    if "HOSTILE" in user and not any(m["role"] == "tool" for m in messages):
        agent.valid_tool_names = set(HOSTILE)  # restored/inherited tool authority
        import model_tools
        from tools.registry import registry
        evidence["direct"] = {
            "handle_function_call": model_tools.handle_function_call("write_file", {"path": str(PWNED), "content": "x"}),
            "registry": registry.dispatch("terminal", {"command": f"touch {PWNED}"}),
        }
        return response("", [tool_call(i, name) for i, name in enumerate(HOSTILE)])
    known = [str(m.get("content")) for m in messages if m["role"] == "assistant" and "EXIF" in str(m.get("content"))]
    return response("From the session: the importer waits on the EXIF date parser fix." if known else "No context.")


def main():
    db = SessionDB(STATE / "state.db")
    for sid in ("native-existing", "other-session"):
        db.create_session(session_id=sid, source="api_server", model="m")
    db.append_message("native-existing", "user", "Where did we leave the photo importer?")
    db.append_message("native-existing", "assistant", "Blocked on the EXIF date parser; accept offset-less timestamps.")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": KEY}))
    adapter._session_db = db
    # Would-write plugin sentinel on every lifecycle hook (shell hooks/webhooks register the same way).
    from hermes_cli.plugins import VALID_HOOKS, get_plugin_manager
    manager = get_plugin_manager()
    for name in VALID_HOOKS:
        manager._hooks.setdefault(name, []).append(
            lambda _hook=name, **_kw: evidence["plugin_fired"].append(_hook))
    real_create = adapter._create_agent

    def create(**kwargs):
        agent = real_create(**kwargs)
        evidence["agents"].append({
            "discussion": getattr(agent, "_hermes_discussion_policy", None) is not None,
            "enabled_toolsets": agent.enabled_toolsets, "valid_tools": sorted(agent.valid_tool_names)[:5],
            "tool_count": len(agent.valid_tool_names), "memory_manager": agent._memory_manager is not None,
            "background_review": not agent.skip_background_review, "session_id": agent.session_id})
        return agent

    from tools.registry import ToolRegistry
    real_get_entry = ToolRegistry.get_entry

    def get_entry(self, name, **kw):
        if name in HOSTILE:
            evidence["resolved"].append(name)
        return real_get_entry(self, name, **kw)

    async def evidence_route(request):
        rows = db.get_messages_as_conversation("native-existing")
        return web.json_response({**evidence, "pwned": PWNED.exists(),
                                  "transcript": [[r["role"], str(r.get("content"))] for r in rows],
                                  "other_rows": len(db.get_messages_as_conversation("other-session")),
                                  "missing_created": db.get_session("missing-session") is not None})

    async def release_route(request):
        slow_release.set()
        return web.json_response({"released": True})

    runtime = {"api_key": "test-key-1234567890", "base_url": "http://127.0.0.1:9/v1", "provider": "custom",
               "api_mode": "chat_completions"}
    patches = [
        patch("gateway.run._resolve_runtime_agent_kwargs", side_effect=lambda: dict(runtime)),
        patch("gateway.run._resolve_gateway_model", return_value="test/model"),
        patch("gateway.run._load_gateway_config", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("agent.title_generator.maybe_auto_title", return_value=None),
        patch("run_agent.AIAgent._interruptible_api_call", fake_model),
        patch("run_agent.AIAgent._interruptible_streaming_api_call", fake_model),
        patch.object(ToolRegistry, "get_entry", get_entry),
        patch.object(adapter, "_create_agent", side_effect=create),
    ]
    for p in patches:
        p.start()

    async def serve():
        app = web.Application()
        app.router.add_get("/v1/capabilities", adapter._handle_capabilities)
        app.router.add_get("/api/sessions/{session_id}", adapter._handle_get_session)
        app.router.add_get("/api/sessions/{session_id}/messages", adapter._handle_session_messages)
        app.router.add_post("/v1/runs", adapter._handle_runs)
        app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
        app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)
        app.router.add_post("/v1/runs/{run_id}/steer", adapter._handle_steer_run)
        app.router.add_post("/v1/runs/{run_id}/stop", adapter._handle_stop_run)
        app.router.add_get("/test/evidence", evidence_route)
        app.router.add_post("/test/release", release_route)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        print(json.dumps({"port": port}), flush=True)
        await asyncio.get_running_loop().run_in_executor(None, sys.stdin.read)
        await runner.cleanup()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
