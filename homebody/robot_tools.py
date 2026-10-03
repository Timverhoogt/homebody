"""Curated local embodiment tools for Reachy Mini Realtime conversations."""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, cast

_LOGGER = logging.getLogger(__name__)

ROBOT_TOOL_NAMES = frozenset(
    {
        "move_reachy_head",
        "express_reachy_emotion",
        "dance_reachy",
    }
)
_QUEUED_ROBOT_ACTION_NAMES = ROBOT_TOOL_NAMES | {
    "nudge_reachy",
    "camera_joystick",
    "acknowledge_presence",
}
_PRECISION_AXES = frozenset(
    {"x", "y", "z", "roll", "pitch", "yaw", "body_yaw", "center_head", "center_base", "center_all"}
)
_HEAD_LIMITS = {
    "x": (-15.0, 15.0),
    "y": (-20.0, 20.0),
    "z": (-20.0, 20.0),
    "roll": (-15.0, 15.0),
    "pitch": (-25.0, 25.0),
    "yaw": (-35.0, 35.0),
}
_BODY_YAW_LIMIT = (-120.0, 120.0)
_BODY_YAW_STEP_LIMIT = 60.0
_HEAD_BODY_YAW_DELTA_LIMIT = 65.0
_CAMERA_HEAD_YAW_SOFT_LIMIT = 28.0
_CAMERA_HEAD_YAW_LIMIT = 35.0
_CAMERA_PAN_STEP = 3.0
_CAMERA_TILT_STEP = 2.0
_CAMERA_BODY_STEP = 2.0
_CAMERA_STREAM_PERIOD = 1.0 / 20.0
_CAMERA_STREAM_SMOOTHING_SECONDS = 0.12
_CAMERA_STREAM_PAN_SPEED = 45.0
_CAMERA_STREAM_TILT_SPEED = 35.0


def _cancelled_action_result(name: str) -> dict[str, object]:
    if name == "camera_joystick":
        return {"ok": True, "cancelled": True, "action": name}
    return {"ok": False, "error": "Robot action was cancelled", "action": name}


_LOOK_POSES: dict[str, dict[str, float]] = {
    "left": {"yaw": 35.0},
    "right": {"yaw": -35.0},
    "up": {"pitch": -25.0},
    "down": {"pitch": 25.0},
    "up_left": {"yaw": 25.0, "pitch": -18.0},
    "up_right": {"yaw": -25.0, "pitch": -18.0},
    "down_left": {"yaw": 25.0, "pitch": 18.0},
    "down_right": {"yaw": -25.0, "pitch": 18.0},
    "center": {},
}

_EMOTION_MOVES: dict[str, str] = {
    "happy": "laughing2",
    "excited": "success2",
    "loving": "loving1",
    "grateful": "grateful1",
    "thinking": "thoughtful1",
    "confused": "confused1",
    "sad": "sad2",
    "surprised": "surprised1",
    "calm": "calming1",
    "welcoming": "welcoming2",
    "yes": "yes1",
    "no": "no1",
}

_DANCE_MOVES: dict[str, str] = {
    "short": "dance1",
    "groovy": "dance2",
    "energetic": "dance3",
}

LOOK_DIRECTIONS = tuple(_LOOK_POSES)
EMOTIONS = tuple(_EMOTION_MOVES)
DANCE_STYLES = tuple(_DANCE_MOVES)


def manual_robot_action(action: str, value: str) -> tuple[str, dict[str, object]]:
    """Translate one UI control into an allow-listed semantic robot tool call."""
    action = action.strip().lower()
    value = value.strip().lower()
    if action == "look" and value in _LOOK_POSES:
        return "move_reachy_head", {"direction": value}
    if action == "emotion" and value in _EMOTION_MOVES:
        return "express_reachy_emotion", {"emotion": value}
    if action == "dance" and value in _DANCE_MOVES:
        return "dance_reachy", {"style": value}
    raise ValueError("Unknown or unsupported manual robot action")


def manual_precision_action(
    axis: str,
    delta: float,
    *,
    body_yaw_degrees: float = 0.0,
) -> tuple[str, dict[str, object]]:
    """Validate one fine relative movement without exposing raw joints."""
    axis = axis.strip().lower()
    if axis not in _PRECISION_AXES:
        raise ValueError("Unknown precision movement axis")
    body_yaw_degrees = float(body_yaw_degrees)
    if not math.isfinite(body_yaw_degrees):
        raise ValueError("Current base yaw must be finite")
    if axis.startswith("center_"):
        return "nudge_reachy", {"axis": axis, "delta": 0.0, "body_yaw_degrees": body_yaw_degrees}
    delta = float(delta)
    step_limit = _BODY_YAW_STEP_LIMIT if axis == "body_yaw" else 10.0
    if not math.isfinite(delta) or delta == 0 or abs(delta) > step_limit:
        raise ValueError(
            f"Precision {axis} movement must be finite and between {-step_limit:g} and {step_limit:g}"
        )
    return "nudge_reachy", {
        "axis": axis,
        "delta": delta,
        "body_yaw_degrees": body_yaw_degrees,
    }


def _head_pose_components(pose: object) -> dict[str, float]:
    """Extract millimetres and XYZ Euler degrees from an SDK 4x4 pose."""
    matrix = cast(object, pose)
    r20 = float(matrix[2][0])  # type: ignore[index]
    pitch = math.asin(max(-1.0, min(1.0, -r20)))
    if abs(math.cos(pitch)) > 1e-7:
        roll = math.atan2(float(matrix[2][1]), float(matrix[2][2]))  # type: ignore[index]
        yaw = math.atan2(float(matrix[1][0]), float(matrix[0][0]))  # type: ignore[index]
    else:
        roll = math.atan2(-float(matrix[1][2]), float(matrix[1][1]))  # type: ignore[index]
        yaw = 0.0
    return {
        "x": float(matrix[0][3]) * 1000.0,  # type: ignore[index]
        "y": float(matrix[1][3]) * 1000.0,  # type: ignore[index]
        "z": float(matrix[2][3]) * 1000.0,  # type: ignore[index]
        "roll": math.degrees(roll),
        "pitch": math.degrees(pitch),
        "yaw": math.degrees(yaw),
    }


def _clamp(value: float, limits: tuple[float, float]) -> float:
    return max(limits[0], min(limits[1], value))


@dataclass(frozen=True, slots=True)
class CameraJoystickSnapshot:
    """Atomic browser joystick state consumed by the 20 Hz robot action."""

    active: bool
    pan: float
    tilt: float
    hold_body_yaw_degrees: float | None


class CameraJoystickStream:
    """Coalesce one gesture's browser updates with a short fail-closed watchdog."""

    def __init__(self, *, watchdog_seconds: float = 0.75) -> None:
        self._lock = threading.Lock()
        self._watchdog_seconds = max(0.05, float(watchdog_seconds))
        self._active = True
        self._pan = 0.0
        self._tilt = 0.0
        self._updated_at = time.monotonic()
        self._hold_body_yaw_degrees: float | None = None

    def update(self, pan: float, tilt: float) -> None:
        if any(isinstance(value, bool) for value in (pan, tilt)) or not all(
            math.isfinite(value) and -1.0 <= value <= 1.0 for value in (pan, tilt)
        ):
            raise ValueError("Camera joystick input must be finite and bounded")
        with self._lock:
            if not self._active:
                raise RuntimeError("Camera joystick stream is not active")
            self._pan = float(pan)
            self._tilt = float(tilt)
            self._updated_at = time.monotonic()

    def stop(self, *, hold_body_yaw_degrees: float | None = None) -> None:
        with self._lock:
            self._active = False
            self._pan = 0.0
            self._tilt = 0.0
            self._hold_body_yaw_degrees = hold_body_yaw_degrees

    def snapshot(self) -> CameraJoystickSnapshot:
        with self._lock:
            if self._active and time.monotonic() - self._updated_at > self._watchdog_seconds:
                self._active = False
            active = self._active
            return CameraJoystickSnapshot(
                active=active,
                pan=self._pan if active else 0.0,
                tilt=self._tilt if active else 0.0,
                hold_body_yaw_degrees=self._hold_body_yaw_degrees,
            )


def robot_control_options() -> dict[str, list[str]]:
    """Return the UI-safe semantic controls without exposing raw motors or joints."""
    return {
        "look": list(LOOK_DIRECTIONS),
        "emotion": list(EMOTIONS),
        "dance": list(DANCE_STYLES),
    }


class RobotLike(Protocol):
    def get_current_head_pose(self) -> object: ...

    def goto_target(self, **kwargs: object) -> object: ...

    def set_target_head_pose(self, head: object) -> object: ...

    def set_target_body_yaw(self, body_yaw: float) -> object: ...

    def set_target_antenna_joint_positions(self, positions: list[float]) -> object: ...

    def cancel_move(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RobotToolCall:
    call_id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class QueuedRobotAction:
    name: str
    arguments: dict[str, object]
    generation: int
    on_complete: Callable[[dict[str, object]], None] | None = None
    hold_pose: bool = False


def create_head_pose(**kwargs: float) -> object:
    """Import the SDK pose helper lazily so unit tests remain hardware-independent."""
    from reachy_mini.utils import create_head_pose as sdk_create_head_pose

    return sdk_create_head_pose(**kwargs)


def interpolate_head_pose(start: object, target: object, ratio: float) -> object:
    """Import SDK SE(3) interpolation lazily for hardware-independent tests."""
    from reachy_mini.utils.interpolation import linear_pose_interpolation

    return linear_pose_interpolation(start, target, ratio)


def _default_library_factory() -> object:
    from reachy_mini.motion.recorded_move import RecordedMoves

    return RecordedMoves("pollen-robotics/reachy-mini-emotions-library")


def completed_robot_tool_call(kind: str, payload: dict[str, object]) -> RobotToolCall | None:
    """Parse a completed, allow-listed robot-local Realtime function call."""
    if kind != "response.output_item.done":
        return None
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "")
    call_id = str(item.get("call_id") or "")
    if (
        item.get("type") != "function_call"
        or item.get("status") != "completed"
        or name not in ROBOT_TOOL_NAMES
        or not call_id
    ):
        return None
    raw_arguments = item.get("arguments") or "{}"
    try:
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
    except json.JSONDecodeError:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return RobotToolCall(call_id, name, arguments)


class ReachyRobotActions:
    """Serialize explicit robot actions away from the microphone streaming loop."""

    def __init__(
        self,
        robot: RobotLike,
        stop_event: threading.Event,
        *,
        library_factory: Callable[[], object] = _default_library_factory,
        before_action: Callable[[], None] | None = None,
        after_action: Callable[[], None] | None = None,
        on_result: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self.robot = robot
        self.stop_event = stop_event
        self._library_factory = library_factory
        self._before_action = before_action
        self._after_action = after_action
        self._on_result = on_result
        self._library: object | None = None
        self._queue: queue.Queue[QueuedRobotAction | None] = queue.Queue(maxsize=4)
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()
        self._busy = threading.Event()
        self._cancel_requested = threading.Event()
        self._state_lock = threading.Lock()
        self._generation = 0
        self._pending_actions = 0

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    @property
    def pending_count(self) -> int:
        with self._state_lock:
            return self._pending_actions

    def _mark_finished(self) -> None:
        with self._state_lock:
            self._pending_actions = max(0, self._pending_actions - 1)

    def wait_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.pending_count == 0 and not self.busy:
                return True
            time.sleep(0.02)
        return self.pending_count == 0 and not self.busy

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="reachy-hermes-actions", daemon=True)
        self._thread.start()

    def enqueue(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        on_complete: Callable[[dict[str, object]], None] | None = None,
        hold_pose: bool = False,
        reject_if_busy: bool = False,
    ) -> dict[str, object]:
        if name not in _QUEUED_ROBOT_ACTION_NAMES:
            return {"ok": False, "error": "Robot action is not allow-listed"}
        if self._closed.is_set() or self.stop_event.is_set():
            return {"ok": False, "error": "Robot action controller is stopping"}
        with self._state_lock:
            if reject_if_busy and self._pending_actions:
                return {"ok": False, "error": "Robot is busy", "code": "robot_busy"}
            generation = self._generation
            try:
                self._queue.put_nowait(
                    QueuedRobotAction(name, dict(arguments), generation, on_complete, hold_pose)
                )
            except queue.Full:
                return {"ok": False, "error": "Robot action queue is full", "code": "robot_busy"}
            self._pending_actions += 1
        return {"accepted": True, "queued": name}

    def _generation_matches(self, generation: int) -> bool:
        with self._state_lock:
            return generation == self._generation and not self._closed.is_set()

    def _run_camera_joystick_stream(
        self,
        stream: CameraJoystickStream,
        *,
        initial_body_yaw_degrees: float,
    ) -> dict[str, object]:
        """Apply one browser gesture as a smoothed 20 Hz head/base target stream."""
        components = _head_pose_components(self.robot.get_current_head_pose())
        target_body = _clamp(float(initial_body_yaw_degrees), _BODY_YAW_LIMIT)
        relative_yaw = _clamp(
            components["yaw"] - target_body,
            (-_CAMERA_HEAD_YAW_LIMIT, _CAMERA_HEAD_YAW_LIMIT),
        )
        target_head_yaw = target_body + relative_yaw
        target_pitch = _clamp(components["pitch"], _HEAD_LIMITS["pitch"])
        smoothed_pan = 0.0
        smoothed_tilt = 0.0
        last_update = time.monotonic()

        while True:
            snapshot = stream.snapshot()
            cancelled = self._cancel_requested.is_set() or self.stop_event.is_set() or self._closed.is_set()
            if cancelled or not snapshot.active:
                hold_body = (
                    snapshot.hold_body_yaw_degrees
                    if snapshot.hold_body_yaw_degrees is not None
                    else target_body
                )
                self.robot.set_target_head_pose(self.robot.get_current_head_pose())
                self.robot.set_target_body_yaw(math.radians(_clamp(hold_body, _BODY_YAW_LIMIT)))
                return _cancelled_action_result("camera_joystick")

            now = time.monotonic()
            elapsed = min(0.1, max(0.001, now - last_update))
            last_update = now
            alpha = 1.0 - math.exp(-elapsed / _CAMERA_STREAM_SMOOTHING_SECONDS)
            smoothed_pan += (snapshot.pan - smoothed_pan) * alpha
            smoothed_tilt += (snapshot.tilt - smoothed_tilt) * alpha

            body_delta = smoothed_pan * _CAMERA_STREAM_PAN_SPEED * elapsed
            next_body = _clamp(target_body + body_delta, _BODY_YAW_LIMIT)
            applied_body_delta = next_body - target_body
            target_body = next_body
            target_head_yaw = _clamp(target_head_yaw + applied_body_delta, (-180.0, 180.0))
            target_pitch = _clamp(
                target_pitch + smoothed_tilt * _CAMERA_STREAM_TILT_SPEED * elapsed,
                _HEAD_LIMITS["pitch"],
            )
            target_head = create_head_pose(
                x=components["x"],
                y=components["y"],
                z=components["z"],
                roll=components["roll"],
                pitch=target_pitch,
                yaw=target_head_yaw,
                mm=True,
                degrees=True,
            )
            self.robot.set_target_head_pose(target_head)
            self.robot.set_target_body_yaw(math.radians(target_body))
            self._cancel_requested.wait(_CAMERA_STREAM_PERIOD)

    def execute(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        """Execute one action synchronously; the worker calls this off the audio loop."""
        if name == "acknowledge_presence":
            raw_direction = arguments.get("direction_degrees")
            if raw_direction is not None and (
                isinstance(raw_direction, bool) or not isinstance(raw_direction, (int, float))
            ):
                return {"ok": False, "error": "Presence direction must be finite and bounded"}
            direction = 0.0 if raw_direction is None else float(raw_direction)
            if not math.isfinite(direction) or not -60.0 <= direction <= 60.0:
                return {"ok": False, "error": "Presence direction must be finite and bounded"}
            components = _head_pose_components(self.robot.get_current_head_pose())
            components["pitch"] = -3.0
            if raw_direction is not None:
                components["yaw"] = _clamp(direction, (-18.0, 18.0))
            target_head = create_head_pose(
                x=components["x"],
                y=components["y"],
                z=components["z"],
                roll=components["roll"],
                pitch=components["pitch"],
                yaw=components["yaw"],
                mm=True,
                degrees=True,
            )
            if not self._run_precision_interpolation(
                target_head=target_head,
                target_body=None,
                start_body=0.0,
                duration=0.45,
            ):
                return _cancelled_action_result(name)
            return {
                "ok": True,
                "action": name,
                "target": {
                    "pitch": -3.0,
                    "yaw": round(components["yaw"], 2),
                },
            }

        if name == "move_reachy_head":
            direction = str(arguments.get("direction") or "").lower()
            pose = _LOOK_POSES.get(direction)
            if pose is None:
                return {"ok": False, "error": "Unknown look direction"}
            target = create_head_pose(**pose, degrees=True)
            self.robot.goto_target(head=target, antennas=None, body_yaw=None, duration=0.6)
            return {"ok": True, "action": name, "direction": direction}

        if name == "camera_joystick":
            stream = arguments.get("stream")
            if isinstance(stream, CameraJoystickStream):
                raw_body_yaw = arguments.get("body_yaw_degrees", 0.0)
                if isinstance(raw_body_yaw, bool) or not isinstance(raw_body_yaw, (int, float)):
                    return {"ok": False, "error": "Camera joystick body yaw must be finite"}
                body_yaw = float(raw_body_yaw)
                if not math.isfinite(body_yaw):
                    return {"ok": False, "error": "Camera joystick body yaw must be finite"}
                return self._run_camera_joystick_stream(
                    stream,
                    initial_body_yaw_degrees=body_yaw,
                )
            if any(isinstance(arguments.get(key), bool) for key in ("pan", "tilt")):
                return {"ok": False, "error": "Camera joystick input must be finite and bounded"}
            pan = float(arguments.get("pan") or 0.0)
            tilt = float(arguments.get("tilt") or 0.0)
            body_yaw = float(arguments.get("body_yaw_degrees") or 0.0)
            if not all(math.isfinite(value) and -1.0 <= value <= 1.0 for value in (pan, tilt)):
                return {"ok": False, "error": "Camera joystick input must be finite and bounded"}
            components = _head_pose_components(self.robot.get_current_head_pose())
            relative_yaw = _clamp(
                components["yaw"] - body_yaw,
                (-_HEAD_BODY_YAW_DELTA_LIMIT, _HEAD_BODY_YAW_DELTA_LIMIT),
            )
            desired_relative_yaw = relative_yaw + pan * _CAMERA_PAN_STEP
            body_delta = 0.0
            if desired_relative_yaw > _CAMERA_HEAD_YAW_SOFT_LIMIT:
                body_delta = min(_CAMERA_BODY_STEP, desired_relative_yaw - _CAMERA_HEAD_YAW_SOFT_LIMIT)
            elif desired_relative_yaw < -_CAMERA_HEAD_YAW_SOFT_LIMIT:
                body_delta = max(-_CAMERA_BODY_STEP, desired_relative_yaw + _CAMERA_HEAD_YAW_SOFT_LIMIT)
            target_body_degrees = _clamp(body_yaw + body_delta, _BODY_YAW_LIMIT)
            applied_body_delta = target_body_degrees - body_yaw
            target_relative_yaw = _clamp(
                desired_relative_yaw - applied_body_delta,
                (-_CAMERA_HEAD_YAW_LIMIT, _CAMERA_HEAD_YAW_LIMIT),
            )
            components["yaw"] = target_body_degrees + target_relative_yaw
            components["pitch"] = _clamp(
                components["pitch"] + tilt * _CAMERA_TILT_STEP,
                _HEAD_LIMITS["pitch"],
            )
            target_head = create_head_pose(
                x=components["x"],
                y=components["y"],
                z=components["z"],
                roll=components["roll"],
                pitch=components["pitch"],
                yaw=components["yaw"],
                mm=True,
                degrees=True,
            )
            target_body = math.radians(target_body_degrees) if abs(applied_body_delta) > 1e-6 else None
            duration = 0.18 + 0.1 * max(abs(pan), abs(tilt))
            if not self._run_precision_interpolation(
                target_head=target_head,
                target_body=target_body,
                start_body=math.radians(body_yaw),
                duration=duration,
            ):
                return _cancelled_action_result(name)
            return {
                "ok": True,
                "action": name,
                "target": {
                    "pitch": round(components["pitch"], 2),
                    "yaw": round(components["yaw"], 2),
                    "body_yaw": round(target_body_degrees, 2),
                },
            }

        if name == "nudge_reachy":
            axis = str(arguments.get("axis") or "").lower()
            if axis not in _PRECISION_AXES:
                return {"ok": False, "error": "Unknown precision movement axis"}
            delta = float(arguments.get("delta") or 0.0)
            body_yaw = float(arguments.get("body_yaw_degrees") or 0.0)
            target_head: object | None = None
            target_body: float | None = None
            result_pose: dict[str, float] = {}

            target_body_degrees: float | None = None
            if axis in {"body_yaw", "center_base", "center_all"}:
                target_body_degrees = 0.0 if axis in {"center_base", "center_all"} else body_yaw + delta
                target_body_degrees = _clamp(target_body_degrees, _BODY_YAW_LIMIT)
                target_body = math.radians(target_body_degrees)
                result_pose["body_yaw"] = round(target_body_degrees, 2)

            if axis in {"body_yaw", "center_base"}:
                components = _head_pose_components(self.robot.get_current_head_pose())
                assert target_body_degrees is not None
                relative_head_yaw = _clamp(
                    components["yaw"] - body_yaw,
                    (-_HEAD_BODY_YAW_DELTA_LIMIT, _HEAD_BODY_YAW_DELTA_LIMIT),
                )
                components["yaw"] = _clamp(
                    target_body_degrees + relative_head_yaw,
                    (-180.0, 180.0),
                )
                target_head = create_head_pose(
                    x=components["x"],
                    y=components["y"],
                    z=components["z"],
                    roll=components["roll"],
                    pitch=components["pitch"],
                    yaw=components["yaw"],
                    mm=True,
                    degrees=True,
                )
                result_pose.update({key: round(value, 2) for key, value in components.items()})
            elif axis not in {"body_yaw", "center_base"}:
                components = _head_pose_components(self.robot.get_current_head_pose())
                if axis in {"center_head", "center_all"}:
                    components = {key: 0.0 for key in _HEAD_LIMITS}
                elif axis in _HEAD_LIMITS:
                    components[axis] = _clamp(components[axis] + delta, _HEAD_LIMITS[axis])
                target_head = create_head_pose(
                    x=components["x"],
                    y=components["y"],
                    z=components["z"],
                    roll=components["roll"],
                    pitch=components["pitch"],
                    yaw=components["yaw"],
                    mm=True,
                    degrees=True,
                )
                result_pose.update({key: round(value, 2) for key, value in components.items()})

            motion_magnitude = (
                abs(target_body_degrees - body_yaw)
                if target_body_degrees is not None
                else abs(delta)
            )
            duration = max(0.4, min(4.5, 0.4 + motion_magnitude / 30.0))
            if not self._run_precision_interpolation(
                target_head=target_head,
                target_body=target_body,
                start_body=math.radians(body_yaw),
                duration=duration,
            ):
                return {"ok": False, "error": "Robot action was cancelled", "action": name}
            return {"ok": True, "action": name, "axis": axis, "target": result_pose}

        if name == "express_reachy_emotion":
            emotion = str(arguments.get("emotion") or "").lower()
            move_name = _EMOTION_MOVES.get(emotion)
            if move_name is None:
                return {"ok": False, "error": "Unknown emotion"}
            if not self._play_recorded_move(move_name):
                return {"ok": False, "error": "Robot action was cancelled", "action": name}
            return {"ok": True, "action": name, "emotion": emotion, "move": move_name}

        if name == "dance_reachy":
            style = str(arguments.get("style") or "short").lower()
            move_name = _DANCE_MOVES.get(style)
            if move_name is None:
                return {"ok": False, "error": "Unknown dance style"}
            if not self._play_recorded_move(move_name):
                return {"ok": False, "error": "Robot action was cancelled", "action": name}
            return {"ok": True, "action": name, "style": style, "move": move_name}

        return {"ok": False, "error": "Robot action is not allow-listed"}

    def _run_precision_interpolation(
        self,
        *,
        target_head: object | None,
        target_body: float | None,
        start_body: float,
        duration: float,
    ) -> bool:
        """Run cancellable app-owned interpolation instead of an opaque daemon task."""
        start_head = self.robot.get_current_head_pose() if target_head is not None else None
        started = time.monotonic()
        period = 1.0 / 50.0
        while True:
            if self._cancel_requested.is_set() or self.stop_event.is_set() or self._closed.is_set():
                return False
            elapsed = time.monotonic() - started
            ratio = min(1.0, elapsed / duration)
            eased = ratio * ratio * (3.0 - 2.0 * ratio)
            if target_head is not None and start_head is not None:
                head = interpolate_head_pose(start_head, target_head, eased)
                self.robot.set_target_head_pose(head)
            if target_body is not None:
                self.robot.set_target_body_yaw(start_body + (target_body - start_body) * eased)
            if ratio >= 1.0:
                return True
            if self._cancel_requested.wait(min(period, max(0.0, duration - elapsed))):
                return False

    def _play_recorded_move(self, move_name: str) -> bool:
        if self._library is None:
            self._library = self._library_factory()
        get_move = getattr(self._library, "get", None)
        if not callable(get_move):
            raise RuntimeError("Reachy recorded-move library is unavailable")
        move = get_move(move_name)
        evaluate = getattr(move, "evaluate", None)
        duration = float(getattr(move, "duration", 0.0))
        if not callable(evaluate) or duration <= 0:
            raise RuntimeError("Reachy recorded move is invalid")

        start_head, start_antennas, start_body_yaw = cast(
            tuple[object | None, object | None, float | None], evaluate(0.0)
        )
        self.robot.goto_target(
            head=start_head,
            antennas=start_antennas,
            body_yaw=start_body_yaw,
            duration=0.25,
        )
        started = time.monotonic()
        period = 1.0 / 50.0
        while time.monotonic() - started < duration:
            if self._cancel_requested.is_set() or self.stop_event.is_set() or self._closed.is_set():
                return False
            elapsed = time.monotonic() - started
            head, antennas, body_yaw = cast(
                tuple[object | None, object | None, float | None], evaluate(min(elapsed, duration - 0.01))
            )
            if head is not None:
                self.robot.set_target_head_pose(head)
            if body_yaw is not None:
                self.robot.set_target_body_yaw(float(body_yaw))
            if antennas is not None:
                self.robot.set_target_antenna_joint_positions(list(cast(Iterable[float], antennas)))
            if self._cancel_requested.wait(period):
                return False
        return True

    def _run(self) -> None:
        while not self._closed.is_set() and not self.stop_event.is_set():
            try:
                action = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if action is None:
                self._queue.task_done()
                break
            if not self._generation_matches(action.generation):
                result = _cancelled_action_result(action.name)
                self._queue.task_done()
                self._mark_finished()
                if action.on_complete is not None:
                    action.on_complete(result)
                if self._on_result is not None:
                    self._on_result(action.name, result)
                continue
            self._cancel_requested.clear()
            self._busy.set()
            result: dict[str, object] = {
                "ok": False,
                "error": "Robot action did not complete",
                "action": action.name,
            }
            try:
                if self._before_action is not None:
                    self._before_action()
                if not self._generation_matches(action.generation):
                    result = _cancelled_action_result(action.name)
                else:
                    result = self.execute(action.name, action.arguments)
                    if not self._generation_matches(action.generation):
                        result = _cancelled_action_result(action.name)
                if not result.get("ok"):
                    _LOGGER.warning("Reachy action rejected: %s", result.get("error"))
            except Exception as exc:
                _LOGGER.exception("Reachy action failed: %s", action.name)
                result = {"ok": False, "error": str(exc), "action": action.name}
            finally:
                try:
                    cancelled = result.get("error") == "Robot action was cancelled"
                    if self._after_action is not None and not action.hold_pose and not cancelled:
                        self._after_action()
                finally:
                    self._busy.clear()
                    self._queue.task_done()
                    self._mark_finished()
            if self._on_result is not None:
                self._on_result(action.name, result)
            if action.on_complete is not None:
                action.on_complete(result)

    def cancel(self, *, stop_media: bool = True) -> bool:
        """Stop the active move and discard queued actions for privacy/power transitions."""
        with self._state_lock:
            self._generation += 1
        cancelled_active = self._busy.is_set()
        self._cancel_requested.set()
        if cancelled_active:
            try:
                if stop_media:
                    self.robot.cancel_move()
                else:
                    # Freeze the current head target while the cooperative worker
                    # exits, without touching the shared voice playback pipeline.
                    self.robot.set_target_head_pose(self.robot.get_current_head_pose())
            except Exception:
                _LOGGER.debug("No active Reachy move to cancel", exc_info=True)
        while True:
            try:
                pending = self._queue.get_nowait()
            except queue.Empty:
                break
            else:
                self._queue.task_done()
                if pending is None:
                    break
                self._mark_finished()
                result = _cancelled_action_result(pending.name)
                if pending.on_complete is not None:
                    pending.on_complete(result)
                if self._on_result is not None:
                    self._on_result(pending.name, result)
        return cancelled_active

    def close(self) -> None:
        self.cancel()
        self._closed.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
