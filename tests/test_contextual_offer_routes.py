from __future__ import annotations

from fastapi.testclient import TestClient

import homebody.main as main_module
from homebody.config import AppConfig
from homebody.main import Homebody

OFFER = {
    "source": "weather",
    "topic": "rain_soon",
    "confidence": 0.9,
    "fingerprint": "weather-rain-1",
    "text": "Rain is expected soon; would you like the short forecast?",
    "accepted_text": "Light rain is expected within the next hour.",
}


class Runtime:
    def __init__(self) -> None:
        self.offer = None
        self.responses: list[tuple[int, str]] = []

    def submit_contextual_offer(self, offer):  # type: ignore[no-untyped-def]
        self.offer = offer
        return {"ok": True, "queued": True, "token": 7}

    def respond_to_contextual_offer(self, token: int, response: str) -> dict[str, object]:
        self.responses.append((token, response))
        return {"ok": True, "token": token, "response": response, "action_executed": False}

    @property
    def kids_controls_locked(self) -> bool:
        return False


def test_offer_submission_requires_bearer_and_forbids_unknown_context(monkeypatch) -> None:
    app = Homebody(False)
    runtime = Runtime()
    app._runtime = runtime  # type: ignore[assignment]
    monkeypatch.setattr(main_module, "load_config", lambda: AppConfig(api_key="secret"))
    client = TestClient(app.settings_app)

    assert client.post("/api/initiative/offers", json=OFFER).status_code == 401
    invalid = client.post(
        "/api/initiative/offers",
        headers={"Authorization": "Bearer secret"},
        json={**OFFER, "source": "email"},
    )
    assert invalid.status_code == 422
    presentation_spoof = client.post(
        "/api/initiative/offers",
        headers={"Authorization": "Bearer secret"},
        json={**OFFER, "source": "presentation"},
    )
    assert presentation_spoof.status_code == 422
    accepted = client.post(
        "/api/initiative/offers",
        headers={"Authorization": "Bearer secret"},
        json=OFFER,
    )
    assert accepted.status_code == 200
    assert accepted.json()["token"] == 7
    assert runtime.offer is not None
    assert runtime.offer.source == "weather"


def test_phone_response_requires_unlocked_adult_ui_and_executes_no_action(monkeypatch) -> None:
    app = Homebody(False)
    runtime = Runtime()
    app._runtime = runtime  # type: ignore[assignment]
    monkeypatch.setattr(main_module, "load_config", lambda: AppConfig(api_key="secret"))
    client = TestClient(app.settings_app)

    denied = client.post("/api/initiative/offers/respond", json={"token": 7, "response": "yes"})
    assert denied.status_code == 403
    accepted = client.post(
        "/api/initiative/offers/respond",
        headers={"X-Reachy-Adult-UI": "unlocked"},
        json={"token": 7, "response": "no"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["action_executed"] is False
    assert runtime.responses == [(7, "no")]
