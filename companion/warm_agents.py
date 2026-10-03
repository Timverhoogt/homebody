"""Warm, per-session Hermes agents for the Reachy bridge's latency-sensitive routes.

Hermes' API server builds a new ``AIAgent`` for every request: provider resolution, tool
discovery, memory-provider start-up and a freshly assembled system prompt on each spoken turn.
Hermes' own messaging gateway avoids that by caching one agent per conversation. This module does
the same for the two Reachy routes that wait on Hermes while someone is listening:

* ``pipeline``: the Hermes pipeline mode's ``/v1/chat/completions`` turns;
* ``realtime``: the Realtime ``ask_hermes`` delegation.

The pool is opt-in (``REACHY_HERMES_WARM_AGENTS=1``) and keeps the authority boundary of the HTTP
path:

* **Session serialization.** One turn at a time per (route, session). A second turn waits in a
  bounded queue rather than sharing an agent's mutable history.
* **Cache signatures.** Each turn recomputes a fingerprint of everything baked into a cached agent:
  model and provider, a hash of the credential, enabled toolsets, the ephemeral system prompt,
  the memory scope and the Hermes config files. A change rebuilds the agent before the turn.
* **Lifecycle controls.** Idle, age and turn limits, an LRU cap, explicit eviction (owner route,
  Kids Mode start, shutdown), and retirement of any agent whose turn failed, timed out or was
  cancelled. A retired agent is closed only after its worker thread has finished.
* **Usage accounting.** Per-route hits, cold builds, rebuild reasons, build and turn seconds, and
  token use. Status never contains prompts, transcripts, credentials or raw session ids.

The pool itself is backend-neutral and unit-tested with a fake factory. ``HermesAgentFactory``
adapts it to Hermes Agent's in-process ``AIAgent`` and fails closed: when Hermes cannot be
imported, or a built agent exposes a tool outside the Reachy boundary, no turn runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

_LOGGER = logging.getLogger("hermes_reachy_bridge.warm_agents")

WARM_ROUTES = ("pipeline", "realtime")
_HISTORY_MESSAGE_LIMIT = 200


class WarmAgentUnavailable(RuntimeError):
    """No turn ran: the caller may safely use the HTTP path instead."""


class WarmAgentBusy(RuntimeError):
    """Another turn for the same session held the agent for longer than the queue allows."""


class WarmAgentBoundaryError(RuntimeError):
    """A built agent exposed a tool outside the Reachy boundary; it was closed unused."""


class WarmAgentTurnError(RuntimeError):
    """The turn started and failed. It may have had side effects, so never retry it elsewhere."""


def _bounded(raw: str | None, default: float, low: float, high: float) -> float:
    try:
        value = float(raw) if raw not in (None, "") else default
    except ValueError:
        value = default
    return max(low, min(value, high))


@dataclass(frozen=True, slots=True)
class WarmAgentSettings:
    enabled: bool = False
    routes: frozenset[str] = frozenset(WARM_ROUTES)
    models: frozenset[str] = frozenset({"hermes-agent"})
    idle_seconds: float = 600.0
    max_age_seconds: float = 3600.0
    max_turns: int = 40
    max_agents: int = 4
    queue_seconds: float = 30.0
    turn_timeout_seconds: float = 170.0

    @classmethod
    def from_env(cls, getenv: Callable[[str, str], str] = os.getenv) -> WarmAgentSettings:
        raw_routes = getenv("REACHY_HERMES_WARM_ROUTES", ",".join(WARM_ROUTES))
        routes = {part.strip().lower() for part in raw_routes.split(",")}
        models = {part.strip() for part in getenv("REACHY_HERMES_WARM_MODELS", "hermes-agent").split(",")}
        return cls(
            enabled=getenv("REACHY_HERMES_WARM_AGENTS", "").strip() == "1",
            routes=frozenset(routes & set(WARM_ROUTES)),
            models=frozenset(model for model in models if model),
            idle_seconds=_bounded(getenv("REACHY_HERMES_WARM_IDLE_SECONDS", ""), 600, 30, 3600),
            max_age_seconds=_bounded(getenv("REACHY_HERMES_WARM_MAX_AGE_SECONDS", ""), 3600, 300, 86_400),
            max_turns=int(_bounded(getenv("REACHY_HERMES_WARM_MAX_TURNS", ""), 40, 1, 200)),
            max_agents=int(_bounded(getenv("REACHY_HERMES_WARM_MAX_AGENTS", ""), 4, 1, 16)),
            queue_seconds=_bounded(getenv("REACHY_HERMES_WARM_QUEUE_SECONDS", ""), 30, 1, 120),
        )

    def public(self) -> dict[str, object]:
        return {
            "routes": sorted(self.routes),
            "idle_seconds": self.idle_seconds,
            "max_age_seconds": self.max_age_seconds,
            "max_turns": self.max_turns,
            "max_agents": self.max_agents,
            "queue_seconds": self.queue_seconds,
        }


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """What a turn asks for. Everything here that shapes the agent belongs in its signature."""

    route: str
    session_id: str
    system_prompt: str
    memory_scope: str = ""


@dataclass(frozen=True, slots=True)
class PreparedAgent:
    signature: str
    build_args: Any = None


@dataclass(slots=True)
class TurnResult:
    text: str
    history: list[dict[str, Any]]
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True)
class WarmTurn:
    text: str
    route: str
    warm: bool
    rebuild_reason: str
    build_seconds: float
    turn_seconds: float
    input_tokens: int
    output_tokens: int

    def usage(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.input_tokens,
            "completion_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
        }


class AgentFactory(Protocol):
    """Blocking operations; the pool runs each of them on a worker thread."""

    def availability(self) -> str | None:
        """``None`` when agents can be built, otherwise a short public reason."""

    def prepare(self, spec: AgentSpec) -> PreparedAgent: ...

    def build(self, spec: AgentSpec, prepared: PreparedAgent) -> tuple[Any, list[dict[str, Any]]]:
        """Return a new agent and the persisted history to continue from."""

    def run_turn(self, agent: Any, user_message: str, history: list[dict[str, Any]]) -> TurnResult: ...

    def interrupt(self, agent: Any) -> None: ...

    def close(self, agent: Any) -> None: ...


@dataclass(slots=True)
class _Entry:
    agent: Any
    signature: str
    history: list[dict[str, Any]]
    created_at: float
    last_used: float
    turns: int = 0


@dataclass(slots=True)
class _Gate:
    """Serializes one (route, session). ``users`` counts holders and waiters, so a gate is only
    forgotten when nobody can still be queued on it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


@dataclass(slots=True)
class _RouteStats:
    turns: int = 0
    warm_hits: int = 0
    cold_builds: int = 0
    failed_turns: int = 0
    fallbacks: int = 0
    build_seconds: float = 0.0
    turn_seconds: float = 0.0
    last_turn_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    rebuilds: Counter[str] = field(default_factory=Counter)
    evictions: Counter[str] = field(default_factory=Counter)

    def public(self) -> dict[str, object]:
        return {
            "turns": self.turns,
            "warm_hits": self.warm_hits,
            "cold_builds": self.cold_builds,
            "failed_turns": self.failed_turns,
            "fallbacks": self.fallbacks,
            "build_seconds": round(self.build_seconds, 3),
            "turn_seconds": round(self.turn_seconds, 3),
            "last_turn_seconds": round(self.last_turn_seconds, 3),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "rebuilds": dict(self.rebuilds),
            "evictions": dict(self.evictions),
        }


def _session_label(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:10]


class WarmAgentPool:
    def __init__(
        self,
        factory: AgentFactory,
        settings: WarmAgentSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.factory = factory
        self.settings = settings
        self._clock = clock
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()
        self._gates: dict[tuple[str, str], _Gate] = {}
        self._stats = {route: _RouteStats() for route in WARM_ROUTES}
        self._background: set[asyncio.Future[Any]] = set()
        self._unavailable: str | None = None
        self._availability_checked = False
        self._closed = False
        self._sweeper: asyncio.Task[None] | None = None
        # Bumped by every evict(); a turn that started before one never leaves its agent cached.
        self._epoch = 0
        self._last_evict_reason = ""

    # -- routing ----------------------------------------------------------------------------

    def accepts(self, route: str, model: str) -> bool:
        """Whether this route/model may use a warm agent; everything else stays on HTTP."""
        if self._closed or not self.settings.enabled or route not in self.settings.routes:
            return False
        if (model or "").strip() not in self.settings.models:
            return False
        return self.unavailable_reason() is None

    def unavailable_reason(self) -> str | None:
        if not self._availability_checked:
            self._availability_checked = True
            try:
                self._unavailable = self.factory.availability()
            except Exception as exc:  # pragma: no cover - defensive; a factory must not crash routing
                self._unavailable = f"warm agents unavailable ({type(exc).__name__})"
            if self._unavailable:
                _LOGGER.warning("Warm Hermes agents disabled: %s", self._unavailable)
        return self._unavailable

    def record_fallback(self, route: str) -> None:
        if route in self._stats:
            self._stats[route].fallbacks += 1

    # -- turns -----------------------------------------------------------------------------

    async def run_turn(self, spec: AgentSpec, user_message: str) -> WarmTurn:
        if spec.route not in WARM_ROUTES:
            raise ValueError(f"unknown warm route {spec.route!r}")
        if self._closed:
            raise WarmAgentUnavailable("warm agent pool is closed")
        key = (spec.route, spec.session_id)
        gate = self._gates.setdefault(key, _Gate())
        gate.users += 1
        try:
            await asyncio.wait_for(gate.lock.acquire(), timeout=self.settings.queue_seconds)
        except TimeoutError as exc:
            self._leave(key, gate)
            raise WarmAgentBusy("another Hermes turn is still running for this conversation") from exc
        except BaseException:
            self._leave(key, gate)
            raise
        release_now = True
        epoch = self._epoch
        try:
            entry, reason, build_seconds = await self._checkout(key, spec)
            stats = self._stats[spec.route]
            started = self._clock()
            worker = asyncio.ensure_future(
                asyncio.to_thread(self.factory.run_turn, entry.agent, user_message, list(entry.history))
            )
            try:
                result = await asyncio.wait_for(asyncio.shield(worker), timeout=self.settings.turn_timeout_seconds)
            except BaseException as exc:
                # Cancelled, timed out or failed: the agent's state is unknown. Stop it, keep the
                # session locked until its thread has finished, and never reuse it.
                stats.failed_turns += 1
                self._retire(key, entry, "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed")
                if not worker.done():
                    self._safe(self.factory.interrupt, entry.agent)
                    release_now = False

                    def finish(agent: Any = entry.agent) -> None:
                        self._close_later(agent)
                        gate.lock.release()
                        self._leave(key, gate)

                    self._after(worker, finish)
                else:
                    self._close_later(entry.agent)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, TimeoutError):
                    raise WarmAgentTurnError("Hermes did not finish the turn in time") from exc
                raise WarmAgentTurnError(f"Hermes turn failed ({type(exc).__name__})") from exc
            elapsed = self._clock() - started
            entry.history = [
                message
                for message in result.history
                if isinstance(message, dict) and message.get("role") != "system"
            ][-_HISTORY_MESSAGE_LIMIT:]
            entry.turns += 1
            entry.last_used = self._clock()
            if epoch != self._epoch and self._entries.get(key) is entry:
                # Evicted while this agent was being built: answer, then let it go.
                self._retire(key, entry, self._last_evict_reason)
                self._close_later(entry.agent)
            stats.turns += 1
            stats.turn_seconds += elapsed
            stats.last_turn_seconds = elapsed
            stats.input_tokens += max(0, int(result.input_tokens))
            stats.output_tokens += max(0, int(result.output_tokens))
            return WarmTurn(
                text=result.text,
                route=spec.route,
                warm=reason == "",
                rebuild_reason=reason,
                build_seconds=build_seconds,
                turn_seconds=elapsed,
                input_tokens=max(0, int(result.input_tokens)),
                output_tokens=max(0, int(result.output_tokens)),
            )
        finally:
            if release_now:
                gate.lock.release()
                self._leave(key, gate)

    def _leave(self, key: tuple[str, str], gate: _Gate) -> None:
        gate.users -= 1
        if gate.users <= 0 and key not in self._entries and self._gates.get(key) is gate:
            self._gates.pop(key, None)

    def _busy(self, key: tuple[str, str]) -> bool:
        gate = self._gates.get(key)
        return gate is not None and gate.lock.locked()

    async def _checkout(self, key: tuple[str, str], spec: AgentSpec) -> tuple[_Entry, str, float]:
        """Return a reusable agent for ``key`` (reason ``""``) or build one and say why."""
        try:
            prepared = await asyncio.to_thread(self.factory.prepare, spec)
        except WarmAgentUnavailable:
            raise
        except Exception as exc:
            raise WarmAgentUnavailable(f"could not resolve the Hermes runtime ({type(exc).__name__})") from exc
        stats = self._stats[spec.route]
        entry = self._entries.get(key)
        now = self._clock()
        reason = "cold"
        if entry is not None:
            if entry.signature != prepared.signature:
                reason = "signature"
            elif now - entry.last_used > self.settings.idle_seconds:
                reason = "idle"
            elif now - entry.created_at > self.settings.max_age_seconds:
                reason = "age"
            elif entry.turns >= self.settings.max_turns:
                reason = "turns"
            else:
                self._entries.move_to_end(key)
                stats.warm_hits += 1
                return entry, "", 0.0
            self._retire(key, entry, reason)
            self._close_later(entry.agent)
        started = self._clock()
        try:
            agent, history = await asyncio.to_thread(self.factory.build, spec, prepared)
        except WarmAgentBoundaryError:
            raise
        except Exception as exc:
            raise WarmAgentUnavailable(f"could not build a Hermes agent ({type(exc).__name__})") from exc
        build_seconds = self._clock() - started
        entry = _Entry(
            agent=agent,
            signature=prepared.signature,
            history=[item for item in history if isinstance(item, dict)][-_HISTORY_MESSAGE_LIMIT:],
            created_at=self._clock(),
            last_used=self._clock(),
        )
        self._entries[key] = entry
        stats.cold_builds += 1
        stats.build_seconds += build_seconds
        if reason != "cold":
            stats.rebuilds[reason] += 1
        self._enforce_cap(protect=key)
        return entry, reason, build_seconds

    # -- lifecycle -------------------------------------------------------------------------

    def _retire(self, key: tuple[str, str], entry: _Entry, reason: str) -> None:
        if self._entries.get(key) is entry:
            self._entries.pop(key, None)
        self._stats[key[0]].evictions[reason] += 1
        gate = self._gates.get(key)
        if gate is not None and gate.users <= 0:
            self._gates.pop(key, None)

    async def start(self) -> None:
        """Probe Hermes off the event loop and start the idle sweeper."""
        if not self.settings.enabled:
            return
        await asyncio.to_thread(self.unavailable_reason)
        if self._unavailable is None and self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep_loop(), name="homebody-warm-agent-sweeper")

    async def _sweep_loop(self) -> None:
        interval = max(5.0, min(self.settings.idle_seconds / 4, 60.0))
        while True:
            await asyncio.sleep(interval)
            try:
                await self.sweep()
            except Exception:  # pragma: no cover - the sweeper must survive one bad pass
                _LOGGER.warning("Warm Hermes agent sweep failed", exc_info=True)

    def _enforce_cap(self, *, protect: tuple[str, str]) -> None:
        for key in list(self._entries):
            if len(self._entries) <= self.settings.max_agents:
                return
            if key == protect or self._busy(key):
                continue
            entry = self._entries[key]
            self._retire(key, entry, "capacity")
            self._close_later(entry.agent)

    async def sweep(self) -> int:
        """Close idle, expired and worn-out agents that are not mid-turn."""
        now = self._clock()
        evicted = 0
        for key, entry in list(self._entries.items()):
            if self._busy(key):
                continue
            if now - entry.last_used > self.settings.idle_seconds:
                reason = "idle"
            elif now - entry.created_at > self.settings.max_age_seconds:
                reason = "age"
            else:
                continue
            self._retire(key, entry, reason)
            self._close_later(entry.agent)
            evicted += 1
        return evicted

    async def evict(self, *, reason: str, route: str | None = None) -> int:
        """Drop every warm agent (optionally one route's); turns in flight keep running and are not reused."""
        self._epoch += 1
        self._last_evict_reason = reason
        evicted = 0
        for key, entry in list(self._entries.items()):
            if route is not None and key[0] != route:
                continue
            gate = self._gates.get(key)
            self._retire(key, entry, reason)
            if gate is not None and gate.lock.locked():
                # The running turn still holds this agent: close it once that turn lets go.
                self._close_when_unlocked(gate.lock, entry.agent)
            else:
                self._close_later(entry.agent)
            evicted += 1
        return evicted

    async def close(self) -> None:
        self._closed = True
        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
            self._sweeper = None
        await self.evict(reason="shutdown")
        pending = [task for task in self._background if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=10)

    def _close_when_unlocked(self, lock: asyncio.Lock, agent: Any) -> None:
        async def wait_then_close() -> None:
            async with lock:
                pass
            await asyncio.to_thread(self._safe, self.factory.close, agent)

        self._track(asyncio.ensure_future(wait_then_close()))

    def _close_later(self, agent: Any) -> None:
        self._track(asyncio.ensure_future(asyncio.to_thread(self._safe, self.factory.close, agent)))

    def _after(self, future: asyncio.Future[Any], callback: Callable[[], Any]) -> None:
        def done(_: asyncio.Future[Any]) -> None:
            if not future.cancelled():
                future.exception()  # retrieve, so a late failure is not logged as unhandled
            callback()

        future.add_done_callback(done)
        self._track(future)

    def _track(self, future: asyncio.Future[Any]) -> None:
        self._background.add(future)
        future.add_done_callback(self._background.discard)

    @staticmethod
    def _safe(function: Callable[[Any], Any], agent: Any) -> None:
        try:
            function(agent)
        except Exception:
            _LOGGER.warning("Warm Hermes agent cleanup failed", exc_info=True)

    # -- status ----------------------------------------------------------------------------

    def status(self) -> dict[str, object]:
        now = self._clock()
        reason = self.unavailable_reason() if self.settings.enabled else None
        return {
            "enabled": self.settings.enabled and not self._closed,
            "available": self.settings.enabled and reason is None and not self._closed,
            "unavailable_reason": reason,
            "settings": self.settings.public(),
            "agents": [
                {
                    "route": route,
                    "session": _session_label(session_id),
                    "turns": entry.turns,
                    "age_seconds": round(now - entry.created_at, 1),
                    "idle_seconds": round(now - entry.last_used, 1),
                    "busy": self._busy((route, session_id)),
                }
                for (route, session_id), entry in self._entries.items()
            ],
            "routes": {route: stats.public() for route, stats in self._stats.items()},
        }


# -- Hermes Agent adapter ------------------------------------------------------------------


def _file_fingerprint(paths: Iterable[Path]) -> list[list[object]]:
    fingerprint: list[list[object]] = []
    for path in paths:
        try:
            stat = path.stat()
            fingerprint.append([path.name, stat.st_mtime_ns, stat.st_size])
        except OSError:
            fingerprint.append([path.name, None, None])
    return fingerprint


class HermesAgentFactory:
    """Build Hermes ``AIAgent`` instances the way Hermes' API server does, but keep them.

    It runs inside the Hermes Agent virtualenv, like the bridge's STT/TTS helpers, and imports
    Hermes lazily. The imports are internal Hermes APIs, so every mismatch disables warm agents
    and the bridge keeps using the API server over HTTP.
    """

    def __init__(
        self,
        *,
        ensure_imports: Callable[[], None],
        hermes_home: Path,
        prohibited_tools: frozenset[str],
        profile: str | None = None,
    ) -> None:
        self._ensure_imports = ensure_imports
        self._hermes_home = hermes_home
        self._prohibited_tools = prohibited_tools
        self._profile = profile
        self._modules: dict[str, Any] | None = None

    def availability(self) -> str | None:
        if self._profile:
            # In-process Hermes reads HERMES_HOME at import time; pointing it at a profile would also
            # move the bridge's own secret lookups. Profiles keep the per-request HTTP path.
            return "warm agents support the default Hermes profile only"
        try:
            self._load()
        except WarmAgentUnavailable as exc:
            return str(exc)
        return None

    def _load(self) -> dict[str, Any]:
        if self._modules is not None:
            return self._modules
        try:
            self._ensure_imports()
            from gateway.run import (  # type: ignore[import-not-found]
                _load_gateway_config,
                _resolve_gateway_model,
                _resolve_runtime_agent_kwargs,
            )
            from hermes_cli.tools_config import _get_platform_tools  # type: ignore[import-not-found]
            from run_agent import AIAgent  # type: ignore[import-not-found]
        except Exception as exc:
            raise WarmAgentUnavailable(f"Hermes Agent is not importable here ({type(exc).__name__})") from exc
        modules: dict[str, Any] = {
            "AIAgent": AIAgent,
            "load_config": _load_gateway_config,
            "resolve_model": _resolve_gateway_model,
            "resolve_runtime": _resolve_runtime_agent_kwargs,
            "platform_tools": _get_platform_tools,
            "max_iterations": None,
            "session_db": None,
        }
        try:
            from gateway.run import _current_max_iterations  # type: ignore[import-not-found]

            modules["max_iterations"] = _current_max_iterations
        except Exception:
            pass
        try:
            from hermes_state_registry import acquire  # type: ignore[import-not-found]

            modules["session_db"] = acquire
        except Exception:
            pass
        self._modules = modules
        return modules

    def prepare(self, spec: AgentSpec) -> PreparedAgent:
        modules = self._load()
        runtime = dict(modules["resolve_runtime"]())
        model = runtime.pop("model", None) or modules["resolve_model"]()
        runtime.pop("_fallback_notice", None)
        config = modules["load_config"]()
        toolsets = sorted(modules["platform_tools"](config, "api_server"))
        api_key = str(runtime.get("api_key") or "")
        blob = json.dumps(
            [
                spec.route,
                spec.system_prompt,
                spec.memory_scope,
                str(model),
                runtime.get("provider") or "",
                runtime.get("base_url") or "",
                runtime.get("api_mode") or "",
                hashlib.sha256(api_key.encode("utf-8")).hexdigest() if api_key else "",
                toolsets,
                _file_fingerprint(
                    self._hermes_home / name for name in ("config.yaml", ".env", "SOUL.md")
                ),
            ],
            sort_keys=True,
            default=str,
        )
        signature = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
        return PreparedAgent(
            signature=signature, build_args={"model": model, "runtime": runtime, "toolsets": toolsets}
        )

    def build(self, spec: AgentSpec, prepared: PreparedAgent) -> tuple[Any, list[dict[str, Any]]]:
        modules = self._load()
        args = prepared.build_args
        session_db = None
        if modules["session_db"] is not None:
            try:
                session_db = modules["session_db"]()
            except Exception:
                _LOGGER.warning("Hermes session store unavailable; warm turns will not see stored history")
        max_iterations = 30
        if modules["max_iterations"] is not None:
            try:
                max_iterations = int(modules["max_iterations"]())
            except Exception:
                pass
        agent = modules["AIAgent"](
            model=args["model"],
            **args["runtime"],
            max_iterations=max_iterations,
            quiet_mode=True,
            verbose_logging=False,
            ephemeral_system_prompt=spec.system_prompt or None,
            enabled_toolsets=args["toolsets"],
            session_id=spec.session_id,
            platform="api_server",
            session_db=session_db,
            gateway_session_key=spec.memory_scope or None,
        )
        exposed = {str(name) for name in (getattr(agent, "valid_tool_names", None) or ())}
        if exposed & self._prohibited_tools:
            self.close(agent)
            raise WarmAgentBoundaryError("Reachy requests are blocked from broad host capabilities")
        history: list[dict[str, Any]] = []
        if session_db is not None:
            try:
                stored = session_db.get_messages_as_conversation(spec.session_id)
                history = [item for item in stored or [] if isinstance(item, dict)]
            except Exception:
                _LOGGER.warning("Could not load stored Hermes history for a warm agent")
        return agent, history

    def run_turn(self, agent: Any, user_message: str, history: list[dict[str, Any]]) -> TurnResult:
        # Mirror Hermes' gateway when it reuses a cached agent for a new turn.
        agent._last_activity_ts = time.time()
        if hasattr(agent, "_last_flushed_db_idx"):
            agent._last_flushed_db_idx = 0
        agent._api_call_count = 0
        before_in = int(getattr(agent, "session_prompt_tokens", 0) or 0)
        before_out = int(getattr(agent, "session_completion_tokens", 0) or 0)
        result = agent.run_conversation(
            user_message=user_message,
            conversation_history=history,
            task_id=str(getattr(agent, "session_id", "") or "") or None,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Hermes returned an invalid turn result")
        text = str(result.get("final_response") or "").strip()
        if not text:
            raise RuntimeError("Hermes returned an empty response")
        messages = result.get("messages")
        if not isinstance(messages, list):
            messages = [*history, {"role": "user", "content": user_message}, {"role": "assistant", "content": text}]
        return TurnResult(
            text=text,
            history=messages,
            input_tokens=int(getattr(agent, "session_prompt_tokens", 0) or 0) - before_in,
            output_tokens=int(getattr(agent, "session_completion_tokens", 0) or 0) - before_out,
        )

    def interrupt(self, agent: Any) -> None:
        interrupt = getattr(agent, "interrupt", None)
        if not callable(interrupt):
            return
        try:
            interrupt("Reachy cancelled this turn", hard_cancel=True)
        except TypeError:
            interrupt()

    def close(self, agent: Any) -> None:
        close = getattr(agent, "close", None)
        if callable(close):
            close()
