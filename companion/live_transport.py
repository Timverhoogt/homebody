"""GPT-Live (preview) relay: the robot speaks to the bridge, the bridge to OpenAI Live.

Credentials and delegation stay on the host. Live has no response/audio-done marker, so no
Realtime events are synthesized: the robot's playback queue owns the audible state. Live sends
only ``delegation.id`` and ``offset_ms`` for a client task, so the request is rebuilt from
bounded, RAM-only transcript deltas with corrections, late-delivery grace and task revisions.

Only a fixed allowlist of provider events reaches the robot. Backend answers go to
``session.commentary.append`` (spoken, verified result) or ``session.thinking.append``
(facts, e.g. a superseded result); ``session.instructions.append`` carries only fixed bridge
policy text, never model, user or tool output.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

try:
    from companion.everyday_manifest import (
        EVERYDAY_TURN_TIMEOUT_SECONDS,
        LocalExecutor,
        LocalToolTransport,
        validate_local_tool_names,
    )
except ModuleNotFoundError:
    from everyday_manifest import (
        EVERYDAY_TURN_TIMEOUT_SECONDS,
        LocalExecutor,
        LocalToolTransport,
        validate_local_tool_names,
    )

_LOGGER = logging.getLogger(__name__)
_TURN_CURRENT: ContextVar[Callable[[], bool]] = ContextVar("live_turn_current", default=lambda: True)


def current_live_turn() -> bool:
    """Original delegation revision shared by host tools as well as local RPCs."""
    return _TURN_CURRENT.get()()

LIVE_URL = "wss://api.openai.com/v1/live/sessions"
LIVE_MODELS = frozenset({"gpt-live-1"})
LIVE_VOICES = frozenset({"marin", "cedar"})
MAX_DELEGATIONS = 64
# Live bounds each append to 500 tokens; 480 UTF-8 bytes stays under that even at one byte/token.
MAX_APPEND_BYTES = 480
MAX_INPUT_AUDIO_BYTES = 96_000
TRANSCRIPT_GRACE_SECONDS = 0.8
CLOSE_GRACE_SECONDS = 2.0
_MAX_MESSAGE_BYTES = 2 * 1024 * 1024
_FORWARDED = frozenset(
    {"session.output_audio.delta", "session.input_transcript.delta", "session.output_transcript.delta"}
)

LIVE_INSTRUCTIONS = (
    "You are Homebody speaking through a Reachy Mini robot. Be concise and natural. "
    "Delegate to the client for the current time or date, weather, home devices, and any action. "
    "Speak delegated results faithfully; never claim a device changed or a fact is current without "
    "a delegated result. Ask for clarification when the request is unclear. "
    "Camera, robot movement and personal memory are unavailable in this session."
)


def utf8_clip(text: str, limit: int = MAX_APPEND_BYTES) -> str:
    """Clip to ``limit`` bytes on a character boundary (never a broken multi-byte sequence)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def event(kind: str, **fields: object) -> dict[str, object]:
    return {"type": kind, "event_id": uuid.uuid4().hex, **fields}


@dataclass(slots=True)
class _Segment:
    role: str
    start_ms: int
    end_ms: int
    text: str


class LiveTranscript:
    """Bounded transcript evidence keyed by audio time, so corrections replace, not duplicate."""

    def __init__(self, limit: int = 240) -> None:
        self.limit = limit
        self.segments: dict[tuple[str, int], _Segment] = {}
        self.consumed_ms = -1
        self.changed = asyncio.Event()

    def observe(self, payload: dict[str, Any]) -> None:
        kind = payload.get("type")
        if kind not in {"session.input_transcript.delta", "session.output_transcript.delta"}:
            return
        start, end = payload.get("start_ms"), payload.get("end_ms")
        text = str(payload.get("delta") or "")[:1000]
        if type(start) is not int or type(end) is not int or end < start or not text:
            return
        role = "user" if kind == "session.input_transcript.delta" else "assistant"
        # A revised fragment that spans earlier ones replaces them (correction/dedupe).
        for key in [
            k
            for k, s in self.segments.items()
            if s.role == role and start <= s.start_ms and s.end_ms <= end and k != (role, start)
        ]:
            del self.segments[key]
        self.segments[(role, start)] = _Segment(role, start, end, text)
        while len(self.segments) > self.limit:
            del self.segments[min(self.segments, key=lambda k: self.segments[k].start_ms)]
        self.changed.set()

    def user_end_ms(self) -> int:
        return max((s.end_ms for s in self.segments.values() if s.role == "user"), default=-1)

    def _utterances(self, *, after: int, until: int) -> list[tuple[str, str]]:
        ordered = sorted(
            (s for s in self.segments.values() if after < s.start_ms <= until),
            key=lambda s: (s.start_ms, s.end_ms),
        )
        merged: list[tuple[str, str]] = []
        for segment in ordered:
            if merged and merged[-1][0] == segment.role:
                merged[-1] = (segment.role, merged[-1][1] + segment.text)
            else:
                merged.append((segment.role, segment.text))
        return [(role, " ".join(text.split())) for role, text in merged if text.strip()]

    def request(self, offset_ms: int) -> tuple[str, str]:
        """Return ``(current user request, bounded earlier context)`` for one delegation."""
        current = [text for role, text in self._utterances(after=self.consumed_ms, until=offset_ms) if role == "user"]
        earlier = self._utterances(after=-1, until=self.consumed_ms)[-4:]
        context = "\n".join(f"{role}: {text}" for role, text in earlier)[-1200:]
        return " ".join(current)[-2000:], context

    def consume(self, offset_ms: int) -> None:
        self.consumed_ms = max(self.consumed_ms, offset_ms)

    def clear(self) -> None:
        self.segments.clear()


async def wait_for_transcript(transcript: LiveTranscript, offset_ms: int, grace: float) -> None:
    """Delegation may arrive before its last user delta; wait (bounded) for coverage."""
    deadline = asyncio.get_running_loop().time() + grace
    while transcript.user_end_ms() < offset_ms:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        transcript.changed.clear()
        try:
            await asyncio.wait_for(transcript.changed.wait(), remaining)
        except TimeoutError:
            return


def _never_look(_text: str, *, engaged: bool = False) -> bool:
    return False


@dataclass(slots=True)
class LiveSessionState:
    look_request: Callable[..., bool] = _never_look
    look_timeout: float = 45.0
    look_engaged: bool = False
    look_waiters: dict[str, Any] = field(default_factory=dict)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    seen: set[str] = field(default_factory=set)
    revision: int = 0
    delegations: int = 0


class AudioCarry:
    """Keep PCM16 frames whole: carry an odd trailing byte into the next chunk."""

    def __init__(self) -> None:
        self.pending = b""

    def push(self, raw: bytes) -> bytes:
        data = self.pending + raw
        cut = len(data) - (len(data) % 2)
        self.pending = data[cut:]
        return data[:cut]


@dataclass(frozen=True, slots=True)
class DelegationResult:
    """A backend reply with its truthful delegation state: ``answered``, ``not_ready`` (setup needed) or
    ``failed`` (fixed text says nothing is confirmed)."""

    text: str
    state: str = "answered"


class DelegationFailure(Exception):
    """A categorical, text-free failure: ``category`` is logged, ``spoken`` is fixed bridge text."""

    def __init__(self, category: str, spoken: str) -> None:
        super().__init__(category)
        self.category, self.spoken = category, spoken


_CATEGORY = frozenset("abcdefghijklmnopqrstuvwxyz_")
_UNVERIFIED = "The backend could not safely verify a result. Nothing is confirmed."

Answer = Callable[[str, str], Awaitable["str | DelegationResult"]]


LOOK_RESULT_TIMEOUT_SECONDS = 45.0
_LOOK_FAILED = "Reachy could not take the requested look right now. Nothing was done."


_no_look = _never_look


async def serve_live(
    request: web.Request,
    *,
    key: str,
    answer: Callable[[dict[str, Any]], Answer],
    allowed: Callable[[dict[str, Any]], Awaitable[bool]],
    upstream_url: str = LIVE_URL,
    connect_timeout: float = 10.0,
    started_timeout: float = 15.0,
    transcript_grace: float = TRANSCRIPT_GRACE_SECONDS,
    look_request: Callable[..., bool] = _no_look,
    look_timeout: float = LOOK_RESULT_TIMEOUT_SECONDS,
    answer_with_local: Callable[[dict[str, Any], LocalExecutor], Answer] | None = None,
) -> web.WebSocketResponse:
    """Relay one robot Live session. ``answer(config)`` returns the narrow backend for its mode;
    ``allowed(config)`` re-checks Kids/generation/authorization and ends the session when false.

    ``look_request(text, engaged=...)`` is the narrow visual-intent classifier: when the person's own
    delegated words are a deliberate look request, the robot is asked (``bridge.look_request``) to take
    one fenced look and answer with text (``client.look_result``). No frame, tool or project session is
    involved; the text is appended as the delegation result like any other backend reply.
    """
    client = web.WebSocketResponse(heartbeat=20, max_msg_size=_MAX_MESSAGE_BYTES)
    await client.prepare(request)
    state = LiveSessionState()
    state.look_request = look_request
    state.look_timeout = look_timeout
    transcript = LiveTranscript()
    tasks: set[asyncio.Task[None]] = set()
    try:
        first = await client.receive(timeout=10)
        config = json.loads(first.data) if first.type == WSMsgType.TEXT else {}
        if not isinstance(config, dict) or config.get("type") != "session.start":
            raise ValueError("session.start required")
        # The original wire context must remain fixed for this socket's lifetime.
        config = json.loads(json.dumps(config))
        if config.get("assistant_mode") == "everyday":
            validate_local_tool_names(config.get("local_tools", []))
        model, voice = config.get("model"), config.get("voice")
        if model not in LIVE_MODELS or voice not in LIVE_VOICES:
            raise ValueError("unsupported Live model or voice")
        timeout = float(config.get("timeout_seconds", 300))
        if not 30 <= timeout <= 900:
            raise ValueError("invalid Live timeout")
        if not await allowed(config):
            raise PermissionError("Live session is not allowed in the current state")
        backend = answer(config)
        await _relay(client, config, key, backend, allowed, state, transcript, tasks, upstream_url,
                     connect_timeout, started_timeout, timeout, transcript_grace, str(model), str(voice),
                     answer_with_local)
    except Exception as exc:
        _LOGGER.warning("Live relay ended (%s)", type(exc).__name__)
        if not client.closed:
            message = (
                "Live is blocked in the current state"
                if isinstance(exc, PermissionError)
                else "Live transport unavailable or session invalid"
            )
            await client.send_json({"type": "bridge.error", "error": message})
    finally:
        state.closed.set()
        transcript.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not client.closed:
            await client.close()
    return client


async def _relay(client, config, key, backend, allowed, state, transcript, tasks, upstream_url,
                 connect_timeout, started_timeout, timeout, transcript_grace, model, voice,
                 answer_with_local=None) -> None:
    send_lock = asyncio.Lock()
    backend_lock = asyncio.Lock()
    carry = AudioCarry()
    local = LocalToolTransport(config.get("local_tools", [])
                               if config.get("assistant_mode") == "everyday" else [], client.send_json)
    active_turn: ContextVar[tuple[int, float]] = ContextVar("live_local_turn")

    async def local_execute(name: str, arguments: dict) -> dict:
        revision, deadline = active_turn.get()
        def current() -> bool:
            return not state.closed.is_set() and not client.closed and state.revision == revision
        if not current() or not await allowed(config):
            raise PermissionError("stale Live operation")
        result = await local.execute(name, arguments, deadline=deadline, current=current)
        if not current() or not await allowed(config):
            raise PermissionError("stale Live result")
        return result

    if local.names and answer_with_local is not None:
        backend = answer_with_local(config, local_execute)
    async with ClientSession(timeout=ClientTimeout(total=None, connect=connect_timeout)) as http:
        async with http.ws_connect(
            upstream_url, headers={"Authorization": f"Bearer {key}"}, max_msg_size=_MAX_MESSAGE_BYTES, heartbeat=20
        ) as upstream:

            async def send(payload: dict[str, object]) -> bool:
                async with send_lock:
                    if state.closed.is_set() or upstream.closed:
                        return False
                    await upstream.send_json(payload)
                    return True

            await send(event("session.start", session={
                "model": model,
                "instructions": (LIVE_INSTRUCTIONS.replace(
                    "Camera, robot movement and personal memory are unavailable in this session.",
                    "Delegate explicit camera, speaker, robot status, power and gesture requests to the client. "
                    "Never claim an operation succeeded without a delegated result. Personal memory is unavailable.")
                    if local.names and answer_with_local is not None else LIVE_INSTRUCTIONS),
                "store": False,
                "audio": {"format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": voice}},
                "delegation": {"type": "client"},
            }))
            started = await upstream.receive(timeout=started_timeout)
            payload = json.loads(started.data) if started.type == WSMsgType.TEXT else {}
            if payload.get("type") != "session.started":
                raise RuntimeError("Live session was not accepted")
            await client.send_json({"type": "session.started"})

            async def append(kind: str, delegation_id: str | None, content: str) -> None:
                await send(event(kind, delegation_id=delegation_id, content=utf8_clip(content)))

            async def robot_look(identifier: str, question: str, *, follow_up: bool = False) -> DelegationResult:
                """Ask the robot for one fenced look; its text answer (or categorical failure) is the result."""
                future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
                state.look_waiters[identifier] = future
                # Once visual data can enter this cloud session, no subsequent delegation may
                # inherit project/home tools. Stop + a fresh wake is the deliberate exit.
                state.look_engaged = True
                try:
                    kind = "bridge.look_follow_up_request" if follow_up else "bridge.look_request"
                    await client.send_json(
                        {"type": kind, "delegation_id": identifier, "question": utf8_clip(question, 500)}
                    )
                    async with asyncio.timeout(state.look_timeout):
                        result = await future
                except TimeoutError as exc:
                    raise DelegationFailure("look_timeout", _LOOK_FAILED) from exc
                finally:
                    state.look_waiters.pop(identifier, None)
                text = result.get("text")
                if isinstance(text, str) and text.strip():
                    state.look_engaged = True
                    return DelegationResult(utf8_clip(text, 1200), "answered")
                raise DelegationFailure("look_failed", _LOOK_FAILED)

            async def delegate(identifier: str, offset_ms: int, revision: int) -> None:
                await wait_for_transcript(transcript, offset_ms, transcript_grace)
                text, context = transcript.request(offset_ms)
                transcript.consume(offset_ms)
                async with backend_lock:
                    if state.closed.is_set():
                        return
                    if revision != state.revision:
                        await append(
                            "session.thinking.append", identifier, "Superseded by a newer request; not executed."
                        )
                        await client.send_json({"type": "bridge.delegation", "state": "superseded"})
                        return
                    if not text:
                        await append(
                            "session.commentary.append",
                            identifier,
                            "I didn't catch the request completely. Ask the user to repeat it.",
                        )
                        await client.send_json({"type": "bridge.delegation", "state": "clarify"})
                        return
                    await client.send_json({"type": "bridge.delegation", "state": "started"})
                    outcome = "answered"
                    try:
                        if not await allowed(config):
                            raise PermissionError("not allowed")
                        token = active_turn.set((revision, asyncio.get_running_loop().time()
                                                 + EVERYDAY_TURN_TIMEOUT_SECONDS))
                        current_token = _TURN_CURRENT.set(
                            lambda: not state.closed.is_set() and not client.closed and state.revision == revision)
                        try:
                            if state.look_request(text, engaged=state.look_engaged):
                                reply = await robot_look(identifier, text)
                            elif state.look_engaged:
                                reply = await robot_look(identifier, text, follow_up=True)
                            elif local.names and answer_with_local is not None:
                                async with asyncio.timeout(EVERYDAY_TURN_TIMEOUT_SECONDS):
                                    reply = await backend(text, context)
                            else:
                                # Legacy and Workspace retain their existing backend budgets.
                                reply = await backend(text, context)
                        finally:
                            _TURN_CURRENT.reset(current_token)
                            active_turn.reset(token)
                        if not await allowed(config):
                            raise PermissionError("not allowed")
                        if isinstance(reply, DelegationResult):
                            result = reply.text
                            outcome = reply.state if reply.state in {"answered", "not_ready", "failed"} else "failed"
                        else:
                            result = str(reply)
                    except asyncio.CancelledError:
                        raise
                    except PermissionError:
                        state.closed.set()
                        return
                    except DelegationFailure as exc:
                        category = exc.category if set(exc.category) <= _CATEGORY else "unknown"
                        _LOGGER.warning("Live delegation failed (category=%s)", category[:48])
                        result, outcome = exc.spoken, "failed"
                    except Exception as exc:
                        # Class name only: exception text may quote the request or provider output.
                        _LOGGER.warning("Live delegation failed (category=unexpected %s)", type(exc).__name__)
                        result, outcome = _UNVERIFIED, "failed"
                    if state.closed.is_set():
                        return  # late result after Stop/privacy: never revive speech
                    if revision != state.revision:
                        await append(
                            "session.thinking.append", identifier, "Result for a superseded request: " + result
                        )
                        await client.send_json({"type": "bridge.delegation", "state": "superseded"})
                        return
                    await append("session.commentary.append", identifier, result)
                    # Only a verified backend reply is "answered"; setup and failure are reported as such.
                    await client.send_json({"type": "bridge.delegation", "state": outcome})

            async def receive_provider() -> None:
                async for message in upstream:
                    if message.type != WSMsgType.TEXT:
                        return
                    payload = json.loads(message.data)
                    if not isinstance(payload, dict):
                        continue
                    kind = payload.get("type")
                    transcript.observe(payload)
                    if kind == "session.delegation.created":
                        delegation = payload.get("delegation") or {}
                        identifier = delegation.get("id") if isinstance(delegation, dict) else None
                        nested = delegation.get("offset_ms") if isinstance(delegation, dict) else None
                        offset = payload.get("offset_ms", nested)
                        if (
                            not isinstance(identifier, str)
                            or not identifier
                            or identifier in state.seen
                            or delegation.get("target") != "client"
                            or type(offset) is not int
                        ):
                            continue
                        state.seen.add(identifier)
                        state.delegations += 1
                        if state.delegations > MAX_DELEGATIONS:
                            await append(
                                "session.commentary.append",
                                identifier,
                                "The task limit for this conversation is reached. Nothing was done.",
                            )
                            continue
                        state.revision += 1  # a newer request supersedes any not-yet-answered one
                        task = asyncio.create_task(delegate(identifier, offset, state.revision))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                    elif kind == "session.output_audio.delta":
                        raw = payload.get("delta", payload.get("audio"))
                        try:
                            pcm = carry.push(base64.b64decode(str(raw or ""), validate=True))
                        except (binascii.Error, ValueError):
                            continue
                        if pcm:
                            await client.send_json({"type": kind, "delta": base64.b64encode(pcm).decode("ascii")})
                    elif kind in _FORWARDED:
                        await client.send_json(
                            {k: payload[k] for k in ("type", "delta", "start_ms", "end_ms") if k in payload}
                        )
                    elif kind == "session.closed":
                        return
                    elif kind == "error":
                        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
                        _LOGGER.warning("Live provider error (%s)", str(error.get("code") or "unknown")[:60])
                        await client.send_json({"type": "bridge.warning", "error": "Live reported an error"})

            async def receive_client() -> None:
                async for message in client:
                    if message.type != WSMsgType.TEXT:
                        return
                    payload = json.loads(message.data)
                    kind = payload.get("type") if isinstance(payload, dict) else None
                    if kind == "session.close":
                        return
                    if kind == "client.look_result":
                        identifier = payload.get("delegation_id")
                        waiter = state.look_waiters.get(identifier) if isinstance(identifier, str) else None
                        if waiter is not None and not waiter.done():
                            waiter.set_result({"text": payload.get("text"), "error": payload.get("error")})
                        continue  # unknown or late results are dropped, never spoken
                    if kind == "everyday.tool_result":
                        if state.closed.is_set() or not await allowed(config):
                            raise PermissionError("stale Live result")
                        local.receive(payload)
                        continue  # robot RPC results never go to the speech provider
                    if kind != "session.input_audio.append" or not isinstance(payload.get("audio"), str):
                        raise ValueError("unsupported Live client event")
                    try:
                        raw = base64.b64decode(payload["audio"], validate=True)
                    except (binascii.Error, ValueError):
                        continue
                    if not raw or len(raw) > MAX_INPUT_AUDIO_BYTES or len(raw) % 2:
                        continue  # robot frames are whole PCM16 samples; drop malformed chunks
                    await send({"type": kind, "audio": payload["audio"]})

            async def boundary() -> None:
                deadline = asyncio.get_running_loop().time() + timeout
                while asyncio.get_running_loop().time() < deadline and not state.closed.is_set():
                    if not await allowed(config):
                        return
                    await asyncio.sleep(0.1)

            pumps = [asyncio.create_task(fn()) for fn in (receive_provider, receive_client, boundary)]
            try:
                await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            finally:
                state.closed.set()  # reject late delegation callbacks before cloud close
                local.close()
                for task in tasks | set(pumps):
                    task.cancel()
                await asyncio.gather(*tasks, *pumps, return_exceptions=True)
                await _finalize(upstream, client)


async def _finalize(upstream, client) -> None:
    """Graceful, bounded cloud close. Local playback was already cleared by the robot."""
    if upstream.closed:
        return
    try:
        await upstream.send_json(event("session.close"))
        async with asyncio.timeout(CLOSE_GRACE_SECONDS):
            while True:
                message = await upstream.receive()
                if message.type != WSMsgType.TEXT:
                    return
                payload = json.loads(message.data)
                if isinstance(payload, dict) and payload.get("type") == "session.closed":
                    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
                    numbers = {k: v for k, v in usage.items() if type(v) is int}
                    _LOGGER.info("Live session closed; usage %s", numbers)
                    if not client.closed:
                        await client.send_json({"type": "session.closed"})
                    return
    except (TimeoutError, ConnectionError, RuntimeError) as exc:
        _LOGGER.info("Live close was not confirmed (%s)", type(exc).__name__)
