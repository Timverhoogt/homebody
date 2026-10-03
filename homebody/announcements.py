"""Exact-text announcement queue, worker, and playback for the voice runtime.

``AnnouncementsMixin`` is mixed into ``HermesVoiceRuntime`` and keeps its state on the runtime
instance. The worker shares the runtime's voice-activity lock and generation, power transitions,
and status, so announcement ordering against conversations is unchanged.
"""

from __future__ import annotations

import logging
import queue
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from .hermes_client import HermesBridgeClient, SpeechAudio
from .wakeword import WAKE_PROMPT

# Keep the runtime logger name so existing log filters still match announcement lines.
_LOGGER = logging.getLogger("homebody.runtime")

_ANNOUNCEMENT_BEHAVIORS = frozenset({"voice_only", "wake_and_return", "wake_and_stay"})
_ANNOUNCEMENT_QUEUE_LIMIT = 20


@dataclass(frozen=True, slots=True)
class Announcement:
    """One bounded text-to-speech announcement requested by the trusted UI."""

    text: str
    provider: str = ""
    model: str = ""
    voice: str = ""
    behavior: str = "wake_and_return"
    repeat: int = 1
    pause_seconds: float = 1.0
    cancellation_generation: int = 0
    contextual_offer_token: int = 0
    cancel_event: threading.Event = field(default_factory=threading.Event, compare=False, repr=False)


class AnnouncementsMixin:
    """Queue trusted announcements and play them between conversations."""

    def _init_announcement_state(self) -> None:
        self._announcement_queue: queue.Queue[Announcement] = queue.Queue(maxsize=_ANNOUNCEMENT_QUEUE_LIMIT)
        self._announcement_state_lock = threading.RLock()
        self._announcement_cancellation_generation = 0
        self._announcement_current: Announcement | None = None
        self._announcement_active = threading.Event()
        self._announcement_playing = threading.Event()
        self._announcement_worker: threading.Thread | None = None

    def queue_announcement(
        self,
        text: str,
        *,
        provider: str = "",
        model: str = "",
        voice: str = "",
        behavior: str = "wake_and_return",
        repeat: int = 1,
        pause_seconds: float = 1.0,
        contextual_offer_token: int = 0,
    ) -> dict[str, object]:
        """Queue TTS without exposing provider credentials to Reachy or the browser."""
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("Announcement text is required")
        if len(clean_text) > 15_000:
            raise ValueError("Announcement text cannot exceed 15,000 characters")
        provider = provider.strip().lower()
        if provider not in {"", "configured", "elevenlabs"}:
            raise ValueError("Unsupported announcement TTS provider")
        behavior = behavior.strip().lower()
        if behavior not in _ANNOUNCEMENT_BEHAVIORS:
            raise ValueError("Unsupported announcement behavior")
        if isinstance(repeat, bool) or not 1 <= int(repeat) <= 10:
            raise ValueError("Announcement repeat must be between 1 and 10")
        if not 0.0 <= float(pause_seconds) <= 60.0:
            raise ValueError("Announcement pause must be between 0 and 60 seconds")
        if self._effective_power_mode() in {"meeting", "sleep"} or self._privacy_requested.is_set():
            raise RuntimeError("Announcements are blocked in Meeting and Sleep")
        if not self._audio_ready or self._announcement_worker is None:
            raise RuntimeError("Announcement audio is not ready")
        with self._announcement_state_lock:
            generation = self._announcement_cancellation_generation
            self._voice_activity_generation += 1
            item = Announcement(
                text=clean_text,
                provider=provider,
                model=model.strip(),
                voice=voice.strip(),
                behavior=behavior,
                repeat=int(repeat),
                pause_seconds=float(pause_seconds),
                cancellation_generation=generation,
                contextual_offer_token=contextual_offer_token,
            )
            try:
                self._announcement_queue.put_nowait(item)
            except queue.Full as exc:
                raise RuntimeError("Announcement queue is full") from exc
            queue_depth = self._announcement_queue.qsize()
        with self._status_lock:
            self._status.announcement_queue_depth = queue_depth
            self._status.announcement_last_error = ""
        _LOGGER.info("Queued announcement (%s characters, behavior=%s)", len(clean_text), behavior)
        return {"ok": True, "queued": True, "queue_depth": queue_depth}

    def stop_announcements(self, *, clear_queue: bool = True) -> dict[str, object]:
        """Stop announcement audio only; conversational and motor Stop controls remain separate."""
        active, cleared = self._cancel_announcements(clear_queue=clear_queue)
        return {"ok": True, "active_cancelled": active, "queued_cleared": cleared}

    def _cancel_announcements(self, *, clear_queue: bool) -> tuple[bool, int]:
        """Atomically invalidate the current item and optionally every queued item."""
        with self._announcement_state_lock:
            self._announcement_cancellation_generation += 1
            self._voice_activity_generation += 1
            current = self._announcement_current
            active = current is not None
            if current is not None:
                current.cancel_event.set()
                if current.contextual_offer_token:
                    self._contextual_offers.cancel(current.contextual_offer_token, "cancelled")
            cleared = self._clear_announcement_queue_unlocked() if clear_queue else 0
        if self._announcement_playing.is_set():
            try:
                self.robot.media.play_sound(str(self.assets / "silence.wav"))
            except Exception:
                _LOGGER.debug("Could not stop announcement playback with silence asset", exc_info=True)
            self._clear_streamed_audio()
        return active, cleared

    def _clear_announcement_queue(self) -> int:
        with self._announcement_state_lock:
            return self._clear_announcement_queue_unlocked()

    def _clear_announcement_queue_unlocked(self) -> int:
        cleared = 0
        while True:
            try:
                item = self._announcement_queue.get_nowait()
                item.cancel_event.set()
                if item.contextual_offer_token:
                    self._contextual_offers.cancel(item.contextual_offer_token, "cancelled")
                self._announcement_queue.task_done()
                cleared += 1
            except queue.Empty:
                break
        with self._status_lock:
            self._status.announcement_queue_depth = 0
        return cleared

    def _announcement_item_cancelled(self, item: Announcement) -> bool:
        with self._announcement_state_lock:
            return (
                self.stop_event.is_set()
                or item.cancel_event.is_set()
                or item.cancellation_generation != self._announcement_cancellation_generation
                or self._announcement_current is not item
            )

    def _run_announcement_worker(self) -> None:
        """Serialize announcements with conversations and restore the requested physical state."""
        while not self.stop_event.is_set():
            try:
                item = self._announcement_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            with self._announcement_state_lock:
                valid = (
                    not self.stop_event.is_set()
                    and not item.cancel_event.is_set()
                    and item.cancellation_generation == self._announcement_cancellation_generation
                )
                if valid:
                    self._announcement_current = item
                    self._announcement_active.set()
            if not valid:
                self._announcement_queue.task_done()
                continue
            with self._status_lock:
                self._status.announcement_busy = True
                self._status.announcement_queue_depth = self._announcement_queue.qsize()
                self._status.announcement_current_preview = item.text[:240]
                self._status.announcement_last_error = ""
            woke_for_announcement = False
            client: HermesBridgeClient | None = None
            try:
                with self._voice_activity_lock:
                    if self._announcement_item_cancelled(item):
                        continue
                    mode = self._effective_power_mode()
                    if mode in {"meeting", "sleep"} or self._privacy_requested.is_set():
                        raise RuntimeError("Announcement was blocked by the current privacy mode")
                    config = self.config_loader()
                    if item.contextual_offer_token:
                        if not self._contextual_offers.is_queued(item.contextual_offer_token):
                            continue
                        reason = self._presence_suppression_reason(
                            config,
                            owns_voice_activity=True,
                            owns_announcement=True,
                        )
                        with self._agent_lock:
                            agent_active = self._capability_profile == "agent"
                        if reason or not config.contextual_offers_enabled or not agent_active:
                            blocked_reason = reason or (
                                "contextual_offers_disabled"
                                if not config.contextual_offers_enabled
                                else "agent_profile_inactive"
                            )
                            self._contextual_offers.cancel(
                                item.contextual_offer_token,
                                blocked_reason,
                            )
                            continue
                    if not config.configured:
                        raise RuntimeError("Configure the Hermes bridge before making announcements")
                    if item.behavior in {"wake_and_return", "wake_and_stay"} and mode == "standby":
                        self.set_power_mode("awake")
                        woke_for_announcement = True
                    if self._announcement_item_cancelled(item):
                        continue
                    speech_config = replace(
                        config,
                        tts_provider=item.provider or config.tts_provider,
                        tts_model=item.model or config.tts_model,
                        tts_voice=item.voice or config.tts_voice,
                    )
                    client = self._new_bridge_client(speech_config)
                    self._set_status(
                        "announcement_synthesizing",
                        "Generating announcement speech",
                        announcement_current_preview=item.text[:240],
                    )
                    speech = client.synthesize(item.text)
                    for index in range(item.repeat):
                        if self._announcement_item_cancelled(item):
                            break
                        self._set_status(
                            "announcing",
                            f"Playing announcement {index + 1} of {item.repeat}",
                            announcement_provider=speech.provider,
                        )
                        self._play_announcement_audio(item, speech, item.text)
                        if index + 1 < item.repeat and item.cancel_event.wait(item.pause_seconds):
                            break
                    if not self._announcement_item_cancelled(item):
                        with self._status_lock:
                            self._status.announcements_completed += 1
                            self._status.announcement_last_text = item.text[:240]
                        if item.contextual_offer_token:
                            self._capture_contextual_offer_response(item, client, config)
            except Exception as exc:
                _LOGGER.exception("Announcement failed")
                if item.contextual_offer_token:
                    self._contextual_offers.cancel(item.contextual_offer_token, "speech_failed")
                with self._status_lock:
                    self._status.announcement_last_error = str(exc)
            finally:
                if client is not None:
                    client.close()
                with self._motor_transition_lock:
                    with self._announcement_state_lock:
                        owns_transition = (
                            self._announcement_current is item
                            and item.cancellation_generation == self._announcement_cancellation_generation
                            and not item.cancel_event.is_set()
                        )
                    if (
                        owns_transition
                        and woke_for_announcement
                        and item.behavior == "wake_and_return"
                        and not self.stop_event.is_set()
                        and self._effective_power_mode() == "awake"
                        and not self._privacy_requested.is_set()
                    ):
                        try:
                            self.set_power_mode("standby", cancel_announcements=False)
                        except RuntimeError as exc:
                            with self._status_lock:
                                self._status.announcement_last_error = str(exc)
                with self._announcement_state_lock:
                    if self._announcement_current is item:
                        self._announcement_current = None
                    self._announcement_active.clear()
                self._announcement_queue.task_done()
                with self._status_lock:
                    self._status.announcement_busy = False
                    self._status.announcement_queue_depth = self._announcement_queue.qsize()
                    self._status.announcement_current_preview = ""
                if (
                    item.contextual_offer_token
                    and not self.stop_event.is_set()
                    and not self._privacy_requested.is_set()
                    and self._effective_power_mode() == "awake"
                ):
                    self._set_status("waiting_for_wake_word", WAKE_PROMPT)

    def _play_announcement_audio(self, item: Announcement, speech: SpeechAudio, text: str) -> None:
        suffix = speech.extension if speech.extension.startswith(".") else ".audio"
        with tempfile.NamedTemporaryFile(prefix="homebody-announcement-", suffix=suffix, delete=False) as output:
            output.write(speech.data)
            path = Path(output.name)
        try:
            if self._announcement_item_cancelled(item):
                return
            duration = self._announcement_audio_duration(path, fallback_text=text)
            if self._motion is not None and self._motors_enabled is True:
                self._motion.speaking()
            self._announcement_playing.set()
            self.robot.media.play_sound(str(path))
            deadline = time.monotonic() + duration + 0.25
            while time.monotonic() < deadline and not self.stop_event.is_set():
                if item.cancel_event.wait(0.02) or self._announcement_item_cancelled(item):
                    self.robot.media.play_sound(str(self.assets / "silence.wav"))
                    self._clear_streamed_audio()
                    break
                if self._effective_power_mode() in {"meeting", "sleep"}:
                    break
            else:
                # Reachy's player is asynchronous. Explicitly flush at the measured
                # end so ownership cannot be released while old audio remains queued.
                self.robot.media.play_sound(str(self.assets / "silence.wav"))
                self._clear_streamed_audio()
        finally:
            self._announcement_playing.clear()
            try:
                path.unlink()
            except OSError:
                pass

    @staticmethod
    def _announcement_audio_duration(path: Path, *, fallback_text: str) -> float:
        """Return full announcement duration, bounded only by a hard 30-minute safety ceiling."""
        try:
            from mutagen import File as MutagenFile

            media = MutagenFile(path)
            if media is not None and media.info is not None:
                return max(0.1, min(float(media.info.length), 1800.0))
        except Exception:
            _LOGGER.debug("Could not inspect announcement TTS duration", exc_info=True)
        return max(1.0, min(len(fallback_text) / 13.0 + 0.6, 1800.0))
