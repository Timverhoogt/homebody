"""Named safety rules and the ordered policies that gate Reachy's autonomous and manual behaviour.

Each policy is an ordered table of ``(rule, reason)`` pairs. Evaluation stops at the first rule that
blocks, so a policy's order decides which reason the caller reports, and each probe read happens
only when the rules before it passed. The runtime supplies a :class:`SafetyProbe` that reads each
piece of state under its own lock, so evaluation never adds lock nesting beyond what the caller holds.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

POWER_MODES = frozenset({"standby", "awake", "meeting", "sleep"})

# Status states that mean the voice runtime is not healthy enough for proactive behaviour.
FAULT_STATES = frozenset({"starting", "stopping", "configuration_error", "power_transition_error"})
IDLE_STATE = "waiting_for_wake_word"


class SafetyProbe(Protocol):
    """Live, individually locked reads of the runtime state that safety rules depend on."""

    def runtime_ready(self) -> bool: ...

    def power_mode(self) -> str: ...

    def privacy_requested(self) -> bool: ...

    def motors_confirmed(self) -> bool: ...

    def kids_engaged(self) -> bool:
        """Kids Mode is active or its parent-control lock is still held."""
        ...

    def kids_session_active(self) -> bool: ...

    def agent_profile_active(self) -> bool: ...

    def camera_control_active(self) -> bool: ...

    def announcement_active(self) -> bool: ...

    def voice_activity_busy(self) -> bool: ...

    def status(self) -> tuple[str, str]:
        """Return ``(state, last_error)`` as one consistent read."""
        ...

    def face_tracking_active(self) -> bool: ...

    def actions_ready(self) -> bool: ...

    def robot_busy(self) -> bool: ...


@dataclass(frozen=True)
class Rule:
    """A named condition that blocks the guarded behaviour while it holds."""

    name: str
    blocks: Callable[[SafetyProbe], bool]

    def __or__(self, other: Rule) -> Rule:
        return Rule(f"{self.name}|{other.name}", lambda probe: self.blocks(probe) or other.blocks(probe))


RUNTIME_NOT_READY = Rule("runtime_not_ready", lambda p: not p.runtime_ready())
MEETING = Rule("meeting", lambda p: p.power_mode() == "meeting")
SLEEP = Rule("sleep", lambda p: p.power_mode() == "sleep")
PRIVACY = Rule("privacy", lambda p: p.privacy_requested())
NOT_AWAKE = Rule("not_awake", lambda p: p.power_mode() != "awake")
MOTORS_NOT_CONFIRMED = Rule("motors_not_confirmed", lambda p: not p.motors_confirmed())
KIDS_ENGAGED = Rule("kids_engaged", lambda p: p.kids_engaged())
KIDS_SESSION_ACTIVE = Rule("kids_session_active", lambda p: p.kids_session_active())
NOT_AGENT_PROFILE = Rule("not_agent_profile", lambda p: not p.agent_profile_active())
CAMERA_CONTROL_ACTIVE = Rule("camera_control_active", lambda p: p.camera_control_active())
ANNOUNCEMENT_ACTIVE = Rule("announcement_active", lambda p: p.announcement_active())
VOICE_ACTIVITY_BUSY = Rule("voice_activity_busy", lambda p: p.voice_activity_busy())
LAST_ERROR = Rule("last_error", lambda p: bool(p.status()[1]))


def _runtime_fault(probe: SafetyProbe) -> bool:
    state, last_error = probe.status()
    return bool(last_error) or state in FAULT_STATES


RUNTIME_FAULT = Rule("runtime_fault", _runtime_fault)
NOT_IDLE = Rule("not_idle", lambda p: p.status()[0] != IDLE_STATE)
FACE_TRACKING_ACTIVE = Rule("face_tracking_active", lambda p: p.face_tracking_active())
ACTIONS_MISSING = Rule("actions_missing", lambda p: not p.actions_ready())
ROBOT_BUSY = Rule("robot_busy", lambda p: p.robot_busy())

Policy = tuple[tuple[Rule, str], ...]

READINESS_POLICY: Policy = ((RUNTIME_NOT_READY, "runtime is not ready"),)

# Shared-physical-context window, evaluated after READINESS_POLICY and its feature flags.
PRESENTATION_POLICY: Policy = (
    (NOT_AWAKE | MOTORS_NOT_CONFIRMED, "Reachy must be safely Awake"),
    (PRIVACY, "privacy mode is active"),
    (NOT_AGENT_PROFILE, "Agent profile is required"),
    (KIDS_ENGAGED, "Kids Mode is active"),
    (CAMERA_CONTROL_ACTIVE, "camera control is active"),
    (ANNOUNCEMENT_ACTIVE | VOICE_ACTIVITY_BUSY, "voice activity is active"),
    (FACE_TRACKING_ACTIVE, "face tracking is active"),
    (ROBOT_BUSY, "robot action is active"),
    (LAST_ERROR, "runtime error is active"),
    (NOT_IDLE, "voice activity is active"),
)

# Silent presence acknowledgement. Reasons are stable codes surfaced in phone status.
PRESENCE_POLICY: Policy = (
    (RUNTIME_NOT_READY, "runtime_not_ready"),
    (MEETING, "meeting"),
    (SLEEP, "sleep"),
    (PRIVACY, "privacy"),
    (NOT_AWAKE, "not_awake"),
    (MOTORS_NOT_CONFIRMED, "motors_not_enabled"),
    (KIDS_ENGAGED, "kids_mode"),
    (ANNOUNCEMENT_ACTIVE, "announcement_active"),
    (VOICE_ACTIVITY_BUSY, "voice_active"),
    (RUNTIME_FAULT, "runtime_error"),
    (NOT_IDLE, "voice_active"),
    (CAMERA_CONTROL_ACTIVE, "camera_control_active"),
    (FACE_TRACKING_ACTIVE, "face_tracking_active"),
    (ROBOT_BUSY, "robot_action_active"),
)

# Local gesture reactions, evaluated after their feature flags.
GESTURE_POLICY: Policy = (
    (NOT_AWAKE | MOTORS_NOT_CONFIRMED, "Reachy must be safely Awake"),
    (PRIVACY, "privacy mode is active"),
    (KIDS_ENGAGED, "Kids Mode is active"),
    (CAMERA_CONTROL_ACTIVE, "camera control is active"),
    (ACTIONS_MISSING | ROBOT_BUSY, "robot action is active"),
)

# Every queued robot action, immediately before it moves.
ROBOT_ACTION_POLICY: Policy = (
    (PRIVACY | MEETING | SLEEP, "Robot action was blocked by privacy mode"),
    (KIDS_ENGAGED, "Robot action was blocked by Kids Mode"),
    (MOTORS_NOT_CONFIRMED, "Robot action was blocked because motor torque is not confirmed"),
)

# Owner camera-feed joystick sessions.
CAMERA_CONTROL_POLICY: Policy = (
    (KIDS_ENGAGED, "Camera controls are blocked while Kids Mode is active or locked"),
    (NOT_AWAKE | PRIVACY, "Camera controls require confirmed Awake outside privacy modes"),
    (MOTORS_NOT_CONFIRMED, "Camera controls require confirmed Awake motor torque"),
    (ACTIONS_MISSING, "Robot action controller is not ready"),
)

# Adult one-frame camera capture. I Spy capture has its own consent-bound check.
CAMERA_CAPTURE_POLICY: Policy = (
    (KIDS_SESSION_ACTIVE, "Camera capture is blocked while Kids Mode is active"),
    (MEETING | SLEEP | PRIVACY, "Camera capture is blocked in the current privacy mode"),
)


class SafetyGate:
    """Evaluate ordered safety policies against a live runtime probe."""

    def __init__(self, probe: SafetyProbe) -> None:
        self._probe = probe

    def block_reason(self, policy: Policy, *, exempt: Iterable[Rule] = ()) -> str:
        """Return the first blocking reason in ``policy``, or ``""`` when nothing blocks.

        ``exempt`` skips rules the caller has already satisfied, such as owning the voice slot.
        """
        skipped = set(exempt)
        for rule, reason in policy:
            if rule not in skipped and rule.blocks(self._probe):
                return reason
        return ""

    def allows(self, policy: Policy) -> bool:
        return not self.block_reason(policy)

    def require(self, policy: Policy) -> None:
        """Raise ``RuntimeError`` with the first blocking reason."""
        reason = self.block_reason(policy)
        if reason:
            raise RuntimeError(reason)
