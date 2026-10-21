"""Bounded adult Everyday actions shared by all voice transports.

A temporary camera session is an app capture lease, not a promise that the daemon's
shared camera hardware is powered off. It never starts tracking, records a video,
changes saved camera settings or uploads frames without a separate look request.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import HOUSEHOLD_SETUP_VERSION, AppConfig
from .look_intent import direct_action_intent, direct_control_intent, requests_camera_session


def local_specs() -> list[dict]:
    try:
        from .bridge.everyday_manifest import local_tool_specs
    except ImportError:
        from companion.everyday_manifest import local_tool_specs
    return local_tool_specs()


def completed_everyday_call(kind: str, payload: dict) -> tuple[str, str, object] | None:
    import json

    item = payload.get("item")
    if (
        kind != "response.output_item.done"
        or not isinstance(item, dict)
        or item.get("type") != "function_call"
        or item.get("status") != "completed"
        or item.get("name") not in {spec["name"] for spec in local_specs()}
        or not isinstance(item.get("call_id"), str)
        or not 1 <= len(item["call_id"]) <= 128
    ):
        return None
    try:
        args = json.loads(item.get("arguments") or "{}")
    except (TypeError, ValueError):
        args = None
    return item["call_id"], item["name"], args


def configured_local_tools(config: AppConfig) -> list[str]:
    names = ["get_reachy_status", "get_reachy_volume", "set_reachy_volume", "stop_reachy_camera",
             "start_reachy_radio", "stop_reachy_radio", "get_reachy_radio"]
    if config.power_tools_enabled:
        names.append("set_reachy_power_mode")
    if config.camera_enabled:
        names += ["capture_reachy_camera", "start_reachy_camera"]
    if config.robot_tools_enabled:
        names += ["move_reachy_head", "express_reachy_emotion", "dance_reachy"]
    return names


def natural_camera_requests(config: AppConfig) -> bool:
    """Owner-approved household setup lets the Everyday model choose one look for a direct request.

    Camera access off always wins; an older ``camera_enabled`` approval alone never broadens to this.
    Kids sessions never use it (their camera policies are separate).
    """
    return bool(
        getattr(config, "camera_enabled", False) is True
        and getattr(config, "camera_natural_requests_enabled", False) is True
        and getattr(config, "household_setup_version", 0) >= HOUSEHOLD_SETUP_VERSION
        and not getattr(config, "kids_mode_enabled", False)
    )


POST_LOOK_REFUSAL = (
    "Looking together is tool-free except direct spoken commands. Say one directly, for example "
    "'set the volume to 30', 'mute', 'dance', 'look left', 'stop looking' or 'go to sleep', "
    "or Stop and wake Reachy again for other help."
)
_DIRECT_ACTION_REPLY = {
    "set_reachy_volume": "Okay, the volume is changed.",
    "start_reachy_radio": "The lo-fi radio is ready. Starting it on my speaker.",
    "stop_reachy_radio": "The radio is stopping.",
    "dance_reachy": "Okay.",
    "move_reachy_head": "Okay.",
    "express_reachy_emotion": "Okay.",
}


GENERIC_CAPTURE_REFUSAL = (
    "Model-selected camera captures are refused; nothing was captured. "
    "Ask Reachy directly, for example 'look at this', for one fresh look."
)


@dataclass
class EverydayCallbackScope:
    """What the person's own words admitted for model-selected callbacks in one voice session.

    Model tool choice, arguments or purpose are never camera consent. ``transcript`` is the
    actual current user transcript; ``visual`` is set once image analysis entered the model
    conversation, after which no further household/status/robot callback is dispatched.
    """

    transcript: str = ""
    visual: threading.Event | None = field(default=None, compare=False)

    def direct_control(self, name: object, arguments: object) -> bool:
        """The robot's own transcript of this turn is exactly this call: Standby/Sleep, or a short
        direct command ("set the volume to 30", "mute", "dance", "look left", "nod") with the same
        arguments. Image text and model output can never satisfy this."""
        action = direct_control_intent(self.transcript)
        if (
            name == "set_reachy_power_mode"
            and action in {"sleep", "standby"}
            and isinstance(arguments, dict)
            and arguments.get("mode") == action
        ):
            return True
        return isinstance(arguments, dict) and direct_action_intent(self.transcript) == (name, arguments)

    def refusal(self, name: object, arguments: object = None) -> str:
        if (
            self.visual is not None
            and self.visual.is_set()
            and name != "stop_reachy_camera"
            and not self.direct_control(name, arguments)
        ):
            return POST_LOOK_REFUSAL
        if name == "capture_reachy_camera":
            return GENERIC_CAPTURE_REFUSAL
        if name == "start_reachy_camera" and not requests_camera_session(self.transcript):
            return "Ask explicitly to turn the camera on; nothing was started or captured."
        return ""


@dataclass(frozen=True)
class EverydayFence:
    stop_generation: int
    agent_generation: int
    cancelled: threading.Event | None = field(default=None, compare=False, hash=False)
    # Camera Stop invalidates old camera requests, without retiring unrelated tools.
    camera_generation: int = field(default=0, compare=False)


class _ActionCancellation:
    """Action worker checks original voice ownership again immediately before moving."""

    def __init__(self, event: threading.Event, current: Callable[[], bool]):
        self.event, self.current = event, current

    def is_set(self) -> bool:
        return self.event.is_set() or not self.current()


class EverydayRuntimeMixin:
    def _init_everyday_state(self) -> None:
        self._everyday_lock = threading.RLock()
        self._everyday_camera_until = 0.0
        self._everyday_camera_generation = 0
        self._everyday_camera_busy = 0
        self._everyday_seen: set[tuple[EverydayFence, str]] = set()
        self._everyday_pending: set[threading.Event] = set()
        self._everyday_worker_slot = threading.BoundedSemaphore(1)
        self._radio = None
        self._radio_voice_active = False
        self._radio_handoff = False

    def _stop_radio(self) -> None:
        with self._everyday_lock:
            self._radio_handoff = False
            player = self._radio
        if player is not None:
            player.stop()

    def _radio_status(self) -> dict:
        with self._everyday_lock:
            player = self._radio
        return player.status() if player is not None else {"state": "stopped", "audible_verified": False}

    def _radio_handoff_ready(self) -> bool:
        with self._everyday_lock:
            return self._radio_handoff

    def _radio_voice_begin(self) -> None:
        with self._everyday_lock:
            self._radio_voice_active = True
            self._radio_handoff = False
            player = self._radio
        if player is not None:
            player.pause()  # Refuse voice if an old SDK push has not drained.

    def _radio_voice_end(self) -> None:
        with self._everyday_lock:
            self._radio_voice_active = False
            self._radio_handoff = False
        if self._effective_power_mode() != "standby":
            self._radio_resume_idle()

    def _radio_resume_idle(self) -> None:
        # Standby's normal end-of-turn fold stops the shared SDK audio pipeline.
        # Resume audio only AFTER verified safe folding, never wake motors for radio.
        with self._motor_transition_lock:
            with self._everyday_lock:
                player = self._radio
                if player is None or player.cancelled.is_set() or not player.thread.is_alive():
                    return
                if self._radio_voice_active or self._privacy_requested.is_set():
                    return
                mode = self._effective_power_mode()
                if mode in {"meeting", "sleep"} or self._kids_active or self._kids_locked:
                    return
                if mode == "standby" and (self._motors_enabled is not False or not self._head_safely_folded):
                    player.stop()
                    return
                self._resume_playback_after_wake()  # Audio gate only, no motion helper or motor command.
                player.resume()

    def _start_radio(self, fence: EverydayFence) -> dict:
        from .radio import RadioPlayer

        # Playback outlives a normal voice session, not Stop/profile/privacy.
        # Never bind its lifetime to the transport's local_closed event.
        def allowed() -> bool:
            return bool(
                not self.stop_event.is_set() and self.control_ready
                and self._conversation_stop_generation == fence.stop_generation
                and self._capability_profile == "conversation"
                and not self._privacy_requested.is_set()
                and not self._kids_active and not self._kids_locked
                and self._effective_power_mode() not in {"meeting", "sleep"}
            )

        self._require_everyday_current(fence)
        with self._everyday_lock:
            if not allowed():
                raise RuntimeError("Radio request was stopped")
            old = self._radio
            if old is not None and old.thread.is_alive():
                if old.cancelled.is_set():
                    raise RuntimeError("Previous radio output is still stopping; try again shortly")
                self._radio_handoff = self._radio_voice_active
                return {"ok": True, **old.status()}
            player = RadioPlayer(self._playback, self._output_sample_rate, allowed=allowed)
            self._radio = player  # Stop can cancel even while the decoder connects.
        result = player.start()
        try:
            self._require_everyday_current(fence)
            with self._everyday_lock:
                if not allowed() or player.cancelled.is_set():
                    raise RuntimeError("Radio request was stopped")
                self._radio_handoff = self._radio_voice_active
            if not self._radio_voice_active:
                self._radio_resume_idle()
        except RuntimeError:
            player.stop()
            raise
        return {"ok": True, **result}

    def revoke_everyday_camera(self) -> None:
        with self._everyday_lock:
            self._everyday_camera_until = 0.0
            self._everyday_camera_generation += 1
        teardown = getattr(self, "_companion_teardown", None)
        if callable(teardown):
            teardown("camera_session_stopped")

    def _revoke_everyday_actions(self) -> None:
        self._stop_radio()
        with self._everyday_lock:
            self._everyday_camera_until = 0.0
            self._everyday_camera_generation += 1
            for cancel in self._everyday_pending:
                cancel.set()
            self._everyday_seen.clear()

    def everyday_tool_fence(self, cancelled: threading.Event | None = None) -> EverydayFence:
        with self._conversation_stop_generation_lock:
            stop = self._conversation_stop_generation
        with self._agent_lock:
            agent = self._agent_session_generation
        with self._everyday_lock:
            camera = self._everyday_camera_generation
        return EverydayFence(stop, agent, cancelled, camera)

    def _require_everyday_current(self, fence: EverydayFence) -> AppConfig:
        if (
            self.everyday_tool_fence() != fence
            or self._turn_stop_requested()
            or self.stop_event.is_set()
            or (fence.cancelled is not None and fence.cancelled.is_set())
        ):
            raise RuntimeError("Everyday action was cancelled")
        if not self.control_ready:
            raise RuntimeError("Reachy is still starting up")
        with self._kids_lock:
            if self._kids_active or self._kids_locked:
                raise RuntimeError("Everyday actions are unavailable during Kids Mode")
        with self._agent_lock:
            if self._capability_profile != "conversation":
                raise RuntimeError("This tool belongs to Everyday, not Workspace")
        if self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"}:
            raise RuntimeError("Everyday actions are blocked by privacy mode")
        return self.config_loader()

    def everyday_local_tools(self) -> list[str]:
        return configured_local_tools(self.config_loader())

    def everyday_local_capabilities(self) -> dict[str, object]:
        config = self.config_loader()
        with self._everyday_lock:
            remaining = max(0.0, self._everyday_camera_until - time.monotonic())
            busy = self._everyday_camera_busy > 0
        if not config.camera_enabled or self._privacy_requested.is_set():
            remaining = 0.0
        return {
            "tools": self.everyday_local_tools(),
            "permissions": {
                "power": config.power_tools_enabled,
                "camera": config.camera_enabled,
                "camera_on_request": config.camera_enabled,  # Older UI compatibility, not a second permission.
                "movement": config.robot_tools_enabled,
                "volume": True,
            },
            "activity": {
                "radio": self._radio_status(),
                "camera_session_active": remaining > 0,
                "camera_request_active": busy,
                "camera_remaining_seconds": math.ceil(remaining),
                "microphone_recording": bool(self._recording),
            },
            "camera_scope": "App capture session only; shared daemon camera power is not controlled here.",
        }

    @staticmethod
    def _validate_everyday_tool(name: str, arguments: object) -> dict[str, object]:
        # Import the same stdlib-only manifest used by the bridge, never its server modules.
        try:
            from .bridge.everyday_manifest import validate_local_arguments
        except ImportError:
            from companion.everyday_manifest import validate_local_arguments
        return validate_local_arguments(name, arguments)

    def execute_everyday_tool(
        self,
        name: str,
        arguments: object,
        *,
        fence: EverydayFence | None = None,
        call_id: str = "",
    ) -> dict[str, object]:
        fence = fence or self.everyday_tool_fence()
        try:
            args = self._validate_everyday_tool(name, arguments)
            config = self._require_everyday_current(fence)
            if name not in configured_local_tools(config):
                raise RuntimeError("This action is disabled in Reachy settings")
            with self._everyday_lock:
                if call_id:
                    key = (fence, call_id)
                    if key in self._everyday_seen or len(self._everyday_seen) >= 2048:
                        raise RuntimeError("Duplicate action or action limit reached")
                    self._everyday_seen.add(key)
            if name == "get_reachy_status":
                local = self.everyday_local_capabilities()
                with self._status_lock:
                    voice_state = self._status.state
                return {
                    "ok": True,
                    "power_mode": self._effective_power_mode(),
                    "microphone_recording": bool(self._recording),
                    "voice_state": voice_state,
                    "motors_enabled": self._motors_enabled,
                    **local["activity"],
                    "camera_hardware_state": "daemon_owned_unknown",
                    "available_tools": self.everyday_local_tools(),
                }
            if name == "get_reachy_volume":
                with self._motor_transition_lock, self._kids_lock:
                    self._require_everyday_current(fence)
                    result = self.speaker_volume()
                self._require_everyday_current(fence)
                return {"ok": True, **result}
            if name == "get_reachy_radio":
                return {"ok": True, **self._radio_status()}
            if name == "start_reachy_radio":
                return self._start_radio(fence)
            if name == "stop_reachy_radio":
                self._stop_radio()
                return {"ok": True, **self._radio_status()}
            if name == "set_reachy_volume":
                with self._motor_transition_lock, self._kids_lock:
                    self._require_everyday_current(fence)
                    result = self.set_speaker_volume(**args, agent=True)
                self._require_everyday_current(fence)
                return {"ok": True, **result}
            if name == "set_reachy_power_mode":
                mode = args["mode"]
                duration = args.get("duration_minutes", 30) * 60 if mode == "meeting" else 0
                with self._motor_transition_lock:
                    self._require_everyday_current(fence)
                    status = self.set_power_mode(mode, duration_seconds=duration)
                verified = status.get("power_mode") == mode
                return {
                    "ok": verified,
                    "verified": verified,
                    "mode": status.get("power_mode"),
                    "conversation_ended": mode != "awake",
                }
            if name == "stop_reachy_camera":
                self.revoke_everyday_camera()
                return {
                    "ok": True,
                    "camera_session_active": False,
                    "scope": "Everyday camera session stopped; shared daemon hardware is not powered off.",
                }
            if name in {"start_reachy_camera", "capture_reachy_camera"}:
                with self._everyday_lock:
                    camera_generation = fence.camera_generation
                    camera_deadline = self._everyday_camera_until
                    if camera_deadline <= time.monotonic():
                        camera_deadline = 0.0  # A fresh explicit one-shot look does not need a window.

                def current() -> None:
                    latest = self._require_everyday_current(fence)
                    if name not in configured_local_tools(latest):
                        raise RuntimeError("Camera permission was revoked")
                    with self._everyday_lock:
                        if camera_generation != self._everyday_camera_generation:
                            raise RuntimeError("Camera request was stopped")
                        if (
                            name == "capture_reachy_camera"
                            and camera_deadline > 0
                            and camera_deadline <= time.monotonic()
                        ):
                            raise RuntimeError("The original camera session expired; ask for a new look")

                current()
                with self._everyday_lock:
                    self._everyday_camera_busy += 1
                try:
                    # For start this is only a local readiness probe: the frame is dropped on the robot,
                    # never analysed or uploaded. Voice callbacks reach here via execute_everyday_callback.
                    jpeg = self._capture_camera_jpeg(adult_current=current)
                    current()
                    with self._status_lock:
                        self._status.camera_captures += 1
                    if name == "start_reachy_camera":
                        with self._everyday_lock:
                            if (
                                self._conversation_stop_generation != fence.stop_generation
                                or self._agent_session_generation != fence.agent_generation
                                or camera_generation != self._everyday_camera_generation
                                or (fence.cancelled is not None and fence.cancelled.is_set())
                            ):
                                raise RuntimeError("Camera session was cancelled")
                            self._everyday_camera_until = time.monotonic() + args["duration_seconds"]
                        return {
                            "ok": True,
                            "camera_session_active": True,
                            "duration_seconds": args["duration_seconds"],
                            "scope": "Ready for explicit looks; no tracking, video recording or automatic uploads.",
                        }
                    answer = self._everyday_describe_frame(
                        jpeg, args["purpose"], fence, config, camera_current=current
                    )
                    current()
                    return {"ok": True, "text": answer}
                finally:
                    with self._everyday_lock:
                        self._everyday_camera_busy -= 1
            return self._everyday_robot_action(name, args, fence)
        except (ValueError, RuntimeError, OSError) as exc:
            return {"ok": False, "error": str(exc)[:300]}

    def _everyday_describe_frame(
        self,
        jpeg: bytes,
        purpose: str,
        fence: EverydayFence,
        config: AppConfig,
        *,
        camera_current: Callable[[], None],
    ) -> str:
        camera_current()
        self._require_everyday_current(fence)
        if config.local_vision_enabled:
            with self._new_local_vision_client(config) as client:
                camera_current()
                result = client.describe(jpeg, purpose)
            camera_current()
            self._require_everyday_current(fence)
            return str(result["answer"])
        from .hermes_client import HermesBridgeClient

        context = self.agent_broker_context(explicit_private_intent=True)
        self._require_everyday_current(fence)
        client = HermesBridgeClient(config)
        try:
            camera_current()
            result = client.everyday_vision(jpeg, purpose, context=context)
        finally:
            client.close()
        self._require_everyday_current(fence)
        camera_current()
        return result

    def _everyday_robot_action(self, name: str, args: dict[str, object], fence: EverydayFence) -> dict[str, object]:
        if self._actions is None:
            raise RuntimeError("Robot action controller is unavailable")
        completed = threading.Event()
        cancelled = threading.Event()
        results = []

        def done(result):
            results.append(result)
            completed.set()

        with self._everyday_lock:
            if (
                self._conversation_stop_generation != fence.stop_generation
                or self._agent_session_generation != fence.agent_generation
            ):
                raise RuntimeError("Robot action was cancelled")
            self._everyday_pending.add(cancelled)
        try:

            def current() -> bool:
                try:
                    latest = self._require_everyday_current(fence)
                    return latest.robot_tools_enabled
                except RuntimeError:
                    return False

            admission = self._actions.enqueue(
                name,
                args,
                on_complete=done,
                cancel_event=_ActionCancellation(cancelled, current),
                reject_if_busy=True,
            )
            if not admission.get("accepted"):
                return admission
            deadline = time.monotonic() + 12
            while not completed.wait(0.05):
                self._require_everyday_current(fence)
                if time.monotonic() >= deadline:
                    raise RuntimeError("Robot action completion was not confirmed")
            self._require_everyday_current(fence)
            return results[0]
        finally:
            cancelled.set()
            with self._everyday_lock:
                self._everyday_pending.discard(cancelled)

    def execute_everyday_callback(
        self,
        name: str,
        arguments: object,
        *,
        scope: EverydayCallbackScope,
        fence: EverydayFence | None = None,
        call_id: str = "",
    ) -> dict[str, object]:
        """Admit one model-selected callback against the person's own words before any device work.

        Explicit visual help is the Companion look route in every engine; a model-selected
        capture never reaches the camera here, and starting the temporary window needs the
        person to have asked for it in this transcript.
        """
        refusal = scope.refusal(name, arguments)
        if refusal:
            return {"ok": False, "verified": False, "error": refusal}
        return self.execute_everyday_tool(name, arguments, fence=fence, call_id=call_id)

    def _voice_direct_control(self, action: str) -> str | None:
        """Apply one direct spoken control (the person's own words) through the guarded transitions.

        ``None`` means the conversation ended (Stop, or a verified Standby/Sleep, which already
        stopped the turn and folded): nothing more may be spoken. Otherwise the fixed reply text.
        Standby is requested even when the mode already reads Standby, so it still folds.
        """
        if action == "stop":
            self._request_conversation_stop()
            return None
        if action == "stop_looking":
            self.revoke_everyday_camera()
            return "Okay, I stopped looking."
        if action not in {"sleep", "standby"}:
            return None
        label = "Sleep" if action == "sleep" else "Standby"
        if not self.config_loader().power_tools_enabled:
            return f"Voice power controls are off in Reachy settings, so {label} was not changed."
        try:
            status = self.set_power_mode(action)
        except (RuntimeError, ValueError):
            status = {}
        if status.get("power_mode") == action:
            return None
        return f"I could not switch to {label}. Nothing is confirmed."

    def _voice_direct_action(self, name: str, arguments: dict[str, object]) -> str:
        """One short direct command in the person's own words, through the guarded Everyday tool path.

        Settings, Kids Mode, privacy and power gates all still apply; failure is never claimed done.
        """
        result = self.execute_everyday_tool(name, dict(arguments))
        if result.get("ok") is True:
            return _DIRECT_ACTION_REPLY.get(name, "Okay.")
        return "That did not work, so nothing is confirmed."

    def launch_everyday_tool(
        self,
        name: str,
        args: object,
        *,
        fence: EverydayFence,
        call_id: str,
        deliver: Callable[[dict[str, object]], None],
        is_current: Callable[[], bool],
        scope: EverydayCallbackScope | None = None,
    ) -> threading.Thread | None:
        """Keep camera/network/movement work off the live microphone pump."""
        # Without the caller's actual transcript no camera callback is ever admitted.
        scope = scope if scope is not None else EverydayCallbackScope()
        refusal = scope.refusal(name, args)
        if refusal:
            if is_current():
                deliver({"ok": False, "verified": False, "error": refusal})
            return None
        if name in {"get_reachy_status", "stop_reachy_camera"}:
            if is_current():
                result = self.execute_everyday_callback(name, args, scope=scope, fence=fence, call_id=call_id)
                if is_current():
                    deliver(result)
            return None
        if not self._everyday_worker_slot.acquire(blocking=False):
            if is_current():
                deliver({"ok": False, "error": "Another Reachy action is still in progress"})
            return None

        def run():
            try:
                if not is_current():
                    return
                result = self.execute_everyday_callback(name, args, scope=scope, fence=fence, call_id=call_id)
                if is_current():
                    deliver(result)
            finally:
                self._everyday_worker_slot.release()

        worker = threading.Thread(target=run, name="homebody-everyday-tool", daemon=True)
        worker.start()
        return worker
