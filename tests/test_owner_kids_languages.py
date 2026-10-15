"""Owner boundary for ES/DE/FR/IT/PT Kids starts (real OwnerBoundary middleware, fake bridge health)."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_owner_auth import ORIGIN, pair

from homebody.config import AppConfig
from homebody.main import Homebody

PROOF = {"kids_voice_sha256": "c" * 64, "kids_tts_model": "eleven_flash_v2_5"}
NAMES = {"es": "Spanish", "de": "German", "fr": "French", "it": "Italian", "pt": "Portuguese"}


@pytest.fixture
def application(monkeypatch, tmp_path):
    import homebody.main as main

    monkeypatch.setattr(main, "default_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(main, "load_config", lambda: AppConfig(bridge_url="http://bridge", api_key="k"))
    # Synthetic health: EN/NL proofs only. No new-language proof is inherited from them.
    health = {"kids_chat_available": True, "kids_tts_streaming_available": True,
              "kids_voice_proofs": {"en": PROOF, "nl": PROOF}}

    class Client:
        def __init__(self, _config):
            pass

        def health(self):
            return health

        def close(self):
            pass

    monkeypatch.setattr(main, "HermesBridgeClient", Client)
    robot = Homebody()
    code = robot.settings_app.state.owner_store.provision(ORIGIN)
    starts = []
    robot._runtime = SimpleNamespace(
        kids_controls_locked=False,
        start_kids_mode=lambda profile: starts.append(profile) or {"active": True, "profile": profile.public_dict()},
    )
    return robot, TestClient(robot.settings_app, base_url=ORIGIN), code, starts, health


def owner_headers(client) -> dict[str, str]:
    session = client.get("/api/owner/session").json()
    return {"Origin": ORIGIN, "X-Homebody-CSRF": session["csrf"]}


@pytest.mark.parametrize("language", sorted(NAMES))
def test_owner_start_admits_a_new_language_only_with_its_own_proof(application, language) -> None:
    _robot, client, code, starts, health = application
    assert client.post("/api/kids/start", json={"language": language}).status_code == 401
    assert pair(client, code).status_code == 200
    headers = owner_headers(client)
    response = client.post("/api/kids/start", json={"language": language}, headers=headers)
    assert response.status_code == 409 and f"{NAMES[language]} voice is not configured" in response.text
    for wrong in ({}, {"kids_voice_sha256": "c" * 64, "kids_tts_model": "eleven_multilingual_v2"},
                  {"kids_voice_sha256": "C" * 64, "kids_tts_model": "eleven_flash_v2_5"}, "proof"):
        health["kids_voice_proofs"][language] = wrong
        assert client.post("/api/kids/start", json={"language": language}, headers=headers).status_code == 409
    assert starts == []
    health["kids_voice_proofs"][language] = PROOF
    # CSRF is still required before admission is even considered.
    assert client.post("/api/kids/start", json={"language": language}, headers={"Origin": ORIGIN}).status_code == 403
    assert starts == []
    response = client.post("/api/kids/start", json={"activity": "story", "language": language}, headers=headers)
    assert response.status_code == 200, response.text
    assert [profile.language for profile in starts] == [language]
    assert "c" * 64 not in response.text  # private proof never echoed to the browser


def test_owner_start_rejects_unsupported_language_and_keeps_en_nl(application) -> None:
    _robot, client, code, starts, health = application
    assert pair(client, code).status_code == 200
    headers = owner_headers(client)
    for unsupported in ("sv", "FR", "fr-FR", "pt-BR", "ita"):
        assert client.post("/api/kids/start", json={"language": unsupported}, headers=headers).status_code == 422
    health.pop("kids_voice_proofs")  # EN/NL never depend on the per-language map
    for language in ("en", "nl"):
        assert client.post("/api/kids/start", json={"language": language}, headers=headers).status_code == 200
    assert [profile.language for profile in starts] == ["en", "nl"]
