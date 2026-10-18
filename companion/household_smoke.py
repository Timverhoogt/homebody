"""Explicit, read-only MCP wire check; never attaches a robot or selects arbitrary tools."""
from __future__ import annotations

import argparse
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Redirect refused")


def endpoint(base: str) -> str:
    p = urlsplit(base)
    if (not p.hostname or p.username or p.password or p.query or p.fragment
            or p.path not in {"", "/"}):
        raise ValueError("Use an origin, without credentials, path, query or fragment")
    if p.scheme != "https" and not (
        p.scheme == "http" and p.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("Remote MCP requires HTTPS; HTTP is loopback-only")
    return base.rstrip("/") + "/mcp"


def check(base: str, token: str, scope: dict | None, *, opener=None) -> dict:
    url = endpoint(base)
    if not token or any(c in token for c in "\r\n"):
        raise ValueError("Provide a caller credential through the environment")
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-03-26"}
    if scope is not None:
        if (not isinstance(scope, dict) or scope.get("connected") is not True
                or not isinstance(scope.get("device_id"), str)
                or not scope["device_id"] or not isinstance(scope.get("homebody_context"), dict)):
            raise ValueError("Use the unmodified Owner connect response")
        headers["X-Reachy-Device-Id"] = scope["device_id"]
    # Do not forward credentials through inherited HTTP proxies or redirects.
    opener = opener or build_opener(ProxyHandler({}), NoRedirect())

    def send(method, params, identifier=None):
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if identifier is not None:
            body["id"] = identifier
        request = Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        with opener.open(request, timeout=20) as response:
            if identifier is None:
                if response.status != 202:
                    raise ValueError("Notification not accepted")
                return {}
            if response.status != 200:
                raise ValueError("Unexpected MCP response status")
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Oversized MCP response")
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("jsonrpc") != "2.0"
                or value.get("id") != identifier or "error" in value
                or not isinstance(value.get("result"), dict)):
            raise ValueError("MCP request refused; check scope and attachment")
        return value["result"]

    init = send("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                              "clientInfo": {"name": "homebody-readonly-check", "version": "1"}}, 1)
    if init.get("protocolVersion") != "2025-03-26" or init.get("serverInfo", {}).get("name") != "homebody-household":
        raise ValueError("Unexpected MCP server identity or protocol")
    send("notifications/initialized", {})
    send("ping", {}, 2)
    if scope is None:
        return {"health": "ok", "scoped_tools": "not_checked"}
    meta = {"homebody_context": scope["homebody_context"]}
    listed = send("tools/list", {"_meta": meta}, 3)
    names = [tool["name"] for tool in listed["tools"]]
    if "get_current_time" not in names:
        raise ValueError("Read-only clock tool missing")
    result = send("tools/call", {"_meta": meta, "name": "get_current_time", "arguments": {}}, 4)
    if result.get("isError") or result.get("structuredContent", {}).get("ok") is not True:
        raise ValueError("Read-only clock call failed")
    # Never print scope, credentials, or arbitrary server content.
    return {"health": "ok", "scoped_tools": "ok", "tool_count": len(names),
            "read_only_call": "get_current_time", "readback_ok": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8644", help="Operator-approved origin")
    parser.add_argument("--scope", help="Private JSON file containing the Owner connect response")
    args = parser.parse_args(argv)
    try:
        scope = None
        if args.scope:
            with open(args.scope, encoding="utf-8") as stream:
                scope = json.load(stream)
        print(json.dumps(check(args.url, os.environ.get("HOMEBODY_HOUSEHOLD_TOKEN", ""), scope)))
    except (ValueError, KeyError, TypeError, AttributeError, OSError, HTTPError, URLError):
        parser.exit(1, "Household check failed; inspect auth, TLS, artifact and current Owner attachment.\n")


if __name__ == "__main__":
    main()
