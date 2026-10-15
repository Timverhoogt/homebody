"""Native Structured Outputs through the REAL installed Hermes Codex adapter with a fake transport.

The installed ``agent.auxiliary_client`` builder, ``create`` streaming/guard path and response
normalizer run unmodified; only the OpenAI client's ``responses.create`` is a synthetic stand-in.
No hermes_bootstrap, no auth resolution, no network. Skips (explicitly) when the installed source
cannot be imported by the isolated test interpreter.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion import subscription_reasoning as sub
from companion.reachy_agent_broker import BrokerUnavailableError

HERMES_SOURCE = Path(os.environ.get("HOMEBODY_HERMES_SOURCE", "/home/timv/.hermes/hermes-agent"))
CODEX_URL = "https://chatgpt.com/backend-api/codex/"
MODEL = "gpt-sub-test"


def installed_adapter():
    if not (HERMES_SOURCE / "agent" / "auxiliary_client.py").is_file():
        pytest.skip(f"installed Hermes source not present at {HERMES_SOURCE}")
    if str(HERMES_SOURCE) not in sys.path:
        sys.path.append(str(HERMES_SOURCE))  # after the worktree: never shadows Homebody modules
    # Full-suite collection imports Homebody's namespace `tools.checks` first. Its cached
    # namespace predates Hermes' regular tools package, so path discovery alone cannot find
    # the real adapter's lazy tools.threat_patterns dependency. Extend only that namespace
    # with the actual installed source; do not stub or replace either project's modules.
    tools = sys.modules.get("tools")
    hermes_tools = str(HERMES_SOURCE / "tools")
    if tools is not None and hasattr(tools, "__path__") and hermes_tools not in tools.__path__:
        tools.__path__.append(hermes_tools)
    try:
        from agent.auxiliary_client import CodexAuxiliaryClient, _CodexCompletionsAdapter
    except Exception as exc:  # noqa: BLE001 - report the exact missing dependency as a skip reason
        pytest.skip(f"installed Hermes adapter not importable here: {type(exc).__name__}: {exc}")
    return CodexAuxiliaryClient, _CodexCompletionsAdapter


def output_message(text: str | None = None, *, refusal: str | None = None) -> SimpleNamespace:
    part = (
        SimpleNamespace(type="refusal", refusal=refusal)
        if refusal is not None
        else SimpleNamespace(type="output_text", text=text, annotations=[])
    )
    return SimpleNamespace(
        type="message", role="assistant", id="msg_synthetic", status="completed", content=[part],
    )


class FakeResponses:
    def __init__(self, replies: list[object]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, object]] = []
        self.lock = threading.Lock()

    def create(self, **kwargs: object) -> SimpleNamespace:
        with self.lock:
            self.calls.append(copy.deepcopy(kwargs))
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        item = reply if isinstance(reply, SimpleNamespace) else output_message(str(reply))
        return SimpleNamespace(
            output=[item], status="completed", output_text=None, usage=None, error=None,
            incomplete_details=None, close=lambda: None,
        )


class FakeOpenAI:
    def __init__(self, replies: list[object]) -> None:
        self.base_url = CODEX_URL
        self.api_key = "synthetic-not-a-token"
        self.responses = FakeResponses(replies)

    def close(self) -> None:
        pass


def codex_client(monkeypatch: pytest.MonkeyPatch, replies: list[object]):
    """A real installed CodexAuxiliaryClient over a fake transport, installed as the resolved client."""
    client_class, _adapter_class = installed_adapter()
    transport = FakeOpenAI(replies)
    client = client_class(transport, MODEL)
    monkeypatch.setattr(sub, "_resolve_client", lambda _model: client)
    return client, transport


def provider() -> sub.SubscriptionTextProvider:
    return sub.SubscriptionTextProvider(MODEL, models={"agent": MODEL, "kids": MODEL, "ispy": MODEL})


MATCH = {
    "type": "object", "additionalProperties": False,
    "properties": {"match": {"type": "boolean"}}, "required": ["match"],
}
GUESS = {
    "type": "object", "additionalProperties": False,
    "properties": {"guess": {"type": "string", "minLength": 1, "maxLength": 60}}, "required": ["guess"],
}


def schema_body(schema: dict, name: str = "ispy_match", **extra: object) -> dict[str, object]:
    return {
        "model": MODEL,
        "messages": [{"role": "system", "content": "Host rules."}, {"role": "user", "content": "Is a mug a cup?"}],
        "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
        "max_completion_tokens": 40, "store": False, **extra,
    }


def run(coro):
    return asyncio.run(coro)


def test_strict_text_format_reaches_the_final_wire_with_reasoning_none(monkeypatch) -> None:
    _client, transport = codex_client(monkeypatch, ['{"match": true}'])
    result = run(provider().complete(schema_body(MATCH), timeout_seconds=5))
    assert result["choices"][0]["message"]["content"] == '{"match": true}'
    [wire] = transport.responses.calls
    assert wire["text"] == {"format": {"type": "json_schema", "name": "ispy_match", "strict": True, "schema": MATCH}}
    assert wire["reasoning"] == {"effort": "none"}
    assert wire["stream"] is True and "response_format" not in wire
    assert "response_format" not in json.dumps(wire.get("extra_body", {}))
    assert "Host rules." in wire["instructions"] and "Return only JSON" in wire["instructions"]


def test_host_only_length_keywords_stay_off_the_wire_and_the_caller_schema_is_not_mutated(monkeypatch) -> None:
    _client, transport = codex_client(monkeypatch, ['{"guess": "mug"}'])
    original = copy.deepcopy(GUESS)
    run(provider().complete(schema_body(GUESS, "ispy_player_object_guess"), timeout_seconds=5))
    wire_schema = transport.responses.calls[0]["text"]["format"]["schema"]
    assert wire_schema["properties"]["guess"] == {"type": "string"}
    assert GUESS == original  # the bridge still validates 1..60 characters on the reply


def test_image_parts_convert_to_responses_input_images(monkeypatch) -> None:
    _client, transport = codex_client(monkeypatch, ['{"match": false}'])
    pixel = base64.b64encode(b"\xff\xd8\xff\xd9").decode("ascii")  # tiny synthetic JPEG markers only
    body = schema_body(MATCH)
    body["messages"] = [{"role": "user", "content": [
        {"type": "text", "text": "Look."},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{pixel}", "detail": "low"}},
    ]}]
    run(provider().complete(body, timeout_seconds=5))
    wire = transport.responses.calls[0]
    serialized = json.dumps(wire.get("extra_body", {}).get("input", wire.get("input")))
    assert "input_image" in serialized and pixel in serialized
    assert wire["text"]["format"]["name"] == "ispy_match"


def test_ordinary_tool_and_history_calls_are_unchanged(monkeypatch) -> None:
    _client, transport = codex_client(monkeypatch, ["Done."])
    tools = [{"type": "function", "function": {
        "name": "get_current_time", "description": "Clock",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}]
    body = {"model": MODEL, "tools": tools, "messages": [
        {"role": "user", "content": "Time?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "get_current_time", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "12:00"},
    ]}
    result = run(provider().complete(body, timeout_seconds=5))
    assert result["choices"][0]["message"]["content"] == "Done."
    wire = transport.responses.calls[0]
    assert "text" not in wire and wire["reasoning"] == {"effort": "none"}
    payload = json.dumps(wire.get("extra_body", {}))
    assert "function_call_output" in payload and "get_current_time" in payload


def test_shared_adapter_is_never_modified_and_concurrent_formats_do_not_mix(monkeypatch) -> None:
    client, transport = codex_client(monkeypatch, ['{"match": true}'])
    _client_class, adapter_class = installed_adapter()
    shared = client.chat.completions
    before = (type(shared), dict(vars(shared)), adapter_class._build_responses_kwargs, adapter_class.create)
    errors: list[BaseException] = []

    def call(schema: dict, name: str) -> None:
        try:
            run(provider().complete(schema_body(schema, name), timeout_seconds=5))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [
        threading.Thread(target=call, args=(MATCH, f"ispy_match_{i}") if i % 2 else (GUESS, f"ispy_guess_{i}"))
        for i in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == []
    formats = {wire["text"]["format"]["name"]: wire["text"]["format"]["schema"] for wire in transport.responses.calls}
    assert len(formats) == 6
    for name, schema in formats.items():
        assert schema == (MATCH if "match" in name else sub._native_schema(GUESS))
    assert (type(shared), dict(vars(shared)), adapter_class._build_responses_kwargs, adapter_class.create) == before
    assert "responses" not in vars(shared) and type(client._real_client.responses) is FakeResponses


@pytest.mark.parametrize(
    "reply",
    [
        output_message(refusal="I can't help with that."),
        "not json",
        "[1, 2]",
        '"just a string"',
    ],
)
def test_refusal_or_malformed_structured_reply_fails_closed(monkeypatch, reply) -> None:
    codex_client(monkeypatch, [reply])
    with pytest.raises(BrokerUnavailableError):
        run(provider().complete(schema_body(MATCH), timeout_seconds=5))


@pytest.mark.parametrize(
    "schema_change",
    [
        {"properties": {"match": {"type": "string", "pattern": "^a$"}}},
        {"properties": {"match": {"anyOf": [{"type": "boolean"}]}}},
        {"additionalProperties": True},
        {"required": []},
    ],
)
def test_unenforceable_schema_fails_closed_before_any_request(monkeypatch, schema_change) -> None:
    _client, transport = codex_client(monkeypatch, ['{"match": true}'])
    with pytest.raises(BrokerUnavailableError):
        run(provider().complete(schema_body({**MATCH, **schema_change}), timeout_seconds=5))
    assert transport.responses.calls == []


@pytest.mark.parametrize("change", [{"strict": False}, {"name": "bad name!"}])
def test_non_strict_or_badly_named_format_fails_closed(monkeypatch, change) -> None:
    _client, transport = codex_client(monkeypatch, ['{"match": true}'])
    body = schema_body(MATCH)
    body["response_format"]["json_schema"].update(change)  # type: ignore[index]
    with pytest.raises(BrokerUnavailableError):
        run(provider().complete(body, timeout_seconds=5))
    assert transport.responses.calls == []


def test_unknown_adapter_cannot_receive_a_schema(monkeypatch) -> None:
    created: list = []
    fake = SimpleNamespace(base_url=CODEX_URL, chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: created.append(kw),
        _build_responses_kwargs=lambda kw: ({"reasoning": {"effort": "none"}}, MODEL, 5))))
    installed_adapter()
    monkeypatch.setattr(sub, "_resolve_client", lambda _model: fake)
    with pytest.raises(BrokerUnavailableError):
        run(provider().complete(schema_body(MATCH), timeout_seconds=5))
    assert created == []


def test_schema_request_fails_closed_when_the_installed_adapter_is_not_importable(monkeypatch) -> None:
    """Runs everywhere: without the installed Codex adapter class, no schema request is ever sent."""
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", None)  # import raises ImportError
    adapter = SimpleNamespace(create=lambda **kw: pytest.fail("schema request sent without enforcement"))
    with pytest.raises(BrokerUnavailableError):
        sub._structured_adapter(adapter, sub.native_text_format(schema_body(MATCH)["response_format"]))


def test_final_wire_guard_rejects_a_missing_or_smuggled_format() -> None:
    expected = {"format": {"type": "json_schema", "name": "x", "strict": True, "schema": MATCH}}
    sent: list = []
    guard = sub._WireCheckedResponses(SimpleNamespace(create=lambda **kw: sent.append(kw)), expected)
    with pytest.raises(BrokerUnavailableError):
        guard.create(model=MODEL)
    with pytest.raises(BrokerUnavailableError):
        guard.create(model=MODEL, text=expected, extra_body={"text": {"format": {"type": "text"}}})
    guard.create(model=MODEL, text=copy.deepcopy(expected))
    assert len(sent) == 1


def test_probe_dry_run_is_offline_and_prints_only_the_native_formats(monkeypatch, capsys) -> None:
    import importlib.util

    path = Path(__file__).resolve().parents[1] / ".artifacts" / "structured_probe.py"
    if not path.is_file():
        pytest.skip("probe script is a local artifact, not shipped")
    monkeypatch.setattr(sub, "_resolve_client", lambda _model: pytest.fail("dry run resolved auth"))
    monkeypatch.setattr(sys, "argv", [str(path)])
    spec = importlib.util.spec_from_file_location("homebody_structured_probe", path)
    probe = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(probe)
    assert probe.main() == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[-1] == {"result": "dry_run_only", "network": False, "auth_resolved": False}
    formats = {line["step"]: line["text"]["format"] for line in lines[:-1]}
    assert formats["tiny_json"]["schema"] == MATCH and formats["tiny_json"]["strict"] is True
    assert formats["tiny_image_ispy_schema"]["name"] == "kids_ispy_candidates"
    assert "hermes_bootstrap" not in sys.modules
    assert probe.png_square().startswith(b"\x89PNG\r\n\x1a\n")


def test_bridge_schemas_project_onto_the_strict_subset() -> None:
    from test_bridge import load_bridge_module

    module = load_bridge_module()
    names = ("_ISPY_RESPONSE_SCHEMA", "_ISPY_MATCH_SCHEMA", "_ISPY_PLAYER_GUESS_SCHEMA", "_KIDS_HOSTED_TURN_SCHEMA")
    for name in names:
        original = copy.deepcopy(getattr(module, name))
        wire = sub._native_schema(getattr(module, name))
        assert "minLength" not in json.dumps(wire) and "maxLength" not in json.dumps(wire)
        assert getattr(module, name) == original
