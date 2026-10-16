"""Real owner middleware: the admitting owner session is bound to a held look and revalidated.

The runtime, routes and owner store are real; only the vision transport is a fake.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from test_companion import make_runtime
from test_owner_auth import ORIGIN, pair

from homebody.main import Homebody


@pytest.fixture
def owner_app(monkeypatch, tmp_path):
    import homebody.main as main

    monkeypatch.setattr(main, "default_config_path", lambda: tmp_path / "config.json")
    runtime, client, captures = make_runtime()
    monkeypatch.setattr(main, "load_config", lambda: runtime.config_loader())
    robot = Homebody()
    code = robot.settings_app.state.owner_store.provision(ORIGIN)
    robot._runtime = runtime  # type: ignore[assignment]
    http = TestClient(robot.settings_app, base_url=ORIGIN)
    assert pair(http, code).status_code == 200
    session = http.get("/api/owner/session").json()
    assert session.get("owner") is True, session
    headers = {"Origin": ORIGIN, "X-Homebody-CSRF": session["csrf"]}
    return robot, http, headers, runtime, client, captures


def test_owner_revoked_while_look_is_held_discards_result_and_new_owner_cannot_adopt_it(owner_app):
    robot, http, headers, runtime, client, captures = owner_app
    client.release.clear()
    outcome: dict[str, object] = {}

    def run() -> None:
        outcome["response"] = http.post("/api/companion/look", json={"question": "read this"}, headers=headers)

    worker = threading.Thread(target=run)
    worker.start()
    for _ in range(500):
        if client.looks:
            break
        threading.Event().wait(0.01)
    assert captures == [1] and client.looks
    # Revoke the exact admitting session through the real owner store while the provider is blocked.
    device_id = http.get("/api/owner/session").json()["device_id"]
    robot.settings_app.state.owner_store.revoke(device_id)
    client.release.set()
    worker.join(5)
    response = outcome["response"]
    assert response.status_code == 409, response.text  # type: ignore[union-attr]
    assert "dentist" not in response.text  # type: ignore[union-attr]
    assert runtime._look_context is None
    # A newly paired owner starts clean: no adoption of the old analysis.
    code = robot.settings_app.state.owner_store.provision(ORIGIN)
    fresh = TestClient(robot.settings_app, base_url=ORIGIN)
    assert pair(fresh, code).status_code == 200
    csrf = fresh.get("/api/owner/session").json()["csrf"]
    fresh_headers = {"Origin": ORIGIN, "X-Homebody-CSRF": csrf}
    follow = fresh.post("/api/companion/look/follow-up", json={"question": "what day?"}, headers=fresh_headers)
    assert follow.status_code == 409 and "look first" in follow.json()["detail"]
    # Positive control: the fresh owner can take a fresh look.
    good = fresh.post("/api/companion/look", json={"question": "read this"}, headers=fresh_headers)
    assert good.status_code == 200 and "dentist" in good.json()["answer"]


def test_owner_stop_route_clears_private_details_and_csrf_is_still_required(owner_app):
    _robot, http, headers, runtime, _client, _captures = owner_app
    runtime._remember_private_detail("calendar", "Dentist at eleven")
    assert http.get("/api/companion/status").json()["private_details"]
    assert http.post("/api/companion/stop", headers={"Origin": ORIGIN}).status_code == 403
    assert http.post("/api/companion/stop", headers=headers).status_code == 200
    assert http.get("/api/companion/status").json()["private_details"] == []


def test_owner_sources_routes_report_setup_needed_without_a_bridge_and_validate_shape(owner_app, monkeypatch):
    _robot, http, headers, runtime, _client, _captures = owner_app
    config = runtime.config_loader()
    config.bridge_url = ""
    catalog = http.get("/api/companion/sources")
    assert catalog.status_code == 200 and catalog.json()["available"] is False
    assert "Connect the Hermes bridge" in catalog.json()["setup_needed"][0]
    malformed = {"home_assistant": [{"id": "x"}]}
    assert http.post("/api/companion/sources", json=malformed, headers=headers).status_code == 422
    too_many = {
        "calendar": [
            {"id": "a", "label": "A", "calendar_entity_id": "calendar.a"},
            {"id": "b", "label": "B", "calendar_entity_id": "calendar.b"},
        ]
    }
    assert http.post("/api/companion/sources", json=too_many, headers=headers).status_code == 422
