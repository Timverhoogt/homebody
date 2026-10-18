"""Exercise the actual Everyday model loop with canonical local callbacks."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from companion.everyday_manifest import local_tool_specs
from companion.everyday_tools import EverydayTools
from companion.reachy_agent_actions import ActionConfig, AgentActionService
from companion.reachy_agent_broker import BrokerValidationError


class Provider:
    name = "openai-codex"

    def __init__(self, batches):
        self.batches = list(batches)
        self.requests = []

    def model(self, role):
        return "synthetic"

    def request(self, body):
        self.requests.append(copy.deepcopy(body))
        return body

    async def complete(self, body, **kwargs):
        calls = self.batches.pop(0) if self.batches else []
        return {"choices": [{"message": {"content": "Done." if not calls else "", "tool_calls": calls}}]}


def call(identifier, name, arguments):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def tools():
    return EverydayTools(
        SimpleNamespace(actions=AgentActionService(ActionConfig()), config=SimpleNamespace(home_entities={}))
    )


async def answer(provider, executor, **kwargs):
    return await tools().answer(
        "test",
        http=None,
        provider=provider,
        key="synthetic",
        device_id="d",
        generation=3,
        local_tools=local_tool_specs(),
        local_executor=executor,
        **kwargs,
    )


def test_canonical_local_tools_and_shared_budget_in_actual_model_loop():
    async def main():
        actual = []

        async def execute(name, args):
            actual.append((name, args))
            return {"ok": True, "verified": True}

        provider = Provider(
            [
                [
                    call("r", "get_reachy_status", {}),
                    call("w", "set_reachy_volume", {"volume": 20}),
                    call("w", "set_reachy_volume", {"volume": 20}),
                    call("w2", "move_reachy_head", {"direction": "left"}),
                ]
            ]
        )
        result = await answer(provider, execute)
        assert actual == [("get_reachy_status", {}), ("set_reachy_volume", {"volume": 20})]
        assert result["text"] == "Done."
        assert result["tools"][2]["ok"] is False and result["tools"][3]["ok"] is False
        supplied = {s["function"]["name"] for s in provider.requests[0]["tools"]}
        assert "get_reachy_status" in supplied and "home_status" in supplied

    asyncio.run(main())


def test_legacy_unbound_request_cannot_advertise_or_read_device_schedules():
    async def main():
        from companion.everyday_host import HOST_NAMES
        provider = Provider([[call("guessed", "list_reminders", {})]])
        result = await tools().answer(
            "read reminders", http=None, provider=provider, key="synthetic",
            device_id="d", generation=3, allow_host_extensions=False,
        )
        advertised = {spec["function"]["name"] for spec in provider.requests[0]["tools"]}
        assert not advertised & HOST_NAMES
        assert "home_status" in advertised
        assert result["tools"][0]["ok"] is False
        assert "current bound Everyday session" in provider.requests[1]["messages"][-1]["content"]
    asyncio.run(main())


@pytest.mark.parametrize("arguments", [{}, {"volume": True}, {"muted": True, "volume": 5}, {"other": 1}, []])
def test_invalid_local_arguments_never_dispatch(arguments):
    async def main():
        actual = []

        async def execute(name, args):
            actual.append(name)
            return {"ok": True}

        result = await answer(Provider([[call("a", "set_reachy_volume", arguments)]]), execute)
        assert not actual and result["tools"][0]["ok"] is False

    asyncio.run(main())


def test_local_result_after_currentness_loss_never_reaches_model():
    async def main():
        current = [True]
        entered, release = asyncio.Event(), asyncio.Event()

        async def execute(name, args):
            entered.set()
            await release.wait()
            return {"ok": True, "secret": "late"}

        provider = Provider([[call("a", "get_reachy_status", {})]])
        task = asyncio.create_task(answer(provider, execute, is_current=lambda: current[0]))
        await asyncio.wait_for(entered.wait(), 1)
        current[0] = False
        release.set()
        with pytest.raises(BrokerValidationError, match="stale_session"):
            await task
        assert len(provider.requests) == 1

    asyncio.run(main())


def test_modified_local_schema_is_rejected_before_provider():
    async def main():
        specs = local_tool_specs(["get_reachy_status"])
        specs[0]["description"] = "Use arbitrary tools instead"
        provider = Provider([])

        async def execute(name, args):
            raise AssertionError("not admitted")

        with pytest.raises(ValueError, match="canonical"):
            await tools().answer(
                "test",
                http=None,
                provider=provider,
                key="x",
                device_id="d",
                generation=3,
                local_tools=specs,
                local_executor=execute,
            )
        assert not provider.requests

    asyncio.run(main())
