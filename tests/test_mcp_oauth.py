from __future__ import annotations

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
    APPROVAL_CODE_ATTEMPTS,
    MAX_CLIENTS,
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
    target = server.approve(pending.pending_id, server.new_approval_code())
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
    with pytest.raises(OAuthError, match="Too many"):
        server.register({"redirect_uris": [REDIRECT]})


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


def test_approval_code_is_single_use_attempt_limited_and_expires() -> None:
    clock = Clock()
    server = OAuthServer(None, clock=clock)
    client_id = server.register({"redirect_uris": [REDIRECT]})["client_id"]

    def start() -> str:
        return server.start_authorization(authorize_params(client_id, verifier()), resource=RESOURCE).pending_id

    pending_id = start()
    with pytest.raises(OAuthError, match="No valid approval code"):
        server.approve(pending_id, "ABCD-EFGH")

    code = server.new_approval_code()
    assert len(code) == 9 and code[4] == "-"
    target = server.approve(pending_id, code.lower().replace("-", " "))  # forgiving about case and separators
    assert target.startswith(REDIRECT + "?") and "state=xyz" in target
    with pytest.raises(OAuthError, match="expired"):
        server.approve(pending_id, code)  # the pending request is used up
    with pytest.raises(OAuthError, match="No valid approval code"):
        server.approve(start(), code)  # and so is the code

    code = server.new_approval_code()
    pending_id = start()
    for _ in range(APPROVAL_CODE_ATTEMPTS):
        with pytest.raises(OAuthError, match="not right"):
            server.approve(pending_id, "WRONG-CODE")
    with pytest.raises(OAuthError, match="No valid approval code"):
        server.approve(pending_id, code)  # guessing burned the code

    code = server.new_approval_code()
    clock.now += 601
    with pytest.raises(OAuthError):
        server.approve(start(), code)


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
        return parse_qs(urlparse(server.approve(pending.pending_id, server.new_approval_code())).query)["code"][0]

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
    assert server.public_status() == {"agents": [], "approval_code_active": False}


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


def build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: AppConfig) -> tuple[Homebody, TestClient]:
    stored = [config]
    monkeypatch.setattr(main_module, "load_config", lambda: stored[-1])
    monkeypatch.setattr(main_module, "save_config", lambda value: stored.append(value))
    app = Homebody(False)
    app._oauth = OAuthServer(tmp_path / "mcp-oauth.json")
    app._runtime = FakeRuntime()  # type: ignore[assignment]
    return app, TestClient(app.settings_app)


def test_oauth_routes_are_hidden_until_turned_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _app, client = build(monkeypatch, tmp_path, AppConfig(mcp_enabled=True, mcp_token_sha256=token_digest(STATIC)))
    assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 404
    assert client.get("/.well-known/oauth-authorization-server").status_code == 404
    assert client.post("/oauth/register", json={"redirect_uris": [REDIRECT]}).status_code == 404
    challenge = client.post("/mcp", json=INIT).headers["www-authenticate"]
    assert "resource_metadata" not in challenge
    assert client.post("/api/mcp/approval-code", json={}).status_code == 409


def test_full_hosted_agent_flow_over_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _app, client = build(monkeypatch, tmp_path, oauth_config())
    public = {"Host": "reachy.example.com"}

    unauthorized = client.post("/mcp", json=INIT, headers=public)
    assert unauthorized.status_code == 401
    assert (
        f'resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/mcp"'
        in (unauthorized.headers["www-authenticate"])
    )
    metadata = client.get("/.well-known/oauth-protected-resource/mcp", headers=public).json()
    assert metadata["resource"] == RESOURCE
    server_metadata = client.get("/.well-known/oauth-authorization-server", headers=public).json()
    assert server_metadata["issuer"] == PUBLIC

    registered = client.post(
        "/oauth/register", json={"client_name": "dots", "redirect_uris": [REDIRECT]}, headers=public
    )
    assert registered.status_code == 201
    client_id = registered.json()["client_id"]
    assert client.post("/oauth/register", content=b"{nope", headers=public).status_code == 400

    code_verifier = verifier()
    page = client.get("/oauth/authorize", params=authorize_params(client_id, code_verifier), headers=public)
    assert page.status_code == 200 and "dots" in page.text
    assert (
        page.headers["x-frame-options"] == "DENY"
        and "frame-ancestors 'none'" in page.headers["content-security-policy"]
    )
    pending_id = page.text.split('name="pending_id" value="')[1].split('"')[0]

    bad = client.get(
        "/oauth/authorize", params=authorize_params(client_id, code_verifier, redirect_uri="https://evil.example/cb")
    )
    assert bad.status_code == 400 and "location" not in bad.headers

    approval = client.post("/api/mcp/approval-code", json={}).json()["code"]
    wrong = client.post(
        "/oauth/authorize",
        data={"pending_id": pending_id, "approval_code": "AAAA-AAAA", "action": "approve"},
        headers=public,
        follow_redirects=False,
    )
    assert wrong.status_code == 400 and "not right" in wrong.text
    approved = client.post(
        "/oauth/authorize",
        data={"pending_id": pending_id, "approval_code": approval, "action": "approve"},
        headers=public,
        follow_redirects=False,
    )
    assert approved.status_code == 303 and approved.headers["location"].startswith(REDIRECT + "?")
    code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]

    token = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_verifier": code_verifier,
            "resource": RESOURCE,
        },
        headers=public,
    )
    assert token.status_code == 200 and token.headers["cache-control"] == "no-store"
    access = token.json()["access_token"]

    ok = client.post("/mcp", json=INIT, headers={**public, "Authorization": f"Bearer {access}"})
    assert ok.status_code == 200 and ok.json()["result"]["serverInfo"]["name"] == "homebody"

    agents = client.get("/api/mcp/status").json()["oauth"]["agents"]
    assert agents == [{"name": "dots", "connected": True}]

    reused = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code}, headers=public)
    assert reused.status_code == 400 and reused.json()["error"] == "invalid_request"

    assert client.post("/oauth/revoke", data={"token": access}, headers=public).status_code == 200
    assert client.post("/mcp", json=INIT, headers={**public, "Authorization": f"Bearer {access}"}).status_code == 401


def test_deny_over_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    app, client = build(monkeypatch, tmp_path, oauth_config())
    client_id = app._oauth.register({"redirect_uris": [REDIRECT]})["client_id"]
    pending = app._oauth.start_authorization(authorize_params(client_id, verifier()), resource=RESOURCE)

    denied = client.post(
        "/oauth/authorize", data={"pending_id": pending.pending_id, "action": "deny"}, follow_redirects=False
    )
    assert denied.status_code == 303 and "error=access_denied" in denied.headers["location"]
    again = client.post(
        "/oauth/authorize", data={"pending_id": pending.pending_id, "action": "deny"}, follow_redirects=False
    )
    assert again.status_code == 400


def test_public_host_only_reaches_agent_routes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _app, client = build(monkeypatch, tmp_path, oauth_config())

    for path in ("/", "/api/status", "/api/config", "/api/mcp/status"):
        assert client.get(path, headers={"Host": "reachy.example.com"}).status_code == 404
        assert client.get(path, headers={"X-Forwarded-Host": "REACHY.example.com"}).status_code == 404
    assert client.post("/api/mcp/approval-code", json={}, headers={"Host": "reachy.example.com"}).status_code == 404
    assert client.get("/api/status").status_code == 200  # the home network still sees everything

    # The never-expiring static token is for the home network only.
    static = {"Authorization": f"Bearer {STATIC}"}
    assert client.post("/mcp", json=INIT, headers=static).status_code == 200
    assert client.post("/mcp", json=INIT, headers={**static, "Host": "reachy.example.com"}).status_code == 401


def test_owner_routes_need_the_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    app, client = build(monkeypatch, tmp_path, oauth_config(api_key="owner-key"))
    granted(app._oauth)

    assert client.post("/api/mcp/approval-code", json={}).status_code == 403
    assert client.post("/api/mcp/oauth/disconnect", json={"current_api_key": "wrong"}).status_code == 403
    created = client.post("/api/mcp/approval-code", json={"current_api_key": "owner-key"})
    assert created.status_code == 200 and created.json()["expires_in"] == 600
    assert client.get("/api/mcp/status").json()["oauth"]["approval_code_active"] is True

    disconnected = client.post("/api/mcp/oauth/disconnect", json={"current_api_key": "owner-key"})
    assert disconnected.status_code == 200 and disconnected.json()["agents"] == []


def test_turning_on_sign_in_needs_the_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _app, client = build(monkeypatch, tmp_path, AppConfig(api_key="owner-key", mcp_enabled=True))
    update = {"mcp_oauth_enabled": True, "mcp_public_url": PUBLIC}

    assert client.post("/api/settings", json=update).status_code == 403
    assert not main_module.load_config().mcp_oauth_enabled
    saved = client.post("/api/settings", json={**update, "current_api_key": "owner-key"})
    assert saved.status_code == 200, saved.text
    assert main_module.load_config().mcp_public_url == PUBLIC
