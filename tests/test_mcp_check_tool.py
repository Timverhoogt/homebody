from __future__ import annotations

import importlib.util
import io
import urllib.error
from pathlib import Path

import pytest
from test_mcp_server import TOKEN, build, enabled_config

_SPEC = importlib.util.spec_from_file_location("mcp_check", Path(__file__).parents[1] / "tools" / "mcp_check.py")
mcp_check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mcp_check)  # type: ignore[union-attr]


def route_to(client, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """Send the script's urllib requests to the in-process app."""

    def urlopen(request, timeout: float):  # type: ignore[no-untyped-def]
        response = client.post("/mcp", content=request.data, headers=dict(request.header_items()))
        if response.status_code >= 400:
            raise urllib.error.HTTPError(request.full_url, response.status_code, "error", {}, None)
        return io.BytesIO(response.content)

    monkeypatch.setattr(mcp_check.urllib.request, "urlopen", urlopen)


def test_check_reports_status_and_actions(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    app, client, _stored = build(monkeypatch, enabled_config())
    route_to(client, monkeypatch)

    code = mcp_check.run(["http://reachy/mcp", "--token", TOKEN, "--say", "Testing", "--emotion", "happy"])

    out = capsys.readouterr().out
    assert code == 0
    assert "✓ connected to Homebody" in out and "✓ tools: get_status, announce, express_emotion" in out
    assert "✓ announce" in out and "✓ express_emotion" in out
    assert app._runtime.announcements == ["Testing"]  # type: ignore[union-attr]


def test_check_is_read_only_by_default_and_explains_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app, client, _stored = build(monkeypatch, enabled_config())
    route_to(client, monkeypatch)

    assert mcp_check.run(["http://reachy/mcp", "--token", TOKEN]) == 0
    assert app._runtime.announcements == [] and app._runtime.actions == []  # type: ignore[union-attr]

    assert mcp_check.run(["http://reachy/mcp", "--token", "wrong"]) == 1
    assert "token was refused" in capsys.readouterr().err
