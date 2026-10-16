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

from .config import AppConfig
from .look_intent import requests_camera_session


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
    names = ["get_reachy_status", "get_reachy_volume", "set_reachy_volume", "stop_reachy_camera"]
    if config.power_tools_enabled:
        names.append("set_reachy_power_mode")
    if config.camera_enabled or config.everyday_camera_enabled:
        names.append("capture_reachy_camera")
    if config.everyday_camera_enabled:
        names.append("start_reachy_camera")
    if config.robot_tools_enabled:
        names += ["move_reachy_head", "express_reachy_emotion", "dance_reachy"]
    return names


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

    def refusal(self, name: object) -> str:
        if self.visual is not None and self.visual.is_set() and name != "stop_reachy_camera":
            return "Looking together is tool-free; Stop and wake again for other help"
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

    def revoke_everyday_camera(self) -> None:
        with self._everyday_lock:
            self._everyday_camera_until = 0.0
            self._everyday_camera_generation += 1
        teardown = getattr(self, "_companion_teardown", None)
        if callable(teardown):
            teardown("camera_session_stopped")

    def _revoke_everyday_actions(self) -> None:
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
        if not config.everyday_camera_enabled or self._privacy_requested.is_set():
            remaining = 0.0
        return {
            "tools": self.everyday_local_tools(),
            "permissions": {
                "power": config.power_tools_enabled,
                "camera": config.camera_enabled,
                "camera_on_request": config.everyday_camera_enabled,
                "movement": config.robot_tools_enabled,
                "volume": True,
            },
            "activity": {
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
                general_camera_permission = config.camera_enabled

                def current() -> None:
                    latest = self._require_everyday_current(fence)
                    if name not in configured_local_tools(latest):
                        raise RuntimeError("Camera permission was revoked")
                    with self._everyday_lock:
                        if camera_generation != self._everyday_camera_generation:
                            raise RuntimeError("Camera request was stopped")
                        if (
                            name == "capture_reachy_camera"
                            and not (general_camera_permission and latest.camera_enabled)
                            and (not latest.everyday_camera_enabled or camera_deadline <= time.monotonic())
                        ):
                            raise RuntimeError(
                                "Start a temporary camera session before looking; the window is off or expired"
                            )

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
        refusal = scope.refusal(name)
        if refusal:
            return {"ok": False, "verified": False, "error": refusal}
        return self.execute_everyday_tool(name, arguments, fence=fence, call_id=call_id)

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
        refusal = scope.refusal(name)
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
