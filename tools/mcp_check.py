#!/usr/bin/env python3
"""Check a Homebody agent-access (MCP) endpoint the way an agent would. Standard library only.

    python tools/mcp_check.py http://reachy-mini.local:8042/mcp --token hb_...
    python tools/mcp_check.py http://reachy-mini.local:8042/mcp --token hb_... --say "Testing, one two"

Read-only by default: it connects, lists the tools and asks for Reachy's status. ``--say`` and
``--emotion`` also exercise the actions, which follow the same Meeting, Sleep, privacy and Kids
Mode rules as any agent. Exit code 0 means every step worked (a refusal with a reason counts).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2025-06-18"


class CheckFailed(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A diagnostic credential belongs only to the endpoint the operator selected."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CheckFailed("MCP endpoint redirected; use its approved direct URL.")


MAX_RESPONSE_BYTES = 1024 * 1024


def rpc(url: str, token: str, method: str, params: dict | None = None, *, request_id: int | None = 1) -> dict:
    message: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if request_id is not None:
        message["id"] = request_id
    request = urllib.request.Request(
        url,
        data=json.dumps(message).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        },
    )
    # Never send a caller token through inherited proxies or to redirect destinations.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        hints = {
            401: "the token was refused. Create a new one in Settings → Agent access (MCP).",
            403: "the request was refused (cross-origin, or an owner setting).",
            404: "agent access is off, or no token exists yet. Turn it on in Settings → Agent access (MCP).",
        }
        raise CheckFailed(f"HTTP {exc.code}: {hints.get(exc.code, 'MCP request failed.')}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CheckFailed("Could not reach the MCP endpoint; check the approved URL and connection.") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise CheckFailed("MCP response exceeds the size limit.")
    if request_id is None:
        return {}
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError) as exc:
        raise CheckFailed("Invalid MCP response JSON.") from exc
    if (
        not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0"
        or type(payload.get("id")) is not type(request_id) or payload.get("id") != request_id
    ):
        raise CheckFailed("Invalid MCP response envelope.")
    if "error" in payload:
        raise CheckFailed("MCP request failed; the server returned an error.")
    if not isinstance(payload.get("result"), dict):
        raise CheckFailed("Invalid MCP response result.")
    return payload["result"]


def tool(url: str, token: str, name: str, arguments: dict) -> tuple[bool, str, dict]:
    result = rpc(url, token, "tools/call", {"name": name, "arguments": arguments}, request_id=3)
    text = " ".join(item.get("text", "") for item in result.get("content", []))
    return bool(result.get("isError")), text, result.get("structuredContent") or {}


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", help="the MCP endpoint, for example http://reachy-mini.local:8042/mcp")
    parser.add_argument("--token", required=True, help="the agent token from Settings → Agent access (MCP)")
    parser.add_argument("--say", help="also ask Reachy to say this aloud")
    parser.add_argument("--emotion", help="also ask Reachy to show this emotion, for example happy")
    args = parser.parse_args(argv)

    try:
        init = rpc(
            args.url,
            args.token,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "homebody-mcp-check", "version": "1"},
            },
        )
        print(
            f"✓ connected to {init['serverInfo'].get('title') or init['serverInfo']['name']} "
            f"(protocol {init['protocolVersion']})"
        )
        rpc(args.url, args.token, "notifications/initialized", request_id=None)
        tools = [item["name"] for item in rpc(args.url, args.token, "tools/list", {}, request_id=2)["tools"]]
        print(f"✓ tools: {', '.join(tools)}")
        _error, text, status = tool(args.url, args.token, "get_status", {})
        print(f"✓ status: {text}")
        for action, reason in (status.get("why_not") or {}).items():
            if reason not in text:
                print(f"  · {action} unavailable: {reason}")
        if args.say:
            refused, text, _ = tool(args.url, args.token, "announce", {"text": args.say})
            print(f"{'·' if refused else '✓'} announce: {text}")
        if args.emotion:
            refused, text, _ = tool(args.url, args.token, "express_emotion", {"emotion": args.emotion})
            print(f"{'·' if refused else '✓'} express_emotion: {text}")
    except CheckFailed as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
