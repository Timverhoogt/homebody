"""Exercise the diagnostic's real HTTP boundary without contacting any device."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_mcp_check_tool import mcp_check


@contextmanager
def endpoint(status=200, body=None, *, redirect=False):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, self.headers.get("Authorization")))
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(status)
            if redirect:
                # A different hostname is a different origin, even on loopback.
                self.send_header("Location", f"http://localhost:{self.server.server_port}/sink")
            self.end_headers()
            self.wfile.write(body if body is not None else b'{"jsonrpc":"2.0","id":1,"result":{}}')

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"jsonrpc":"2.0","id":1,"result":{}}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_never_forwards_caller_credential(status):
    with endpoint(status, redirect=True) as (url, requests):
        with pytest.raises(mcp_check.CheckFailed):
            mcp_check.rpc(url, "test-only-token", "ping")
        assert requests == [("/mcp", "Bearer test-only-token")]


def test_valid_rpc_retains_direct_authenticated_transport():
    with endpoint(body=b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}') as (url, requests):
        assert mcp_check.rpc(url, "test-only-token", "ping") == {"ok": True}
        assert requests == [("/mcp", "Bearer test-only-token")]


@pytest.mark.parametrize("payload", [
    {"jsonrpc": "2.0", "id": 99, "result": {}},
    {"jsonrpc": "1.0", "id": 1, "result": {}},
    {"jsonrpc": "2.0", "id": 1, "result": []},
    [],
])
def test_rpc_rejects_invalid_envelope(payload):
    with endpoint(body=json.dumps(payload).encode()) as (url, _requests):
        with pytest.raises(mcp_check.CheckFailed):
            mcp_check.rpc(url, "test-only-token", "ping")


def test_rpc_rejects_oversized_response():
    with endpoint(body=b'{"jsonrpc":"2.0","id":1,"result":{"text":"' + b'x' * (1024 * 1024) + b'"}}') as (url, _):
        with pytest.raises(mcp_check.CheckFailed, match="response"):
            mcp_check.rpc(url, "test-only-token", "ping")


def test_rpc_does_not_echo_untrusted_server_error():
    payload = {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "private-server-content"}}
    with endpoint(body=json.dumps(payload).encode()) as (url, _requests):
        with pytest.raises(mcp_check.CheckFailed) as caught:
            mcp_check.rpc(url, "test-only-token", "ping")
        assert "private-server-content" not in str(caught.value)


def test_rpc_does_not_use_inherited_proxy(monkeypatch):
    with endpoint() as (proxy, requests):
        monkeypatch.setenv("http_proxy", proxy.removesuffix("/mcp"))
        monkeypatch.setenv("https_proxy", proxy.removesuffix("/mcp"))
        monkeypatch.setattr(mcp_check.urllib.request, "_opener", None)
        monkeypatch.setenv("no_proxy", "")
        monkeypatch.setenv("NO_PROXY", "")
        # The reserved invalid destination must never reach the proxy with its token.
        with pytest.raises(mcp_check.CheckFailed):
            mcp_check.rpc("http://homebody.invalid/mcp", "test-only-token", "ping")
        assert requests == []
