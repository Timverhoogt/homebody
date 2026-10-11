"""Native REST/SSE and Gateway WebSocket transports; never chat-completions identity.

Only public message text and explicit lifecycle facts cross the display boundary.
Raw tool arguments, results, generic payloads and reasoning are not forwarded.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections import deque
from urllib.parse import quote

from aiohttp import ClientSession, ClientTimeout, WSMsgType

try:
    from companion.native_workspace import WorkspaceError
    from companion.reachy_agent_broker import redact_payload
except ModuleNotFoundError:
    from native_workspace import WorkspaceError
    from reachy_agent_broker import redact_payload


async def json_request(http, method, url, *, headers, body=None):
    async with http.request(
        method, url, headers=headers, json=body, timeout=ClientTimeout(total=20), allow_redirects=False
    ) as response:
        chunks, size = [], 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > 256_000:
                raise WorkspaceError("native response exceeded safe size")
            chunks.append(chunk)
        raw = b"".join(chunks)
        if response.status >= 300:
            # Do not leak upstream diagnostic bodies (which may include tokens).
            raise WorkspaceError(f"native endpoint returned HTTP {response.status}")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise WorkspaceError("native response must be a bounded JSON object")
        return parsed


def public_messages(payload):
    messages = payload.get("messages", payload.get("data", [])) if isinstance(payload, dict) else []
    result = []
    for message in messages[-120:]:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") in {"text", "output_text", "input_text"}
            )
        if isinstance(content, str) and content.strip():
            result.append({"role": message["role"], "text": redact_payload(content)[:4000]})
    return result


def public_tool_name(value):
    return redact_payload(value)[:80] if isinstance(value, str) else "tool"


def public_output(value):
    if isinstance(value, str):
        return str(redact_payload(value))[:4000]
    if isinstance(value, list):
        messages = public_messages({"messages": [{"role": "assistant", "content": value}]})
        return messages[0]["text"] if messages else ""
    return ""  # Never stringify a generic payload containing hidden/tool/credential fields.


class HermesNativeAdapter:
    def __init__(self, http: ClientSession, secret):
        self.http, self.secret = http, secret

    def headers(self, target):
        token = self.secret(target.token_env)
        if not isinstance(token, str) or not token:
            raise WorkspaceError("native host credential is not configured")
        return {"Authorization": f"Bearer {token}"}

    async def history(self, target):
        metadata = await json_request(
            self.http,
            "GET",
            f"{target.url}/api/sessions/{quote(target.session_id, safe='')}",
            headers=self.headers(target),
        )
        identity = metadata.get("session", metadata.get("data", metadata))
        if not isinstance(identity, dict) or identity.get("id", identity.get("session_id")) != target.session_id:
            raise WorkspaceError("native history identity mismatch")
        payload = await json_request(
            self.http,
            "GET",
            f"{target.url}/api/sessions/{quote(target.session_id, safe='')}/messages"
            "?inline_images=false&limit=30&order=latest",
            headers=self.headers(target),
        )
        if payload.get("session_id", target.session_id) != target.session_id:
            raise WorkspaceError("native history resolved to a different session")
        return public_messages(payload)

    async def submit(self, target, text, request_id):
        headers = {**self.headers(target), "Idempotency-Key": request_id}
        payload = await json_request(
            self.http,
            "POST",
            f"{target.url}/v1/runs",
            headers=headers,
            body={"session_id": target.session_id, "input": text},
        )
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise WorkspaceError("native run admission did not return an exact run identity")
        return run_id

    async def events(self, target, run_id):
        status_url = f"{target.url}/v1/runs/{quote(run_id, safe='')}"
        # Poll before attaching: a terminal run/restart must not require replay buffers.
        payload = await json_request(self.http, "GET", status_url, headers=self.headers(target))
        if payload.get("session_id") != target.session_id:
            raise WorkspaceError("native run belongs to a different session")
        if payload.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
            yield {"event": f"run.{payload['status']}", "output": public_output(payload.get("output", ""))}
            return
        async with self.http.get(
            status_url + "/events",
            headers=self.headers(target),
            allow_redirects=False,
            timeout=ClientTimeout(total=None, sock_read=30),
        ) as response:
            if response.status != 200:
                raise WorkspaceError("native event stream unavailable")
            data, size = [], 0
            async for raw in response.content:
                size += len(raw)
                if size > 256_000:
                    raise WorkspaceError("native event frame exceeded safe size")
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line:
                    if data:
                        item = json.loads("\n".join(data))
                        if item.get("run_id") not in {None, run_id}:
                            raise WorkspaceError("native event run identity mismatch")
                        event = self.public_event(item)
                        if event:
                            yield event
                            if event["event"] in {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}:
                                return
                    data, size = [], 0

    @staticmethod
    def public_event(item):
        kind = item.get("event") or item.get("type")
        if kind in {"tool.started", "tool.completed", "tool.failed"}:
            if item.get("error") is True and kind == "tool.completed":
                kind = "tool.failed"
            return {"event": kind, "tool": public_tool_name(item.get("tool", "tool"))}
        if kind == "approval.request":
            return {"event": kind}
        if kind in {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}:
            return {"event": kind, "output": public_output(item.get("output", ""))}
        return None

    async def stop(self, target, run_id):
        payload = await json_request(
            self.http,
            "POST",
            f"{target.url}/v1/runs/{quote(run_id, safe='')}/stop",
            headers=self.headers(target),
            body={},
        )
        return payload.get("status", "stopping")

    async def close(self):
        pass  # the bridge owns the shared HTTP session


class OpenClawNativeAdapter:
    """Loopback trusted-backend connection (including an owner-managed SSH tunnel).

    No insecure pairing bypass, implicit device approval, HTTP endpoint enablement,
    or primary-session selection is performed by this client.
    """

    def __init__(self, http: ClientSession, secret):
        self.http, self.secret = http, secret
        self.connections = {}
        self._lock = asyncio.Lock()

    async def connection(self, target):
        key = (target.url, target.token_env, getattr(target, "observe_approvals", False))
        async with self._lock:
            connection = self.connections.get(key)
            if connection is None or connection.ws.closed or connection.reader.done():
                if connection is not None:
                    await connection.close()
                token = self.secret(target.token_env)
                if not isinstance(token, str) or not token:
                    raise WorkspaceError("native host credential is not configured")
                connection = GatewayConnection(self.http, target.url, token, observe_approvals=key[2])
                await connection.connect()
                self.connections[key] = connection
            connection.allowed_sessions.add(target.session_id)
            return connection

    async def history(self, target):
        connection = await self.connection(target)
        resolved = await connection.rpc("sessions.resolve", {"key": target.session_id})
        if resolved.get("ok") is not True or resolved.get("key") != target.session_id:
            raise WorkspaceError("OpenClaw native session is missing, ambiguous, or changed")
        payload = await connection.rpc(
            "chat.history", {"sessionKey": target.session_id, "limit": 120, "maxBytes": 128_000}
        )
        if payload.get("sessionKey") != target.session_id or not payload.get("sessionId"):
            raise WorkspaceError("existing native session could not be verified")
        return public_messages(payload)

    async def submit(self, target, text, request_id):
        connection = await self.connection(target)
        await connection.rpc(
            "sessions.messages.subscribe",
            {
                "key": target.session_id,
                **({"includeApprovals": True} if getattr(target, "observe_approvals", False) else {}),
            },
        )
        payload = await connection.rpc(
            "chat.send",
            {
                "sessionKey": target.session_id,
                "message": text,
                "idempotencyKey": request_id,
                "deliver": False,
                "suppressCommandInterpretation": True,
            },
        )
        run_id = payload.get("runId")
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise WorkspaceError("OpenClaw admission did not return an exact run identity")
        return run_id

    async def events(self, target, run_id):
        connection = await self.connection(target)
        await connection.rpc(
            "sessions.messages.subscribe",
            {
                "key": target.session_id,
                **({"includeApprovals": True} if getattr(target, "observe_approvals", False) else {}),
            },
        )
        cursor = -1
        while not connection.ws.closed:
            for sequence, frame in list(connection.events):
                if sequence <= cursor:
                    continue
                cursor = sequence
                event = self.public_event(frame, target.session_id, run_id)
                if event:
                    yield event
                    if event["event"] in {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}:
                        return
            connection.changed.clear()
            if "agent.wait" in connection.methods:
                state = await connection.rpc("agent.wait", {"runId": run_id, "timeoutMs": 1000})
                if state.get("runId") != run_id:
                    raise WorkspaceError("OpenClaw run status identity mismatch")
                if state.get("endedAt") and state.get("status") in {"ok", "error"}:
                    yield {"event": "run.completed" if state["status"] == "ok" else "run.failed", "output": ""}
                    return
            try:
                async with asyncio.timeout(2):
                    await connection.changed.wait()
            except TimeoutError:
                # Silence is not completion or connection loss. The WebSocket owns heartbeat failure.
                continue
        raise WorkspaceError("OpenClaw connection lost")

    @staticmethod
    def public_event(frame, session_id, run_id):
        payload = frame.get("payload", {})
        if (
            isinstance(payload, dict)
            and frame.get("event") == "session.approval"
            and payload.get("sessionKey") == session_id
            and payload.get("phase") == "pending"
        ):
            return {"event": "session.approval.request"}
        if not isinstance(payload, dict) or payload.get("runId") != run_id:
            return None
        # Run ownership is exact; a supplied foreign session is still rejected.
        if payload.get("sessionKey") not in {None, session_id}:
            return None
        if frame.get("event") == "chat":
            state = payload.get("state")
            kinds = {"final": "run.completed", "error": "run.failed", "aborted": "run.cancelled"}
            if state in kinds:
                messages = public_messages({"messages": [payload.get("message", {})]})
                return {"event": kinds[state], "output": messages[0]["text"] if messages else ""}
        if frame.get("event") == "agent":
            data = payload.get("data", {})
            if payload.get("stream") == "tool" and isinstance(data, dict):
                kinds = {"start": "tool.started", "result": "tool.completed"}
                if data.get("phase") in kinds:
                    kind = "tool.failed" if data.get("isError") else kinds[data["phase"]]
                    return {"event": kind, "tool": str(data.get("name", "tool"))[:80]}
        return None

    async def stop(self, target, run_id):
        connection = await self.connection(target)
        await connection.rpc("chat.abort", {"sessionKey": target.session_id, "runId": run_id})
        return "stopping"

    async def close(self):
        for connection in self.connections.values():
            await connection.close()


class GatewayConnection:
    def __init__(self, http, url, token, *, observe_approvals=False):
        self.observe_approvals = observe_approvals
        self.http, self.url, self.token = http, url, token
        self.ws = None
        self.pending = {}
        self.events = deque(maxlen=256)
        self.sequence = 0
        self.changed = asyncio.Event()
        self.reader = None
        self.methods = set()
        self.allowed_sessions = set()

    async def connect(self):
        async with asyncio.timeout(20):
            self.ws = await self.http.ws_connect(self.url, max_msg_size=256_000, heartbeat=15)
        try:
            challenge = await self.ws.receive_json(timeout=15)
            if challenge.get("event") != "connect.challenge":
                raise WorkspaceError("OpenClaw challenge is missing")
            self.reader = asyncio.create_task(self._read())
            hello = await self.rpc(
                "connect",
                {
                    "minProtocol": 4,
                    "maxProtocol": 4,
                    "client": {"id": "gateway-client", "version": "0.4.0", "platform": "linux", "mode": "backend"},
                    "role": "operator",
                    "scopes": ["operator.read", "operator.write"]
                    + (["operator.approvals"] if self.observe_approvals else []),
                    "caps": ["tool-events"],
                    "auth": {"token": self.token},
                },
            )
            self.methods = set(hello.get("features", {}).get("methods", []))
            required = {"sessions.resolve", "chat.history", "chat.send", "chat.abort", "sessions.messages.subscribe"}
            if not required <= set(hello.get("features", {}).get("methods", [])):
                raise WorkspaceError("OpenClaw Gateway does not advertise the required native contract")
        except BaseException:
            await self.close()
            raise
        finally:
            self.token = ""  # never retain or echo the handshake credential in event/history state

    async def rpc(self, method, params):
        identifier = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.ws.send_json({"type": "req", "id": identifier, "method": method, "params": params})
            async with asyncio.timeout(20):
                return await future
        finally:
            self.pending.pop(identifier, None)

    async def _read(self):
        try:
            async for message in self.ws:
                if message.type != WSMsgType.TEXT:
                    break
                frame = json.loads(message.data)
                if frame.get("type") == "res":
                    future = self.pending.get(frame.get("id"))
                    if future and not future.done():
                        if frame.get("ok") is True:
                            future.set_result(frame.get("payload", {}))
                        else:
                            error = frame.get("error", {})
                            code = error.get("code", "RPC_REJECTED")
                            if not isinstance(code, str) or not re.fullmatch(r"[A-Z_]{1,48}", code):
                                code = "RPC_REJECTED"
                            future.set_exception(WorkspaceError(f"OpenClaw native RPC refused ({code})"))
                elif frame.get("type") == "event" and frame.get("event") in {"chat", "agent", "session.approval"}:
                    payload = frame.get("payload", {})
                    if not isinstance(payload, dict) or payload.get("sessionKey") not in self.allowed_sessions:
                        continue
                    projected = {"runId": payload.get("runId"), "sessionKey": payload.get("sessionKey")}
                    if frame["event"] == "session.approval":
                        if not self.observe_approvals or payload.get("phase") != "pending":
                            continue
                        projected["phase"] = "pending"
                    elif frame["event"] == "chat":
                        texts = [
                            m
                            for m in public_messages({"messages": [payload.get("message", {})]})
                            if m["role"] == "assistant"
                        ]
                        if payload.get("state") not in {"delta", "final", "error", "aborted"}:
                            continue
                        projected.update(
                            state=payload.get("state"),
                            message={
                                "role": "assistant",
                                "content": texts[0]["text"] if texts else "",
                            },
                        )
                    elif payload.get("stream") == "tool":
                        data = payload.get("data", {})
                        if not isinstance(data, dict):
                            continue
                        if data.get("phase") not in ("start", "update", "result", "error", "end"):
                            continue
                        projected.update(
                            stream="tool",
                            data={
                                "phase": data.get("phase"),
                                "name": public_tool_name(data.get("name", "tool")),
                                "isError": data.get("isError") is True,
                            },
                        )
                    else:
                        continue
                    self.sequence += 1
                    self.events.append((self.sequence, {"event": frame["event"], "payload": projected}))
                    self.changed.set()
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(WorkspaceError("OpenClaw connection lost"))
            self.changed.set()

    async def close(self):
        if self.ws is not None:
            await self.ws.close()
        if self.reader is not None:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
