from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from homebody import safety_gate as gate
from homebody.safety_gate import SafetyGate


@dataclass
class FakeProbe:
    """A fully permissive state that individual tests make unsafe one field at a time."""

    ready: bool = True
    mode: str = "awake"
    privacy: bool = False
    motors: bool = True
    kids_active: bool = False
    kids_locked: bool = False
    agent_profile: bool = True
    camera_control: bool = False
    announcement: bool = False
    voice_busy: bool = False
    state: str = gate.IDLE_STATE
    last_error: str = ""
    face_tracking: bool = False
    actions: bool = True
    busy: bool = False
    reads: list[str] = field(default_factory=list)

    def runtime_ready(self) -> bool:
        self.reads.append("ready")
        return self.ready

    def power_mode(self) -> str:
        self.reads.append("power")
        return self.mode

    def privacy_requested(self) -> bool:
        return self.privacy

    def motors_confirmed(self) -> bool:
        return self.motors

    def kids_engaged(self) -> bool:
        self.reads.append("kids")
        return self.kids_active or self.kids_locked

    def kids_session_active(self) -> bool:
        self.reads.append("kids")
        return self.kids_active

    def agent_profile_active(self) -> bool:
        return self.agent_profile

    def camera_control_active(self) -> bool:
        return self.camera_control

    def announcement_active(self) -> bool:
        return self.announcement

    def voice_activity_busy(self) -> bool:
        return self.voice_busy

    def status(self) -> tuple[str, str]:
        return self.state, self.last_error

    def face_tracking_active(self) -> bool:
        return self.face_tracking

    def actions_ready(self) -> bool:
        return self.actions

    def robot_busy(self) -> bool:
        return self.actions and self.busy


ALL_POLICIES = [
    gate.READINESS_POLICY,
    gate.PRESENTATION_POLICY,
    gate.PRESENCE_POLICY,
    gate.GESTURE_POLICY,
    gate.ROBOT_ACTION_POLICY,
    gate.CAMERA_CONTROL_POLICY,
    gate.CAMERA_CAPTURE_POLICY,
]


@pytest.mark.parametrize("policy", ALL_POLICIES)
def test_safe_awake_idle_state_passes_every_policy(policy: gate.Policy) -> None:
    assert SafetyGate(FakeProbe()).block_reason(policy) == ""


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"ready": False}, "runtime_not_ready"),
        ({"mode": "meeting"}, "meeting"),
        ({"mode": "sleep", "privacy": True}, "sleep"),
        ({"privacy": True, "mode": "standby"}, "privacy"),
        ({"mode": "standby"}, "not_awake"),
        ({"motors": False}, "motors_not_enabled"),
        ({"kids_locked": True}, "kids_mode"),
        ({"announcement": True}, "announcement_active"),
        ({"voice_busy": True}, "voice_active"),
        ({"state": "power_transition_error"}, "runtime_error"),
        ({"last_error": "boom", "state": "speaking"}, "runtime_error"),
        ({"state": "speaking"}, "voice_active"),
        ({"camera_control": True}, "camera_control_active"),
        ({"face_tracking": True}, "face_tracking_active"),
        ({"busy": True}, "robot_action_active"),
    ],
)
def test_presence_policy_reports_stable_reason_codes_in_priority_order(
    change: dict[str, object], reason: str
) -> None:
    assert SafetyGate(FakeProbe(**change)).block_reason(gate.PRESENCE_POLICY) == reason


def test_presence_owner_exemptions_skip_only_the_owned_voice_rules() -> None:
    probe = FakeProbe(announcement=True, voice_busy=True)
    safety = SafetyGate(probe)

    assert safety.block_reason(gate.PRESENCE_POLICY, exempt=[gate.ANNOUNCEMENT_ACTIVE]) == "voice_active"
    assert (
        safety.block_reason(gate.PRESENCE_POLICY, exempt=[gate.ANNOUNCEMENT_ACTIVE, gate.VOICE_ACTIVITY_BUSY]) == ""
    )


def test_kids_lock_alone_blocks_controls_but_not_adult_camera_capture() -> None:
    safety = SafetyGate(FakeProbe(kids_locked=True))

    assert safety.block_reason(gate.ROBOT_ACTION_POLICY) == "Robot action was blocked by Kids Mode"
    assert not safety.allows(gate.GESTURE_POLICY)
    with pytest.raises(RuntimeError, match="Kids Mode is active or locked"):
        safety.require(gate.CAMERA_CONTROL_POLICY)
    # Capture is gated on the live session; I Spy capture uses its own consent-bound check.
    safety.require(gate.CAMERA_CAPTURE_POLICY)


@pytest.mark.parametrize("mode", ["meeting", "sleep"])
def test_private_power_modes_block_robot_action_and_capture(mode: str) -> None:
    safety = SafetyGate(FakeProbe(mode=mode))

    assert safety.block_reason(gate.ROBOT_ACTION_POLICY) == "Robot action was blocked by privacy mode"
    assert safety.block_reason(gate.CAMERA_CAPTURE_POLICY) == "Camera capture is blocked in the current privacy mode"


def test_camera_control_requires_an_action_controller() -> None:
    safety = SafetyGate(FakeProbe(actions=False))

    assert safety.block_reason(gate.CAMERA_CONTROL_POLICY) == "Robot action controller is not ready"
    assert not safety.allows(gate.GESTURE_POLICY)


def test_presentation_requires_agent_profile_after_physical_safety() -> None:
    assert SafetyGate(FakeProbe(agent_profile=False)).block_reason(gate.PRESENTATION_POLICY) == (
        "Agent profile is required"
    )
    assert SafetyGate(FakeProbe(agent_profile=False, motors=False)).block_reason(gate.PRESENTATION_POLICY) == (
        "Reachy must be safely Awake"
    )


def test_evaluation_short_circuits_before_later_locked_reads() -> None:
    probe = FakeProbe(ready=False)

    assert SafetyGate(probe).block_reason(gate.PRESENCE_POLICY) == "runtime_not_ready"
    assert probe.reads == ["ready"]


def test_combined_rules_block_when_either_side_blocks() -> None:
    rule = gate.NOT_AWAKE | gate.MOTORS_NOT_CONFIRMED

    assert rule.blocks(FakeProbe(mode="standby")) is True
    assert rule.blocks(FakeProbe(motors=False)) is True
    assert rule.blocks(FakeProbe()) is False
