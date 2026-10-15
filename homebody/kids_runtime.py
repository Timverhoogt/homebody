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
from .kids_acknowledgements import AckSelector
from .kids_game_master import (
    MAX_CHOICE_REPROMPTS,
    ROUND_TURN_LIMITS,
    available_activities,
    host_line,
    lobby_choices,
    parse_host_command,
    suggest_other,
)
from .kids_mode import (
    KIDS_TTS_MODEL,
    LANGUAGE_NAMES,
    SESSION_VOICE_LANGUAGES,
    KidsProfile,
    build_kids_prompt,
    kids_greeting,
    kids_message,
)

# Keep the runtime logger name so existing log filters and dashboards still match Kids Mode lines.
_LOGGER = logging.getLogger("homebody.runtime")

# How long a finished Start greeting may wait for the wake loop to open the first turn.
_KIDS_FIRST_LISTEN_TTL_SECONDS = 10.0
_KIDS_VOICE_ADMISSION_TIMEOUT_SECONDS = 3.0


def valid_kids_voice_proof(proof: object) -> bool:
    """A well-formed private digest+model proof of one language's Kids voice (never the raw id)."""
    if not isinstance(proof, dict) or proof.get("kids_tts_model") != KIDS_TTS_MODEL:
        return False
    digest = proof.get("kids_voice_sha256")
    return isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)


def _host_selector(key: str, **slots: str) -> tuple[dict[str, str], ...]:
    """One host-line catalog selector (activity-id slots only) for the bridge to render and approve."""
    return ({"kind": "host", "key": key, **slots},)


class ISpyInputUnavailable(RuntimeError):
    """A safe categorical typed-input lifecycle/availability error."""


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
        self._kids_input_token = secrets.token_hex(16)
        self._kids_input_stop_generation = -1
        self._typed_stop_generation: int | None = None
        self._kids_ispy_player_motion_index = 0
        self._kids_ispy_motion_started = False
        self._kids_last_end_reason = ""
        self._kids_last_fold_succeeded: bool | None = None
        # Single-use (session_id, kids generation, conversation-stop generation, deadline) ticket,
        # armed only after the Start greeting/clue finished playing. The stop generation is the
        # one sampled right after Start, so any later Stop, End, privacy or Standby invalidates it.
        self._kids_first_listen: tuple[str, int, int, float] | None = None
        self._kids_ack_selector = AckSelector()
        self._kids_ack_producer = None  # latest filler producer; quarantines audio while a push is blocked
        self._kids_start_stop_generation = -1
        # Game master (host) state: categorical only, reset by every Start and End.
        self._kids_host_phase = ""
        self._kids_host_activity = ""
        self._kids_host_choices: tuple[str, ...] = ()
        self._kids_host_notice = ""
        self._kids_host_round_turns = 0
        self._kids_host_reprompts = 0
        self._kids_host_offer_index = 0
        self._kids_host_typed_handled = False
        # Outer play-session identity: set by each caregiver Start, kept across game switches, so a
        # child Stop binds to the session (never a stale per-game generation) and never to a later Start.
        self._kids_play_id = ""

    def _kids_admission_fence(self) -> tuple[int, int]:
        """(Kids generation, conversation stop generation): any Stop/End/privacy/power bump changes it."""
        with self._kids_lock:
            kids_generation = self._kids_generation
        with self._conversation_stop_generation_lock:
            return kids_generation, self._conversation_stop_generation

    def _admit_kids_session_voice(self, language: str) -> None:
        """Refuse an ES/DE/FR/IT/PT session unless the bridge proves that language's own Kids voice."""
        name = LANGUAGE_NAMES[language]
        client: HermesBridgeClient | None = None
        try:
            client = self._new_bridge_client(self.config_loader())
            proof = client.kids_voice_proof(language=language, timeout=_KIDS_VOICE_ADMISSION_TIMEOUT_SECONDS)
        except Exception as exc:
            _LOGGER.warning("Kids %s voice admission failed (%s)", name, type(exc).__name__)
            raise RuntimeError(f"Kids Mode could not confirm the {name} voice on the Hermes host") from None
        finally:
            if client is not None:
                client.close()
        if not valid_kids_voice_proof(proof):
            raise RuntimeError(f"Kids Mode {name} voice is not configured on the Hermes host")

    def _kids_first_listen_valid(self, ticket: tuple[str, int, int, float], session_id: str) -> bool:
        """Caller holds _kids_lock. Compare immutable generations, not just the active flag."""
        ticket_session, ticket_generation, ticket_stop_generation, deadline = ticket
        with self._conversation_stop_generation_lock:
            stop_generation = self._conversation_stop_generation
        return (
            bool(session_id)
            and ticket_session == session_id
            and ticket_generation == self._kids_generation
            and ticket_stop_generation == stop_generation
            and time.monotonic() < deadline
            and self._kids_session_is_current(session_id)
            and not self.stop_event.is_set()
            and not self._privacy_requested.is_set()
            and self._effective_power_mode() not in {"meeting", "sleep"}
        )

    def _arm_kids_first_listen(self, session_id: str, *, generation: int | None = None) -> bool:
        """Arm the first listening turn for exactly the still-current, never-stopped Start session."""
        with self._kids_lock:
            if generation is not None and generation != self._kids_generation:
                return False  # the line belonged to an earlier, stopped or replaced generation
            ticket = (
                session_id, self._kids_generation, self._kids_start_stop_generation,
                time.monotonic() + _KIDS_FIRST_LISTEN_TTL_SECONDS,
            )
            if not self._kids_first_listen_valid(ticket, session_id):
                return False
            self._kids_first_listen = ticket
            return True

    def _take_kids_first_listen(self, session_id: str) -> tuple[str, int, int, float] | None:
        """Consume the ticket; return it only while it still belongs to the active session."""
        with self._kids_lock:
            ticket, self._kids_first_listen = self._kids_first_listen, None
            if ticket is None or not self._kids_first_listen_valid(ticket, session_id):
                return None
            return ticket

    def _admit_kids_first_listen(self, ticket: tuple[str, int, int, float]) -> bool:
        """Caller holds _motor_transition_lock. Revalidate, then accept the turn atomically.

        Runs before any motor admission and before turn cancellation is reset, so a Stop or End
        that completed after the ticket was consumed wins; a later End folds after this wake.
        """
        with self._kids_lock:
            if not self._kids_first_listen_valid(ticket, ticket[0]):
                return False
            with self._conversation_stop_generation_lock:
                if self._conversation_stop_generation != ticket[2]:
                    return False
                self._conversation_stop_requested.clear()
                self._turn_stop_generation = self._conversation_stop_generation
            return True

    @property
    def kids_controls_locked(self) -> bool:
        # Read without _kids_lock: the async HTTP middleware calls this on the event loop, and
        # _kids_lock can be held across multi-second robot transitions. Writers still hold the
        # lock; a single attribute read is atomic.
        return self._kids_locked

    def start_kids_mode(self, profile: KidsProfile, *, greet: bool = True) -> dict[str, object]:
        """Start one time-bounded, camera-free, private-tool-free Realtime session."""
        admission_fence: tuple[int, int] | None = None
        if profile.language in SESSION_VOICE_LANGUAGES:
            # Snapshot the cancellation fence before the unlocked network proof; a Stop/End that
            # completes meanwhile makes this Start stale. Never refreshed after the proof returns.
            admission_fence = self._kids_admission_fence()
            # Admission precedes every side effect: no presentation, camera, session, motion or audio.
            self._admit_kids_session_voice(profile.language)
        self.stop_presentation_window("kids_mode")
        if self._effective_power_mode() in {"meeting", "sleep"} or self._privacy_requested.is_set():
            raise RuntimeError("Kids Mode is blocked in Meeting and Sleep")
        if not self._audio_ready:
            raise RuntimeError("Kids Mode audio is not ready")
        self.revoke_camera_control()
        with self._motor_transition_lock, self._kids_lock:
            if admission_fence is not None and self._kids_admission_fence() != admission_fence:
                raise RuntimeError("Kids Mode start was cancelled before it began")
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
            self._kids_input_token = secrets.token_hex(16)
            self._kids_first_listen = None
            generation = self._kids_generation
            self._kids_ispy_player_motion_index = 0
            self._kids_ispy_motion_started = False
            self._kids_active = True
            self._kids_camera_active = False
            self._kids_locked = True
            self._presence.clear("kids_mode")
            self._kids_profile = profile
            self._kids_started_at = time.monotonic()
            self._kids_ends_at = self._kids_started_at + profile.duration_minutes * 60.0
            self._kids_session_id = "kids-" + secrets.token_hex(16)
            self._kids_play_id = secrets.token_hex(16)
            with self._status_lock:
                self._kids_turns_at_start = self._status.turns_completed
            self._kids_last_end_reason = ""
            self._kids_last_fold_succeeded = None
            self._reset_kids_host(
                "lobby" if profile.host else "",
                lobby_choices(camera_consent=profile.camera_consent) if profile.host else (),
            )
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
            # Cancel the previous turn and keep the receipt of exactly that cancellation as this
            # Start's immutable baseline, inside admission and before any unlocked callback. Any
            # later Stop/End/Standby/privacy bumps the generation past it; it is never refreshed.
            receipt = self._request_conversation_stop()
            self._kids_start_stop_generation = receipt if isinstance(receipt, int) else -1
        if replaced_session_id:
            self._notify_bridge_kids_session_async(replaced_session_id, active=False)
        self._notify_bridge_kids_session_async(started_session_id, active=True)
        with self._status_lock:
            self._status.transcript = ""
            self._status.response_preview = ""
        if self._motion is not None:
            self._motion.enabled = profile.motion_enabled
        ispy_target: ISpyTarget | None = None
        # Spoken fixed copy is rendered and approved by the bridge from catalog selectors only; the
        # caregiver-typed nickname is free text, so it is never part of an approved spoken line.
        greeting = kids_greeting(replace(profile, nickname=""))
        lines: tuple[dict[str, str], ...] = ({"kind": "greeting"},)
        if profile.host:
            # The host opens in the lobby: no game, no camera, two concrete choices.
            first, second = lobby_choices(camera_consent=profile.camera_consent)
            greeting = host_line(profile.language, "lobby", a=first, b=second)
            lines = ({"kind": "host", "key": "lobby", "a": first, "b": second},)
        elif profile.activity == "ispy":
            try:
                ispy_target = self._prepare_ispy_round(generation, profile)
            except Exception as exc:
                _LOGGER.warning("I Spy start failed (%s): %s", type(exc).__name__, exc)
                if self._kids_callback_is_current(generation):
                    with self._kids_lock:
                        motion_started = self._kids_ispy_motion_started
                    self.stop_kids_mode(
                        reason="ispy_start_failed", fold=motion_started, expected_generation=generation,
                    )
                raise
        if greet:
            try:
                if ispy_target is not None and not self._kids_callback_is_current(generation):
                    raise RuntimeError("I Spy start was cancelled before its clue")
                self.queue_announcement(
                    ispy_target.clue(profile.language) if ispy_target else greeting,
                    behavior="wake_and_stay",
                    kids_listen_session_id=started_session_id,
                    kids_voice_session_id=started_session_id,
                    kids_speech_lines=({"kind": "ispy_clue"},) if ispy_target else lines,
                    kids_generation=generation,
                )
            except Exception:
                self.stop_kids_mode(
                    reason="start_failed", fold=self._kids_ispy_motion_started,
                    expected_generation=generation,
                )
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
            # Discard a consent-bound readiness frame before enabling torque or searching.
            self._capture_camera_jpeg(kids_generation=generation)
            with self._motor_transition_lock:
                with self._kids_lock:
                    if (
                        not self._kids_callback_is_current(generation)
                        or self.stop_event.is_set()
                        or self._privacy_requested.is_set()
                        or self._effective_power_mode() in {"meeting", "sleep"}
                    ):
                        raise RuntimeError("I Spy search was cancelled before wake")
                    self._kids_ispy_motion_started = True
                self.set_power_mode("awake", cancel_announcements=False)
                self._assert_ispy_current(generation)
                if self._motors_enabled is not True or self._head_safely_folded:
                    raise RuntimeError("I Spy requires a successful Awake transition")
                previous_body_yaw = 0.0
                for body_yaw, capture in scan_route:
                    self._assert_ispy_current(generation)
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
                    if self._kids_generation == generation:
                        self._kids_camera_active = False
                for body_yaw in return_route:
                    self._assert_ispy_current(generation)
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
                if self._kids_generation == generation:
                    self._kids_camera_active = False
            self._assert_ispy_current(generation)
            if client is None:
                client = self._new_bridge_client(self.config_loader())
            self._assert_ispy_current(generation)
            self._set_status("thinking", "Choosing a safe I Spy object")
            target = client.select_ispy_target(
                frames,
                session_id=session_id,
                age_band=profile.age_band,
                language=profile.language,
            )
            try:
                self._assert_ispy_current(generation)
            except RuntimeError:
                try:
                    client.cancel_ispy_session(session_id)
                except Exception:
                    _LOGGER.warning("Could not delete a cancelled I Spy bridge session")
                raise RuntimeError("I Spy search was cancelled") from None
            with self._kids_lock:
                self._kids_ispy_player_motion_index = 0
            return target
        finally:
            with self._kids_lock:
                if self._kids_generation == generation:
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
            self.stop_kids_mode(reason="ispy_round_failed", fold=True, expected_generation=generation)
            raise

    def _assert_ispy_current(self, generation: int) -> None:
        """Revalidate after every blocking boundary, including before provider handoff."""
        if (
            not self._kids_callback_is_current(generation)
            or self.stop_event.is_set()
            or self._privacy_requested.is_set()
            or self._effective_power_mode() in {"meeting", "sleep"}
        ):
            raise RuntimeError("I Spy search was cancelled")

    def _kids_callback_is_current(self, generation: int) -> bool:
        with self._kids_lock:
            return (self._kids_active and generation == self._kids_generation
                    and time.monotonic() < self._kids_ends_at)

    def _warn_kids_mode(self, generation: int) -> None:
        with self._kids_lock:
            if not self._kids_active or generation != self._kids_generation or self._kids_profile is None:
                return
            language = self._kids_profile.language
            session_id = self._kids_session_id
        try:
            self._request_conversation_stop()
            self._clear_streamed_audio()
            self.queue_announcement(
                text=kids_message(language, "five_minutes"),
                repeat=1,
                pause_seconds=0.0,
                behavior="voice_only",
                kids_voice_session_id=session_id,
                kids_speech_lines=({"kind": "notice", "key": "five_minutes"},),
                kids_generation=generation,
            )
        except Exception:
            _LOGGER.exception("Could not play the Kids Mode five-minute warning")

    def _expire_kids_mode(self, generation: int) -> None:
        with self._kids_lock:
            if not self._kids_active or generation != self._kids_generation:
                return
        try:
            self.stop_kids_mode(reason="time_limit", fold=True, expected_generation=generation)
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

    def stop_kids_mode(
        self, *, reason: str = "parent", fold: bool = True, expected_generation: int | None = None,
        expected_play_id: str | None = None,
    ) -> dict[str, object]:
        """End Kids Mode immediately, cancel its voice/motion, and optionally fold safely."""
        with self._kids_lock:
            # Admission and invalidation are one atomic step under _kids_lock.
            if expected_generation is not None and self._kids_generation != expected_generation:
                return dict(self.status()["kids_mode"])
            if expected_play_id is not None and (
                not expected_play_id or not self._kids_active or self._kids_play_id != expected_play_id
            ):
                return dict(self.status()["kids_mode"])
            self._kids_play_id = ""
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
            self._kids_input_token = secrets.token_hex(16)
            self._kids_first_listen = None
            stopped_generation = self._kids_generation
            self._kids_active = False
            self._kids_camera_active = False
            self._kids_locked = False
            self._kids_profile = None
            self._kids_ends_at = 0.0
            self._kids_session_id = ""
            self._kids_last_end_reason = reason[:40]
            self._kids_last_fold_succeeded = None
            self._reset_kids_host("", ())
            # This cancellation does not acquire the motor lock; invalidate/cancel
            # before waiting for the scan to release it. New admission waits on both.
            cancel_move = getattr(self.robot, "cancel_move", None)
            if was_active and callable(cancel_move):
                try:
                    cancel_move()
                except Exception:
                    _LOGGER.warning("Could not cancel the active Kids Mode movement", exc_info=True)
        with self._motor_transition_lock:
            with self._kids_lock:
                if self._kids_generation != stopped_generation:
                    return dict(self.status()["kids_mode"])
            return self._finish_kids_stop(reason, fold, was_active, ended_session_id, ispy_session_id)

    def _finish_kids_stop(
        self, reason: str, fold: bool, was_active: bool, ended_session_id: str, ispy_session_id: str,
    ) -> dict[str, object]:
        """Complete current teardown under the motor lock, never across a replacement."""
        if ended_session_id:
            self._notify_bridge_kids_session_async(ended_session_id, active=False)
        self._request_conversation_stop()
        self._cancel_announcements(clear_queue=True)
        self._clear_streamed_audio()

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

    def _invalidate_ispy_input(self) -> None:
        with self._kids_lock, self._conversation_stop_generation_lock:
            self._kids_input_token = secrets.token_hex(16)
            self._kids_input_stop_generation = self._conversation_stop_generation

    def _ispy_typed_session_is_current(self, session_id: str) -> bool:
        """Typed input: an I Spy session, or any phase of a hosted play session (same capability)."""
        with self._kids_lock:
            return (
                self._kids_active and self._kids_session_id == session_id
                and self._kids_profile is not None
                and (self._kids_profile.activity == "ispy" or self._kids_profile.host)
                and self._kids_host_phase != "transition"
                and time.monotonic() < self._kids_ends_at
                and not self._kids_camera_active
            )

    def ispy_input_context(self) -> dict[str, object]:
        """A RAM-only input capability, never a private session/target/transcript."""
        with self._kids_lock:
            available = (
                self._ispy_typed_session_is_current(self._kids_session_id)
                and not self.stop_event.is_set() and not self._privacy_requested.is_set()
                and self._effective_power_mode() not in {"meeting", "sleep", "standby"}
                and self._motors_enabled is True
                and not self._voice_activity_lock.locked()
                and not self._announcement_active.is_set()
            )
            with self._conversation_stop_generation_lock:
                if self._kids_input_stop_generation != self._conversation_stop_generation:
                    self._kids_input_token = secrets.token_hex(16)
                    self._kids_input_stop_generation = self._conversation_stop_generation
            # While a hosted game switch or camera search runs, the same one-use token admits only "stop".
            stop_only = not available and self._hosted_stop_only_open()
            context: dict[str, object] = {
                "available": available,
                "token": self._kids_input_token if available or stop_only else "",
            }
            if stop_only:
                context["stop_only"] = True
            return context

    def _hosted_stop_only_open(self) -> bool:
        """Caller holds _kids_lock."""
        return (
            self._kids_active and self._kids_profile is not None and self._kids_profile.host
            and (self._kids_host_phase == "transition" or self._kids_camera_active)
            and time.monotonic() < self._kids_ends_at
            and not self.stop_event.is_set() and not self._privacy_requested.is_set()
        )

    def _submit_hosted_typed_stop(self, text: str, token: str) -> bool:
        """An explicit typed Stop during a switch/search: needs no voice slot or motor lock, so it wins."""
        with self._kids_lock:
            language = self._kids_profile.language if self._kids_profile is not None else ""
        if parse_host_command(text, phase="playing", language=language).kind != "stop":
            return False
        with self._kids_lock:
            if (
                not self._hosted_stop_only_open()
                or not token or not secrets.compare_digest(token, self._kids_input_token)
            ):
                return False
            self._kids_input_token = secrets.token_hex(16)  # single use; no replay
            play_id = self._kids_play_id
        self._request_conversation_stop()
        threading.Thread(
            target=self._kids_host_child_stop, args=(play_id,), name="kids-host-stop", daemon=True,
        ).start()
        return True

    def submit_ispy_guess(self, text: str, token: str) -> dict[str, object]:
        """Serialize typed input with spoken turns; never wake, capture or acquire new authority."""
        if not isinstance(text, str) or not text.strip() or len(text) > 80:
            raise ValueError("Enter an I Spy guess of 1–80 characters")
        if self._submit_hosted_typed_stop(text, token):
            return {"accepted": True}
        if not self._voice_activity_lock.acquire(blocking=False):
            raise ISpyInputUnavailable("Wait until Reachy finishes the current turn")
        try:
            with self._kids_lock:
                admission_stop_generation = self._kids_input_stop_generation
            # Config I/O occurs outside locks; Stop during this wait invalidates admission.
            base = self.config_loader()
            with self._motor_transition_lock, self._kids_lock:
                if (
                    not self._ispy_typed_session_is_current(self._kids_session_id)
                    or not token or not secrets.compare_digest(token, self._kids_input_token)
                    or self.stop_event.is_set() or self._privacy_requested.is_set()
                    or self._effective_power_mode() in {"meeting", "sleep", "standby"}
                    or self._motors_enabled is not True or self._announcement_active.is_set()
                ):
                    raise ISpyInputUnavailable("This I Spy turn is no longer available; refresh and try again")
                session_id = self._kids_session_id
                config = replace(self._kids_voice_config(base), continuous_conversation=False, barge_in_enabled=False)
                # Do not hold this cancellation lock across power/config/network work.
                with self._conversation_stop_generation_lock:
                    if admission_stop_generation != self._conversation_stop_generation:
                        raise ISpyInputUnavailable("Stop invalidated this I Spy submission")
                    # A queued spoken wake cannot reset this immutable owner generation.
                    self._kids_input_token = secrets.token_hex(16)
                    self._typed_stop_generation = admission_stop_generation
                    self._conversation_stop_requested.clear()
                    self._turn_stop_generation = admission_stop_generation
            self._kids_host_typed_handled = False
            self._run_conversation(config, typed_input=text.strip())
            if self._kids_host_typed_handled:
                # The game master answered (switch, help, choice or stop); its own line follows.
                return {"accepted": True}
            if (
                not self._ispy_typed_session_is_current(session_id)
                or self._turn_stop_requested() or self._privacy_requested.is_set()
                or self.stop_event.is_set()
            ):
                raise ISpyInputUnavailable("The I Spy turn ended before the reply completed")
            return {"accepted": True}
        finally:
            with self._conversation_stop_generation_lock:
                self._typed_stop_generation = None
            self._voice_activity_lock.release()

    # ---- Game master (host) -------------------------------------------------------------------
    # The host owns one outer session (deadline, Stop, privacy, fold); each game is an adapter run
    # under its own bridge session id and Kids generation, so a switch invalidates every in-flight
    # reply, stream, camera capture, first-listen ticket and bridge response of the old game.

    def _reset_kids_host(self, phase: str, choices: tuple[str, ...]) -> None:
        """Caller holds _kids_lock."""
        self._kids_host_phase = phase
        self._kids_host_activity = ""
        self._kids_host_choices = tuple(choices)
        self._kids_host_notice = ""
        self._kids_host_round_turns = 0
        self._kids_host_reprompts = 0
        self._kids_host_offer_index = 0

    def _kids_host_public(self) -> dict[str, object]:
        """Caller holds _kids_lock. Categorical owner-safe host state only."""
        enabled = bool(self._kids_active and self._kids_profile is not None and self._kids_profile.host)
        return {
            "enabled": enabled,
            "phase": self._kids_host_phase if enabled else "",
            "activity": self._kids_host_activity if enabled else "",
            "choices": list(self._kids_host_choices) if enabled else [],
            "notice": self._kids_host_notice if enabled else "",
            "available": list(available_activities(camera_consent=self._kids_profile.camera_consent))
            if enabled and self._kids_profile is not None else [],
        }

    def _kids_host_current(self, generation: int) -> bool:
        """Caller holds _kids_lock."""
        return (
            self._kids_active and generation == self._kids_generation
            and self._kids_profile is not None and self._kids_profile.host
            and time.monotonic() < self._kids_ends_at
            and not self.stop_event.is_set() and not self._privacy_requested.is_set()
            and self._effective_power_mode() not in {"meeting", "sleep"}
        )

    def _rearm_kids_timers(self, generation: int) -> None:
        """Caller holds _kids_lock. Keep the original monotonic deadline across a game switch."""
        for timer in (self._kids_timer, self._kids_warning_timer):
            if timer is not None and timer is not threading.current_thread():
                timer.cancel()
        remaining = max(0.0, self._kids_ends_at - time.monotonic())
        timer = threading.Timer(remaining, self._expire_kids_mode, args=(generation,))
        timer.daemon = True
        self._kids_timer = timer
        timer.start()
        self._kids_warning_timer = None
        if remaining > 301.0:  # the five-minute warning has not been due yet
            warning = threading.Timer(remaining - 300.0, self._warn_kids_mode, args=(generation,))
            warning.daemon = True
            self._kids_warning_timer = warning
            warning.start()

    def _kids_host_speak(
        self, text: str, generation: int, *, phase: str, lines: tuple[dict[str, str], ...], listen: bool = True,
    ) -> bool:
        """Queue one fixed host line for exactly this generation; Stop/End/privacy always win.

        Queued under the motor and Kids locks, so a concurrent End either clears it from the queue
        or makes this generation stale before it is queued. The first-listen ticket is re-based on a
        fresh stop receipt, so the child can answer without a wake phrase.
        """
        with self._motor_transition_lock, self._kids_lock:
            if not self._kids_host_current(generation):
                return False
            self._kids_host_phase = phase
            self._kids_first_listen = None
            receipt = self._request_conversation_stop()
            self._kids_start_stop_generation = receipt if isinstance(receipt, int) else -1
            self.queue_announcement(
                text,
                behavior="wake_and_stay",
                kids_listen_session_id=self._kids_session_id if listen else "",
                kids_voice_session_id=self._kids_session_id,
                kids_speech_lines=lines,
                kids_generation=generation,
            )
            return True

    def kids_host_select(self, activity: str, *, camera_consent: bool = False) -> dict[str, object]:
        """Caregiver direct selection inside a hosted session (owner route). May grant I Spy consent."""
        with self._kids_lock:
            profile = self._kids_profile
            if not self._kids_active or profile is None or not profile.host:
                raise RuntimeError("No hosted Kids play session is active")
            if activity not in available_activities(camera_consent=True):
                raise ValueError("Unsupported Kids Mode activity")
            if activity == "ispy" and not (profile.camera_consent or camera_consent):
                raise ValueError("Camera opt-in is required for I Spy")
            if activity == "ispy" and camera_consent and not profile.camera_consent:
                self._kids_profile = profile = replace(profile, camera_consent=True)
            generation = self._kids_generation
        self._kids_host_choose(activity, generation)
        return dict(self.status()["kids_mode"])  # type: ignore[arg-type]

    def _kids_host_choose(self, activity: str, generation: int) -> None:
        """Start ``activity`` as a fresh round, or explain the caregiver camera step for I Spy."""
        with self._kids_lock:
            if not self._kids_host_current(generation) or self._kids_profile is None:
                return
            profile = self._kids_profile
            if activity == "ispy" and not profile.camera_consent:
                # A child's voice never authorizes the camera; tell them and the caregiver precisely.
                self._kids_host_notice = "camera_consent_needed"
                phase = self._kids_host_phase or "lobby"
                text = host_line(profile.language, "camera_needed")
            else:
                text = ""
                self._kids_host_phase = "transition"
                self._kids_host_notice = ""
        if text:
            self._kids_host_speak(text, generation, phase=phase, lines=_host_selector("camera_needed"))
            return
        threading.Thread(
            target=self._kids_host_transition, args=(activity, generation),
            name="kids-host-transition", daemon=True,
        ).start()

    def _kids_host_transition(self, activity: str, expected_generation: int) -> None:
        """Switch games inside the same outer session; never extends the session deadline."""
        cancel_move = getattr(self.robot, "cancel_move", None)
        with self._motor_transition_lock, self._kids_lock:
            if not self._kids_host_current(expected_generation) or self._kids_profile is None:
                return
            old_session_id = self._kids_session_id
            old_ispy = self._kids_profile.activity == "ispy" and bool(self._kids_host_activity)
            self._kids_generation += 1
            generation = self._kids_generation
            self._kids_input_token = secrets.token_hex(16)
            self._kids_first_listen = None
            self._kids_camera_active = False
            self._kids_ispy_player_motion_index = 0
            self._kids_ispy_motion_started = False
            self._kids_session_id = new_session_id = "kids-" + secrets.token_hex(16)
            self._kids_profile = profile = replace(self._kids_profile, activity=activity)
            self._kids_host_phase = "transition"
            self._kids_host_activity = activity
            self._kids_host_choices = ()
            self._kids_host_round_turns = 0
            self._kids_host_reprompts = 0
            self._rearm_kids_timers(generation)
            receipt = self._request_conversation_stop()
            self._kids_start_stop_generation = receipt if isinstance(receipt, int) else -1
            if old_ispy and callable(cancel_move):
                try:
                    cancel_move()
                except Exception:
                    _LOGGER.warning("Could not cancel the previous I Spy movement", exc_info=True)
        self._cancel_announcements(clear_queue=True)
        self._clear_streamed_audio()
        self._notify_bridge_kids_session_async(old_session_id, active=False)
        if old_ispy:
            threading.Thread(
                target=self._cancel_ispy_bridge_session, args=(old_session_id,),
                name="kids-ispy-cancel", daemon=True,
            ).start()
        self._notify_bridge_kids_session_async(new_session_id, active=True)
        intro = host_line(profile.language, "start", a=activity)
        if activity == "ispy":
            try:
                target = self._prepare_ispy_round(generation, profile)
            except Exception as exc:
                _LOGGER.warning("Hosted I Spy round failed (%s)", type(exc).__name__)
                if self._kids_callback_is_current(generation):
                    with self._kids_lock:
                        motion_started = self._kids_ispy_motion_started
                    self.stop_kids_mode(reason="ispy_start_failed", fold=motion_started,
                                        expected_generation=generation)
                return
            text = f"{intro} {target.clue(profile.language)}"
            lines = (*_host_selector("start", a=activity), {"kind": "ispy_clue"})
        else:
            text = f"{intro} {kids_greeting(replace(profile, nickname=''))}"
            lines = (*_host_selector("start", a=activity), {"kind": "greeting"})
        self._kids_host_speak(text, generation, phase="playing", lines=lines)

    def _kids_host_offer(self, generation: int, *, finished_activity: str) -> bool:
        with self._kids_lock:
            if not self._kids_host_current(generation) or self._kids_profile is None:
                return False
            profile = self._kids_profile
            other = suggest_other(
                finished_activity, camera_consent=profile.camera_consent, turn=self._kids_host_offer_index,
            )
            self._kids_host_offer_index += 1
            self._kids_host_choices = ("again", other)
            self._kids_host_reprompts = 0
        return self._kids_host_speak(
            host_line(profile.language, "offer", a=finished_activity, b=other), generation, phase="offer",
            lines=_host_selector("offer", a=finished_activity, b=other),
        )

    def _kids_host_handle_transcript(self, session_id: str, transcript: str, *, typed: bool = False) -> bool:
        """Return True when the host consumed this child input (the game never sees it)."""
        with self._kids_lock:
            profile = self._kids_profile
            if (
                not self._kids_active or profile is None or not profile.host
                or session_id != self._kids_session_id
            ):
                return False
            generation = self._kids_generation
            play_id = self._kids_play_id
            phase = self._kids_host_phase
            activity = self._kids_host_activity
            choices = self._kids_host_choices
        command = parse_host_command(transcript, phase=phase, language=profile.language)
        if phase == "transition" and command.kind != "stop":
            if typed:
                self._kids_host_typed_handled = True
            return True  # the next game is starting; nothing reaches the old or new game
        if phase == "playing" and command.kind in {"none", "again", "no"}:
            return False
        if phase == "playing" and command.kind == "switch" and command.activity == activity:
            return False  # e.g. "I spy" inside I Spy is a game move, not a switch
        if typed:
            self._kids_host_typed_handled = True
        if command.kind == "stop" or (phase != "playing" and command.kind == "no"):
            # Stop is dominant: cancel the turn now and end the outer session without persuasion.
            self._request_conversation_stop()
            threading.Thread(
                target=self._kids_host_child_stop, args=(play_id,), name="kids-host-stop", daemon=True,
            ).start()
            return True
        language = profile.language
        if command.kind == "switch":
            self._kids_host_choose(command.activity, generation)
        elif command.kind == "again" and phase in {"offer", "waiting"} and activity:
            self._kids_host_choose(activity, generation)
        elif command.kind == "different":
            options = [c for c in choices if c not in {"again", activity}] or [
                suggest_other(activity, camera_consent=profile.camera_consent)
            ]
            self._kids_host_choose(options[0], generation)
        elif command.kind == "help" and phase == "playing":
            self._kids_host_speak(
                host_line(language, "help_playing"), generation, phase="playing",
                lines=_host_selector("help_playing"),
            )
        else:
            with self._kids_lock:
                if not self._kids_host_current(generation):
                    return True
                if command.kind != "help":
                    self._kids_host_reprompts += 1
                reprompts = self._kids_host_reprompts
            first, second = choices if len(choices) == 2 else lobby_choices(camera_consent=profile.camera_consent)
            first_name = activity if first == "again" else first
            if reprompts > MAX_CHOICE_REPROMPTS:
                # Bounded: stop asking, wait quietly; a game name, wake word or caregiver can resume.
                self._kids_host_speak(
                    host_line(language, "waiting"), generation, phase="waiting", listen=False,
                    lines=_host_selector("waiting"),
                )
            else:
                key = "offer_retry" if first == "again" and command.kind != "help" else "help"
                self._kids_host_speak(host_line(language, key, a=first_name, b=second), generation,
                                      phase=phase or "lobby", lines=_host_selector(key, a=first_name, b=second))
        return True

    def _kids_host_child_stop(self, play_id: str) -> None:
        """Bound to the outer play session, so a concurrent game switch cannot drop it."""
        try:
            self.stop_kids_mode(reason="child_stop", fold=True, expected_play_id=play_id)
        except Exception:
            _LOGGER.exception("Kids play session stop could not complete the safe fold")

    def _kids_host_after_reply(self, session_id: str, game_state: str) -> bool:
        """After a spoken game reply: on a real finished outcome or the turn bound, offer choices."""
        with self._kids_lock:
            profile = self._kids_profile
            if (
                not self._kids_active or profile is None or not profile.host
                or session_id != self._kids_session_id or self._kids_host_phase != "playing"
            ):
                return False
            generation = self._kids_generation
            activity = self._kids_host_activity
            self._kids_host_round_turns += 1
            finished = game_state == "finished" or self._kids_host_round_turns >= ROUND_TURN_LIMITS[activity]
        if not finished:
            return False
        return self._kids_host_offer(generation, finished_activity=activity)

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
            kids_host_enabled=profile.host,
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
            return (
                bool(session_id) and self._kids_active and self._kids_session_id == session_id
                and time.monotonic() < self._kids_ends_at
            )


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
                or self._turn_stop_requested()
                or self._privacy_requested.is_set()
                or self._effective_power_mode() in {"meeting", "sleep"}
            )

        if cancelled() or self._ack_output_quarantined():
            return False  # never stream over (or let an old flush cut) a still-blocked filler push
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
                                    self._voice_timings.mark("first_provider_audio")
                                queued_samples += int(output.size)
                                if queued_samples > self._output_sample_rate * 120:
                                    raise HermesBridgeError("Kids Mode streaming speech exceeded the playback limit")
                                self.robot.media.push_audio_sample(output)
                                # A successful SDK enqueue is not proof of audible playback.
                                self._voice_timings.mark("player_enqueued")

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

