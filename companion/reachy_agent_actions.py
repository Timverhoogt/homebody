"""Bounded owner actions for Reachy Agent Mode.

The broker remains the authority. This module contains only fixed schemas,
empty-by-default allowlists, exact one-shot approvals, and reversible local state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import time
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_MAX_TEXT = 2_000
_ACTION_IDS = (
    "control_home_entity",
    "set_timer",
    "cancel_timer",
    "create_reminder",
    "cancel_reminder",
    "play_media",
    "pause_media",
    "set_media_volume",
    "undo_last_reversible_action",
    "list_calendar_events",
    "draft_calendar_event",
    "create_calendar_event",
    "draft_message",
    "send_approved_message",
    "draft_note",
    "append_scoped_note",
)
_APPROVAL_REQUIRED = frozenset(
    {
        "play_media",
        "pause_media",
        "set_media_volume",
        "create_calendar_event",
        "send_approved_message",
        "append_scoped_note",
    }
)
_PRIVATE_ACTIONS = frozenset(
    {
        "list_calendar_events",
        "draft_calendar_event",
        "create_calendar_event",
        "draft_message",
        "send_approved_message",
        "draft_note",
        "append_scoped_note",
    }
)


_LOGGER = logging.getLogger(__name__)
_HA_ENTITY_ID_RE = re.compile(r"[a-z_]+\.[a-z0-9_]+")
_MAX_SCHEDULED_ITEMS = 32


class ActionValidationError(ValueError):
    pass


class ActionUnavailableError(RuntimeError):
    pass


def _schema(properties: dict[str, object], *, required: tuple[str, ...] = ()) -> dict[str, object]:
    result: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        result["required"] = list(required)
    return result


def action_specs() -> dict[str, dict[str, object]]:
    text = {"type": "string", "minLength": 1, "maxLength": _MAX_TEXT}
    identifier = {"type": "string", "minLength": 1, "maxLength": 96}
    return {
        "control_home_entity": {
            "description": "Control one allowlisted light, switch, or scene and verify Home Assistant state.",
            "risk_tier": "T2_BOUNDED_LOCAL_ACTION",
            "arguments_schema": _schema(
                {"entity_id": identifier, "action": {"type": "string", "enum": ["turn_on", "turn_off", "toggle"]}},
                required=("entity_id", "action"),
            ),
            "reversible": True,
        },
        "set_timer": {
            "description": "Set one in-memory timer for up to 24 hours.",
            "risk_tier": "T2_BOUNDED_LOCAL_ACTION",
            "arguments_schema": _schema(
                {"seconds": {"type": "integer", "minimum": 1, "maximum": 86_400}, "label": text},
                required=("seconds",),
            ),
            "reversible": True,
        },
        "cancel_timer": {
            "description": "Cancel one timer created in this Agent session.",
            "risk_tier": "T2_BOUNDED_LOCAL_ACTION",
            "arguments_schema": _schema({"timer_id": identifier}, required=("timer_id",)),
            "reversible": False,
        },
        "create_reminder": {
            "description": "Create one local reminder for up to 30 days from now.",
            "risk_tier": "T2_BOUNDED_LOCAL_ACTION",
            "arguments_schema": _schema(
                {"seconds": {"type": "integer", "minimum": 1, "maximum": 2_592_000}, "text": text},
                required=("seconds", "text"),
            ),
            "reversible": True,
        },
        "cancel_reminder": {
            "description": "Cancel one reminder created in this Agent session.",
            "risk_tier": "T2_BOUNDED_LOCAL_ACTION",
            "arguments_schema": _schema({"reminder_id": identifier}, required=("reminder_id",)),
            "reversible": False,
        },
        "play_media": {
            "description": (
                "Play one explicit media URI on an allowlisted Home Assistant media player after phone approval."
            ),
            "risk_tier": "T2_BOUNDED_LOCAL_ACTION",
            "arguments_schema": _schema(
                {"entity_id": identifier, "media_uri": text, "media_type": identifier},
                required=("entity_id", "media_uri", "media_type"),
            ),
            "reversible": True,
        },
        "pause_media": {
            "description": "Pause one allowlisted Home Assistant media player after phone approval.",
            "risk_tier": "T2_BOUNDED_LOCAL_ACTION",
            "arguments_schema": _schema({"entity_id": identifier}, required=("entity_id",)),
            "reversible": True,
        },
        "set_media_volume": {
            "description": "Set an allowlisted media player volume between 0 and 0.8 after phone approval.",
            "risk_tier": "T2_BOUNDED_LOCAL_ACTION",
            "arguments_schema": _schema(
                {"entity_id": identifier, "volume": {"type": "number", "minimum": 0, "maximum": 0.8}},
                required=("entity_id", "volume"),
            ),
            "reversible": True,
        },
        "undo_last_reversible_action": {
            "description": "Undo the most recent reversible action from this device and session.",
            "risk_tier": "T2_BOUNDED_LOCAL_ACTION",
            "arguments_schema": _schema({}),
            "reversible": False,
        },
        "list_calendar_events": {
            "description": "List events from one allowlisted Home Assistant calendar in a bounded time window.",
            "risk_tier": "T1_PRIVATE_READ",
            "arguments_schema": _schema(
                {"entity_id": identifier, "start": identifier, "end": identifier},
                required=("entity_id", "start", "end"),
            ),
            "reversible": False,
        },
        "draft_calendar_event": {
            "description": "Prepare an exact calendar event draft without creating it.",
            "risk_tier": "T1_PRIVATE_READ",
            "arguments_schema": _schema(
                {
                    "entity_id": identifier,
                    "summary": text,
                    "start": identifier,
                    "end": identifier,
                    "description": text,
                },
                required=("entity_id", "summary", "start", "end"),
            ),
            "reversible": False,
        },
        "create_calendar_event": {
            "description": "Create one exact drafted event on an allowlisted calendar after phone approval.",
            "risk_tier": "T3_EXTERNAL_SIDE_EFFECT",
            "arguments_schema": _schema(
                {
                    "entity_id": identifier,
                    "summary": text,
                    "start": identifier,
                    "end": identifier,
                    "description": text,
                },
                required=("entity_id", "summary", "start", "end"),
            ),
            "reversible": False,
        },
        "draft_message": {
            "description": "Prepare an exact single-recipient message without sending it.",
            "risk_tier": "T1_PRIVATE_READ",
            "arguments_schema": _schema(
                {"channel": identifier, "recipient": identifier, "text": text},
                required=("channel", "recipient", "text"),
            ),
            "reversible": False,
        },
        "send_approved_message": {
            "description": (
                "Send one exact single-recipient draft through an allowlisted Home Assistant notify service after "
                "phone approval."
            ),
            "risk_tier": "T3_EXTERNAL_SIDE_EFFECT",
            "arguments_schema": _schema(
                {"channel": identifier, "recipient": identifier, "text": text},
                required=("channel", "recipient", "text"),
            ),
            "reversible": False,
        },
        "draft_note": {
            "description": "Prepare exact text to append to one allowlisted note without writing it.",
            "risk_tier": "T1_PRIVATE_READ",
            "arguments_schema": _schema(
                {"root": identifier, "path": identifier, "text": text},
                required=("root", "path", "text"),
            ),
            "reversible": False,
        },
        "append_scoped_note": {
            "description": "Append one exact draft to an allowlisted text note after phone approval.",
            "risk_tier": "T3_EXTERNAL_SIDE_EFFECT",
            "arguments_schema": _schema(
                {"root": identifier, "path": identifier, "text": text},
                required=("root", "path", "text"),
            ),
            "reversible": False,
        },
    }


def approval_required(capability_id: str) -> bool:
    return capability_id in _APPROVAL_REQUIRED


def private_action(capability_id: str) -> bool:
    return capability_id in _PRIVATE_ACTIONS


def canonical_action(capability_id: str, arguments: Mapping[str, object]) -> bytes:
    return json.dumps(
        {"capability_id": capability_id, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def validate_action_arguments(capability_id: str, arguments: Mapping[str, object]) -> None:
    spec = action_specs().get(capability_id)
    if spec is None:
        raise ActionValidationError("unknown action capability")
    schema = spec["arguments_schema"]
    assert isinstance(schema, dict)
    properties = schema["properties"]
    required = set(schema.get("required", []))
    assert isinstance(properties, dict)
    if set(arguments) - set(properties) or not required <= set(arguments):
        raise ActionValidationError("arguments do not match capability schema")
    for name, value in arguments.items():
        field_schema = properties[name]
        assert isinstance(field_schema, dict)
        expected = field_schema.get("type")
        if expected == "string":
            if not isinstance(value, str) or not value.strip():
                raise ActionValidationError(f"{name} must be a non-empty string")
            if len(value) > int(field_schema.get("maxLength", _MAX_TEXT)):
                raise ActionValidationError(f"{name} exceeds the capability limit")
        elif expected == "integer":
            if type(value) is not int:
                raise ActionValidationError(f"{name} must be an integer")
        elif expected == "number":
            if type(value) not in {int, float}:
                raise ActionValidationError(f"{name} must be a number")
        if "enum" in field_schema and value not in field_schema["enum"]:
            raise ActionValidationError(f"{name} is not allowed")
        if isinstance(value, (int, float)):
            if value < field_schema.get("minimum", value) or value > field_schema.get("maximum", value):
                raise ActionValidationError(f"{name} is outside the safe range")
    if capability_id == "send_approved_message" and re.search(r"[,;\n]", str(arguments.get("recipient", ""))):
        raise ActionValidationError("group or multi-recipient messages are not supported")


@dataclass(slots=True)
class ActionConfig:
    hass_url: str = ""
    hass_token: str = ""
    home_actions: dict[str, frozenset[str]] = field(default_factory=dict)
    media_entities: frozenset[str] = frozenset()
    calendar_entities: frozenset[str] = frozenset()
    notify_targets: dict[str, frozenset[str]] = field(default_factory=dict)
    note_roots: dict[str, Path] = field(default_factory=dict)
    reminder_callback_url: str = ""
    reminder_callback_token: str = ""

    @classmethod
    def from_env(cls, *, note_roots: dict[str, Path], hass_url: str, hass_token: str) -> ActionConfig:
        def obj(name: str) -> dict[str, Any]:
            raw = os.getenv(name, "").strip()
            if not raw:
                return {}
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError(f"{name} must be a JSON object")
            return parsed

        home = obj("REACHY_AGENT_HA_ACTION_ALLOWLIST")
        notify = obj("REACHY_AGENT_NOTIFY_ALLOWLIST")
        media = json.loads(os.getenv("REACHY_AGENT_MEDIA_ALLOWLIST", "[]"))
        calendars = json.loads(os.getenv("REACHY_AGENT_CALENDAR_ALLOWLIST", "[]"))
        if not isinstance(media, list) or not isinstance(calendars, list):
            raise ValueError("Agent media/calendar allowlists must be JSON arrays")
        return cls(
            hass_url=hass_url,
            hass_token=hass_token,
            home_actions={
                str(key): frozenset(map(str, value)) for key, value in home.items() if isinstance(value, list)
            },
            media_entities=frozenset(map(str, media)),
            calendar_entities=frozenset(map(str, calendars)),
            notify_targets={
                str(key): frozenset(map(str, value)) for key, value in notify.items() if isinstance(value, list)
            },
            note_roots=note_roots,
            reminder_callback_url=os.getenv("REACHY_AGENT_REMINDER_CALLBACK_URL", "").strip().rstrip("/"),
            reminder_callback_token=os.getenv("REACHY_AGENT_REMINDER_CALLBACK_TOKEN", "").strip(),
        )


@dataclass(frozen=True, slots=True)
class _Approval:
    digest: str
    device_id: str
    generation: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class _Undo:
    capability_id: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class _Pending:
    draft_id: str
    capability_id: str
    arguments: dict[str, object]
    expires_at: float


class AgentActionService:
    def __init__(self, config: ActionConfig) -> None:
        self.config = config
        self._approvals: dict[str, _Approval] = {}
        self._timers: dict[tuple[str, int], dict[str, dict[str, object]]] = defaultdict(dict)
        self._reminders: dict[tuple[str, int], dict[str, dict[str, object]]] = defaultdict(dict)
        self._undo: dict[tuple[str, int], deque[_Undo]] = defaultdict(lambda: deque(maxlen=20))
        self._pending: dict[tuple[str, int], _Pending] = {}
        self._scheduled_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    def _require_reminder_callback(self) -> None:
        parsed = urlsplit(self.config.reminder_callback_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not self.config.reminder_callback_token
        ):
            raise ActionUnavailableError("timer and reminder delivery is not configured")

    async def _deliver_later(
        self,
        item_id: str,
        delay_seconds: int,
        text: str,
        http: Any,
    ) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            async with http.post(
                f"{self.config.reminder_callback_url}/api/agent/reminder-delivery",
                headers={"Authorization": f"Bearer {self.config.reminder_callback_token}"},
                json={"item_id": item_id, "text": text},
                allow_redirects=False,
            ) as response:
                await response.read()
                if response.status != 200:
                    raise ActionUnavailableError("timer or reminder delivery failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Nothing awaits these background tasks; log instead of "exception was never retrieved".
            _LOGGER.warning("Timer or reminder %s could not be delivered: %s", item_id, exc)
        finally:
            async with self._lock:
                self._scheduled_tasks.pop(item_id, None)

    def _require_schedule_capacity(self) -> None:
        """Bound background timer/reminder tasks; caller holds self._lock."""
        if len(self._scheduled_tasks) >= _MAX_SCHEDULED_ITEMS:
            raise ActionValidationError("too many active timers and reminders")

    async def shutdown(self) -> None:
        """Cancel every pending timer and reminder delivery when the bridge stops."""
        async with self._lock:
            tasks = list(self._scheduled_tasks.values())
            self._scheduled_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def pending(self, device_id: str, generation: int) -> dict[str, object] | None:
        key = (device_id, generation)
        async with self._lock:
            pending = self._pending.get(key)
            if pending is not None and pending.expires_at <= time.monotonic():
                self._pending.pop(key, None)
                pending = None
        if pending is None:
            return None
        return {
            "draft_id": pending.draft_id,
            "capability_id": pending.capability_id,
            "arguments": dict(pending.arguments),
            "expires_in_seconds": max(0, round(pending.expires_at - time.monotonic())),
        }

    async def _stage_pending(
        self,
        device_id: str,
        generation: int,
        capability_id: str,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        pending = _Pending(
            draft_id=f"draft-{secrets.token_hex(12)}",
            capability_id=capability_id,
            arguments=dict(arguments),
            expires_at=time.monotonic() + 300.0,
        )
        async with self._lock:
            self._pending[(device_id, generation)] = pending
        return {
            "draft_id": pending.draft_id,
            "capability_id": capability_id,
            "arguments": dict(arguments),
            "requires_exact_phone_approval": True,
            "expires_in_seconds": 300,
        }

    async def approve_pending(
        self,
        device_id: str,
        generation: int,
        draft_id: str,
        http: Any,
    ) -> tuple[object, list[dict[str, object]], float, bool]:
        key = (device_id, generation)
        async with self._lock:
            pending = self._pending.get(key)
            if pending is None or pending.draft_id != draft_id or pending.expires_at <= time.monotonic():
                raise ActionValidationError("pending approval is missing, stale, or mismatched")
            # Claim the draft before any await so concurrent approvals (double tap, retry)
            # cannot both execute it.
            self._pending.pop(key, None)
        approval = await self.issue_approval(
            device_id,
            generation,
            pending.capability_id,
            pending.arguments,
        )
        result = await self.execute(
            pending.capability_id,
            pending.arguments,
            http,
            device_id=device_id,
            generation=generation,
            approval_token=str(approval["approval_token"]),
        )
        return result

    async def issue_approval(
        self, device_id: str, generation: int, capability_id: str, arguments: Mapping[str, object]
    ) -> dict[str, object]:
        validate_action_arguments(capability_id, arguments)
        if not approval_required(capability_id):
            raise ActionValidationError("capability does not require phone approval")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(canonical_action(capability_id, arguments)).hexdigest()
        async with self._lock:
            now = time.monotonic()
            self._approvals = {key: value for key, value in self._approvals.items() if value.expires_at > now}
            self._approvals[token] = _Approval(digest, device_id, generation, now + 120.0)
        return {
            "approval_token": token,
            "capability_id": capability_id,
            "arguments": dict(arguments),
            "expires_in_seconds": 120,
        }

    async def _consume_approval(
        self, token: str, device_id: str, generation: int, capability_id: str, arguments: Mapping[str, object]
    ) -> None:
        digest = hashlib.sha256(canonical_action(capability_id, arguments)).hexdigest()
        async with self._lock:
            record = self._approvals.pop(token, None)
        if (
            record is None
            or record.expires_at <= time.monotonic()
            or record.device_id != device_id
            or record.generation != generation
            or not secrets.compare_digest(record.digest, digest)
        ):
            raise ActionValidationError("exact phone approval is missing, stale, used, or mismatched")

    async def execute(
        self,
        capability_id: str,
        arguments: Mapping[str, object],
        http: Any,
        *,
        device_id: str,
        generation: int,
        approval_token: str = "",
    ) -> tuple[object, list[dict[str, object]], float, bool]:
        validate_action_arguments(capability_id, arguments)
        if approval_required(capability_id) and not approval_token:
            pending = await self._stage_pending(device_id, generation, capability_id, arguments)
            observed = time.time()
            return (
                pending,
                [{"source": "agent_approval_queue", "observed_at": observed}],
                observed,
                False,
            )
        if approval_required(capability_id):
            await self._consume_approval(approval_token, device_id, generation, capability_id, arguments)
        now = time.time()
        key = (device_id, generation)
        if capability_id in {"draft_calendar_event", "draft_message", "draft_note"}:
            target = {
                "draft_calendar_event": "create_calendar_event",
                "draft_message": "send_approved_message",
                "draft_note": "append_scoped_note",
            }[capability_id]
            pending = await self._stage_pending(device_id, generation, target, arguments)
            return pending, [{"source": "agent_draft", "observed_at": now}], now, False
        if capability_id == "set_timer":
            self._require_reminder_callback()
            item_id = f"timer-{secrets.token_hex(8)}"
            seconds = arguments["seconds"]
            assert type(seconds) is int
            item = {
                "timer_id": item_id,
                "due_at": now + seconds,
                "label": str(arguments.get("label", "")),
                "verified": True,
            }
            async with self._lock:
                self._require_schedule_capacity()
                self._timers[key][item_id] = item
                self._undo[key].append(_Undo("cancel_timer", {"timer_id": item_id}))
                self._scheduled_tasks[item_id] = asyncio.create_task(
                    self._deliver_later(
                        item_id,
                        seconds,
                        f"Timer finished{': ' + item['label'] if item['label'] else ''}.",
                        http,
                    )
                )
            return item, [{"source": "agent_timer", "observed_at": now}], now, True
        if capability_id == "cancel_timer":
            async with self._lock:
                item = self._timers[key].pop(str(arguments["timer_id"]), None)
                task = self._scheduled_tasks.pop(str(arguments["timer_id"]), None)
            if item is None:
                raise ActionValidationError("timer is not active in this Agent session")
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return (
                {"timer_id": item["timer_id"], "cancelled": True, "verified": True},
                [{"source": "agent_timer", "observed_at": now}],
                now,
                True,
            )
        if capability_id == "create_reminder":
            self._require_reminder_callback()
            item_id = f"reminder-{secrets.token_hex(8)}"
            seconds = arguments["seconds"]
            assert type(seconds) is int
            item = {
                "reminder_id": item_id,
                "due_at": now + seconds,
                "text": str(arguments["text"]),
                "verified": True,
            }
            async with self._lock:
                self._require_schedule_capacity()
                self._reminders[key][item_id] = item
                self._undo[key].append(_Undo("cancel_reminder", {"reminder_id": item_id}))
                self._scheduled_tasks[item_id] = asyncio.create_task(
                    self._deliver_later(
                        item_id,
                        seconds,
                        f"Reminder: {item['text']}",
                        http,
                    )
                )
            return item, [{"source": "agent_reminder", "observed_at": now}], now, True
        if capability_id == "cancel_reminder":
            async with self._lock:
                item = self._reminders[key].pop(str(arguments["reminder_id"]), None)
                task = self._scheduled_tasks.pop(str(arguments["reminder_id"]), None)
            if item is None:
                raise ActionValidationError("reminder is not active in this Agent session")
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return (
                {"reminder_id": item["reminder_id"], "cancelled": True, "verified": True},
                [{"source": "agent_reminder", "observed_at": now}],
                now,
                True,
            )
        if capability_id == "undo_last_reversible_action":
            async with self._lock:
                undo = self._undo[key].pop() if self._undo[key] else None
            if undo is None:
                raise ActionValidationError("there is no reversible action in this Agent session")
            data, evidence, observed, _ = await self.execute(
                undo.capability_id, undo.arguments, http, device_id=device_id, generation=generation
            )
            return {"undone": undo.capability_id, "result": data, "verified": True}, evidence, observed, True
        if capability_id == "append_scoped_note":
            return await asyncio.to_thread(self._append_note, arguments)
        if capability_id == "list_calendar_events":
            return await self._list_calendar(arguments, http)
        if capability_id == "create_calendar_event":
            return await self._calendar_create(arguments, http)
        if capability_id == "send_approved_message":
            return await self._send_message(arguments, http)
        if capability_id in {"control_home_entity", "play_media", "pause_media", "set_media_volume"}:
            return await self._home_action(capability_id, arguments, http, key)
        raise ActionValidationError("unknown action capability")

    def _headers(self) -> dict[str, str]:
        if not self.config.hass_url or not self.config.hass_token:
            raise ActionUnavailableError("Home Assistant actions are not configured")
        return {"Authorization": f"Bearer {self.config.hass_token}", "Content-Type": "application/json"}

    async def _state(self, entity_id: str, http: Any) -> dict[str, object]:
        async with http.get(
            f"{self.config.hass_url}/api/states/{entity_id}", headers=self._headers(), allow_redirects=False
        ) as response:
            payload = await response.json(content_type=None)
            if response.status != 200 or not isinstance(payload, dict):
                raise ActionUnavailableError(f"Home Assistant state unavailable for {entity_id}")
            return payload

    async def _call(self, domain: str, service: str, data: dict[str, object], http: Any) -> None:
        async with http.post(
            f"{self.config.hass_url}/api/services/{domain}/{service}",
            headers=self._headers(),
            json=data,
            allow_redirects=False,
        ) as response:
            await response.read()
            if response.status not in {200, 201}:
                raise ActionUnavailableError("Home Assistant action failed")

    async def _home_action(
        self, capability_id: str, arguments: Mapping[str, object], http: Any, key: tuple[str, int]
    ) -> tuple[object, list[dict[str, object]], float, bool]:
        entity = str(arguments["entity_id"])
        domain = entity.split(".", 1)[0]
        # Validate and allowlist before any authenticated Home Assistant request, so a model-chosen
        # string can never reach /api/states/<anything> with the HA token.
        if not _HA_ENTITY_ID_RE.fullmatch(entity):
            raise ActionValidationError("invalid Home Assistant entity_id")
        if capability_id == "control_home_entity":
            if domain not in {"light", "switch", "scene"} or str(arguments["action"]) not in (
                self.config.home_actions.get(entity, frozenset())
            ):
                raise ActionValidationError("Home Assistant action is not allowlisted")
        elif entity not in self.config.media_entities or domain != "media_player":
            raise ActionValidationError("media player is not allowlisted")
        previous = await self._state(entity, http)
        if capability_id == "control_home_entity":
            action = str(arguments["action"])
            if domain not in {"light", "switch", "scene"} or action not in self.config.home_actions.get(
                entity, frozenset()
            ):
                raise ActionValidationError("Home Assistant action is not allowlisted")
            await self._call(domain, action, {"entity_id": entity}, http)
            expected = {"turn_on": "on", "turn_off": "off"}.get(action)
            if expected is not None:
                observed = await self._state(entity, http)
                if observed.get("state") != expected:
                    raise ActionUnavailableError("Home Assistant did not verify the requested state")
            prior = str(previous.get("state", ""))
            if domain in {"light", "switch"} and prior in {"on", "off"}:
                undo_action = "turn_on" if prior == "on" else "turn_off"
                async with self._lock:
                    self._undo[key].append(_Undo("control_home_entity", {"entity_id": entity, "action": undo_action}))
        else:
            if entity not in self.config.media_entities or domain != "media_player":
                raise ActionValidationError("media player is not allowlisted")
            service = {"play_media": "play_media", "pause_media": "media_pause", "set_media_volume": "volume_set"}[
                capability_id
            ]
            data: dict[str, object] = {"entity_id": entity}
            if capability_id == "play_media":
                data.update(
                    {"media_content_id": arguments["media_uri"], "media_content_type": arguments["media_type"]}
                )
            elif capability_id == "set_media_volume":
                data["volume_level"] = arguments["volume"]
            await self._call("media_player", service, data, http)
            observed = await self._state(entity, http)
            if capability_id == "pause_media" and observed.get("state") not in {"paused", "idle"}:
                raise ActionUnavailableError("media pause was not verified")
            attributes = previous.get("attributes", {})
            if capability_id == "set_media_volume" and isinstance(attributes, dict):
                prior_volume = attributes.get("volume_level")
                if isinstance(prior_volume, (int, float)) and 0 <= prior_volume <= 0.8:
                    async with self._lock:
                        self._undo[key].append(
                            _Undo("set_media_volume", {"entity_id": entity, "volume": prior_volume})
                        )
        observed_at = time.time()
        return (
            {"entity_id": entity, "verified": True},
            [{"source": "home_assistant", "entity_id": entity, "observed_at": observed_at}],
            observed_at,
            True,
        )

    async def _list_calendar(
        self, arguments: Mapping[str, object], http: Any
    ) -> tuple[object, list[dict[str, object]], float, bool]:
        entity = str(arguments["entity_id"])
        if entity not in self.config.calendar_entities:
            raise ActionValidationError("calendar is not allowlisted")
        async with http.get(
            f"{self.config.hass_url}/api/calendars/{entity}",
            headers=self._headers(),
            params={"start": arguments["start"], "end": arguments["end"]},
            allow_redirects=False,
        ) as response:
            payload = await response.json(content_type=None)
            if response.status != 200 or not isinstance(payload, list):
                raise ActionUnavailableError("calendar events are unavailable")
        observed = time.time()
        return (
            {"entity_id": entity, "events": payload[:50]},
            [{"source": "home_assistant_calendar", "observed_at": observed}],
            observed,
            False,
        )

    async def _calendar_create(
        self, arguments: Mapping[str, object], http: Any
    ) -> tuple[object, list[dict[str, object]], float, bool]:
        entity = str(arguments["entity_id"])
        if entity not in self.config.calendar_entities:
            raise ActionValidationError("calendar is not allowlisted")
        data = {key: value for key, value in arguments.items() if key != "entity_id"}
        data["entity_id"] = entity
        await self._call("calendar", "create_event", data, http)
        observed = time.time()
        return (
            {"entity_id": entity, "created": True, "verified": True},
            [{"source": "home_assistant_calendar", "observed_at": observed}],
            observed,
            True,
        )

    async def _send_message(
        self, arguments: Mapping[str, object], http: Any
    ) -> tuple[object, list[dict[str, object]], float, bool]:
        channel = str(arguments["channel"])
        recipient = str(arguments["recipient"])
        if recipient not in self.config.notify_targets.get(channel, frozenset()):
            raise ActionValidationError("message channel or recipient is not allowlisted")
        if re.search(r"[,;\n]", recipient):
            raise ActionValidationError("group or multi-recipient messages are not supported")
        await self._call("notify", channel, {"message": arguments["text"], "target": [recipient]}, http)
        observed = time.time()
        return (
            {"channel": channel, "recipient": recipient, "sent": True, "verified": True},
            [{"source": "home_assistant_notify", "observed_at": observed}],
            observed,
            True,
        )

    def _append_note(self, arguments: Mapping[str, object]) -> tuple[object, list[dict[str, object]], float, bool]:
        root = self.config.note_roots.get(str(arguments["root"]))
        relative = Path(str(arguments["path"]))
        if (
            root is None
            or relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ActionValidationError("note path is not allowlisted")
        if relative.suffix.lower() not in {".txt", ".md"}:
            raise ActionValidationError("only text and Markdown notes may be appended")
        text = str(arguments["text"]).replace("\x00", "").strip()
        raw = (text + "\n").encode("utf-8")
        descriptors: list[int] = []
        try:
            descriptors.append(os.open(root, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW))
            for component in relative.parts[:-1]:
                descriptors.append(
                    os.open(
                        component,
                        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptors[-1],
                    )
                )
            descriptor = os.open(
                relative.parts[-1],
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=descriptors[-1],
            )
            descriptors.append(descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ActionValidationError("note must be one regular, non-linked file")
            if metadata.st_size + len(raw) > 512_000:
                raise ActionValidationError("note would exceed the safe size limit")
            os.write(descriptor, raw)
            os.fsync(descriptor)
        except OSError as exc:
            raise ActionValidationError("note path is unavailable or contains a link") from exc
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        observed = time.time()
        return (
            {"root": arguments["root"], "path": relative.as_posix(), "appended": True, "verified": True},
            [{"source": "scoped_note", "observed_at": observed}],
            observed,
            True,
        )
