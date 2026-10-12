"""Everyday tools against loopback fake Home Assistant / chat-provider servers (no live services)."""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from companion.everyday_tools import (
    EVERYDAY_TOOL_NAMES,
    EverydayConfig,
    EverydayTools,
    TurnBudget,
    chat_tool_specs,
    realtime_tool_specs,
)
from companion.llm_providers import LLMProviderConfigError, everyday_provider_from_env
from companion.reachy_agent_actions import ActionConfig, AgentActionService
from companion.reachy_agent_broker import BrokerConfig, ReachyAgentBroker

NOW = datetime(2026, 10, 12, 9, 30, tzinfo=UTC).timestamp()


class FakeHomeAssistant:
    def __init__(self):
        self.states = {
            "light.desk": {"state": "off", "attributes": {"friendly_name": "Desk", "secret": "x"}},
            "light.hall": {"state": "on", "attributes": {}},
            "switch.kettle": {"state": "off", "attributes": {}},
            "lock.front": {"state": "locked", "attributes": {}},
            "weather.home": {
                "state": "rainy", "last_updated": "2026-10-12T09:00:00+00:00",
                "attributes": {"temperature": 12.5, "temperature_unit": "°C", "humidity": 80},
            },
        }
        self.calls = []
        self.verify_fails = False
        self.hang = False
        self.forecast_requests = []

    def app(self):
        app = web.Application()
        app.router.add_get("/api/states/{entity}", self.state)
        app.router.add_post("/api/services/{domain}/{service}", self.service)
        return app

    async def state(self, request):
        assert request.headers["Authorization"] == "Bearer ha-token"
        if self.hang:
            await asyncio.sleep(30)
        entity = request.match_info["entity"]
        if entity not in self.states:
            return web.json_response({}, status=404)
        return web.json_response(self.states[entity])

    async def service(self, request):
        body = await request.json()
        domain, service = request.match_info["domain"], request.match_info["service"]
        if domain == "weather":
            self.forecast_requests.append(request.query_string)
            return web.json_response({"service_response": {"weather.home": {"forecast": [
                {"datetime": "2026-10-12T00:00:00+00:00", "condition": "rainy", "temperature": 14, "templow": 8},
                {"datetime": "2026-10-13T00:00:00+00:00", "condition": "sunny", "temperature": 17, "templow": 9,
                 "precipitation_probability": 10},
            ]}}})
        self.calls.append((domain, service, body["entity_id"]))
        if not self.verify_fails:
            self.states[body["entity_id"]]["state"] = "on" if service == "turn_on" else "off"
        return web.json_response([])


def make_tools(url, **config):
    broker = ReachyAgentBroker(BrokerConfig(
        home_entities={"light.desk": frozenset({"friendly_name"}), "light.hall": frozenset()},
        hass_url=url, hass_token="ha-token",
    ))
    broker.actions = AgentActionService(ActionConfig(
        hass_url=url, hass_token="ha-token",
        home_actions={
            "light.desk": frozenset({"turn_on", "turn_off"}),
            "light.hall": frozenset({"turn_on", "turn_off"}),
            "switch.kettle": frozenset({"turn_on"}),
            "lock.front": frozenset({"turn_on"}),
        },
    ))
    defaults = {"aliases": {"light.desk": ("desk light", "lamp"), "light.hall": ("hall light", "lamp"),
                            "switch.kettle": ("kettle",)},
                "weather_entity": "weather.home", "timezone_name": "Europe/Amsterdam"}
    defaults.update(config)
    return EverydayTools(broker, EverydayConfig(**defaults), clock=lambda: NOW)


def run_with_ha(test, **config):
    async def main():
        ha = FakeHomeAssistant()
        server = TestServer(ha.app())
        await server.start_server()
        try:
            async with ClientSession() as http:
                tools = make_tools(str(server.make_url("")).rstrip("/"), **config)
                await test(tools, ha, http)
        finally:
            await server.close()

    asyncio.run(main())


def call(tools, http, name, arguments=None, *, budget=None, **kwargs):
    return tools.call(name, arguments or {}, http, device_id="dev", generation=1,
                      budget=budget or TurnBudget(), **kwargs)


def test_tool_surface_is_narrow_and_shapes_match():
    names = {tool["name"] for tool in realtime_tool_specs()}
    assert names == EVERYDAY_TOOL_NAMES
    assert {tool["function"]["name"] for tool in chat_tool_specs()} == names
    forbidden = {"ask_hermes", "ask_openclaw", "recall_personal_context", "search_conversation_history",
                 "read_scoped_note", "read_project_roadmap", "list_projects", "capture_reachy_camera"}
    assert not names & forbidden


def test_resolution_is_exact_and_ambiguity_is_returned():
    tools = make_tools("http://127.0.0.1:1")
    controls = set(tools.broker.actions.config.home_actions)
    assert tools.resolve("Desk light!", controls) == {"entity_id": "light.desk"}
    ambiguous = tools.resolve("lamp", controls)
    assert ambiguous["ambiguous"] is True and set(ambiguous["names"]) == {"desk light", "hall light"}
    unknown = tools.resolve("bedroom light", controls)
    assert unknown["unknown"] is True and "desk light" in unknown["names"]
    # Near-misses are never guessed and entities outside the allowlist never resolve.
    assert "entity_id" not in tools.resolve("desk", controls)
    assert "entity_id" not in tools.resolve("light.garage", controls)
    assert tools.public_names()["controls"] == ["desk light", "hall light", "kettle"]


def test_control_verifies_state_and_undo_restores_prior():
    async def test(tools, ha, http):
        budget = TurnBudget()
        result = await call(tools, http, "home_control", {"name": "desk light", "action": "turn_on"},
                            budget=budget, call_id="c1")
        assert result["ok"] is True and result["verified"] is True and ha.calls == [("light", "turn_on", "light.desk")]
        # at most once: replaying the same call id and a second change in the same request are refused
        again = await call(tools, http, "home_control", {"name": "desk light", "action": "turn_off"},
                           budget=budget, call_id="c1")
        assert again["duplicate"] is True
        second = await call(tools, http, "home_control", {"name": "hall light", "action": "turn_off"}, budget=budget)
        assert second["ok"] is False and "one device change" in second["error"]
        assert len(ha.calls) == 1
        undo = await call(tools, http, "home_undo")
        assert undo["verified"] is True and ha.states["light.desk"]["state"] == "off"

    run_with_ha(test)


def test_control_failures_are_honest():
    async def test(tools, ha, http):
        ha.verify_fails = True
        failed = await call(tools, http, "home_control", {"name": "desk light", "action": "turn_on"})
        assert failed["ok"] is False and failed["verified"] is False and "Unavailable" in failed["error"]
        refused = await call(tools, http, "home_control", {"name": "lock.front", "action": "turn_on"})
        assert "not voice controls" in refused["error"]
        not_allowed = await call(tools, http, "home_control", {"name": "kettle", "action": "turn_off"})
        assert "not allowed" in not_allowed["error"]
        ambiguous = await call(tools, http, "home_control", {"name": "lamp", "action": "turn_on"})
        assert ambiguous["ambiguous"] is True
        assert len(ha.calls) == 1  # only the verify-failure attempt reached Home Assistant
        stale = await call(tools, http, "home_control", {"name": "hall light", "action": "turn_off"},
                           is_current=lambda: False)
        kids = await call(tools, http, "home_status", {"names": []}, kids_live=lambda: True)
        assert stale["ok"] is False and kids["ok"] is False and len(ha.calls) == 1

    run_with_ha(test)


def test_timeout_and_status_filtering():
    async def test(tools, ha, http):
        status = await call(tools, http, "home_status", {"names": ["desk light"]})
        assert status["entities"][0]["attributes"] == {"friendly_name": "Desk"}  # allowlisted attributes only
        ha.hang = True
        tools.config.ha_timeout_seconds = 0.2
        timed_out = await call(tools, http, "home_status", {"names": []})
        assert timed_out["ok"] is False and "in time" in timed_out["error"]

    run_with_ha(test)


def test_weather_freshness_forecast_and_unconfigured():
    async def test(tools, ha, http):
        now = await call(tools, http, "get_weather", {"when": "now"})
        assert now["condition"] == "rainy" and now["age_seconds"] == 1800 and now["stale"] is False
        tomorrow = await call(tools, http, "get_weather", {"when": "tomorrow"})
        assert tomorrow["date"] == "2026-10-13" and tomorrow["temperature_high"] == 17
        assert ha.forecast_requests == ["return_response"]
        ha.states["weather.home"]["last_updated"] = "2026-10-12T01:00:00+00:00"
        assert (await call(tools, http, "get_weather", {"when": "now"}))["stale"] is True
        ha.states["weather.home"]["state"] = "unavailable"
        assert (await call(tools, http, "get_weather", {"when": "now"}))["ok"] is False
        tools.config.weather_entity = ""
        missing = await call(tools, http, "get_weather", {"when": "now"})
        assert missing["ok"] is False and "No weather source" in missing["error"]

    run_with_ha(test)


def test_time_uses_host_clock_and_config_validation():
    tools = make_tools("http://127.0.0.1:1")
    now = tools.current_time()
    assert now["local_time"] == "11:30" and now["source"] == "hermes_host_clock"
    with pytest.raises(ValueError, match="explicit"):
        EverydayConfig.from_env(lambda n: {"REACHY_EVERYDAY_WEATHER_PROVIDER": "open-meteo"}.get(n, ""))
    configured = EverydayConfig.from_env(lambda n: {
        "REACHY_EVERYDAY_WEATHER_PROVIDER": "open-meteo", "REACHY_EVERYDAY_WEATHER_LATLON": "52.1234,5.1234",
    }.get(n, ""))
    assert (configured.latitude, configured.longitude) == (52.12, 5.12)
    disclosure = EverydayTools(tools.broker, configured).weather_disclosure()
    assert disclosure["location_leaves_home"] is True and "52" not in json.dumps(disclosure)


def test_everyday_provider_defaults_and_secret_resolution():
    assert everyday_provider_from_env(lambda n: "").model("everyday") == "gpt-4.1-mini"
    openrouter = everyday_provider_from_env(lambda n: {"REACHY_EVERYDAY_PROVIDER": "openrouter"}.get(n, ""))
    assert openrouter.base_url == "https://openrouter.ai/api/v1"
    assert openrouter.model("everyday") == "openai/gpt-4.1-mini"
    keys = {"OPENROUTER_API_KEY": "or-key", "OPENAI_API_KEY": "oa"}
    assert openrouter.api_key(lambda n: keys.get(n, "")) == "or-key"
    assert "store" not in openrouter.request({"store": False, "max_completion_tokens": 5})
    with pytest.raises(LLMProviderConfigError, match="REACHY_EVERYDAY_MODEL"):
        everyday_provider_from_env(lambda n: {"REACHY_EVERYDAY_PROVIDER": "cortecs"}.get(n, ""))


def test_answer_runs_bounded_tool_loop_with_compatible_payloads():
    seen = []

    async def chat(request):
        body = await request.json()
        seen.append((request.headers["Authorization"], body))
        if len(seen) == 1:
            return web.json_response({"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "get_current_time", "arguments": "{}"}},
                {"id": "t2", "type": "function", "function": {"name": "ask_hermes", "arguments": "{}"}},
            ]}}]})
        return web.json_response({"choices": [{"message": {"content": "It's half past eleven."}}]})

    async def main():
        app = web.Application()
        app.router.add_post("/v1/chat/completions", chat)
        server = TestServer(app)
        await server.start_server()
        try:
            provider = everyday_provider_from_env(lambda n: {
                "REACHY_EVERYDAY_PROVIDER": "openrouter",
                "REACHY_EVERYDAY_URL": str(server.make_url("/v1")),
            }.get(n, ""))
            async with ClientSession() as http:
                result = await make_tools("http://127.0.0.1:1").answer(
                    "what time is it", http=http, provider=provider, key="or-key", device_id="dev", generation=1,
                )
        finally:
            await server.close()
        return result

    result = asyncio.run(main())
    assert result["text"] == "It's half past eleven."
    assert result["tools"] == [{"name": "get_current_time", "ok": True, "verified": False},
                               {"name": "ask_hermes", "ok": False, "verified": False}]
    header, first = seen[0]
    assert header == "Bearer or-key" and first["model"] == "openai/gpt-4.1-mini" and "store" not in first
    assert {t["function"]["name"] for t in first["tools"]} == EVERYDAY_TOOL_NAMES
    tool_messages = [m for m in seen[1][1]["messages"] if m["role"] == "tool"]
    assert json.loads(tool_messages[0]["content"])["local_time"] == "11:30"
    assert "not available in Everyday" in tool_messages[1]["content"]
