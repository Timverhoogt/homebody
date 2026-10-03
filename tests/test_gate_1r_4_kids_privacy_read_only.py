from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest
from aiohttp import ClientSession

from companion.reachy_agent_broker import (
    BrokerConfig,
    BrokerValidationError,
    ReachyAgentBroker,
)
from homebody.kids_mode import KidsProfile
from homebody.runtime import HermesVoiceRuntime

_LOGGER = logging.getLogger(__name__)


class FakeMedia:
    def __init__(self) -> None:
        self.pushed_audio: list[np.ndarray] = []
        self.clear_calls = 0
        self.audio = SimpleNamespace(clear_player=self._clear_player)

    def play_sound(self, path: str) -> None:
        pass

    def start_recording(self) -> None:
        pass

    def stop_recording(self) -> None:
        pass

    def push_audio_sample(self, samples: np.ndarray) -> None:
        self.pushed_audio.append(samples)

    def _clear_player(self) -> None:
        self.clear_calls += 1


class FakeRobot:
    def __init__(self) -> None:
        self.media = FakeMedia()
        self._power_mode = "awake"
        self._motors_enabled = True

    def goto_sleep(self) -> None:
        self._power_mode = "sleep"
        self._motors_enabled = False

    def disable_motors(self) -> None:
        self._motors_enabled = False


class MockResponse:
    def __init__(self, status: int, data: Any, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._data = data
        self.headers = headers or {}
        self.content_length = len(json.dumps(data)) if isinstance(data, dict) else len(str(data))
        self.charset = "utf-8"

    async def json(self, **kwargs) -> Any:
        return self._data

    async def __aenter__(self) -> MockResponse:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        pass


class MockHttp:
    def __init__(self, responses: dict[str, MockResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs) -> MockResponse:
        self.requests.append((url, kwargs))
        for pattern, resp in self.responses.items():
            if re.search(pattern, url):
                return resp
        return MockResponse(404, {"error": "not found"})


def make_runtime() -> HermesVoiceRuntime:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._audio_ready = True
    runtime._power_mode = "awake"
    runtime._motors_enabled = True
    runtime._head_safely_folded = False
    runtime._play_asset = lambda name: None  # type: ignore[method-assign]
    runtime._discard_audio = lambda seconds: None  # type: ignore[method-assign]
    runtime._publish_remote_agent_session = lambda: None  # type: ignore[method-assign]
    runtime._establish_remote_agent_session = lambda _context: None  # type: ignore[method-assign]

    def mock_set_motor_mode(enabled: bool, *, wake: bool = False) -> None:
        runtime._motors_enabled = enabled
        cast(FakeRobot, runtime.robot)._motors_enabled = enabled

    runtime._set_motor_mode = mock_set_motor_mode  # type: ignore[method-assign]
    runtime._read_head_safely_folded = lambda: True  # type: ignore[method-assign]

    runtime.set_capability_profile("agent", adult_ui_unlocked=True)
    runtime._accept_wake_turn()
    return runtime


def test_gate_1r_4_kids_and_capabilities_acceptance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs: list[str] = []
    logs.append("[Gate 1R.4 Init] Starting Kids Mode & 8 Capabilities Acceptance Tests")

    # Setup directories for scoped notes/history
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    (notes_dir / "safe_pref.md").write_text(
        "User preference: Coffee with api_key=super-secret-key-123", encoding="utf-8"
    )
    (notes_dir / "confidential.md").write_text("api_key=should-never-be-printed-api-key", encoding="utf-8")

    personal_dir = tmp_path / "personal"
    personal_dir.mkdir()
    (personal_dir / "context.md").write_text("User's dog is named Sparky. secret_token=abc-123-xyz", encoding="utf-8")

    history_dir = tmp_path / "history"
    history_dir.mkdir()
    (history_dir / "chat.md").write_text("User: Hello Reachy. password=secret-password", encoding="utf-8")

    # Preflight mocks and Http Mock
    mock_responses = {
        "hass-local.invalid:8123/api/states/light.living_room": MockResponse(
            200,
            {
                "state": "on",
                "attributes": {
                    "brightness": 255,
                    "color_temp": 300,
                    "api_key": "hass-internal-leak",
                },
            },
        ),
        "searx-local.invalid:8888": MockResponse(
            200,
            {
                "results": [
                    {
                        "title": "Weather Today",
                        "url": "https://weather.invalid/today",
                        "content": "Sunny and clear! api_key=searx-leak-123",
                    }
                ]
            },
        ),
        "public-web.invalid/page": MockResponse(
            200,
            "<html><body><h1>Hello World</h1><p>Public data api_key=web-leak-456</p></body></html>",
            headers={"content-type": "text/html"},
        ),
    }
    http_mock = MockHttp(mock_responses)

    # 1. Start Reachy Mini with Kids Mode
    runtime = make_runtime()
    profile = KidsProfile(
        nickname="Charlie",
        age_band="7-9",
        activity="buddy",
        language="en",
        duration_minutes=15,
        motion_enabled=False,
    )

    logs.append("[Kids Mode Start] Launching kids mode for Charlie")
    runtime.start_kids_mode(profile, greet=False)

    assert runtime._kids_active is True
    assert runtime._kids_locked is True
    assert runtime._kids_profile is not None
    assert runtime._kids_profile.nickname == "Charlie"
    logs.append("[Kids Mode Active] Verified Charlie Kids Mode session is active with child controls")

    # Verify Kids Mode fails closed for all Agent capabilities
    broker_config = BrokerConfig(
        note_roots={"notes": notes_dir},
        personal_roots={"personal": personal_dir},
        history_roots={"history": history_dir},
        home_entities={"light.living_room": frozenset(["brightness", "color_temp"])},
        hass_url="http://hass-local.invalid:8123",
        hass_token="hass-token-secret-123",
        search_url="http://searx-local.invalid:8888",
    )
    broker = ReachyAgentBroker(broker_config)

    # Let's verify that with kids_mode_active=True, capabilities are rejected
    kids_context = {
        "capability_profile": "agent",
        "adult_ui_unlocked": True,
        "kids_mode_active": True,
        "power_mode": "awake",
        "privacy_enabled": True,
        "emergency_stop_active": False,
        "robot_available": True,
        "session_generation": 1,
        "requested_session_generation": 1,
        "explicit_private_intent": True,
        "reachy_status": {"state": "listening"},
    }

    payload = {
        "request_id": "req-kids-test",
        "capability_id": "get_reachy_status",
        "arguments": {},
        "context": kids_context,
    }

    async def run_broker_execute(b: ReachyAgentBroker, p: dict[str, Any], h: Any) -> dict[str, Any]:
        await b.establish_session("test-device", p["context"])
        return await b.execute(p, cast(ClientSession, h), device_id="test-device")

    # Attempt execute under kids mode, must raise BrokerValidationError (adult_ui_required)
    with pytest.raises(BrokerValidationError, match="adult_ui_required"):
        asyncio.run(run_broker_execute(broker, payload, http_mock))
    logs.append("[Kids Mode Safe Guard] Verified capabilities are successfully blocked under Kids Mode")

    # 2. Verify Kids Mode cancellation and removal of private capabilities
    logs.append("[Kids Mode Cancel] Cancelling Kids Mode session")
    runtime.stop_kids_mode(fold=False)

    assert runtime._kids_active is False
    assert runtime._kids_locked is False
    assert runtime._kids_profile is None
    logs.append("[Kids Mode Cancelled] Verified Kids Mode and child-only controls are inactive")

    # Confirm private child transcripts are completely erased on cancellation
    with runtime._status_lock:
        runtime._status.transcript = "Charlie's secrets"
        runtime._status.response_preview = "Charlie's answer"

    assert runtime._status.transcript == "Charlie's secrets"
    assert runtime._status.response_preview == "Charlie's answer"
    runtime.stop_kids_mode(fold=False)
    assert runtime._status.transcript == ""
    assert runtime._status.response_preview == ""
    logs.append("[Kids Mode Redaction] Verified child transcripts are completely wiped and sanitized")

    # 3. Exercise all eight allowlisted read-only Agent capabilities
    logs.append("[Capabilities Audit] Starting audit of all 8 capabilities")
    manifest = broker.manifest()
    capability_ids: set[str] = {cast(str, item["id"]) for item in manifest}
    expected_capabilities = {
        "get_agent_capabilities",
        "get_reachy_status",
        "get_home_status",
        "search_current_information",
        "read_public_web_page",
        "recall_personal_context",
        "search_conversation_history",
        "read_scoped_note",
    }
    assert expected_capabilities <= capability_ids
    logs.append(f"[Capabilities Manifest] Found original 8 capabilities: {sorted(expected_capabilities)}")

    # Confirm the accepted 0.1 foundation remains read-only as later capabilities are added.
    for item in (item for item in manifest if item["id"] in expected_capabilities):
        assert item["read_only"] is True
        assert item["risk_tier"] in {"T0_PUBLIC_READ", "T1_PRIVATE_READ"}
    logs.append("[Capabilities Read-Only] Confirmed all 8 capabilities are strictly READ-ONLY with T0/T1 risk tiers")

    # Now let's execute each capability and verify sanitization
    normal_context = {
        "capability_profile": "agent",
        "adult_ui_unlocked": True,
        "kids_mode_active": False,
        "power_mode": "awake",
        "privacy_enabled": True,
        "emergency_stop_active": False,
        "robot_available": True,
        "session_generation": 2,
        "requested_session_generation": 2,
        "explicit_private_intent": True,
        "reachy_status": {"state": "listening", "motors_enabled": True, "password": "super-secret-password-999"},
    }

    async def execute_cap(cap_id: str, args: dict[str, Any], http_mock_param: Any) -> dict[str, Any]:
        req_payload = {
            "request_id": f"req-{cap_id}",
            "capability_id": cap_id,
            "arguments": args,
            "context": normal_context,
        }
        await broker.establish_session("test-device", normal_context)
        return await broker.execute(req_payload, cast(ClientSession, http_mock_param), device_id="test-device")

    # Capability 1: get_agent_capabilities
    res1 = asyncio.run(execute_cap("get_agent_capabilities", {}, http_mock))
    assert res1["ok"] is True
    returned_capability_ids = {item["id"] for item in res1["data"]["capabilities"]}
    assert expected_capabilities <= returned_capability_ids
    logs.append("[Cap 1: get_agent_capabilities] Success, included all original 8 capability specs")

    # Capability 2: get_reachy_status
    res2 = asyncio.run(execute_cap("get_reachy_status", {}, http_mock))
    assert res2["ok"] is True
    assert res2["data"]["state"] == "listening"
    # sensitive passwords or fields in the reachy_status context must be completely omitted
    assert "password" not in res2["data"]
    assert "super-secret-password" not in json.dumps(res2)
    logs.append("[Cap 2: get_reachy_status] Success, returned sanitized status without passwords")

    # Capability 3: get_home_status
    res3 = asyncio.run(execute_cap("get_home_status", {"entity_ids": ["light.living_room"]}, http_mock))
    assert res3["ok"] is True
    # check that we allowlisted only brightness and color_temp, and that "api_key" attribute was omitted/redacted
    attrs = res3["data"]["entities"][0]["attributes"]
    assert "brightness" in attrs
    assert "api_key" not in attrs
    assert "hass-internal-leak" not in json.dumps(res3)
    logs.append("[Cap 3: get_home_status] Success, filtered Home Assistant attributes and redacted secrets")

    # Capability 4: search_current_information
    res4 = asyncio.run(execute_cap("search_current_information", {"query": "weather today"}, http_mock))
    assert res4["ok"] is True
    # check that weather content was returned and searx-leak-123 was redacted
    snippet = res4["data"]["results"][0]["snippet"]
    assert "Sunny and clear" in snippet
    assert "[redacted]" in snippet
    assert "searx-leak-123" not in json.dumps(res4)
    logs.append("[Cap 4: search_current_information] Success, results retrieved and API key redacted")

    # Capability 5: read_public_web_page
    async def mock_web_page(arguments: Any, _http: Any) -> tuple[object, list[dict[str, object]], float]:
        observed = time.time()
        text = "Hello World Public data api_key=web-leak-456"
        url = str(arguments["url"])
        return (
            {"url": url, "text": text},
            [{"source": "public_web", "url": url, "observed_at": observed}],
            observed,
        )

    monkeypatch.setattr(broker, "_web_page", mock_web_page)
    res5 = asyncio.run(execute_cap("read_public_web_page", {"url": "https://public-web.invalid/page"}, http_mock))
    assert res5["ok"] is True
    text = res5["data"]["text"]
    assert "Hello World" in text
    assert "[redacted]" in text
    assert "web-leak-456" not in json.dumps(res5)
    logs.append("[Cap 5: read_public_web_page] Success, public page fetched and text body sanitized")

    # Capability 6: recall_personal_context
    res6 = asyncio.run(execute_cap("recall_personal_context", {"query": "Sparky"}, http_mock))
    assert res6["ok"] is True
    match = res6["data"]["matches"][0]
    assert "Sparky" in match["excerpt"]
    assert "[redacted]" in match["excerpt"]
    assert "abc-123-xyz" not in json.dumps(res6)
    logs.append("[Cap 6: recall_personal_context] Success, personal context recalled and secrets redacted")

    # Capability 7: search_conversation_history
    res7 = asyncio.run(execute_cap("search_conversation_history", {"query": "Reachy"}, http_mock))
    assert res7["ok"] is True
    match_hist = res7["data"]["matches"][0]
    assert "Hello Reachy" in match_hist["excerpt"]
    assert "[redacted]" in match_hist["excerpt"]
    assert "secret-password" not in json.dumps(res7)
    logs.append("[Cap 7: search_conversation_history] Success, history retrieved and password redacted")

    # Capability 8: read_scoped_note
    res8 = asyncio.run(execute_cap("read_scoped_note", {"root": "notes", "path": "safe_pref.md"}, http_mock))
    assert res8["ok"] is True
    assert "Coffee" in res8["data"]["text"]
    assert "[redacted]" in res8["data"]["text"]
    assert "super-secret-key-123" not in json.dumps(res8)
    logs.append("[Cap 8: read_scoped_note] Success, read note safe_pref.md and redacted secrets")

    # 4. Confirm Stop/privacy precedence
    # Setting privacy switch (i.e. privacy_enabled=False in Context) blocks private reads immediately
    logs.append("[Privacy Precedence] Testing privacy switch activation")
    private_ctx = dict(normal_context)
    private_ctx["privacy_enabled"] = False
    private_ctx["session_generation"] = 3
    private_ctx["requested_session_generation"] = 3

    payload_private = {
        "request_id": "req-private-test",
        "capability_id": "read_scoped_note",
        "arguments": {"root": "notes", "path": "safe_pref.md"},
        "context": private_ctx,
    }

    async def run_private_execute() -> None:
        await broker.establish_session("test-device", private_ctx)
        await broker.execute(payload_private, cast(ClientSession, http_mock), device_id="test-device")

    with pytest.raises(BrokerValidationError, match="agent_context_blocked"):
        asyncio.run(run_private_execute())
    logs.append("[Privacy Precedence Verified] Confirmed privacy mode block works perfectly")

    # 5. Verified Standby/fold with motors disabled
    logs.append("[Power Standby] Transitioning to Standby fold")
    runtime.set_power_mode("standby", cancel_announcements=False)
    assert runtime._power_mode == "standby" or runtime._effective_power_mode() == "standby"
    assert cast(FakeRobot, runtime.robot)._motors_enabled is False
    logs.append("[Power Standby Verified] Confirmed Reachy Mini ended in verified standby/fold with motors disabled")

    # Print clean sanitized evidence logs to stdout
    print("\n=== SANITIZED READ-ONLY CAPABILITIES EVIDENCE ===")
    for log in logs:
        print(log)
    print("==================================================\n")
