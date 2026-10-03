"""Kids Mode lifecycle, I Spy rounds, and moderated Kids speech streaming for the voice runtime.

``KidsModeMixin`` is mixed into ``HermesVoiceRuntime``. It keeps its state on the runtime instance
and relies on the runtime's robot, locks, status, announcements, and power transitions, so the
established lock order still applies: ``_motor_transition_lock`` before ``_kids_lock``.
"""

from __future__ import annotations

import logging
import math
import queue
import secrets
import threading
import time
from dataclasses import replace

import numpy as np

from .audio import resample_linear
from .config import AppConfig
from .hermes_client import HermesBridgeClient, HermesBridgeError
from .ispy import ISpyTarget
from .kids_mode import KidsProfile, build_kids_prompt, kids_greeting

# Keep the runtime logger name so existing log filters and dashboards still match Kids Mode lines.
_LOGGER = logging.getLogger("homebody.runtime")


class KidsModeMixin:
    """Supervised Kids Mode: start/stop, timers, I Spy, voice policy, and streaming playback."""

    def _init_kids_state(self) -> None:
        # Lock order: _motor_transition_lock before _kids_lock. status() reads Kids state and is
        # called while holding the motor lock, so the reverse nesting would deadlock.
        self._kids_lock = threading.RLock()
        self._kids_active = False
        self._kids_camera_active = False
        self._kids_locked = False
        self._kids_profile: KidsProfile | None = None
        self._kids_started_at = 0.0
        self._kids_ends_at = 0.0
        self._kids_session_id = ""
        self._kids_turns_at_start = 0
        self._kids_timer: threading.Timer | None = None
        self._kids_warning_timer: threading.Timer | None = None
        self._kids_generation = 0
        self._kids_ispy_player_motion_index = 0
        self._kids_last_end_reason = ""
        self._kids_last_fold_succeeded: bool | None = None

    @property
    def kids_controls_locked(self) -> bool:
        # Read without _kids_lock: the async HTTP middleware calls this on the event loop, and
        # _kids_lock can be held across multi-second robot transitions. Writers still hold the
        # lock; a single attribute read is atomic.
        return self._kids_locked

    def start_kids_mode(self, profile: KidsProfile, *, greet: bool = True) -> dict[str, object]:
        """Start one time-bounded, camera-free, private-tool-free Realtime session."""
        self.stop_presentation_window("kids_mode")
        if self._effective_power_mode() in {"meeting", "sleep"} or self._privacy_requested.is_set():
            raise RuntimeError("Kids Mode is blocked in Meeting and Sleep")
        if not self._audio_ready:
            raise RuntimeError("Kids Mode audio is not ready")
        self.revoke_camera_control()
        with self._kids_lock:
            # Keep the Kids guard while invalidating Agent work and forcing the
            # conversation profile, matching set_capability_profile's lock order.
            self.cancel_agent_work("kids_mode")
            with self._agent_lock:
                self._capability_profile = "conversation"
            previous = self._kids_timer
            previous_warning = self._kids_warning_timer
            replaced_session_id = self._kids_session_id if self._kids_active else ""
            if previous is not None:
                previous.cancel()
            if previous_warning is not None:
                previous_warning.cancel()
            self._kids_generation += 1
            generation = self._kids_generation
            self._kids_ispy_player_motion_index = 0
            self._kids_active = True
            self._kids_camera_active = False
            self._kids_locked = True
            self._presence.clear("kids_mode")
            self._kids_profile = profile
            self._kids_started_at = time.monotonic()
            self._kids_ends_at = self._kids_started_at + profile.duration_minutes * 60.0
            self._kids_session_id = "kids-" + secrets.token_hex(16)
            with self._status_lock:
                self._kids_turns_at_start = self._status.turns_completed
            self._kids_last_end_reason = ""
            self._kids_last_fold_succeeded = None
            timer = threading.Timer(profile.duration_minutes * 60.0, self._expire_kids_mode, args=(generation,))
            timer.daemon = True
            self._kids_timer = timer
            warning = threading.Timer(
                max(1.0, profile.duration_minutes * 60.0 - 300.0),
                self._warn_kids_mode,
                args=(generation,),
            )
            warning.daemon = True
            self._kids_warning_timer = warning
            timer.start()
            warning.start()
            started_session_id = self._kids_session_id
        if replaced_session_id:
            self._notify_bridge_kids_session_async(replaced_session_id, active=False)
        self._notify_bridge_kids_session_async(started_session_id, active=True)
        self._request_conversation_stop()
        with self._status_lock:
            self._status.transcript = ""
            self._status.response_preview = ""
        if self._motion is not None:
            self._motion.enabled = profile.motion_enabled
        ispy_target: ISpyTarget | None = None
        if profile.activity == "ispy":
            try:
                ispy_target = self._prepare_ispy_round(generation, profile)
            except Exception as exc:
                _LOGGER.warning("I Spy start failed (%s): %s", type(exc).__name__, exc)
                if self._kids_callback_is_current(generation):
                    self.stop_kids_mode(reason="ispy_start_failed", fold=True)
                raise
        if greet:
            try:
                if profile.activity == "ispy" and not self._kids_callback_is_current(generation):
                    raise RuntimeError("I Spy start was cancelled before its clue")
                self.queue_announcement(
                    ispy_target.clue(profile.language) if ispy_target else kids_greeting(profile),
                    behavior="wake_and_stay",
                    provider="elevenlabs",
                    model="eleven_flash_v2_5",
                    voice="cgSgspJ2msm6clMCkdW9",
                )
            except Exception:
                self.stop_kids_mode(reason="start_failed", fold=False)
                raise
        _LOGGER.info(
            "Kids Mode started (age=%s activity=%s duration=%sm motion=%s)",
            profile.age_band,
            profile.activity,
            profile.duration_minutes,
            profile.motion_enabled,
        )
        return dict(self.status()["kids_mode"])  # type: ignore[arg-type]

    @staticmethod
    def _ispy_head_target(yaw_degrees: float, pitch_degrees: float) -> np.ndarray:
        yaw, pitch = math.radians(yaw_degrees), math.radians(pitch_degrees)
        cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
        return np.array(
            [
                [cy * cp, -sy, cy * sp, 0.0],
                [sy * cp, cy, sy * sp, 0.0],
                [-sp, 0.0, cp, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ]
        )

    def _prepare_ispy_round(
        self,
        generation: int,
        profile: KidsProfile,
        *,
        client: HermesBridgeClient | None = None,
    ) -> ISpyTarget:
        """Run one consented, cancellable search; frames exist only on this stack."""
        if not profile.camera_consent:
            raise RuntimeError("Camera opt-in is required for I Spy")
        session_id = self._kids_session_id
        self.set_power_mode("awake", cancel_announcements=False)
        # Five retained viewpoints cover a 240° desk arc while every commanded segment remains <= 60°.
        # Two intermediate waypoints cross safely from the left extreme to the right side without capturing.
        scan_route = (
            (0.0, True),
            (-60.0, True),
            (-120.0, True),
            (-60.0, False),
            (0.0, False),
            (60.0, True),
            (120.0, True),
        )
        return_route = (60.0, 0.0)
        frames: list[bytes] = []
        owns_client = client is None
        goto_target = getattr(self.robot, "goto_target", None)
        if not callable(goto_target):
            raise RuntimeError("I Spy search motion is unavailable")
        with self._kids_lock:
            if not self._kids_callback_is_current(generation):
                raise RuntimeError("I Spy search was cancelled")
            self._kids_camera_active = True
        try:
            with self._motor_transition_lock:
                previous_body_yaw = 0.0
                for body_yaw, capture in scan_route:
                    if not self._kids_callback_is_current(generation):
                        raise RuntimeError("I Spy search was cancelled")
                    angular_distance = abs(body_yaw - previous_body_yaw)
                    goto_target(
                        head=self._ispy_head_target(body_yaw, -5.0),
                        body_yaw=math.radians(body_yaw),
                        antennas=np.radians(np.asarray([8.0, -8.0])),
                        duration=max(0.6, angular_distance / 30.0),
                        method="minjerk",
                    )
                    previous_body_yaw = body_yaw
                    if capture:
                        frames.append(self._capture_camera_jpeg(kids_generation=generation))
                with self._kids_lock:
                    self._kids_camera_active = False
                for body_yaw in return_route:
                    if not self._kids_callback_is_current(generation):
                        raise RuntimeError("I Spy search was cancelled")
                    angular_distance = abs(body_yaw - previous_body_yaw)
                    goto_target(
                        head=self._ispy_head_target(body_yaw, -5.0 if body_yaw else 0.0),
                        body_yaw=math.radians(body_yaw),
                        antennas=np.asarray([0.0, 0.0]),
                        duration=max(0.6, angular_distance / 30.0),
                        method="minjerk",
                    )
                    previous_body_yaw = body_yaw
            with self._kids_lock:
                self._kids_camera_active = False
            if not self._kids_callback_is_current(generation):
                raise RuntimeError("I Spy search was cancelled")
            if client is None:
                client = self._new_bridge_client(self.config_loader())
            target = client.select_ispy_target(
                frames,
                session_id=session_id,
                age_band=profile.age_band,
                language=profile.language,
            )
            if not self._kids_callback_is_current(generation):
                try:
                    client.cancel_ispy_session(session_id)
                except Exception:
                    _LOGGER.warning("Could not delete a cancelled I Spy bridge session")
                raise RuntimeError("I Spy search was cancelled")
            with self._kids_lock:
                self._kids_ispy_player_motion_index = 0
            return target
        finally:
            with self._kids_lock:
                self._kids_camera_active = False
            frames.clear()
            if owns_client and client is not None:
                client.close()

    def _perform_ispy_player_guess_motion(self, generation: int) -> None:
        """Use both the base and head for each player-object guess, without camera access."""
        goto_target = getattr(self.robot, "goto_target", None)
        if not callable(goto_target):
            raise RuntimeError("I Spy player-turn motion is unavailable")
        with self._kids_lock:
            if not self._kids_callback_is_current(generation):
                raise RuntimeError("I Spy player-turn motion was cancelled")
            index = self._kids_ispy_player_motion_index
            self._kids_ispy_player_motion_index += 1
        body_yaws = (-12.0, 12.0, -8.0, 8.0, -5.0, 5.0)
        body_yaw = body_yaws[index % len(body_yaws)]
        head_yaw = body_yaw * 1.5
        with self._motor_transition_lock:
            if not self._kids_callback_is_current(generation):
                raise RuntimeError("I Spy player-turn motion was cancelled")
            goto_target(
                head=self._ispy_head_target(head_yaw, -4.0),
                body_yaw=math.radians(body_yaw),
                antennas=np.radians(np.asarray([5.0, -5.0])),
                duration=1.5,
                method="minjerk",
            )
        if not self._kids_callback_is_current(generation):
            raise RuntimeError("I Spy player-turn motion was cancelled")

    def _try_ispy_player_guess_motion(self, generation: int) -> bool:
        """Keep an optional embodied gesture from suppressing an approved guess."""
        try:
            self._perform_ispy_player_guess_motion(generation)
            return True
        except Exception:
            _LOGGER.warning("I Spy player-picker gesture failed; continuing with speech", exc_info=True)
            return False

    def _continue_ispy_reachy_turn(
        self,
        client: HermesBridgeClient,
        *,
        barge_in: bool,
    ) -> bool:
        """Search with base motion, select a fresh target, and speak its approved clue."""
        with self._kids_lock:
            generation = self._kids_generation
            profile = self._kids_profile
            session_id = self._kids_session_id
            if (
                not self._kids_active
                or profile is None
                or profile.activity != "ispy"
                or client.config.kids_session_id != session_id
            ):
                raise RuntimeError("I Spy turn continuation is stale")
        try:
            self._prepare_ispy_round(generation, profile, client=client)
            if not self._kids_callback_is_current(generation):
                raise RuntimeError("I Spy turn continuation was cancelled")
            clue = client.ispy_clue(session_id)
            if not self._kids_callback_is_current(generation):
                raise RuntimeError("I Spy clue was cancelled")
            self._set_status(
                "speaking",
                "Streaming the next I Spy colour clue",
                tts_provider="elevenlabs-flash-stream",
            )
            return self._play_kids_stream(client, clue, barge_in=barge_in)
        except Exception:
            self.stop_kids_mode(reason="ispy_round_failed", fold=True)
            raise

    def _kids_callback_is_current(self, generation: int) -> bool:
        with self._kids_lock:
            return self._kids_active and generation == self._kids_generation

    def _warn_kids_mode(self, generation: int) -> None:
        with self._kids_lock:
            if not self._kids_active or generation != self._kids_generation:
                return
        try:
            self._request_conversation_stop()
            self._clear_streamed_audio()
            self.queue_announcement(
                text="Five minutes left in Kids Mode. Let's finish this activity soon.",
                repeat=1,
                pause_seconds=0.0,
                behavior="voice_only",
                provider="elevenlabs",
                model="eleven_flash_v2_5",
                voice="cgSgspJ2msm6clMCkdW9",
            )
        except Exception:
            _LOGGER.exception("Could not play the Kids Mode five-minute warning")

    def _expire_kids_mode(self, generation: int) -> None:
        with self._kids_lock:
            if not self._kids_active or generation != self._kids_generation:
                return
        try:
            self.stop_kids_mode(reason="time_limit", fold=True)
        except Exception:
            _LOGGER.exception("Kids Mode expiry could not complete the safe fold")

    def _notify_bridge_kids_session(self, session_id: str, active: bool) -> None:
        client = self._new_bridge_client(self.config_loader())
        try:
            client.set_kids_session_state(session_id, active=active)
        except Exception:
            _LOGGER.warning("Could not report the Kids session state to the Hermes bridge")
        finally:
            client.close()

    def _notify_bridge_kids_session_async(self, session_id: str, active: bool) -> None:
        threading.Thread(
            target=self._notify_bridge_kids_session,
            args=(session_id, active),
            name="kids-bridge-session",
            daemon=True,
        ).start()

    def _cancel_ispy_bridge_session(self, session_id: str) -> None:
        client = self._new_bridge_client(self.config_loader())
        try:
            client.cancel_ispy_session(session_id)
        except Exception:
            _LOGGER.warning("Could not delete stopped I Spy bridge session")
        finally:
            client.close()

    def stop_kids_mode(self, *, reason: str = "parent", fold: bool = True) -> dict[str, object]:
        """End Kids Mode immediately, cancel its voice/motion, and optionally fold safely."""
        with self._kids_lock:
            was_active = self._kids_active
            ended_session_id = self._kids_session_id if was_active else ""
            ispy_session_id = (
                self._kids_session_id
                if self._kids_profile is not None and self._kids_profile.activity == "ispy"
                else ""
            )
            timer, self._kids_timer = self._kids_timer, None
            warning, self._kids_warning_timer = self._kids_warning_timer, None
            if timer is not None and timer is not threading.current_thread():
                timer.cancel()
            if warning is not None and warning is not threading.current_thread():
                warning.cancel()
            self._kids_generation += 1
            self._kids_active = False
            self._kids_camera_active = False
            self._kids_locked = False
            self._kids_profile = None
            self._kids_ends_at = 0.0
            self._kids_session_id = ""
            self._kids_last_end_reason = reason[:40]
            self._kids_last_fold_succeeded = None
        if ended_session_id:
            self._notify_bridge_kids_session_async(ended_session_id, active=False)
        self._request_conversation_stop()
        self._cancel_announcements(clear_queue=True)
        self._clear_streamed_audio()
        cancel_move = getattr(self.robot, "cancel_move", None)
        if was_active and callable(cancel_move):
            try:
                cancel_move()
            except Exception:
                _LOGGER.warning("Could not cancel the active Kids Mode movement", exc_info=True)
        if ispy_session_id:
            cancel_thread = threading.Thread(
                target=self._cancel_ispy_bridge_session,
                args=(ispy_session_id,),
                name="kids-ispy-cancel",
                daemon=True,
            )
            cancel_thread.start()
        with self._status_lock:
            self._status.transcript = ""
            self._status.response_preview = ""
        if self._motion is not None:
            try:
                self._motion.enabled = self.config_loader().motion_enabled
            except Exception:
                self._motion.enabled = False
        if self._actions is not None:
            self._actions.cancel(stop_media=False)
        if fold:
            try:
                if self._effective_power_mode() not in {"meeting", "sleep"}:
                    self.set_power_mode("standby", cancel_announcements=False)
                if not self._head_safely_folded or self._motors_enabled is not False:
                    raise RuntimeError("Kids Mode ended, but Reachy's safe fold could not be verified")
            except Exception:
                with self._kids_lock:
                    self._kids_last_fold_succeeded = False
                    self._kids_last_end_reason = f"{reason[:28]}_fold_failed"
                raise
            with self._kids_lock:
                self._kids_last_fold_succeeded = True
        _LOGGER.info("Kids Mode ended (reason=%s, was_active=%s)", reason, was_active)
        return dict(self.status()["kids_mode"])  # type: ignore[arg-type]

    def _kids_voice_config(self, base: AppConfig) -> AppConfig:
        with self._kids_lock:
            if not self._kids_active or self._kids_profile is None:
                return base
            profile = self._kids_profile
            remaining = max(30.0, self._kids_ends_at - time.monotonic())
        return replace(
            base,
            conversation_mode="pipeline",
            home_assistant_assist_enabled=False,
            kids_mode_enabled=True,
            kids_session_id=self._kids_session_id,
            kids_age_band=profile.age_band,
            kids_activity=profile.activity,
            language=profile.language,
            system_prompt=build_kids_prompt(profile),
            continuous_conversation=True,
            conversation_timeout_seconds=min(base.conversation_timeout_seconds, remaining),
            camera_enabled=False,
            camera_feed_enabled=False,
            face_tracking_enabled=False,
            doa_enabled=False,
            robot_tools_enabled=False,
            agent_tools_enabled=False,
            power_tools_enabled=False,
        )

    def _kids_session_is_current(self, session_id: str) -> bool:
        """Return whether network/audio work still belongs to the active child session."""
        with self._kids_lock:
            return bool(session_id) and self._kids_active and self._kids_session_id == session_id


    def _play_kids_stream(
        self,
        client: HermesBridgeClient,
        text: str,
        *,
        barge_in: bool = True,
        animate_motion: bool = True,
    ) -> bool:
        """Stream child PCM while retaining immediate stop/privacy/wake control."""

        def cancelled() -> bool:
            return (
                self.stop_event.is_set()
                or (
                    client.config.kids_mode_enabled
                    and not self._kids_session_is_current(client.config.kids_session_id)
                )
                or self._conversation_stop_requested.is_set()
                or self._privacy_requested.is_set()
                or self._effective_power_mode() in {"meeting", "sleep"}
            )

        if cancelled():
            return False
        if animate_motion and self._motion is not None:
            self._motion.speaking()
        if self._spotter is not None:
            self._spotter.reset()

        stream_queue: queue.Queue[bytes | Exception | object] = queue.Queue(maxsize=16)
        stream_done = object()
        stream_cancel = threading.Event()

        def put_stream_item(item: bytes | Exception | object) -> None:
            while not stream_cancel.is_set():
                try:
                    stream_queue.put(item, timeout=0.1)
                    return
                except queue.Full:
                    continue

        def produce_stream() -> None:
            try:
                for chunk in client.iter_kids_speech(
                    text,
                    should_stop=lambda: stream_cancel.is_set() or cancelled(),
                ):
                    if stream_cancel.is_set() or cancelled():
                        break
                    put_stream_item(chunk)
            except Exception as exc:  # preserve the producer error for the conversation thread
                put_stream_item(exc)
            finally:
                put_stream_item(stream_done)

        producer = threading.Thread(
            target=produce_stream,
            name="reachy-kids-tts-stream",
            daemon=True,
        )
        producer.start()

        first_audio_at: float | None = None
        queued_samples = 0
        remainder = b""
        download_complete = False
        interrupted = False
        try:
            while not download_complete or (
                first_audio_at is not None
                and time.monotonic() < first_audio_at + queued_samples / self._output_sample_rate + 0.15
            ):
                if cancelled():
                    self._clear_streamed_audio()
                    return False

                if not download_complete:
                    try:
                        item = stream_queue.get(timeout=0.01)
                    except queue.Empty:
                        item = None
                    if item is stream_done:
                        download_complete = True
                    elif isinstance(item, Exception):
                        raise item
                    elif isinstance(item, bytes):
                        raw = remainder + item
                        usable = len(raw) - (len(raw) % 2)
                        remainder = raw[usable:]
                        if usable:
                            audio = np.frombuffer(raw[:usable], dtype="<i2").astype(np.float32) / 32768.0
                            output = resample_linear(audio, 24000, self._output_sample_rate)
                            if output.size:
                                if first_audio_at is None:
                                    first_audio_at = time.monotonic()
                                queued_samples += int(output.size)
                                if queued_samples > self._output_sample_rate * 120:
                                    raise HermesBridgeError("Kids Mode streaming speech exceeded the playback limit")
                                self.robot.media.push_audio_sample(output)

                if barge_in and self._spotter is not None:
                    frame = self._read_16k_frame()
                    if frame is not None:
                        keyword = self._spotter.accept(frame, 16000)
                        if keyword:
                            interrupted = True
                            self._clear_streamed_audio()
                            self._spotter.reset()
                            with self._status_lock:
                                self._status.interruptions += 1
                            self._set_status("listening", "Response interrupted; listening")
                            if self._motion is not None:
                                self._motion.listening()
                            _LOGGER.info("Streaming playback interrupted by local wake phrase: %s", keyword)
                            return True
                elif download_complete:
                    time.sleep(0.02)

            if first_audio_at is None or queued_samples == 0:
                raise HermesBridgeError("Kids Mode streaming speech returned no playable audio")
            _LOGGER.info(
                "Kids streaming speech played %.2fs of PCM using %s",
                queued_samples / self._output_sample_rate,
                client.last_tts_provider,
            )
            return interrupted
        except Exception:
            self._clear_streamed_audio()
            raise
        finally:
            stream_cancel.set()
            producer.join(timeout=2.5)
            if producer.is_alive():
                _LOGGER.warning("Kids Mode TTS producer did not exit promptly after cancellation")

