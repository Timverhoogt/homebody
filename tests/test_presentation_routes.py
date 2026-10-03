from __future__ import annotations

from fastapi.testclient import TestClient

from homebody.main import Homebody


class Runtime:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start_presentation_window(self) -> dict[str, object]:
        self.started += 1
        return {
            "enabled": True,
            "state": "watching",
            "reason": "waiting_for_presented_object_or_text",
            "visible_indicator": True,
            "expires_seconds_remaining": 20,
            "semantic_analysis": False,
            "frames_retained": 0,
        }

    def stop_presentation_window(self, reason: str) -> dict[str, object]:
        self.stopped += 1
        return {
            "enabled": True,
            "state": "cancelled",
            "reason": reason,
            "visible_indicator": False,
            "expires_seconds_remaining": 0,
            "semantic_analysis": False,
            "frames_retained": 0,
        }

    @property
    def kids_controls_locked(self) -> bool:
        return False


def test_presentation_routes_require_unlocked_adult_ui() -> None:
    app = Homebody(False)
    runtime = Runtime()
    app._runtime = runtime  # type: ignore[assignment]
    client = TestClient(app.settings_app)

    assert client.post("/api/presentation/start").status_code == 403
    started = client.post(
        "/api/presentation/start",
        headers={"X-Reachy-Adult-UI": "unlocked"},
    )
    assert started.status_code == 200
    assert started.json()["presentation"]["state"] == "watching"
    assert runtime.started == 1

    assert client.post("/api/presentation/stop").status_code == 403
    stopped = client.post(
        "/api/presentation/stop",
        headers={"X-Reachy-Adult-UI": "unlocked"},
    )
    assert stopped.status_code == 200
    assert stopped.json()["presentation"]["reason"] == "user_stopped"
    assert runtime.stopped == 1
