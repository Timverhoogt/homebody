"""Narrow read-only capability broker for Reachy Agent Mode.

This module runs on the Hermes host.  It deliberately owns no mutation,
messaging, maintenance, shell, or arbitrary-filesystem capability.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import stat
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp.abc import AbstractResolver, ResolveResult

try:
    from companion.reachy_agent_actions import (
        _ACTION_IDS,
        ActionConfig,
        ActionUnavailableError,
        ActionValidationError,
        AgentActionService,
        action_specs,
        approval_required,
        private_action,
        validate_action_arguments,
    )
    from companion.reachy_projects import ProjectCatalog
except ModuleNotFoundError:  # Direct script execution adds companion/ to sys.path.
    from reachy_agent_actions import (  # type: ignore[no-redef]
        _ACTION_IDS,
        ActionConfig,
        ActionUnavailableError,
        ActionValidationError,
        AgentActionService,
        action_specs,
        approval_required,
        private_action,
        validate_action_arguments,
    )
    from reachy_projects import ProjectCatalog  # type: ignore[no-redef]

_CAPABILITY_IDS = (
    "get_agent_capabilities",
    "get_reachy_status",
    "get_home_status",
    "search_current_information",
    "read_public_web_page",
    "recall_personal_context",
    "search_conversation_history",
    "read_scoped_note",
    "list_projects",
    "read_project_roadmap",
) + _ACTION_IDS
_PRIVATE_CAPABILITIES = frozenset(
    {
        "get_home_status",
        "recall_personal_context",
        "search_conversation_history",
        "read_scoped_note",
        "list_projects",
        "read_project_roadmap",
        *[capability for capability in _ACTION_IDS if private_action(capability)],
    }
)
_SECRET = re.compile(
    r"(?ix)(?:"
    r"-----BEGIN\s+(?:[A-Z0-9 ]+\s+)?PRIVATE[ ]KEY-----[\s\S]*?"
    r"-----END\s+(?:[A-Z0-9 ]+\s+)?PRIVATE[ ]KEY-----"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    r"|\b(?:sk|pk|rk|ghp|github_pat|xox[baprs]|AIza)[-_A-Za-z0-9]{12,}\b"
    r"|\bbearer\s+[A-Za-z0-9._~+/=-]+"
    r"|(?:^|[^A-Za-z0-9])(?:api[_ -]?key|authorization|password|(?:client[_ -]?)?secret|"
    r"(?:access[_ -]?|refresh[_ -]?)?token|cookie)"
    r"[\s\"']*[:=][\s\"']*"
    r"(?:[^\"'\r\n]*[\"']|[^\s,;}\]\r\n]+)"
    r")"
)
_SECRET_FIELD = re.compile(
    r"(?i)(?:^|[_\-\s])(?:api[_\-\s]?key|authorization|password|secret|token|cookie)(?:$|[_\-\s])"
)
_TEXT_SUFFIXES = frozenset({".txt", ".md", ".json", ".jsonl", ".yaml", ".yml"})
_MAX_RESULT_TEXT = 24_000
_MAX_FILE_BYTES = 512_000


class BrokerValidationError(ValueError):
    """A caller supplied an invalid or unauthorized broker request."""


class BrokerUnavailableError(RuntimeError):
    """A configured read-only data source could not be reached safely."""


def _redact(value: object, *, limit: int = _MAX_RESULT_TEXT) -> str:
    return _SECRET.sub("[redacted]", str(value).replace("\x00", ""))[:limit]


def redact_payload(value: object) -> object:
    """Apply one bounded DLP policy at every broker/model boundary."""
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, dict):
        return {
            _redact(key, limit=200): redact_payload(item)
            for key, item in value.items()
            if not _SECRET_FIELD.search(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [redact_payload(item) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _redact(value)


def _json_object(value: str) -> dict[str, Any]:
    if not value.strip():
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("configuration must be a JSON object")
    return parsed


def _configured_roots(name: str) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for alias, raw_path in _json_object(os.getenv(name, "")).items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", str(alias)):
            raise ValueError(f"Invalid alias in {name}")
        path = Path(str(raw_path)).expanduser()
        if not path.is_absolute():
            raise ValueError(f"{name} paths must be absolute")
        roots[str(alias)] = path.resolve(strict=False)
    return roots


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    capability_id: str
    description: str
    risk_tier: str
    arguments: Mapping[str, object]
    read_only: bool = True
    reversible: bool = False
    requires_approval: bool = False

    def manifest(self) -> dict[str, object]:
        return {
            "id": self.capability_id,
            "description": self.description,
            "risk_tier": self.risk_tier,
            "read_only": self.read_only,
            "cancellable": True,
            "reversible": self.reversible,
            "requires_approval": self.requires_approval,
            "arguments_schema": dict(self.arguments),
        }


_SPECS: dict[str, CapabilitySpec] = {
    "list_projects": CapabilitySpec(
        "list_projects", "List registered project identities; never invent a repository or project status.",
        "T1_PRIVATE_READ",
        {"type": "object", "properties": {}, "additionalProperties": False},
    ),
    "read_project_roadmap": CapabilitySpec(
        "read_project_roadmap",
        "Read the registered project roadmap with source lines and freshness. "
        "Do not infer omitted lines or implementation progress. This cannot start project work.",
        "T1_PRIVATE_READ",
        {"type": "object", "properties": {
            "project_id": {"type": "string", "minLength": 1, "maxLength": 32}},
         "required": ["project_id"], "additionalProperties": False},
    ),
    "get_agent_capabilities": CapabilitySpec(
        "get_agent_capabilities",
        "List the live Reachy Agent Broker capability manifest.",
        "T0_PUBLIC_READ",
        {"type": "object", "properties": {}, "additionalProperties": False},
    ),
    "get_reachy_status": CapabilitySpec(
        "get_reachy_status",
        "Read the current sanitized Reachy runtime status supplied by Reachy.",
        "T0_PUBLIC_READ",
        {"type": "object", "properties": {}, "additionalProperties": False},
    ),
    "get_home_status": CapabilitySpec(
        "get_home_status",
        "Read state and approved attributes for allowlisted Home Assistant entities.",
        "T1_PRIVATE_READ",
        {
            "type": "object",
            "properties": {"entity_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20}},
            "additionalProperties": False,
        },
    ),
    "search_current_information": CapabilitySpec(
        "search_current_information",
        "Search the configured current public-information index.",
        "T0_PUBLIC_READ",
        {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 300}},
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    "read_public_web_page": CapabilitySpec(
        "read_public_web_page",
        "Read bounded text from one public HTTP or HTTPS page without redirects.",
        "T0_PUBLIC_READ",
        {
            "type": "object",
            "properties": {"url": {"type": "string", "minLength": 1, "maxLength": 2048}},
            "required": ["url"],
            "additionalProperties": False,
        },
    ),
    "recall_personal_context": CapabilitySpec(
        "recall_personal_context",
        "Search explicitly configured personal-context files.",
        "T1_PRIVATE_READ",
        {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200}},
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    "search_conversation_history": CapabilitySpec(
        "search_conversation_history",
        "Search explicitly configured conversation-history roots.",
        "T1_PRIVATE_READ",
        {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200}},
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    "read_scoped_note": CapabilitySpec(
        "read_scoped_note",
        "Read one text note below an explicitly configured named root.",
        "T1_PRIVATE_READ",
        {
            "type": "object",
            "properties": {
                "root": {"type": "string", "minLength": 1, "maxLength": 32},
                "path": {"type": "string", "minLength": 1, "maxLength": 512},
            },
            "required": ["root", "path"],
            "additionalProperties": False,
        },
    ),
}

for _action_id, _action in action_specs().items():
    _SPECS[_action_id] = CapabilitySpec(
        capability_id=_action_id,
        description=str(_action["description"]),
        risk_tier=str(_action["risk_tier"]),
        arguments=_action["arguments_schema"],  # type: ignore[arg-type]
        read_only=_action_id.startswith("draft_") or _action_id == "list_calendar_events",
        reversible=bool(_action["reversible"]),
        requires_approval=approval_required(_action_id),
    )


@dataclass(frozen=True, slots=True)
class BrokerContext:
    capability_profile: str
    adult_ui_unlocked: bool
    kids_mode_active: bool
    power_mode: str
    privacy_enabled: bool
    emergency_stop_active: bool
    robot_available: bool
    session_generation: int
    requested_session_generation: int
    explicit_private_intent: bool
    reachy_status: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def parse(cls, payload: object) -> BrokerContext:
        if not isinstance(payload, dict):
            raise BrokerValidationError("context must be an object")
        allowed = {
            "capability_profile", "adult_ui_unlocked", "kids_mode_active", "power_mode",
            "privacy_enabled", "emergency_stop_active", "robot_available", "session_generation",
            "requested_session_generation", "explicit_private_intent", "reachy_status",
        }
        if set(payload) - allowed:
            raise BrokerValidationError("context contains unsupported fields")
        required = allowed - {"reachy_status"}
        if not required <= set(payload):
            raise BrokerValidationError("context is incomplete")
        boolean_fields = {
            "adult_ui_unlocked", "kids_mode_active", "privacy_enabled", "emergency_stop_active",
            "robot_available", "explicit_private_intent",
        }
        if any(type(payload[name]) is not bool for name in boolean_fields):
            raise BrokerValidationError("context boolean fields must be booleans")
        for name in ("session_generation", "requested_session_generation"):
            if type(payload[name]) is not int or int(payload[name]) < 0:
                raise BrokerValidationError("session generations must be non-negative integers")
        status = payload.get("reachy_status", {})
        if not isinstance(status, dict):
            raise BrokerValidationError("reachy_status must be an object")
        profile = payload["capability_profile"]
        power_mode = payload["power_mode"]
        if type(profile) is not str or profile not in {"conversation", "agent"}:
            raise BrokerValidationError("invalid capability_profile")
        if type(power_mode) is not str or power_mode not in {"standby", "awake", "meeting", "sleep"}:
            raise BrokerValidationError("invalid power_mode")
        return cls(
            capability_profile=profile,
            adult_ui_unlocked=payload["adult_ui_unlocked"],
            kids_mode_active=payload["kids_mode_active"],
            power_mode=power_mode,
            privacy_enabled=payload["privacy_enabled"],
            emergency_stop_active=payload["emergency_stop_active"],
            robot_available=payload["robot_available"],
            session_generation=payload["session_generation"],
            requested_session_generation=payload["requested_session_generation"],
            explicit_private_intent=payload["explicit_private_intent"],
            reachy_status=status,
        )


@dataclass(frozen=True, slots=True)
class BrokerRequest:
    request_id: str
    capability_id: str
    arguments: Mapping[str, object]
    context: BrokerContext
    approval_token: str = ""

    @classmethod
    def parse(cls, payload: object) -> BrokerRequest:
        required = {"request_id", "capability_id", "arguments", "context"}
        if (
            not isinstance(payload, dict)
            or not required <= set(payload)
            or set(payload) - (required | {"approval_token"})
        ):
            raise BrokerValidationError(
                "request must contain request_id, capability_id, arguments, context, and optional approval_token"
            )
        request_id = str(payload["request_id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
            raise BrokerValidationError("invalid request_id")
        capability_id = str(payload["capability_id"])
        if capability_id not in _SPECS:
            raise BrokerValidationError("unknown capability")
        arguments = payload["arguments"]
        if not isinstance(arguments, dict):
            raise BrokerValidationError("arguments must be an object")
        _validate_arguments(capability_id, arguments)
        approval_token = payload.get("approval_token", "")
        if not isinstance(approval_token, str) or len(approval_token) > 200:
            raise BrokerValidationError("invalid approval_token")
        return cls(
            request_id,
            capability_id,
            arguments,
            BrokerContext.parse(payload["context"]),
            approval_token,
        )


@dataclass(slots=True)
class BrokerConfig:
    home_entities: dict[str, frozenset[str]] = field(default_factory=dict)
    note_roots: dict[str, Path] = field(default_factory=dict)
    personal_roots: dict[str, Path] = field(default_factory=dict)
    history_roots: dict[str, Path] = field(default_factory=dict)
    hass_url: str = ""
    hass_token: str = ""
    search_url: str = "http://127.0.0.1:8888/search"
    timeout_seconds: float = 15.0
    projects: ProjectCatalog = field(default_factory=ProjectCatalog)

    @classmethod
    def from_env(cls) -> BrokerConfig:
        entity_config = _json_object(os.getenv("REACHY_AGENT_HA_ALLOWLIST", ""))
        entities = {
            str(entity): frozenset(str(attribute) for attribute in attributes)
            for entity, attributes in entity_config.items()
            if isinstance(attributes, list)
        }
        return cls(
            home_entities=entities,
            projects=ProjectCatalog.from_json(os.getenv("REACHY_AGENT_PROJECTS_JSON", "")),
            note_roots=_configured_roots("REACHY_AGENT_NOTE_ROOTS"),
            personal_roots=_configured_roots("REACHY_AGENT_PERSONAL_ROOTS"),
            history_roots=_configured_roots("REACHY_AGENT_HISTORY_ROOTS"),
            hass_url=os.getenv("HASS_URL", "").strip().rstrip("/"),
            hass_token=os.getenv("HASS_TOKEN", "").strip(),
            search_url=os.getenv("REACHY_AGENT_SEARCH_URL", "http://127.0.0.1:8888/search").strip(),
            timeout_seconds=max(1.0, min(float(os.getenv("REACHY_AGENT_TIMEOUT_SECONDS", "15")), 60.0)),
        )


@dataclass(slots=True)
class _DeviceLease:
    generation: int
    authorization_state: tuple[object, ...]
    tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    project_turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    project_dialogue: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=6))
    selected_project_id: str = ""
    project_updated_at: float = 0.0
    project_expiry: asyncio.TimerHandle | None = None


def _validate_arguments(capability_id: str, arguments: Mapping[str, object]) -> None:
    if capability_id in _ACTION_IDS:
        try:
            validate_action_arguments(capability_id, arguments)
        except ActionValidationError as exc:
            raise BrokerValidationError(str(exc)) from exc
        return
    allowed: dict[str, set[str]] = {
        "get_agent_capabilities": set(), "get_reachy_status": set(), "get_home_status": {"entity_ids"},
        "list_projects": set(),
        "search_current_information": {"query"}, "read_public_web_page": {"url"},
        "recall_personal_context": {"query"}, "search_conversation_history": {"query"},
        "read_scoped_note": {"root", "path"},
        "read_project_roadmap": {"project_id"},
    }
    required = {
        "search_current_information": {"query"}, "read_public_web_page": {"url"},
        "recall_personal_context": {"query"}, "search_conversation_history": {"query"},
        "read_scoped_note": {"root", "path"},
        "read_project_roadmap": {"project_id"},
    }.get(capability_id, set())
    if set(arguments) - allowed[capability_id] or not required <= set(arguments):
        raise BrokerValidationError("arguments do not match capability schema")
    for name in required:
        if not isinstance(arguments[name], str) or not str(arguments[name]).strip():
            raise BrokerValidationError(f"{name} must be a non-empty string")
    limits = {
        ("search_current_information", "query"): 300,
        ("read_public_web_page", "url"): 2_048,
        ("recall_personal_context", "query"): 200,
        ("search_conversation_history", "query"): 200,
        ("read_scoped_note", "root"): 32,
        ("read_scoped_note", "path"): 512,
        ("read_project_roadmap", "project_id"): 32,
    }
    for name in required:
        if len(str(arguments[name])) > limits[(capability_id, name)]:
            raise BrokerValidationError(f"{name} exceeds the capability limit")
    if capability_id == "get_home_status" and "entity_ids" in arguments:
        entities = arguments["entity_ids"]
        if (
            not isinstance(entities, list)
            or len(entities) > 20
            or any(not isinstance(item, str) or not item or len(item) > 255 for item in entities)
        ):
            raise BrokerValidationError("entity_ids must be an array of at most 20 strings")


def validate_capability_arguments(capability_id: str, arguments: Mapping[str, object]) -> None:
    """Validate one broker call without executing it (used by Agent 0.5 previews)."""
    _validate_arguments(capability_id, arguments)


def capability_requires_private_intent(capability_id: str) -> bool:
    """Return whether previewing/executing a capability needs explicit private intent."""
    return capability_id in _PRIVATE_CAPABILITIES


class ReachyAgentBroker:
    """Execute only the fixed owner surface and retain sanitized activity."""

    def __init__(self, config: BrokerConfig | None = None) -> None:
        self.config = config or BrokerConfig.from_env()
        self._activity: deque[dict[str, object]] = deque(maxlen=50)
        self._activity_lock = asyncio.Lock()
        self._leases: dict[str, _DeviceLease] = {}
        self._leases_lock = asyncio.Lock()
        self.actions = AgentActionService(
            ActionConfig.from_env(
                note_roots=self.config.note_roots,
                hass_url=self.config.hass_url,
                hass_token=self.config.hass_token,
            )
        )

    def manifest(self) -> list[dict[str, object]]:
        return [
            _SPECS[name].manifest() for name in _CAPABILITY_IDS
            if self.config.projects.projects or name not in {"list_projects", "read_project_roadmap"}
        ]

    @staticmethod
    def clear_project_conversation(lease: _DeviceLease) -> None:
        lease.project_dialogue.clear()
        lease.selected_project_id = ""
        if lease.project_expiry is not None:
            lease.project_expiry.cancel()
            lease.project_expiry = None

    def refresh_project_conversation(self, lease: _DeviceLease) -> None:
        lease.project_updated_at = time.monotonic()
        if lease.project_expiry is not None:
            lease.project_expiry.cancel()
        lease.project_expiry = asyncio.get_running_loop().call_later(
            600, self.clear_project_conversation, lease
        )

    @asynccontextmanager
    async def project_conversation(
        self, device_id: str, context: BrokerContext
    ) -> AsyncIterator[_DeviceLease]:
        """Serialize bounded project dialogue inside an already-authorized live lease."""
        self.authorize_context(context)
        async with self._leases_lock:
            lease = self._leases.get(device_id)
            if lease is None or lease.generation != context.session_generation:
                raise BrokerValidationError("stale_session")
        async with lease.project_turn_lock:
            async with self._leases_lock:
                if (self._leases.get(device_id) is not lease
                        or lease.authorization_state != self._authorization_state(context)):
                    raise BrokerValidationError("stale_session")
            if time.monotonic() - lease.project_updated_at > 600:
                self.clear_project_conversation(lease)
            yield lease

    async def recent_activity(
        self, device_id: str = "test-device", generation: int | None = None, limit: int = 20
    ) -> list[dict[str, object]]:
        if generation is None:
            async with self._leases_lock:
                lease = self._leases.get(device_id)
                if lease is None:
                    return []
                generation = lease.generation
        await self.assert_current(device_id, generation)
        async with self._activity_lock:
            scoped = [
                item
                for item in self._activity
                if item.get("device_id") == device_id and item.get("session_generation") == generation
            ]
            return [self._public_activity(item) for item in scoped[-max(0, min(limit, 50)):]]

    async def execute(
        self, payload: object, http: ClientSession, *, device_id: str = "test-device"
    ) -> dict[str, object]:
        request = BrokerRequest.parse(payload)
        await self.register_request(device_id, request.context, request.request_id)
        completed = False
        try:
            self._authorize(request)
            await self._record(device_id, request, "started", "running")
            async with asyncio.timeout(self.config.timeout_seconds):
                data, evidence, observed_at, side_effect = await self._dispatch(
                    request, http, device_id=device_id
                )
            completed = time.time()
            result = {
                "ok": True,
                "request_id": request.request_id,
                "capability_id": request.capability_id,
                "data": redact_payload(data),
                "evidence": redact_payload(evidence),
                "freshness": {
                    "observed_at": round(observed_at, 3),
                    "completed_at": round(completed, 3),
                    "age_seconds": max(0.0, round(completed - observed_at, 3)),
                },
                "read_only": not side_effect,
                "side_effect": side_effect,
            }
            encoded = json.dumps(result, ensure_ascii=True)
            if len(encoded.encode("utf-8")) > 256_000:
                raise BrokerUnavailableError("broker result exceeded the safe response limit")
            # Atomically validate the lease, record success, and unregister the
            # task. The success path performs no later await before handoff.
            await self._complete_request_if_current(device_id, request)
            completed = True
            return result
        except asyncio.CancelledError:
            await self._record(device_id, request, "cancelled", "cancelled")
            raise
        except Exception as exc:
            await self._record(device_id, request, "failed", type(exc).__name__)
            raise
        finally:
            if not completed:
                await self.unregister_request(
                    device_id, request.context.session_generation, request.request_id
                )

    @staticmethod
    def _authorization_state(context: BrokerContext) -> tuple[object, ...]:
        return (
            context.capability_profile,
            context.adult_ui_unlocked,
            context.kids_mode_active,
            context.power_mode,
            context.privacy_enabled,
            context.emergency_stop_active,
            context.robot_available,
        )

    async def register_request(
        self,
        device_id: str,
        context: BrokerContext | Mapping[str, object],
        request_id: str,
        task: asyncio.Task[Any] | None = None,
    ) -> BrokerContext:
        """Bind work only to a lease established by the authenticated runtime."""
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,96}", device_id):
            raise BrokerValidationError("invalid device identity")
        parsed = context if isinstance(context, BrokerContext) else BrokerContext.parse(context)
        if parsed.requested_session_generation != parsed.session_generation:
            raise BrokerValidationError("stale_session")
        current_task = task or asyncio.current_task()
        if current_task is None:
            raise BrokerValidationError("request task is unavailable")
        state = self._authorization_state(parsed)
        async with self._leases_lock:
            lease = self._leases.get(device_id)
            if lease is None or parsed.session_generation != lease.generation:
                raise BrokerValidationError("stale_session")
            if state != lease.authorization_state:
                raise BrokerValidationError("session_context_changed_without_generation")
            if request_id in lease.tasks and lease.tasks[request_id] is not current_task:
                raise BrokerValidationError("request_id is already active")
            lease.tasks[request_id] = current_task
        return parsed

    async def establish_session(
        self, device_id: str, context: BrokerContext | Mapping[str, object]
    ) -> BrokerContext:
        """Install authoritative live state and cancel superseded device work."""
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,96}", device_id):
            raise BrokerValidationError("invalid device identity")
        parsed = context if isinstance(context, BrokerContext) else BrokerContext.parse(context)
        if parsed.requested_session_generation != parsed.session_generation:
            raise BrokerValidationError("stale_session")
        state = self._authorization_state(parsed)
        stale_tasks: list[asyncio.Task[Any]] = []
        clear_activity = False
        async with self._leases_lock:
            lease = self._leases.get(device_id)
            if lease is not None and parsed.session_generation < lease.generation:
                raise BrokerValidationError("stale_session")
            if lease is None or parsed.session_generation > lease.generation or state != lease.authorization_state:
                if lease is not None:
                    self.clear_project_conversation(lease)
                    stale_tasks = list(lease.tasks.values())
                clear_activity = lease is None or parsed.session_generation != lease.generation
                self._leases[device_id] = _DeviceLease(parsed.session_generation, state)
        if clear_activity:
            async with self._activity_lock:
                self._activity = deque(
                    (item for item in self._activity if item.get("device_id") != device_id),
                    maxlen=50,
                )
        for stale in stale_tasks:
            stale.cancel()
        return parsed

    async def unregister_request(self, device_id: str, generation: int, request_id: str) -> None:
        async with self._leases_lock:
            lease = self._leases.get(device_id)
            if lease is not None and lease.generation == generation:
                lease.tasks.pop(request_id, None)

    async def assert_current(self, device_id: str, generation: int) -> None:
        async with self._leases_lock:
            lease = self._leases.get(device_id)
            if lease is None or lease.generation != generation:
                raise BrokerValidationError("stale_session")

    async def cancel(self, device_id: str, request_id: str) -> bool:
        async with self._leases_lock:
            lease = self._leases.get(device_id)
            task = lease.tasks.get(request_id) if lease is not None else None
        if task is None:
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    def _authorize(self, request: BrokerRequest) -> None:
        context = request.context
        self.authorize_context(context)
        if request.capability_id in _PRIVATE_CAPABILITIES and not context.explicit_private_intent:
            raise BrokerValidationError("explicit_private_intent_required")

    async def issue_approval(
        self,
        device_id: str,
        context: BrokerContext | Mapping[str, object],
        capability_id: str,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        """Issue one exact short-lived phone approval for the current lease."""
        parsed = context if isinstance(context, BrokerContext) else BrokerContext.parse(context)
        await self.assert_current(device_id, parsed.session_generation)
        self.authorize_context(parsed)
        try:
            return await self.actions.issue_approval(
                device_id,
                parsed.session_generation,
                capability_id,
                arguments,
            )
        except ActionValidationError as exc:
            raise BrokerValidationError(str(exc)) from exc

    async def pending_approval(
        self,
        device_id: str,
        context: BrokerContext | Mapping[str, object],
    ) -> dict[str, object] | None:
        parsed = context if isinstance(context, BrokerContext) else BrokerContext.parse(context)
        await self.assert_current(device_id, parsed.session_generation)
        self.authorize_context(parsed)
        return await self.actions.pending(device_id, parsed.session_generation)

    async def approve_pending(
        self,
        device_id: str,
        context: BrokerContext | Mapping[str, object],
        draft_id: str,
        http: ClientSession,
    ) -> dict[str, object]:
        parsed = context if isinstance(context, BrokerContext) else BrokerContext.parse(context)
        await self.assert_current(device_id, parsed.session_generation)
        self.authorize_context(parsed)
        try:
            data, evidence, observed_at, side_effect = await self.actions.approve_pending(
                device_id,
                parsed.session_generation,
                draft_id,
                http,
            )
        except ActionValidationError as exc:
            raise BrokerValidationError(str(exc)) from exc
        except ActionUnavailableError as exc:
            raise BrokerUnavailableError(str(exc)) from exc
        completed = time.time()
        return {
            "ok": True,
            "draft_id": draft_id,
            "data": redact_payload(data),
            "evidence": redact_payload(evidence),
            "freshness": {
                "observed_at": round(observed_at, 3),
                "completed_at": round(completed, 3),
                "age_seconds": max(0.0, round(completed - observed_at, 3)),
            },
            "read_only": not side_effect,
            "side_effect": side_effect,
        }

    def authorize_context(self, context: BrokerContext) -> None:
        """Authorize current live state without granting any private data class."""
        if context.requested_session_generation != context.session_generation:
            raise BrokerValidationError("stale_session")
        if context.capability_profile != "agent":
            raise BrokerValidationError("agent_profile_inactive")
        if not context.adult_ui_unlocked or context.kids_mode_active:
            raise BrokerValidationError("adult_ui_required")
        if context.power_mode in {"meeting", "sleep"}:
            raise BrokerValidationError("power_mode_blocked")
        if not context.privacy_enabled or context.emergency_stop_active or not context.robot_available:
            raise BrokerValidationError("agent_context_blocked")

    async def _record(
        self, device_id: str, request: BrokerRequest, event: str, result_class: str
    ) -> None:
        item = {
            "timestamp": round(time.time(), 3),
            "device_id": device_id,
            "session_generation": request.context.session_generation,
            "request_id": request.request_id[:80],
            "capability_id": request.capability_id,
            "event": event,
            "result_class": result_class,
        }
        async with self._activity_lock:
            self._activity.append(item)

    async def _complete_request_if_current(self, device_id: str, request: BrokerRequest) -> None:
        item = {
            "timestamp": round(time.time(), 3),
            "device_id": device_id,
            "session_generation": request.context.session_generation,
            "request_id": request.request_id[:80],
            "capability_id": request.capability_id,
            "event": "completed",
            "result_class": "success",
        }
        async with self._leases_lock:
            lease = self._leases.get(device_id)
            if lease is None or lease.generation != request.context.session_generation:
                raise BrokerValidationError("stale_session")
            async with self._activity_lock:
                self._activity.append(item)
            if request.capability_id == "read_project_roadmap":
                project_id = str(request.arguments["project_id"])
                if lease.selected_project_id != project_id:
                    lease.project_dialogue.clear()
                lease.selected_project_id = project_id
                self.refresh_project_conversation(lease)
            lease.tasks.pop(request.request_id, None)

    @staticmethod
    def _public_activity(item: Mapping[str, object]) -> dict[str, object]:
        return {
            key: item[key]
            for key in ("timestamp", "request_id", "capability_id", "event", "result_class")
            if key in item
        }

    async def _dispatch(
        self, request: BrokerRequest, http: ClientSession, *, device_id: str
    ) -> tuple[object, list[dict[str, object]], float, bool]:
        capability = request.capability_id
        if capability in _ACTION_IDS:
            try:
                return await self.actions.execute(
                    capability,
                    request.arguments,
                    http,
                    device_id=device_id,
                    generation=request.context.session_generation,
                    approval_token=request.approval_token,
                )
            except ActionValidationError as exc:
                raise BrokerValidationError(str(exc)) from exc
            except ActionUnavailableError as exc:
                raise BrokerUnavailableError(str(exc)) from exc
        if capability == "get_agent_capabilities":
            now = time.time()
            return (
                {"capabilities": self.manifest()},
                [{"source": "broker_manifest", "observed_at": now}],
                now,
                False,
            )
        if capability == "get_reachy_status":
            data, evidence, observed = self._reachy_status(request.context.reachy_status)
            return data, evidence, observed, False
        if capability == "get_home_status":
            data, evidence, observed = await self._home_status(request.arguments, http)
            return data, evidence, observed, False
        if capability == "search_current_information":
            data, evidence, observed = await self._search(request.arguments, http)
            return data, evidence, observed, False
        if capability == "read_public_web_page":
            data, evidence, observed = await self._web_page(request.arguments, http)
            return data, evidence, observed, False
        if capability == "recall_personal_context":
            data, evidence, observed = await asyncio.to_thread(
                self._search_roots, self.config.personal_roots, request.arguments
            )
            return data, evidence, observed, False
        if capability == "search_conversation_history":
            data, evidence, observed = await asyncio.to_thread(
                self._search_roots, self.config.history_roots, request.arguments
            )
            return data, evidence, observed, False
        if capability in {"list_projects", "read_project_roadmap"}:
            if not self.config.projects.projects:
                raise BrokerUnavailableError("project roadmaps are not configured")
            observed = time.time()
            if capability == "list_projects":
                return ({"projects": self.config.projects.public_catalog()},
                        [{"source": "project_catalog", "observed_at": observed}], observed, False)
            try:
                data = await asyncio.to_thread(
                    self.config.projects.snapshot, str(request.arguments["project_id"]), _read_scoped_file
                )
            except (ValueError, UnicodeError) as exc:
                raise BrokerValidationError("registered project roadmap is unavailable or invalid") from exc
            observed = time.time()
            return (data, [{"source": "project_roadmap", "project_id": data["project_id"],
                           "path": data["roadmap"], "source_sha256": data["source_sha256"],
                           "source_ranges": data["source_ranges"], "observed_at": observed}], observed, False)
        if capability == "read_scoped_note":
            data, evidence, observed = await asyncio.to_thread(self._read_note, request.arguments)
            return data, evidence, observed, False
        raise BrokerValidationError("unknown capability")

    def _reachy_status(
        self, status: Mapping[str, object]
    ) -> tuple[object, list[dict[str, object]], float]:
        allowed = {
            "state", "detail", "power_mode", "motors_enabled", "head_safely_folded", "bridge_healthy",
            "robot_action_busy", "camera_enabled", "camera_captures", "last_error",
        }
        sanitized = {
            key: _redact(status[key], limit=500) if isinstance(status[key], str) else status[key]
            for key in allowed
            if key in status and isinstance(status[key], (str, int, float, bool, type(None)))
        }
        captured_at = status.get("observed_at")
        observed = (
            float(captured_at)
            if isinstance(captured_at, (int, float)) and 0 < float(captured_at) <= time.time()
            else time.time()
        )
        return sanitized, [{"source": "reachy_runtime", "observed_at": observed}], observed

    async def _home_status(
        self, arguments: Mapping[str, object], http: ClientSession
    ) -> tuple[object, list[dict[str, object]], float]:
        if not self.config.hass_url or not self.config.hass_token:
            raise BrokerUnavailableError("Home Assistant read access is not configured")
        requested = arguments.get("entity_ids")
        entities = list(self.config.home_entities) if requested is None else list(requested)  # type: ignore[arg-type]
        if any(entity not in self.config.home_entities for entity in entities):
            raise BrokerValidationError("Home Assistant entity is not allowlisted")
        states: list[dict[str, object]] = []
        evidence: list[dict[str, object]] = []
        observed = time.time()
        headers = {"Authorization": f"Bearer {self.config.hass_token}"}
        for entity in entities:
            async with http.get(
                f"{self.config.hass_url}/api/states/{entity}",
                headers=headers,
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise BrokerUnavailableError(f"Home Assistant state unavailable for {entity}")
                payload = await response.json(content_type=None)
            if not isinstance(payload, dict) or not isinstance(payload.get("attributes", {}), dict):
                raise BrokerUnavailableError("Home Assistant returned invalid state data")
            attributes = {
                key: _redact(payload["attributes"][key], limit=500)
                if isinstance(payload["attributes"][key], str)
                else payload["attributes"][key]
                for key in self.config.home_entities[entity]
                if key in payload["attributes"]
                and isinstance(payload["attributes"][key], (str, int, float, bool, type(None)))
            }
            states.append(
                {
                    "entity_id": entity,
                    "state": _redact(payload.get("state", ""), limit=200),
                    "attributes": attributes,
                }
            )
            evidence.append({"source": "home_assistant", "entity_id": entity, "observed_at": observed})
        return {"entities": states}, evidence, observed

    async def _search(
        self, arguments: Mapping[str, object], http: ClientSession
    ) -> tuple[object, list[dict[str, object]], float]:
        query = str(arguments["query"]).strip()[:300]
        async with http.get(
            self.config.search_url,
            params={"q": query, "format": "json", "language": "auto", "safesearch": "1"},
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise BrokerUnavailableError("current-information search is unavailable")
            payload = await response.json(content_type=None)
        raw_results = payload.get("results", []) if isinstance(payload, dict) else []
        results = []
        evidence = []
        observed = time.time()
        for item in raw_results[:5] if isinstance(raw_results, list) else []:
            if not isinstance(item, dict):
                continue
            result = {
                "title": _redact(item.get("title", ""), limit=300),
                "url": _public_evidence_url(str(item.get("url", ""))),
                "snippet": _redact(item.get("content", ""), limit=1_000),
            }
            results.append(result)
            evidence.append({"source": "public_search", "url": result["url"], "observed_at": observed})
        return {"query": query, "results": results}, evidence, observed

    async def _web_page(
        self, arguments: Mapping[str, object], _http: ClientSession
    ) -> tuple[object, list[dict[str, object]], float]:
        url = str(arguments["url"]).strip()
        addresses = await _require_public_url(url)
        parsed = urlsplit(url)
        connector = TCPConnector(resolver=_PinnedResolver(parsed.hostname or "", addresses))
        timeout = ClientTimeout(total=self.config.timeout_seconds)
        async with ClientSession(connector=connector, timeout=timeout) as pinned_http:
            async with pinned_http.get(
                url,
                allow_redirects=False,
                headers={
                    "Accept": "text/html,text/plain,application/json",
                    "User-Agent": "ReachyAgentBroker/0.1",
                },
            ) as response:
                if response.status != 200:
                    raise BrokerUnavailableError(f"public page returned HTTP {response.status}")
                if response.content_length is not None and response.content_length > _MAX_FILE_BYTES:
                    raise BrokerUnavailableError("public page exceeds the safe size limit")
                raw = await response.content.read(_MAX_FILE_BYTES + 1)
                if len(raw) > _MAX_FILE_BYTES:
                    raise BrokerUnavailableError("public page exceeds the safe size limit")
                content_type = response.headers.get("content-type", "").lower()
                if not any(kind in content_type for kind in ("text/", "json", "xml")):
                    raise BrokerUnavailableError("public page is not a supported text resource")
                charset = response.charset or "utf-8"
        text = raw.decode(charset, errors="replace")
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = _redact(" ".join(text.split()))
        observed = time.time()
        evidence_url = _public_evidence_url(url)
        return (
            {"url": evidence_url, "text": text},
            [{"source": "public_web", "url": evidence_url, "observed_at": observed}],
            observed,
        )

    def _search_roots(
        self, roots: Mapping[str, Path], arguments: Mapping[str, object]
    ) -> tuple[object, list[dict[str, object]], float]:
        if not roots:
            raise BrokerUnavailableError("this private read source is not configured")
        query = str(arguments["query"]).strip().casefold()
        matches: list[dict[str, object]] = []
        observed = time.time()
        for alias, root in roots.items():
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if len(matches) >= 20:
                    break
                try:
                    relative = path.relative_to(root)
                    if relative.suffix.lower() not in _TEXT_SUFFIXES:
                        continue
                    raw = _read_scoped_file(root, relative)
                except (BrokerValidationError, ValueError):
                    continue
                for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                    if query in line.casefold():
                        matches.append(
                            {
                                "root": alias,
                                "path": relative.as_posix(),
                                "line": number,
                                "excerpt": _redact(line, limit=500),
                            }
                        )
                        if len(matches) >= 20:
                            break
        return (
            {"query": _redact(arguments["query"], limit=200), "matches": matches},
            [{"source": "scoped_private_index", "observed_at": observed}],
            observed,
        )

    def _read_note(
        self, arguments: Mapping[str, object]
    ) -> tuple[object, list[dict[str, object]], float]:
        alias = str(arguments["root"])
        root = self.config.note_roots.get(alias)
        if root is None:
            raise BrokerValidationError("note root is not allowlisted")
        relative_path = Path(str(arguments["path"]))
        if relative_path.suffix.lower() not in _TEXT_SUFFIXES:
            raise BrokerValidationError("note type is not allowed")
        text = _redact(_read_scoped_file(root, relative_path).decode("utf-8", errors="replace"))
        observed = time.time()
        relative = relative_path.as_posix()
        return (
            {"root": alias, "path": relative, "text": text},
            [{"source": "scoped_note", "root": alias, "path": relative, "observed_at": observed}],
            observed,
        )


def _read_scoped_file(root: Path, relative: Path) -> bytes:
    """Read from the same no-follow descriptor that was validated (no TOCTOU gap)."""
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise BrokerValidationError("invalid scoped path")
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(root, directory_flags))
        for component in relative.parts[:-1]:
            descriptors.append(os.open(component, directory_flags, dir_fd=descriptors[-1]))
        file_descriptor = os.open(relative.parts[-1], file_flags, dir_fd=descriptors[-1])
        descriptors.append(file_descriptor)
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BrokerValidationError("scoped path is not a regular file")
        if metadata.st_size > _MAX_FILE_BYTES:
            raise BrokerValidationError("scoped file exceeds the safe size limit")
        chunks: list[bytes] = []
        remaining = _MAX_FILE_BYTES + 1
        while remaining:
            chunk = os.read(file_descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_FILE_BYTES:
            raise BrokerValidationError("scoped file exceeds the safe size limit")
        return raw
    except OSError as exc:
        raise BrokerValidationError("scoped path is unavailable or contains a symlink") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


class _PinnedResolver(AbstractResolver):
    """Pin preflight DNS answers so the subsequent request cannot be rebound."""

    def __init__(self, hostname: str, addresses: tuple[tuple[str, int], ...]) -> None:
        self.hostname = hostname
        self.addresses = addresses

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> list[ResolveResult]:
        if host != self.hostname:
            raise OSError("unexpected broker hostname")
        return [
            ResolveResult(
                hostname=host,
                host=address,
                port=port,
                family=address_family,
                proto=0,
                flags=0,
            )
            for address, address_family in self.addresses
        ]

    async def close(self) -> None:
        return None


async def _require_public_url(url: str) -> tuple[tuple[str, int], ...]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise BrokerValidationError("URL must be public HTTP(S) without credentials")
    if parsed.port not in {None, 80, 443}:
        raise BrokerValidationError("URL port is not allowed")
    sensitive_name = re.compile(r"(?i)(token|key|secret|password|auth|session|signature)")
    if any(sensitive_name.search(name) for name, _ in parse_qsl(parsed.query)):
        raise BrokerValidationError("URL contains a sensitive query parameter")
    try:
        addresses = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            ),
        )
    except OSError as exc:
        raise BrokerUnavailableError("public page host could not be resolved") from exc
    if not addresses:
        raise BrokerUnavailableError("public page host could not be resolved")
    validated: list[tuple[str, int]] = []
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise BrokerValidationError("URL resolves to a non-public address")
        validated.append((str(ip), address[0]))
    return tuple(validated)


def _public_evidence_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))[:2048]


def new_request_id() -> str:
    return f"agent-{uuid.uuid4().hex}"
