from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import homebody.main as main_module
from homebody.config import AppConfig
from homebody.main import Homebody
from homebody.mcp_oauth import (
    ACCESS_TOKEN_SECONDS,
    MAX_CLIENTS,
    MAX_PENDING,
    MAX_PENDING_PER_CLIENT,
    UNAPPROVED_CLIENT_SECONDS,
    OAuthError,
    OAuthServer,
    consent_page,
    pkce_challenge,
)
from homebody.mcp_server import token_digest

PUBLIC = "https://reachy.example.com"
RESOURCE = f"{PUBLIC}/mcp"
REDIRECT = "https://agent.example/callback"
STATIC = "hb_static-token"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def verifier() -> str:
    return secrets.token_urlsafe(48)


def authorize_params(client_id: str, code_verifier: str, **overrides: str) -> dict[str, str]:
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "code_challenge": pkce_challenge(code_verifier),
        "code_challenge_method": "S256",
        "state": "xyz",
        "resource": RESOURCE,
    }
    params.update(overrides)
    return params


def granted(server: OAuthServer) -> tuple[str, str, dict[str, Any]]:
    """Run the whole flow directly against the server; returns client id, verifier and tokens."""
    client_id = server.register({"client_name": "Test agent", "redirect_uris": [REDIRECT]})["client_id"]
    code_verifier = verifier()
    pending = server.start_authorization(authorize_params(client_id, code_verifier), resource=RESOURCE)
    server.owner_decide(pending.pending_id, approve=True)
    target = server.complete(pending.pending_id)
    code = parse_qs(urlparse(target).query)["code"][0]
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_verifier": code_verifier,
        "resource": RESOURCE,
    }
    return client_id, code_verifier, server.exchange(form, resource=RESOURCE)


def test_pkce_matches_the_rfc_7636_example() -> None:
    assert (
        pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_metadata_points_at_this_server_only() -> None:
    resource = OAuthServer.protected_resource_metadata(PUBLIC + "/")
    assert resource["resource"] == RESOURCE and resource["authorization_servers"] == [PUBLIC]

    server = OAuthServer.authorization_server_metadata(PUBLIC)
    assert server["issuer"] == PUBLIC
    assert server["code_challenge_methods_supported"] == ["S256"]
    assert server["token_endpoint_auth_methods_supported"] == ["none"]
    assert server["registration_endpoint"] == f"{PUBLIC}/oauth/register"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"redirect_uris": []},
        {"redirect_uris": ["http://agent.example/callback"]},
        {"redirect_uris": ["javascript:alert(1)"]},
        {"redirect_uris": ["https://agent.example/cb#frag"]},
        {"redirect_uris": [REDIRECT] * 6},
        {"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "client_secret_basic"},
        {"redirect_uris": [REDIRECT], "grant_types": ["client_credentials"]},
    ],
)
def test_registration_accepts_only_public_clients_with_safe_redirects(payload: object) -> None:
    with pytest.raises(OAuthError):
        OAuthServer(None).register(payload)


def test_registration_allows_loopback_and_limits_clients() -> None:
    server = OAuthServer(None)
    loopback = server.register({"redirect_uris": ["http://127.0.0.1:33418/callback"], "client_name": " a\n b "})
    assert loopback["client_name"] == "a b" and loopback["token_endpoint_auth_method"] == "none"
    for _ in range(MAX_CLIENTS - 1):
        server.register({"redirect_uris": [REDIRECT]})
    # Strangers cannot lock the owner out: the oldest never-approved registration makes room.
    newest = server.register({"redirect_uris": [REDIRECT]})["client_id"]
    assert newest in server._clients and loopback["client_id"] not in server._clients
    for client in server._clients.values():
        client["approved_at"] = 1
    with pytest.raises(OAuthError, match="Too many"):
        server.register({"redirect_uris": [REDIRECT]})


def test_open_authorization_requests_are_bounded() -> None:
    server = OAuthServer(None)
    flooding = server.register({"redirect_uris": [REDIRECT]})["client_id"]
    for _ in range(MAX_PENDING_PER_CLIENT * 4):
        server.start_authorization(authorize_params(flooding, verifier()), resource=RESOURCE)
    assert len(server._pending) == MAX_PENDING_PER_CLIENT

    others = [server.register({"redirect_uris": [REDIRECT]})["client_id"] for _ in range(MAX_CLIENTS - 1)]
    for client_id in others * MAX_PENDING_PER_CLIENT:
        server.start_authorization(authorize_params(client_id, verifier()), resource=RESOURCE)
    assert len(server._pending) == MAX_PENDING
    # The newest request always gets in, so a real connection is delayed at worst, never refused.
    latest = server.start_authorization(authorize_params(flooding, verifier()), resource=RESOURCE)
    assert server.pending(latest.pending_id) is not None and len(server._pending) == MAX_PENDING


def test_unapproved_registrations_expire_but_approved_agents_stay() -> None:
    clock = Clock()
    server = OAuthServer(None, clock=clock)
    stale = server.register({"redirect_uris": [REDIRECT]})["client_id"]
    approved, _verifier, _tokens = granted(server)
    pending = server.start_authorization(authorize_params(stale, verifier()), resource=RESOURCE)

    clock.now += UNAPPROVED_CLIENT_SECONDS + 1
    server.register({"redirect_uris": [REDIRECT]})
    assert stale not in server._clients and approved in server._clients
    # Its open request went with it, so a late approval cannot resurrect it.
    assert server.pending(pending.pending_id) is None


def test_approval_fails_when_the_registration_was_evicted() -> None:
    server = OAuthServer(None)
    client_id = server.register({"redirect_uris": [REDIRECT]})["client_id"]
    pending = server.start_authorization(authorize_params(client_id, verifier()), resource=RESOURCE)
    server.owner_decide(pending.pending_id, approve=True)
    server._clients.pop(client_id)
    with pytest.raises(OAuthError, match="registration expired"):
        server.complete(pending.pending_id)


def test_authorize_rejects_unknown_clients_bad_redirects_and_missing_pkce() -> None:
    server = OAuthServer(None)
    client_id = server.register({"redirect_uris": [REDIRECT]})["client_id"]
    code_verifier = verifier()

    with pytest.raises(OAuthError, match="Unknown client"):
        server.start_authorization(authorize_params("hbc_nope", code_verifier), resource=RESOURCE)
    with pytest.raises(OAuthError, match="redirect_uri"):
        server.start_authorization(
            authorize_params(client_id, code_verifier, redirect_uri="https://evil.example/cb"), resource=RESOURCE
        )
    with pytest.raises(OAuthError, match="PKCE"):
        server.start_authorization(
            authorize_params(client_id, code_verifier, code_challenge_method="plain"), resource=RESOURCE
        )
    with pytest.raises(OAuthError, match="PKCE"):
        server.start_authorization(authorize_params(client_id, code_verifier, code_challenge=""), resource=RESOURCE)
    with pytest.raises(OAuthError) as wrong_target:
        server.start_authorization(
            authorize_params(client_id, code_verifier, resource="https://other.example/mcp"), resource=RESOURCE
        )
    assert wrong_target.value.error == "invalid_target"
    with pytest.raises(OAuthError) as wrong_scope:
        server.start_authorization(authorize_params(client_id, code_verifier, scope="admin"), resource=RESOURCE)
    assert wrong_scope.value.error == "invalid_scope"


def test_only_the_owner_approved_request_can_complete() -> None:
    clock = Clock()
    server = OAuthServer(None, clock=clock)
    client_id = server.register({"client_name": "dots", "redirect_uris": [REDIRECT]})["client_id"]

    def start() -> str:
        return server.start_authorization(authorize_params(client_id, verifier()), resource=RESOURCE).pending_id

    mine, theirs = start(), start()
    # Nothing the agent side does can grant access before the owner decides.
    with pytest.raises(OAuthError, match="Not approved yet") as waiting:
        server.complete(mine)
    assert waiting.value.error == "authorization_pending"

    listed = {item["id"]: item for item in server.pending_for_owner()}
    assert listed[mine]["agent"] == "dots" and listed[mine]["returns_to"] == "agent.example"
    assert len(listed[mine]["match_code"]) == 4 and listed[mine]["approved"] is False

    server.owner_decide(mine, approve=True)
    # Approving one request never lets a different (possibly phishing) request through.
    with pytest.raises(OAuthError, match="Not approved yet"):
        server.complete(theirs)
    target = server.complete(mine)
    assert target.startswith(REDIRECT + "?") and "code=" in target and "state=xyz" in target
    with pytest.raises(OAuthError, match="expired"):
        server.complete(mine)  # used up

    server.owner_decide(theirs, approve=False)
    denied = server.complete(theirs)
    assert "error=access_denied" in denied and "code=" not in denied

    late = start()
    clock.now += 601
    with pytest.raises(OAuthError, match="expired"):
        server.owner_decide(late, approve=True)


def test_there_is_no_shared_code_for_strangers_to_guess_or_burn() -> None:
    server = OAuthServer(None)
    client_id = server.register({"redirect_uris": [REDIRECT]})["client_id"]
    pending = server.start_authorization(authorize_params(client_id, verifier()), resource=RESOURCE)
    for _ in range(50):
        with pytest.raises(OAuthError, match="Not approved yet"):
            server.complete(pending.pending_id)
    server.owner_decide(pending.pending_id, approve=True)
    assert "code=" in server.complete(pending.pending_id)


def test_damaged_store_records_are_dropped_not_crashed_on(tmp_path: Path) -> None:
    path = tmp_path / "mcp-oauth.json"
    server = OAuthServer(path)
    client_id, _verifier, tokens = granted(server)

    payload = json.loads(path.read_text())
    payload["clients"]["hbc_broken"] = {"name": 5}
    payload["grants"]["orphan"] = {"client_id": "hbc_gone", "resource": RESOURCE, "refresh": "x", "used": []}
    payload["grants"]["bad"] = ["not", "a", "dict"]
    payload["access"]["deadbeef"] = {"grant_id": "orphan", "resource": RESOURCE, "expires_at": 9e12}
    payload["access"]["cafe"] = {"grant_id": next(iter(payload["grants"])), "resource": RESOURCE}
    path.write_text(json.dumps(payload))

    reloaded = OAuthServer(path)
    assert set(reloaded._clients) == {client_id}
    assert set(reloaded._grants) == set(server._grants)
    assert reloaded.validate_access_token(tokens["access_token"], resource=RESOURCE)
    assert reloaded.public_status()["agents"] == [{"name": "Test agent", "connected": True}]


def test_deny_redirects_with_access_denied() -> None:
    server = OAuthServer(None)
    client_id = server.register({"redirect_uris": [REDIRECT]})["client_id"]
    pending = server.start_authorization(authorize_params(client_id, verifier()), resource=RESOURCE)

    target = server.deny(pending.pending_id)
    assert target is not None and "error=access_denied" in target and "state=xyz" in target
    assert server.deny(pending.pending_id) is None


def test_code_exchange_checks_pkce_binding_and_is_single_use() -> None:
    server = OAuthServer(None)
    client_id = server.register({"redirect_uris": [REDIRECT]})["client_id"]

    def fresh_code(code_verifier: str) -> str:
        pending = server.start_authorization(authorize_params(client_id, code_verifier), resource=RESOURCE)
        server.owner_decide(pending.pending_id, approve=True)
        return parse_qs(urlparse(server.complete(pending.pending_id)).query)["code"][0]

    code_verifier = verifier()
    code = fresh_code(code_verifier)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_verifier": verifier(),
    }
    with pytest.raises(OAuthError, match="PKCE verification"):
        server.exchange(form, resource=RESOURCE)
    with pytest.raises(OAuthError, match="invalid or expired"):
        server.exchange({**form, "code_verifier": code_verifier}, resource=RESOURCE)  # burned by the failure

    code = fresh_code(code_verifier)
    with pytest.raises(OAuthError, match="does not match"):
        server.exchange(
            {**form, "code": code, "code_verifier": code_verifier, "client_id": "hbc_other"}, resource=RESOURCE
        )

    code = fresh_code(code_verifier)
    with pytest.raises(OAuthError) as moved:
        server.exchange({**form, "code": code, "code_verifier": code_verifier}, resource="https://new.example/mcp")
    assert moved.value.error == "invalid_target"

    code = fresh_code(code_verifier)
    tokens = server.exchange({**form, "code": code, "code_verifier": code_verifier}, resource=RESOURCE)
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == ACCESS_TOKEN_SECONDS
    assert server.validate_access_token(tokens["access_token"], resource=RESOURCE)
    assert not server.validate_access_token(tokens["access_token"], resource="https://other.example/mcp")
    with pytest.raises(OAuthError, match="invalid or expired"):
        server.exchange({**form, "code": code, "code_verifier": code_verifier}, resource=RESOURCE)


def test_access_tokens_expire_and_refresh_rotates() -> None:
    clock = Clock()
    server = OAuthServer(None, clock=clock)
    client_id, _verifier, tokens = granted(server)

    clock.now += ACCESS_TOKEN_SECONDS + 1
    assert not server.validate_access_token(tokens["access_token"], resource=RESOURCE)

    refresh = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id}
    with pytest.raises(OAuthError, match="client_id"):
        server.exchange({**refresh, "client_id": "hbc_other"}, resource=RESOURCE)
    rotated = server.exchange(refresh, resource=RESOURCE)
    assert rotated["refresh_token"] != tokens["refresh_token"]
    assert server.validate_access_token(rotated["access_token"], resource=RESOURCE)

    # Replaying the old refresh token looks like theft: the whole grant ends.
    with pytest.raises(OAuthError, match="reuse"):
        server.exchange(refresh, resource=RESOURCE)
    assert not server.validate_access_token(rotated["access_token"], resource=RESOURCE)
    with pytest.raises(OAuthError):
        server.exchange({**refresh, "refresh_token": rotated["refresh_token"]}, resource=RESOURCE)


def test_refresh_tokens_expire() -> None:
    clock = Clock()
    server = OAuthServer(None, clock=clock)
    client_id, _verifier, tokens = granted(server)
    clock.now += 31 * 86400
    refresh = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id}
    with pytest.raises(OAuthError, match="expired"):
        server.exchange(refresh, resource=RESOURCE)


def test_revocation_and_disconnect() -> None:
    server = OAuthServer(None)
    _client_id, _verifier, tokens = granted(server)
    assert server.public_status()["agents"] == [{"name": "Test agent", "connected": True}]

    server.revoke_token("unknown")  # not an error
    server.revoke_token(tokens["refresh_token"])
    assert not server.validate_access_token(tokens["access_token"], resource=RESOURCE)
    assert server.public_status()["agents"] == [{"name": "Test agent", "connected": False}]

    _client_id, _verifier, tokens = granted(server)
    server.disconnect_all()
    assert not server.validate_access_token(tokens["access_token"], resource=RESOURCE)
    assert server.public_status() == {"agents": [], "pending": []}


def test_state_persists_as_hashes_with_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "mcp-oauth.json"
    server = OAuthServer(path)
    _client_id, _verifier, tokens = granted(server)

    stored = path.read_text(encoding="utf-8")
    assert tokens["access_token"] not in stored and tokens["refresh_token"] not in stored
    assert path.stat().st_mode & 0o777 == 0o600
    assert OAuthServer(path).validate_access_token(tokens["access_token"], resource=RESOURCE)

    path.write_text("not json", encoding="utf-8")
    assert not OAuthServer(path).validate_access_token(tokens["access_token"], resource=RESOURCE)


def test_consent_page_escapes_agent_supplied_text() -> None:
    server = OAuthServer(None)
    client_id = server.register({"client_name": "<script>alert(1)</script>", "redirect_uris": [REDIRECT]})["client_id"]
    pending = server.start_authorization(authorize_params(client_id, verifier()), resource=RESOURCE)

    html = consent_page(pending, error="<b>bad</b>", vision=True)
    assert "<script>" not in html and "&lt;script&gt;" in html and "&lt;b&gt;" in html
    assert "agent.example" in html and "receives text, never images" in html
    assert "never images" not in consent_page(pending)


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://reachy.example.com", True),
        ("https://reachy.example.com/", True),
        ("http://localhost:8042", True),
        ("http://reachy.example.com", False),
        ("https://reachy.example.com/mcp", False),
        ("https://reachy.example.com?x=1", False),
        ("reachy.example.com", False),
    ],
)
def test_public_url_validation(url: str, ok: bool) -> None:
    if ok:
        assert AppConfig(mcp_public_url=url).mcp_public_url == url.rstrip("/")
    else:
        with pytest.raises(ValueError, match="mcp_public_url"):
            AppConfig(mcp_public_url=url)


def test_oauth_needs_a_public_url() -> None:
    with pytest.raises(ValueError, match="public HTTPS address"):
        AppConfig(mcp_oauth_enabled=True)


# -- HTTP ---------------------------------------------------------------------------------------


class FakeRuntime:
    control_ready = True
    kids_controls_locked = False

    def status(self) -> dict[str, Any]:
        return {"power_mode": "awake", "kids_mode": {"active": False}, "presence": {"enabled": False}}


def oauth_config(**overrides: Any) -> AppConfig:
    values: dict[str, Any] = {
        "mcp_enabled": True,
        "mcp_token_sha256": token_digest(STATIC),
        "mcp_oauth_enabled": True,
        "mcp_public_url": PUBLIC,
    }
    values.update(overrides)
    return AppConfig(**values)


class FakeListener:
    def __init__(self) -> None:
        self.synced: list[AppConfig] = []

    def sync(self, config: AppConfig) -> None:
        self.synced.append(config)

    def status(self) -> dict[str, object]:
        return {"running": False, "address": "", "error": ""}

    def close(self) -> None:
        pass


def build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: AppConfig
) -> tuple[Homebody, TestClient, TestClient]:
    """The app, a client for the home dashboard port, and one for the public hosted-agent listener."""
    stored = [config]
    monkeypatch.setattr(main_module, "load_config", lambda: stored[-1])
    monkeypatch.setattr(main_module, "save_config", lambda value: stored.append(value))
    app = Homebody(False)
    app._oauth = OAuthServer(tmp_path / "mcp-oauth.json")
    app._agent_listener = FakeListener()  # type: ignore[assignment]
    app._runtime = FakeRuntime()  # type: ignore[assignment]
    return app, TestClient(app.settings_app), TestClient(app._build_public_app())


def test_oauth_routes_are_hidden_until_turned_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _app, client, public = build(
        monkeypatch, tmp_path, AppConfig(mcp_enabled=True, mcp_token_sha256=token_digest(STATIC))
    )
    assert public.get("/.well-known/oauth-protected-resource/mcp").status_code == 404
    assert public.get("/.well-known/oauth-authorization-server").status_code == 404
    assert public.post("/oauth/register", json={"redirect_uris": [REDIRECT]}).status_code == 404
    assert public.post("/mcp", json=INIT).status_code == 404
    challenge = client.post("/mcp", json=INIT).headers["www-authenticate"]
    assert "resource_metadata" not in challenge
    assert client.post("/api/mcp/oauth/approve", json={"pending_id": "x"}).status_code == 409


def test_full_hosted_agent_flow_over_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _app, client, agent = build(monkeypatch, tmp_path, oauth_config())

    unauthorized = agent.post("/mcp", json=INIT)
    assert unauthorized.status_code == 401
    assert (
        f'resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/mcp"'
        in (unauthorized.headers["www-authenticate"])
    )
    metadata = agent.get("/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["resource"] == RESOURCE
    server_metadata = agent.get("/.well-known/oauth-authorization-server").json()
    assert server_metadata["issuer"] == PUBLIC

    registered = agent.post("/oauth/register", json={"client_name": "dots", "redirect_uris": [REDIRECT]})
    assert registered.status_code == 201
    client_id = registered.json()["client_id"]
    assert agent.post("/oauth/register", content=b"{nope").status_code == 400

    code_verifier = verifier()
    page = agent.get("/oauth/authorize", params=authorize_params(client_id, code_verifier))
    assert page.status_code == 200 and "dots" in page.text
    assert (
        page.headers["x-frame-options"] == "DENY"
        and "frame-ancestors 'none'" in page.headers["content-security-policy"]
    )
    pending_id = page.text.split('name="pending_id" value="')[1].split('"')[0]

    bad = agent.get(
        "/oauth/authorize", params=authorize_params(client_id, code_verifier, redirect_uri="https://evil.example/cb")
    )
    assert bad.status_code == 400 and "location" not in bad.headers

    waiting = agent.post(
        "/oauth/authorize", data={"pending_id": pending_id, "action": "continue"}, follow_redirects=False
    )
    assert waiting.status_code == 200 and "Not approved yet" in waiting.text and "location" not in waiting.headers
    pending = client.get("/api/mcp/status").json()["oauth"]["pending"]
    assert [item["id"] for item in pending] == [pending_id]
    assert pending[0]["match_code"] in page.text and pending[0]["returns_to"] in page.text
    assert client.post("/api/mcp/oauth/approve", json={"pending_id": pending_id}).status_code == 200
    approved = agent.post(
        "/oauth/authorize", data={"pending_id": pending_id, "action": "continue"}, follow_redirects=False
    )
    assert approved.status_code == 303 and approved.headers["location"].startswith(REDIRECT + "?")
    code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]

    token = agent.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_verifier": code_verifier,
            "resource": RESOURCE,
        },
    )
    assert token.status_code == 200 and token.headers["cache-control"] == "no-store"
    access = token.json()["access_token"]

    ok = agent.post("/mcp", json=INIT, headers={"Authorization": f"Bearer {access}"})
    assert ok.status_code == 200 and ok.json()["result"]["serverInfo"]["name"] == "homebody"

    agents = client.get("/api/mcp/status").json()["oauth"]["agents"]
    assert agents == [{"name": "dots", "connected": True}]

    reused = agent.post("/oauth/token", data={"grant_type": "authorization_code", "code": code})
    assert reused.status_code == 400 and reused.json()["error"] == "invalid_request"

    assert agent.post("/oauth/revoke", data={"token": access}).status_code == 200
    assert agent.post("/mcp", json=INIT, headers={"Authorization": f"Bearer {access}"}).status_code == 401


def test_deny_over_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    app, _client, agent = build(monkeypatch, tmp_path, oauth_config())
    client_id = app._oauth.register({"redirect_uris": [REDIRECT]})["client_id"]
    pending = app._oauth.start_authorization(authorize_params(client_id, verifier()), resource=RESOURCE)

    denied = agent.post(
        "/oauth/authorize", data={"pending_id": pending.pending_id, "action": "deny"}, follow_redirects=False
    )
    assert denied.status_code == 303 and "error=access_denied" in denied.headers["location"]
    again = agent.post(
        "/oauth/authorize", data={"pending_id": pending.pending_id, "action": "deny"}, follow_redirects=False
    )
    assert again.status_code == 400


def test_dashboard_port_never_serves_hosted_agents(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _app, client, agent = build(monkeypatch, tmp_path, oauth_config())

    # The OAuth endpoints do not exist on the dashboard port at all.
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource/mcp"):
        assert client.get(path).status_code == 404
    assert client.post("/oauth/register", json={"redirect_uris": [REDIRECT]}).status_code == 404
    # A tunnel pointed at the dashboard port by mistake gets nothing when it keeps the public Host.
    for path in ("/", "/api/status", "/api/mcp/status", "/mcp"):
        assert client.get(path, headers={"Host": "reachy.example.com"}).status_code == 404
        assert client.get(path, headers={"X-Forwarded-Host": "REACHY.example.com"}).status_code == 404
    assert client.get("/api/status").status_code == 200  # the home network still sees everything
    static = {"Authorization": f"Bearer {STATIC}"}
    assert client.post("/mcp", json=INIT, headers=static).status_code == 200


def test_public_listener_serves_only_agent_routes_whatever_the_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _app, _client, agent = build(monkeypatch, tmp_path, oauth_config())
    static = {"Authorization": f"Bearer {STATIC}"}
    # A proxy that rewrites Host (nginx default, ngrok --host-header=rewrite) changes nothing here.
    for headers in ({}, {"Host": "reachy.example.com"}, {"Host": "127.0.0.1:8043"}):
        for path in ("/", "/api/status", "/api/config", "/api/mcp/status", "/static/main.js"):
            assert agent.get(path, headers=headers).status_code == 404
        assert agent.post("/api/mcp/oauth/approve", json={}, headers=headers).status_code == 404
        assert agent.post("/api/settings", json={}, headers=headers).status_code == 404
        # The never-expiring static token is for the home network only.
        assert agent.post("/mcp", json=INIT, headers={**static, **headers}).status_code == 401
        assert agent.get("/.well-known/oauth-authorization-server", headers=headers).status_code == 200


def test_saving_settings_moves_the_public_listener(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    app, client, _agent = build(monkeypatch, tmp_path, oauth_config())
    saved = client.post("/api/settings", json={"mcp_public_port": 8443})
    assert saved.status_code == 200, saved.text
    assert app._agent_listener.synced[-1].mcp_public_port == 8443  # type: ignore[attr-defined]


def test_owner_routes_need_the_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    app, client, _agent = build(monkeypatch, tmp_path, oauth_config(api_key="owner-key"))
    granted(app._oauth)
    client_id = app._oauth.register({"redirect_uris": [REDIRECT]})["client_id"]
    pending = app._oauth.start_authorization(authorize_params(client_id, verifier()), resource=RESOURCE)

    decision = {"pending_id": pending.pending_id}
    assert client.post("/api/mcp/oauth/approve", json=decision).status_code == 403
    assert client.post("/api/mcp/oauth/deny", json={**decision, "current_api_key": "wrong"}).status_code == 403
    assert client.post("/api/mcp/oauth/disconnect", json={"current_api_key": "wrong"}).status_code == 403
    approved = client.post("/api/mcp/oauth/approve", json={**decision, "current_api_key": "owner-key"})
    assert approved.status_code == 200 and approved.json()["pending"][0]["approved"] is True
    gone = client.post("/api/mcp/oauth/approve", json={"pending_id": "nope", "current_api_key": "owner-key"})
    assert gone.status_code == 409

    disconnected = client.post("/api/mcp/oauth/disconnect", json={"current_api_key": "owner-key"})
    assert disconnected.status_code == 200 and disconnected.json()["agents"] == []


def test_turning_on_sign_in_needs_the_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _app, client, _agent = build(monkeypatch, tmp_path, AppConfig(api_key="owner-key", mcp_enabled=True))
    update = {"mcp_oauth_enabled": True, "mcp_public_url": PUBLIC}

    assert client.post("/api/settings", json=update).status_code == 403
    assert not main_module.load_config().mcp_oauth_enabled
    saved = client.post("/api/settings", json={**update, "current_api_key": "owner-key"})
    assert saved.status_code == 200, saved.text
    assert main_module.load_config().mcp_public_url == PUBLIC
