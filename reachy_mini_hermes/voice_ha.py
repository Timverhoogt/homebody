"""Home Assistant voice front-end: Assist conversations after local wake and HA-owned media playback.

``HomeAssistantVoiceMixin`` is mixed into ``HermesVoiceRuntime`` and uses the runtime's ESPHome bridge,
voice-activity lock, and stop/privacy state.
"""

from __future__ import annotations

import logging
import math
import tempfile
import threading
import time
import uuid
from pathlib import Path

import httpx
import numpy as np

from .config import AppConfig
from .home_assistant import HermesHomeAssistantProvider

# Keep the runtime logger name so existing log filters still match these lines.
_LOGGER = logging.getLogger("reachy_mini_hermes.runtime")

_HA_MEDIA_VOICE_WAIT_SECONDS = 30.0


class HomeAssistantVoiceMixin:
    """Stream locally woken turns through Home Assistant Assist and play bounded HA media."""

    def _home_assistant_provider(self) -> HermesHomeAssistantProvider:
        bridge = self._home_assistant_bridge
        if bridge is None or not bridge.connected:
            raise RuntimeError("Home Assistant is not connected to the Reachy ESPHome bridge")
        provider = bridge.provider
        if not isinstance(provider, HermesHomeAssistantProvider):
            raise RuntimeError("Home Assistant runtime provider is unavailable")
        return provider

    def _stop_home_assistant_voice_if_active(self) -> None:
        bridge = self._home_assistant_bridge
        if bridge is None:
            return
        provider = bridge.provider
        if not isinstance(provider, HermesHomeAssistantProvider):
            return
        if not bool(provider.voice_snapshot().get("active")):
            return
        bridge.stop_voice()
        provider.cancel_voice()

    def _play_home_assistant_media(self, provider: HermesHomeAssistantProvider, url: str) -> None:
        """Download and play one HA-owned audio URL with peer and size checks."""
        validated = provider.validate_media_url(url)
        parsed = httpx.URL(validated)
        suffix = Path(parsed.path).suffix.lower()
        if suffix not in {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac"}:
            suffix = ".audio"
        temporary_path = ""
        maximum_bytes = 15 * 1024 * 1024
        try:
            with tempfile.NamedTemporaryFile(prefix="reachy-ha-", suffix=suffix, delete=False) as temporary:
                temporary_path = temporary.name
                total = 0
                with httpx.stream("GET", validated, timeout=20.0, follow_redirects=True) as response:
                    response.raise_for_status()
                    provider.validate_media_url(str(response.url))
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > maximum_bytes:
                        raise RuntimeError("Home Assistant media exceeds the 15 MB limit")
                    for chunk in response.iter_bytes():
                        if self.stop_event.is_set() or self._privacy_requested.is_set():
                            raise RuntimeError("Home Assistant media playback was cancelled")
                        total += len(chunk)
                        if total > maximum_bytes:
                            raise RuntimeError("Home Assistant media exceeds the 15 MB limit")
                        temporary.write(chunk)
                if total == 0:
                    raise RuntimeError("Home Assistant returned empty media")

            duration = 0.0
            try:
                from mutagen import File as MutagenFile

                metadata = MutagenFile(temporary_path)
                duration = float(metadata.info.length) if metadata is not None and metadata.info is not None else 0.0
            except Exception:
                duration = 0.0
            if not math.isfinite(duration) or duration <= 0:
                duration = min(60.0, max(1.0, total / 32_000.0))
            if duration > 120.0:
                raise RuntimeError("Home Assistant media exceeds the 120 second playback limit")

            if self._motion is not None:
                self._motion.speaking()
            self._set_status("speaking", "Home Assistant is responding")
            self.robot.media.play_sound(temporary_path)
            deadline = time.monotonic() + duration + 0.25
            while time.monotonic() < deadline:
                if (
                    self.stop_event.is_set()
                    or self._conversation_stop_requested.is_set()
                    or self._privacy_requested.is_set()
                    or self._effective_power_mode() in {"meeting", "sleep"}
                ):
                    try:
                        self.robot.media.play_sound(str(self.assets / "silence.wav"))
                    except Exception:
                        pass
                    raise RuntimeError("Home Assistant media playback was cancelled")
                self.stop_event.wait(min(0.05, deadline - time.monotonic()))
        finally:
            if temporary_path:
                Path(temporary_path).unlink(missing_ok=True)

    def queue_home_assistant_media(self, url: str | list[str], *, announcement: bool) -> None:
        """Play an ESPHome media-player request without blocking the protocol loop."""
        provider = self._home_assistant_provider()
        urls = [url] if isinstance(url, str) else list(url)
        if not urls:
            raise ValueError("Home Assistant media playlist is empty")
        for item in urls:
            provider.validate_media_url(item)

        def play() -> None:
            try:
                # Never queue an unbounded number of blocked threads behind a long conversation;
                # media that cannot start soon is stale for Home Assistant anyway.
                if not self._voice_activity_lock.acquire(timeout=_HA_MEDIA_VOICE_WAIT_SECONDS):
                    raise RuntimeError("Home Assistant media was skipped because Reachy stayed busy")
                try:
                    for item in urls:
                        self._play_home_assistant_media(provider, item)
                finally:
                    self._voice_activity_lock.release()
                if announcement and self._home_assistant_bridge is not None:
                    self._home_assistant_bridge.voice_announcement_finished()
            except Exception as exc:
                _LOGGER.warning("Home Assistant media playback failed: %s", exc)
                with self._status_lock:
                    self._status.last_error = str(exc)
            finally:
                if self._motion is not None:
                    self._motion.idle()

        threading.Thread(target=play, name="reachy-ha-media", daemon=True).start()

    def _run_home_assistant_conversation(self, config: AppConfig, wake_word: str) -> None:
        """Stream a locally awakened voice turn through Home Assistant Assist."""
        bridge = self._home_assistant_bridge
        provider = self._home_assistant_provider()
        assert bridge is not None
        conversation_id = str(uuid.uuid4())
        overall_deadline = time.monotonic() + config.conversation_timeout_seconds
        wake_phrase = wake_word or "Hey Hermes"
        follow_up = False

        while not self.stop_event.is_set() and time.monotonic() < overall_deadline:
            provider.begin_voice()
            if not bridge.start_voice(
                wake_word_phrase="" if follow_up else wake_phrase,
                conversation_id=conversation_id,
            ):
                raise RuntimeError("Home Assistant Assist request could not be sent")
            pipeline_deadline = min(overall_deadline, time.monotonic() + 120.0)
            latest: dict[str, object] = {}
            last_stage = ""
            while not self.stop_event.is_set() and time.monotonic() < pipeline_deadline:
                if self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"}:
                    raise RuntimeError("Home Assistant Assist was cancelled by privacy mode")
                latest = provider.wait_voice_update(0.01)
                error = str(latest.get("error") or "")
                if error:
                    raise RuntimeError(error)
                stage = str(latest.get("stage") or "")
                if stage != last_stage and stage == "listening":
                    self._set_status("listening", "Home Assistant Assist is listening")
                    if self._motion is not None:
                        self._motion.listening()
                elif stage != last_stage and stage == "thinking":
                    self._set_status("thinking", "Home Assistant Assist is processing")
                    if self._motion is not None:
                        self._motion.thinking()
                last_stage = stage

                tts_url = str(latest.get("tts_url") or "")
                if tts_url:
                    self._play_home_assistant_media(provider, tts_url)
                    bridge.voice_announcement_finished()
                    provider.voice_playback_finished()
                    latest = provider.voice_snapshot()

                if bool(latest.get("done")):
                    break
                if bool(latest.get("streaming")):
                    frame = self._read_16k_frame()
                    if frame is not None:
                        pcm16 = (np.clip(frame, -1.0, 1.0) * 32767.0).astype("<i2", copy=False).tobytes()
                        if not bridge.send_voice_audio(pcm16):
                            raise RuntimeError("Home Assistant disconnected during Assist audio streaming")
            else:
                raise RuntimeError("Home Assistant Assist pipeline timed out")

            with self._status_lock:
                self._status.turns_completed += 1
            follow_up = bool(latest.get("continue_conversation")) or config.continuous_conversation
            if not follow_up:
                return
            self._set_status("listening", "Home Assistant Assist is waiting for a follow-up")
        if time.monotonic() >= overall_deadline:
            raise RuntimeError("Home Assistant Assist conversation timed out")
