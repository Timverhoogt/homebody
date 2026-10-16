"""GPT-Live (preview) voice front-end: one full-duplex session per wake through the bridge.

Live has no response/audio-done marker, so the local playback queue decides when Reachy is
audible. Output audio is bounded; local Stop/privacy flushes playback before the (bounded)
cloud close. Physical barge-in and acoustic echo behaviour are unverified until a supervised
test: with barge-in disabled the microphone is not streamed while Reachy is audible.
"""

from __future__ import annotations

import logging
import time

from .audio import resample_linear
from .config import AppConfig
from .kids_acknowledgements import output_quarantined
from .live_client import LiveBridgeSession
from .realtime_client import RealtimeBridgeError
from .voice_realtime import RealtimePlayback

_LOGGER = logging.getLogger("homebody.runtime")
# Never queue more than this much speech ahead of the speaker.
_MAX_QUEUED_SECONDS = 30.0


class LiveVoiceMixin:
    """Run GPT-Live conversations; no camera, movement or memory tools are exposed to Live."""

    def _new_live_session(self, *args: object, **kwargs: object) -> LiveBridgeSession:
        return LiveBridgeSession(*args, **kwargs)  # type: ignore[arg-type]

    def _run_live_conversation(self, config: AppConfig) -> None:
        if self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"}:
            return
        if output_quarantined(self):
            return  # direct entry: same common audio-quarantine gate as the dispatcher
        broker_context = self.agent_broker_context(explicit_private_intent=False)
        agent_request_id = ""
        if broker_context.capability_profile == "agent":
            agent_request_id, broker_context = self._begin_agent_request("Live Workspace session")
        mode = "workspace" if broker_context.capability_profile == "agent" else "everyday"
        timings = self._voice_timings
        timings.begin("live", mode, warm=False)
        session = self._new_live_session(config, agent_context=broker_context, agent_request_id=agent_request_id)
        workspace_generation = self._workspace_lease()
        completed = False
        try:
            self._play_asset("listening.wav")
            self._discard_audio(0.34)
            self._set_status("connecting_realtime", "Opening GPT-Live preview session", bridge_healthy=True,
                             last_error="")
            session.start()
            pending = session.wait_started()
            timings.mark("session_ready")
            if self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"}:
                return
            self._workspace_event(workspace_generation, "activity", "Listening via GPT-Live preview")
            self._set_status("listening", "GPT-Live session active")
            if self._motion is not None:
                self._motion.listening()
            self._live_loop(config, session, pending, mode, workspace_generation)
            completed = True
        finally:
            # Local Stop wins: flush the speaker before any cloud finalization can fail or stall.
            self._clear_streamed_audio()
            session.close()
            timings.end()
            self._workspace_event(workspace_generation, "activity", "Live voice session ended")
            if agent_request_id:
                self._finish_agent_request(agent_request_id, broker_context.session_generation, succeeded=completed)

    def _live_loop(self, config, session, pending, mode, workspace_generation) -> None:
        timings = self._voice_timings
        playback = RealtimePlayback()
        speaking = False
        heard: list[str] = []
        said: list[str] = []
        last_activity = time.monotonic()
        dropped_audio = False
        while not self.stop_event.is_set() and not self._turn_stop_requested():
            if self._effective_power_mode() in {"meeting", "sleep"}:
                return
            now = time.monotonic()
            if now - last_activity >= config.conversation_timeout_seconds:
                _LOGGER.info("Live conversation closed after inactivity timeout")
                return
            frame = self._read_16k_frame()
            if frame is not None and (config.barge_in_enabled or not playback.audible(now)):
                session.send_audio(resample_linear(frame, 16000, 24000))
            events, pending = pending + session.events(), []
            for event in events:
                kind, payload = event.type, event.payload
                if kind == "bridge.error":
                    raise RealtimeBridgeError(str(payload.get("error") or "Live session failed"))
                if kind == "session.closed":
                    return
                if kind == "session.input_transcript.delta":
                    last_activity = time.monotonic()
                    heard.append(str(payload.get("delta") or ""))
                    self._set_status("thinking", "Listening", transcript="".join(heard)[-240:].strip(),
                                     stt_provider="openai-live")
                elif kind == "session.output_transcript.delta":
                    said.append(str(payload.get("delta") or ""))
                    self._set_status("speaking", "GPT-Live is speaking", response_preview="".join(said)[-240:])
                elif kind == "bridge.delegation":
                    state = str(payload.get("state") or "")
                    if state == "started":
                        timings.mark("tool_requested")
                        self._set_status("thinking", "Checking that for you")
                    elif state in {"answered", "not_ready", "failed"}:
                        timings.mark("tool_result")  # backend returned; only "answered" is a verified reply
                        if state == "not_ready":
                            self._set_status("thinking", "Project session not open")
                elif kind == "session.output_audio.delta":
                    audio = session.audio_samples(event)
                    if not audio.size:
                        continue
                    now = time.monotonic()
                    timings.mark("first_provider_audio", at=now)
                    if playback.audible(now) and playback.queued_until - now > _MAX_QUEUED_SECONDS:
                        if not dropped_audio:
                            _LOGGER.warning("Live output queue full; dropping audio beyond %ss", _MAX_QUEUED_SECONDS)
                            dropped_audio = True
                        continue
                    output = resample_linear(audio, 24000, self._output_sample_rate)
                    self._playback.push_audio_sample(output)
                    playback.add(now, output.size / self._output_sample_rate)
                    timings.mark("player_enqueued", at=now)
                    if playback.started_at is not None:
                        timings.mark("playback_estimated", at=playback.started_at)
                    last_activity = now
                    if not speaking:
                        speaking = True
                        self._set_status("speaking", "GPT-Live is speaking", tts_provider="openai-live")
                        if self._motion is not None:
                            self._motion.speaking()
                if self._turn_stop_requested():
                    return
            if speaking and not playback.audible(time.monotonic()):
                # The queue drained: that, not a provider marker, ends the audible reply.
                speaking = False
                dropped_audio = False
                playback.reset()
                if heard:
                    self._workspace_event(workspace_generation, "user", " ".join("".join(heard).split()))
                if said:
                    self._workspace_event(workspace_generation, "assistant", " ".join("".join(said).split()))
                heard, said = [], []
                with self._status_lock:
                    self._status.turns_completed += 1
                timings.end()
                timings.begin("live", mode, warm=True)
                self._set_status("listening", "Waiting for a follow-up")
                if self._motion is not None:
                    self._motion.listening()
