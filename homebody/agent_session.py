"""Agent capability profile, session generation, request lease, and activity for the voice runtime.

``AgentSessionMixin`` is mixed into ``HermesVoiceRuntime`` and keeps its state on the runtime instance.
Profile and Kids transitions take ``_kids_lock`` before ``_agent_lock``.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time

from .agent_policy import AgentPolicy
from .hermes_client import AgentBrokerContext

# Keep the runtime logger name so existing log filters still match these lines.
_LOGGER = logging.getLogger("homebody.runtime")

class AgentSessionMixin:
    """Adult Agent authority: profile switches, generation-bound requests, broker context, and activity."""

    def _init_agent_session_state(self) -> None:
        self._agent_lock = threading.RLock()
        # Agent authority is never restored across process/session boundaries;
        # an unlocked adult UI must explicitly start each fresh Agent session.
        self._capability_profile = "conversation"
        # The companion bridge can outlive this app process.  Starting every
        # process at zero made a healthy restart look older than the bridge's
        # retained lease and permanently fail with ``stale_session``.  A
        # wall-clock nanosecond epoch preserves the existing +1 invalidation
        # semantics while ordering fresh app processes after their predecessors.
        self._agent_session_generation = time.time_ns()
        self._agent_current_task = ""
        self._agent_active_request_id = ""
        self._agent_pending_approval = False
        self._agent_activity: list[dict[str, object]] = []
        self._agent_policy = AgentPolicy()

    def set_capability_profile(self, profile: str, *, adult_ui_unlocked: bool) -> dict[str, object]:
        """Switch profiles only from the unlocked adult UI and start a fresh generation."""
        profile = profile.strip().lower()
        if profile not in {"conversation", "agent"}:
            raise ValueError("Unsupported capability profile")
        if profile == "agent":
            if not adult_ui_unlocked:
                raise RuntimeError("Agent profile requires an unlocked adult UI action")
            if self._effective_power_mode() in {"meeting", "sleep"} or self._privacy_requested.is_set():
                raise RuntimeError("Agent profile is blocked by the current privacy mode")
        # Profile/Kids transitions always acquire _kids_lock before _agent_lock.
        # Holding both makes the mutual-exclusion check and profile mutation atomic.
        with self._kids_lock:
            if self._kids_active or self._kids_locked:
                raise RuntimeError("Agent profile is unavailable while Kids Mode is active or locked")
            with self._agent_lock:
                active_request_id = self._agent_active_request_id
                self._agent_session_generation += 1
                self._capability_profile = profile
                self._agent_current_task = ""
                self._agent_active_request_id = ""
                self._agent_pending_approval = False
                self._record_agent_activity_unlocked("profile_changed")
                payload = self._agent_status_unlocked()
        self._request_conversation_stop()
        if profile != "agent":
            self.cancel_contextual_offer("agent_profile_inactive")
            self.stop_presentation_window("agent_profile_inactive")
        if active_request_id:
            threading.Thread(
                target=self._cancel_remote_agent_request,
                args=(active_request_id,),
                name="reachy-agent-cancel",
                daemon=True,
            ).start()
        self._publish_remote_agent_session()
        return payload

    def cancel_agent_work(self, reason: str = "stopped") -> dict[str, object]:
        """Invalidate pending Agent work and stop voice only for teardown reasons."""
        allowed_reasons = {
            "stopped",
            "profile_changed",
            "power_standby",
            "power_meeting",
            "power_sleep",
            "kids_mode",
            "privacy",
            "emergency_stop",
            "session_changed",
        }
        # Only the internal, explicit session_changed reason may preserve the
        # voice loop. Unknown callers fail closed as a normal Stop request.
        safe_reason = reason if reason in allowed_reasons else "stopped"
        with self._agent_lock:
            active_request_id = self._agent_active_request_id
            self._agent_session_generation += 1
            self._agent_current_task = ""
            self._agent_active_request_id = ""
            self._agent_pending_approval = False
            self._record_agent_activity_unlocked(safe_reason)
            payload = self._agent_status_unlocked()
        # A newly accepted wake advances the Agent generation so stale tool
        # results cannot cross into the new voice session. It is not itself a
        # Stop request. In particular, do not clear the event here: a real
        # Stop/privacy/power/Kids cancellation may have raced with this call.
        if safe_reason != "session_changed":
            self._request_conversation_stop()
        if active_request_id:
            threading.Thread(
                target=self._cancel_remote_agent_request,
                args=(active_request_id,),
                name="reachy-agent-cancel",
                daemon=True,
            ).start()
        self._publish_remote_agent_session()
        return payload

    def _cancel_remote_agent_request(self, request_id: str) -> None:
        client = self._new_bridge_client(self.config_loader())
        try:
            client.cancel_agent_request(request_id)
        except Exception:
            _LOGGER.warning("Could not confirm remote Agent request cancellation", exc_info=True)
        finally:
            client.close()

    def _publish_remote_agent_session(self) -> None:
        """Best-effort invalidation of the Hermes-hosted device lease."""
        config = self.config_loader()
        if not config.api_key:
            return
        context = self.agent_broker_context(explicit_private_intent=False)

        def publish() -> None:
            client = self._new_bridge_client(config)
            try:
                client.establish_agent_session(context)
            except Exception:
                _LOGGER.warning("Could not publish Agent session generation", exc_info=True)
            finally:
                client.close()

        threading.Thread(target=publish, name="reachy-agent-session", daemon=True).start()

    def _establish_remote_agent_session(self, context: AgentBrokerContext) -> None:
        client = self._new_bridge_client(self.config_loader())
        try:
            client.establish_agent_session(context)
        finally:
            client.close()

    def agent_broker_context(self, *, explicit_private_intent: bool) -> AgentBrokerContext:
        """Capture one sanitized, generation-bound broker authorization context."""
        with self._kids_lock:
            kids_active = self._kids_active or self._kids_locked
            with self._agent_lock:
                profile = self._capability_profile
                generation = self._agent_session_generation
        with self._status_lock:
            state = self._status.state
            detail = self._status.detail
            bridge_healthy = self._status.bridge_healthy
            last_error = self._status.last_error
        power_mode = self._effective_power_mode()
        return AgentBrokerContext(
            capability_profile=profile,
            adult_ui_unlocked=profile == "agent",
            kids_mode_active=kids_active,
            power_mode=power_mode,
            privacy_enabled=not self._privacy_requested.is_set(),
            emergency_stop_active=False,
            robot_available=not self.stop_event.is_set() and self._audio_ready,
            session_generation=generation,
            requested_session_generation=generation,
            explicit_private_intent=explicit_private_intent,
            reachy_status={
                "observed_at": time.time(),
                "state": state,
                "detail": detail,
                "power_mode": power_mode,
                "motors_enabled": self._motors_enabled,
                "head_safely_folded": self._head_safely_folded,
                "bridge_healthy": bridge_healthy,
                "robot_action_busy": bool(self._actions and self._actions.pending_count),
                "last_error": last_error,
            },
        )

    def _begin_agent_request(self, _summary: str) -> tuple[str, AgentBrokerContext]:
        # The bridge derives T1 intent from this exact request and capability;
        # the robot never grants blanket private-read intent for an Agent session.
        context = self.agent_broker_context(explicit_private_intent=False)
        request_id = f"agent-{secrets.token_hex(16)}"
        self._establish_remote_agent_session(context)
        with self._agent_lock:
            if context.session_generation != self._agent_session_generation or self._capability_profile != "agent":
                raise RuntimeError("Agent session changed before the request started")
            if self._agent_active_request_id:
                raise RuntimeError("Another Agent request is already active")
            self._agent_active_request_id = request_id
            # Browser status is a low-trust surface: never mirror a transcript,
            # query, note name, token, or other request content into it.
            self._agent_current_task = "Processing a bounded owner request"
            self._record_agent_activity_unlocked("request_started")
        return request_id, context

    def _finish_agent_request(self, request_id: str, generation: int, *, succeeded: bool) -> bool:
        with self._agent_lock:
            current = generation == self._agent_session_generation and self._agent_active_request_id == request_id
            if not current:
                return False
            self._agent_active_request_id = ""
            self._agent_current_task = ""
            self._record_agent_activity_unlocked("request_completed" if succeeded else "request_failed")
            return True

    def agent_session_is_current(self, generation: int) -> bool:
        with self._agent_lock:
            return generation == self._agent_session_generation

    def record_agent_run_event(self, action: str, run: dict[str, object]) -> None:
        """Record only run identity/status; never persist goals, arguments, or results."""
        run_id = str(run.get("run_id") or "")[:32]
        status = str(run.get("status") or "unknown")[:32]
        with self._agent_lock:
            self._agent_activity.append(
                {
                    "event": f"run_{action}"[:48],
                    "generation": self._agent_session_generation,
                    "run_id": run_id,
                    "result_class": status,
                }
            )
            del self._agent_activity[:-20]
            if self._agent_audit is not None:
                self._agent_audit.append(
                    "agent_run",
                    reason=action,
                    session_generation=self._agent_session_generation,
                    result_class=status,
                    summary=f"{run_id}:{status}",
                )

    def _record_agent_activity_unlocked(self, event: str) -> None:
        self._agent_activity.append({"event": event, "generation": self._agent_session_generation})
        del self._agent_activity[:-20]
        if self._agent_audit is not None:
            result_classes = {
                "profile_changed": "profile_updated",
                "request_started": "running",
                "request_completed": "success",
                "request_failed": "failed",
            }
            self._agent_audit.append(
                "agent_session",
                reason=event,
                session_generation=self._agent_session_generation,
                result_class=result_classes.get(event, "cancelled"),
            )

    def _agent_status_unlocked(self) -> dict[str, object]:
        return {
            "profile": self._capability_profile,
            "session_generation": self._agent_session_generation,
            "enabled_capabilities": [
                definition.capability_id.value for definition in self._agent_policy.enabled_capabilities()
            ]
            if self._capability_profile == "agent"
            else [],
            "current_task": self._agent_current_task,
            "pending_approval": self._agent_pending_approval,
            "recent_activity": [dict(item) for item in self._agent_activity[-20:]],
        }
