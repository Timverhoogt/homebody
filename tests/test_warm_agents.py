from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import types
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from companion.warm_agents import (
    AgentSpec,
    HermesAgentFactory,
    PreparedAgent,
    TurnResult,
    WarmAgentBoundaryError,
    WarmAgentBusy,
    WarmAgentPool,
    WarmAgentSettings,
    WarmAgentTurnError,
    WarmAgentUnavailable,
)

ROOT = Path(__file__).resolve().parents[1]


def load_bridge():  # type: ignore[no-untyped-def]
    path = ROOT / "companion" / "hermes_reachy_bridge.py"
    spec = importlib.util.spec_from_file_location("hermes_reachy_bridge", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class FakeAgent:
    def __init__(self, number: int, spec: AgentSpec) -> None:
        self.number = number
        self.spec = spec
        self.closed = False
        self.interrupted = False


class FakeFactory:
    """Records every lifecycle call; ``signature`` and ``unavailable`` are mutable per test."""

    def __init__(self) -> None:
        self.signature = "sig-a"
        self.unavailable: str | None = None
        self.built: list[FakeAgent] = []
        self.turns: list[tuple[int, str, list[dict[str, Any]]]] = []
        self.fail_turn = False
        self.fail_build = False
        self.block: threading.Event | None = None
        self.started = threading.Event()
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def availability(self) -> str | None:
        return self.unavailable

    def prepare(self, spec: AgentSpec) -> PreparedAgent:
        return PreparedAgent(signature=self.signature)

    def build(self, spec: AgentSpec, prepared: PreparedAgent) -> tuple[Any, list[dict[str, Any]]]:
        if self.fail_build:
            raise RuntimeError("provider down")
        agent = FakeAgent(len(self.built) + 1, spec)
        self.built.append(agent)
        return agent, [{"role": "user", "content": "stored"}, {"role": "assistant", "content": "earlier"}]

    def run_turn(self, agent: Any, user_message: str, history: list[dict[str, Any]]) -> TurnResult:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.started.set()
            if self.block is not None:
                self.block.wait(5)
            self.turns.append((agent.number, user_message, history))
            if self.fail_turn:
                raise RuntimeError("tool crashed")
            reply = f"agent {agent.number}: {user_message}"
            return TurnResult(
                text=reply,
                history=[
                    {"role": "system", "content": "never kept"},
                    *history,
                    {"role": "user", "content": user_message},
                    {"role": "assistant", "content": reply},
                ],
                input_tokens=100,
                output_tokens=7,
            )
        finally:
            with self._lock:
                self.active -= 1

    def interrupt(self, agent: Any) -> None:
        agent.interrupted = True

    def close(self, agent: Any) -> None:
        agent.closed = True


def settings(**overrides: Any) -> WarmAgentSettings:
    values: dict[str, Any] = {"enabled": True, "queue_seconds": 1.0, "turn_timeout_seconds": 5.0}
    values.update(overrides)
    return WarmAgentSettings(**values)


SPEC = AgentSpec("pipeline", "reachy-robot-abc", "Be brief.", "agent:main:reachy-mini:robot")


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0.01)


def test_settings_default_off_and_are_bounded() -> None:
    assert WarmAgentSettings.from_env(lambda name, default: default).enabled is False
    env = {
        "REACHY_HERMES_WARM_AGENTS": "1",
        "REACHY_HERMES_WARM_ROUTES": "realtime, bogus",
        "REACHY_HERMES_WARM_MAX_AGENTS": "999",
        "REACHY_HERMES_WARM_IDLE_SECONDS": "nonsense",
        "REACHY_HERMES_WARM_MODELS": "hermes-agent,robot",
    }
    parsed = WarmAgentSettings.from_env(lambda name, default: env.get(name, default))
    assert parsed.enabled is True
    assert parsed.routes == frozenset({"realtime"})
    assert parsed.max_agents == 16 and parsed.idle_seconds == 600
    assert parsed.models == frozenset({"hermes-agent", "robot"})


def test_second_turn_reuses_the_agent_and_carries_history() -> None:
    factory = FakeFactory()
    pool = WarmAgentPool(factory, settings())

    async def scenario() -> tuple[Any, Any]:
        first = await pool.run_turn(SPEC, "hello")
        second = await pool.run_turn(SPEC, "and now?")
        return first, second

    first, second = asyncio.run(scenario())

    assert len(factory.built) == 1
    assert (first.warm, first.rebuild_reason, second.warm) == (False, "cold", True)
    assert first.text == "agent 1: hello" and second.text == "agent 1: and now?"
    # Stored history seeds a cold agent; the agent's own transcript (minus system rows) carries on.
    assert factory.turns[0][2] == [{"role": "user", "content": "stored"}, {"role": "assistant", "content": "earlier"}]
    assert factory.turns[1][2][-2:] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "agent 1: hello"},
    ]
    assert all(item["role"] != "system" for item in factory.turns[1][2])
    stats = pool.status()["routes"]["pipeline"]
    assert (stats["turns"], stats["warm_hits"], stats["cold_builds"]) == (2, 1, 1)
    assert (stats["input_tokens"], stats["output_tokens"]) == (200, 14)
    assert second.usage() == {"prompt_tokens": 100, "completion_tokens": 7, "total_tokens": 107}


def test_routes_and_sessions_get_separate_agents() -> None:
    factory = FakeFactory()
    pool = WarmAgentPool(factory, settings())

    async def scenario() -> None:
        await pool.run_turn(SPEC, "a")
        await pool.run_turn(AgentSpec("realtime", SPEC.session_id, "Be brief."), "b")
        await pool.run_turn(AgentSpec("pipeline", "reachy-robot-other", "Be brief."), "c")

    asyncio.run(scenario())
    assert len(factory.built) == 3
    assert {agent.spec.route for agent in factory.built} == {"pipeline", "realtime"}


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("signature", "signature"),
        ("idle", "idle"),
        ("age", "age"),
        ("turns", "turns"),
    ],
)
def test_changed_or_worn_agents_are_rebuilt_and_closed(change: str, reason: str) -> None:
    factory = FakeFactory()
    clock = Clock()
    pool = WarmAgentPool(factory, settings(max_turns=1 if change == "turns" else 40), clock=clock)

    async def scenario() -> Any:
        await pool.run_turn(SPEC, "first")
        if change == "signature":
            factory.signature = "sig-b"
        elif change == "idle":
            clock.now += 601
        elif change == "age":
            for _ in range(6):  # stays under the idle limit, reaching 3540 s of age
                clock.now += 590
                await pool.run_turn(SPEC, "keep warm")
            clock.now += 100
        result = await pool.run_turn(SPEC, "second")
        await settle()
        return result

    result = asyncio.run(scenario())
    assert result.rebuild_reason == reason and result.warm is False
    assert factory.built[0].closed is True and factory.built[-1].closed is False
    status = pool.status()
    assert status["routes"]["pipeline"]["rebuilds"] == {reason: 1}
    assert len(status["agents"]) == 1


def test_one_turn_at_a_time_per_session_and_bounded_queue() -> None:
    factory = FakeFactory()
    factory.block = threading.Event()
    pool = WarmAgentPool(factory, settings(queue_seconds=0.2))

    async def scenario() -> tuple[list[Any], Any]:
        first = asyncio.create_task(pool.run_turn(SPEC, "one"))
        await asyncio.to_thread(factory.started.wait, 2)
        with pytest.raises(WarmAgentBusy):
            await pool.run_turn(SPEC, "too late")
        second = asyncio.create_task(pool.run_turn(SPEC, "two"))
        await asyncio.sleep(0.05)
        factory.block.set()
        return list(await asyncio.gather(first, second)), pool.status()

    results, status = asyncio.run(scenario())
    assert factory.max_active == 1
    assert [result.text for result in results] == ["agent 1: one", "agent 1: two"]
    assert len(factory.built) == 1 and status["agents"][0]["busy"] is False


def test_capacity_evicts_the_least_recently_used_idle_agent() -> None:
    factory = FakeFactory()
    pool = WarmAgentPool(factory, settings(max_agents=2))

    async def scenario() -> None:
        for session in ("s-1", "s-2", "s-1", "s-3"):
            await pool.run_turn(AgentSpec("pipeline", session, ""), "hi")
        await settle()

    asyncio.run(scenario())
    by_session = {agent.spec.session_id: agent for agent in factory.built}
    assert by_session["s-2"].closed is True
    assert by_session["s-1"].closed is False and by_session["s-3"].closed is False
    assert pool.status()["routes"]["pipeline"]["evictions"] == {"capacity": 1}


def test_failed_turn_retires_the_agent_and_is_not_retried() -> None:
    factory = FakeFactory()
    pool = WarmAgentPool(factory, settings())

    async def scenario() -> Any:
        await pool.run_turn(SPEC, "ok")
        factory.fail_turn = True
        with pytest.raises(WarmAgentTurnError):
            await pool.run_turn(SPEC, "breaks")
        factory.fail_turn = False
        result = await pool.run_turn(SPEC, "after")
        await settle()
        return result

    result = asyncio.run(scenario())
    assert factory.built[0].closed is True
    assert result.rebuild_reason == "cold" and len(factory.built) == 2
    # The failed message ran exactly once.
    assert [turn[1] for turn in factory.turns].count("breaks") == 1
    assert pool.status()["routes"]["pipeline"]["failed_turns"] == 1


def test_cancelled_turn_interrupts_and_holds_the_session_until_the_thread_ends() -> None:
    factory = FakeFactory()
    factory.block = threading.Event()
    pool = WarmAgentPool(factory, settings(queue_seconds=2.0))

    async def scenario() -> Any:
        first = asyncio.create_task(pool.run_turn(SPEC, "slow"))
        await asyncio.to_thread(factory.started.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        agent = factory.built[0]
        assert agent.interrupted is True and agent.closed is False
        follow_up = asyncio.create_task(pool.run_turn(SPEC, "next"))
        await asyncio.sleep(0.05)
        assert not follow_up.done()  # still serialized behind the interrupted thread
        factory.block.set()
        result = await follow_up
        await settle()
        return result

    result = asyncio.run(scenario())
    assert factory.built[0].closed is True
    assert result.text == "agent 2: next" and factory.max_active == 1
    assert pool.status()["routes"]["pipeline"]["evictions"] == {"cancelled": 1}


def test_unavailable_and_failed_builds_never_start_a_turn() -> None:
    factory = FakeFactory()
    factory.unavailable = "Hermes Agent is not importable here (ImportError)"
    pool = WarmAgentPool(factory, settings())
    assert pool.accepts("pipeline", "hermes-agent") is False
    assert pool.status()["unavailable_reason"].startswith("Hermes Agent is not importable")

    factory = FakeFactory()
    factory.fail_build = True
    pool = WarmAgentPool(factory, settings())
    with pytest.raises(WarmAgentUnavailable):
        asyncio.run(pool.run_turn(SPEC, "hi"))
    assert factory.turns == []


def test_routing_follows_settings_and_model_alias() -> None:
    pool = WarmAgentPool(FakeFactory(), settings(routes=frozenset({"realtime"})))
    assert pool.accepts("realtime", "hermes-agent") is True
    assert pool.accepts("pipeline", "hermes-agent") is False
    assert pool.accepts("realtime", "anthropic/some-model") is False
    assert WarmAgentPool(FakeFactory(), WarmAgentSettings()).accepts("realtime", "hermes-agent") is False


def test_evict_waits_for_running_turns_and_status_hides_session_ids() -> None:
    factory = FakeFactory()
    factory.block = threading.Event()
    clock = Clock()
    pool = WarmAgentPool(factory, settings(), clock=clock)

    async def scenario() -> tuple[int, int, dict[str, Any]]:
        running = asyncio.create_task(pool.run_turn(SPEC, "busy"))
        await asyncio.to_thread(factory.started.wait, 2)
        status = pool.status()
        evicted = await pool.evict(reason="kids")
        assert factory.built[0].closed is False  # never closed under a running turn
        factory.block.set()
        await running
        await settle()
        await pool.run_turn(AgentSpec("pipeline", "s-idle", ""), "x")
        clock.now += 10_000
        swept = await pool.sweep()
        await pool.close()
        return evicted, swept, status

    evicted, swept, status = asyncio.run(scenario())
    assert evicted == 1 and swept == 1
    assert all(agent.closed for agent in factory.built)
    text = repr(status)
    assert SPEC.session_id not in text and "Be brief" not in text and SPEC.memory_scope not in text
    assert status["agents"][0]["busy"] is True
    assert pool.status()["enabled"] is False


def test_agent_built_across_an_eviction_is_not_kept() -> None:
    factory = FakeFactory()
    gate = threading.Event()
    original_build = factory.build

    def slow_build(spec: AgentSpec, prepared: PreparedAgent) -> tuple[Any, list[dict[str, Any]]]:
        gate.wait(5)
        return original_build(spec, prepared)

    factory.build = slow_build  # type: ignore[method-assign]
    pool = WarmAgentPool(factory, settings())

    async def scenario() -> Any:
        turn = asyncio.create_task(pool.run_turn(SPEC, "hi"))
        await asyncio.sleep(0.05)
        assert await pool.evict(reason="kids") == 0  # nothing cached yet
        gate.set()
        result = await turn
        await settle()
        return result

    result = asyncio.run(scenario())
    assert result.text == "agent 1: hi"
    assert factory.built[0].closed is True and pool.status()["agents"] == []
    assert pool.status()["routes"]["pipeline"]["evictions"] == {"kids": 1}


# -- Hermes adapter --------------------------------------------------------------------------


class FakeAIAgent:
    tools_exposed: set[str] = {"web_search", "memory"}
    instances: list[FakeAIAgent] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.session_id = kwargs["session_id"]
        self.valid_tool_names = set(self.tools_exposed)
        self.session_prompt_tokens = 1_000
        self.session_completion_tokens = 50
        self._last_flushed_db_idx = 9
        self.closed = False
        self.interrupts: list[tuple[Any, ...]] = []
        FakeAIAgent.instances.append(self)

    def run_conversation(self, *, user_message: str, conversation_history: list[Any], task_id: str | None) -> Any:
        assert self._last_flushed_db_idx == 0
        self.session_prompt_tokens += 300
        self.session_completion_tokens += 20
        return {
            "final_response": f" reply to {user_message} ",
            "messages": [*conversation_history, {"role": "user", "content": user_message}],
        }

    def interrupt(self, message: str | None = None, *, hard_cancel: bool = False) -> None:
        self.interrupts.append((message, hard_cancel))

    def close(self) -> None:
        self.closed = True


class FakeSessionDB:
    def get_messages_as_conversation(self, session_id: str) -> list[dict[str, str]]:
        return [{"role": "user", "content": f"stored for {session_id}"}]


@pytest.fixture
def fake_hermes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    state: dict[str, Any] = {
        "runtime": {"provider": "openrouter", "api_key": "sk-secret", "base_url": "https://x", "model": "m-1"},
        "toolsets": ["web", "memory"],
    }
    run_module = types.ModuleType("gateway.run")
    run_module._resolve_runtime_agent_kwargs = lambda: dict(state["runtime"])  # type: ignore[attr-defined]
    run_module._resolve_gateway_model = lambda: "fallback-model"  # type: ignore[attr-defined]
    run_module._load_gateway_config = lambda: {"platform_toolsets": state["toolsets"]}  # type: ignore[attr-defined]
    run_module._current_max_iterations = lambda: 12  # type: ignore[attr-defined]
    tools_module = types.ModuleType("hermes_cli.tools_config")
    tools_module._get_platform_tools = lambda config, platform: list(config["platform_toolsets"])  # type: ignore[attr-defined]
    agent_module = types.ModuleType("run_agent")
    agent_module.AIAgent = FakeAIAgent  # type: ignore[attr-defined]
    registry_module = types.ModuleType("hermes_state_registry")
    registry_module.acquire = lambda: FakeSessionDB()  # type: ignore[attr-defined]
    for name, module in {
        "gateway": types.ModuleType("gateway"),
        "gateway.run": run_module,
        "hermes_cli": types.ModuleType("hermes_cli"),
        "hermes_cli.tools_config": tools_module,
        "run_agent": agent_module,
        "hermes_state_registry": registry_module,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    FakeAIAgent.instances = []
    FakeAIAgent.tools_exposed = {"web_search", "memory"}
    (tmp_path / "config.yaml").write_text("a: 1\n", encoding="utf-8")
    state["home"] = tmp_path
    return state


def hermes_factory(home: Path, profile: str | None = None) -> HermesAgentFactory:
    return HermesAgentFactory(
        ensure_imports=lambda: None,
        hermes_home=home,
        prohibited_tools=frozenset({"terminal", "read_file"}),
        profile=profile,
    )


def test_hermes_factory_builds_like_the_api_server_and_tracks_usage(fake_hermes: dict[str, Any]) -> None:
    factory = hermes_factory(fake_hermes["home"])
    assert factory.availability() is None
    prepared = factory.prepare(SPEC)
    assert "sk-secret" not in repr(prepared.signature)

    agent, history = factory.build(SPEC, prepared)
    assert history == [{"role": "user", "content": f"stored for {SPEC.session_id}"}]
    assert agent.kwargs["model"] == "m-1" and agent.kwargs["provider"] == "openrouter"
    assert agent.kwargs["enabled_toolsets"] == ["memory", "web"]
    assert agent.kwargs["ephemeral_system_prompt"] == "Be brief."
    assert agent.kwargs["gateway_session_key"] == SPEC.memory_scope
    assert agent.kwargs["platform"] == "api_server" and agent.kwargs["max_iterations"] == 12

    result = factory.run_turn(agent, "hi", history)
    assert result.text == "reply to hi"
    assert (result.input_tokens, result.output_tokens) == (300, 20)

    factory.interrupt(agent)
    factory.close(agent)
    assert agent.interrupts == [("Reachy cancelled this turn", True)] and agent.closed is True


def test_hermes_signature_changes_with_runtime_tools_prompt_and_config(fake_hermes: dict[str, Any]) -> None:
    factory = hermes_factory(fake_hermes["home"])
    base = factory.prepare(SPEC).signature
    assert factory.prepare(SPEC).signature == base

    fake_hermes["runtime"]["api_key"] = "sk-rotated"
    rotated = factory.prepare(SPEC).signature
    fake_hermes["toolsets"] = ["web"]
    fewer_tools = factory.prepare(SPEC).signature
    other_prompt = factory.prepare(AgentSpec("pipeline", SPEC.session_id, "Other.", SPEC.memory_scope)).signature
    (fake_hermes["home"] / "config.yaml").write_text("a: 22\n", encoding="utf-8")
    edited = factory.prepare(AgentSpec("pipeline", SPEC.session_id, "Other.", SPEC.memory_scope)).signature

    assert len({base, rotated, fewer_tools, other_prompt, edited}) == 5


def test_hermes_factory_fails_closed(fake_hermes: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAIAgent.tools_exposed = {"web_search", "terminal"}
    factory = hermes_factory(fake_hermes["home"])
    with pytest.raises(WarmAgentBoundaryError):
        factory.build(SPEC, factory.prepare(SPEC))
    assert FakeAIAgent.instances[-1].closed is True

    assert hermes_factory(fake_hermes["home"], profile="robot").availability() is not None

    monkeypatch.setitem(sys.modules, "run_agent", None)
    assert "not importable" in str(hermes_factory(fake_hermes["home"]).availability())


# -- bridge integration ----------------------------------------------------------------------


class FakeHermesServer:
    def __init__(self) -> None:
        self.chat_requests = 0

    def app(self) -> web.Application:
        async def toolsets(request: web.Request) -> web.Response:
            return web.json_response([{"enabled": True, "tools": ["web_search"]}])

        async def chat(request: web.Request) -> web.Response:
            self.chat_requests += 1
            return web.json_response({"choices": [{"message": {"role": "assistant", "content": "via HTTP"}}]})

        async def health(request: web.Request) -> web.Response:
            return web.json_response({"status": "ok"})

        app = web.Application()
        app.router.add_get("/health", health)
        app.router.add_get("/v1/toolsets", toolsets)
        app.router.add_post("/v1/chat/completions", chat)
        return app


AUTH = {"Authorization": "Bearer bridge-key"}
SESSION_HEADERS = {
    **AUTH,
    "X-Hermes-Session-Id": "reachy-robot-abc",
    "X-Hermes-Session-Key": "agent:main:reachy-mini:robot",
}
REACHY_CHAT = {
    "model": "hermes-agent",
    "stream": False,
    "messages": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hello"}],
}


def run_bridge(pool: WarmAgentPool, scenario):  # type: ignore[no-untyped-def]
    bridge_module = load_bridge()
    upstream = FakeHermesServer()

    async def main() -> Any:
        server = TestServer(upstream.app())
        await server.start_server()
        try:
            app = bridge_module.create_app(
                api_key="bridge-key",
                hermes_url=str(server.make_url("")).rstrip("/"),
                warm_agents=pool,
            )
            async with TestClient(TestServer(app)) as client:
                return await scenario(client, app)
        finally:
            await server.close()

    return asyncio.run(main()), upstream


def test_bridge_pipeline_turns_use_a_warm_agent() -> None:
    factory = FakeFactory()
    pool = WarmAgentPool(factory, settings())

    async def scenario(client: TestClient, app: web.Application) -> Any:
        first = await client.post("/v1/chat/completions", headers=SESSION_HEADERS, json=REACHY_CHAT)
        second = await client.post("/v1/chat/completions", headers=SESSION_HEADERS, json=REACHY_CHAT)
        status = await (await client.get("/v1/warm-agents", headers=AUTH)).json()
        unauthorized = await client.get("/v1/warm-agents")
        health = await (await client.get("/health")).json()
        return first, await first.json(), second.headers, status, unauthorized.status, health

    (first, body, second_headers, status, unauthorized, health), upstream = run_bridge(pool, scenario)
    assert first.status == 200 and first.headers["X-Reachy-Warm-Agent"] == "cold"
    assert first.headers["X-Hermes-Session-Id"] == "reachy-robot-abc"
    assert second_headers["X-Reachy-Warm-Agent"] == "hit"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "agent 1: hello"}
    assert body["usage"]["total_tokens"] == 107
    assert upstream.chat_requests == 0 and len(factory.built) == 1
    spec = factory.built[0].spec
    assert (spec.route, spec.system_prompt) == ("pipeline", "Be brief.")
    assert spec.memory_scope == "agent:main:reachy-mini:robot"
    assert status["routes"]["pipeline"]["warm_hits"] == 1 and unauthorized == 401
    assert health["warm_agents"] == {"enabled": True, "available": True}


@pytest.mark.parametrize(
    ("headers", "payload"),
    [
        ({**AUTH}, REACHY_CHAT),  # no session id
        (SESSION_HEADERS, {**REACHY_CHAT, "model": "anthropic/claude"}),  # explicit model route
        (SESSION_HEADERS, {**REACHY_CHAT, "tools": []}),  # anything beyond the Reachy shape
        (
            SESSION_HEADERS,
            {**REACHY_CHAT, "messages": [{"role": "assistant", "content": "x"}, {"role": "user", "content": "y"}]},
        ),
    ],
)
def test_bridge_other_requests_keep_the_api_server(headers: dict[str, str], payload: dict[str, Any]) -> None:
    factory = FakeFactory()

    async def scenario(client: TestClient, app: web.Application) -> int:
        return (await client.post("/v1/chat/completions", headers=headers, json=payload)).status

    status, upstream = run_bridge(WarmAgentPool(factory, settings()), scenario)
    assert status == 200 and upstream.chat_requests == 1 and factory.built == []


def test_bridge_falls_back_only_when_no_turn_ran() -> None:
    factory = FakeFactory()
    factory.fail_build = True
    pool = WarmAgentPool(factory, settings())

    async def build_fails(client: TestClient, app: web.Application) -> Any:
        response = await client.post("/v1/chat/completions", headers=SESSION_HEADERS, json=REACHY_CHAT)
        return response.status, await response.json()

    (status, body), upstream = run_bridge(pool, build_fails)
    assert status == 200 and body["choices"][0]["message"]["content"] == "via HTTP"
    assert upstream.chat_requests == 1 and pool.status()["routes"]["pipeline"]["fallbacks"] == 1

    factory = FakeFactory()
    factory.fail_turn = True

    async def turn_fails(client: TestClient, app: web.Application) -> int:
        return (await client.post("/v1/chat/completions", headers=SESSION_HEADERS, json=REACHY_CHAT)).status

    status, upstream = run_bridge(WarmAgentPool(factory, settings()), turn_fails)
    assert status == 502 and upstream.chat_requests == 0  # a started turn is never replayed


def test_kids_start_and_owner_route_evict_warm_agents() -> None:
    factory = FakeFactory()
    pool = WarmAgentPool(factory, settings())

    async def scenario(client: TestClient, app: web.Application) -> Any:
        await client.post("/v1/chat/completions", headers=SESSION_HEADERS, json=REACHY_CHAT)
        kids = await client.post(
            "/v1/kids/session", headers=AUTH, json={"session_id": "kids-" + "a" * 32, "state": "active"}
        )
        await settle()
        after_kids = pool.status()
        await client.post("/v1/kids/session", headers=AUTH, json={"session_id": "kids-" + "a" * 32, "state": "ended"})
        await client.post("/v1/chat/completions", headers=SESSION_HEADERS, json=REACHY_CHAT)
        evicted = await (await client.delete("/v1/warm-agents", headers=AUTH)).json()
        await settle()
        return kids.status, after_kids, evicted

    (kids_status, after_kids, evicted), _ = run_bridge(pool, scenario)
    assert kids_status == 200 and after_kids["agents"] == []
    assert after_kids["routes"]["pipeline"]["evictions"] == {"kids": 1}
    assert evicted == {"ok": True, "evicted": 1}
    assert [agent.closed for agent in factory.built] == [True, True]


def test_realtime_ask_hermes_uses_its_own_warm_route() -> None:
    bridge_module = load_bridge()
    factory = FakeFactory()
    pool = WarmAgentPool(factory, settings())
    upstream = FakeHermesServer()

    async def main() -> list[str]:
        server = TestServer(upstream.app())
        await server.start_server()
        bridge = bridge_module.Bridge(
            api_key="k", hermes_url=str(server.make_url("")).rstrip("/"), warm_agents=pool
        )
        await bridge.start(web.Application())
        try:
            return [
                await bridge._hermes_answer(text, model="hermes-agent", system_prompt="Realtime.", session_id="rt-1")
                for text in ("weather?", "and tomorrow?")
            ]
        finally:
            await bridge.stop(web.Application())
            await server.close()

    answers = asyncio.run(main())
    assert answers == ["agent 1: weather?", "agent 1: and tomorrow?"]
    assert factory.built[0].spec.route == "realtime" and upstream.chat_requests == 0
    assert factory.built[0].closed is True  # bridge shutdown closes warm agents
