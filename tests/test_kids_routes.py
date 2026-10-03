from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import homebody.main as main_module
from homebody.config import AppConfig, load_config
from homebody.main import Homebody
from homebody.runtime import HermesVoiceRuntime


def test_pin_routes_schema_and_legacy_config_are_removed(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"api_key": "test-key", "kids_parent_pin_hash": "legacy-scrypt-value"}),
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.api_key == "test-key"
    assert not hasattr(config, "kids_parent_pin_hash")

    app = Homebody(False)
    schema = app.settings_app.openapi()
    serialized = json.dumps(schema)
    assert not any("/kids/parent" in path for path in schema["paths"])
    assert "parent_pin" not in serialized
    assert "kids_parent_pin" not in serialized
    assert set(schema["components"]["schemas"]["KidsModeRequest"]["properties"]) == {
        "nickname",
        "age_band",
        "activity",
        "language",
        "duration_minutes",
        "motion_enabled",
        "camera_consent",
    }


def test_locked_status_route_uses_child_allowlist_and_redacts_runtime_text(monkeypatch) -> None:
    app = Homebody(False)
    runtime = HermesVoiceRuntime(SimpleNamespace(), threading.Event())
    runtime._kids_locked = True
    with runtime._status_lock:
        runtime._status.detail = "private detail"
        runtime._status.transcript = "private child transcript"
        runtime._status.response_preview = "private model answer"
        runtime._status.announcement_current_preview = "Hello private nickname"
        runtime._status.announcement_last_text = "Hello private nickname"
        runtime._status.announcement_last_error = "private bridge URL"
        runtime._status.camera_last_error = "private camera error"
        runtime._status.robot_action_last_error = "private robot error"
        runtime._status.stt_provider = "private-stt"
        runtime._status.tts_provider = "private-tts"
        runtime._status.last_error = "private provider error"
    app._runtime = runtime
    config = AppConfig(
        bridge_url="https://private-bridge.invalid",
        api_key="private-key",
        system_prompt="private management prompt",
    )
    monkeypatch.setattr(main_module, "load_config", lambda: config)

    response = TestClient(app.settings_app).get("/api/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["config"] == {
        "configured": True,
        "api_key_configured": True,
    }
    assert set(payload["runtime"]) == {
        "state",
        "power_mode",
        "motors_enabled",
        "head_safely_folded",
        "kids_mode",
    }
    serialized = str(payload)
    for private_value in (
        "private child transcript",
        "private model answer",
        "Hello private nickname",
        "private bridge URL",
        "private provider error",
        "private detail",
        "private camera error",
        "private robot error",
        "private-stt",
        "private-tts",
        "private management prompt",
        "https://private-bridge.invalid",
        "private-key",
    ):
        assert private_value not in serialized


def test_locked_child_status_omits_agent_session_details(monkeypatch) -> None:
    app = Homebody(False)
    runtime = HermesVoiceRuntime(SimpleNamespace(), threading.Event())
    runtime.set_capability_profile("agent", adult_ui_unlocked=True)
    runtime._kids_locked = True
    app._runtime = runtime
    monkeypatch.setattr(main_module, "load_config", lambda: AppConfig(api_key="secret-value"))
    assert app.settings_app is not None

    payload = TestClient(app.settings_app).get("/api/status").json()

    assert "agent" not in payload["runtime"]
    assert "secret-value" not in str(payload)
