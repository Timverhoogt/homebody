"""Read-only indoor averages through real broker + loopback HA; no household effects."""

import asyncio
from datetime import UTC, datetime

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from companion.everyday_tools import EverydayConfig, EverydayTools, TurnBudget, realtime_tool_specs
from companion.reachy_agent_broker import BrokerConfig, ReachyAgentBroker

NOW = datetime(2026, 10, 17, 7, tzinfo=UTC).timestamp()
STAMP = "2026-10-17T06:30:00+00:00"


def test_config_is_explicit_sensor_only_and_bounded():
    for raw in ["{}", '["light.lamp"]', '["sensor.x","sensor.x"]', '["sensor.x/../secret"]', "null"]:
        with pytest.raises(ValueError):
            EverydayConfig.from_env(lambda k, raw=raw: raw if k == "REACHY_EVERYDAY_TEMPERATURE_ENTITIES" else "")
    c = EverydayConfig.from_env(lambda k: '["sensor.lounge"]' if k == "REACHY_EVERYDAY_TEMPERATURE_ENTITIES" else "")
    assert c.temperature_entities == ("sensor.lounge",)
    assert EverydayConfig.from_env(lambda k: "").temperature_entities == ()


def test_tool_spec_is_read_only_home_scoped():
    spec = next(x for x in realtime_tool_specs() if x["name"] == "get_home_temperature")
    assert spec["parameters"]["required"] == ["names"]
    assert not any(x["name"] == "get_home_temperature" for x in realtime_tool_specs(home=False))


def exercise(check, *, entities=("sensor.lounge", "sensor.bedroom"), allowed=None):
    async def run():
        requests = []
        states = {
            e: {
                "state": str(20 + i * 2),
                "last_updated": STAMP,
                "attributes": {"device_class": "temperature", "unit_of_measurement": "°C", "secret": "NO"},
            }
            for i, e in enumerate(entities)
        }

        async def state(request):
            assert request.headers["Authorization"] == "Bearer synthetic"
            e = request.match_info["entity"]
            requests.append(e)
            if "http_status" in states[e]:
                return web.json_response({}, status=states[e]["http_status"])
            return web.json_response(states[e])

        app = web.Application()
        app.router.add_get("/api/states/{entity}", state)
        server = TestServer(app)
        await server.start_server()
        try:
            broker = ReachyAgentBroker(
                BrokerConfig(
                    home_entities={
                        e: frozenset({"device_class", "unit_of_measurement", "last_updated"})
                        for e in (allowed if allowed is not None else entities)
                    },
                    hass_url=str(server.make_url("")).rstrip("/"),
                    hass_token="synthetic",
                )
            )
            tools = EverydayTools(
                broker,
                EverydayConfig(
                    temperature_entities=entities,
                    aliases={"sensor.lounge": ("living room",), "sensor.bedroom": ("bedroom",)},
                ),
                clock=lambda: NOW,
            )
            async with ClientSession() as http:

                async def call(args=None, **kw):
                    return await tools.call(
                        "get_home_temperature",
                        args if args is not None else {"names": []},
                        http,
                        device_id="test",
                        generation=1,
                        budget=TurnBudget(),
                        **kw,
                    )

                await check(call, states, requests, tools)
        finally:
            await server.close()

    asyncio.run(run())


def test_actual_broker_average_units_and_no_private_attributes():
    async def check(call, states, requests, tools):
        states["sensor.bedroom"]["state"] = "71.6"
        states["sensor.bedroom"]["attributes"]["unit_of_measurement"] = "°F"
        result = await call()
        assert result["ok"] is True
        assert result["average_celsius"] == 21.0 and result["sensor_count"] == 2
        assert result["partial"] is False and result["historical"] is False
        assert result["entities"][0]["last_updated"] == STAMP
        assert "NO" not in str(result)
        assert sorted(requests) == ["sensor.bedroom", "sensor.lounge"]
        assert tools.capability_groups()["temperature"]["configured"] is True

    exercise(check)


@pytest.mark.parametrize(
    "state,unit,stamp",
    [
        ("unavailable", "°C", STAMP),
        ("nan", "°C", STAMP),
        ("inf", "°C", STAMP),
        ("22", "watts", STAMP),
        ("22", "°C", "2026-10-16T01:00:00+00:00"),
        ("22", "°C", None),
        ("22", "°C", "2026-10-18T01:00:00+00:00"),
    ],
)
def test_unusable_readings_are_excluded_with_partial_disclosure(state, unit, stamp):
    async def check(call, states, requests, tools):
        states["sensor.bedroom"].update(state=state, last_updated=stamp)
        states["sensor.bedroom"]["attributes"]["unit_of_measurement"] = unit
        result = await call()
        assert result["average_celsius"] == 20 and result["sensor_count"] == 1
        assert result["partial"] is True and result["requested_count"] == 2
        assert len(result["excluded"]) == 1

    exercise(check)


def test_none_available_is_not_zero_temperature():
    async def check(call, states, requests, tools):
        for s in states.values():
            s["state"] = "unknown"
        r = await call()
        assert r["ok"] is False and r["average_celsius"] is None and r["sensor_count"] == 0

    exercise(check)


def test_names_deduplicated_and_unknown_or_invalid_requests_do_not_read():
    async def check(call, states, requests, tools):
        for args in [{"names": ["sensor.secret"]}, {"names": "bedroom"}, {}, {"names": [], "history": True}]:
            assert (await call(args))["ok"] is False
        assert requests == []
        r = await call({"names": ["living room", "sensor.lounge"]})
        assert r["sensor_count"] == 1 and requests == ["sensor.lounge"]

    exercise(check)


def test_config_cannot_widen_broker_read_allowlist():
    async def check(call, states, requests, tools):
        assert (await call())["ok"] is False and requests == []

    exercise(check, allowed=["sensor.lounge"])


def test_model_tool_roundtrip_uses_computed_temperature():
    import json
    from types import SimpleNamespace

    async def check(call, states, requests, tools):
        async def complete(body, **kwargs):
            results = [m for m in body["messages"] if m["role"] == "tool"]
            if not results:
                assert any(t["function"]["name"] == "get_home_temperature" for t in body["tools"])
                message = {
                    "tool_calls": [
                        {
                            "id": "temperature",
                            "type": "function",
                            "function": {"name": "get_home_temperature", "arguments": '{"names": []}'},
                        }
                    ]
                }
            else:
                data = json.loads(results[0]["content"])
                assert data["average_celsius"] == 21.0 and data["sensor_count"] == 2
                message = {"content": "Gemiddeld 21 graden bij de twee sensoren."}
            return {"choices": [{"message": message}]}

        provider = SimpleNamespace(
            name="openai-codex", model=lambda _: "synthetic", request=lambda b: b, complete=complete
        )
        async with ClientSession() as http:
            answer = await tools.answer(
                "Wat is de gemiddelde temperatuur in huis?",
                http=http,
                provider=provider,
                key="synthetic",
                device_id="test",
                generation=1,
                language="nl",
            )
        assert answer["text"] == "Gemiddeld 21 graden bij de twee sensoren."
        assert sorted(requests) == ["sensor.bedroom", "sensor.lounge"]

    exercise(check)


def test_stale_result_is_not_delivered():
    async def check(call, states, requests, tools):
        current = [True]
        original = tools.broker._home_status

        async def finish(*args):
            result = await original(*args)
            current[0] = False
            return result

        tools.broker._home_status = finish
        r = await call(is_current=lambda: current[0])
        assert r["ok"] is False and "entities" not in r and "average_celsius" not in r
        assert requests == ["sensor.lounge"]  # cancellation also prevents the next sensor read

    exercise(check)


@pytest.mark.parametrize("entity", ["sensor.lounge", "sensor.bedroom"])
@pytest.mark.parametrize("status", [404, 401, 403, 500])
def test_missing_entity_is_partial_but_auth_and_service_failures_are_not(entity, status):
    async def check(call, states, requests, tools):
        states[entity] = {"http_status": status}
        result = await call()
        if status == 404:
            assert result["ok"] and result["partial"] and result["sensor_count"] == 1
            assert result["requested_count"] == 2 and len(result["excluded"]) == 1
            assert result["average_celsius"] == (22.0 if entity == "sensor.lounge" else 20.0)
            assert sorted(requests) == ["sensor.bedroom", "sensor.lounge"]
        else:
            assert result["ok"] is False and "average_celsius" not in result

    exercise(check)


def test_kids_and_stale_turn_have_no_reads():
    async def check(call, states, requests, tools):
        assert (await call(kids_live=lambda: True))["ok"] is False
        assert (await call(is_current=lambda: False))["ok"] is False
        assert requests == []

    exercise(check)
