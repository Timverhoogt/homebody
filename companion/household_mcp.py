"""One authenticated household MCP surface over host tools and scoped robot callbacks.

MCP callers cannot establish robot leases, choose remote URLs or borrow voice consent.
"""
from __future__ import annotations

import asyncio
import json
import uuid

from aiohttp import web

try:
    from .everyday_host import validate_arguments
    from .everyday_manifest import validate_local_arguments
    from .everyday_tools import TurnBudget
    from .household_catalogue import household_specs, mcp_specs
    from .reachy_agent_broker import BrokerContext, BrokerValidationError
except ImportError:
    from everyday_host import validate_arguments
    from everyday_manifest import validate_local_arguments
    from everyday_tools import TurnBudget
    from household_catalogue import household_specs, mcp_specs
    from reachy_agent_broker import BrokerContext, BrokerValidationError


async def household_mcp(bridge, request):
    bridge.require_auth(request)
    device = request.headers.get("X-Reachy-Device-Id", "")
    try:
        message = await request.json()
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ValueError
        params = message.get("params", {})
        if not isinstance(params, dict):
            raise ValueError
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="Invalid MCP request") from None
    identifier = message.get("id")

    def result(value):
        return web.json_response({"jsonrpc": "2.0", "id": identifier, "result": value},
                                 headers={"Cache-Control": "no-store"})

    def error(code, text):
        return web.json_response({"jsonrpc": "2.0", "id": identifier,
                                  "error": {"code": code, "message": text}},
                                 headers={"Cache-Control": "no-store"})

    if "id" not in message:
        return web.Response(status=202)
    method = message.get("method")
    if method == "initialize":
        return result({"protocolVersion": "2025-03-26", "serverInfo": {"name": "homebody-household", "version": "1"},
                       "capabilities": {"tools": {"listChanged": False}}})
    if method == "ping":
        return result({})
    if method not in {"tools/list", "tools/call"}:
        return error(-32601, "Unsupported method")
    try:
        meta = params.get("_meta", {})
        context = BrokerContext.parse(meta.get("homebody_context"))
        fence = bridge.agent_broker.everyday_read_fence(device, context)
        if not device or context.capability_profile != "conversation" or fence is None or not fence():
            raise BrokerValidationError("stale_session")
        robot = getattr(bridge, "household_robots", {}).get(device)
        if getattr(bridge, "robot_api_key", "") and (
            robot is None or robot.context != context or not robot.current()
        ):
            raise BrokerValidationError("robot_unavailable")
        lease_fence = fence

        def fence():
            return (lease_fence() and not bridge.kids_session_live()
                    and (robot is None or robot.current()))

        if bridge.kids_session_live():
            raise BrokerValidationError("kids_mode")
    except (BrokerValidationError, ValueError, TypeError, AttributeError):
        return error(-32001, "A current adult household device session is required")
    specs = household_specs(local_names=robot.transport.names if robot else ())
    if method == "tools/list":
        return result({"tools": mcp_specs(specs)})
    name, arguments = params.get("name"), params.get("arguments", {})
    spec = next((s for s in specs if s["name"] == name), None)
    if not isinstance(name, str) or spec is None or not validate_arguments(spec, arguments):
        return error(-32602, "Unknown tool or invalid arguments")
    if robot is not None and name in robot.transport.names:
        try:
            validate_local_arguments(name, arguments)
        except ValueError:
            return error(-32602, "Invalid robot arguments")
    if bridge.http is None:
        return error(-32000, "Household transport is unavailable")
    request_id = uuid.uuid4().hex
    task = asyncio.current_task()
    if robot is not None:
        robot.tasks.add(task)
    try:
        await bridge.agent_broker.register_request(device, context, request_id)
        if robot is not None and name in robot.transport.names:
            value = await robot.transport.execute(
                name, arguments, deadline=asyncio.get_running_loop().time() + 15, current=fence,
            )
            if not fence():
                raise BrokerValidationError("stale_session")
            return result(value)  # Robot already returns a bounded MCP tool result.
        value = await bridge.everyday.call(
            name, arguments, bridge.http, device_id=device, generation=context.session_generation,
            budget=TurnBudget(), kids_live=bridge.kids_session_live, is_current=fence,
        )
        if not fence():
            raise BrokerValidationError("stale_session")
    except (BrokerValidationError, asyncio.CancelledError, PermissionError, TimeoutError):
        return error(-32001, "Household request was cancelled")
    finally:
        if robot is not None:
            robot.tasks.discard(task)
        await bridge.agent_broker.unregister_request(device, context.session_generation, request_id)
    return result({"content": [{"type": "text", "text": json.dumps(value)}],
                   "structuredContent": value, "isError": value.get("ok") is not True})
