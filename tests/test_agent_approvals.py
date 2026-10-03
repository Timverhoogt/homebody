from __future__ import annotations

import pytest

from homebody.agent_approvals import ApprovalStore, canonical_arguments
from homebody.agent_policy import CapabilityId, RiskTier


def test_canonical_arguments_are_order_independent_and_reject_nan() -> None:
    assert canonical_arguments({"b": 2, "a": 1}) == canonical_arguments({"a": 1, "b": 2})
    with pytest.raises(ValueError):
        canonical_arguments({"value": float("nan")})


def test_approval_is_bound_to_exact_request_generation_and_one_time() -> None:
    store = ApprovalStore(ttl_seconds=30)
    arguments = {"target": "desk", "enabled": True}
    token = store.issue(
        CapabilityId.GET_REACHY_STATUS,
        arguments,
        risk_tier=RiskTier.T3_EXTERNAL_SIDE_EFFECT,
        session_generation=7,
        approval_method="phone",
    )

    assert store.consume(token, CapabilityId.GET_REACHY_STATUS, {"target": "other"}, session_generation=7) is None
    assert store.consume(token, CapabilityId.GET_REACHY_STATUS, arguments, session_generation=8) is None
    record = store.consume(token, CapabilityId.GET_REACHY_STATUS, arguments, session_generation=7)
    assert record is not None
    assert record.session_generation == 7
    assert store.consume(token, CapabilityId.GET_REACHY_STATUS, arguments, session_generation=7) is None


def test_expired_and_invalidated_approvals_cannot_be_consumed() -> None:
    now = [10.0]
    store = ApprovalStore(ttl_seconds=2, clock=lambda: now[0])
    token = store.issue(
        CapabilityId.GET_REACHY_STATUS,
        {},
        risk_tier=RiskTier.T3_EXTERNAL_SIDE_EFFECT,
        session_generation=1,
        approval_method="pin",
    )
    now[0] = 13.0
    assert store.consume(token, CapabilityId.GET_REACHY_STATUS, {}, session_generation=1) is None

    token = store.issue(
        CapabilityId.GET_REACHY_STATUS,
        {},
        risk_tier=RiskTier.T3_EXTERNAL_SIDE_EFFECT,
        session_generation=1,
        approval_method="phone+pin",
    )
    assert store.invalidate_all() == 1
    assert store.consume(token, CapabilityId.GET_REACHY_STATUS, {}, session_generation=1) is None
