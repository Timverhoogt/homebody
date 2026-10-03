"""Reachy Mini App SDK entry point for Homebody."""

from __future__ import annotations

import json
import logging
import secrets
import socket
import subprocess
import threading
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from reachy_mini import ReachyMini, ReachyMiniApp
from starlette.concurrency import run_in_threadpool

from .agent_audit import AgentAuditLog
from .bluetooth import BluetoothGamepadService
from .config import AppConfig, config_transaction, default_config_path, load_config, merge_config, save_config
from .contextual_offers import ContextualOffer
from .gpio_buttons import ButtonEvent, ButtonName, GpioButtonService
from .hermes_client import HermesBridgeClient
from .kids_mode import KidsProfile
from .local_vision import LocalVisionClient, LocalVisionError
from .mcp_oauth import OAuthError, OAuthServer
from .mcp_public import PublicAgentListener, build_public_app
from .mcp_server import PROTOCOL_VERSIONS, McpServer, new_token, token_digest, token_matches
from .platform_info import host
from .presence import PresenceObservation
from .robot_tools import robot_control_options
from .runtime import HermesVoiceRuntime

_LOGGER = logging.getLogger(__name__)
_STATIC_DIR = Path(__file__).resolve().parent / "static"


class SettingsUpdate(BaseModel):
    """A deliberately bounded settings payload for the app UI."""

    bridge_url: str | None = Field(default=None, max_length=2048)
    api_key: str | None = Field(default=None, max_length=4096)
    current_api_key: str | None = Field(default=None, max_length=4096)
    model: str | None = Field(default=None, max_length=200)
    conversation_mode: str | None = Field(default=None, max_length=32)
    language: str | None = Field(default=None, max_length=12)
    stt_provider: str | None = Field(default=None, max_length=64)
    stt_model: str | None = Field(default=None, max_length=200)
    tts_provider: str | None = Field(default=None, max_length=64)
    tts_model: str | None = Field(default=None, max_length=200)
    tts_voice: str | None = Field(default=None, max_length=200)
    system_prompt: str | None = Field(default=None, max_length=16000)
    continuous_conversation: bool | None = None
    conversation_timeout_seconds: float | None = Field(default=None, ge=30, le=3600)
    initial_speech_timeout_seconds: float | None = Field(default=None, ge=1, le=30)
    max_utterance_seconds: float | None = Field(default=None, ge=1, le=120)
    end_silence_seconds: float | None = Field(default=None, ge=0.1, le=5)
    vad_min_rms: float | None = Field(default=None, ge=0.001, le=0.5)
    vad_noise_multiplier: float | None = Field(default=None, ge=1, le=20)
    wake_keyword_score: float | None = Field(default=None, ge=0, le=10)
    wake_keyword_threshold: float | None = Field(default=None, ge=0.01, le=1)
    wake_cooldown_seconds: float | None = Field(default=None, ge=0.5, le=30)
    motion_enabled: bool | None = None
    barge_in_enabled: bool | None = None
    camera_enabled: bool | None = None
    camera_feed_enabled: bool | None = None
    camera_controls_enabled: bool | None = None
    camera_controls_handedness: Literal["left", "right"] | None = None
    face_tracking_enabled: bool | None = None
    face_tracking_weight: float | None = Field(default=None, ge=0, le=1)
    doa_enabled: bool | None = None
    proactive_presence_enabled: bool | None = None
    presence_acknowledgement_enabled: bool | None = None
    presence_acknowledgement_cooldown_seconds: float | None = Field(default=None, ge=30, le=3600)
    initiative_policy_enabled: bool | None = None
    initiative_mode: Literal["quiet", "balanced", "engaged"] | None = None
    initiative_quiet_hours_enabled: bool | None = None
    initiative_quiet_hours_start: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    initiative_quiet_hours_end: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    initiative_hourly_budget: int | None = Field(default=None, ge=1, le=10)
    initiative_daily_budget: int | None = Field(default=None, ge=1, le=30)
    initiative_topic_cooldown_seconds: float | None = Field(default=None, ge=60, le=86400)
    initiative_duplicate_window_seconds: float | None = Field(default=None, ge=30, le=3600)
    initiative_dismissal_backoff_seconds: float | None = Field(default=None, ge=60, le=86400)
    contextual_offers_enabled: bool | None = None
    contextual_offer_response_window_seconds: float | None = Field(default=None, ge=5, le=30)
    shared_physical_context_enabled: bool | None = None
    presentation_window_seconds: float | None = Field(default=None, ge=5, le=30)
    robot_tools_enabled: bool | None = None
    home_assistant_enabled: bool | None = None
    home_assistant_controls_enabled: bool | None = None
    home_assistant_camera_enabled: bool | None = None
    home_assistant_assist_enabled: bool | None = None
    home_assistant_port: int | None = Field(default=None, ge=1024, le=65535)
    realtime_model: str | None = Field(default=None, max_length=200)
    realtime_voice: str | None = Field(default=None, max_length=64)
    realtime_reasoning_effort: str | None = Field(default=None, max_length=32)
    local_vision_enabled: bool | None = None
    local_vision_url: str | None = Field(default=None, max_length=2048)
    local_vision_model: str | None = Field(default=None, max_length=200)
    local_ai_accelerator: Literal["auto", "cpu"] | None = None
    mcp_enabled: bool | None = None
    mcp_vision_enabled: bool | None = None
    mcp_oauth_enabled: bool | None = None
    mcp_public_url: str | None = Field(default=None, max_length=300)
    mcp_public_bind: str | None = Field(default=None, max_length=64)
    mcp_public_port: int | None = Field(default=None, ge=1024, le=65535)


def _authorize_credential_change(current: AppConfig, merged: AppConfig, provided: str | None) -> None:
    """Require the existing bridge key before the unauthenticated UI can redirect or replace it.

    Without this, any LAN client could swap in its own key (unlocking bearer-protected
    routes such as the camera snapshot) or point the bridge URL at a host it controls
    and receive the real key on the next authenticated bridge call.
    """
    if not current.api_key:
        return
    if (
        merged.bridge_url == current.bridge_url
        and merged.api_key == current.api_key
        # Camera frames go to the vision server, so redirecting it is as sensitive as the bridge.
        and merged.local_vision_url == current.local_vision_url
        # Publishing agent sign-in to the internet is an owner decision.
        and merged.mcp_public_url == current.mcp_public_url
        and merged.mcp_oauth_enabled == current.mcp_oauth_enabled
        and merged.mcp_public_bind == current.mcp_public_bind
        and merged.mcp_public_port == current.mcp_public_port
    ):
        return
    if not provided or not secrets.compare_digest(provided.strip(), current.api_key):
        raise HTTPException(
            status_code=403,
            detail="Enter the current API key to change the bridge URL, API key, vision server URL or agent sign-in",
        )


class PresenceSignalRequest(BaseModel):
    """One identity-free signal from an explicitly trusted local integration."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["home_assistant", "trusted_sensor"]
    occupied: StrictBool
    attentive: StrictBool = False
    direction_degrees: float | None = Field(default=None, ge=-60, le=60)
    confidence: float = Field(default=1.0, ge=0, le=1)

    @field_validator("direction_degrees", "confidence", mode="before")
    @classmethod
    def require_json_number(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("presence values must be JSON numbers")
        return value


class ContextualOfferRequest(BaseModel):
    """One pre-rendered offer from an explicitly allowlisted local context source."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["calendar", "reminder", "timer", "home_assistant", "weather", "project"]
    topic: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,47}$")
    confidence: float = Field(ge=0, le=1)
    fingerprint: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    text: str = Field(min_length=1, max_length=180)
    accepted_text: str = Field(min_length=1, max_length=240)

    @field_validator("confidence", mode="before")
    @classmethod
    def require_offer_number(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("contextual offer confidence must be a JSON number")
        return value


class ContextualOfferResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: int = Field(ge=1)
    response: Literal["yes", "no", "later"]


InitiativeCategory = Literal[
    "presence", "calendar", "reminder", "timer", "home_assistant", "weather", "project", "presentation"
]


class InitiativePreferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: InitiativeCategory
    disabled: StrictBool


class InitiativePreferenceResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: InitiativeCategory | None = None


class RobotActionRequest(BaseModel):
    action: str = Field(min_length=1, max_length=32)
    value: str = Field(min_length=1, max_length=32)


class RobotNudgeRequest(BaseModel):
    axis: str = Field(min_length=1, max_length=32)
    delta: float = Field(default=0.0, ge=-60, le=60)


class CameraControlMoveRequest(BaseModel):
    session_id: str = Field(pattern=r"^camera-[0-9a-f]{32}$")
    sequence: int = Field(ge=1, le=2_147_483_647)
    pan: float = Field(ge=-1.0, le=1.0)
    tilt: float = Field(ge=-1.0, le=1.0)

    @field_validator("pan", "tilt", mode="before")
    @classmethod
    def require_json_number(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("camera control values must be JSON numbers")
        return value


class CameraControlSessionRequest(BaseModel):
    session_id: str = Field(pattern=r"^camera-[0-9a-f]{32}$")
    sequence: int = Field(ge=1, le=2_147_483_647)


class BluetoothDeviceRequest(BaseModel):
    address: str = Field(pattern=r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")


class BluetoothScanRequest(BaseModel):
    seconds: int = Field(default=12, ge=5, le=30)


class GamepadEnabledRequest(BaseModel):
    enabled: bool


class McpTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_api_key: str = Field(default="", max_length=4096)


class McpOAuthDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_api_key: str = Field(default="", max_length=4096)
    pending_id: str = Field(min_length=1, max_length=64)


_MCP_MAX_BODY_BYTES = 64 * 1024


def _public_host(config: AppConfig) -> str:
    return urlparse(config.mcp_public_url).netloc.lower() if config.mcp_public_url else ""


def _arrived_on_public_host(request: Request, config: AppConfig) -> bool:
    public = _public_host(config)
    if not public:
        return False
    hosts = {request.headers.get("host", "").lower(), request.headers.get("x-forwarded-host", "").lower()}
    return public in hosts


def _same_origin(origin: str, request: Request) -> bool:
    """MCP over HTTP must reject foreign browser origins (DNS-rebinding protection)."""
    host = request.headers.get("host", "")
    return origin.rstrip("/").split("://", 1)[-1] == host


class VisionQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(default="What do you see?", min_length=1, max_length=300)


class LocalVisionTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_vision_url: str | None = Field(default=None, max_length=2048)
    local_vision_model: str | None = Field(default=None, max_length=200)
    current_api_key: str | None = Field(default=None, max_length=4096)


class GpioButtonsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool
    green_pin: int | None = Field(default=17, ge=0, le=1023)
    red_pin: int | None = Field(default=None, ge=0, le=1023)
    long_press_seconds: float = Field(default=2.0, ge=0.5, le=10.0)


class PowerRequest(BaseModel):
    mode: str
    duration_minutes: float = Field(default=60, ge=1, le=480)


class ConfirmationRequest(BaseModel):
    confirm: str


class AnnouncementRequest(BaseModel):
    text: str = Field(min_length=1, max_length=15_000)
    provider: str = Field(default="", max_length=32)
    model: str = Field(default="", max_length=120)
    voice: str = Field(default="", max_length=120)
    behavior: str = Field(default="wake_and_return", max_length=32)
    repeat: int = Field(default=1, ge=1, le=10)
    pause_seconds: float = Field(default=1.0, ge=0, le=60)


class AnnouncementStopRequest(BaseModel):
    clear_queue: bool = True


class KidsModeRequest(BaseModel):
    nickname: str = Field(default="", max_length=32)
    age_band: str = Field(default="7-9", pattern=r"^(4-6|7-9|10-12)$")
    activity: str = Field(default="buddy", pattern=r"^(buddy|story|quiz|riddles|calm|ispy)$")
    language: str = Field(default="en", pattern=r"^(en|nl)$")
    duration_minutes: int = Field(default=30, ge=15, le=60)
    motion_enabled: bool = True
    camera_consent: bool = False

class AgentProfileRequest(BaseModel):
    profile: Literal["conversation", "agent"]


class AgentApprovalRequest(BaseModel):
    capability_id: str = Field(min_length=1, max_length=96, pattern=r"^[a-z][a-z0-9_]+$")
    arguments: dict[str, object]


class AgentPendingApprovalRequest(BaseModel):
    draft_id: str = Field(pattern=r"^draft-[0-9a-f]{24}$")


class AgentRunPreviewRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=2_000)


class AgentRunRequest(BaseModel):
    run_id: str = Field(pattern=r"^run-[0-9a-f]{24}$")
    step_id: str = Field(default="", pattern=r"^(?:|step-[1-5])$")


class AgentReminderDeliveryRequest(BaseModel):
    item_id: str = Field(pattern=r"^(timer|reminder)-[0-9a-f]{16}$")
    text: str = Field(min_length=1, max_length=2_000)


class Homebody(ReachyMiniApp):
    """Embodied voice frontend for a user's own agent (Hermes Agent or OpenClaw)."""

    custom_app_url: str | None = "http://0.0.0.0:8042"
    request_media_backend: str | None = "local"

    def __init__(self, running_on_wireless: bool = False) -> None:
        super().__init__(running_on_wireless=running_on_wireless)
        self._runtime: HermesVoiceRuntime | None = None
        self._bluetooth = BluetoothGamepadService(self._handle_gamepad_action)
        self._gamepad_config_lock = threading.Lock()
        self._gpio_buttons = GpioButtonService(self._handle_button_event)
        self._gpio_config_lock = threading.Lock()
        self._mcp = McpServer(lambda: self._runtime)
        self._oauth = OAuthServer(default_config_path().with_name("mcp-oauth.json"))
        self._agent_listener = PublicAgentListener(self._build_public_app)
        self._register_settings_routes()

    def _handle_gamepad_action(self, kind: str, action: str, value: str) -> bool:
        """Route controller input through the same safety gates as the Robot tab."""
        if self._runtime is None:
            raise RuntimeError("Voice runtime has not started")
        if kind == "stop":
            result = self._runtime.stop_manual_robot_action()
            if result.get("robot_stopped") is not True:
                raise RuntimeError("Robot action controller did not confirm Stop completion")
            return True
        if kind == "precision":
            self._runtime.queue_precision_robot_action(action, float(value))
            return True
        self._runtime.queue_manual_robot_action(action, value)
        return True

    def _handle_button_event(self, event: ButtonEvent) -> None:
        """Map the physical buttons: red stops or sleeps, green listens/wakes or stands by."""
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("Voice runtime has not started")
        _LOGGER.info("GPIO %s button %s press", event.button, event.gesture)
        if event.button == "red" and event.gesture == "short":
            # Stop is always honoured, even while starting or Kids-locked.
            runtime.physical_stop()
            return
        if not runtime.control_ready:
            raise RuntimeError("Voice runtime is still starting")
        if event.button == "red":
            try:
                runtime.physical_stop()
            finally:
                runtime.set_power_mode("sleep")
        elif event.gesture == "short":
            runtime.request_button_wake()
        else:
            runtime.set_power_mode("standby")

    def _gpio_status(self, config: AppConfig | None = None) -> dict[str, object]:
        config = config or load_config()
        return {
            **self._gpio_buttons.status(),
            "configured": {
                "enabled": config.gpio_buttons_enabled,
                "chip": config.gpio_chip,
                "green_pin": config.gpio_green_pin,
                "red_pin": config.gpio_red_pin,
                "long_press_seconds": config.gpio_long_press_seconds,
            },
        }

    def _start_gpio_buttons(self, config: AppConfig) -> None:
        if not config.gpio_buttons_enabled:
            self._gpio_buttons.stop()
            return
        pins: dict[ButtonName, int] = {}
        if config.gpio_green_pin is not None:
            pins["green"] = config.gpio_green_pin
        if config.gpio_red_pin is not None:
            pins["red"] = config.gpio_red_pin
        self._gpio_buttons.start(chip=config.gpio_chip, pins=pins, long_press_seconds=config.gpio_long_press_seconds)

    def _register_settings_routes(self) -> None:
        if self.settings_app is None:
            return

        @self.settings_app.middleware("http")
        async def lock_management_routes(request: Request, call_next):  # type: ignore[no-untyped-def]
            """Fail closed on management APIs while the child-facing UI is locked."""
            try:
                public_request = _arrived_on_public_host(request, load_config())
            except Exception:
                public_request = False
            # Hosted agents use the separate public listener (mcp_public_port). If a tunnel is pointed
            # at the dashboard port by mistake, nothing here answers it. This is only a backstop: a
            # proxy that rewrites Host is not caught, which is why the public routes live elsewhere.
            if public_request:
                return JSONResponse(status_code=404, content={"detail": "Not found"})
            allowed = {
                "/api/status",
                "/api/kids/stop",
                "/api/robot/stop",
                "/api/agent/stop",
            }
            if (
                request.url.path.startswith("/api/")
                and request.url.path not in allowed
                and self._runtime is not None
                and self._runtime.kids_controls_locked
            ):
                return JSONResponse(
                    status_code=423,
                    content={"detail": "Parent controls are locked while Kids Mode is active"},
                )
            return await call_next(request)

        @self.settings_app.get("/manifest.webmanifest", include_in_schema=False)
        def web_manifest() -> FileResponse:
            return FileResponse(
                _STATIC_DIR / "manifest.webmanifest",
                media_type="application/manifest+json",
                headers={"Cache-Control": "no-cache"},
            )

        @self.settings_app.get("/service-worker.js", include_in_schema=False)
        def service_worker() -> FileResponse:
            return FileResponse(
                _STATIC_DIR / "service-worker.js",
                media_type="application/javascript",
                headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
            )

        @self.settings_app.get("/api/status")
        def status() -> dict[str, object]:
            runtime_payload = self._runtime.status() if self._runtime is not None else {"state": "not_started"}
            kids_payload = runtime_payload.get("kids_mode")
            child_locked = isinstance(kids_payload, dict) and kids_payload.get("locked") is True
            try:
                config = load_config()
                config_payload: dict[str, object] = (
                    config.child_status_dict() if child_locked else config.redacted_dict()
                )
                config_error = ""
            except Exception as exc:
                config_payload = {}
                config_error = "Configuration is unavailable" if child_locked else str(exc)
            return {
                "app": "homebody",
                "wake_phrase": "Hey Homebody",
                "wake_phrases": ["Hey Homebody", "Hey Hermes", "Okay Nabu", "Hey Reachy"],
                "config": config_payload,
                "config_error": config_error,
                "runtime": runtime_payload,
                "host": host().as_dict(),
            }

        @self.settings_app.post("/api/presence/signal")
        def presence_signal(
            signal: PresenceSignalRequest,
            authorization: str = Header(default=""),
        ) -> dict[str, object]:
            config = load_config()
            if not config.api_key:
                raise HTTPException(status_code=503, detail="Presence signal authentication is not configured")
            if not secrets.compare_digest(authorization, f"Bearer {config.api_key}"):
                raise HTTPException(status_code=401, detail="Unauthorized")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            try:
                presence = self._runtime.observe_presence(
                    PresenceObservation(
                        source=signal.source,
                        occupied=bool(signal.occupied),
                        attentive=bool(signal.attentive),
                        direction_degrees=signal.direction_degrees,
                        confidence=signal.confidence,
                    )
                )
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"ok": True, "presence": presence}

        @self.settings_app.post("/api/initiative/offers")
        def contextual_offer(
            request: ContextualOfferRequest,
            authorization: str = Header(default=""),
        ) -> dict[str, object]:
            config = load_config()
            if not config.api_key:
                raise HTTPException(status_code=503, detail="Contextual offer authentication is not configured")
            if not secrets.compare_digest(authorization, f"Bearer {config.api_key}"):
                raise HTTPException(status_code=401, detail="Unauthorized")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            try:
                return self._runtime.submit_contextual_offer(
                    ContextualOffer(
                        source=request.source,
                        topic=request.topic,
                        confidence=request.confidence,
                        fingerprint=request.fingerprint,
                        text=request.text,
                        accepted_text=request.accepted_text,
                    )
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @self.settings_app.post("/api/initiative/offers/respond")
        def contextual_offer_response(
            request: ContextualOfferResponseRequest,
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            if x_reachy_adult_ui != "unlocked":
                raise HTTPException(status_code=403, detail="An unlocked adult UI action is required")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            try:
                return self._runtime.respond_to_contextual_offer(request.token, request.response)
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @self.settings_app.post("/api/initiative/preferences")
        def set_initiative_preference(
            request: InitiativePreferenceRequest,
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            if x_reachy_adult_ui != "unlocked":
                raise HTTPException(status_code=403, detail="An unlocked adult UI action is required")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            preferences = self._runtime.set_initiative_category_disabled(request.category, request.disabled)
            return {"ok": True, "preferences": preferences}

        @self.settings_app.post("/api/initiative/preferences/reset")
        def reset_initiative_preferences(
            request: InitiativePreferenceResetRequest,
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            if x_reachy_adult_ui != "unlocked":
                raise HTTPException(status_code=403, detail="An unlocked adult UI action is required")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            return {"ok": True, "preferences": self._runtime.reset_initiative_preferences(request.category)}

        @self.settings_app.post("/api/presentation/start")
        def start_presentation(
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            if x_reachy_adult_ui != "unlocked":
                raise HTTPException(status_code=403, detail="An unlocked adult UI action is required")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            try:
                return {"ok": True, "presentation": self._runtime.start_presentation_window()}
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @self.settings_app.post("/api/presentation/stop")
        def stop_presentation(
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            if x_reachy_adult_ui != "unlocked":
                raise HTTPException(status_code=403, detail="An unlocked adult UI action is required")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            return {
                "ok": True,
                "presentation": self._runtime.stop_presentation_window("user_stopped"),
            }

        @self.settings_app.post("/api/settings")
        def update_settings(update: SettingsUpdate) -> dict[str, object]:
            try:
                with config_transaction():
                    current = load_config()
                    changes = update.model_dump(exclude_none=True)
                    provided_key = changes.pop("current_api_key", None)
                    merged = merge_config(current, changes)
                    _authorize_credential_change(current, merged, provided_key)
                    path = save_config(merged)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except OSError as exc:
                _LOGGER.error("Could not save Homebody settings: %s", exc)
                raise HTTPException(status_code=500, detail="Settings could not be saved on Reachy") from exc
            _LOGGER.info("Homebody settings updated at %s (secret values redacted)", path)
            if self._runtime is not None:
                # Start, stop or move the hosted-agent listener; outside run() nothing listens.
                self._agent_listener.sync(merged)
            if self._runtime is not None and (
                not merged.camera_feed_enabled or not merged.camera_controls_enabled
            ):
                revoke_camera_control = getattr(self._runtime, "revoke_camera_control", None)
                if callable(revoke_camera_control):
                    revoke_camera_control()
            if self._runtime is not None and not merged.contextual_offers_enabled:
                cancel_contextual_offer = getattr(self._runtime, "cancel_contextual_offer", None)
                if callable(cancel_contextual_offer):
                    cancel_contextual_offer("contextual_offers_disabled")
            if self._runtime is not None and (
                not merged.shared_physical_context_enabled or not merged.camera_enabled
            ):
                stop_presentation = getattr(self._runtime, "stop_presentation_window", None)
                if callable(stop_presentation):
                    stop_presentation("shared_physical_context_disabled")
            return {
                "ok": True,
                "config": merged.redacted_dict(),
                "note": (
                    "Connection and conversation settings apply on the next wake. "
                    "Wake-model tuning applies after an app restart."
                ),
            }

        @self.settings_app.post("/api/agent/profile")
        def set_agent_profile(
            update: AgentProfileRequest,
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            if x_reachy_adult_ui != "unlocked":
                raise HTTPException(status_code=403, detail="An unlocked adult UI action is required")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            try:
                agent = self._runtime.set_capability_profile(update.profile, adult_ui_unlocked=True)
                with config_transaction():
                    save_config(merge_config(load_config(), {"capability_profile": update.profile}))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"ok": True, "agent": agent}

        @self.settings_app.post("/api/agent/stop")
        def stop_agent() -> dict[str, object]:
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            return {"ok": True, "agent": self._runtime.cancel_agent_work("stopped")}

        @self.settings_app.get("/api/agent/capabilities")
        def agent_capabilities() -> dict[str, object]:
            try:
                client = HermesBridgeClient(load_config())
                try:
                    return {"capabilities": client.agent_capabilities()}
                finally:
                    client.close()
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        @self.settings_app.get("/api/agent/activity")
        def agent_activity(
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            if x_reachy_adult_ui != "unlocked":
                raise HTTPException(status_code=403, detail="An unlocked adult UI action is required")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            # Timeline reads must remain available while a voice request is active;
            # do not claim the runtime's single owner-request slot just to poll it.
            # The generation check below still prevents private activity crossing a
            # Kids/profile/privacy transition while this request is in flight.
            context = self._runtime.agent_broker_context(explicit_private_intent=True)
            if context.capability_profile != "agent" or context.kids_mode_active:
                raise HTTPException(status_code=423, detail="Agent activity is unavailable")
            request_id = f"agent-activity-{secrets.token_hex(8)}"
            try:
                client = HermesBridgeClient(load_config())
                try:
                    # The bridge may have restarted since the profile was selected.
                    # Re-publish the same authoritative generation before this
                    # read-only poll; equal-generation/equal-state updates do not
                    # cancel the active voice task.
                    client.establish_agent_session(context)
                    activity = client.agent_activity(context, request_id=request_id)
                finally:
                    client.close()
                if not self._runtime.agent_session_is_current(context.session_generation):
                    raise HTTPException(status_code=423, detail="Agent activity became stale")
                return {"activity": activity}
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        @self.settings_app.post("/api/agent/run/preview")
        def preview_agent_run(
            request: AgentRunPreviewRequest,
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            if x_reachy_adult_ui != "unlocked":
                raise HTTPException(status_code=403, detail="An unlocked adult UI action is required")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            request_id = ""
            context = None
            try:
                request_id, context = self._runtime._begin_agent_request("Agent 0.5 plan preview")
                client = HermesBridgeClient(load_config())
                try:
                    client.establish_agent_session(context)
                    run = client.preview_agent_run(request.goal, context, request_id=request_id)
                finally:
                    client.close()
                if not self._runtime._finish_agent_request(
                    request_id, context.session_generation, succeeded=True
                ):
                    raise HTTPException(status_code=423, detail="Agent run preview became stale")
                self._runtime.record_agent_run_event("previewed", run)
                return {"run": run}
            except RuntimeError as exc:
                raise HTTPException(status_code=423, detail=str(exc)) from exc
            except HTTPException:
                raise
            except Exception as exc:
                if request_id and context is not None:
                    self._runtime._finish_agent_request(
                        request_id, context.session_generation, succeeded=False
                    )
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        def agent_run_action(
            action: str,
            request: AgentRunRequest,
            x_reachy_adult_ui: str,
        ) -> dict[str, object]:
            if x_reachy_adult_ui != "unlocked":
                raise HTTPException(status_code=403, detail="An unlocked adult UI action is required")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            context = self._runtime.agent_broker_context(explicit_private_intent=True)
            if context.capability_profile != "agent" or context.kids_mode_active:
                raise HTTPException(status_code=423, detail="Agent run control is unavailable")
            try:
                client = HermesBridgeClient(load_config())
                try:
                    run = client.agent_run_action(
                        action,
                        request.run_id,
                        context,
                        step_id=request.step_id,
                    )
                finally:
                    client.close()
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            if not self._runtime.agent_session_is_current(context.session_generation):
                raise HTTPException(status_code=423, detail="Agent run result became stale")
            if action != "status":
                self._runtime.record_agent_run_event(action, run)
            return {"run": run}

        @self.settings_app.post("/api/agent/run/current")
        def agent_run_current(
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            if x_reachy_adult_ui != "unlocked":
                raise HTTPException(status_code=403, detail="An unlocked adult UI action is required")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            context = self._runtime.agent_broker_context(explicit_private_intent=True)
            if context.capability_profile != "agent" or context.kids_mode_active:
                raise HTTPException(status_code=423, detail="Agent run control is unavailable")
            try:
                client = HermesBridgeClient(load_config())
                try:
                    run = client.current_agent_run(context)
                finally:
                    client.close()
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            if not self._runtime.agent_session_is_current(context.session_generation):
                raise HTTPException(status_code=423, detail="Agent run result became stale")
            return {"run": run}

        @self.settings_app.post("/api/agent/run/status")
        def agent_run_status(
            request: AgentRunRequest,
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            return agent_run_action("status", request, x_reachy_adult_ui)

        @self.settings_app.post("/api/agent/run/start")
        def agent_run_start(
            request: AgentRunRequest,
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            return agent_run_action("start", request, x_reachy_adult_ui)

        @self.settings_app.post("/api/agent/run/approve")
        def agent_run_approve(
            request: AgentRunRequest,
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            if not request.step_id:
                raise HTTPException(status_code=422, detail="step_id is required for approval")
            return agent_run_action("approve", request, x_reachy_adult_ui)

        @self.settings_app.post("/api/agent/run/pause")
        def agent_run_pause(
            request: AgentRunRequest,
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            return agent_run_action("pause", request, x_reachy_adult_ui)

        @self.settings_app.post("/api/agent/run/resume")
        def agent_run_resume(
            request: AgentRunRequest,
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            return agent_run_action("resume", request, x_reachy_adult_ui)

        @self.settings_app.post("/api/agent/run/cancel")
        def agent_run_cancel(
            request: AgentRunRequest,
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            return agent_run_action("cancel", request, x_reachy_adult_ui)

        @self.settings_app.post("/api/agent/approve")
        def approve_agent_action(
            request: AgentApprovalRequest,
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            """Approve one exact action body; edits require a new approval."""
            if x_reachy_adult_ui != "unlocked":
                raise HTTPException(status_code=403, detail="An unlocked adult UI action is required")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            context = self._runtime.agent_broker_context(explicit_private_intent=True)
            if context.capability_profile != "agent" or context.kids_mode_active:
                raise HTTPException(status_code=423, detail="Agent approval is unavailable")
            client = HermesBridgeClient(load_config())
            try:
                result = client.approve_agent_action(
                    request.capability_id,
                    request.arguments,
                    context,
                )
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            finally:
                client.close()
            if not self._runtime.agent_session_is_current(context.session_generation):
                raise HTTPException(status_code=423, detail="Agent approval became stale")
            return {
                "ok": True,
                "capability_id": result.capability_id,
                "data": result.data,
                "verified": result.side_effect,
            }

        @self.settings_app.get("/api/agent/pending-approval")
        def pending_agent_approval(
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            if x_reachy_adult_ui != "unlocked":
                raise HTTPException(status_code=403, detail="An unlocked adult UI action is required")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            context = self._runtime.agent_broker_context(explicit_private_intent=True)
            if context.capability_profile != "agent" or context.kids_mode_active:
                raise HTTPException(status_code=423, detail="Agent approval is unavailable")
            client = HermesBridgeClient(load_config())
            try:
                pending = client.pending_agent_approval(context)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            finally:
                client.close()
            if not self._runtime.agent_session_is_current(context.session_generation):
                raise HTTPException(status_code=423, detail="Agent approval became stale")
            return {"pending_approval": pending}

        @self.settings_app.post("/api/agent/approve-pending")
        def approve_pending_agent_action(
            request: AgentPendingApprovalRequest,
            x_reachy_adult_ui: str = Header(default=""),
        ) -> dict[str, object]:
            if x_reachy_adult_ui != "unlocked":
                raise HTTPException(status_code=403, detail="An unlocked adult UI action is required")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            context = self._runtime.agent_broker_context(explicit_private_intent=True)
            if context.capability_profile != "agent" or context.kids_mode_active:
                raise HTTPException(status_code=423, detail="Agent approval is unavailable")
            client = HermesBridgeClient(load_config())
            try:
                result = client.approve_pending_agent_action(request.draft_id, context)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            finally:
                client.close()
            if not self._runtime.agent_session_is_current(context.session_generation):
                raise HTTPException(status_code=423, detail="Agent approval became stale")
            return {"ok": True, "data": result.get("data"), "verified": True}

        @self.settings_app.post("/api/agent/reminder-delivery")
        def deliver_agent_reminder(
            request: AgentReminderDeliveryRequest,
            authorization: str = Header(default=""),
        ) -> dict[str, object]:
            api_key = load_config().api_key
            if not api_key:
                raise HTTPException(status_code=503, detail="Reminder delivery authentication is not configured")
            if not secrets.compare_digest(authorization, f"Bearer {api_key}"):
                raise HTTPException(status_code=401, detail="Unauthorized")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            kids = self._runtime.status().get("kids_mode", {})
            if isinstance(kids, dict) and (kids.get("active") or kids.get("locked")):
                raise HTTPException(status_code=423, detail="Reminder delivery is blocked by Kids Mode")
            try:
                queued = self._runtime.queue_announcement(
                    request.text,
                    behavior="voice_only",
                    repeat=1,
                    pause_seconds=0.0,
                )
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"ok": True, "item_id": request.item_id, "delivery": queued}

        @self.settings_app.post("/api/test-connection")
        def test_connection(update: SettingsUpdate | None = None) -> dict[str, object]:
            try:
                config: AppConfig = load_config()
                if update is not None:
                    config = merge_config(config, update.model_dump(exclude_none=True))
                client = HermesBridgeClient(config)
                try:
                    health = client.health()
                finally:
                    client.close()
                return {"ok": True, "health": health}
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        @self.settings_app.get("/api/models")
        def models() -> dict[str, object]:
            try:
                client = HermesBridgeClient(load_config())
                try:
                    return {"models": client.models(), "health": client.health()}
                finally:
                    client.close()
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        @self.settings_app.get("/api/voice-options")
        def voice_options() -> dict[str, object]:
            try:
                client = HermesBridgeClient(load_config())
                try:
                    return client.voice_options()
                finally:
                    client.close()
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        @self.settings_app.post("/api/camera/test")
        def test_camera(request: ConfirmationRequest) -> dict[str, object]:
            if request.confirm.strip().lower() != "camera":
                raise HTTPException(status_code=400, detail="Confirmation must be 'camera'")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            try:
                return {"ok": True, **self._runtime.test_camera()}
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

        @self.settings_app.post("/api/camera/snapshot")
        def camera_snapshot(
            request: ConfirmationRequest,
            authorization: str = Header(default=""),
        ) -> Response:
            api_key = load_config().api_key
            if not api_key:
                raise HTTPException(status_code=503, detail="Camera snapshot authentication is not configured")
            if not secrets.compare_digest(authorization, f"Bearer {api_key}"):
                raise HTTPException(status_code=401, detail="Unauthorized")
            if request.confirm.strip().lower() != "camera":
                raise HTTPException(status_code=400, detail="Confirmation must be 'camera'")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            try:
                jpeg = self._runtime.camera_snapshot()
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            return Response(
                content=jpeg,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store", "Content-Disposition": "inline"},
            )

        @self.settings_app.post("/mcp", include_in_schema=False)
        async def mcp_endpoint(request: Request) -> Response:
            """Model Context Protocol over Streamable HTTP, answered with plain JSON (stateless)."""
            config = load_config()
            if not config.mcp_enabled or not config.mcp_token_sha256:
                return JSONResponse(status_code=404, content={"detail": "Agent access (MCP) is turned off"})
            origin = request.headers.get("origin", "")
            if origin and not _same_origin(origin, request):
                return JSONResponse(status_code=403, content={"detail": "Cross-origin MCP requests are not allowed"})
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            # Home network only, with the static token. Hosted agents sign in on the public listener.
            if scheme.lower() != "bearer" or not token_matches(token.strip(), config.mcp_token_sha256):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "A valid Homebody MCP token is required"},
                    headers={"WWW-Authenticate": 'Bearer realm="homebody"'},
                )
            return await self._answer_mcp(request, config)

        @self.settings_app.get("/mcp", include_in_schema=False)
        @self.settings_app.delete("/mcp", include_in_schema=False)
        def mcp_no_stream() -> Response:
            # Stateless server: no server-initiated SSE stream and no sessions to delete.
            return Response(status_code=405, headers={"Allow": "POST"})

        @self.settings_app.get("/api/mcp/status")
        def mcp_status() -> dict[str, object]:
            config = load_config()
            return {
                "ok": True,
                "enabled": config.mcp_enabled,
                "vision_enabled": config.mcp_vision_enabled,
                "token_configured": bool(config.mcp_token_sha256),
                "endpoint_path": "/mcp",
                "oauth_enabled": config.mcp_oauth_enabled,
                "public_url": config.mcp_public_url,
                "public_listener": self._agent_listener.status(),
                "oauth": self._oauth.public_status(),
                **self._mcp.status(),
            }

        def _require_owner(provided: str) -> AppConfig:
            current = load_config()
            if current.api_key and not secrets.compare_digest(provided.strip(), current.api_key):
                raise HTTPException(status_code=403, detail="Enter the current API key to manage agent access")
            return current

        @self.settings_app.post("/api/mcp/token")
        def mcp_create_token(request: McpTokenRequest) -> dict[str, object]:
            """Issue a new MCP token (replacing any old one). It is shown once and stored only as a hash."""
            with config_transaction():
                current = _require_owner(request.current_api_key)
                token = new_token()
                try:
                    save_config(merge_config(current, {"mcp_token_sha256": token_digest(token)}))
                except (OSError, ValueError) as exc:
                    raise HTTPException(status_code=500, detail=f"Could not save the token: {exc}") from exc
            return {"ok": True, "token": token, "endpoint_path": "/mcp"}

        @self.settings_app.post("/api/mcp/token/revoke")
        def mcp_revoke_token(request: McpTokenRequest) -> dict[str, object]:
            with config_transaction():
                current = _require_owner(request.current_api_key)
                try:
                    save_config(merge_config(current, {"mcp_token_sha256": ""}))
                except (OSError, ValueError) as exc:
                    raise HTTPException(status_code=500, detail=f"Could not revoke the token: {exc}") from exc
            return {"ok": True, "token_configured": False}

        @self.settings_app.post("/api/mcp/oauth/approve")
        def mcp_oauth_approve(request: McpOAuthDecisionRequest) -> dict[str, object]:
            """Approve one specific hosted-agent request; its consent page then lets the agent continue."""
            config = _require_owner(request.current_api_key)
            if not (config.mcp_enabled and config.mcp_oauth_enabled):
                raise HTTPException(status_code=409, detail="Turn on agent access and agent sign-in first")
            try:
                self._oauth.owner_decide(request.pending_id, approve=True)
            except OAuthError as exc:
                raise HTTPException(status_code=409, detail=exc.description) from exc
            return {"ok": True, **self._oauth.public_status()}

        @self.settings_app.post("/api/mcp/oauth/deny")
        def mcp_oauth_deny(request: McpOAuthDecisionRequest) -> dict[str, object]:
            _require_owner(request.current_api_key)
            try:
                self._oauth.owner_decide(request.pending_id, approve=False)
            except OAuthError as exc:
                raise HTTPException(status_code=409, detail=exc.description) from exc
            return {"ok": True, **self._oauth.public_status()}

        @self.settings_app.post("/api/mcp/oauth/disconnect")
        def mcp_oauth_disconnect(request: McpTokenRequest) -> dict[str, object]:
            _require_owner(request.current_api_key)
            self._oauth.disconnect_all()
            return {"ok": True, **self._oauth.public_status()}

        @self.settings_app.post("/api/vision/describe")
        def describe_camera_view(
            request: VisionQuestionRequest,
            authorization: str = Header(default=""),
        ) -> dict[str, object]:
            # A description reveals what the camera sees, so it needs the same key as a snapshot.
            api_key = load_config().api_key
            if not api_key:
                raise HTTPException(
                    status_code=503,
                    detail="Set a bridge API key before asking about the camera view",
                )
            if not secrets.compare_digest(authorization, f"Bearer {api_key}"):
                raise HTTPException(status_code=401, detail="Enter the bridge API key to ask about the camera view")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            try:
                return {"ok": True, **self._runtime.describe_camera_view(request.question)}
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @self.settings_app.post("/api/vision/test")
        def test_local_vision(request: LocalVisionTestRequest) -> dict[str, object]:
            # Test the values in the form before they are saved.
            updates = {
                key: value
                for key, value in request.model_dump(exclude={"current_api_key"}).items()
                if value is not None
            }
            current = load_config()
            try:
                config = merge_config(current, updates)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            # Probing an unsaved URL makes Reachy fetch from it, so it needs the same key as saving it.
            _authorize_credential_change(current, config, request.current_api_key)
            try:
                with LocalVisionClient(config.local_vision_url, config.local_vision_model, timeout=10.0) as client:
                    return {"ok": True, **client.health()}
            except LocalVisionError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        @self.settings_app.post("/api/announcements")
        def create_announcement(request: AnnouncementRequest) -> dict[str, object]:
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            try:
                return self._runtime.queue_announcement(
                    request.text,
                    provider=request.provider,
                    model=request.model,
                    voice=request.voice,
                    behavior=request.behavior,
                    repeat=request.repeat,
                    pause_seconds=request.pause_seconds,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @self.settings_app.post("/api/announcements/stop")
        def stop_announcements(request: AnnouncementStopRequest) -> dict[str, object]:
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            return self._runtime.stop_announcements(clear_queue=request.clear_queue)

        @self.settings_app.post("/api/kids/start")
        def start_kids_mode(request: KidsModeRequest) -> dict[str, object]:
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            config = load_config()
            if not config.configured:
                raise HTTPException(status_code=409, detail="Configure the Hermes bridge first")
            client = HermesBridgeClient(config)
            try:
                try:
                    health = client.health()
                except Exception as exc:
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
            finally:
                client.close()
            if health.get("kids_chat_available") is not True:
                raise HTTPException(
                    status_code=409,
                    detail="Kids Mode requires the private moderated child bridge route",
                )
            if health.get("kids_tts_streaming_available") is not True:
                raise HTTPException(
                    status_code=409,
                    detail="Kids Mode requires ElevenLabs Flash streaming on the private bridge",
                )
            try:
                profile = KidsProfile(**request.model_dump())
                kids_mode = self._runtime.start_kids_mode(profile)
                # Re-read: `config` was loaded before the bridge health check and Kids start.
                with config_transaction():
                    save_config(merge_config(load_config(), {"capability_profile": "conversation"}))
                return {"ok": True, "kids_mode": kids_mode}
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @self.settings_app.post("/api/kids/stop")
        def stop_kids_mode() -> dict[str, object]:
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            try:
                return {
                    "ok": True,
                    "kids_mode": self._runtime.stop_kids_mode(reason="parent", fold=True),
                }
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @self.settings_app.get("/api/bluetooth/status")
        def bluetooth_status() -> dict[str, object]:
            return {"ok": True, **self._bluetooth.refresh()}

        @self.settings_app.post("/api/bluetooth/scan")
        def bluetooth_scan(request: BluetoothScanRequest) -> dict[str, object]:
            try:
                return {"ok": True, **self._bluetooth.scan(seconds=request.seconds)}
            except (RuntimeError, ValueError) as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

        @self.settings_app.post("/api/bluetooth/pair")
        def bluetooth_pair(request: BluetoothDeviceRequest) -> dict[str, object]:
            try:
                return {"ok": True, **self._bluetooth.pair(request.address)}
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        @self.settings_app.post("/api/bluetooth/connect")
        def bluetooth_connect(request: BluetoothDeviceRequest) -> dict[str, object]:
            try:
                return {"ok": True, **self._bluetooth.connect(request.address)}
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        @self.settings_app.post("/api/bluetooth/disconnect")
        def bluetooth_disconnect(request: BluetoothDeviceRequest) -> dict[str, object]:
            try:
                return {"ok": True, **self._bluetooth.disconnect(request.address)}
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        @self.settings_app.post("/api/bluetooth/remove")
        def bluetooth_remove(request: BluetoothDeviceRequest) -> dict[str, object]:
            try:
                return {"ok": True, **self._bluetooth.remove(request.address)}
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        @self.settings_app.get("/api/gpio/status")
        def gpio_status() -> dict[str, object]:
            try:
                return {"ok": True, **self._gpio_status()}
            except ValueError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc

        @self.settings_app.post("/api/gpio/buttons")
        def gpio_buttons(request: GpioButtonsRequest) -> dict[str, object]:
            updates = {
                "gpio_buttons_enabled": request.enabled,
                "gpio_green_pin": request.green_pin,
                "gpio_red_pin": request.red_pin,
                "gpio_long_press_seconds": request.long_press_seconds,
            }
            with self._gpio_config_lock:
                try:
                    with config_transaction():
                        config = merge_config(load_config(), updates)
                        save_config(config)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                except OSError as exc:
                    raise HTTPException(status_code=500, detail=f"Could not persist button setting: {exc}") from exc
                # Persist first: a reboot then matches what the owner chose, even if the lines are busy now.
                self._start_gpio_buttons(config)
                return {"ok": True, **self._gpio_status(config)}

        @self.settings_app.post("/api/bluetooth/gamepad")
        def bluetooth_gamepad(request: GamepadEnabledRequest) -> dict[str, object]:
            with self._gamepad_config_lock:
                try:
                    if request.enabled:
                        status = self._bluetooth.set_gamepad_enabled(True)
                        try:
                            with config_transaction():
                                save_config(merge_config(load_config(), {"gamepad_enabled": True}))
                        except Exception:
                            self._bluetooth.set_gamepad_enabled(False)
                            raise
                    else:
                        # Persist the fail-safe disabled state before stopping the reader.
                        with config_transaction():
                            save_config(merge_config(load_config(), {"gamepad_enabled": False}))
                        status = self._bluetooth.set_gamepad_enabled(False)
                    return {"ok": True, **status}
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                except RuntimeError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                except OSError as exc:
                    detail = f"Could not persist controller setting: {exc}"
                    raise HTTPException(status_code=500, detail=detail) from exc

        @self.settings_app.get("/api/robot/options")
        def robot_options() -> dict[str, object]:
            return {"ok": True, **robot_control_options()}

        @self.settings_app.post("/api/robot/action")
        def robot_action(request: RobotActionRequest) -> dict[str, object]:
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            try:
                return self._runtime.queue_manual_robot_action(request.action, request.value)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @self.settings_app.get("/api/robot/pose")
        def robot_pose() -> dict[str, object]:
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            try:
                return {"ok": True, "pose": self._runtime.robot_pose()}
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

        @self.settings_app.post("/api/robot/nudge")
        def robot_nudge(request: RobotNudgeRequest) -> dict[str, object]:
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            try:
                return self._runtime.queue_precision_robot_action(request.axis, request.delta)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @self.settings_app.post("/api/camera-control/session")
        def start_camera_control(
            x_reachy_adult_ui: str | None = Header(default=None),
        ) -> dict[str, object]:
            if x_reachy_adult_ui != "unlocked":
                raise HTTPException(status_code=403, detail="An unlocked adult UI action is required")
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            config = load_config()
            try:
                return self._runtime.start_camera_control(
                    camera_feed_enabled=config.camera_feed_enabled,
                    controls_enabled=config.camera_controls_enabled,
                    adult_ui_unlocked=True,
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @self.settings_app.post("/api/camera-control/move")
        def move_camera_control(request: CameraControlMoveRequest) -> dict[str, object]:
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            config = load_config()
            if not config.camera_feed_enabled or not config.camera_controls_enabled:
                self._runtime.revoke_camera_control()
                raise HTTPException(status_code=409, detail="Camera movement controls are disabled")
            try:
                return self._runtime.queue_camera_control(
                    request.session_id,
                    request.sequence,
                    request.pan,
                    request.tilt,
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @self.settings_app.post("/api/camera-control/center")
        def center_camera_control(request: CameraControlSessionRequest) -> dict[str, object]:
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            config = load_config()
            if not config.camera_feed_enabled or not config.camera_controls_enabled:
                self._runtime.revoke_camera_control()
                raise HTTPException(status_code=409, detail="Camera movement controls are disabled")
            try:
                return self._runtime.center_camera_control(request.session_id, request.sequence)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @self.settings_app.post("/api/camera-control/end")
        def end_camera_control(request: CameraControlSessionRequest) -> dict[str, object]:
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            try:
                return self._runtime.end_camera_control(request.session_id, request.sequence)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @self.settings_app.post("/api/robot/stop")
        def stop_robot_action() -> dict[str, object]:
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            try:
                return self._runtime.stop_manual_robot_action()
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @self.settings_app.post("/api/power")
        def power(request: PowerRequest) -> dict[str, object]:
            if self._runtime is None:
                raise HTTPException(status_code=409, detail="Voice runtime has not started")
            if not self._runtime.control_ready:
                raise HTTPException(
                    status_code=409,
                    detail="Voice runtime is still starting; no power transition was attempted",
                )
            try:
                runtime = self._runtime.set_power_mode(
                    request.mode,
                    duration_seconds=request.duration_minutes * 60.0,
                )
                return {"ok": True, "runtime": runtime}
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @self.settings_app.post("/api/app-off")
        def app_off(request: ConfirmationRequest) -> dict[str, object]:
            if request.confirm.strip().lower() != "off":
                raise HTTPException(status_code=400, detail="Confirmation must be 'off'")

            def stop_app() -> None:
                try:
                    # The daemon intentionally holds this response until the app
                    # has exited. Waiting for it from inside the app creates a
                    # shutdown cycle, so dispatch the request and close without
                    # waiting for response headers.
                    with socket.create_connection(("127.0.0.1", 8000), timeout=2.0) as connection:
                        connection.sendall(
                            b"POST /api/apps/stop-current-app HTTP/1.1\r\n"
                            b"Host: 127.0.0.1\r\n"
                            b"Content-Length: 0\r\n"
                            b"Connection: close\r\n\r\n"
                        )
                except Exception:
                    _LOGGER.exception("Could not stop Reachy app")

            timer = threading.Timer(0.4, stop_app)
            timer.daemon = True
            timer.start()
            return {"ok": True, "state": "stopping"}

        @self.settings_app.post("/api/shutdown")
        def shutdown(request: ConfirmationRequest) -> dict[str, object]:
            if request.confirm.strip().lower() != "shutdown":
                raise HTTPException(status_code=400, detail="Confirmation must be 'shutdown'")
            if not host().shutdown_supported:
                raise HTTPException(
                    status_code=409,
                    detail=f"Reachy will not power off this {host().label}; shut it down from the computer itself",
                )
            if self._runtime is not None:
                try:
                    self._runtime.set_power_mode("sleep")
                except RuntimeError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc

            def poweroff() -> None:
                try:
                    subprocess.run(
                        ["sudo", "-n", "systemctl", "poweroff", "--no-wall"],
                        check=True,
                        timeout=10,
                    )
                except Exception:
                    _LOGGER.exception("Could not shut down the Reachy host")

            threading.Timer(0.8, poweroff).start()
            return {"ok": True, "state": "shutting_down"}

    def _build_public_app(self) -> FastAPI:
        """The hosted-agent app served on mcp_public_port; it shares the OAuth store and MCP tools."""
        return build_public_app(
            oauth=self._oauth,
            config_loader=lambda: load_config(),
            answer_mcp=self._answer_mcp,
            same_origin=_same_origin,
        )

    async def _answer_mcp(self, request: Request, config: AppConfig) -> Response:
        """Answer one authorized MCP JSON-RPC request; shared by the home and public listeners."""
        version = request.headers.get("mcp-protocol-version", "")
        if version and version not in PROTOCOL_VERSIONS:
            return JSONResponse(status_code=400, content={"detail": f"Unsupported MCP protocol version {version}"})
        body = await request.body()
        if len(body) > _MCP_MAX_BODY_BYTES:
            return JSONResponse(status_code=413, content={"detail": "MCP request is too large"})
        try:
            message = json.loads(body)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            )
        if isinstance(message, list):
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Batches are not supported"},
                },
            )
        answer = await run_in_threadpool(self._mcp.handle, message, config)
        if answer is None:
            return Response(status_code=202)
        return JSONResponse(answer, headers={"Cache-Control": "no-store"})

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        """Run wake detection, gamepad monitoring, and serialized Hermes voice turns."""
        current = load_config()
        if current.capability_profile != "conversation":
            save_config(merge_config(current, {"capability_profile": "conversation"}))
        audit = AgentAuditLog(default_config_path().with_name("agent-audit.jsonl"))
        self._runtime = HermesVoiceRuntime(
            reachy_mini,
            stop_event,
            agent_audit=audit,
            preferences_path=default_config_path().parent / "initiative-preferences.json",
        )
        try:
            startup_config = load_config()
            if startup_config.gamepad_enabled:
                self._bluetooth.set_gamepad_enabled(True)
            self._start_gpio_buttons(startup_config)
            self._agent_listener.sync(startup_config)
            self._runtime.run()
        finally:
            self._agent_listener.close()
            self._gpio_buttons.close()
            self._bluetooth.close()


def run_cli() -> None:
    """Launch the app outside the daemon for development."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = Homebody()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        app.stop()


if __name__ == "__main__":
    run_cli()
