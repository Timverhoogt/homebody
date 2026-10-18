"""Real Owner/CSRF boundary for first-run progress. No media or provider calls."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import homebody.main as main_module
from homebody.config import load_config, merge_config, save_config
from homebody.main import Homebody
from homebody.owner_auth import OwnerStore, default_owner_path

ORIGIN = "https://robot.example.ts.net"


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEBODY_CONFIG", str(tmp_path / "config.json"))
    store = OwnerStore(default_owner_path())
    code = store.provision(ORIGIN)
    app = Homebody(False)
    client = TestClient(app.settings_app, base_url=ORIGIN)
    response = client.post("/api/owner/pair", json={"code": code}, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    session = client.get("/api/owner/session").json()
    headers = {"Origin": ORIGIN, "X-Homebody-CSRF": session["csrf"]}
    return client, headers, app


def test_owner_setup_is_private_and_csrf_protected(setup):
    client, headers, app = setup
    guest = TestClient(app.settings_app, base_url=ORIGIN)
    assert guest.get("/api/onboarding").status_code == 401
    assert guest.post("/api/onboarding/progress", json={"step": "connection"}).status_code == 401
    assert client.post("/api/onboarding/progress", json={"step": "connection"}).status_code == 403
    result = client.get("/api/onboarding")
    assert result.status_code == 200
    assert result.json()["required"] is True
    assert result.json()["connection_checked"] is False
    assert result.json()["speech_tested"] is False
    assert client.post("/api/onboarding/progress", json={"step": "connection"}, headers=headers).status_code == 200


def test_owner_progress_resumes_without_install_or_media_side_effects(setup):
    client, headers, app = setup
    result = client.post(
        "/api/onboarding/progress", json={"step": "connection", "entry_route": "marketplace"}, headers=headers
    )
    assert result.status_code == 200
    assert load_config().setup_step == "connection"
    assert load_config().setup_pending is True
    assert app._runtime is None
    result = client.get("/api/onboarding").json()
    assert result["entry_route"] == "marketplace"
    assert result["step"] == "connection"
    assert "setup_connection_sha256" not in result
    for payload in [
        {"step": "complete"},
        {"step": "connection", "api_key": "not allowed"},
        {"step": "connection", "entry_route": "admin"},
    ]:
        assert client.post("/api/onboarding/progress", json=payload, headers=headers).status_code == 422


def test_owner_empty_backend_never_contacts_a_service(setup, monkeypatch):
    client, headers, _ = setup
    forbidden = Mock(side_effect=AssertionError("Unconfigured check must not contact backend"))
    monkeypatch.setattr(main_module, "HermesBridgeClient", forbidden)
    result = client.post("/api/onboarding/check", headers=headers)
    assert result.status_code == 409
    forbidden.assert_not_called()
    assert client.post("/api/onboarding/finish", json={"confirm": "restart"}, headers=headers).status_code == 409
    assert load_config().setup_pending is True


def configured():
    save_config(merge_config(load_config(), {"api_key": "private-test-key", "bridge_url": "http://127.0.0.1:9999"}))


@pytest.mark.parametrize("mode", ["realtime", "live"])
def test_fresh_household_voice_onboarding_needs_no_agent_bridge(setup, monkeypatch, mode):
    client, headers, app = setup
    body = {"url": "https://house.invalid", "robot_credential": "synthetic-house-key",
            "enabled": False, "acknowledged": True}
    assert client.post("/api/household/tools/configure", json=body, headers=headers).status_code == 200
    save_config(merge_config(load_config(), {"conversation_mode": mode}))
    assert not load_config().configured and load_config().voice_connection_configured
    bridge = Mock()
    monkeypatch.setattr(main_module, "HermesBridgeClient", Mock(return_value=bridge))
    result = client.post("/api/onboarding/check", headers=headers)
    assert result.status_code == 200 and result.json()["connection_checked"]
    bridge.everyday_capabilities.assert_called_once()
    bridge.health.assert_not_called()
    bridge.models.assert_not_called()
    assert "synthetic-house-key" not in result.text
    done = client.post("/api/onboarding/finish", json={"confirm": "restart"}, headers=headers)
    assert done.status_code == 200 and not load_config().setup_pending
    assert app._runtime is None and done.json()["speech_tested"] is False
    save_config(merge_config(load_config(), {"household_api_key": "rotated-synthetic"}))
    assert not client.get("/api/onboarding").json()["connection_checked"]


def test_owner_checked_connection_is_not_acoustic_acceptance(setup, monkeypatch):
    client, headers, app = setup
    configured()
    bridge = Mock()
    bridge.health.return_value = {"status": "ok", "private": "do not echo"}
    bridge.models.return_value = [{"id": "test"}]
    monkeypatch.setattr(main_module, "HermesBridgeClient", Mock(return_value=bridge))
    checked = client.post("/api/onboarding/check", headers=headers)
    assert checked.status_code == 200
    assert checked.json()["connection_checked"] is True
    assert "private-test-key" not in checked.text
    assert "do not echo" not in checked.text
    bridge.models.assert_called_once()
    bridge.close.assert_called_once()
    assert client.post("/api/onboarding/finish", json={"confirm": "yes"}, headers=headers).status_code == 422
    done = client.post("/api/onboarding/finish", json={"confirm": "restart"}, headers=headers)
    assert done.status_code == 200
    assert load_config().setup_pending is False
    assert app._runtime is None
    assert done.json()["speech_tested"] is False


def test_owner_changed_backend_invalidates_connection_proof(setup, monkeypatch):
    client, headers, _ = setup
    configured()
    bridge = Mock()
    bridge.models.return_value = []
    monkeypatch.setattr(main_module, "HermesBridgeClient", Mock(return_value=bridge))
    assert client.post("/api/onboarding/check", headers=headers).status_code == 200
    save_config(merge_config(load_config(), {"language": "nl"}))
    assert client.get("/api/onboarding").json()["connection_checked"] is False
    assert client.post("/api/onboarding/finish", json={"confirm": "restart"}, headers=headers).status_code == 409


def test_owner_failed_check_clears_previous_proof_and_redacts_error(setup, monkeypatch):
    client, headers, _ = setup
    configured()
    bridge = Mock()
    bridge.models.return_value = []
    monkeypatch.setattr(main_module, "HermesBridgeClient", Mock(return_value=bridge))
    assert client.post("/api/onboarding/check", headers=headers).status_code == 200
    bridge.health.side_effect = RuntimeError("private-test-key http://private-host/path")
    failed = client.post("/api/onboarding/check", headers=headers)
    assert failed.status_code == 502
    assert "private-test-key" not in failed.text
    assert "private-host" not in failed.text
    assert client.get("/api/onboarding").json()["connection_checked"] is False


def test_owner_config_change_during_check_cannot_verify_new_settings(setup, monkeypatch):
    client, headers, _ = setup
    configured()
    bridge = Mock()
    bridge.models.side_effect = lambda: save_config(merge_config(load_config(), {"model": "changed"}))
    monkeypatch.setattr(main_module, "HermesBridgeClient", Mock(return_value=bridge))
    assert client.post("/api/onboarding/check", headers=headers).status_code == 409
    assert client.get("/api/onboarding").json()["connection_checked"] is False


def test_owner_newer_failed_check_cannot_be_overwritten_by_old_success(setup, monkeypatch):
    import threading

    client, headers, _ = setup
    configured()
    started, release = threading.Event(), threading.Event()
    slow, failed = Mock(), Mock()

    def held_models():
        started.set()
        assert release.wait(3)
        return []

    slow.models.side_effect = held_models
    failed.health.side_effect = RuntimeError("backend failed")
    factory = Mock(side_effect=[slow, failed])
    monkeypatch.setattr(main_module, "HermesBridgeClient", factory)
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(client.post("/api/onboarding/check", headers=headers)))
    thread.start()
    try:
        assert started.wait(3)
        assert client.post("/api/onboarding/check", headers=headers).status_code == 502
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert outcomes[0].status_code == 409
    assert client.get("/api/onboarding").json()["connection_checked"] is False
