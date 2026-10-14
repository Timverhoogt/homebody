# ruff: noqa: F811 -- imported fixture is intentionally a test parameter
"""Late host fallback failures must not escape into a switched/ended child turn."""

from __future__ import annotations

import pytest
from test_robot_stt_game_master import FakeRobotStt, Turn, host, provenance, start, switch_to_quiz  # noqa: F401

from homebody.hermes_client import HermesBridgeError


def install_fallback(runtime, turn, *, phase, boundary=None, receipt=None):
    original = runtime._new_bridge_client

    def make(config):
        client = original(config)

        def cross():
            if boundary == "switch":
                switch_to_quiz(runtime)
            elif boundary == "end":
                runtime.stop_kids_mode(reason="parent")
            elif boundary == "privacy":
                runtime._privacy_requested.set()

        original_health = client.health

        def health():
            # Let the conversation's admission health read pass. Fail only the health read
            # after the fake robot has actually attempted recognition (the fallback boundary).
            if not turn.robot.calls:
                return original_health()
            cross()
            raise HermesBridgeError("PRIVATE_HOST_ERROR_MUST_NOT_SURFACE")

        def transcribe(_wav, *, provider=None):
            turn.host_calls.append(provider)
            cross()
            if receipt:
                client.last_stt_receipt = receipt
                return "late untrusted answer"
            raise HermesBridgeError("PRIVATE_HOST_ERROR_MUST_NOT_SURFACE")

        if phase == "health":
            client.health = health
        else:
            client.transcribe = transcribe
        return client

    runtime._new_bridge_client = make


@pytest.mark.parametrize("phase", ["health", "transcribe"])
@pytest.mark.parametrize("boundary", ["switch", "end", "privacy"])
def test_late_fallback_exception_is_cancelled_not_a_new_turn_error(host, phase, boundary):
    runtime = host.runtime
    start(host, language="nl")
    turn = Turn(runtime, FakeRobotStt(error="decode_timeout"), fallback="host")
    install_fallback(runtime, turn, phase=phase, boundary=boundary)
    try:
        turn.run()
    finally:
        runtime._privacy_requested.clear()
    assert turn.calls == [] and turn.errors == []
    assert runtime._status.last_error == ""
    assert getattr(runtime, "_turn_error", None) is None
    assert provenance(runtime) == ("unknown", False, "unknown")
    assert turn.host_calls == ([] if phase == "health" else ["local"])


@pytest.mark.parametrize("phase", ["health", "transcribe"])
def test_current_fallback_exception_is_categorical_infrastructure_not_silence(host, phase):
    runtime = host.runtime
    start(host, language="nl")
    turn = Turn(runtime, FakeRobotStt(error="decode_timeout"), fallback="host", speech=(True, True, True))
    install_fallback(runtime, turn, phase=phase)
    turn.run()
    assert len(turn.robot.calls) == 1 and turn.calls == []
    assert turn.errors == [1]
    assert runtime._status.last_error == "Speech recognition on Reachy is unavailable"
    assert runtime._status.state != "error"
    assert getattr(runtime, "_turn_error", None) is None
    assert provenance(runtime) == ("unknown", False, "unknown")


@pytest.mark.parametrize("boundary", ["switch", "end"])
def test_late_nonlocal_fallback_receipt_is_cancelled_before_validation(host, boundary):
    runtime = host.runtime
    start(host, language="nl")
    turn = Turn(runtime, FakeRobotStt(error="decode_timeout"), fallback="host")
    install_fallback(runtime, turn, phase="transcribe", boundary=boundary, receipt="openai")
    turn.run()
    assert turn.host_calls == ["local"] and turn.calls == [] and turn.errors == []
    assert runtime._status.last_error == "" and getattr(runtime, "_turn_error", None) is None
    assert provenance(runtime) == ("unknown", False, "unknown")
