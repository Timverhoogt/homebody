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

from .audio import AdaptiveEndpointRecorder, EndpointResult, resample_linear
from .config import AppConfig
from .hermes_client import HermesBridgeError, SpeechAudio
from .kids_acknowledgements import KidsAckPlayer, eligible_clips, output_quarantined
from .kids_acknowledgements import catalog as kids_ack_catalog
from .robot_stt import RobotSttClient
from .stt_routing import (
    SttInfrastructureError,
    SttResult,
    SttTurnCancelled,
    receipt_locality,
    transcribe_endpoint,
)

__all__ = ["PipelineVoiceMixin", "receipt_locality", "stt_locality"]

# Keep the runtime logger name so existing log filters still match these lines.
_LOGGER = logging.getLogger("homebody.runtime")

_MEDIA_TAG = re.compile(r"(?m)^\s*(?:\[\[audio_as_voice\]\]\s*)?MEDIA:\S+\s*$")
_MARKDOWN = re.compile(r"[`*_#>|]+")
# Consecutive silent/empty Kids turns that re-open the microphone before returning to wake.
_KIDS_EMPTY_TURN_RETRIES = 2
_STT_LOCALITIES = frozenset({"local", "cloud"})


def kids_ack_voice_proof(health: dict[str, object], language: str) -> tuple[object, object]:
    """Choose the immutable turn language's private proof; malformed/missing entries fail closed."""
    if "kids_voice_proofs" in health:
        proofs = health["kids_voice_proofs"]
        proof = proofs.get(language) if isinstance(proofs, dict) else None
        proof = proof if isinstance(proof, dict) else {}
    else:
        proof = health  # legacy bridges have one voice for both languages
    return proof.get("kids_voice_sha256"), proof.get("kids_tts_model")


def stt_locality(config: AppConfig, health: dict[str, object]) -> str:
    """Categorical STT location for this turn: 'local', 'cloud' or 'unknown'.

    ElevenLabs is always a cloud transcription. The robot route never borrows the host bridge's
    health claim; only its own per-turn receipt sets a location. Otherwise the bridge uses
    Hermes' configured provider, so only the bridge's server-derived health can confirm 'local'.
    """
    if config.conversation_mode != "pipeline" and not config.kids_mode_enabled:
        return "unknown"
    if config.stt_provider == "elevenlabs":
        return "cloud"
    if config.stt_provider == "robot":
        return "unknown"
    value = health.get("stt_locality") if isinstance(health, dict) else None
    return value if value in _STT_LOCALITIES else "unknown"


class PipelineVoiceMixin:
    """Run continued pipeline conversations and play their responses interruptibly."""

    def _run_conversation(self, initial_config: AppConfig, *, typed_input: str | None = None) -> None:
        if typed_input is not None and not (
            initial_config.kids_mode_enabled
            and (initial_config.kids_activity == "ispy" or initial_config.kids_host_enabled)
        ):
            raise ValueError("Typed input is limited to an active Kids I Spy game or play session")
        if initial_config.kids_mode_enabled and (
            initial_config.kids_activity == "ispy" or initial_config.kids_host_enabled
        ):
            self._invalidate_ispy_input()
        config = initial_config
        client = self._new_bridge_client(config)
        workspace_generation = None

        def conversation_is_current() -> bool:
            current = not initial_config.kids_mode_enabled or self._kids_session_is_current(
                initial_config.kids_session_id
            )
            return current and (
                typed_input is None or self._ispy_typed_session_is_current(initial_config.kids_session_id)
            )

        kids_misses = 0

        def kids_retry_allowed() -> bool:
            """Give a child a bounded re-listen after silence or an empty transcript."""
            nonlocal kids_misses
            if not initial_config.kids_mode_enabled or typed_input is not None:
                return False
            kids_misses += 1
            if kids_misses > _KIDS_EMPTY_TURN_RETRIES or not conversation_is_current():
                return False
            if (
                self.stop_event.is_set()
                or self._turn_stop_requested()
                or self._privacy_requested.is_set()
                or self._effective_power_mode() in {"meeting", "sleep"}
            ):
                return False
            _LOGGER.info("Kids turn heard nothing usable; listening again (%s/%s)", kids_misses,
                         _KIDS_EMPTY_TURN_RETRIES)
            self._set_status("listening", "Didn't catch that; listening again")
            return True

        ack_player: KidsAckPlayer | None = None

        def settle_ack() -> bool:
            """Quiesce the filler; False means audio output is quarantined (fail closed)."""
            nonlocal ack_player
            quiet = True
            if ack_player is not None:
                quiet = ack_player.settle()
                ack_player = None
            if quiet and not self._ack_output_quarantined():
                return True
            self._set_status("waiting_for_wake_word", "Audio output is busy; ending this turn")
            return False

        def set_locality(value: str) -> None:
            with self._status_lock:
                self._status.stt_locality = value

        def set_processing(location: str, verified: bool) -> None:
            with self._status_lock:
                self._status.stt_processing_location = location
                self._status.stt_processing_verified = verified

        try:
            health = client.health()
            if not isinstance(health, dict):
                health = {}
            # Per-conversation proofs; RAM only. The voice digest never enters public status.
            voice_proof = kids_ack_voice_proof(health, initial_config.language)
            locality = stt_locality(config, health)
            locality_provider = config.stt_provider
            stt_turns = 0
            self._set_status(
                "listening", "Wake word accepted", bridge_healthy=True, last_error="",
                stt_locality=locality,
            )
            _LOGGER.debug("Hermes bridge health: %s", health.get("status"))

            first_turn = True
            turns_started = 0
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

                stt_result: SttResult | None = None
                if typed_input is not None:
                    transcript = typed_input
                    workspace_generation = None
                else:
                    if self._ack_output_quarantined():
                        # An old filler push is still blocked in the SDK: no new microphone turn.
                        self._set_status("waiting_for_wake_word", "Audio output is busy; ending this turn")
                        break
                    self._play_asset("listening.wav")
                    if self._motion is not None:
                        self._motion.listening()
                    # Keep draining the microphone while the local earcon plays so
                    # its samples cannot become the start of the user's utterance.
                    self._discard_audio(0.34)

                    workspace_generation = self._workspace_lease()
                    self._workspace_event(workspace_generation, "activity", "Listening for your voice")
                    with self._agent_lock:
                        timing_mode = "workspace" if self._capability_profile == "agent" else "everyday"
                    self._voice_timings.begin("pipeline", timing_mode, warm=turns_started > 0)
                    turns_started += 1
                    endpoint = self._record_utterance(config)
                    # local_speech_end keeps its historical meaning (endpoint returned);
                    # speech_last_voiced is the actual last voiced frame, endpoint_final the decision.
                    self._voice_timings.mark("local_speech_end")
                    self._voice_timings.mark("endpoint_final")
                    last_voiced_at = getattr(endpoint, "last_voiced_at", None)
                    if last_voiced_at is not None:
                        self._voice_timings.mark("speech_last_voiced", at=last_voiced_at)
                    if not endpoint.speech_detected or endpoint.samples.size == 0:
                        if kids_retry_allowed():
                            continue
                        self._set_status("waiting_for_wake_word", "No speech detected")
                        if not config.continuous_conversation:
                            self._signal_error()
                        break

                    # Kids already have thinking motion and delayed cached acknowledgement.
                    # Do not wait for the full processing cue before submitting their audio.
                    if not config.kids_mode_enabled:
                        self._play_asset("processing.wav")
                    if self._motion is not None:
                        self._motion.thinking()
                    self._workspace_event(workspace_generation, "activity", "Transcribing your voice")
                    if config.stt_provider != locality_provider:
                        # Provider selection changed: drop any older local claim until a receipt.
                        locality = "cloud" if config.stt_provider == "elevenlabs" else "unknown"
                        locality_provider = config.stt_provider
                    if config.stt_provider == "robot":
                        locality = "unknown"  # never a previous turn's or the host's claim
                    set_processing("unknown", False)
                    self._set_status("transcribing", "Command received; transcribing", stt_locality=locality)
                    # A cached Kids clip may play only if STT is still pending after a pause.
                    # Turn 1 uses the proof sampled with this conversation's health; every later
                    # turn re-proves the effective Kids voice from the bridge before any clip.
                    if stt_turns == 0:
                        ack_player = self._start_kids_ack(initial_config, voice_proof)
                    else:
                        refresh = getattr(client, "kids_voice_proof", None)
                        ack_player = (
                            self._start_kids_ack(initial_config, refresh=refresh) if callable(refresh) else None
                        )
                    stt_turns += 1
                    stt_generation, stt_is_current = self._stt_turn_fence(initial_config.kids_session_id)

                    def show_location(location: str, verified: bool) -> None:
                        set_processing(location, verified)
                        if not verified:
                            where = "on Reachy" if location == "robot" else "on the Hermes host"
                            self._set_status("transcribing", f"Transcribing {where}")

                    try:
                        stt_result = transcribe_endpoint(
                            self, client, config, endpoint.samples,
                            is_current=lambda fence=stt_is_current: fence() and conversation_is_current(),
                            session_id=initial_config.kids_session_id if initial_config.kids_mode_enabled else "",
                            generation=stt_generation,
                            on_location=show_location,
                            on_submitted=lambda: self._voice_timings.mark("stt_submitted"),
                        )
                    except (SttTurnCancelled, SttInfrastructureError) as exc:
                        locality = "unknown"
                        set_locality(locality)
                        set_processing("unknown", False)
                        if not settle_ack():
                            break
                        if isinstance(exc, SttInfrastructureError):
                            # Infrastructure failure is not silence: no retry loop, no reload.
                            _LOGGER.warning("Speech recognition unavailable (%s)", exc.category)
                            notice = (
                                "On-device speech recognition is not set up on Reachy yet"
                                if exc.category == "unprovisioned"
                                else "Speech recognition on Reachy is unavailable"
                            )
                            self._set_status("waiting_for_wake_word", notice, last_error=notice)
                            self._signal_error()
                        break
                    except BaseException:
                        locality = "unknown"
                        set_locality(locality)
                        set_processing("unknown", False)
                        raise
                    finally:
                        if ack_player is not None:
                            ack_player.stt_finished()
                    self._voice_timings.mark("stt_final")
                    transcript = stt_result.text
                    # The latest actual receipt wins over the conversation-start health snapshot;
                    # a missing receipt never keeps an older "local" claim.
                    locality = stt_result.locality
                    set_processing(stt_result.processing_location, stt_result.processing_location != "unknown")
                    if not transcript:
                        if not settle_ack():  # never re-open the microphone over the filler
                            break
                        if kids_retry_allowed():
                            continue
                        self._set_status("waiting_for_wake_word", "No speech detected")
                        break
                    kids_misses = 0
                if (
                    not conversation_is_current()
                    or self.stop_event.is_set()
                    or self._turn_stop_requested()
                    or self._privacy_requested.is_set()
                    or self._effective_power_mode() in {"meeting", "sleep"}
                ):
                    break
                if initial_config.kids_host_enabled and self._kids_host_handle_transcript(
                    initial_config.kids_session_id, transcript, typed=typed_input is not None,
                ):
                    # The game master answers with its own fixed line and re-opens listening.
                    self._set_status("waiting_for_wake_word", "Game host is responding")
                    with self._status_lock:
                        self._status.turns_completed += 1
                    break
                with self._agent_lock:
                    agent_profile_active = self._capability_profile == "agent"
                # Name the route actually taken below; Everyday never reaches the full Hermes agent.
                self._workspace_event(workspace_generation, "user", transcript)
                self._workspace_event(
                    workspace_generation, "activity",
                    "Workspace is responding" if agent_profile_active else "Everyday is responding",
                )
                _LOGGER.info("Transcript accepted (%s characters)", len(transcript))
                self._set_status(
                    "thinking",
                    "Homebody is working",
                    transcript=transcript,
                    stt_provider=(
                        "robot" if stt_result is not None and stt_result.processing_location == "robot"
                        else client.last_stt_provider
                    ),
                )

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
                elif client.config.kids_mode_enabled:
                    response_text = client.chat(transcript)  # dedicated Kids route, unchanged
                else:
                    # Everyday: the narrow fast backend, never the full Hermes agent.
                    response_text = client.everyday_turn(transcript)
                self._voice_timings.mark("tool_result")  # backend text returned (not "heard")
                if (
                    not conversation_is_current()
                    or self.stop_event.is_set()
                    or self._turn_stop_requested()
                    or self._privacy_requested.is_set()
                    or self._effective_power_mode() in {"meeting", "sleep"}
                ):
                    break
                self._workspace_event(workspace_generation, "assistant", response_text)
                source = getattr(client, "last_answer_source", None) or {}
                if source.get("kind") == "native_hermes":
                    self._workspace_event(
                        workspace_generation, "activity",
                        f"Answered in your Hermes session {source.get('session_id', '')} "
                        f"(run {source.get('run_id', '')})",
                    )
                elif source.get("kind") == "restricted_broker":
                    self._workspace_event(
                        workspace_generation, "activity",
                        "Answered by the restricted roadmap reader — not your Hermes session",
                    )
                self._workspace_event(workspace_generation, "activity", "Preparing the spoken reply")
                if not settle_ack():  # the moderated answer never overlaps a cached clip
                    break
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
                    self._voice_timings.mark("first_provider_audio")
                    if (
                        self._turn_stop_requested()
                        or self._privacy_requested.is_set()
                        or self._effective_power_mode() in {"meeting", "sleep"}
                    ):
                        break
                    self._voice_timings.mark("player_enqueued")
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
                    and client.config.kids_host_enabled
                    and self._kids_host_after_reply(
                        client.config.kids_session_id, getattr(client, "last_kids_game_state", ""),
                    )
                ):
                    # A real finished outcome: the host offers two choices instead of continuing.
                    with self._status_lock:
                        self._status.turns_completed += 1
                    break
                if (
                    not interrupted
                    and client.config.kids_mode_enabled
                    and not client.config.kids_host_enabled
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
                if typed_input is not None:
                    break  # One submitted guess; never open the microphone for a follow-up.
                if interrupted:
                    first_turn = False
                    continue
                if not config.continuous_conversation:
                    break
        except HermesBridgeError:
            self._set_status("error", "Hermes bridge request failed", bridge_healthy=False)
            raise
        finally:
            settle_ack()
            set_locality("unknown")  # provenance is per conversation; never left stale
            set_processing("unknown", False)
            self._voice_timings.end()
            self._workspace_event(workspace_generation, "activity", "Voice session ended")
            client.close()

    def _robot_stt_client(self) -> RobotSttClient:
        """The one owned on-robot ASR client; created on first robot-routed turn."""
        with self._status_lock:
            client = getattr(self, "_robot_stt", None)
            if client is None:
                client = self._robot_stt = RobotSttClient()
            return client

    def _stt_turn_fence(self, session_id: str) -> tuple[int, Callable[[], bool]]:
        """Immutable turn snapshot for recognition: (generation, is_current), never resampled.

        A game-master switch rotates the Kids session id and advances the stop generation, so
        either check fences a decode that began in the previous game.
        """
        with self._conversation_stop_generation_lock:
            turn_generation = self._turn_stop_generation

        def is_current() -> bool:
            with self._conversation_stop_generation_lock:
                stopped = (
                    self._conversation_stop_requested.is_set()
                    or self._conversation_stop_generation != turn_generation
                )
            return (
                not stopped
                and (not session_id or self._kids_session_is_current(session_id))
                and not self.stop_event.is_set()
                and not self._privacy_requested.is_set()
                and self._effective_power_mode() not in {"meeting", "sleep"}
            )

        return turn_generation, is_current

    def _ack_output_quarantined(self) -> bool:
        """True while an old filler push is still blocked inside the SDK (fail closed)."""
        return output_quarantined(self)

    def _start_kids_ack(
        self,
        config: AppConfig,
        voice_proof: tuple[object, object] | None = None,
        refresh: Callable[[], object] | None = None,
    ) -> KidsAckPlayer | None:
        """Arm one delayed cached clip for an active Kids audio turn; None when not eligible.

        ``voice_proof`` is a just-sampled bridge proof (voice digest, model) of the effective Kids
        streaming voice, used for the first turn of a conversation. ``refresh`` instead fetches a
        fresh proof from the bridge when the delay elapses (later turns), off the answer path.
        Missing, failed or mismatching proof => no filler.
        """
        if not config.kids_mode_enabled or config.kids_activity == "calm":
            return None
        if (voice_proof is None and refresh is None) or self._ack_output_quarantined():
            return None
        language_clips = kids_ack_catalog().get(config.language, ())
        push = getattr(self.robot.media, "push_audio_sample", None)
        if not language_clips or not callable(push):
            return None  # missing/rejected assets or no PCM output: no filler, never live TTS
        if refresh is None:
            clips = eligible_clips(language_clips, *voice_proof)
            if not clips:
                return None
        else:

            def clips() -> tuple:
                proof = refresh()
                if not isinstance(proof, dict):
                    return ()
                return eligible_clips(language_clips, proof.get("kids_voice_sha256"), proof.get("kids_tts_model"))
        session_id = config.kids_session_id
        with self._conversation_stop_generation_lock:
            turn_generation = self._turn_stop_generation  # immutable snapshot; never resampled

        def is_current() -> bool:
            with self._conversation_stop_generation_lock:
                stopped = (
                    self._conversation_stop_requested.is_set()
                    or self._conversation_stop_generation != turn_generation
                )
            return (
                not stopped
                and self._kids_session_is_current(session_id)
                and not self.stop_event.is_set()
                and not self._privacy_requested.is_set()
                and self._effective_power_mode() not in {"meeting", "sleep"}
            )

        if not is_current():
            return None
        self._kids_ack_producer = player = KidsAckPlayer(
            clips=clips,
            selector=self._kids_ack_selector,
            push=push,
            clear=self._clear_streamed_audio,
            is_current=is_current,
            resample=resample_linear,
            output_rate=self._output_sample_rate,
            clock=time.monotonic,
        )
        return player.start()

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
