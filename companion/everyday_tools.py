"""Everyday: a narrow, fast tool surface for ordinary voice requests.

No Hermes agent, personal memory, project catalog, notes or native Workspace is reachable from
here. Home Assistant reads and actions reuse the broker's allowlisted status reader and the
action service (allowlist, prior-state undo, verification). Spoken names resolve only by exact
alias or entity id inside those allowlists; ambiguity is returned to the model to ask about.
Weather and time come from a configured source or are reported unavailable — never guessed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import ClientTimeout

try:
    from companion.reachy_agent_actions import ActionUnavailableError, ActionValidationError
    from companion.reachy_agent_broker import BrokerUnavailableError, BrokerValidationError
except ModuleNotFoundError:
    from reachy_agent_actions import ActionUnavailableError, ActionValidationError
    from reachy_agent_broker import BrokerUnavailableError, BrokerValidationError

_LOGGER = logging.getLogger(__name__)

EVERYDAY_TOOL_NAMES = frozenset(
    {"home_status", "home_control", "home_undo", "get_weather", "get_current_time"}
)
# Even when allowlisted for Agent Mode, only directly verifiable on/off domains are spoken controls.
_CONTROL_DOMAINS = frozenset({"light", "switch"})
_REFUSED_DOMAINS = frozenset(
    {"lock", "cover", "alarm_control_panel", "climate", "garage", "garage_door", "valve", "vacuum", "siren"}
)
_MAX_TOOL_CALLS_PER_TURN = 3
_MAX_MUTATIONS_PER_TURN = 1
_MAX_TOOL_ROUNDS = 3
_MAX_RESULT_CHARS = 2_000
_WORKSPACE_REPLY = "That's Workspace work. Switch to Workspace on the phone to continue with a project."

EVERYDAY_INSTRUCTIONS = (
    "You are Homebody's Everyday voice: quick, friendly, concise spoken answers. "
    "Answer general knowledge and explanations directly. You have no personal memory, notes, files, "
    "projects, coding tools or Hermes agent. For project, coding or roadmap requests reply exactly: "
    f"\"{_WORKSPACE_REPLY}\" "
    "For the time or date call get_current_time; for weather call get_weather; never guess either. "
    "For other live facts (news, prices, scores) say Everyday cannot check that. "
    "For home devices use home_status and home_control with the spoken name. If a tool returns "
    "ambiguous or unknown, ask which device was meant and list the offered names; never pick one. "
    "Only say a device changed when the tool returned verified true. "
    "If weather is stale, say how old it is. One device change per request."
)


def _normalise(name: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(name).casefold()).split())


@dataclass(slots=True)
class EverydayConfig:
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    weather_entity: str = ""
    weather_provider: str = ""
    latitude: float | None = None
    longitude: float | None = None
    timezone_name: str = ""
    ha_timeout_seconds: float = 4.0
    weather_timeout_seconds: float = 3.0
    weather_stale_seconds: float = 3 * 3600.0

    @classmethod
    def from_env(cls, env: Callable[[str], str] | None = None) -> EverydayConfig:
        source = env or (lambda name: os.getenv(name, ""))

        def get(name: str) -> str:
            return str(source(name) or "").strip()

        aliases: dict[str, tuple[str, ...]] = {}
        raw = get("REACHY_EVERYDAY_ALIASES")
        if raw:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("REACHY_EVERYDAY_ALIASES must be a JSON object of entity_id -> [names]")
            for entity, names in parsed.items():
                if isinstance(names, str):
                    names = [names]
                if isinstance(names, list):
                    aliases[str(entity)] = tuple(str(n)[:60] for n in names if str(n).strip())[:8]
        entity = get("REACHY_EVERYDAY_WEATHER_ENTITY")
        if entity and not re.fullmatch(r"weather\.[a-z0-9_]+", entity):
            raise ValueError("REACHY_EVERYDAY_WEATHER_ENTITY must be a weather.* entity id")
        provider = get("REACHY_EVERYDAY_WEATHER_PROVIDER").lower()
        if provider not in {"", "open-meteo"}:
            raise ValueError("REACHY_EVERYDAY_WEATHER_PROVIDER must be empty or open-meteo")
        latitude = longitude = None
        latlon = get("REACHY_EVERYDAY_WEATHER_LATLON")
        if latlon:
            try:
                lat_text, lon_text = latlon.split(",", 1)
                latitude, longitude = round(float(lat_text), 2), round(float(lon_text), 2)
            except ValueError as exc:
                raise ValueError("REACHY_EVERYDAY_WEATHER_LATLON must be 'latitude,longitude'") from exc
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError("REACHY_EVERYDAY_WEATHER_LATLON is out of range")
        if provider and latitude is None:
            raise ValueError("A public weather provider needs an explicit REACHY_EVERYDAY_WEATHER_LATLON")
        tz = get("REACHY_EVERYDAY_TIMEZONE")
        if tz:
            try:
                ZoneInfo(tz)
            except ZoneInfoNotFoundError as exc:
                raise ValueError("REACHY_EVERYDAY_TIMEZONE must be an IANA zone such as Europe/Amsterdam") from exc
        return cls(aliases, entity, provider, latitude, longitude, tz)


class TurnBudget:
    """At most three tool calls and one device change per user request; call ids run once."""

    def __init__(self) -> None:
        self.calls = 0
        self.mutations = 0
        self.seen: set[str] = set()

    def admit(self, call_id: str, name: str) -> str:
        if call_id and call_id in self.seen:
            return "duplicate"
        if call_id:
            self.seen.add(call_id)
        if self.calls >= _MAX_TOOL_CALLS_PER_TURN:
            return "Tool budget for this request is used up; answer with what you have."
        if name in {"home_control", "home_undo"} and self.mutations >= _MAX_MUTATIONS_PER_TURN:
            return "Only one device change is allowed per request; ask the user to repeat the next one."
        self.calls += 1
        if name in {"home_control", "home_undo"}:
            self.mutations += 1
        return ""


def _function(name: str, description: str, properties: dict[str, object], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def realtime_tool_specs(*, home: bool = True) -> list[dict[str, Any]]:
    """Realtime/Responses-shaped function tools for Everyday."""
    names = {"type": "array", "items": {"type": "string", "maxLength": 60}, "maxItems": 8}
    tools = [
        _function(
            "get_current_time",
            "Return the actual current local date and time from the Hermes host clock.",
            {},
            [],
        ),
        _function(
            "get_weather",
            "Return the configured local weather source for now, today or tomorrow, with its observation time.",
            {"when": {"type": "string", "enum": ["now", "today", "tomorrow"]}},
            ["when"],
        ),
    ]
    if home:
        tools += [
            _function(
                "home_status",
                "Read the current state of allowlisted home devices by spoken name. Empty list reads all.",
                {"names": names},
                ["names"],
            ),
            _function(
                "home_control",
                "Turn one allowlisted light or switch on or off by its spoken name and verify the new state.",
                {
                    "name": {"type": "string", "maxLength": 60},
                    "action": {"type": "string", "enum": ["turn_on", "turn_off"]},
                },
                ["name", "action"],
            ),
            _function("home_undo", "Undo the last verified device change from this conversation.", {}, []),
        ]
    return tools


def chat_tool_specs(*, home: bool = True) -> list[dict[str, Any]]:
    """The same tools in chat-completions shape (OpenAI, OpenRouter and compatible routers)."""
    return [
        {"type": "function", "function": {k: v for k, v in spec.items() if k != "type"}}
        for spec in realtime_tool_specs(home=home)
    ]


def session_generation(session_key: str) -> int:
    """Map an opaque voice-session key to the action service's per-session undo namespace."""
    return int(hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:12], 16)


class EverydayTools:
    """Execute Everyday tools against the broker's allowlists; never the Agent/Workspace surface."""

    def __init__(self, broker: Any, config: EverydayConfig | None = None, *, clock: Callable[[], float] = time.time):
        self.broker = broker
        self.config = config or EverydayConfig()
        self.clock = clock

    # -- name resolution -------------------------------------------------------------------
    def _read_entities(self) -> frozenset[str]:
        return frozenset(self.broker.config.home_entities)

    def _control_entities(self) -> dict[str, frozenset[str]]:
        return dict(self.broker.actions.config.home_actions)

    def _names_for(self, entity: str) -> tuple[str, ...]:
        return self.config.aliases.get(entity, ())

    def resolve(self, spoken: str, candidates: frozenset[str] | set[str]) -> dict[str, object]:
        """Exact alias/entity-id match within ``candidates`` only; never fuzzy, never widening."""
        wanted = _normalise(spoken)
        matches = sorted(
            entity
            for entity in candidates
            if wanted and (wanted == _normalise(entity) or wanted in {_normalise(n) for n in self._names_for(entity)})
        )
        if len(matches) == 1:
            return {"entity_id": matches[0]}
        offered = sorted({(self._names_for(e) or (e,))[0] for e in (matches or candidates)})[:12]
        return {"ambiguous" if matches else "unknown": True, "names": offered}

    def public_names(self) -> dict[str, list[str]]:
        def label(entity: str) -> str:
            return (self._names_for(entity) or (entity,))[0]

        controls = [e for e, actions in self._control_entities().items() if e.split(".", 1)[0] in _CONTROL_DOMAINS
                    and actions & {"turn_on", "turn_off"}]
        return {
            "status": sorted(label(e) for e in self._read_entities()),
            "controls": sorted(label(e) for e in controls),
        }

    # -- dispatch --------------------------------------------------------------------------
    async def call(
        self,
        name: str,
        arguments: Mapping[str, object],
        http: Any,
        *,
        device_id: str,
        generation: int,
        budget: TurnBudget,
        call_id: str = "",
        kids_live: Callable[[], bool] = lambda: False,
        is_current: Callable[[], bool] = lambda: True,
    ) -> dict[str, object]:
        if kids_live():
            return {"ok": False, "error": "Unavailable while Kids Mode is active."}
        if name not in EVERYDAY_TOOL_NAMES:
            return {"ok": False, "error": "That tool is not available in Everyday."}
        refusal = budget.admit(call_id, name)
        if refusal == "duplicate":
            return {"ok": False, "error": "This tool call was already handled.", "duplicate": True}
        if refusal:
            return {"ok": False, "error": refusal}
        if not isinstance(arguments, Mapping):
            arguments = {}
        try:
            if name == "get_current_time":
                result = self.current_time()
            elif name == "get_weather":
                result = await asyncio.wait_for(
                    self.weather(str(arguments.get("when") or "now"), http), self.config.weather_timeout_seconds + 1
                )
            elif name == "home_status":
                result = await asyncio.wait_for(
                    self.home_status(arguments.get("names"), http), self.config.ha_timeout_seconds
                )
            elif not is_current():
                result = {"ok": False, "error": "The conversation ended; nothing was changed."}
            elif name == "home_control":
                result = await asyncio.wait_for(
                    self.home_control(str(arguments.get("name") or ""), str(arguments.get("action") or ""), http,
                                      device_id=device_id, generation=generation),
                    self.config.ha_timeout_seconds + 2,
                )
            else:
                result = await asyncio.wait_for(
                    self.home_undo(http, device_id=device_id, generation=generation),
                    self.config.ha_timeout_seconds + 2,
                )
        except TimeoutError:
            result = {"ok": False, "verified": False,
                      "error": "The source did not answer in time; nothing is confirmed."}
        except (BrokerValidationError, ActionValidationError) as exc:
            result = {"ok": False, "verified": False, "error": f"Not allowed: {str(exc)[:120]}"}
        except (BrokerUnavailableError, ActionUnavailableError) as exc:
            result = {"ok": False, "verified": False, "error": f"Unavailable: {str(exc)[:120]}"}
        except Exception as exc:  # never leak URLs/tokens from transport errors
            _LOGGER.warning("Everyday tool %s failed (%s)", name, type(exc).__name__)
            result = {"ok": False, "verified": False, "error": "The source failed; nothing is confirmed."}
        if not is_current():
            return {"ok": False, "error": "The conversation ended before the result arrived."}
        return result

    # -- tools -----------------------------------------------------------------------------
    def _zone(self) -> Any:
        return ZoneInfo(self.config.timezone_name) if self.config.timezone_name else datetime.now().astimezone().tzinfo

    def current_time(self) -> dict[str, object]:
        now = datetime.fromtimestamp(self.clock(), tz=UTC).astimezone(self._zone())
        return {
            "ok": True,
            "local_time": now.strftime("%H:%M"),
            "weekday": now.strftime("%A"),
            "date": now.date().isoformat(),
            "timezone": self.config.timezone_name or str(now.tzinfo),
            "source": "hermes_host_clock",
        }

    async def home_status(self, names: object, http: Any) -> dict[str, object]:
        readable = self._read_entities()
        if not readable:
            return {"ok": False, "error": "No home devices are shared with Everyday."}
        entity_ids: list[str] = []
        for spoken in (names if isinstance(names, list) else [])[:8]:
            resolved = self.resolve(str(spoken), readable)
            if "entity_id" not in resolved:
                return {"ok": False, **resolved}
            entity_ids.append(str(resolved["entity_id"]))
        data, _evidence, observed = await self.broker._home_status(
            {"entity_ids": entity_ids or sorted(readable)}, http
        )
        states = [
            {"name": (self._names_for(item["entity_id"]) or (item["entity_id"],))[0], **item}
            for item in data.get("entities", [])
        ]
        return {"ok": True, "entities": states, "observed_at": int(observed), "source": "home_assistant"}

    async def home_control(
        self, spoken: str, action: str, http: Any, *, device_id: str, generation: int
    ) -> dict[str, object]:
        if action not in {"turn_on", "turn_off"}:
            return {"ok": False, "verified": False, "error": "Only turning a device on or off is supported."}
        controls = self._control_entities()
        everyday_controls = {e for e in controls if e.split(".", 1)[0] in _CONTROL_DOMAINS}
        resolved = self.resolve(spoken, everyday_controls)
        if "entity_id" not in resolved:
            refused = self.resolve(spoken, {e for e in controls if e.split(".", 1)[0] in _REFUSED_DOMAINS})
            if "entity_id" in refused:
                return {"ok": False, "verified": False,
                        "error": "Locks, covers, alarms and climate are not voice controls."}
            return {"ok": False, "verified": False, **resolved}
        entity = str(resolved["entity_id"])
        if action not in controls.get(entity, frozenset()):
            return {"ok": False, "verified": False, "error": "That action is not allowed for this device."}
        data, _evidence, observed, _side_effect = await self.broker.actions.execute(
            "control_home_entity",
            {"entity_id": entity, "action": action},
            http,
            device_id=f"everyday:{device_id}",
            generation=generation,
        )
        verified = isinstance(data, dict) and data.get("verified") is True
        return {
            "ok": verified,
            "verified": verified,
            "name": (self._names_for(entity) or (entity,))[0],
            "state": "on" if action == "turn_on" else "off",
            "observed_at": int(observed),
            "undo_available": True,
        }

    async def home_undo(self, http: Any, *, device_id: str, generation: int) -> dict[str, object]:
        data, _evidence, observed, _ = await self.broker.actions.execute(
            "undo_last_reversible_action", {}, http, device_id=f"everyday:{device_id}", generation=generation
        )
        verified = isinstance(data, dict) and data.get("verified") is True
        return {"ok": verified, "verified": verified, "observed_at": int(observed)}

    def weather_disclosure(self) -> dict[str, object]:
        if self.config.weather_entity:
            return {"source": "home_assistant", "label": "Home Assistant weather entity",
                    "location_leaves_home": False}
        if self.config.weather_provider == "open-meteo":
            return {
                "source": "open-meteo",
                "label": "Open-Meteo public forecast",
                "location_leaves_home": True,
                "detail": "The configured coordinates (rounded to ~1 km) are sent to api.open-meteo.com.",
            }
        return {"source": None, "label": "Not configured", "location_leaves_home": False}

    async def weather(self, when: str, http: Any) -> dict[str, object]:
        if when not in {"now", "today", "tomorrow"}:
            when = "now"
        if self.config.weather_entity:
            return await self._ha_weather(when, http)
        if self.config.weather_provider == "open-meteo":
            return await self._open_meteo(when, http)
        return {"ok": False, "error": "No weather source is configured for Everyday."}

    def _target_date(self, when: str) -> str:
        today = datetime.fromtimestamp(self.clock(), tz=UTC).astimezone(self._zone()).date()
        return (today + timedelta(days=1 if when == "tomorrow" else 0)).isoformat()

    async def _ha_weather(self, when: str, http: Any) -> dict[str, object]:
        url, token = self.broker.config.hass_url, self.broker.config.hass_token
        if not url or not token:
            return {"ok": False, "error": "Home Assistant is not configured on the Hermes host."}
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        entity = self.config.weather_entity
        timeout = ClientTimeout(total=self.config.weather_timeout_seconds)
        async with http.get(f"{url}/api/states/{entity}", headers=headers, allow_redirects=False,
                            timeout=timeout) as response:
            state = await response.json(content_type=None) if response.status == 200 else None
        if not isinstance(state, dict) or state.get("state") in {None, "unavailable", "unknown"}:
            return {"ok": False, "error": "The Home Assistant weather entity is unavailable."}
        attributes = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
        observed_at = self._parse_time(state.get("last_updated"))
        age = None if observed_at is None else max(0, int(self.clock() - observed_at))
        result: dict[str, object] = {
            "ok": True,
            "source": "home_assistant",
            "age_seconds": age,
            "stale": age is None or age > self.config.weather_stale_seconds,
            "temperature_unit": attributes.get("temperature_unit"),
        }
        if when == "now":
            result.update(
                condition=state.get("state"),
                temperature=attributes.get("temperature"),
                humidity=attributes.get("humidity"),
                wind_speed=attributes.get("wind_speed"),
            )
            return result
        async with http.post(f"{url}/api/services/weather/get_forecasts?return_response", headers=headers,
                             json={"entity_id": entity, "type": "daily"}, allow_redirects=False,
                             timeout=timeout) as response:
            payload = await response.json(content_type=None) if response.status == 200 else None
        forecasts = []
        if isinstance(payload, dict):
            service = payload.get("service_response", payload)
            entry = service.get(entity) if isinstance(service, dict) else None
            forecasts = entry.get("forecast", []) if isinstance(entry, dict) else []
        target = self._target_date(when)
        for item in forecasts if isinstance(forecasts, list) else []:
            when_at = self._parse_time(item.get("datetime")) if isinstance(item, dict) else None
            if when_at is None:
                continue
            local = datetime.fromtimestamp(when_at, tz=UTC).astimezone(self._zone()).date().isoformat()
            if local == target:
                result.update(
                    date=target,
                    condition=item.get("condition"),
                    temperature_high=item.get("temperature"),
                    temperature_low=item.get("templow"),
                    precipitation_probability=item.get("precipitation_probability"),
                )
                return result
        return {"ok": False, "error": f"Home Assistant has no forecast for {when}."}

    async def _open_meteo(self, when: str, http: Any) -> dict[str, object]:
        params = {
            "latitude": f"{self.config.latitude:.2f}",
            "longitude": f"{self.config.longitude:.2f}",
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": "2",
            "timezone": self.config.timezone_name or "auto",
        }
        async with http.get("https://api.open-meteo.com/v1/forecast", params=params, allow_redirects=False,
                            timeout=ClientTimeout(total=self.config.weather_timeout_seconds)) as response:
            payload = await response.json(content_type=None) if response.status == 200 else None
        if not isinstance(payload, dict):
            return {"ok": False, "error": "The public weather provider is unavailable."}
        base: dict[str, object] = {"ok": True, "source": "open-meteo", "temperature_unit": "°C"}
        if when == "now":
            current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
            observed = self._parse_time(current.get("time"), assume=self._zone())
            age = None if observed is None else max(0, int(self.clock() - observed))
            base.update(
                weather_code=current.get("weather_code"),
                temperature=current.get("temperature_2m"),
                wind_speed=current.get("wind_speed_10m"),
                age_seconds=age,
                stale=age is None or age > self.config.weather_stale_seconds,
            )
            return base if current else {"ok": False, "error": "No current weather was returned."}
        daily = payload.get("daily") if isinstance(payload.get("daily"), dict) else {}
        dates = daily.get("time") if isinstance(daily.get("time"), list) else []
        target = self._target_date(when)
        if target not in dates:
            return {"ok": False, "error": f"No forecast for {when}."}
        index = dates.index(target)

        def pick(key: str) -> object:
            values = daily.get(key)
            return values[index] if isinstance(values, list) and index < len(values) else None

        base.update(
            date=target,
            weather_code=pick("weather_code"),
            temperature_high=pick("temperature_2m_max"),
            temperature_low=pick("temperature_2m_min"),
            precipitation_probability=pick("precipitation_probability_max"),
            stale=False,
        )
        return base

    @staticmethod
    def _parse_time(value: object, *, assume: Any = UTC) -> float | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=assume)
        return parsed.timestamp()

    # -- text backend (Pipeline and GPT-Live delegations) ----------------------------------
    async def answer(
        self,
        text: str,
        *,
        http: Any,
        provider: Any,
        key: str,
        device_id: str,
        generation: int,
        language: str = "",
        kids_live: Callable[[], bool] = lambda: False,
        is_current: Callable[[], bool] = lambda: True,
        timeout_seconds: float = 20.0,
    ) -> dict[str, object]:
        """One bounded tool-calling text turn on the fast Everyday model."""
        if not key:
            raise BrokerUnavailableError("The Everyday text provider key is not configured on the Hermes host")
        system = EVERYDAY_INSTRUCTIONS + (f" Reply in the language with code '{language[:12]}'." if language else "")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": text[:4000]},
        ]
        budget = TurnBudget()
        used: list[dict[str, object]] = []
        tools = chat_tool_specs()

        async def run() -> str:
            for round_index in range(_MAX_TOOL_ROUNDS + 1):
                body = provider.request({
                    "model": provider.model("everyday"),
                    "messages": messages,
                    "max_completion_tokens": 400,
                    "store": False,
                    **({"tools": tools, "tool_choice": "auto"} if round_index < _MAX_TOOL_ROUNDS else {}),
                })
                async with http.post(provider.chat_url, headers={"Authorization": f"Bearer {key}"}, json=body,
                                     allow_redirects=False) as response:
                    payload = await response.json(content_type=None)
                    if response.status != 200 or not isinstance(payload, dict):
                        raise BrokerUnavailableError(f"Everyday text provider returned HTTP {response.status}")
                message = payload["choices"][0]["message"]
                calls = message.get("tool_calls") or []
                if not calls:
                    return str(message.get("content") or "").strip()
                messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
                for call in calls[:_MAX_TOOL_CALLS_PER_TURN + 1]:
                    function = call.get("function") or {}
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except (TypeError, ValueError):
                        arguments = {}
                    result = await self.call(
                        str(function.get("name") or ""), arguments, http, device_id=device_id,
                        generation=generation, budget=budget, call_id=str(call.get("id") or ""),
                        kids_live=kids_live, is_current=is_current,
                    )
                    used.append({"name": str(function.get("name") or "")[:40], "ok": result.get("ok") is True,
                                 "verified": result.get("verified") is True})
                    messages.append({"role": "tool", "tool_call_id": call.get("id"),
                                     "content": json.dumps(result)[:_MAX_RESULT_CHARS]})
            return ""

        reply = await asyncio.wait_for(run(), timeout_seconds)
        if not is_current() or kids_live():
            raise BrokerValidationError("stale_session")
        return {
            "text": reply or "Sorry, I couldn't work that out. Could you say it another way?",
            "tools": used,
            "source": {"kind": "everyday", "provider": provider.name, "model": provider.model("everyday")},
        }
