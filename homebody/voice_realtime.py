"""OpenAI Realtime voice front-end: one speech-to-speech session per wake, with tool calls and playback.

``RealtimeVoiceMixin`` is mixed into ``HermesVoiceRuntime``. The module-level Realtime event parsers
live here and are re-exported from ``runtime`` for existing imports.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass

from .audio import resample_linear
from .config import AppConfig
from .kids_acknowledgements import output_quarantined
from .realtime_client import RealtimeBridgeError, RealtimeBridgeSession
from .robot_tools import completed_robot_tool_call
from .safety_gate import POWER_MODES as _POWER_MODES

# Keep the runtime logger name so existing log filters still match these lines.
_LOGGER = logging.getLogger("homebody.runtime")


@dataclass(slots=True)
class RealtimePlayback:
    """Track audio that may still be buffered after generation has finished."""

    item_id: str = ""
    started_at: float | None = None
    queued_until: float = 0.0
    duration_seconds: float = 0.0

    def add(self, now: float, duration_seconds: float) -> None:
        if self.started_at is None or now >= self.queued_until:
            self.started_at = now
            self.queued_until = now
            self.duration_seconds = 0.0
        self.duration_seconds += duration_seconds
        self.queued_until = max(now, self.queued_until) + duration_seconds

    def audible(self, now: float) -> bool:
        return self.started_at is not None and now < self.queued_until

    def played_ms(self, now: float) -> int:
        if self.started_at is None:
            return 0
        elapsed = max(0.0, now - self.started_at)
        return int(min(elapsed, self.duration_seconds) * 1000.0)

    def reset(self) -> None:
        self.item_id = ""
        self.started_at = None
        self.queued_until = 0.0
        self.duration_seconds = 0.0


def realtime_audio_item_id(kind: str, payload: dict[str, object]) -> str:
    """Return only an assistant message ID that can legally be audio-truncated."""
    if kind in {"response.output_audio.delta", "response.audio.delta"}:
        return str(payload.get("item_id") or "")
    if kind != "response.output_item.added":
        return ""
    item = payload.get("item")
    if not isinstance(item, dict):
        return ""
    if item.get("type") != "message" or item.get("role") != "assistant":
        return ""
    return str(item.get("id") or "")


def realtime_response_id(kind: str, payload: dict[str, object]) -> str:
    """Return the response owning an event so interrupted output can be dropped."""
    direct = str(payload.get("response_id") or "")
    if direct:
        return direct
    if kind in {"response.created", "response.done", "response.cancelled", "response.failed"}:
        response = payload.get("response")
        if isinstance(response, dict):
            return str(response.get("id") or "")
    return ""


@dataclass(frozen=True, slots=True)
class PowerModeToolCall:
    call_id: str
    mode: str
    duration_minutes: int | None


def completed_power_mode_call(
    kind: str,
    payload: dict[str, object],
) -> PowerModeToolCall | None:
    """Parse a local power request only after its Realtime call is completed."""
    if kind != "response.output_item.done":
        return None
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    call_id = str(item.get("call_id") or "")
    if (
        item.get("type") != "function_call"
        or item.get("name") != "set_reachy_power_mode"
        or item.get("status") != "completed"
        or not call_id
    ):
        return None
    try:
        arguments = json.loads(item.get("arguments") or "{}")
    except (TypeError, json.JSONDecodeError):
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    mode = str(arguments.get("mode") or "").strip().lower()
    raw_duration = arguments.get("duration_minutes", 30)
    if isinstance(raw_duration, bool):
        duration_minutes = None
    else:
        try:
            duration_minutes = int(raw_duration)
        except (TypeError, ValueError):
            duration_minutes = None
    return PowerModeToolCall(call_id, mode, duration_minutes)


def completed_camera_call_id(kind: str, payload: dict[str, object]) -> str:
    """Return a completed camera tool call ID, never an in-progress/cancelled one."""
    if kind != "response.output_item.done":
        return ""
    item = payload.get("item")
    if not isinstance(item, dict):
        return ""
    if (
        item.get("type") != "function_call"
        or item.get("name") != "capture_reachy_camera"
        or item.get("status") != "completed"
    ):
        return ""
    return str(item.get("call_id") or "")


def camera_call_purpose(payload: dict[str, object]) -> str:
    """Return the model's stated reason for a camera call, used as the local vision question."""
    item = payload.get("item")
    if not isinstance(item, dict):
        return ""
    try:
        arguments = json.loads(str(item.get("arguments") or "{}"))
    except ValueError:
        return ""
    purpose = arguments.get("purpose") if isinstance(arguments, dict) else ""
    return " ".join(str(purpose or "").split())[:300]


class RealtimeVoiceMixin:
    """Run Realtime conversations, including power-mode and camera tool calls and interruptible playback."""

    def _handle_power_mode_call(
        self,
        session: RealtimeBridgeSession,
        power_call: PowerModeToolCall,
    ) -> dict[str, object]:
        """Apply a completed local power call and report its real resulting state."""
        mode = power_call.mode
        duration_minutes = power_call.duration_minutes
        if mode not in _POWER_MODES:
            result: dict[str, object] = {
                "ok": False,
                "error": "Mode must be standby, awake, meeting, or sleep",
            }
        elif mode == "meeting" and (duration_minutes is None or not 1 <= duration_minutes <= 480):
            result = {
                "ok": False,
                "error": "Meeting duration must be between 1 and 480 minutes",
            }
        else:
            duration_seconds = float((duration_minutes or 30) * 60) if mode == "meeting" else 0.0
            try:
                self.set_power_mode(mode, duration_seconds=duration_seconds)
                result = {"ok": True, "mode": mode}
            except RuntimeError as exc:
                result = {"ok": False, "mode": mode, "error": str(exc)}
            if mode == "meeting":
                result["duration_minutes"] = duration_minutes or 30
        session.send_tool_result(
            power_call.call_id,
            result,
            continue_response=not result.get("ok") or mode == "awake",
        )
        _LOGGER.info("Realtime power mode tool: %s", result)
        return result

    def _describe_for_realtime(
        self,
        session: RealtimeBridgeSession,
        call_id: str,
        question: str,
        config: AppConfig,
    ) -> None:
        """Answer a Realtime camera call with the local vision model off the event loop.

        A slow or unreachable vision server must not stall audio, barge-in or Stop, so the frame
        is described on a worker thread and the answer is sent whenever it arrives.
        """

        from .companion import LookCancelled

        authority = self._companion_authority("voice")

        def describe() -> None:
            # Same fenced orchestration as the phone and Pipeline entries: the tool call is admitted
            # under this voice turn's ownership and original Stop generation; one capture, no frame
            # is ever attached to the Realtime session, and a cancelled look stays silent.
            try:
                seen = self.look_at_this(question, origin="voice", authority=authority)
            except LookCancelled:
                return
            except Exception as exc:
                category = "blocked" if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
                _LOGGER.warning("Camera call could not be answered (%s)", category)
                with self._status_lock:
                    self._status.camera_last_error = str(exc)[:200]
                if not authority():
                    return
                try:
                    session.send_camera_error(call_id, "Reachy could not take the requested look right now")
                except Exception:
                    _LOGGER.debug("Could not report the look failure for %s", call_id, exc_info=True)
                return
            if not self._companion_result_current(seen, authority):
                # Privacy was requested while the model was looking; drop the description.
                return
            try:
                session.send_tool_result(
                    call_id,
                    {
                        "ok": True,
                        "image_attached": False,
                        "seen_by": str(seen.get("processing") or "unknown"),
                        "description": seen["answer"],
                    },
                )
            except Exception:
                _LOGGER.exception("Could not deliver the look answer for %s", call_id)

        threading.Thread(target=describe, name="realtime-local-vision", daemon=True).start()

    def _run_realtime_conversation(self, config: AppConfig) -> None:
        """Run a persistent speech-to-speech session after the local wake word."""
        if self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"}:
            return
        if output_quarantined(self):
            return  # direct entry: same common audio-quarantine gate as the dispatcher
        from .companion import LOOK_CONTEXT_TTL_SECONDS
        from .look_intent import looks_like_look_request

        broker_context = self.agent_broker_context(explicit_private_intent=False)
        agent_request_id = ""
        if broker_context.capability_profile == "agent":
            agent_request_id, broker_context = self._begin_agent_request("Realtime Agent session")
        try:
            session = self._new_realtime_session(
                config,
                agent_context=broker_context,
                agent_request_id=agent_request_id,
            )
        except Exception:
            if agent_request_id:
                self._finish_agent_request(agent_request_id, broker_context.session_generation, succeeded=False)
            raise
        workspace_generation = self._workspace_lease()
        self._workspace_event(workspace_generation, "activity", "Listening via Realtime")
        timings = self._voice_timings
        timing_mode = "workspace" if broker_context.capability_profile == "agent" else "everyday"
        timings.begin("realtime", timing_mode, warm=False)
        transcript_parts: list[str] = []
        response_parts: list[str] = []
        last_activity = time.monotonic()
        speaking = False
        generation_done = False
        playback = RealtimePlayback()
        handled_camera_call_ids: set[str] = set()
        handled_robot_call_ids: set[str] = set()
        handled_power_call_ids: set[str] = set()
        active_response_id = ""
        interrupted_response_ids: set[str] = set()
        spoken_text = ""
        spoken_revision = 0
        captured_revision = -1
        visual_deadline = 0.0
        deferred_camera = []
        deferred_deadline = 0.0
        input_item_id = ""
        spoken_item_id = ""
        try:
            self._play_asset("listening.wav")
            self._discard_audio(0.34)
            self._set_status(
                "connecting_realtime",
                "Opening private GPT Realtime session",
                bridge_healthy=True,
                last_error="",
            )
            session.start()
        except Exception:
            if agent_request_id:
                self._finish_agent_request(agent_request_id, broker_context.session_generation, succeeded=False)
            raise
        if self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"}:
            session.close()
            if agent_request_id:
                self._finish_agent_request(agent_request_id, broker_context.session_generation, succeeded=False)
            return
        timings.mark("session_ready")
        self._set_status("listening", "Realtime session active")
        if self._motion is not None:
            self._motion.listening()
        session_completed = False
        try:
            while not self.stop_event.is_set() and not self._turn_stop_requested():
                if visual_deadline and time.monotonic() >= visual_deadline:
                    break  # visual data never outlives the bounded native session
                if self._effective_power_mode() in {"meeting", "sleep"}:
                    break
                if time.monotonic() - last_activity >= config.conversation_timeout_seconds:
                    _LOGGER.info("Realtime conversation closed after inactivity timeout")
                    break

                frame = self._read_16k_frame()
                if frame is not None:
                    session.send_audio(resample_linear(frame, 16000, 24000))

                events = list(session.events())
                if deferred_camera and time.monotonic() >= deferred_deadline:
                    for held in deferred_camera:
                        identifier = completed_camera_call_id(held.type, held.payload)
                        handled_camera_call_ids.add(identifier)
                        session.send_camera_error(identifier, "A current explicit spoken look could not be verified")
                    deferred_camera = []
                if spoken_text and deferred_camera:
                    events.extend(deferred_camera)
                    deferred_camera = []
                for event in events:
                    if self._turn_stop_requested() or self._privacy_requested.is_set():
                        break
                    kind = event.type
                    payload = event.payload
                    event_response_id = realtime_response_id(kind, payload)
                    audio_item_id = realtime_audio_item_id(kind, payload)
                    if audio_item_id:
                        playback.item_id = audio_item_id
                    if kind in {"bridge.error", "error"}:
                        error = payload.get("error")
                        if isinstance(error, dict):
                            error = error.get("message") or error
                        message = str(error or "Realtime session failed")
                        if "Only model output audio messages can be truncated" in message:
                            _LOGGER.warning(
                                "Realtime audio truncation was rejected after local queue clear: %s",
                                message,
                            )
                            continue
                        raise RealtimeBridgeError(message)
                    if kind != "input_audio_buffer.speech_started" and (
                        event_response_id and event_response_id in interrupted_response_ids
                    ):
                        if kind in {"response.done", "response.cancelled", "response.failed"}:
                            interrupted_response_ids.discard(event_response_id)
                            if event_response_id == active_response_id:
                                active_response_id = ""
                        continue
                    if kind == "input_audio_buffer.speech_stopped":
                        timings.mark("provider_speech_end")
                    elif (
                        kind == "response.output_item.done"
                        and (payload.get("item") or {}).get("type") == "function_call"
                    ):
                        timings.mark("tool_requested")
                    if kind == "input_audio_buffer.speech_started":
                        spoken_text = ""
                        for held in deferred_camera:
                            handled_camera_call_ids.add(completed_camera_call_id(held.type, held.payload))
                        deferred_camera = []
                        now = time.monotonic()
                        last_activity = now
                        transcript_parts.clear()
                        if active_response_id:
                            interrupted_response_ids.add(active_response_id)
                        if speaking or playback.audible(now):
                            self._workspace_event(
                                workspace_generation,
                                "activity",
                                "Reply interrupted; listening again",
                            )
                            played_ms = playback.played_ms(now)
                            self._clear_streamed_audio()
                            if playback.item_id:
                                session.truncate_audio(playback.item_id, played_ms)
                            _LOGGER.info(
                                "Realtime interruption: cleared buffered audio at %s ms",
                                played_ms,
                            )
                            speaking = False
                            generation_done = False
                            playback.reset()
                            with self._status_lock:
                                self._status.interruptions += 1
                        self._set_status("listening", "Listening to interruption")
                        if self._motion is not None:
                            self._motion.listening()
                    elif kind == "input_audio_buffer.committed":
                        input_item_id = str(payload.get("item_id") or "")
                        if input_item_id != spoken_item_id:
                            spoken_text = ""
                    elif kind in {
                        "conversation.item.input_audio_transcription.delta",
                        "conversation.item.input_audio_transcription.completed",
                    }:
                        text = str(payload.get("delta") or payload.get("transcript") or "")
                        if text:
                            if kind.endswith(".delta"):
                                transcript_parts.append(text)
                            else:
                                transcript_parts = [text]
                            if kind.endswith(".completed"):
                                spoken_item_id = str(payload.get("item_id") or "")
                                if input_item_id and spoken_item_id != input_item_id:
                                    continue  # an earlier utterance grants no current camera consent
                                spoken_text = text
                                spoken_revision += 1
                                if visual_deadline and not looks_like_look_request(text, engaged=True):
                                    try:
                                        self._take_follow_up(text)
                                    except RuntimeError:
                                        self._request_conversation_stop()
                                        break
                                self._workspace_event(
                                    workspace_generation,
                                    "user",
                                    text,
                                    key=str(payload.get("item_id") or ""),
                                )
                            self._set_status(
                                "thinking",
                                "Homebody is responding",
                                transcript="".join(transcript_parts).strip(),
                                stt_provider="openai-realtime",
                            )
                    camera_call_id = completed_camera_call_id(kind, payload)
                    robot_call = completed_robot_tool_call(kind, payload)
                    power_call = completed_power_mode_call(kind, payload)
                    if power_call is not None and power_call.call_id not in handled_power_call_ids:
                        handled_power_call_ids.add(power_call.call_id)
                        if visual_deadline:
                            session.send_tool_result(
                                power_call.call_id,
                                {
                                    "ok": False,
                                    "error": "Looking together is tool-free; Stop and wake again for other help",
                                },
                            )
                        elif config.power_tools_enabled:
                            self._handle_power_mode_call(session, power_call)
                        else:
                            session.send_tool_result(
                                power_call.call_id,
                                {"ok": False, "error": "Power tools are disabled for this session"},
                            )
                    elif camera_call_id and camera_call_id not in handled_camera_call_ids:
                        if not spoken_text:
                            if not deferred_camera:
                                deferred_deadline = time.monotonic() + 3.0
                            if len(deferred_camera) < 8:
                                deferred_camera.append(event)
                            else:
                                handled_camera_call_ids.add(camera_call_id)
                                session.send_camera_error(camera_call_id, "Too many pending camera requests")
                            continue  # transcription may complete after tool selection; never guess consent
                        handled_camera_call_ids.add(camera_call_id)
                        if spoken_revision == captured_revision or not looks_like_look_request(
                            spoken_text, engaged=self.look_context_live()
                        ):
                            session.send_camera_error(camera_call_id, "Ask explicitly for one look or a fresh look")
                            continue
                        captured_revision = spoken_revision
                        visual_deadline = time.monotonic() + LOOK_CONTEXT_TTL_SECONDS
                        self._set_status("looking", "Capturing one on-demand camera frame")
                        try:
                            if not config.camera_enabled:
                                raise RuntimeError("Camera access is disabled in Reachy settings")
                            if self._effective_power_mode() in {"meeting", "sleep"}:
                                raise RuntimeError("Camera capture is blocked in the current privacy mode")
                            # Both configured routes use the same original-authority orchestration.
                            # Only bounded analysis reaches Realtime, never image bytes or model-selected consent.
                            self._describe_for_realtime(session, camera_call_id, spoken_text, config)
                        except Exception as exc:
                            message = str(exc)
                            _LOGGER.exception("Could not provide Reachy camera frame")
                            with self._status_lock:
                                self._status.camera_last_error = message
                            session.send_camera_error(camera_call_id, message)
                            self._set_status("thinking", "Camera capture failed; Hermes is responding")
                    elif robot_call is not None and robot_call.call_id not in handled_robot_call_ids:
                        handled_robot_call_ids.add(robot_call.call_id)
                        if visual_deadline:
                            result: dict[str, object] = {
                                "ok": False,
                                "error": "Physical tools are unavailable while looking together",
                            }
                        elif not config.robot_tools_enabled:
                            result: dict[str, object] = {
                                "ok": False,
                                "error": "Robot tools are disabled in Reachy settings",
                            }
                        elif self._effective_power_mode() in {"meeting", "sleep"}:
                            result = {
                                "ok": False,
                                "error": "Physical actions are blocked in the current privacy mode",
                            }
                        elif self._actions is None:
                            result = {"ok": False, "error": "Robot action controller is unavailable"}
                        else:

                            def complete_robot_tool(
                                completed: dict[str, object],
                                call_id: str = robot_call.call_id,
                            ) -> None:
                                try:
                                    session.send_tool_result(call_id, completed)
                                except Exception:
                                    _LOGGER.exception("Could not complete Realtime robot tool %s", call_id)

                            result = self._actions.enqueue(
                                robot_call.name,
                                robot_call.arguments,
                                on_complete=complete_robot_tool,
                            )
                        if not result.get("accepted"):
                            session.send_tool_result(robot_call.call_id, result)
                        _LOGGER.info("Realtime robot tool %s: %s", robot_call.name, result)
                        self._set_status("thinking", "Hermes queued a Reachy action")
                    elif kind == "response.created":
                        response_parts.clear()
                        self._workspace_event(workspace_generation, "activity", "Realtime is responding")
                        active_response_id = event_response_id
                        last_activity = time.monotonic()
                        generation_done = False
                        playback.reset()
                        self._set_status("thinking", "Homebody is responding")
                        if self._motion is not None:
                            self._motion.thinking()
                    elif kind == "response.output_item.added":
                        last_activity = time.monotonic()
                        self._set_status("thinking", "Homebody is responding")
                    elif kind in {"response.output_audio.delta", "response.audio.delta"}:
                        audio = session.audio_samples(event)
                        if audio.size:
                            now = time.monotonic()
                            timings.mark("first_provider_audio", at=now)
                            if not speaking:
                                speaking = True
                                self._workspace_event(workspace_generation, "activity", "Speaking the reply")
                                self._set_status(
                                    "speaking",
                                    "Hermes Realtime is speaking",
                                    tts_provider="openai-realtime",
                                )
                                if self._motion is not None:
                                    self._motion.speaking()
                            output = resample_linear(audio, 24000, self._output_sample_rate)
                            self.robot.media.push_audio_sample(output)
                            playback.add(now, output.size / self._output_sample_rate)
                            timings.mark("player_enqueued", at=now)
                            if playback.started_at is not None:
                                timings.mark("playback_estimated", at=playback.started_at)
                            last_activity = now
                    elif kind in {
                        "response.output_audio_transcript.delta",
                        "response.audio_transcript.delta",
                    }:
                        response_parts.append(str(payload.get("delta") or ""))
                        self._set_status(
                            "speaking",
                            "Hermes Realtime is speaking",
                            response_preview="".join(response_parts)[-240:],
                        )
                    elif kind in {"response.output_audio_transcript.done", "response.audio_transcript.done"}:
                        self._workspace_event(
                            workspace_generation,
                            "assistant",
                            str(payload.get("transcript") or "".join(response_parts)),
                            key=event_response_id or active_response_id,
                        )
                    elif kind in {"response.done", "response.output_audio.done", "response.audio.done"}:
                        if kind == "response.done":
                            response = payload.get("response") or {}
                            if isinstance(response, dict) and response.get("status") == "completed":
                                self._workspace_event(
                                    workspace_generation,
                                    "assistant",
                                    "".join(response_parts),
                                    key=event_response_id or active_response_id,
                                )
                            if not event_response_id or event_response_id == active_response_id:
                                active_response_id = ""
                            with self._status_lock:
                                self._status.turns_completed += 1
                            generation_done = True
                        last_activity = time.monotonic()
                    if self._turn_stop_requested():
                        break
                if self._turn_stop_requested():
                    break
                if generation_done and speaking and not playback.audible(time.monotonic()):
                    speaking = False
                    generation_done = False
                    playback.reset()
                    timings.end()
                    timings.begin("realtime", timing_mode, warm=True)
                    self._set_status("listening", "Waiting for a follow-up")
                    if self._motion is not None:
                        self._motion.listening()
            session_completed = True
        finally:
            # Local Stop wins: flush the speaker before the (possibly slow) socket close.
            self._end_companion_voice()
            self._clear_streamed_audio()
            session.close()
            timings.end()
            self._workspace_event(workspace_generation, "activity", "Realtime voice session ended")
            if agent_request_id:
                self._finish_agent_request(
                    agent_request_id,
                    broker_context.session_generation,
                    succeeded=session_completed,
                )
