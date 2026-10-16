"""Host Everyday expansion: synthetic transports only, no device or account writes."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from companion.everyday_tools import EverydayConfig, EverydayTools, TurnBudget, realtime_tool_specs
from companion.reachy_agent_actions import ActionConfig, AgentActionService


def make_tools(**kwargs):
    actions = AgentActionService(ActionConfig(**kwargs))
    broker = SimpleNamespace(actions=actions, config=SimpleNamespace(home_entities={}))
    return EverydayTools(broker)


def invoke(tools, name, args=None, **kwargs):
    return tools.call(
        name,
        args or {},
        kwargs.pop("http", None),
        device_id="dev",
        generation=7,
        budget=kwargs.pop("budget", TurnBudget()),
        **kwargs,
    )


def test_new_host_surface_and_shared_mutation_budget():
    names = {s["name"] for s in realtime_tool_specs()}
    assert {
        "set_timer",
        "list_timers",
        "timer_status",
        "cancel_timer",
        "create_reminder",
        "list_reminders",
        "reminder_status",
        "cancel_reminder",
        "public_search",
        "home_brightness",
        "home_scene",
    } <= names
    budget = TurnBudget()
    assert budget.admit("a", "set_reachy_volume") == ""
    assert "one device change" in budget.admit("b", "set_timer")
    assert budget.admit("c", "get_reachy_status") == ""


def test_timer_delivery_is_required_and_stale_reads_do_not_run():
    async def main():
        tools = make_tools()
        result = await invoke(tools, "set_timer", {"seconds": 5})
        assert not result["ok"] and "delivery" in result["error"]
        assert not tools.broker.actions._scheduled_tasks
        assert not (await invoke(tools, "list_timers", is_current=lambda: False))["ok"]

    asyncio.run(main())


def test_device_scoped_schedule_lifecycle_across_wakes_and_no_false_delivered_claim():
    async def main():
        tools = make_tools(reminder_callback_url="https://callback.invalid", reminder_callback_token="synthetic")
        try:
            created = await invoke(tools, "set_timer", {"seconds": 60, "label": "tea"})
            assert created["ok"] and created["scope"] == "adult_device_ram"
            timer_id = created["timer_id"]
            listed = await invoke(tools, "list_timers")
            assert listed["items"][0]["state"] == "scheduled"
            assert 0 < listed["items"][0]["remaining_seconds"] <= 60
            next_wake = await tools.call(
                "timer_status", {"timer_id": timer_id}, None, device_id="dev", generation=8, budget=TurnBudget()
            )
            assert next_wake["ok"] and next_wake["timer_id"] == timer_id
            foreign = await tools.call(
                "timer_status", {"timer_id": timer_id}, None, device_id="other", generation=8, budget=TurnBudget()
            )
            assert not foreign["ok"]
            cancelled = await invoke(tools, "cancel_timer", {"timer_id": timer_id})
            assert cancelled["cancelled"] and not tools.broker.actions._scheduled_tasks
            assert not (await invoke(tools, "list_timers"))["items"]
            reminder = await invoke(tools, "create_reminder", {"seconds": 30, "text": "water plants"})
            assert reminder["ok"]
            key = ("everyday:dev", 0)
            item = tools.broker.actions._reminders[key][reminder["reminder_id"]]
            item["due_at"] = 0
            status = await invoke(tools, "reminder_status", {"reminder_id": reminder["reminder_id"]})
            assert status["state"] == "delivery_unconfirmed" and status["remaining_seconds"] == 0
            assert not (await invoke(tools, "cancel_reminder", {"reminder_id": reminder["reminder_id"]}))["ok"]
        finally:
            await tools.broker.actions.shutdown()

    asyncio.run(main())


@pytest.mark.parametrize("seconds", [True, 0, 86401, 1.1, "5"])
def test_timer_rejects_invalid_arguments(seconds):
    async def main():
        tools = make_tools(reminder_callback_url="https://callback.invalid", reminder_callback_token="synthetic")
        assert not (await invoke(tools, "set_timer", {"seconds": seconds}))["ok"]
        assert not tools.broker.actions._scheduled_tasks

    asyncio.run(main())


def test_brightness_and_scene_exact_allowlists_and_readback():
    async def main():
        state = {
            "light.desk": {"state": "on", "attributes": {"brightness": 100}},
            "scene.evening": {"state": "2020-01-01T00:00:00+00:00", "attributes": {}},
        }
        writes = []
        verify = [True]

        async def read(request):
            return web.json_response(state[request.match_info["entity"]])

        async def write(request):
            body = await request.json()
            writes.append((request.path, body))
            if verify[0]:
                if body["entity_id"].startswith("scene."):
                    state[body["entity_id"]]["state"] = datetime.now(UTC).isoformat()
                else:
                    state[body["entity_id"]]["attributes"]["brightness"] = round(body["brightness_pct"] * 255 / 100)
            return web.json_response([])

        app = web.Application()
        app.router.add_get("/api/states/{entity}", read)
        app.router.add_post("/api/services/{domain}/{service}", write)
        async with TestServer(app) as server, ClientSession() as http:
            tools = make_tools(
                hass_url=str(server.make_url("")).rstrip("/"),
                hass_token="fake",
                home_actions={"light.desk": frozenset({"turn_on"}), "scene.evening": frozenset({"turn_on"})},
            )
            good = await invoke(tools, "home_brightness", {"name": "light.desk", "brightness": 50}, http=http)
            assert good["verified"] and good["brightness"] == 50
            verify[0] = False
            bad = await invoke(tools, "home_brightness", {"name": "light.desk", "brightness": 90}, http=http)
            assert not bad["verified"]
            assert not (
                await invoke(tools, "home_brightness", {"name": "scene.evening", "brightness": 20}, http=http)
            )["ok"]
            verify[0] = True
            scene = await invoke(tools, "home_scene", {"name": "scene.evening"}, http=http)
            assert scene["ok"] and scene["verified"] and scene["activation_verified"]
            assert writes[-1] == ("/api/services/scene/turn_on", {"entity_id": "scene.evening"})
            before = len(writes)
            assert not (await invoke(tools, "home_scene", {"name": "scene.unknown"}, http=http))["ok"]
            assert len(writes) == before

    asyncio.run(main())


def test_search_configuration_is_fixed_and_unconfigured_is_honest():
    with pytest.raises(ValueError, match="SEARCH_PROVIDER"):
        EverydayConfig.from_env(lambda n: {"REACHY_EVERYDAY_SEARCH_PROVIDER": "http://localhost"}.get(n, ""))

    async def main():
        assert not (await invoke(make_tools(), "public_search", {"query": "latest news"}))["ok"]

    asyncio.run(main())


def test_schedule_revalidates_inside_service_lock_after_cancellation():
    async def main():
        tools = make_tools(reminder_callback_url="https://callback.invalid", reminder_callback_token="synthetic")
        current = [True]
        async with tools.broker.actions._lock:
            task = asyncio.create_task(invoke(tools, "set_timer", {"seconds": 60}, is_current=lambda: current[0]))
            await asyncio.sleep(0)
            current[0] = False
        result = await task
        assert not result["ok"] and not tools.broker.actions._scheduled_tasks

    asyncio.run(main())


def test_configured_schedule_delivers_only_to_authenticated_existing_callback():
    async def main():
        delivered = asyncio.Event()
        received = []

        async def delivery(request):
            received.append((request.headers["Authorization"], await request.json()))
            delivered.set()
            return web.json_response({"accepted": True})

        app = web.Application()
        app.router.add_post("/api/agent/reminder-delivery", delivery)
        async with TestServer(app) as server, ClientSession() as http:
            tools = make_tools(
                reminder_callback_url=str(server.make_url("")).rstrip("/"), reminder_callback_token="synthetic"
            )
            try:
                created = await invoke(tools, "create_reminder", {"seconds": 1, "text": "tea"}, http=http)
                await asyncio.wait_for(delivered.wait(), 2)
                assert received == [("Bearer synthetic", {"item_id": created["reminder_id"], "text": "Reminder: tea"})]
                status = await invoke(tools, "reminder_status", {"reminder_id": created["reminder_id"]})
                assert status["state"] == "delivery_unconfirmed"
            finally:
                await tools.broker.actions.shutdown()

    asyncio.run(main())


@pytest.mark.parametrize("revoke", ["session", "kids", "allowlist"])
def test_held_brightness_read_never_admits_stale_or_revoked_write(revoke):
    async def main():
        tools = make_tools(home_actions={"light.desk": frozenset({"turn_on"})})
        entered, release = asyncio.Event(), asyncio.Event()
        current, kids, writes = [True], [False], []

        async def read(entity, http):
            entered.set()
            await release.wait()
            return {"state": "on", "attributes": {"brightness": 20}}

        async def write(*args):
            writes.append(args)

        tools.broker.actions._state = read
        tools.broker.actions._call = write
        task = asyncio.create_task(
            invoke(
                tools,
                "home_brightness",
                {"name": "light.desk", "brightness": 50},
                is_current=lambda: current[0],
                kids_live=lambda: kids[0],
            )
        )
        await asyncio.wait_for(entered.wait(), 1)
        if revoke == "session":
            current[0] = False
        elif revoke == "kids":
            kids[0] = True
        else:
            tools.broker.actions.config.home_actions.clear()
        release.set()
        result = await task
        assert not result["ok"] and not writes

    asyncio.run(main())


def test_schedule_capacity_and_shutdown_reuse_service_global_bound():
    async def main():
        tools = make_tools(reminder_callback_url="https://callback.invalid", reminder_callback_token="synthetic")
        try:
            for _ in range(32):
                assert (await invoke(tools, "set_timer", {"seconds": 60}))["ok"]
            assert not (await invoke(tools, "create_reminder", {"seconds": 60, "text": "tea"}))["ok"]
            assert len(tools.broker.actions._scheduled_tasks) == 32
        finally:
            await tools.broker.actions.shutdown()
        status = await invoke(tools, "list_timers")
        assert all(item["state"] == "delivery_unconfirmed" for item in status["items"])

    asyncio.run(main())


def test_capability_readiness_is_configuration_only_and_never_exposes_secrets():
    tools = make_tools()
    assert not tools.capability_groups()["timers"]["configured"]
    assert not tools.capability_groups()["public_lookup"]["configured"]
    tools.config.search_provider, tools.config.search_api_key = "brave", "hidden-search-token"
    tools.broker.actions.config.reminder_callback_url = "https://callback.invalid"
    tools.broker.actions.config.reminder_callback_token = "hidden-callback-token"
    groups = tools.capability_groups()
    assert groups["timers"]["configured"] and groups["public_lookup"]["configured"]
    assert "hidden-" not in str(groups) and "hidden-search-token" not in repr(tools.config)
