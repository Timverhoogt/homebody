"""Owner incident 2026-10-17 17:10 UTC: Standby wake → /v1/agent/session 200 → Live
``PermissionError category=blocked`` → normal post-turn fold ("wake → immediate fold").

Mechanism: a Standby wake turn keeps ``_power_mode == "standby"`` while torque is on and the wake
pose is reached, and the robot published exactly that. The bridge's Live ``allowed()`` uses the
broker's ``everyday_read_fence``, which (unchanged) only admits ``power_mode == "awake"``, so Live
was refused at every Standby wake, including the first one after Kids End returned to Standby.

Runs the real wake loop, ``_run_live_conversation``, ``agent_broker_context`` publication, the real
broker lease + read fence and the bridge's ``_failure`` category mapping. The relay socket is a
loopback model; no provider, robot or network is contacted. NOT physical acceptance.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import asdict

import pytest
from test_kids_end_single_fold import Rig

from companion.live_transport import _failure
from companion.reachy_agent_broker import BrokerConfig, BrokerContext, BrokerValidationError, ReachyAgentBroker
from homebody.config import AppConfig
from homebody.live_client import bridge_error_text
from homebody.realtime_client import RealtimeBridgeError
from homebody.runtime import HermesVoiceRuntime

DEVICE = "reachy-incident-test"


class BridgeModel:
    """The bridge side the robot talks to: the device lease and Live's Everyday admission."""

    def __init__(self) -> None:
        self.broker = ReachyAgentBroker(BrokerConfig())
        self.kids_live = False
        self.publications: list[dict[str, object]] = []
        self.decisions: list[bool] = []

    def establish(self, context) -> None:  # POST /v1/agent/session
        payload = asdict(context)
        try:
            asyncio.run(self.broker.establish_session(DEVICE, payload))
        except BrokerValidationError:
            return  # an older generation published late is rejected, as on the bridge
        self.publications.append(payload)

    def live_allowed(self, agent_context: dict[str, object]) -> bool:
        """``HermesReachyBridge.live.allowed`` for an Everyday (conversation) context."""
        if self.kids_live:
            return False
        try:
            return self.broker.everyday_read_fence(DEVICE, BrokerContext.parse(agent_context)) is not None
        except BrokerValidationError:
            return False


class Client:
    def __init__(self, model: BridgeModel) -> None:
        self.model = model

    def establish_agent_session(self, context, **_kwargs) -> None:
        self.model.establish(context)

    def cancel_agent_request(self, _request_id) -> None:
        pass

    def close(self) -> None:
        pass


class LiveSession:
    """``serve_live`` admission: ``allowed(config)`` before anything else, then session.started."""

    def __init__(self, model: BridgeModel, config, *, agent_context, agent_request_id="") -> None:
        self.model, self.agent_context = model, agent_context

    def start(self) -> None:
        self.allowed = self.model.live_allowed(asdict(self.agent_context))
        self.model.decisions.append(self.allowed)

    def wait_started(self):
        if not self.allowed:
            category, _detail, text = _failure(PermissionError("Live session is not allowed in the current state"))
            raise RealtimeBridgeError(
                bridge_error_text({"error": text, "category": category}, "Live session was not accepted")
            )
        return []

    def close(self) -> None:
        pass


def live_rig(monkeypatch, *, prior_kids: bool):
    rig = Rig(monkeypatch, wakes_at={})
    runtime = rig.runtime
    runtime.config_loader = lambda: AppConfig(
        api_key="test-key", wake_cooldown_seconds=0.0, conversation_mode="live", instance_id="reachy-test",
    )
    del runtime._run_selected_voice_conversation  # the real dispatcher → _run_live_conversation
    model = BridgeModel()
    runtime._new_bridge_client = lambda _config: Client(model)  # type: ignore[method-assign]
    runtime._new_live_session = lambda config, **kw: LiveSession(model, config, **kw)  # type: ignore[method-assign]
    loops: list[str] = []
    runtime._live_loop = lambda *_a, **_k: loops.append("live")  # type: ignore[method-assign]
    if prior_kids:
        rig.start_kids()
        runtime.stop_kids_mode(reason="parent")  # Kids End returns to folded Standby
    else:
        runtime._power_mode = "standby"
        runtime._motors_enabled, runtime._head_safely_folded = False, True
        rig.daemon.control_mode, rig.daemon.folded = "disabled", True
    with runtime._status_lock:
        runtime._status.power_mode = "standby"
    assert runtime._effective_power_mode() == "standby" and runtime._motors_enabled is False
    return rig, model, loops


def wake_once(rig: Rig, model: BridgeModel) -> dict[str, object]:
    sleeps_before = rig.daemon.sleeps
    rig.keywords[3] = "hey"
    rig.thread.start()
    deadline = time.monotonic() + 5
    while not model.decisions:
        assert time.monotonic() < deadline, "Live admission never ran"
        time.sleep(0.005)
    status = rig.finish()
    status["post_turn_sleeps"] = rig.daemon.sleeps - sleeps_before
    return status


@pytest.mark.parametrize("prior_kids", [False, True], ids=["standby-wake", "after-kids-end"])
def test_baseline_standby_wake_reproduces_live_blocked_then_fold(monkeypatch, prior_kids):
    rig, model, loops = live_rig(monkeypatch, prior_kids=prior_kids)
    rig.runtime._standby_wake_turn_awake = lambda: False  # 0.5.33: publish the selected mode only
    status = wake_once(rig, model)

    assert model.decisions == [False] and loops == []
    assert model.publications[-1]["power_mode"] == "standby"
    assert "Live is blocked in the current state (category=blocked)" in str(status["last_error"])
    assert status["post_turn_sleeps"] == 1, "the failed turn then folds normally, as logged"


@pytest.mark.parametrize("prior_kids", [False, True], ids=["standby-wake", "after-kids-end"])
def test_standby_wake_turn_is_admitted_by_the_unchanged_broker_fence(monkeypatch, prior_kids):
    rig, model, loops = live_rig(monkeypatch, prior_kids=prior_kids)
    status = wake_once(rig, model)

    assert model.decisions == [True] and loops == ["live"]
    # The publication the wake made before Live (16.041 before 16.359 in the log) is the lease used.
    assert model.publications[-1]["power_mode"] == "awake"
    assert model.publications[-1]["reachy_status"]["power_mode"] == "standby"  # still truthful
    assert "category=blocked" not in str(status["last_error"])
    assert status["post_turn_sleeps"] == 1  # Standby still folds once when the turn ends
    assert rig.runtime._voice_turn_awake_generation is None
    assert rig.runtime.agent_broker_context(explicit_private_intent=False).power_mode == "standby"


def _turn_runtime(monkeypatch):
    runtime = HermesVoiceRuntime(object(), threading.Event(), config_loader=lambda: AppConfig(api_key="k"))
    runtime._audio_ready = True
    runtime._power_mode = "standby"
    runtime._motors_enabled, runtime._head_safely_folded = True, False
    monkeypatch.setattr(runtime, "_publish_remote_agent_session", lambda: None)
    runtime._accept_wake_turn()
    with runtime._conversation_stop_generation_lock:
        runtime._voice_turn_awake_generation = runtime._turn_stop_generation
    return runtime


def _admitted(runtime) -> bool:
    model = BridgeModel()
    context = runtime.agent_broker_context(explicit_private_intent=False)
    model.establish(context)
    return model.live_allowed(asdict(context))


@pytest.mark.parametrize("change", ["stop", "privacy", "kids", "folded", "unconfirmed", "idle", "shutdown"])
def test_authority_is_never_widened_beyond_the_running_wake_turn(monkeypatch, change):
    runtime = _turn_runtime(monkeypatch)
    assert _admitted(runtime) is True
    if change == "stop":
        runtime._request_conversation_stop()
    elif change == "privacy":
        runtime._privacy_requested.set()
    elif change == "kids":
        with runtime._kids_lock:
            runtime._kids_locked = True
    elif change == "folded":
        runtime._head_safely_folded = True
    elif change == "unconfirmed":
        runtime._motors_enabled = None
    elif change == "idle":
        runtime._voice_turn_awake_generation = None
    else:
        runtime.stop_event.set()
    assert _admitted(runtime) is False


def test_bridge_kids_latch_still_refuses_live(monkeypatch):
    runtime = _turn_runtime(monkeypatch)
    model = BridgeModel()
    model.kids_live = True  # a bridge-side Kids session still live: Live stays blocked
    context = runtime.agent_broker_context(explicit_private_intent=False)
    model.establish(context)
    assert model.live_allowed(asdict(context)) is False
