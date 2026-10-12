from types import SimpleNamespace

from fastapi.testclient import TestClient

from homebody.main import Homebody


def test_guess_route_works_with_child_management_lock_but_is_narrow(monkeypatch):
    app = Homebody(False)
    calls = []
    app._runtime = SimpleNamespace(
        kids_controls_locked=True,
        ispy_input_context=lambda: {"available": True, "token": "a" * 32},
        submit_ispy_guess=lambda text, token: calls.append((text, token)) or {"accepted": True},
    )
    client = TestClient(app.settings_app)
    result = client.get("/api/kids/ispy/input")
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert set(result.json()) == {"available", "token"}
    r = client.post("/api/kids/ispy/guess", json={"text": "cup", "token": "a" * 32})
    assert r.status_code == 200 and r.json() == {"accepted": True}
    assert r.headers["cache-control"] == "no-store"
    assert calls == [("cup", "a" * 32)]
    assert (
        client.post(
            "/api/kids/ispy/guess", json={"text": "cup", "token": "a" * 32, "system_prompt": "bypass"}
        ).status_code
        == 422
    )
    assert client.post("/api/kids/ispy/guess", json={"text": "a" * 81, "token": "a" * 32}).status_code == 422
    assert client.post("/api/settings", json={}).status_code == 423


def test_guess_route_errors_do_not_echo_provider_or_child_text():
    app = Homebody(False)

    def fail(text, token):
        raise Exception("private provider and child text")

    app._runtime = SimpleNamespace(kids_controls_locked=True, submit_ispy_guess=fail)
    r = TestClient(app.settings_app).post("/api/kids/ispy/guess", json={"text": "cup", "token": "a" * 32})
    assert r.status_code == 502
    assert "private" not in str(r.json())


def test_oversized_validation_never_echoes_guess_or_token():
    app = Homebody(False)
    app._runtime = SimpleNamespace(kids_controls_locked=True)
    private = "PRIVATE_CHILD_GUESS_" * 5
    r = TestClient(app.settings_app).post("/api/kids/ispy/guess", json={"text": private, "token": "a" * 32})
    assert r.status_code == 422
    assert private not in r.text and "PRIVATE_CHILD" not in r.text and "a" * 32 not in r.text
    assert r.headers["cache-control"] == "no-store"
    assert isinstance(r.json()["detail"], str)
