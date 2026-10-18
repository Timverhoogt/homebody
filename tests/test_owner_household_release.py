"""Offline route isolation, saved Owner authority and bounded lease renewal."""

import asyncio
import json
from dataclasses import asdict

import httpx
import pytest
from test_everyday_runtime import runtime
from test_owner_auth import pair
from test_owner_household_setup import owner  # noqa: F401
from test_owner_show_and_tell import owner_headers

from homebody import household_client as connection
from homebody.config import AppConfig, load_config
from homebody.hermes_client import HermesBridgeClient


def test_unset_defaults_and_private_secret():
    config = AppConfig(api_key="legacy")
    assert config.household_endpoint == config.bridge_url
    assert config.household_credential == "legacy"
    config = AppConfig(api_key="legacy", household_url="https://house.invalid", household_api_key="house-secret")
    assert "house-secret" not in json.dumps(config.redacted_dict())
    assert "house-secret" not in json.dumps(config.child_status_dict())
    assert config.household_credential == "house-secret"


@pytest.mark.parametrize(
    "url", ["http://remote.invalid", "https://user:secret@house.invalid", "https://house.invalid?q=secret"]
)
def test_unsafe_endpoint_refused(url):
    with pytest.raises(ValueError):
        AppConfig(household_url=url, household_api_key="synthetic")


def test_owner_configuration_preserves_legacy_and_never_returns_secret(owner):  # noqa: F811
    robot, client, code, path, calls = owner
    body = dict(url="https://house.invalid", robot_credential="house-secret", enabled=True, acknowledged=True)
    assert client.post("/api/household/tools/configure", json=body).status_code == 401
    assert pair(client, code).status_code == 200
    assert client.post("/api/household/tools/configure", json=body).status_code == 403
    headers = owner_headers(client)
    assert (
        client.post(
            "/api/household/tools/configure", json={**body, "acknowledged": False}, headers=headers
        ).status_code
        == 400
    )
    response = client.post("/api/household/tools/configure", json=body, headers=headers)
    assert response.status_code == 200 and "house-secret" not in response.text
    saved = load_config(path)
    assert saved.api_key == "test-key" and saved.bridge_url == AppConfig().bridge_url
    assert saved.household_api_key == "house-secret" and saved.household_connection_enabled
    assert calls == []
    assert "house-secret" not in client.get("/api/household/tools/status").text
    assert (
        client.post(
            "/api/household/tools/configure", json={**body, "robot_credential": ""}, headers=headers
        ).status_code
        == 200
    )
    assert load_config(path).household_api_key == "house-secret"
    robot._runtime.kids_controls_locked = True
    assert client.post("/api/household/tools/configure", json=body, headers=headers).status_code == 423


@pytest.mark.parametrize("profile,offline", [("conversation", "legacy.invalid"), ("agent", "house.invalid")])
def test_session_routes_are_independent_and_revoke_both(profile, offline):
    r, _ = runtime()
    context = r.agent_broker_context(explicit_private_intent=False)
    from dataclasses import replace

    context = replace(context, capability_profile=profile)
    calls = []

    def send(request):
        calls.append((request.url.host, request.headers["authorization"], request.url.path))
        if request.url.host == offline:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True, "session_generation": context.session_generation})

    config = AppConfig(
        bridge_url="https://legacy.invalid",
        api_key="legacy",
        household_url="https://house.invalid",
        household_api_key="house",
    )
    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        HermesBridgeClient(config, client=http).establish_agent_session(context)
    assert calls == [
        ("house.invalid", "Bearer house", "/v1/household/session"),
        ("legacy.invalid", "Bearer legacy", "/v1/agent/session"),
    ]


def test_http_everyday_and_shared_speech_routes_do_not_cross():
    calls = []

    def send(request):
        calls.append((request.url.host, request.headers.get("authorization"), request.url.path))
        return httpx.Response(200, json={"text": "synthetic", "tool_free": True, "status": "ok"})

    config = AppConfig(
        bridge_url="https://legacy.invalid",
        api_key="legacy",
        household_url="https://house.invalid",
        household_api_key="house",
    )
    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        client = HermesBridgeClient(config, client=http)
        client.everyday_capabilities()
        client.everyday_turn("synthetic")
        client.look(b"synthetic", "synthetic")
        client.look_follow_up("synthetic", "synthetic")
        client.set_kids_session_state("kids-" + "a" * 32, active=False)
    assert all(host == "house.invalid" and auth == "Bearer house" for host, auth, _ in calls[:4])
    assert calls[-1][:2] == ("legacy.invalid", "Bearer legacy")


@pytest.mark.parametrize("revoke", ["stop", "kids", "privacy", "permission", "endpoint"])
def test_renewal_cannot_restore_revoked_authority(monkeypatch, revoke):
    r, config = runtime(mcp_enabled=True, household_connection_enabled=True)
    attempts = []

    async def callbacks(runtime, **kw):
        attempts.append(asdict(kw["context"]))
        kw["ready"].set()
        if revoke == "stop":
            r._request_conversation_stop()
        elif revoke == "kids":
            r._kids_active = True
        elif revoke == "privacy":
            r._privacy_requested.set()
        elif revoke == "permission":
            config.mcp_enabled = False
        else:
            config.household_url = "https://changed.invalid"
        raise TimeoutError

    monkeypatch.setattr(connection, "household_callbacks", callbacks)
    with pytest.raises(RuntimeError):
        asyncio.run(connection.household_connection(r))
    assert len(attempts) == 1


@pytest.mark.parametrize("early_close", [False, True])
def test_transient_failures_have_bounded_retries(monkeypatch, early_close):
    r, _ = runtime(mcp_enabled=True, household_connection_enabled=True)
    attempts, delays = [], []

    async def callbacks(*args, **kw):
        attempts.append(1)
        if early_close:
            kw["ready"].set()
            return False
        raise OSError("synthetic")

    async def sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(connection, "household_callbacks", callbacks)
    monkeypatch.setattr(connection.asyncio, "sleep", sleep)
    with pytest.raises(RuntimeError, match="limit"):
        asyncio.run(connection.household_connection(r))
    assert len(attempts) == 4 and delays == [2, 4, 8]


def test_expired_healthy_lease_renews_then_stop_ends(monkeypatch):
    r, _ = runtime(mcp_enabled=True, household_connection_enabled=True)
    attempts = []

    async def callbacks(*args, **kw):
        attempts.append(kw)
        kw["ready"].set()
        if len(attempts) == 2:
            r._request_conversation_stop()
        raise TimeoutError

    async def sleep(seconds):
        assert seconds == 1

    monkeypatch.setattr(connection, "household_callbacks", callbacks)
    monkeypatch.setattr(connection.asyncio, "sleep", sleep)
    with pytest.raises(RuntimeError):
        asyncio.run(connection.household_connection(r))
    assert len(attempts) == 2


@pytest.mark.parametrize("mode", ["everyday", "agent"])
@pytest.mark.parametrize("kind", ["live", "realtime"])
def test_speech_websockets_select_only_their_mode_endpoint(monkeypatch, mode, kind):
    from homebody import live_client, realtime_client
    module = live_client if kind == "live" else realtime_client
    cls = live_client.LiveBridgeSession if kind == "live" else realtime_client.RealtimeBridgeSession
    calls = []
    class Socket:
        def send(self, payload):
            pass
        def close(self):
            pass
    def connect(url, **kwargs):
        calls.append((url, kwargs["additional_headers"]["Authorization"]))
        return Socket()
    monkeypatch.setattr(module, "connect", connect)
    monkeypatch.setattr(cls, "_receive_loop", lambda self: None)
    config = AppConfig(bridge_url="https://legacy.invalid", api_key="legacy",
                       household_url="https://house.invalid", household_api_key="house")
    from dataclasses import replace
    r, _ = runtime()
    context = replace(r.agent_broker_context(explicit_private_intent=False),
                      capability_profile="conversation" if mode == "everyday" else "agent")
    session = cls(config, agent_context=context)
    session.start()
    session.close()
    host, key = ("house.invalid", "house") if mode == "everyday" else ("legacy.invalid", "legacy")
    assert calls == [(f"wss://{host}/v1/{kind}", f"Bearer {key}")]

