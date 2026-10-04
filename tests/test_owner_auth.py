"""Owner boundary tests: no daemon, hardware, or external network."""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homebody.owner_auth import COOKIE, OwnerStore, install_owner_auth

ORIGIN = "https://robot.example.ts.net"


@pytest.fixture
def owner(tmp_path):
    store = OwnerStore(tmp_path / "owner.sqlite3")
    code = store.provision(ORIGIN)
    app = FastAPI()
    install_owner_auth(app, store)

    @app.post("/api/power")
    def power():
        return {"ok": True}

    @app.get("/api/status")
    def status():
        return {"private": True}

    return store, code, TestClient(app, base_url=ORIGIN)


def pair(client, code):
    return client.post("/api/owner/pair", json={"code": code, "name": "Phone"}, headers={"Origin": ORIGIN})


def test_pair_once_remember_revoke(owner):
    store, code, client = owner
    response = pair(client, code)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert pair(client, code).status_code == 401
    session = client.get("/api/owner/session").json()
    assert session["owner"] is True
    assert client.get("/api/status").status_code == 200
    headers = {"Origin": ORIGIN, "X-Homebody-CSRF": session["csrf"]}
    assert client.post("/api/power", headers=headers).status_code == 200
    assert (
        client.post("/api/owner/revoke", json={"device_id": session["device_id"]}, headers=headers).status_code == 200
    )
    assert client.get("/api/status").status_code == 401
    assert store.session(client.cookies.get(COOKIE)) is None


def test_expiry_and_no_plaintext_credentials(owner):
    store, code, client = owner
    with store.connect() as db:
        db.execute("UPDATE pairing SET expires = ?", (time.time() - 1,))
    assert pair(client, code).status_code == 401
    code = store.provision(ORIGIN)
    assert pair(client, code).status_code == 200
    token = client.cookies.get(COOKIE)
    assert token.encode() not in store.path.read_bytes()
    assert code.encode() not in store.path.read_bytes()
    with store.connect() as db:
        db.execute("UPDATE devices SET expires = ?", (time.time() - 1,))
    assert client.get("/api/status").status_code == 401


def test_csrf_origin_host_and_guest(owner):
    _, code, client = owner
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/power", headers={"X-Reachy-Adult-UI": "unlocked"}).status_code == 401
    assert pair(client, code).status_code == 200
    csrf = client.get("/api/owner/session").json()["csrf"]
    for headers in ({}, {"Origin": ORIGIN}, {"Origin": "https://evil.test", "X-Homebody-CSRF": csrf}):
        assert client.post("/api/power", headers=headers).status_code == 403
    assert client.get("/api/status", headers={"Host": "evil.test"}).status_code == 403
    assert client.get("/api/status", headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.get("/api/status", headers={"X-Forwarded-Host": "evil.test"}).status_code == 200
    assert client.get("/api/status", follow_redirects=False, headers={"Cookie": f"{COOKIE}=bad"}).status_code == 401


def test_pair_origin_and_throttle(owner):
    _, code, client = owner
    assert client.post("/api/owner/pair", json={"code": code}).status_code == 403
    assert (
        client.post("/api/owner/pair", json={"code": code}, headers={"Origin": "https://evil.test"}).status_code == 403
    )
    for _ in range(10):
        assert pair(client, "wrong").status_code == 401
    assert pair(client, code).status_code == 429


def test_unprovisioned_fails_closed(tmp_path):
    app = FastAPI()
    install_owner_auth(app, OwnerStore(tmp_path / "owner.sqlite3"))
    client = TestClient(app)
    assert client.get("/api/status").status_code == 503
    assert client.post("/api/owner/pair", json={"code": "guess"}).status_code == 503


def test_database_permissions_and_restart(owner):
    store, code, client = owner
    pair(client, code)
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert OwnerStore(store.path).session(client.cookies.get(COOKIE))


def test_simultaneous_redemption_only_one(owner):
    from concurrent.futures import ThreadPoolExecutor

    store, code, _ = owner

    def redeem(_):
        try:
            return store.redeem(code, "Phone")
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(result is not None for result in pool.map(redeem, range(4))) == 1
