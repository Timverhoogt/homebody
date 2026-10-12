"""Owner-only update status must not offer the already installed version after restart."""
from types import SimpleNamespace

import pytest
from test_owner_auth import ORIGIN, pair
from test_owner_updates import app as _app_fixture
from test_updates import manifest

from homebody import update_api
from homebody.updates import atomic_json

app = _app_fixture


@pytest.mark.parametrize("offer", ["0.5.0", "0.5.1", "0.5.2"])
def test_owner_status_filters_stale_offer_without_rewriting_catalog(app, monkeypatch, offer):
    robot, client, code = app
    monkeypatch.setattr(update_api, "installed_identity", lambda: {"version": "0.5.1"})
    pair(client, code)
    service = robot.settings_app.state.updates
    path = service.root / "catalog.json"
    data = {"available": {**manifest(), "version": offer}, "notes": [], "checked_at": 1,
            "error": "previous check error"}
    atomic_json(path, data)
    before = path.read_bytes()
    for _ in range(2):
        body = client.get("/api/updates").json()
        assert bool(body["available"]) is (offer == "0.5.2")
        assert body["current"]["version"] == "0.5.1"
        assert body["error"] == "previous check error"
    assert path.read_bytes() == before  # status remains side-effect-free


def test_owner_install_rejects_already_installed_cached_offer_before_discovery(app, monkeypatch):
    robot, client, code = app
    pair(client, code)
    service = robot.settings_app.state.updates
    monkeypatch.setattr(update_api, "installed_identity", lambda: {"version": "0.5.1"})
    monkeypatch.setattr(service, "supported", lambda: (True, ""))
    monkeypatch.setattr(update_api, "discover", lambda _: pytest.fail("stale preview must be rejected first"))
    robot._runtime = SimpleNamespace(kids_controls_locked=False)
    atomic_json(service.root / "catalog.json", {"available": {**manifest(), "version": "0.5.1"},
                                               "notes": [], "checked_at": 1})
    headers = {"Origin": ORIGIN, "X-Homebody-CSRF": client.get("/api/owner/session").json()["csrf"]}
    response = client.post("/api/updates/install", json={"confirm": "INSTALL", "release_id": 42}, headers=headers)
    assert response.status_code == 409
    assert "Preview this exact release" in response.text
