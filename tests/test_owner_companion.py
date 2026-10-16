"""Companion owner routes through the real owner middleware: pairing, CSRF, Kids lock, no private leak."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_owner_auth import ORIGIN, pair

from homebody.config import AppConfig
from homebody.main import Homebody


class Runtime:
    kids_controls_locked = False

    def __init__(self) -> None:
        self.looks: list[tuple[str, str]] = []
        self.follow_ups: list[str] = []
        self.stops = 0
        self.fail = ""

    def look_at_this(self, question: str, *, origin: str = "phone", authority=None) -> dict[str, object]:
        if self.fail:
            raise RuntimeError(self.fail)
        assert callable(authority) and authority() is True  # the route binds the admitting owner session
        self.looks.append((question, origin))
        return {"ok": True, "answer": "A letter.", "captures": 1, "processing": "host_provider", "model": "x"}

    def follow_up_look(self, question: str, *, origin: str = "phone", authority=None) -> dict[str, object]:
        assert callable(authority) and authority() is True
        self.follow_ups.append(question)
        return {"ok": True, "answer": "Friday.", "captures": 0}

    def companion_stop(self) -> dict[str, object]:
        self.stops += 1
        return {"ok": True, "stopped": ["look", "speech", "offer"]}

    def companion_owner_status(self) -> dict[str, object]:
        return {"private_details": [{"source": "calendar", "detail": "Dentist 15:00", "seconds_remaining": 100}]}

    def status(self) -> dict[str, object]:
        return {"state": "waiting_for_wake_word", "kids_mode": {"active": False, "locked": False}}


@pytest.fixture
def application(monkeypatch, tmp_path):
    import homebody.main as main

    monkeypatch.setattr(main, "default_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(main, "load_config", lambda: AppConfig(camera_enabled=True))
    robot = Homebody()
    code = robot.settings_app.state.owner_store.provision(ORIGIN)
    runtime = Runtime()
    robot._runtime = runtime  # type: ignore[assignment]
    return robot, TestClient(robot.settings_app, base_url=ORIGIN), code, runtime


def test_owner_companion_look_requires_owner_and_csrf_then_reaches_runtime(application):
    _robot, client, code, runtime = application
    body = {"question": "What does this say?"}
    assert client.post("/api/companion/look", json=body, headers={"X-Reachy-Adult-UI": "unlocked"}).status_code == 401
    assert pair(client, code).status_code == 200
    csrf = client.get("/api/owner/session").json()["csrf"]
    assert client.post("/api/companion/look", json=body, headers={"Origin": ORIGIN}).status_code == 403
    headers = {"Origin": ORIGIN, "X-Homebody-CSRF": csrf}
    response = client.post("/api/companion/look", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["answer"] == "A letter." and runtime.looks == [("What does this say?", "phone")]
    assert client.post("/api/companion/look", json={"question": ""}, headers=headers).status_code == 422
    assert client.post("/api/companion/look", json={"question": "x" * 501}, headers=headers).status_code == 422
    follow = client.post("/api/companion/look/follow-up", json={"question": "What day?"}, headers=headers)
    assert follow.status_code == 200 and follow.json()["captures"] == 0 and runtime.follow_ups == ["What day?"]
    runtime.fail = "Camera capture is blocked in the current privacy mode"
    blocked = client.post("/api/companion/look", json=body, headers=headers)
    assert blocked.status_code == 409 and "privacy" in blocked.json()["detail"]
    stopped = client.post("/api/companion/stop", headers=headers)
    assert stopped.status_code == 200 and runtime.stops == 1


def test_owner_companion_private_details_are_owner_only_and_kids_lock_blocks_looks(application):
    robot, client, code, runtime = application
    assert client.get("/api/companion/status").status_code == 401
    assert pair(client, code).status_code == 200
    csrf = client.get("/api/owner/session").json()["csrf"]
    headers = {"Origin": ORIGIN, "X-Homebody-CSRF": csrf}
    status = client.get("/api/companion/status")
    assert status.status_code == 200 and status.json()["private_details"][0]["detail"] == "Dentist 15:00"
    robot._runtime = SimpleNamespace(  # type: ignore[assignment]
        kids_controls_locked=True,
        status=lambda: {"kids_mode": {"active": True, "locked": True}},
        look_at_this=runtime.look_at_this,
    )
    assert client.post("/api/companion/look", json={"question": "read"}, headers=headers).status_code == 423
    assert runtime.looks == []
