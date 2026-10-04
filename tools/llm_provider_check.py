#!/usr/bin/env python3
"""Check that your text-model provider (OpenAI, Cortecs, LLMrouter.eu or custom) handles every bridge request.

Run it on the machine that runs the bridge, with the same environment, from the repository root:

    REACHY_LLM_PROVIDER=cortecs CORTECS_API_KEY=... REACHY_LLM_MODEL=mistral-small-2603 \\
        python tools/llm_provider_check.py
    python tools/llm_provider_check.py --photo desk.jpg   # also I Spy target selection from a photo

It starts the real bridge in-process (nothing listens on the network) and drives the same code paths
Reachy uses, so any provider quirk shows up here first:

* I Spy guess judging and Reachy's own guesses: strict JSON-schema output;
* Agent Mode planning: tools with ``tool_choice: required`` and parallel tool calls;
* Agent Mode answers: tools and strict JSON-schema output together, plus a tool-result round trip;
* Kids chat and I Spy target selection: these also need ``OPENAI_API_KEY``, because Kids Mode
  moderation always stays on OpenAI. Without it they are skipped.

Prompts are synthetic; no conversation, camera frame or personal data is sent unless you pass --photo.
Exit code 0 means every check that ran passed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import secrets
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEVICE = "provider-check"


class Recorder:
    """Wrap the bridge's HTTP session to keep the provider's last raw answer for diagnosis."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self.last: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def post(self, *args: Any, **kwargs: Any) -> Any:
        recorder, context = self, self._session.post(*args, **kwargs)

        class _Recording:
            async def __aenter__(self) -> Any:
                response = await context.__aenter__()
                original = response.json

                async def json_and_keep(*json_args: Any, **json_kwargs: Any) -> Any:
                    body = await original(*json_args, **json_kwargs)
                    recorder.last = {"status": response.status, "body": body}
                    return body

                response.json = json_and_keep
                return response

            async def __aexit__(self, *exc: Any) -> Any:
                return await context.__aexit__(*exc)

        return _Recording()


def diagnose(last: dict[str, Any]) -> str:
    """Turn the provider's raw answer into a hint a person can act on."""
    body = last.get("body")
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if isinstance(error, dict):
        return f"provider said: {str(error.get('message') or error)[:300]}"
    try:
        choice = body["choices"][0]
        content = choice["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        return f"unexpected response shape: {json.dumps(body)[:300]}"
    usage = body.get("usage") or {}
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if choice.get("finish_reason") == "length" or (not content and reasoning):
        return (
            "the answer was cut off by the token limit"
            + (f" after {reasoning} reasoning tokens" if reasoning else "")
            + ". Choose a non-reasoning model for this purpose (REACHY_ISPY_MODEL, REACHY_KIDS_MODEL "
            "or REACHY_AGENT_MODEL)."
        )
    return f"model answered: {str(content)[:300]!r}"


async def run_checks(args: argparse.Namespace) -> int:
    from aiohttp import ClientSession
    from aiohttp.test_utils import TestClient, TestServer

    from companion import hermes_reachy_bridge as bridge_module
    from companion.llm_providers import LLMProviderConfigError, TextModelProvider

    def resolve(name: str) -> str:
        return bridge_module._resolve_secret(name, args.profile)

    try:
        llm = TextModelProvider.from_env(resolve)
    except LLMProviderConfigError as exc:
        print(f"✗ provider settings: {exc}")
        return 1
    key = llm.api_key(resolve)
    print(f"Provider: {llm.label} ({llm.region}) at {llm.base_url}")
    for purpose, model in llm.models.items():
        print(f"  {purpose:5} model: {model}")
    if llm.extras:
        print(f"  extra request fields: {json.dumps(llm.extras)}")
    if not key:
        print(f"✗ no API key: set {' or '.join(llm.key_names)}")
        return 1
    openai_key = resolve("OPENAI_API_KEY")

    results: list[str] = []

    def report(status: str, name: str, detail: str, started: float | None = None) -> None:
        took = f" ({time.monotonic() - started:.1f}s)" if started is not None else ""
        symbol = {"pass": "✓", "fail": "✗", "skip": "·", "warn": "!"}[status]
        print(f"{symbol} {name}{took}: {detail}")
        results.append(status)

    async with ClientSession() as session:
        url = f"{llm.base_url}/models"
        try:
            async with session.get(url, headers={"Authorization": f"Bearer {key}"}) as response:
                listing = await response.json(content_type=None)
                status = response.status
        except Exception as exc:  # noqa: BLE001 - report any network failure plainly
            report("fail", "reach provider", f"{url}: {exc}")
            return 1
        # A tiny request proves the key; some model lists are public and some routers report a bad
        # key as HTTP 500, so the status code alone is not enough.
        probe = llm.request(
            {
                "model": llm.model("agent"),
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_completion_tokens": 16,
            }
        )
        try:
            async with session.post(llm.chat_url, headers={"Authorization": f"Bearer {key}"}, json=probe) as response:
                answer = await response.json(content_type=None)
                probe_status = response.status
        except Exception as exc:  # noqa: BLE001
            report("fail", "reach provider", f"{llm.chat_url}: {exc}")
            return 1
        error = answer.get("error") if isinstance(answer, dict) else None
        error_type = str(error.get("type") or "") if isinstance(error, dict) else ""
        if probe_status in {401, 403} or "auth" in error_type:
            message = str(error.get("message") or "") if isinstance(error, dict) else ""
            report("fail", "API key", f"{llm.label} refused the key (HTTP {probe_status}) {message}".strip())
            return 1
        report("pass", "API key", f"{llm.label} accepted the key")
        if status != 200 or not isinstance(listing, dict) or not isinstance(listing.get("data"), list):
            report("warn", "model list", f"could not read {url} (HTTP {status}); model names not checked")
            listing = None
        models = {item.get("id"): item for item in (listing or {}).get("data", []) if isinstance(item, dict)}
        for purpose, model in llm.models.items() if listing else ():
            info = models.get(model)
            if info is None:
                report("warn", f"{purpose} model listed", f"{model} is not in {url}; check the name")
                continue
            features = set(info.get("supported_features") or [])
            tags = set(info.get("tags") or [])
            notes = []
            if purpose == "agent" and features and "tools" not in features:
                notes.append("does not advertise tool calling")
            if "Reasoning" in tags and purpose in {"ispy", "kids"}:
                notes.append("is a reasoning model; short answers may be cut off")
            if notes:
                report("warn", f"{purpose} model", f"{model} " + " and ".join(notes))
            else:
                providers = ", ".join(info.get("providers") or []) or "listed"
                report("pass", f"{purpose} model", f"{model} ({providers})")

    app = bridge_module.create_app(
        api_key=(bridge_key := secrets.token_urlsafe(24)), hermes_url="http://127.0.0.1:9", llm=llm
    )
    auth = {"Authorization": f"Bearer {bridge_key}", "X-Reachy-Device-Id": DEVICE}
    async with TestClient(TestServer(app)) as client:
        bridge = next(r.handler.__self__ for r in app.router.routes() if r.resource.canonical == "/health")
        recorder = Recorder(bridge.http)
        bridge.http = recorder

        async def check(name: str, run: Callable[[], Awaitable[str]]) -> None:
            if args.only and name.split()[0] not in args.only:
                return
            recorder.last = {}
            started = time.monotonic()
            try:
                report("pass", name, await run(), started)
            except AssertionError as exc:
                report("fail", name, f"{exc} {diagnose(recorder.last)}".strip(), started)
            except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
                detail = getattr(exc, "text", None) or str(exc) or type(exc).__name__
                report("fail", name, f"{detail}. {diagnose(recorder.last)}".strip(), started)

        async def ispy_judge() -> str:
            same = await bridge._judge_ispy_guess("mug", {"object_name": "cup"}, language="en", openai_key="")
            different = await bridge._judge_ispy_guess("banana", {"object_name": "cup"}, language="en", openai_key="")
            assert same is True, "judged 'mug' and 'cup' as different objects"
            assert different is False, "judged 'banana' to be a 'cup'"
            return "strict JSON schema works; 'mug' matches 'cup', 'banana' does not"

        async def ispy_guess() -> str:
            guess = await bridge._guess_player_ispy_object(
                ["it is round", "you can drink from it", "it is on the table"], ["plate"], language="en", openai_key=""
            )
            return f"Reachy guessed {guess!r}"

        async def agent_plan() -> str:
            steps = await bridge._plan_agent_run("What can you do for me?")
            return "planned " + ", ".join(str(step["capability_id"]) for step in steps)

        async def agent_ask() -> str:
            context = {
                "capability_profile": "agent",
                "adult_ui_unlocked": True,
                "kids_mode_active": False,
                "power_mode": "awake",
                "privacy_enabled": True,
                "emergency_stop_active": False,
                "robot_available": True,
                "session_generation": 1,
                "requested_session_generation": 1,
                "explicit_private_intent": False,
                "reachy_status": {"state": "listening", "motors_enabled": True},
            }
            session = await client.post("/v1/agent/session", json={"context": context}, headers=auth)
            assert session.status == 200, f"session setup failed: {await session.text()}"
            asked = await client.post(
                "/v1/agent/ask",
                json={
                    "request_id": "provider-check-" + secrets.token_hex(4),
                    "request": "Check your agent capabilities, then tell me in one sentence what you can do.",
                    "context": context,
                },
                headers=auth,
            )
            assert asked.status == 200, f"HTTP {asked.status}: {await asked.text()}"
            return f"answered {(await asked.json())['text'][:120]!r}"

        async def kids_chat() -> str:
            response = await client.post(
                "/v1/kids/chat",
                json={
                    "input": "Can you tell me one fun fact about owls?",
                    "session_id": "kids-" + secrets.token_hex(16),
                    "profile": {"age_band": "7-9", "activity": "buddy", "language": "en"},
                },
                headers=auth,
            )
            assert response.status == 200, f"HTTP {response.status}: {await response.text()}"
            return f"answered {(await response.json())['text'][:120]!r}"

        async def ispy_select() -> str:
            frame = base64.b64encode(Path(args.photo).read_bytes()).decode("ascii")
            response = await client.post(
                "/v1/kids/ispy/select",
                json={
                    "session_id": "kids-" + secrets.token_hex(16),
                    "age_band": "7-9",
                    "language": "en",
                    "frames_jpeg": [frame] * 5,
                },
                headers=auth,
            )
            assert response.status == 200, f"HTTP {response.status}: {await response.text()}"
            target = (await response.json())["target"]
            return f"picked a {target['colour']} {target['object_name']}"

        await check("ispy judging", ispy_judge)
        await check("ispy guessing", ispy_guess)
        await check("agent planning", agent_plan)
        await check("agent answer", agent_ask)
        if openai_key:
            await check("kids chat", kids_chat)
        else:
            report("skip", "kids chat", "needs OPENAI_API_KEY for Kids Mode moderation")
        if not args.photo:
            report("skip", "ispy target selection", "pass --photo desk.jpg to test vision")
        elif not openai_key:
            report("skip", "ispy target selection", "needs OPENAI_API_KEY for Kids Mode moderation")
        else:
            await check("ispy target selection", ispy_select)

    failed = results.count("fail")
    print(
        f"\n{results.count('pass')} passed, {failed} failed, {results.count('warn')} warnings, "
        f"{results.count('skip')} skipped"
    )
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    import logging

    # The bridge logs provider rejections as warnings; this tool already reports them per check.
    logging.basicConfig(level=logging.ERROR)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", help="Hermes profile whose .env holds the keys, as for the bridge")
    parser.add_argument("--photo", help="a JPEG of a desk or shelf, to also test I Spy target selection")
    parser.add_argument("--only", type=lambda value: set(value.split(",")), help="comma-separated: ispy, agent, kids")
    return asyncio.run(run_checks(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
