from __future__ import annotations

import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient

import homebody.main as main_module
from homebody.agent_audit import AgentAuditLog
from homebody.config import AppConfig
from homebody.kids_mode import KidsProfile
from homebody.main import Homebody
from homebody.runtime import HermesVoiceRuntime


def build_client(monkeypatch):
    config = AppConfig(api_key="sk-super-secret-value")
    saved: list[AppConfig] = []
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(main_module, "save_config", lambda value: saved.append(value))
    app = Homebody(False)
    runtime = HermesVoiceRuntime(SimpleNamespace(), threading.Event(), config_loader=lambda: config)
    runtime._establish_remote_agent_session = lambda _context: None  # type: ignore[method-assign]
    runtime._publish_remote_agent_session = lambda: None  # type: ignore[method-assign]
    runtime._cancel_remote_agent_request = lambda _request_id: None  # type: ignore[method-assign]
    app._runtime = runtime
    assert app.settings_app is not None
    return app, runtime, TestClient(app.settings_app), saved


def test_early_power_route_rejects_before_motion(monkeypatch) -> None:
    _app, runtime, client, _saved = build_client(monkeypatch)
    calls: list[tuple[bool, bool]] = []
    runtime._runtime_started = True
    runtime._set_motor_mode = (  # type: ignore[method-assign]
        lambda enabled, wake=False: calls.append((enabled, wake))
    )

    response = client.post("/api/power", json={"mode": "awake"})

    assert response.status_code == 409
    assert response.json()["detail"] == "Voice runtime is still starting; no power transition was attempted"
    assert calls == []


def test_agent_profile_requires_explicit_unlocked_adult_ui(monkeypatch) -> None:
    _app, _runtime, client, saved = build_client(monkeypatch)
    assert client.post("/api/agent/profile", json={"profile": "agent"}).status_code == 403

    response = client.post(
        "/api/agent/profile",
        headers={"X-Reachy-Adult-UI": "unlocked"},
        json={"profile": "agent"},
    )
    assert response.status_code == 200
    assert response.json()["agent"]["profile"] == "agent"
    enabled = set(response.json()["agent"]["enabled_capabilities"])
    assert {
        "get_agent_capabilities",
        "get_reachy_status",
        "get_home_status",
        "search_current_information",
        "read_public_web_page",
        "recall_personal_context",
        "search_conversation_history",
        "read_scoped_note",
    } <= enabled
    assert {"control_home_entity", "set_timer", "draft_message", "append_scoped_note"} <= enabled
    assert saved[-1].capability_profile == "agent"


def test_presence_signal_requires_bearer_auth_and_rejects_unbounded_payloads(monkeypatch) -> None:
    _app, runtime, client, _saved = build_client(monkeypatch)
    monkeypatch.setattr(main_module, "load_config", lambda: AppConfig(api_key="presence-test-key"))
    observed: list[object] = []
    runtime.observe_presence = lambda value: observed.append(value) or {  # type: ignore[method-assign]
        "enabled": True,
        "level": "present",
        "speech_enabled": False,
    }
    payload = {
        "source": "home_assistant",
        "occupied": True,
        "attentive": False,
        "direction_degrees": 15,
        "confidence": 0.9,
    }

    assert client.post("/api/presence/signal", json=payload).status_code == 401
    response = client.post(
        "/api/presence/signal",
        headers={"Authorization": "Bearer presence-test-key"},
        json=payload,
    )
    assert response.status_code == 200
    assert response.json()["presence"]["speech_enabled"] is False
    assert len(observed) == 1

    for invalid in (
        {**payload, "source": "camera"},
        {**payload, "occupied": "yes"},
        {**payload, "direction_degrees": 90},
        {**payload, "person_name": "private identity"},
    ):
        rejected = client.post(
            "/api/presence/signal",
            headers={"Authorization": "Bearer presence-test-key"},
            json=invalid,
        )
        assert rejected.status_code == 422


def test_presence_signal_fails_closed_without_configured_bearer_key(monkeypatch) -> None:
    _app, _runtime, client, _saved = build_client(monkeypatch)
    monkeypatch.setattr(main_module, "load_config", lambda: AppConfig())

    response = client.post(
        "/api/presence/signal",
        headers={"Authorization": "Bearer anything"},
        json={"source": "trusted_sensor", "occupied": True},
    )

    assert response.status_code == 503


def test_initiative_settings_are_bounded_and_persisted(monkeypatch) -> None:
    _app, _runtime, client, saved = build_client(monkeypatch)

    response = client.post(
        "/api/settings",
        json={
            "initiative_policy_enabled": True,
            "initiative_mode": "balanced",
            "initiative_quiet_hours_enabled": True,
            "initiative_quiet_hours_start": "22:00",
            "initiative_quiet_hours_end": "07:00",
            "initiative_hourly_budget": 2,
            "initiative_daily_budget": 6,
        },
    )

    assert response.status_code == 200
    assert saved[-1].initiative_policy_enabled is True
    assert saved[-1].initiative_mode == "balanced"
    assert saved[-1].initiative_quiet_hours_start == "22:00"
    assert saved[-1].initiative_hourly_budget == 2

    assert client.post("/api/settings", json={"initiative_mode": "chatty"}).status_code == 422
    invalid_time = client.post("/api/settings", json={"initiative_quiet_hours_start": "99:99"})
    assert invalid_time.status_code == 400


def test_agent_routes_reject_kids_lock_and_stop_invalidates_generation(monkeypatch) -> None:
    _app, runtime, client, _saved = build_client(monkeypatch)
    agent = runtime.status()["agent"]
    assert isinstance(agent, dict)
    before = int(agent["session_generation"])
    runtime._kids_locked = True
    blocked = client.post(
        "/api/agent/profile",
        headers={"X-Reachy-Adult-UI": "unlocked"},
        json={"profile": "agent"},
    )
    assert blocked.status_code == 423

    stopped = client.post("/api/agent/stop")
    assert stopped.status_code == 200
    assert stopped.json()["agent"]["session_generation"] == before + 1


def test_fresh_runtime_uses_newer_agent_generation_after_restart(monkeypatch) -> None:
    epochs = iter((1_000_000_000, 2_000_000_000))
    monkeypatch.setattr("homebody.runtime.time.time_ns", lambda: next(epochs))

    first = HermesVoiceRuntime(SimpleNamespace(), threading.Event())
    second = HermesVoiceRuntime(SimpleNamespace(), threading.Event())

    first_agent = first.status()["agent"]
    second_agent = second.status()["agent"]
    assert isinstance(first_agent, dict)
    assert isinstance(second_agent, dict)
    first_generation = int(first_agent["session_generation"])
    second_generation = int(second_agent["session_generation"])
    assert first_generation == 1_000_000_000
    assert second_generation == 2_000_000_000
    assert second_generation > first_generation


def test_kids_start_wins_over_racing_agent_enable() -> None:
    runtime = HermesVoiceRuntime(SimpleNamespace(media=SimpleNamespace()), threading.Event())
    runtime._audio_ready = True
    agent_reached_privacy_check = threading.Event()
    allow_agent_to_continue = threading.Event()
    agent_errors: list[Exception] = []

    def controlled_power_mode() -> str:
        if threading.current_thread().name == "agent-enable":
            agent_reached_privacy_check.set()
            assert allow_agent_to_continue.wait(timeout=2.0)
        return "awake"

    runtime._effective_power_mode = controlled_power_mode  # type: ignore[method-assign]

    def enable_agent() -> None:
        try:
            runtime.set_capability_profile("agent", adult_ui_unlocked=True)
        except Exception as exc:
            agent_errors.append(exc)

    agent_thread = threading.Thread(target=enable_agent, name="agent-enable")
    agent_thread.start()
    assert agent_reached_privacy_check.wait(timeout=2.0)

    runtime.start_kids_mode(KidsProfile(duration_minutes=15), greet=False)
    allow_agent_to_continue.set()
    agent_thread.join(timeout=2.0)

    assert not agent_thread.is_alive()
    assert len(agent_errors) == 1
    assert isinstance(agent_errors[0], RuntimeError)
    with runtime._kids_lock, runtime._agent_lock:
        assert runtime._capability_profile == "conversation"
        assert runtime._kids_active is True
    runtime.stop_kids_mode(fold=False)


def test_status_has_strict_sanitized_agent_shape(monkeypatch) -> None:
    _app, _runtime, client, _saved = build_client(monkeypatch)
    payload = client.get("/api/status").json()
    assert set(payload["runtime"]["agent"]) == {
        "profile",
        "session_generation",
        "enabled_capabilities",
        "current_task",
        "pending_approval",
        "recent_activity",
    }
    serialized = str(payload)
    assert "sk-super-secret-value" not in serialized
    for unrestricted_name in ("terminal", "read_file", "write_file", "search_files", "execute_code"):
        assert unrestricted_name not in serialized


def test_status_never_exposes_agent_transcript_or_secret(monkeypatch) -> None:
    _app, runtime, client, _saved = build_client(monkeypatch)
    runtime.set_capability_profile("agent", adult_ui_unlocked=True)
    secret_request = "Read my note using " + "sk-" + "proj-" + "abcdefghijklmnopqrstuvwxyz"
    runtime._begin_agent_request(secret_request)
    with runtime._status_lock:
        runtime._status.transcript = secret_request
        runtime._status.response_preview = "private model answer with client_secret=hidden"
    payload = client.get("/api/status").json()
    serialized = str(payload)
    assert secret_request not in serialized
    assert "sk-proj" not in serialized
    assert "transcript" not in payload["runtime"]
    assert "response_preview" not in payload["runtime"]
    assert payload["runtime"]["agent"]["current_task"] == "Processing a bounded owner request"


def test_agent_activity_is_server_gated_by_adult_and_kids_state(monkeypatch) -> None:
    _app, runtime, client, _saved = build_client(monkeypatch)
    runtime.set_capability_profile("agent", adult_ui_unlocked=True)
    assert client.get("/api/agent/activity").status_code == 403
    runtime._kids_locked = True
    response = client.get(
        "/api/agent/activity", headers={"X-Reachy-Adult-UI": "unlocked"}
    )
    assert response.status_code == 423


def test_pending_approval_routes_require_adult_and_preserve_exact_body(monkeypatch) -> None:
    _app, runtime, client, _saved = build_client(monkeypatch)
    runtime.set_capability_profile("agent", adult_ui_unlocked=True)
    pending = {
        "draft_id": "draft-0123456789abcdef01234567",
        "capability_id": "send_approved_message",
        "arguments": {"channel": "mobile", "recipient": "tim", "text": "Exact body"},
        "expires_in_seconds": 300,
    }
    approved: list[str] = []

    class BridgeClient:
        def __init__(self, _config: AppConfig) -> None:
            pass

        def pending_agent_approval(self, _context):
            return pending

        def approve_pending_agent_action(self, draft_id: str, _context):
            approved.append(draft_id)
            return {"ok": True, "data": {"verified": True}, "side_effect": True}

        def close(self) -> None:
            pass

    monkeypatch.setattr(main_module, "HermesBridgeClient", BridgeClient)
    assert client.get("/api/agent/pending-approval").status_code == 403
    fetched = client.get(
        "/api/agent/pending-approval",
        headers={"X-Reachy-Adult-UI": "unlocked"},
    )
    assert fetched.status_code == 200
    assert fetched.json()["pending_approval"]["arguments"]["text"] == "Exact body"
    completed = client.post(
        "/api/agent/approve-pending",
        headers={"X-Reachy-Adult-UI": "unlocked"},
        json={"draft_id": pending["draft_id"]},
    )
    assert completed.status_code == 200
    assert completed.json()["verified"] is True
    assert approved == [pending["draft_id"]]


def test_timer_delivery_is_authenticated_and_blocked_by_kids(monkeypatch) -> None:
    _app, runtime, client, _saved = build_client(monkeypatch)
    monkeypatch.setattr(main_module, "load_config", lambda: AppConfig(api_key="bridge-secret"))
    queued: list[tuple[str, dict[str, object]]] = []
    runtime.queue_announcement = (  # type: ignore[method-assign]
        lambda text, **kwargs: queued.append((text, kwargs)) or {"queued": True}
    )
    body = {"item_id": "timer-0123456789abcdef", "text": "Timer finished."}
    assert client.post("/api/agent/reminder-delivery", json=body).status_code == 401
    delivered = client.post(
        "/api/agent/reminder-delivery",
        headers={"Authorization": "Bearer bridge-secret"},
        json=body,
    )
    assert delivered.status_code == 200
    assert queued == [
        (
            "Timer finished.",
            {"behavior": "voice_only", "repeat": 1, "pause_seconds": 0.0},
        )
    ]
    runtime._kids_locked = True
    blocked = client.post(
        "/api/agent/reminder-delivery",
        headers={"Authorization": "Bearer bridge-secret"},
        json=body,
    )
    assert blocked.status_code == 423


def test_agent_activity_poll_does_not_claim_or_finish_active_voice_request(monkeypatch) -> None:
    _app, runtime, client, _saved = build_client(monkeypatch)
    runtime.set_capability_profile("agent", adult_ui_unlocked=True)
    active_request_id, active_context = runtime._begin_agent_request("private owner request")

    class BridgeClient:
        def __init__(self, _config: AppConfig) -> None:
            pass

        def establish_agent_session(self, context) -> None:
            assert context.session_generation == active_context.session_generation

        def agent_activity(self, context, *, request_id: str):
            assert context.session_generation == active_context.session_generation
            assert request_id.startswith("agent-activity-")
            return [
                {
                    "event": "started",
                    "capability_id": "get_home_status",
                    "result_class": "running",
                }
            ]

        def close(self) -> None:
            pass

    monkeypatch.setattr(main_module, "HermesBridgeClient", BridgeClient)
    response = client.get(
        "/api/agent/activity", headers={"X-Reachy-Adult-UI": "unlocked"}
    )

    assert response.status_code == 200
    assert response.json()["activity"][0]["result_class"] == "running"
    with runtime._agent_lock:
        assert runtime._agent_active_request_id == active_request_id
        assert runtime._agent_current_task == "Processing a bounded owner request"
    assert runtime._finish_agent_request(
        active_request_id, active_context.session_generation, succeeded=True
    )


def test_agent_activity_cannot_return_after_kids_lock_races_request(monkeypatch) -> None:
    _app, runtime, client, _saved = build_client(monkeypatch)
    runtime.set_capability_profile("agent", adult_ui_unlocked=True)
    started = threading.Event()
    release = threading.Event()

    class BridgeClient:
        def __init__(self, _config: AppConfig) -> None:
            pass

        def establish_agent_session(self, _context) -> None:
            pass

        def agent_activity(self, _context, *, request_id: str):
            started.set()
            assert release.wait(timeout=2.0)
            return [{"event": "adult-secret"}]

        def close(self) -> None:
            pass

    monkeypatch.setattr(main_module, "HermesBridgeClient", BridgeClient)
    responses = []

    def fetch() -> None:
        responses.append(
            client.get("/api/agent/activity", headers={"X-Reachy-Adult-UI": "unlocked"})
        )

    worker = threading.Thread(target=fetch)
    worker.start()
    assert started.wait(timeout=2.0)
    with runtime._kids_lock:
        runtime._kids_active = True
        runtime._kids_locked = True
        runtime.cancel_agent_work("kids_mode")
    release.set()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert responses[0].status_code == 423
    assert "adult-secret" not in responses[0].text


def test_agent_audit_result_classes_match_request_outcomes(tmp_path) -> None:
    audit = AgentAuditLog(tmp_path / "agent-audit.jsonl")
    runtime = HermesVoiceRuntime(SimpleNamespace(), threading.Event(), agent_audit=audit)
    runtime._establish_remote_agent_session = lambda _context: None  # type: ignore[method-assign]
    runtime._publish_remote_agent_session = lambda: None  # type: ignore[method-assign]
    runtime.set_capability_profile("agent", adult_ui_unlocked=True)
    request_id, context = runtime._begin_agent_request("secret request body")
    assert runtime._finish_agent_request(request_id, context.session_generation, succeeded=True)
    request_id, context = runtime._begin_agent_request("another secret request")
    assert runtime._finish_agent_request(request_id, context.session_generation, succeeded=False)
    result_classes = {
        (item.get("reason"), item.get("result_class")) for item in audit.recent(limit=10)
    }
    assert ("request_started", "running") in result_classes
    assert ("request_completed", "success") in result_classes
    assert ("request_failed", "failed") in result_classes


def _agent_run_payload(status: str = "preview") -> dict[str, object]:
    return {
        "run_id": "run-" + "a" * 24,
        "goal": "Check two safe sources",
        "status": status,
        "generation": 1,
        "created_at": 1.0,
        "started_at": None,
        "completed_at": None,
        "active_step_id": "",
        "tool_calls_used": 0,
        "side_effects_used": 0,
        "resumable": True,
        "budgets": {
            "max_steps": 5,
            "max_tool_calls": 5,
            "max_side_effects": 2,
            "max_seconds": 120,
            "heartbeat_seconds": 15,
        },
        "steps": [
            {
                "step_id": "step-1",
                "capability_id": "get_reachy_status",
                "arguments": {},
                "status": "queued",
            }
        ],
    }


def test_agent_run_preview_is_trusted_ui_only_and_releases_owner_slot(monkeypatch) -> None:
    _app, runtime, client, _saved = build_client(monkeypatch)
    runtime.set_capability_profile("agent", adult_ui_unlocked=True)
    assert client.post("/api/agent/run/preview", json={"goal": "Check status"}).status_code == 403
    seen: dict[str, object] = {}

    class BridgeClient:
        def __init__(self, _config: AppConfig) -> None:
            pass

        def establish_agent_session(self, context) -> None:
            seen["generation"] = context.session_generation

        def preview_agent_run(self, goal: str, context, *, request_id: str):
            seen.update(goal=goal, request_id=request_id, context=context)
            return _agent_run_payload()

        def close(self) -> None:
            pass

    monkeypatch.setattr(main_module, "HermesBridgeClient", BridgeClient)
    response = client.post(
        "/api/agent/run/preview",
        headers={"X-Reachy-Adult-UI": "unlocked"},
        json={"goal": "Check status"},
    )

    assert response.status_code == 200
    assert response.json()["run"]["status"] == "preview"
    assert seen["goal"] == "Check status"
    assert str(seen["request_id"]).startswith("agent-")
    with runtime._agent_lock:
        assert runtime._agent_active_request_id == ""


def test_agent_run_actions_use_private_context_and_reject_stale_result(monkeypatch) -> None:
    _app, runtime, client, _saved = build_client(monkeypatch)
    runtime.set_capability_profile("agent", adult_ui_unlocked=True)
    calls: list[tuple[str, str, str, bool]] = []

    class BridgeClient:
        def __init__(self, _config: AppConfig) -> None:
            pass

        def agent_run_action(self, action: str, run_id: str, context, *, step_id: str = ""):
            calls.append((action, run_id, step_id, context.explicit_private_intent))
            return _agent_run_payload("running")

        def close(self) -> None:
            pass

    monkeypatch.setattr(main_module, "HermesBridgeClient", BridgeClient)
    run_id = "run-" + "a" * 24
    started = client.post(
        "/api/agent/run/start",
        headers={"X-Reachy-Adult-UI": "unlocked"},
        json={"run_id": run_id},
    )
    approved = client.post(
        "/api/agent/run/approve",
        headers={"X-Reachy-Adult-UI": "unlocked"},
        json={"run_id": run_id, "step_id": "step-1"},
    )

    assert started.status_code == 200
    assert approved.status_code == 200
    assert calls == [
        ("start", run_id, "", True),
        ("approve", run_id, "step-1", True),
    ]

    class RacingBridgeClient(BridgeClient):
        def agent_run_action(self, action: str, run_id: str, context, *, step_id: str = ""):
            runtime.cancel_agent_work("kids_mode")
            return _agent_run_payload("running")

    monkeypatch.setattr(main_module, "HermesBridgeClient", RacingBridgeClient)
    stale = client.post(
        "/api/agent/run/status",
        headers={"X-Reachy-Adult-UI": "unlocked"},
        json={"run_id": run_id},
    )
    assert stale.status_code == 423


def test_agent_05_trusted_ui_exposes_preview_budget_progress_and_control() -> None:
    root = main_module._STATIC_DIR
    html = (root / "index.html").read_text(encoding="utf-8")
    script = (root / "main.js").read_text(encoding="utf-8")
    worker = (root / "service-worker.js").read_text(encoding="utf-8")
    for element_id in (
        "agent-run-goal",
        "agent-run-preview-button",
        "agent-run-start-button",
        "agent-run-pause-button",
        "agent-run-resume-button",
        "agent-run-cancel-button",
        "agent-run-steps",
        "agent-run-budget",
    ):
        assert f'id="{element_id}"' in html
        assert element_id in script
    assert "/api/agent/run/status" in script
    assert "/api/agent/run/current" in script
    assert "Approve this exact step once?" in script
    assert "homebody-shell-v51" in worker


def test_settings_require_current_key_to_change_bridge_credentials(monkeypatch) -> None:
    _app, _runtime, client, saved = build_client(monkeypatch)

    hijack_key = client.post("/api/settings", json={"api_key": "attacker-key"})
    assert hijack_key.status_code == 403
    redirect = client.post("/api/settings", json={"bridge_url": "http://attacker.example:8643"})
    assert redirect.status_code == 403
    wrong_key = client.post(
        "/api/settings",
        json={"bridge_url": "http://attacker.example:8643", "current_api_key": "guess"},
    )
    assert wrong_key.status_code == 403
    assert saved == []

    unchanged = client.post(
        "/api/settings",
        json={"bridge_url": "http://127.0.0.1:8643/", "api_key": "********", "language": "nl"},
    )
    assert unchanged.status_code == 200
    assert saved[-1].api_key == "sk-super-secret-value"

    rotated = client.post(
        "/api/settings",
        json={
            "bridge_url": "http://hermes.local:8643",
            "api_key": "sk-rotated",
            "current_api_key": "sk-super-secret-value",
        },
    )
    assert rotated.status_code == 200
    assert saved[-1].bridge_url == "http://hermes.local:8643"
    assert saved[-1].api_key == "sk-rotated"


def test_settings_reject_unbounded_strings_and_report_save_failures(monkeypatch) -> None:
    _app, _runtime, client, saved = build_client(monkeypatch)

    assert client.post("/api/settings", json={"system_prompt": "x" * 16001}).status_code == 422
    assert client.post("/api/settings", json={"language": "x" * 13}).status_code == 422
    assert saved == []

    def failing_save(_value: AppConfig) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(main_module, "save_config", failing_save)
    response = client.post("/api/settings", json={"language": "nl"})
    assert response.status_code == 500
    assert response.json()["detail"] == "Settings could not be saved on Reachy"


def test_initiative_preference_routes_require_adult_ui_and_update_status(monkeypatch) -> None:
    _app, runtime, client, _saved = build_client(monkeypatch)
    adult = {"X-Reachy-Adult-UI": "unlocked"}

    weather_off = {"category": "weather", "disabled": True}
    assert client.post("/api/initiative/preferences", json=weather_off).status_code == 403
    assert client.post(
        "/api/initiative/preferences", headers=adult, json={"category": "shell", "disabled": True}
    ).status_code == 422
    assert client.post(
        "/api/initiative/preferences", headers=adult, json={"category": "weather", "disabled": "yes"}
    ).status_code == 422

    disabled = client.post("/api/initiative/preferences", headers=adult, json=weather_off)
    assert disabled.status_code == 200
    rows = {row["category"]: row for row in disabled.json()["preferences"]}
    assert rows["weather"]["state"] == "disabled"
    assert runtime._initiative.preferences.suppression_reason("weather") == "category_disabled"

    runtime._initiative.preferences.record("calendar", "dismissed")
    reset_one = client.post("/api/initiative/preferences/reset", headers=adult, json={"category": "calendar"})
    rows = {row["category"]: row for row in reset_one.json()["preferences"]}
    assert rows["calendar"]["dismissed"] == 0
    assert rows["weather"]["state"] == "disabled"

    reset_all = client.post("/api/initiative/preferences/reset", headers=adult, json={})
    assert all(row["state"] == "normal" for row in reset_all.json()["preferences"])
    assert client.post("/api/initiative/preferences/reset", json={}).status_code == 403


def test_contextual_offer_route_accepts_later(monkeypatch) -> None:
    _app, runtime, client, _saved = build_client(monkeypatch)
    calls: list[tuple[int, str]] = []
    runtime.respond_to_contextual_offer = lambda token, response: calls.append((token, response)) or {  # type: ignore[method-assign]
        "ok": True
    }

    response = client.post(
        "/api/initiative/offers/respond",
        headers={"X-Reachy-Adult-UI": "unlocked"},
        json={"token": 3, "response": "later"},
    )

    assert response.status_code == 200
    assert calls == [(3, "later")]
