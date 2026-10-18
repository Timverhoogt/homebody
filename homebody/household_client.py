"""Opt-in household MCP robot attachment; never impersonates microphone input.

The embedding app owns this coroutine's lifetime. It is not started on import and
never changes config. Only an existing adult runtime/session may attach.
"""
from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field
from websockets.asyncio.client import connect as _WebSocketConnect
from websockets.exceptions import WebSocketException

from .everyday_runtime import configured_local_tools


class connect(_WebSocketConnect):
    """Pin credential-bearing handshakes to the Owner-approved endpoint."""

    def process_redirect(self, exc: Exception) -> Exception:
        # Reject even same-origin redirects; never start a second handshake.
        return exc


class HouseholdConnectionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=2048)
    robot_credential: str = Field(default="", max_length=4096)
    enabled: bool
    acknowledged: bool


class HouseholdRobotExecutor:
    """External-tool authority, distinct from household speech/camera consent."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.permissions = self._permissions()
        self.cancel_requested: Callable[[], bool] = lambda: False
        owner = self

        class PermissionCancellation(threading.Event):
            def is_set(self):
                return super().is_set() or owner.cancel_requested() or owner._permissions() != owner.permissions

        self.cancelled = PermissionCancellation()
        self.fence = runtime.everyday_tool_fence(self.cancelled)
        self.names = self._names()
        self.seen = set()
        self.require_current()

    def _permissions(self):
        c = self.runtime.config_loader()
        return (c.mcp_enabled, c.mcp_vision_enabled, c.local_vision_enabled,
                c.camera_enabled, c.power_tools_enabled, c.robot_tools_enabled,
                c.household_setup_version, c.camera_natural_requests_enabled,
                c.household_url, c.household_api_key, c.household_connection_enabled)

    def _names(self):
        c = self.runtime.config_loader()
        names = configured_local_tools(c)
        if not (c.mcp_vision_enabled and c.local_vision_enabled):
            names = [n for n in names if n not in {"capture_reachy_camera", "start_reachy_camera"}]
        return names

    def require_current(self):
        self.runtime._require_everyday_current(self.fence)
        if not self.permissions[0] or self._permissions() != self.permissions:
            self.cancelled.set()
            raise RuntimeError("Household external-tool permission changed")

    def call(self, name, arguments, call_id):
        try:
            self.require_current()
            if name not in self.names or call_id in self.seen or len(self.seen) >= 30:
                raise RuntimeError("Unavailable or repeated household call")
            self.seen.add(call_id)
            # Original cancellation/generation fence reaches capture and movement workers.
            # Camera uses the runtime's one-frame local vision path, not voice callback consent.
            call_fence = self.runtime.everyday_tool_fence(self.cancelled)
            self.require_current()
            value = self.runtime.execute_everyday_tool(name, arguments, fence=call_fence, call_id=call_id)
            self.require_current()
        except (ValueError, RuntimeError):
            value = {"ok": False, "error": "Household call refused or cancelled"}
        return {"content": [{"type": "text", "text": json.dumps(value)}],
                "structuredContent": value, "isError": value.get("ok") is not True}


async def household_callbacks(
    runtime, *, bridge_url: str, robot_token: str, device_id: str, context, ready: asyncio.Event | None = None,
):
    """Attach one current adult generation, at most five minutes / thirty calls.

    Return True only for the completed call quota, False for early clean EOF.

    Pass the real runtime's agent_broker_context(), not model-supplied context. A
    caller cancellation, Stop, Kids, privacy or permission change closes the socket.
    Reattachment belongs to the embedding app, never the MCP caller.
    """
    parsed = urlsplit(bridge_url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Explicit household host URL required")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Non-loopback household callbacks require HTTPS")
    if not robot_token or not device_id:
        raise ValueError("Robot credential and identity required")
    url = urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc,
                     parsed.path.rstrip("/") + "/v1/household/robot", "", ""))
    executor = HouseholdRobotExecutor(runtime)
    owner_task = asyncio.current_task()
    executor.cancel_requested = lambda: owner_task is not None and owner_task.cancelling() > 0
    guard = None
    try:
        async with asyncio.timeout(300), connect(
            url, additional_headers={"Authorization": "Bearer " + robot_token, "X-Reachy-Device-Id": device_id},
            open_timeout=5, close_timeout=1, max_size=64 * 1024,
        ) as socket:
            executor.require_current()
            payload_context = asdict(context) if is_dataclass(context) and not isinstance(context, type) else context
            await socket.send(json.dumps({"type": "household.attach", "local_tools": executor.names,
                                          "context": payload_context}))
            if json.loads(await socket.recv()) != {"type": "household.ready"}:
                raise RuntimeError("Household attachment refused")
            if ready is not None:
                ready.set()

            async def boundary():
                try:
                    while True:
                        executor.require_current()
                        await asyncio.sleep(0.05)
                except RuntimeError:
                    executor.cancelled.set()
                    await socket.close()

            guard = asyncio.create_task(boundary())
            async for raw in socket:
                payload = json.loads(raw)
                if (type(payload) is not dict or set(payload) != {"type", "call_id", "name", "arguments"}
                        or payload["type"] != "everyday.tool_call"
                        or type(payload["call_id"]) is not str or not 1 <= len(payload["call_id"]) <= 128):
                    raise ValueError("Invalid household callback")
                executor.require_current()
                result = await asyncio.to_thread(
                    executor.call, payload["name"], payload["arguments"], payload["call_id"]
                )
                executor.require_current()
                await socket.send(json.dumps({"type": "everyday.tool_result", "call_id": payload["call_id"],
                                              "result": result}))
                if len(executor.seen) >= 30:
                    break  # renew the transport, never replay a call
            return len(executor.seen) >= 30
    finally:
        executor.cancelled.set()
        if guard is not None:
            guard.cancel()
            await asyncio.gather(guard, return_exceptions=True)


async def household_connection(runtime, *, ready=None):
    """Renew bounded leases only within the original Owner-approved authority.

    Transport failures get three retries; a completed lease renews without waking,
    capturing, or re-authorizing. Stop/privacy/Kids/settings changes permanently
    retire this task. Another Owner connect is needed after authority is revoked.
    """
    approval = HouseholdRobotExecutor(runtime)
    config = runtime.config_loader()
    if not config.household_connection_enabled:
        raise RuntimeError("Durable household connection is not approved")
    failures = 0
    while True:
        approval.require_current()
        connected = asyncio.Event()
        async def report_ready(connected=connected):
            await connected.wait()
            if ready is not None:
                ready.set()
        reporter = asyncio.create_task(report_ready())
        try:
            completed = await household_callbacks(
                runtime, bridge_url=config.household_endpoint, robot_token=config.household_credential,
                device_id=config.instance_id,
                context=runtime.agent_broker_context(explicit_private_intent=False), ready=connected)
            failures = 0 if completed is True else failures + 1
        except TimeoutError:
            failures = 0 if connected.is_set() else failures + 1
        except (OSError, RuntimeError, WebSocketException):
            failures += 1
        finally:
            reporter.cancel()
            await asyncio.gather(reporter, return_exceptions=True)
        approval.require_current()
        if failures > 3:
            raise RuntimeError("Household reconnect limit reached")
        await asyncio.sleep(min(2 ** failures, 8))


def install_household_connection_routes(app, runtime_loader, config_loader):
    """Owner-bound opt-in connection; callers cannot supply URLs, tokens or context.

    Install only on the settings app, behind its existing Owner/CSRF middleware.
    Nothing connects until the Owner posts connect; saved settings are not changed.
    """
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    from .config import config_transaction, save_config
    from .owner_auth import owner_authenticated

    task = None
    lock = asyncio.Lock()

    @app.post("/api/household/tools/configure")
    async def configure_tools(request: HouseholdConnectionSettings):
        nonlocal task
        if not owner_authenticated.get():
            raise HTTPException(status_code=403, detail="Owner approval required")
        if not request.acknowledged:
            raise HTTPException(status_code=400, detail="Acknowledge the connection disclosure first")
        from dataclasses import replace
        async with lock:
            try:
                with config_transaction():
                    current = config_loader()
                    saved = replace(current, household_url=request.url,
                                    household_api_key=(request.robot_credential or current.household_api_key)
                                    if request.url else "",
                                    household_connection_enabled=request.enabled)
                    saved.validate()
                    save_config(saved)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid household endpoint or credential pair") from None
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                task = None
        return {"saved": True, "enabled": saved.household_connection_enabled,
                "credential_configured": bool(saved.household_api_key)}

    @app.post("/api/household/tools/validate")
    async def validate_tools():
        from .hermes_client import HermesBridgeClient
        def check():
            client = HermesBridgeClient(config_loader())
            try:
                client.everyday_capabilities()
            finally:
                client.close()
        try:
            await asyncio.wait_for(asyncio.to_thread(check), 12)
        except Exception:
            raise HTTPException(status_code=409, detail="Household service validation failed") from None
        return {"ok": True, "scope": "household", "speech_services_checked": False}

    @app.get("/api/household/tools/status")
    async def tools_status():
        config = config_loader()
        return {"connected": task is not None and not task.done(),
                "url": config.household_url,
                "enabled": config.household_connection_enabled,
                "credential_configured": bool(config.household_api_key)}

    @app.post("/api/household/tools/connect")
    async def connect_tools():
        nonlocal task
        async with lock:
            if task is not None and not task.done():
                raise HTTPException(status_code=409, detail="Household tools are already connected")
            runtime = runtime_loader()
            if runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            config = config_loader()
            context = runtime.agent_broker_context(explicit_private_intent=False)
            ready = asyncio.Event()
            if config.household_connection_enabled:
                task = asyncio.create_task(household_connection(runtime, ready=ready))
            else:
                task = asyncio.create_task(household_callbacks(
                    runtime, bridge_url=config.household_endpoint, robot_token=config.household_credential,
                    device_id=config.instance_id, context=context, ready=ready))
            waiter = asyncio.create_task(ready.wait())
            try:
                done, _ = await asyncio.wait([task, waiter], timeout=6, return_when=asyncio.FIRST_COMPLETED)
                if task in done or waiter not in done:
                    raise RuntimeError("Attachment failed")
            except (RuntimeError, asyncio.CancelledError) as exc:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise HTTPException(status_code=409, detail="Household attachment unavailable") from None
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            # Retrieve eventual connection errors without logging tokens or payloads.
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            return JSONResponse(
                {"connected": True, "device_id": config.instance_id,
                 "homebody_context": asdict(context), "maximum_seconds": 300},
                headers={"Cache-Control": "no-store"},
            )

    @app.post("/api/household/tools/disconnect")
    async def disconnect_tools():
        nonlocal task
        async with lock:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                task = None
            return {"connected": False}

