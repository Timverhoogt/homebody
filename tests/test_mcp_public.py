from __future__ import annotations

import socket

import httpx
import pytest

from homebody.config import AppConfig
from homebody.mcp_oauth import OAuthServer
from homebody.mcp_public import PublicAgentListener, build_public_app, public_listener_wanted

PUBLIC = "https://reachy.example.com"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def config(port: int, **overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "mcp_enabled": True,
        "mcp_oauth_enabled": True,
        "mcp_public_url": PUBLIC,
        "mcp_public_port": port,
    }
    values.update(overrides)
    return AppConfig(**values)  # type: ignore[arg-type]


def listener_for(current: list[AppConfig]) -> PublicAgentListener:
    async def no_mcp(request, config):  # type: ignore[no-untyped-def]
        raise AssertionError("not called")

    return PublicAgentListener(
        lambda: build_public_app(
            oauth=OAuthServer(None),
            config_loader=lambda: current[-1],
            answer_mcp=no_mcp,
            same_origin=lambda origin, request: False,
        )
    )


def test_listener_only_runs_while_sign_in_is_on() -> None:
    assert public_listener_wanted(config(9000, mcp_oauth_enabled=False, mcp_public_url="")) is None
    assert public_listener_wanted(config(9000, mcp_enabled=False)) is None
    assert public_listener_wanted(config(9000)) == ("127.0.0.1", 9000)


def test_listener_starts_serves_only_agent_routes_moves_and_stops() -> None:
    pytest.importorskip("uvicorn")
    first, second = free_port(), free_port()
    current = [config(first)]
    listener = listener_for(current)
    try:
        listener.sync(current[-1])
        assert listener.status() == {"running": True, "address": f"127.0.0.1:{first}", "error": ""}
        base = f"http://127.0.0.1:{first}"
        assert httpx.get(f"{base}/.well-known/oauth-authorization-server").json()["issuer"] == PUBLIC
        for path in ("/", "/api/status", "/api/mcp/status"):
            assert httpx.get(f"{base}{path}", headers={"Host": "127.0.0.1"}).status_code == 404

        current.append(config(second))
        listener.sync(current[-1])
        assert listener.status()["address"] == f"127.0.0.1:{second}"
        with pytest.raises(httpx.ConnectError):
            httpx.get(f"{base}/.well-known/oauth-authorization-server")

        current.append(config(second, mcp_oauth_enabled=False, mcp_public_url=""))
        listener.sync(current[-1])
        assert listener.status() == {"running": False, "address": "", "error": ""}
        with pytest.raises(httpx.ConnectError):
            httpx.get(f"http://127.0.0.1:{second}/.well-known/oauth-authorization-server")
    finally:
        listener.stop()


def test_a_busy_port_is_reported_instead_of_crashing() -> None:
    pytest.importorskip("uvicorn")
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = int(busy.getsockname()[1])
        current = [config(port)]
        listener = listener_for(current)
        try:
            listener.sync(current[-1])
            status = listener.status()
            assert status["running"] is False and str(port) in str(status["error"])
        finally:
            listener.stop()


@pytest.mark.parametrize(
    ("overrides", "ok"),
    [
        ({"mcp_public_port": 8043}, True),
        ({"mcp_public_bind": "0.0.0.0"}, True),
        ({"mcp_public_port": 8042}, False),
        ({"mcp_public_port": 80}, False),
        ({"mcp_public_bind": "reachy.local"}, False),
    ],
)
def test_listener_address_validation(overrides: dict[str, object], ok: bool) -> None:
    if ok:
        AppConfig(**overrides)  # type: ignore[arg-type]
    else:
        with pytest.raises(ValueError, match="mcp_public"):
            AppConfig(**overrides)  # type: ignore[arg-type]
