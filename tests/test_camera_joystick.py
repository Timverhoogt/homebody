from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import reachy_mini_hermes.main as main_module
from reachy_mini_hermes import safety_gate
from reachy_mini_hermes.config import AppConfig
from reachy_mini_hermes.main import ReachyMiniHermes
from reachy_mini_hermes.robot_tools import CameraJoystickStream
from reachy_mini_hermes.runtime import HermesVoiceRuntime

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "reachy_mini_hermes" / "static"


class FakeActions:
    def __init__(self) -> None:
        self.pending_count = 0
        self.busy = False
        self.commands: list[tuple[str, dict[str, object], bool, bool]] = []
        self.cancel_calls: list[bool] = []

    def enqueue(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        hold_pose: bool,
        reject_if_busy: bool,
    ) -> dict[str, object]:
        self.commands.append((name, arguments, hold_pose, reject_if_busy))
        return {"accepted": True, "queued": name}

    def cancel(self, *, stop_media: bool = True) -> bool:
        self.cancel_calls.append(stop_media)
        return False

    def wait_idle(self, timeout: float) -> bool:
        return timeout > 0


class RouteRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.kids_controls_locked = False

    def start_camera_control(
        self,
        *,
        camera_feed_enabled: bool,
        controls_enabled: bool,
        adult_ui_unlocked: bool,
    ) -> dict[str, object]:
        self.calls.append(("start", camera_feed_enabled, controls_enabled, adult_ui_unlocked))
        return {"ok": True, "session_id": "camera-" + "a" * 32}

    def queue_camera_control(self, session_id: str, sequence: int, pan: float, tilt: float) -> dict[str, object]:
        self.calls.append(("move", session_id, sequence, pan, tilt))
        return {"ok": True, "accepted": True}

    def center_camera_control(self, session_id: str, sequence: int) -> dict[str, object]:
        self.calls.append(("center", session_id, sequence))
        return {"ok": True, "accepted": True}

    def end_camera_control(self, session_id: str, sequence: int) -> dict[str, object]:
        self.calls.append(("end", session_id, sequence))
        return {"ok": True, "held": True}

    def revoke_camera_control(self) -> None:
        self.calls.append(("revoke",))


def ready_runtime() -> tuple[HermesVoiceRuntime, FakeActions]:
    runtime = HermesVoiceRuntime(SimpleNamespace(), threading.Event())
    actions = FakeActions()
    runtime._actions = actions  # type: ignore[assignment]
    runtime._motors_enabled = True
    runtime._power_mode = "awake"
    runtime._kids_active = False
    runtime._kids_locked = False
    runtime.robot_pose = lambda: {"body_yaw": 0.0}  # type: ignore[method-assign]
    return runtime, actions


def test_camera_controls_are_separate_and_off_by_default() -> None:
    config = AppConfig()

    assert config.camera_feed_enabled is False
    assert config.camera_controls_enabled is False
    assert config.camera_controls_handedness == "right"
    assert AppConfig(camera_controls_handedness=" LEFT ").camera_controls_handedness == "left"
    with pytest.raises(ValueError, match="camera control handedness"):
        AppConfig(camera_controls_handedness="center")


def test_camera_viewer_contains_optional_accessible_control_overlay() -> None:
    html = (STATIC / "index.html").read_text()
    style = (STATIC / "style.css").read_text()

    for element_id in (
        "camera-control-overlay",
        "camera-joystick",
        "camera-joystick-knob",
        "camera-control-center",
        "camera-control-stop",
        "camera-control-fullscreen-exit",
        "camera_controls_enabled",
        "camera_controls_handedness",
    ):
        assert f'id="{element_id}"' in html
    assert 'aria-label="Camera pan and tilt joystick"' in html
    assert 'role="application"' in html
    assert ".camera-control-overlay" in style
    assert ".camera-control-overlay[data-handedness=\"left\"]" in style
    assert "touch-action: none" in style
    assert "min-height: 34px" in style
    assert ".camera-overlay-exit" in style
    exit_rule = style.split(".camera-overlay-exit {", 1)[1].split("}", 1)[0]
    assert "align-items: center" in exit_rule
    assert "justify-content: center" in exit_rule
    assert "env(safe-area-inset-bottom)" in style
    assert ".camera-viewer:fullscreen .camera-control-overlay" in style
    assert ".camera-viewer.camera-app-fullscreen" in style


def test_camera_script_cancels_every_pointer_and_lifecycle_boundary() -> None:
    camera = (STATIC / "camera.js").read_text()

    for event in (
        '"pointerdown"',
        '"pointermove"',
        '"pointerup"',
        '"pointercancel"',
        '"lostpointercapture"',
        '"blur"',
        '"visibilitychange"',
        '"fullscreenchange"',
        '"orientationchange"',
    ):
        assert event in camera
    assert 'document.body.appendChild(viewer)' in camera
    assert 'document.createComment("camera-viewer-home")' in camera
    assert "setPointerCapture" in camera
    assert "releasePointerCapture" in camera
    assert "cameraControlSession" in camera
    for endpoint in (
        "/api/camera-control/session",
        "/api/camera-control/move",
        "/api/camera-control/end",
        "/api/camera-control/center",
        "/api/robot/stop",
    ):
        assert endpoint in camera
    assert "CAMERA_CONTROL_DEAD_ZONE" in camera
    assert "CAMERA_CONTROL_INTERVAL_MS" in camera
    assert "camera-app-fullscreen" in camera


def test_camera_control_routes_require_explicit_opt_in_and_adult_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    app = ReachyMiniHermes(False)
    runtime = RouteRuntime()
    app._runtime = runtime  # type: ignore[assignment]
    monkeypatch.setattr(
        main_module,
        "load_config",
        lambda: AppConfig(camera_feed_enabled=True, camera_controls_enabled=True),
    )
    client = TestClient(app.settings_app)

    denied = client.post("/api/camera-control/session")
    assert denied.status_code == 403

    started = client.post(
        "/api/camera-control/session",
        headers={"X-Reachy-Adult-UI": "unlocked"},
    )
    assert started.status_code == 200
    assert runtime.calls == [("start", True, True, True)]


def test_camera_control_routes_forward_generation_bound_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    app = ReachyMiniHermes(False)
    runtime = RouteRuntime()
    app._runtime = runtime  # type: ignore[assignment]
    monkeypatch.setattr(
        main_module,
        "load_config",
        lambda: AppConfig(camera_feed_enabled=True, camera_controls_enabled=True),
    )
    client = TestClient(app.settings_app)
    session_id = "camera-" + "b" * 32

    assert client.post(
        "/api/camera-control/move",
        json={"session_id": session_id, "sequence": 1, "pan": 0.7, "tilt": -0.4},
    ).status_code == 200
    assert client.post(
        "/api/camera-control/center",
        json={"session_id": session_id, "sequence": 2},
    ).status_code == 200
    assert client.post(
        "/api/camera-control/end",
        json={"session_id": session_id, "sequence": 3},
    ).status_code == 200
    assert runtime.calls == [
        ("move", session_id, 1, 0.7, -0.4),
        ("center", session_id, 2),
        ("end", session_id, 3),
    ]


def test_camera_control_route_revokes_when_saved_opt_in_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    app = ReachyMiniHermes(False)
    runtime = RouteRuntime()
    app._runtime = runtime  # type: ignore[assignment]
    monkeypatch.setattr(main_module, "load_config", lambda: AppConfig(camera_feed_enabled=True))
    client = TestClient(app.settings_app)

    response = client.post(
        "/api/camera-control/move",
        json={
            "session_id": "camera-" + "c" * 32,
            "sequence": 1,
            "pan": 0.5,
            "tilt": 0.0,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Camera movement controls are disabled"
    assert runtime.calls == [("revoke",)]


@pytest.mark.parametrize("bad_value", [True, "0.5", None, 1.01, -1.01])
def test_camera_control_route_rejects_coerced_or_unbounded_input(
    monkeypatch: pytest.MonkeyPatch,
    bad_value: object,
) -> None:
    app = ReachyMiniHermes(False)
    runtime = RouteRuntime()
    app._runtime = runtime  # type: ignore[assignment]
    monkeypatch.setattr(
        main_module,
        "load_config",
        lambda: AppConfig(camera_feed_enabled=True, camera_controls_enabled=True),
    )
    client = TestClient(app.settings_app)

    response = client.post(
        "/api/camera-control/move",
        json={
            "session_id": "camera-" + "d" * 32,
            "sequence": 1,
            "pan": bad_value,
            "tilt": 0.0,
        },
    )

    assert response.status_code == 422
    assert runtime.calls == []


def test_runtime_rejects_standby_kids_lock_and_replayed_sequence() -> None:
    runtime, _ = ready_runtime()
    runtime._power_mode = "standby"
    with pytest.raises(RuntimeError, match="confirmed Awake"):
        runtime.start_camera_control(camera_feed_enabled=True, controls_enabled=True, adult_ui_unlocked=True)
    assert runtime._power_mode == "standby"

    runtime._power_mode = "awake"
    with pytest.raises(RuntimeError, match="must both be enabled"):
        runtime.start_camera_control(camera_feed_enabled=True, controls_enabled=False, adult_ui_unlocked=True)
    with pytest.raises(RuntimeError, match="unlocked adult UI"):
        runtime.start_camera_control(camera_feed_enabled=True, controls_enabled=True, adult_ui_unlocked=False)

    runtime._kids_locked = True
    with pytest.raises(RuntimeError, match="Kids Mode"):
        runtime.start_camera_control(camera_feed_enabled=True, controls_enabled=True, adult_ui_unlocked=True)

    runtime._kids_locked = False
    started = runtime.start_camera_control(camera_feed_enabled=True, controls_enabled=True, adult_ui_unlocked=True)
    session_id = str(started["session_id"])
    runtime.queue_camera_control(session_id, 1, 0.8, -0.5)
    with pytest.raises(RuntimeError, match="stale or replayed"):
        runtime.queue_camera_control(session_id, 1, 0.2, 0.2)


def test_runtime_camera_stream_coalesces_updates_then_release_holds_and_invalidates() -> None:
    runtime, actions = ready_runtime()
    started = runtime.start_camera_control(camera_feed_enabled=True, controls_enabled=True, adult_ui_unlocked=True)
    session_id = str(started["session_id"])

    assert len(actions.commands) == 1
    name, arguments, hold_pose, reject_if_busy = actions.commands[0]
    assert name == "camera_joystick"
    assert hold_pose is True
    assert reject_if_busy is True
    stream = arguments["stream"]
    assert isinstance(stream, CameraJoystickStream)

    moved = runtime.queue_camera_control(session_id, 1, 1.0, -1.0)
    assert moved["ok"] is True
    assert len(actions.commands) == 1
    snapshot = stream.snapshot()
    assert snapshot.pan == 1.0
    assert snapshot.tilt == -1.0
    assert snapshot.active is True

    ended = runtime.end_camera_control(session_id, 2)
    assert ended == {"ok": True, "held": True}
    assert stream.snapshot().active is False
    assert actions.cancel_calls == [False]
    with pytest.raises(RuntimeError, match="session is not active"):
        runtime.queue_camera_control(session_id, 3, 0.5, 0.0)


def test_runtime_policy_revocation_cancels_and_invalidates_active_camera_control() -> None:
    runtime, actions = ready_runtime()
    started = runtime.start_camera_control(camera_feed_enabled=True, controls_enabled=True, adult_ui_unlocked=True)

    runtime.revoke_camera_control()

    assert actions.cancel_calls == [False]
    with pytest.raises(RuntimeError, match="session is not active"):
        runtime.queue_camera_control(str(started["session_id"]), 1, 0.5, 0.0)


def test_camera_pointer_and_keyboard_vertical_controls_follow_sdk_pitch_convention() -> None:
    camera = (STATIC / "camera.js").read_text()

    assert "tilt: unitY * scaledMagnitude" in camera
    assert "ArrowUp: [0, -0.55]" in camera
    assert "ArrowDown: [0, 0.55]" in camera


def test_camera_pointer_move_sends_immediately_through_the_in_flight_guard() -> None:
    camera = (STATIC / "camera.js").read_text()
    move_handler = camera.split("function movePointerControl(event) {", 1)[1].split("\n  }", 1)[0]

    assert "state.desiredPan = vector.pan" in move_handler
    assert "state.desiredTilt = vector.tilt" in move_handler
    assert "void sendControlCommand()" in move_handler


def test_camera_stream_uses_official_app_twenty_hertz_command_cadence() -> None:
    camera = (STATIC / "camera.js").read_text()

    assert "const CAMERA_CONTROL_INTERVAL_MS = 50;" in camera
    sender = camera.split("async function sendControlCommand() {", 1)[1].split("\n  }", 1)[0]
    assert "desiredPan === 0" not in sender


def test_camera_pointer_gesture_pins_its_origin_across_status_layout_changes() -> None:
    camera = (STATIC / "camera.js").read_text()

    assert "state.controlOriginX = rect.left + rect.width / 2" in camera
    assert "state.controlOriginY = rect.top + rect.height / 2" in camera
    assert "state.controlRadius = Math.min(rect.width, rect.height) * 0.36" in camera
    assert "const originX = state.controlOriginX ?? rect.left + rect.width / 2" in camera
    assert "const originY = state.controlOriginY ?? rect.top + rect.height / 2" in camera


def test_abandoned_camera_control_session_expires_for_every_reader() -> None:
    runtime, actions = ready_runtime()
    started = runtime.start_camera_control(camera_feed_enabled=True, controls_enabled=True, adult_ui_unlocked=True)
    session_id = str(started["session_id"])
    stream = actions.commands[0][1]["stream"]
    assert isinstance(stream, CameraJoystickStream)
    runtime.queue_camera_control(session_id, 1, 0.5, 0.0)

    # While the gesture is live, other owners stay blocked.
    assert runtime.status()["camera_control_active"] is True
    with pytest.raises(RuntimeError, match="blocked during camera control"):
        runtime.queue_manual_robot_action("look", "left")

    # The browser tab closes mid-drag: no end packet, no further sequence numbers.
    runtime._camera_control_last_activity -= 31.0

    assert runtime.status()["camera_control_active"] is False
    assert runtime._safety_gate.block_reason(safety_gate.GESTURE_POLICY) == ""
    assert stream.snapshot().active is False
    result = runtime.queue_manual_robot_action("look", "left")
    assert result["ok"] is True
    with pytest.raises(RuntimeError, match="not active"):
        runtime.queue_camera_control(session_id, 2, 0.5, 0.0)


def test_camera_control_session_expires_on_its_own_next_packet() -> None:
    runtime, _actions = ready_runtime()
    started = runtime.start_camera_control(camera_feed_enabled=True, controls_enabled=True, adult_ui_unlocked=True)
    session_id = str(started["session_id"])
    runtime._camera_control_last_activity -= 31.0

    with pytest.raises(RuntimeError, match="expired"):
        runtime.queue_camera_control(session_id, 1, 0.5, 0.0)
    assert runtime._camera_control_session_id == ""
