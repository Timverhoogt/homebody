from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from homebody.agent_audit import AgentAuditLog
from homebody.agent_policy import (
    AgentPolicy,
    CapabilityDefinition,
    CapabilityId,
    PolicyContext,
    RiskTier,
)


def enabled_policy(*, camera: bool = False, approval: bool = False) -> AgentPolicy:
    definition = CapabilityDefinition(
        CapabilityId.GET_REACHY_STATUS,
        RiskTier.T1_PRIVATE_READ,
        "status",
        requires_camera=camera,
        requires_approval=approval,
        enabled=True,
    )
    return AgentPolicy({definition.capability_id: definition})


def allowed_context() -> PolicyContext:
    return PolicyContext(
        capability_profile="agent",
        adult_ui_unlocked=True,
        power_mode="awake",
        privacy_enabled=True,
        robot_available=True,
        session_generation=4,
        requested_session_generation=4,
    )


def test_agent_inventory_has_fixed_risk_and_approval_boundaries() -> None:
    enabled = AgentPolicy().enabled_capabilities()
    assert {item.capability_id for item in enabled} == set(CapabilityId)
    assert max(item.risk_tier for item in enabled) == RiskTier.T3_EXTERNAL_SIDE_EFFECT
    assert all(
        item.requires_approval
        for item in enabled
        if item.risk_tier == RiskTier.T3_EXTERNAL_SIDE_EFFECT
    )
    assert not any(item.risk_tier == RiskTier.T4_PRIVILEGED for item in enabled)
    assert AgentPolicy().decide(CapabilityId.GET_REACHY_STATUS, allowed_context()).allowed is True


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"adult_ui_unlocked": False}, "adult_ui_required"),
        ({"kids_mode_active": True}, "adult_ui_required"),
        ({"power_mode": "meeting"}, "power_mode_blocked"),
        ({"power_mode": "sleep"}, "power_mode_blocked"),
        ({"privacy_enabled": False}, "privacy_disabled"),
        ({"emergency_stop_active": True}, "emergency_stop"),
        ({"requested_session_generation": 3}, "stale_session"),
    ],
)
def test_policy_fails_closed_on_override_states(change: dict[str, object], reason: str) -> None:
    decision = enabled_policy().decide(CapabilityId.GET_REACHY_STATUS, replace(allowed_context(), **change))
    assert decision.allowed is False
    assert decision.reason == reason


def test_camera_and_approval_require_explicit_context() -> None:
    policy = enabled_policy(camera=True, approval=True)
    assert policy.decide(CapabilityId.GET_REACHY_STATUS, allowed_context()).reason == "camera_not_permitted"
    camera = replace(allowed_context(), camera_permitted=True)
    assert policy.decide(CapabilityId.GET_REACHY_STATUS, camera).reason == "approval_required"
    assert policy.decide(CapabilityId.GET_REACHY_STATUS, replace(camera, approval_granted=True)).allowed is True


def test_audit_is_append_only_bounded_and_sanitized(tmp_path: Path) -> None:
    path = tmp_path / "agent-audit.jsonl"
    audit = AgentAuditLog(path, max_bytes=1024, backups=1)
    event = audit.append(
        "capability_requested",
        capability_id="get_reachy_status",
        session_generation=3,
        summary="Bearer top-secret token_abcdefgh and terminal read_file",
        prompt_body="must never be stored",
        api_key="must never be stored",
    )
    serialized = str(event)
    assert "top-secret" not in serialized
    assert "token_abcdefgh" not in serialized
    assert "terminal" not in serialized
    assert "read_file" not in serialized
    assert "must never be stored" not in serialized
    assert path.stat().st_mode & 0o777 == 0o600
    assert audit.recent() == [event]
