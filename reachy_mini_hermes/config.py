"""Persistent configuration for the Reachy Mini Hermes app."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import urlparse

_CONFIG_LOCK = threading.RLock()
_CONFIG_ENV = "REACHY_MINI_HERMES_CONFIG"


def default_config_path() -> Path:
    """Return a user-writable path that survives package upgrades."""
    override = os.getenv(_CONFIG_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "reachy_mini_hermes" / "config.json"


@dataclass(slots=True)
class AppConfig:
    """Runtime settings. Secrets are only exposed through ``redacted_dict``."""

    bridge_url: str = "http://127.0.0.1:8643"
    api_key: str = ""
    model: str = "hermes-agent"
    conversation_mode: str = "pipeline"
    capability_profile: str = "conversation"
    language: str = "en"
    stt_provider: str = "configured"
    stt_model: str = "base"
    tts_provider: str = "configured"
    tts_model: str = "eleven_flash_v2_5"
    tts_voice: str = "pNInz6obpgDQGcFmaJgB"
    system_prompt: str = (
        "You are speaking through a Reachy Mini robot. Reply naturally and concisely for speech. "
        "Avoid Markdown tables, long lists, file paths, and MEDIA tags unless the user asks for them."
    )
    continuous_conversation: bool = False
    conversation_timeout_seconds: float = 300.0
    initial_speech_timeout_seconds: float = 5.0
    max_utterance_seconds: float = 20.0
    end_silence_seconds: float = 0.8
    vad_min_rms: float = 0.012
    vad_noise_multiplier: float = 3.0
    wake_keyword_score: float = 1.5
    wake_keyword_threshold: float = 0.25
    wake_cooldown_seconds: float = 2.0
    motion_enabled: bool = True
    barge_in_enabled: bool = True
    camera_enabled: bool = False
    camera_feed_enabled: bool = False
    camera_controls_enabled: bool = False
    camera_controls_handedness: str = "right"
    face_tracking_enabled: bool = False
    face_tracking_weight: float = 0.65
    gesture_detection_enabled: bool = False
    doa_enabled: bool = False
    proactive_presence_enabled: bool = False
    presence_acknowledgement_enabled: bool = True
    presence_acknowledgement_cooldown_seconds: float = 120.0
    initiative_policy_enabled: bool = False
    initiative_mode: str = "quiet"
    initiative_quiet_hours_enabled: bool = True
    initiative_quiet_hours_start: str = "22:00"
    initiative_quiet_hours_end: str = "07:00"
    initiative_hourly_budget: int = 2
    initiative_daily_budget: int = 6
    initiative_topic_cooldown_seconds: float = 1800.0
    initiative_duplicate_window_seconds: float = 300.0
    initiative_dismissal_backoff_seconds: float = 3600.0
    contextual_offers_enabled: bool = False
    contextual_offer_response_window_seconds: float = 10.0
    shared_physical_context_enabled: bool = False
    presentation_window_seconds: float = 20.0
    robot_tools_enabled: bool = True
    home_assistant_enabled: bool = False
    home_assistant_controls_enabled: bool = False
    home_assistant_camera_enabled: bool = False
    home_assistant_assist_enabled: bool = False
    home_assistant_port: int = 6053
    gamepad_enabled: bool = False
    gpio_buttons_enabled: bool = False
    gpio_chip: str = "/dev/gpiochip0"
    gpio_green_pin: int | None = 17
    gpio_red_pin: int | None = None
    gpio_long_press_seconds: float = 2.0
    local_ai_accelerator: str = "auto"
    local_vision_enabled: bool = False
    local_vision_url: str = "http://127.0.0.1:11434/v1"
    local_vision_model: str = "qwen2.5vl:3b"
    agent_tools_enabled: bool = True
    power_tools_enabled: bool = True
    kids_mode_enabled: bool = False
    kids_session_id: str = ""
    kids_age_band: str = ""
    kids_activity: str = ""
    realtime_model: str = "gpt-realtime-2.1"
    realtime_voice: str = "marin"
    realtime_reasoning_effort: str = "low"
    instance_id: str = ""

    def __post_init__(self) -> None:
        self.bridge_url = self.bridge_url.strip().rstrip("/")
        self.api_key = self.api_key.strip()
        self.model = self.model.strip() or "hermes-agent"
        self.conversation_mode = self.conversation_mode.strip().lower() or "pipeline"
        self.capability_profile = self.capability_profile.strip().lower() or "conversation"
        self.language = self.language.strip() or "en"
        self.stt_provider = self.stt_provider.strip().lower() or "configured"
        self.stt_model = self.stt_model.strip() or "base"
        self.tts_provider = self.tts_provider.strip().lower() or "configured"
        self.tts_model = self.tts_model.strip() or "eleven_flash_v2_5"
        self.tts_voice = self.tts_voice.strip() or "pNInz6obpgDQGcFmaJgB"
        self.realtime_model = self.realtime_model.strip() or "gpt-realtime-2.1"
        self.realtime_voice = self.realtime_voice.strip() or "marin"
        self.realtime_reasoning_effort = self.realtime_reasoning_effort.strip().lower() or "low"
        self.camera_controls_handedness = self.camera_controls_handedness.strip().lower() or "right"
        self.local_vision_url = self.local_vision_url.strip().rstrip("/")
        self.local_vision_model = self.local_vision_model.strip()
        self.kids_session_id = self.kids_session_id.strip()
        self.kids_age_band = self.kids_age_band.strip()
        self.kids_activity = self.kids_activity.strip()
        if not self.instance_id:
            self.instance_id = uuid.uuid4().hex
        self.validate()

    def validate(self) -> None:
        parsed = urlparse(self.bridge_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("bridge_url must be an absolute http(s) URL")
        if not 0.01 <= float(self.wake_keyword_threshold) <= 1.0:
            raise ValueError("wake_keyword_threshold must be between 0.01 and 1.0")
        if not 0.1 <= float(self.end_silence_seconds) <= 5.0:
            raise ValueError("end_silence_seconds must be between 0.1 and 5.0")
        if not 1.0 <= float(self.max_utterance_seconds) <= 120.0:
            raise ValueError("max_utterance_seconds must be between 1 and 120")
        if not 1.0 <= float(self.initial_speech_timeout_seconds) <= 30.0:
            raise ValueError("initial_speech_timeout_seconds must be between 1 and 30")
        if not 0.001 <= float(self.vad_min_rms) <= 0.5:
            raise ValueError("vad_min_rms must be between 0.001 and 0.5")
        if not 0.0 <= float(self.face_tracking_weight) <= 1.0:
            raise ValueError("face_tracking_weight must be between 0 and 1")
        if not 30.0 <= float(self.presence_acknowledgement_cooldown_seconds) <= 3600.0:
            raise ValueError("presence acknowledgement cooldown must be between 30 and 3600 seconds")
        if self.initiative_mode not in {"quiet", "balanced", "engaged"}:
            raise ValueError("Unsupported initiative mode")
        for value in (self.initiative_quiet_hours_start, self.initiative_quiet_hours_end):
            try:
                hour_text, minute_text = value.split(":", 1)
                valid_time = (
                    len(hour_text) == 2
                    and len(minute_text) == 2
                    and 0 <= int(hour_text) <= 23
                    and 0 <= int(minute_text) <= 59
                )
            except (AttributeError, TypeError, ValueError):
                valid_time = False
            if not valid_time:
                raise ValueError("Initiative quiet hours must use valid HH:MM values")
        if not 1 <= int(self.initiative_hourly_budget) <= 10:
            raise ValueError("Initiative hourly budget must be between 1 and 10")
        if not 1 <= int(self.initiative_daily_budget) <= 30:
            raise ValueError("Initiative daily budget must be between 1 and 30")
        if not 60.0 <= float(self.initiative_topic_cooldown_seconds) <= 86400.0:
            raise ValueError("Initiative topic cooldown must be between 60 and 86400 seconds")
        if not 30.0 <= float(self.initiative_duplicate_window_seconds) <= 3600.0:
            raise ValueError("Initiative duplicate window must be between 30 and 3600 seconds")
        if not 60.0 <= float(self.initiative_dismissal_backoff_seconds) <= 86400.0:
            raise ValueError("Initiative dismissal backoff must be between 60 and 86400 seconds")
        if not 5.0 <= float(self.contextual_offer_response_window_seconds) <= 30.0:
            raise ValueError("Contextual offer response window must be between 5 and 30 seconds")
        if not 5.0 <= float(self.presentation_window_seconds) <= 30.0:
            raise ValueError("Presentation window must be between 5 and 30 seconds")
        if self.stt_provider not in {"configured", "local", "elevenlabs"}:
            raise ValueError("Unsupported STT provider")
        if self.tts_provider not in {"configured", "elevenlabs"}:
            raise ValueError("Unsupported TTS provider")
        if self.conversation_mode not in {"pipeline", "realtime"}:
            raise ValueError("Unsupported conversation mode")
        if self.capability_profile not in {"conversation", "agent"}:
            raise ValueError("Unsupported capability profile")
        if self.realtime_reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError("Unsupported realtime reasoning effort")
        if self.camera_controls_handedness not in {"left", "right"}:
            raise ValueError("Unsupported camera control handedness")
        if not 1024 <= int(self.home_assistant_port) <= 65535:
            raise ValueError("home_assistant_port must be between 1024 and 65535")
        if not re.fullmatch(r"/dev/gpiochip[0-9]{1,2}", str(self.gpio_chip)):
            raise ValueError("gpio_chip must be a /dev/gpiochipN device")
        pins = [pin for pin in (self.gpio_green_pin, self.gpio_red_pin) if pin is not None]
        # Raspberry Pi header pins are BCM 0-27; Jetson header lines sit at higher chip offsets.
        if any(isinstance(pin, bool) or not isinstance(pin, int) or not 0 <= pin <= 1023 for pin in pins):
            raise ValueError("GPIO button pins must be line numbers between 0 and 1023")
        if len(set(pins)) != len(pins):
            raise ValueError("The green and red buttons need different GPIO pins")
        vision = urlparse(str(self.local_vision_url))
        if vision.scheme not in {"http", "https"} or not vision.netloc:
            raise ValueError("local_vision_url must be an absolute http(s) URL")
        if not 1 <= len(str(self.local_vision_model).strip()) <= 200:
            raise ValueError("local_vision_model must be 1-200 characters")
        if self.local_ai_accelerator not in {"auto", "cpu"}:
            raise ValueError("local_ai_accelerator must be 'auto' or 'cpu'")
        if not 0.5 <= float(self.gpio_long_press_seconds) <= 10.0:
            raise ValueError("gpio_long_press_seconds must be between 0.5 and 10")
        if self.home_assistant_assist_enabled and not self.home_assistant_enabled:
            raise ValueError("Home Assistant Assist requires the Home Assistant bridge")
        if self.home_assistant_camera_enabled and not self.home_assistant_enabled:
            raise ValueError("Home Assistant camera requires the Home Assistant bridge")
        if self.home_assistant_controls_enabled and not self.home_assistant_enabled:
            raise ValueError("Home Assistant controls require the Home Assistant bridge")
        if self.kids_mode_enabled:
            if self.kids_age_band not in {"4-6", "7-9", "10-12"}:
                raise ValueError("Unsupported Kids Mode age band")
            if self.kids_activity not in {"buddy", "story", "quiz", "riddles", "calm", "ispy"}:
                raise ValueError("Unsupported Kids Mode activity")

    @property
    def configured(self) -> bool:
        return bool(self.bridge_url and self.api_key)

    def child_status_dict(self) -> dict[str, object]:
        """Return only non-sensitive readiness flags during an active Kids session."""
        return {
            "configured": self.configured,
            "api_key_configured": bool(self.api_key),
        }

    def redacted_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["api_key"] = "********" if self.api_key else ""
        payload["api_key_configured"] = bool(self.api_key)
        payload.pop("kids_session_id", None)
        payload.pop("kids_mode_enabled", None)
        payload.pop("kids_age_band", None)
        payload.pop("kids_activity", None)
        return payload


@contextmanager
def config_transaction() -> Iterator[None]:
    """Hold the config lock across a whole load -> merge -> save so concurrent writers cannot
    overwrite each other's changes. load_config and save_config may be called inside."""
    with _CONFIG_LOCK:
        yield


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or default_config_path()
    with _CONFIG_LOCK:
        if not config_path.exists():
            return AppConfig()
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("configuration root must be an object")
            allowed = {f.name for f in fields(AppConfig)}
            return AppConfig(**{key: value for key, value in payload.items() if key in allowed})
        except Exception as exc:
            raise ValueError(f"Could not load {config_path}: {exc}") from exc


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    config.validate()
    config_path = path or default_config_path()
    with _CONFIG_LOCK:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = config_path.with_suffix(config_path.suffix + ".tmp")
        temporary.write_text(json.dumps(asdict(config), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(config_path)
        os.chmod(config_path, 0o600)
    return config_path


def merge_config(current: AppConfig, updates: dict[str, object]) -> AppConfig:
    """Merge settings while treating a masked API key as "keep existing"."""
    allowed = {f.name for f in fields(AppConfig)}
    payload = asdict(current)
    for key, value in updates.items():
        if key not in allowed or key == "instance_id":
            continue
        if key == "api_key" and isinstance(value, str) and (not value or set(value) == {"*"}):
            continue
        payload[key] = value
    return AppConfig(**payload)
