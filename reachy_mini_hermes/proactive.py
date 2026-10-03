"""Proactive behaviour for the voice runtime: presence acknowledgement, initiative, contextual offers,
and the intentional shared-physical-context (presentation) window.

``ProactiveMixin`` is mixed into ``HermesVoiceRuntime`` and keeps its state on the runtime instance.
Every proactive action is gated by ``safety_gate`` policies and the deterministic initiative policy.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import replace
from typing import cast

from .adaptation import PreferenceLedger
from .announcements import Announcement
from .audio import encode_wav
from .config import AppConfig
from .contextual_offers import ContextualOffer, ContextualOfferState, parse_offer_response
from .hermes_client import HermesBridgeClient
from .initiative import InitiativeCandidate, InitiativeDecision, InitiativeMode, InitiativePolicy, InitiativeSettings
from .presence import PresenceObservation, PresenceState
from .presentation import IntentionalPresentationGate
from .safety_gate import (
    ANNOUNCEMENT_ACTIVE,
    PRESENCE_POLICY,
    PRESENTATION_POLICY,
    READINESS_POLICY,
    VOICE_ACTIVITY_BUSY,
)

# Keep the runtime logger name so existing log filters still match these lines.
_LOGGER = logging.getLogger("reachy_mini_hermes.runtime")

class ProactiveMixin:
    """Silent presence, initiative budgets, one-question contextual offers, and presentation windows."""

    def _init_proactive_state(self) -> None:
        self._presence = PresenceState()
        self._presence_action_active = threading.Event()
        self._initiative = InitiativePolicy(preferences=PreferenceLedger(self._preferences_path))
        self._contextual_offers = ContextualOfferState()
        self._presentation_lock = threading.RLock()
        self._presentation_generation = 0
        self._presentation_state = "idle"
        self._presentation_reason = "none"
        self._presentation_started_at = 0.0
        self._presentation_expires_at = 0.0
        self._presentation_samples = 0
        self._presentation_detections = 0
        self._presentation_gate: IntentionalPresentationGate | None = None
        self._presentation_stop_requested = threading.Event()
        self._presentation_worker: threading.Thread | None = None

    def _presentation_public_status(self, config: AppConfig | None = None) -> dict[str, object]:
        config = config or self.config_loader()
        enabled = bool(config.shared_physical_context_enabled)
        with self._presentation_lock:
            if not enabled:
                return {
                    "enabled": False,
                    "state": "disabled",
                    "reason": "disabled",
                    "visible_indicator": False,
                    "expires_seconds_remaining": 0,
                    "samples": 0,
                    "detections": self._presentation_detections,
                    "semantic_analysis": False,
                    "frames_retained": 0,
                }
            remaining = (
                max(0, int(math.ceil(self._presentation_expires_at - time.monotonic())))
                if self._presentation_state in {"starting", "watching"}
                else 0
            )
            return {
                "enabled": True,
                "state": self._presentation_state,
                "reason": self._presentation_reason,
                "visible_indicator": self._presentation_state in {"starting", "watching"},
                "expires_seconds_remaining": remaining,
                "samples": self._presentation_samples,
                "detections": self._presentation_detections,
                "semantic_analysis": False,
                "frames_retained": 0,
            }

    def _presentation_suppression_reason(self, config: AppConfig) -> str:
        reason = self._safety_gate.block_reason(READINESS_POLICY)
        if reason:
            return reason
        if not config.shared_physical_context_enabled:
            return "shared physical context is disabled"
        if not config.camera_enabled:
            return "camera access is disabled"
        if not config.contextual_offers_enabled or not config.initiative_policy_enabled:
            return "contextual offers are disabled"
        return self._safety_gate.block_reason(PRESENTATION_POLICY)

    def _presentation_window_active(self) -> bool:
        with self._presentation_lock:
            return self._presentation_state in {"starting", "watching"}

    def start_presentation_window(self) -> dict[str, object]:
        """Start one visible, local, non-semantic presentation window."""
        config = self.config_loader()
        reason = self._presentation_suppression_reason(config)
        if reason:
            raise RuntimeError(reason)
        duration = float(config.presentation_window_seconds)
        with self._presentation_lock:
            if self._presentation_state in {"starting", "watching"}:
                raise RuntimeError("A presentation window is already active")
            self._presentation_generation += 1
            generation = self._presentation_generation
            self._presentation_state = "starting"
            self._presentation_reason = "capturing_local_baseline"
            self._presentation_started_at = time.monotonic()
            self._presentation_expires_at = self._presentation_started_at + duration
            self._presentation_samples = 0
            self._presentation_stop_requested.clear()
            self._presentation_gate = None
        try:
            baseline = self._capture_camera_jpeg()
            gate = IntentionalPresentationGate(required_stable_frames=3)
            gate.begin(baseline)
            baseline = b""
        except Exception as exc:
            with self._presentation_lock:
                if generation == self._presentation_generation:
                    self._presentation_state = "error"
                    self._presentation_reason = "camera_unavailable"
                    self._presentation_expires_at = 0.0
                    self._presentation_gate = None
            raise RuntimeError(f"Could not start local presentation window: {exc}") from exc
        with self._presentation_lock:
            if generation != self._presentation_generation or self._presentation_stop_requested.is_set():
                gate.clear()
                raise RuntimeError("Presentation window was cancelled")
            self._presentation_gate = gate
            self._presentation_state = "watching"
            self._presentation_reason = "waiting_for_presented_object_or_text"
        self._start_presentation_worker(generation, gate, duration)
        return self._presentation_public_status(config)

    def _start_presentation_worker(
        self,
        generation: int,
        gate: IntentionalPresentationGate,
        duration: float,
    ) -> None:
        worker = threading.Thread(
            target=self._run_presentation_window,
            args=(generation, gate, duration),
            name="reachy-presentation",
            daemon=True,
        )
        with self._presentation_lock:
            self._presentation_worker = worker
        worker.start()

    def _run_presentation_window(
        self,
        generation: int,
        gate: IntentionalPresentationGate,
        duration: float,
    ) -> None:
        with self._presentation_lock:
            deadline = self._presentation_expires_at
        try:
            while not self._presentation_stop_requested.wait(0.5):
                if self.stop_event.is_set():
                    self.stop_presentation_window("runtime_stopping")
                    return
                with self._presentation_lock:
                    if generation != self._presentation_generation or self._presentation_state != "watching":
                        return
                if time.monotonic() >= deadline:
                    with self._presentation_lock:
                        if generation == self._presentation_generation:
                            self._presentation_state = "expired"
                            self._presentation_reason = "no_stable_presentation"
                            self._presentation_expires_at = 0.0
                            self._presentation_gate = None
                    return
                reason = self._presentation_suppression_reason(self.config_loader())
                if reason:
                    self.stop_presentation_window(reason.replace(" ", "_"))
                    return
                try:
                    jpeg = self._capture_camera_jpeg()
                    detected = self._observe_presentation_frame(generation, jpeg)
                    jpeg = b""
                except Exception:
                    _LOGGER.exception("Local presentation sampling failed")
                    self.stop_presentation_window("camera_error")
                    return
                if detected:
                    return
        finally:
            gate.clear()

    def _observe_presentation_frame(self, generation: int, jpeg: bytes) -> bool:
        with self._presentation_lock:
            if generation != self._presentation_generation or self._presentation_state != "watching":
                return False
            gate = self._presentation_gate
        if gate is None:
            return False
        try:
            detected = gate.observe(jpeg)
        except RuntimeError:
            with self._presentation_lock:
                if generation != self._presentation_generation or self._presentation_state != "watching":
                    return False
            raise
        if not detected:
            with self._presentation_lock:
                if generation == self._presentation_generation and self._presentation_state == "watching":
                    self._presentation_samples += 1
            return False
        with self._presentation_lock:
            if generation != self._presentation_generation or self._presentation_state != "watching":
                return False
            self._presentation_samples += 1
            self._presentation_detections += 1
            self._presentation_state = "detected"
            self._presentation_reason = "stable_presented_change"
            self._presentation_expires_at = 0.0
            self._presentation_gate = None
            self._presentation_stop_requested.set()
        offer = ContextualOffer(
            source="presentation",
            topic="presented_context",
            confidence=0.95,
            fingerprint=f"presentation-{generation}",
            text="I can see you are showing me something; would you like help looking at or reading it?",
            accepted_text="Okay—say Hey Hermes and ask me to look at what you are showing me.",
        )
        try:
            result = self.submit_contextual_offer(offer)
            reason = "offer_queued" if result.get("queued") is True else str(result.get("reason") or "suppressed")
        except (ValueError, RuntimeError) as exc:
            reason = f"offer_suppressed:{str(exc)[:80]}"
        with self._presentation_lock:
            if generation == self._presentation_generation:
                self._presentation_reason = reason
        return True

    def stop_presentation_window(self, reason: str = "user_stopped") -> dict[str, object]:
        """Cancel local sampling and discard every ephemeral visual feature."""
        with self._presentation_lock:
            active = self._presentation_state in {"starting", "watching"}
            self._presentation_generation += 1
            self._presentation_stop_requested.set()
            gate = self._presentation_gate
            self._presentation_gate = None
            self._presentation_expires_at = 0.0
            if active:
                self._presentation_state = "cancelled"
                self._presentation_reason = reason[:96] or "cancelled"
            if gate is not None:
                gate.clear()
        return self._presentation_public_status()

    def _presence_suppression_reason(
        self,
        config: AppConfig,
        *,
        owns_voice_activity: bool = False,
        owns_announcement: bool = False,
    ) -> str:
        """Return why silent presence motion is currently unsafe, or an empty string."""
        exempt = []
        if owns_announcement:
            exempt.append(ANNOUNCEMENT_ACTIVE)
        if owns_voice_activity:
            exempt.append(VOICE_ACTIVITY_BUSY)
        reason = self._safety_gate.block_reason(PRESENCE_POLICY, exempt=exempt)
        if reason:
            return reason
        if self._motion is None or not config.motion_enabled:
            return "motion_disabled"
        return ""

    def observe_presence(
        self,
        observation: PresenceObservation,
        *,
        allow_acknowledgement: bool = True,
    ) -> dict[str, object]:
        """Record one bounded signal and optionally perform one safe, silent acknowledgement."""
        config = self.config_loader()
        if not config.proactive_presence_enabled:
            self._presence.clear("disabled")
            return self._presence.public_status(enabled=False)
        self._presence.observe(observation)
        if not observation.occupied:
            if config.initiative_policy_enabled:
                self._evaluate_presence_initiative(observation, config, "away")
            return self._presence.public_status(enabled=True)
        if not allow_acknowledgement:
            self._presence.record_suppression(f"observed_{observation.source}")
            if config.initiative_policy_enabled:
                self._evaluate_presence_initiative(observation, config, "observation_only")
            return self._presence.public_status(enabled=True)
        if not config.presence_acknowledgement_enabled:
            self._presence.record_suppression("acknowledgement_disabled")
            if config.initiative_policy_enabled:
                self._evaluate_presence_initiative(observation, config, "acknowledgement_disabled")
            return self._presence.public_status(enabled=True)
        if not self._presence.acknowledgement_due(config.presence_acknowledgement_cooldown_seconds):
            self._presence.record_suppression("cooldown")
            if config.initiative_policy_enabled:
                self._evaluate_presence_initiative(observation, config, "cooldown")
            return self._presence.public_status(enabled=True)

        reason = self._presence_suppression_reason(config)
        initiative_decision: InitiativeDecision | None = None
        if config.initiative_policy_enabled:
            initiative_decision = self._evaluate_presence_initiative(observation, config, reason)
            if initiative_decision.outcome == "remain_silent":
                self._presence.record_suppression(initiative_decision.reason)
                return self._presence.public_status(enabled=True)
        if reason:
            self._presence.record_suppression(reason)
            return self._presence.public_status(enabled=True)
        if not self._voice_activity_lock.acquire(blocking=False):
            if initiative_decision is not None:
                self._initiative.cancel(initiative_decision, "voice_active")
            self._presence.record_suppression("voice_active")
            return self._presence.public_status(enabled=True)
        try:
            # Recheck after acquiring the voice owner slot so a concurrent wake,
            # power transition, Kids transition, or explicit action cannot race.
            with self._motor_transition_lock:
                with self._kids_lock:
                    reason = self._presence_suppression_reason(config, owns_voice_activity=True)
                    actions = self._actions
                    if reason:
                        if initiative_decision is not None:
                            self._initiative.cancel(initiative_decision, reason)
                        self._presence.record_suppression(reason)
                    elif actions is None:
                        if initiative_decision is not None:
                            self._initiative.cancel(initiative_decision, "runtime_not_ready")
                        self._presence.record_suppression("runtime_not_ready")
                    else:
                        self._presence_action_active.set()
                        arguments: dict[str, object] = {}
                        if observation.direction_degrees is not None:
                            arguments["direction_degrees"] = observation.direction_degrees
                        queued = actions.enqueue(
                            "acknowledge_presence",
                            arguments,
                            hold_pose=True,
                            reject_if_busy=True,
                        )
                        if queued.get("accepted"):
                            self._presence.reserve_acknowledgement()
                            if initiative_decision is not None:
                                self._initiative.commit(initiative_decision)
                        else:
                            self._presence_action_active.clear()
                            queue_reason = str(queued.get("code") or "motion_failed")
                            if initiative_decision is not None:
                                self._initiative.cancel(initiative_decision, queue_reason)
                            self._presence.record_suppression(queue_reason)
        finally:
            self._voice_activity_lock.release()
        return self._presence.public_status(enabled=True)

    @staticmethod
    def _initiative_settings(config: AppConfig) -> InitiativeSettings:
        return InitiativeSettings(
            enabled=config.initiative_policy_enabled,
            mode=cast(InitiativeMode, config.initiative_mode),
            quiet_hours_enabled=config.initiative_quiet_hours_enabled,
            quiet_hours_start=config.initiative_quiet_hours_start,
            quiet_hours_end=config.initiative_quiet_hours_end,
            hourly_budget=config.initiative_hourly_budget,
            daily_budget=config.initiative_daily_budget,
            topic_cooldown_seconds=config.initiative_topic_cooldown_seconds,
            duplicate_window_seconds=config.initiative_duplicate_window_seconds,
            dismissal_backoff_seconds=config.initiative_dismissal_backoff_seconds,
        )

    def _evaluate_presence_initiative(
        self,
        observation: PresenceObservation,
        config: AppConfig,
        suppression_reason: str,
    ) -> InitiativeDecision:
        return self._initiative.evaluate(
            InitiativeCandidate(
                topic="office_presence",
                category="presence",
                requested_outcome="physical_acknowledgement",
                confidence=observation.confidence,
                attentive=observation.attentive,
                fingerprint=f"presence:{observation.source}:{'attentive' if observation.attentive else 'present'}",
            ),
            self._initiative_settings(config),
            suppression_reason=suppression_reason,
        )

    def evaluate_initiative_candidate(
        self,
        candidate: InitiativeCandidate,
        *,
        commit: bool = False,
    ) -> InitiativeDecision:
        """Evaluate a sanitized future offer without generating or speaking content."""
        config = self.config_loader()
        decision = self._initiative.evaluate(
            candidate,
            self._initiative_settings(config),
            suppression_reason=self._presence_suppression_reason(config),
        )
        if commit and decision.outcome != "remain_silent":
            self._initiative.commit(decision)
        return decision

    def submit_contextual_offer(self, offer: ContextualOffer) -> dict[str, object]:
        """Evaluate and queue one read-only spoken offer from trusted structured context."""
        config = self.config_loader()
        with self._agent_lock:
            agent_active = self._capability_profile == "agent"
        suppression_reason = self._presence_suppression_reason(config)
        if not config.contextual_offers_enabled:
            suppression_reason = suppression_reason or "contextual_offers_disabled"
        elif not agent_active:
            suppression_reason = suppression_reason or "agent_profile_inactive"
        decision = self._initiative.evaluate(
            InitiativeCandidate(
                topic=offer.topic,
                category=offer.source,
                requested_outcome="offer_candidate",
                confidence=offer.confidence,
                fingerprint=offer.fingerprint,
            ),
            self._initiative_settings(config),
            suppression_reason=suppression_reason,
        )
        if decision.outcome != "offer_candidate":
            return {
                "ok": True,
                "queued": False,
                "decision": decision.outcome,
                "reason": decision.reason,
            }
        token = 0
        try:
            token = self._contextual_offers.queue(
                offer,
                response_window_seconds=config.contextual_offer_response_window_seconds,
            )
            queued = self.queue_announcement(
                offer.text,
                behavior="voice_only",
                contextual_offer_token=token,
            )
        except Exception:
            if token:
                self._contextual_offers.cancel(token, "queue_failed")
            self._initiative.cancel(decision, "queue_failed")
            raise
        if not self._initiative.commit(decision):
            self._contextual_offers.cancel(token, "stale_decision")
            raise RuntimeError("Contextual offer eligibility became stale")
        return {
            **queued,
            "decision": "offer_candidate",
            "reason": "committed",
            "token": token,
        }

    def respond_to_contextual_offer(self, token: int, response: str) -> dict[str, object]:
        """Record one exact phone/voice response without performing a consequential action."""
        result = self._contextual_offers.respond(token, response)
        offer = self._contextual_offers.current_offer(token)
        config = self.config_loader()
        if response == "yes":
            self._initiative.record_welcomed(offer.topic, category=offer.source)
            self.queue_announcement(str(result["accepted_text"]), behavior="voice_only")
        elif response == "later":
            self._initiative.record_snoozed(offer.source)
        else:
            self._initiative.record_dismissal(offer.topic, self._initiative_settings(config), category=offer.source)
        return {"ok": True, "token": token, "response": response, "action_executed": False}

    def cancel_contextual_offer(self, reason: str = "disabled") -> bool:
        """Cancel only the active contextual offer, preserving unrelated announcements."""
        token = self._contextual_offers.cancel_active(reason)
        if not token:
            return False
        stop_playback = False
        with self._announcement_state_lock:
            current = self._announcement_current
            if current is not None and current.contextual_offer_token == token:
                current.cancel_event.set()
                stop_playback = self._announcement_playing.is_set()
        if stop_playback:
            try:
                play_sound = getattr(getattr(self.robot, "media", None), "play_sound", None)
                if callable(play_sound):
                    play_sound(str(self.assets / "silence.wav"))
            except Exception:
                _LOGGER.debug("Could not stop contextual-offer playback", exc_info=True)
            self._clear_streamed_audio()
        return True

    def _capture_contextual_offer_response(
        self,
        item: Announcement,
        client: HermesBridgeClient,
        config: AppConfig,
    ) -> None:
        """Listen once for an explicit yes/no after a successfully spoken offer."""
        token = item.contextual_offer_token
        if not token or not self._contextual_offers.mark_spoken(token):
            return
        if not self._contextual_offers.begin_listening(token):
            return
        if self._announcement_item_cancelled(item):
            self._contextual_offers.cancel(token, "cancelled")
            return
        self._set_status("listening", "Waiting for a yes or no response")
        self._play_asset("listening.wav")
        self._discard_audio(0.34)
        response_config = replace(
            config,
            initial_speech_timeout_seconds=config.contextual_offer_response_window_seconds,
            max_utterance_seconds=4.0,
            end_silence_seconds=min(config.end_silence_seconds, 0.6),
        )
        endpoint = self._record_utterance(
            response_config,
            should_stop=lambda: self._announcement_item_cancelled(item)
            or self._privacy_requested.is_set()
            or self._effective_power_mode() != "awake",
        )
        if (
            not endpoint.speech_detected
            or endpoint.samples.size == 0
        ):
            self._contextual_offers.finish_listening(token)
            return
        if (
            self._announcement_item_cancelled(item)
            or self._privacy_requested.is_set()
            or self._effective_power_mode() != "awake"
        ):
            self._contextual_offers.cancel(token, "cancelled")
            return
        transcript = client.transcribe(encode_wav(endpoint.samples, 16000))
        response = parse_offer_response(transcript)
        if response == "unknown":
            self._contextual_offers.cancel(token, "unrecognized_response")
            return
        try:
            self.respond_to_contextual_offer(token, response)
        except RuntimeError:
            # The trusted phone may have answered while local transcription ran.
            _LOGGER.info("Contextual offer response was already resolved")
