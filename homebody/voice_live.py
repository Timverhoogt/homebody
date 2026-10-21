"""GPT-Live (preview) voice front-end: one full-duplex session per wake through the bridge.

Live has no response/audio-done marker, so the local playback queue decides when Reachy is
audible. Output audio is bounded; local Stop/privacy flushes playback before the (bounded)
cloud close. Physical barge-in and acoustic echo behaviour are unverified until a supervised
test: with barge-in disabled the microphone is not streamed while Reachy is audible.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from .audio import resample_linear
from .config import AppConfig
from .everyday_runtime import EverydayCallbackScope, natural_camera_requests
from .kids_acknowledgements import output_quarantined
from .live_client import LiveBridgeSession, bridge_error_text
from .realtime_client import RealtimeBridgeError
from .voice_realtime import RealtimePlayback

_LOGGER = logging.getLogger("homebody.runtime")
# Never queue more than this much speech ahead of the speaker.
_MAX_QUEUED_SECONDS = 30.0


class LiveVoiceMixin:
    """Run GPT-Live conversations with bounded Everyday local tools; never private memory."""

    def _new_live_session(self, *args: object, **kwargs: object) -> LiveBridgeSession:
        return LiveBridgeSession(*args, **kwargs)  # type: ignore[arg-type]

    def _live_camera_scope(self, config, original_fence, closed) -> Callable[[], bool]:
        # Freeze before session startup, not after a blocking microphone/provider read.
        # Never refresh a look authority or a tool advertisement inside the old session.
        enabled = (config.camera_enabled, natural_camera_requests(config))

        def current() -> bool:
            loader = getattr(self, "config_loader", None)
            latest_config = loader() if callable(loader) else None
            # Revoking the household approval retires an open session just like Camera access off.
            changed = latest_config is not None and (
                latest_config.camera_enabled,
                natural_camera_requests(latest_config),
            ) != enabled
            if original_fence is not None:
                latest = self.everyday_tool_fence()
                # A general Stop/new wake already has its own original-fence refusals.
                # Only camera-specific revocation needs this additional restart boundary.
                changed = changed or (
                    latest == original_fence and latest.camera_generation != original_fence.camera_generation
                )
            if changed:
                closed.set()  # revoke queued callbacks before reporting or closing transport
                message = "Camera access changed; wake Reachy again and repeat your request"
                self._set_status("waiting_for_wake_word", message, last_error=message)
                # Wake polling replaces detail immediately. Reuse bounded turn-error retention
                # for visible next-step guidance, without throwing or playing an error sound.
                self._turn_error = (message, time.monotonic() + 30.0)
                return False
            return not closed.is_set()

        return current

    def _run_live_conversation(self, config: AppConfig) -> None:
        if self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"}:
            return
        if output_quarantined(self):
            return  # direct entry: same common audio-quarantine gate as the dispatcher
        local_closed = threading.Event()
        original_fence = (
            self.everyday_tool_fence(local_closed) if callable(getattr(self, "everyday_tool_fence", None)) else None
        )
        camera_current = self._live_camera_scope(config, original_fence, local_closed)
        look_authority = (
            self._companion_authority("voice", lambda: not local_closed.is_set() and not self._turn_stop_requested())
            if original_fence is not None
            else None
        )
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
            self._set_status(
                "connecting_realtime", "Opening GPT-Live preview session", bridge_healthy=True, last_error=""
            )
            session.start()
            pending = session.wait_started()
            timings.mark("session_ready")
            if (
                self._turn_stop_requested()
                or self.stop_event.is_set()
                or self._privacy_requested.is_set()
                or self._effective_power_mode() in {"meeting", "sleep"}
            ):
                return
            if not camera_current():
                return
            self._workspace_event(workspace_generation, "activity", "Listening via GPT-Live preview")
            self._set_status("listening", "GPT-Live session active")
            if self._motion is not None:
                self._motion.listening()
            self._live_loop(
                config,
                session,
                pending,
                mode,
                workspace_generation,
                original_fence=original_fence,
                look_authority=look_authority,
                camera_current=camera_current,
            )
            completed = True
        finally:
            # Local Stop wins: flush the speaker before any cloud finalization can fail or stall.
            local_closed.set()
            self._end_companion_voice()
            try:
                self._clear_streamed_audio()
            finally:
                # A local audio failure must never leak the provider session.
                session.close()
                timings.end()
            self._workspace_event(workspace_generation, "activity", "Live voice session ended")
            if agent_request_id:
                self._finish_agent_request(agent_request_id, broker_context.session_generation, succeeded=completed)

    def _start_live_look(
        self,
        session: LiveBridgeSession,
        delegation_id: str,
        question: str,
        *,
        follow_up: bool = False,
        authority: Callable[[], bool] | None = None,
    ) -> None:
        from .companion import CompanionAuthority, LookCancelled

        if not delegation_id or not question:
            return
        original_authority = self._companion_authority("voice", authority)
        guard = CompanionAuthority(
            original_authority.identity,
            "voice",
            lambda: not self._turn_stop_requested() and original_authority(),
        )
        if not guard():
            return
        self._set_status("looking", "Looking at what you are showing")

        def run() -> None:
            if not guard():
                return  # A queued worker cannot adopt a Stop or a later wake's authority.
            try:
                operation = self.follow_up_look if follow_up else self.look_at_this
                seen = operation(question, origin="voice", authority=guard)
            except LookCancelled:
                return  # Stop/privacy/Kids/profile won: silent
            except (RuntimeError, ValueError) as exc:
                _LOGGER.info("Live look could not be answered (%s)", type(exc).__name__)
                if not guard():
                    return
                try:
                    session.send_look_result(delegation_id, error="look_failed")
                except Exception:
                    _LOGGER.debug("Could not report the Live look failure", exc_info=True)
                return
            if not self._companion_result_current(seen, guard):
                return
            try:
                session.send_look_result(delegation_id, text=str(seen["answer"]))
            except Exception:
                _LOGGER.debug("Could not deliver the Live look answer", exc_info=True)

        import threading

        threading.Thread(target=run, name="reachy-live-look", daemon=True).start()

    def _live_loop(
        self,
        config,
        session,
        pending,
        mode,
        workspace_generation,
        *,
        original_fence=None,
        look_authority=None,
        camera_current=None,
    ) -> None:
        closed = original_fence.cancelled if original_fence is not None else threading.Event()
        if look_authority is None and callable(getattr(self, "everyday_tool_fence", None)):
            # Direct loop entry must freeze its scope before the first blocking microphone read too.
            look_authority = self._companion_authority(
                "voice", lambda: not closed.is_set() and not self._turn_stop_requested()
            )
        fence = original_fence if mode == "everyday" else None
        if camera_current is None:
            camera_current = self._live_camera_scope(config, original_fence, closed)
        try:
            self._live_loop_with_tools(
                config, session, pending, mode, workspace_generation, closed, fence, look_authority, camera_current
            )
        finally:
            closed.set()

    def _live_loop_with_tools(
        self, config, session, pending, mode, workspace_generation, closed, fence, look_authority, camera_current
    ) -> None:
        timings = self._voice_timings
        seen_local = set()
        visual = threading.Event()
        playback = RealtimePlayback()
        speaking = False
        heard: list[str] = []
        said: list[str] = []
        last_activity = time.monotonic()
        dropped_audio = False
        while not self.stop_event.is_set() and not self._turn_stop_requested():
            if self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"}:
                return
            if not camera_current():
                return
            now = time.monotonic()
            if now - last_activity >= config.conversation_timeout_seconds:
                _LOGGER.info("Live conversation closed after inactivity timeout")
                return
            frame = self._read_16k_frame()
            if self._turn_stop_requested() or self._privacy_requested.is_set() or self.stop_event.is_set():
                return
            if not camera_current():
                return
            if frame is not None and (config.barge_in_enabled or not playback.audible(now)):
                session.send_audio(resample_linear(frame, 16000, 24000))
            events, pending = pending + session.events(), []
            for event in events:
                if self._turn_stop_requested() or self._privacy_requested.is_set() or self.stop_event.is_set():
                    return
                if not camera_current():
                    return
                kind, payload = event.type, event.payload
                if kind == "bridge.error":
                    raise RealtimeBridgeError(bridge_error_text(payload, "Live session failed"))
                if kind == "session.closed":
                    return
                if kind == "bridge.conversation_stop":
                    # The person's own direct "Stop" (bridge-classified input transcript): the same
                    # local Stop as the button, so pending looks and playback end without a late reply.
                    self._request_conversation_stop()
                    return
                if kind == "everyday.tool_call":
                    identifier = payload.get("call_id")
                    if (
                        fence is None
                        or not isinstance(identifier, str)
                        or not 1 <= len(identifier) <= 128
                        or identifier in seen_local
                        or len(seen_local) >= 128
                    ):
                        raise RealtimeBridgeError("Invalid or replayed Everyday action")
                    seen_local.add(identifier)
                    # Camera admission comes from this turn's own transcript, never the model's choice;
                    # once a look's analysis entered this session, no callback is dispatched or delivered.
                    self.launch_everyday_tool(
                        payload.get("name"),
                        payload.get("arguments"),
                        fence=fence,
                        call_id=identifier,
                        deliver=lambda result, call_id=identifier: session.send_everyday_result(call_id, result),
                        is_current=lambda: not closed.is_set() and not self._turn_stop_requested(),
                        scope=EverydayCallbackScope(transcript="".join(heard), visual=visual),
                    )
                elif kind == "session.input_transcript.delta":
                    last_activity = time.monotonic()
                    heard.append(str(payload.get("delta") or ""))
                    self._set_status(
                        "thinking", "Listening", transcript="".join(heard)[-240:].strip(), stt_provider="openai-live"
                    )
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
                elif kind in {"bridge.look_request", "bridge.look_follow_up_request"}:
                    # The host recognised a deliberate spoken look request in this turn's own words.
                    # The look runs through the same fenced orchestration as the phone entry, off this
                    # loop so audio and Stop stay responsive; only text goes back, never a frame.
                    visual.set()
                    self._start_live_look(
                        session,
                        str(payload.get("delegation_id") or ""),
                        str(payload.get("question") or ""),
                        follow_up=kind == "bridge.look_follow_up_request",
                        authority=look_authority,
                    )
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
                if getattr(self, "_radio_handoff_ready", lambda: False)():
                    return  # Record the reply, then close cloud microphone before radio resumes.
                timings.begin("live", mode, warm=True)
                self._set_status("listening", "Waiting for a follow-up")
                if self._motion is not None:
                    self._motion.listening()
