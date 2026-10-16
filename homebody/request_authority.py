"""Request-local cancellation at the final HTTP submission boundary.

Callbacks stay in process, never in headers or JSON. Checking both before dispatch and in
HTTPX's request hook covers client construction and request/body preparation independently.
Already-sent bytes cannot be revoked; their responses still need the caller's result fence.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

Current = Callable[[], bool] | None
_EXTENSION = "homebody_request_current"


def require_current(current: Current) -> None:
    if current is not None and not current():
        raise RuntimeError("Request was cancelled before submission")


def _request_guard(request: httpx.Request) -> None:
    current = request.extensions.pop(_EXTENSION, None)
    require_current(current)


def install_request_guard(client: httpx.Client) -> None:
    hooks = client.event_hooks.setdefault("request", [])
    if _request_guard not in hooks:
        hooks.append(_request_guard)


def guarded_json_post(client: httpx.Client, url: str, *, current: Current, **kwargs) -> httpx.Response:
    require_current(current)
    if current is not None:
        kwargs["extensions"] = {_EXTENSION: current}
    return client.post(url, **kwargs)
