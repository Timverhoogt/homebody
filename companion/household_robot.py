"""Scoped outbound robot callback attachment, using the existing local RPC wire contract.

Only the separately authenticated robot may attach. No URLs, schemas, transcripts or
credentials supplied by an MCP caller are accepted. A disconnected peer grants nothing.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field

from aiohttp import web

try:
    from .everyday_manifest import MAX_LOCAL_MESSAGE_BYTES, LocalToolTransport
    from .reachy_agent_broker import BrokerContext
except ImportError:
    from everyday_manifest import MAX_LOCAL_MESSAGE_BYTES, LocalToolTransport
    from reachy_agent_broker import BrokerContext


@dataclass
class RobotAttachment:
    context: BrokerContext
    transport: LocalToolTransport
    current: Callable[[], bool]
    tasks: set[asyncio.Task] = field(default_factory=set)

    def close(self):
        self.transport.close()
        for task in tuple(self.tasks):
            task.cancel()


async def robot_callbacks(bridge, request):
    bridge.require_robot_auth(request)
    device = request.headers.get("X-Reachy-Device-Id", "")
    socket = web.WebSocketResponse(heartbeat=10, max_msg_size=MAX_LOCAL_MESSAGE_BYTES)
    await socket.prepare(request)
    attachment = None
    guard = None
    try:
        message = await socket.receive(timeout=5)
        payload = json.loads(message.data)
        if type(payload) is not dict or set(payload) != {"type", "context", "local_tools"}:
            raise ValueError("Invalid attachment")
        if payload["type"] != "household.attach":
            raise ValueError("Invalid attachment")
        context = BrokerContext.parse(payload["context"])
        if context.capability_profile != "conversation" or bridge.kids_session_live():
            raise PermissionError("Adult household only")
        transport = LocalToolTransport(payload["local_tools"], socket.send_json)
        await bridge.agent_broker.establish_session(device, context)
        fence = bridge.agent_broker.everyday_read_fence(device, context)
        if fence is None or not fence():
            raise PermissionError("Inactive household session")
        previous = bridge.household_robots.get(device)
        if previous is not None:
            previous.close()

        def current():
            return (not socket.closed and not transport.closed and fence()
                    and not bridge.kids_session_live() and bridge.household_robots.get(device) is attachment)

        attachment = RobotAttachment(context, transport, current)
        bridge.household_robots[device] = attachment

        async def boundary():
            while current():
                await asyncio.sleep(0.05)
            attachment.close()
            await socket.close()

        guard = asyncio.create_task(boundary())
        await socket.send_json({"type": "household.ready"})
        async for message in socket:
            if message.type != web.WSMsgType.TEXT or not current():
                break
            transport.receive(json.loads(message.data))
    except (ValueError, TypeError, KeyError, PermissionError, TimeoutError):
        pass  # Fail closed; do not expose caller input or internal state.
    finally:
        if attachment is not None:
            attachment.close()
            if bridge.household_robots.get(device) is attachment:
                bridge.household_robots.pop(device, None)
        if guard is not None:
            guard.cancel()
            await asyncio.gather(guard, return_exceptions=True)
        await socket.close()
    return socket
