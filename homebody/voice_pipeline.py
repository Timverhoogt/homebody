"""Hermes pipeline voice front-end: endpointed recording, STT, Hermes agent or Kids chat, and TTS playback.

``PipelineVoiceMixin`` is mixed into ``HermesVoiceRuntime`` and uses the runtime's shared audio I/O,
stop generation, and Kids streaming.
"""

from __future__ import annotations

import logging
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from .audio import AdaptiveEndpointRecorder, EndpointResult, encode_wav
from .config import AppConfig
from .hermes_client import HermesBridgeError, SpeechAudio

# Keep the runtime logger name so existing log filters still match these lines.
_LOGGER = logging.getLogger("homebody.runtime")

_MEDIA_TAG = re.compile(r"(?m)^\s*(?:\[\[audio_as_voice\]\]\s*)?MEDIA:\S+\s*$")
_MARKDOWN = re.compile(r"[`*_#>|]+")


class PipelineVoiceMixin:
    """Run continued pipeline conversations and play their responses interruptibly."""

    def _run_conversation(self, initial_config: AppConfig) -> None:
        config = initial_config
        client = self._new_bridge_client(config)
        workspace_generation = None

        def conversation_is_current() -> bool:
            return not initial_config.kids_mode_enabled or self._kids_session_is_current(
                initial_config.kids_session_id
            )

        try:
            health = client.health()
            self._set_status("listening", "Wake word accepted", bridge_healthy=True, last_error="")
            _LOGGER.debug("Hermes bridge health: %s", health.get("status"))

            first_turn = True
            while (
                not self.stop_event.is_set()
                and not self._turn_stop_requested()
                and conversation_is_current()
            ):
                if self._effective_power_mode() in {"meeting", "sleep"}:
                    break
                if not first_turn:
                    config = initial_config if initial_config.kids_mode_enabled else self.config_loader()
                    if not config.continuous_conversation:
                        break
                    self._set_status("listening", "Waiting for a follow-up")
                first_turn = False

                self._play_asset("listening.wav")
                if self._motion is not None:
                    self._motion.listening()
                # Keep draining the microphone while the local earcon plays so
                # its samples cannot become the start of the user's utterance.
                self._discard_audio(0.34)

                workspace_generation = self._workspace_lease()
                self._workspace_event(workspace_generation, "activity", "Listening for your voice")
                endpoint = self._record_utterance(config)
                if not endpoint.speech_detected or endpoint.samples.size == 0:
                    self._set_status("waiting_for_wake_word", "No speech detected")
                    if not config.continuous_conversation:
                        self._signal_error()
                    break

                self._play_asset("processing.wav")
                if self._motion is not None:
                    self._motion.thinking()
                self._workspace_event(workspace_generation, "activity", "Transcribing your voice")
                self._set_status("transcribing", "Command received; transcribing")
                transcript = client.transcribe(encode_wav(endpoint.samples, 16000))
                if not transcript:
                    self._set_status("waiting_for_wake_word", "No speech detected")
                    break
                if (
                    not conversation_is_current()
                    or self.stop_event.is_set()
                    or self._turn_stop_requested()
                    or self._privacy_requested.is_set()
                    or self._effective_power_mode() in {"meeting", "sleep"}
                ):
                    break
                self._workspace_event(workspace_generation, "user", transcript)
                self._workspace_event(workspace_generation, "activity", "Hermes is responding")
                _LOGGER.info("Transcript accepted (%s characters)", len(transcript))
                self._set_status(
                    "thinking",
                    "Hermes is working",
                    transcript=transcript,
                    stt_provider=client.last_stt_provider,
                )

                with self._agent_lock:
                    agent_profile_active = self._capability_profile == "agent"
                if agent_profile_active:
                    request_id, broker_context = self._begin_agent_request(transcript)
                    try:
                        response_text = client.ask_agent(
                            transcript,
                            broker_context,
                            request_id=request_id,
                        )
                    except Exception:
                        self._finish_agent_request(
                            request_id,
                            broker_context.session_generation,
                            succeeded=False,
                        )
                        raise
                    if not self._finish_agent_request(
                        request_id,
                        broker_context.session_generation,
                        succeeded=True,
                    ):
                        break
                else:
                    response_text = client.chat(transcript)
                if (
                    not conversation_is_current()
                    or self.stop_event.is_set()
                    or self._turn_stop_requested()
                    or self._privacy_requested.is_set()
                    or self._effective_power_mode() in {"meeting", "sleep"}
                ):
                    break
                self._workspace_event(workspace_generation, "assistant", response_text)
                self._workspace_event(workspace_generation, "activity", "Preparing the spoken reply")
                preserve_ispy_guess_motion = False
                if (
                    client.config.kids_mode_enabled
                    and client.config.kids_activity == "ispy"
                    and client.last_kids_ispy_role == "player_picker"
                    and client.last_kids_ispy_phase == "awaiting_confirmation"
                ):
                    with self._kids_lock:
                        ispy_generation = self._kids_generation
                    preserve_ispy_guess_motion = self._try_ispy_player_guess_motion(ispy_generation)
                spoken_text = (
                    response_text if client.config.kids_mode_enabled else self._speech_friendly(response_text)
                )
                self._set_status(
                    "synthesizing",
                    "Generating speech",
                    response_preview=response_text[:240],
                )
                if client.config.kids_mode_enabled:
                    try:
                        self._set_status(
                            "speaking",
                            "Streaming ElevenLabs Flash speech",
                            tts_provider="elevenlabs-flash-stream",
                        )
                        interrupted = self._play_kids_stream(
                            client,
                            spoken_text,
                            barge_in=config.barge_in_enabled,
                            animate_motion=not preserve_ispy_guess_motion,
                        )
                    except (HermesBridgeError, httpx.HTTPError):
                        _LOGGER.warning(
                            "Kids low-latency speech stream failed; considering configured TTS fallback",
                            exc_info=True,
                        )
                        self._clear_streamed_audio()
                        if (
                            not conversation_is_current()
                            or self.stop_event.is_set()
                            or self._turn_stop_requested()
                            or self._privacy_requested.is_set()
                            or self._effective_power_mode() in {"meeting", "sleep"}
                        ):
                            break
                        speech = client.synthesize(spoken_text)
                        if (
                            not conversation_is_current()
                            or self.stop_event.is_set()
                            or self._turn_stop_requested()
                            or self._privacy_requested.is_set()
                            or self._effective_power_mode() in {"meeting", "sleep"}
                        ):
                            break
                        self._set_status(
                            "speaking",
                            "Reachy is speaking with fallback audio",
                            tts_provider=speech.provider,
                        )
                        interrupted = self._play_response(
                            speech,
                            spoken_text,
                            barge_in=config.barge_in_enabled,
                            animate_motion=not preserve_ispy_guess_motion,
                        )
                else:
                    speech = client.synthesize(spoken_text)
                    if (
                        self._turn_stop_requested()
                        or self._privacy_requested.is_set()
                        or self._effective_power_mode() in {"meeting", "sleep"}
                    ):
                        break
                    self._workspace_event(workspace_generation, "activity", "Speaking the reply")
                    self._set_status("speaking", "Reachy is speaking", tts_provider=speech.provider)
                    interrupted = self._play_response(
                        speech,
                        spoken_text,
                        barge_in=config.barge_in_enabled,
                    )
                if (
                    not conversation_is_current()
                    or self._turn_stop_requested()
                    or self._effective_power_mode() in {"meeting", "sleep"}
                ):
                    break
                if (
                    not interrupted
                    and client.config.kids_mode_enabled
                    and client.config.kids_activity == "ispy"
                    and client.last_kids_next_action == "prepare_robot_round"
                ):
                    interrupted = self._continue_ispy_reachy_turn(
                        client,
                        barge_in=config.barge_in_enabled,
                    )
                    if (
                        not conversation_is_current()
                        or self._turn_stop_requested()
                        or self._effective_power_mode() in {"meeting", "sleep"}
                    ):
                        break
                self._workspace_event(
                    workspace_generation, "activity",
                    "Reply interrupted" if interrupted else "Voice turn finished",
                )
                with self._status_lock:
                    self._status.turns_completed += 1
                if interrupted:
                    first_turn = False
                    continue
                if not config.continuous_conversation:
                    break
        except HermesBridgeError:
            self._set_status("error", "Hermes bridge request failed", bridge_healthy=False)
            raise
        finally:
            self._workspace_event(workspace_generation, "activity", "Voice session ended")
            client.close()

    def _record_utterance(
        self,
        config: AppConfig,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> EndpointResult:
        recorder = AdaptiveEndpointRecorder(
            initial_timeout=config.initial_speech_timeout_seconds,
            max_duration=config.max_utterance_seconds,
            end_silence=config.end_silence_seconds,
            minimum_rms=config.vad_min_rms,
            noise_multiplier=config.vad_noise_multiplier,
        )
        result = recorder.record(
            self._read_16k_frame,
            noise_floor=self._noise.value,
            should_stop=should_stop or self.stop_event.is_set,
        )
        _LOGGER.info(
            "Speech endpoint: reason=%s speech=%s duration=%.2fs threshold=%.4f",
            result.reason,
            result.speech_detected,
            result.samples.size / 16000.0,
            result.threshold,
        )
        return result

    def _play_response(
        self,
        speech: SpeechAudio,
        text: str,
        *,
        barge_in: bool = True,
        animate_motion: bool = True,
    ) -> bool:
        """Play a response and allow a local wake-phrase barge-in.

        Pipeline mode cannot safely use open-mic RMS detection because Reachy's
        speaker is audible to its microphone. Reusing the local wake spotter
        avoids self-interruption; Realtime mode provides natural semantic VAD.
        """
        suffix = speech.extension if speech.extension.startswith(".") else ".audio"
        with tempfile.NamedTemporaryFile(prefix="homebody-response-", suffix=suffix, delete=False) as output:
            output.write(speech.data)
            path = Path(output.name)
        interrupted = False
        try:
            if (
                self._turn_stop_requested()
                or self._privacy_requested.is_set()
                or self._effective_power_mode() in {"meeting", "sleep"}
            ):
                return False
            duration = self._audio_duration(path, fallback_text=text)
            self._set_status("speaking", "Hermes is speaking")
            if animate_motion and self._motion is not None:
                self._motion.speaking()
            if self._spotter is not None:
                self._spotter.reset()
            self.robot.media.play_sound(str(path))
            deadline = time.monotonic() + duration + 0.15
            while time.monotonic() < deadline and not self.stop_event.is_set():
                if self._turn_stop_requested() or self._effective_power_mode() in {"meeting", "sleep"}:
                    # Response audio is a file played with play_sound; the streaming buffer flush
                    # alone does not stop it, so replace it with silence like every other cancel path.
                    self._clear_streamed_audio()
                    self.robot.media.play_sound(str(self.assets / "silence.wav"))
                    break
                if not barge_in or self._spotter is None:
                    time.sleep(0.02)
                    continue
                frame = self._read_16k_frame()
                if frame is None:
                    continue
                keyword = self._spotter.accept(frame, 16000)
                if not keyword:
                    continue
                interrupted = True
                self.robot.media.play_sound(str(self.assets / "silence.wav"))
                self._spotter.reset()
                with self._status_lock:
                    self._status.interruptions += 1
                self._set_status("listening", "Response interrupted; listening")
                if self._motion is not None:
                    self._motion.listening()
                _LOGGER.info("Playback interrupted by local wake phrase: %s", keyword)
                break
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        return interrupted

    @staticmethod
    def _audio_duration(path: Path, *, fallback_text: str) -> float:
        try:
            from mutagen import File as MutagenFile

            media = MutagenFile(path)
            if media is not None and media.info is not None:
                return max(0.1, min(float(media.info.length), 120.0))
        except Exception:
            _LOGGER.debug("Could not inspect TTS duration", exc_info=True)
        return max(1.0, min(len(fallback_text) / 13.0 + 0.6, 45.0))

    @staticmethod
    def _speech_friendly(text: str) -> str:
        text = _MEDIA_TAG.sub("", text)
        text = _MARKDOWN.sub("", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text or "I completed the request, but there is no spoken response."
