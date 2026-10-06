"""Authenticated native project routes, separate from bounded home-action plans."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from aiohttp import web

try:
    from companion.native_adapters import HermesNativeAdapter, OpenClawNativeAdapter
    from companion.native_workspace import NativeWorkspace, TargetConfig, WorkspaceError
    from companion.reachy_agent_broker import BrokerContext, BrokerValidationError, new_request_id
except ModuleNotFoundError:
    from native_adapters import HermesNativeAdapter, OpenClawNativeAdapter
    from native_workspace import NativeWorkspace, TargetConfig, WorkspaceError
    from reachy_agent_broker import BrokerContext, BrokerValidationError, new_request_id


class NativeWorkspaceRoutes:
    def init_native_workspace(self):
        self.native_workspace = None

    async def start_native_workspace(self, secret, home):
        raw = os.getenv("REACHY_NATIVE_TARGETS_JSON", "").strip()
        if not raw:
            return
        projects = self.agent_broker.config.projects
        targets = TargetConfig.parse(json.loads(raw), projects)
        self.native_workspace = NativeWorkspace(
            projects,
            targets,
            {"hermes": HermesNativeAdapter(self.http, secret), "openclaw": OpenClawNativeAdapter(self.http, secret)},
            Path(home) / "homebody-native-workspace.json",
        )
        await self.native_workspace.resume()

    async def stop_native_workspace(self):
        if self.native_workspace is not None:
            # Backend work remains native/durable across a bridge process restart.
            await self.native_workspace.close()

    async def seed_native_project_conversation(self, device, context):
        manager = self.native_workspace
        binding = manager.bindings.get(device) if manager is not None else None
        if not binding or binding.get("detached"):
            return
        async with self.agent_broker.project_conversation(device, context) as lease:
            if not lease.selected_project_id:
                lease.selected_project_id = manager._target(binding).project_id
                self.agent_broker.refresh_project_conversation(lease)

    async def invalidate_native_workspace(self, device, *, preserve=False):
        if self.native_workspace is not None and not preserve:
            await self.native_workspace.stop(device, detach=True)

    async def native_workspace_action(self, request):
        self.require_auth(request)
        device = request.headers.get("X-Reachy-Device-Id", "")
        if not device or len(device) > 96:
            raise web.HTTPForbidden(text="Native Workspace requires a device identity")
        manager = self.native_workspace
        action = request.match_info["action"]
        if manager is None:
            if action in {"read", "stop", "show"}:
                return web.json_response(
                    {"targets": [], "binding": None, "messages": [], "events": [], "run": {}, "pending": None},
                    headers={"Cache-Control": "no-store"},
                )
            raise web.HTTPConflict(text="Native project sessions are not configured; no work has started")
        if action == "stop":
            # Stopping is safe even after the generation/profile has changed or Kids has latched.
            await manager.stop(device, detach=True)
            return web.json_response({"ok": True}, headers={"Cache-Control": "no-store"})
        request_id = new_request_id()
        context = None
        try:
            payload = await request.json()
            fields = {
                "read": set(),
                "show": set(),
                "bind": {"target_id"},
                "prepare": {"text"},
                "approve": {"approval_id"},
                "cancel": set(),
            }
            if action not in fields or not isinstance(payload, dict) or set(payload) != fields[action] | {"context"}:
                raise WorkspaceError("invalid native Workspace action fields")
            context = await self.agent_broker.register_request(device, payload["context"], request_id)
            self.agent_broker.authorize_context(context)
            if action == "bind":
                result = await manager.bind(device, payload["target_id"])
                async with self.agent_broker.project_conversation(device, context) as lease:
                    project = manager.targets[payload["target_id"]].project_id
                    if lease.selected_project_id != project:
                        self.agent_broker.clear_project_conversation(lease)
                    lease.selected_project_id = project
                    self.agent_broker.refresh_project_conversation(lease)
            elif action == "prepare":
                async with self.agent_broker.project_conversation(device, context) as lease:
                    selected = manager._target(manager._binding(device)).project_id
                    discussion = list(lease.project_dialogue) if lease.selected_project_id == selected else []
                    result = await manager.prepare(device, payload["text"], discussion=discussion)
            elif action == "approve":
                result = await manager.approve(device, payload["approval_id"])
            elif action == "cancel":
                result = await manager.stop(device)
            elif action == "show":
                result = await manager.show(device)
            else:
                result = await manager.snapshot(device)
            await self.agent_broker.assert_current(device, context.session_generation)
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        except asyncio.CancelledError:
            # A launch may already have been admitted. Preserve its stop control, not false success.
            if action in {"approve", "bind"}:
                await asyncio.shield(manager.stop(device, detach=True))
            raise
        except BrokerValidationError as exc:
            raise web.HTTPForbidden(text=str(exc)) from exc
        except (WorkspaceError, TypeError) as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        finally:
            if context is not None:
                await self.agent_broker.unregister_request(device, context.session_generation, request_id)

    async def native_voice_proposal(self, device, context, text):
        """Voice can propose exact work, not grant consent or arbitrary session selection."""
        manager = self.native_workspace
        if manager is None or device not in manager.bindings or manager.bindings[device].get("detached"):
            return None
        parsed = BrokerContext.parse(context)
        self.agent_broker.authorize_context(parsed)
        if re.fullmatch(
            r"(?i)\s*(?:is it (?:ready|done)|ready (?:to test|yet)|"
            r"what(?:[’\']s| is) (?:the )?(?:work|run|project) status|"
            r"how is (?:it|the work) going|ben je klaar|hoe gaat het)\??\s*",
            text,
        ):
            run = manager.bindings[device].get("run", {})
            receipt = run.get("receipt") or {}
            if (
                run.get("status") == "completed"
                and run.get("ready_to_test") is True
                and receipt.get("verified") is True
                and run.get("run_id")
                and receipt.get("native_run_id") == run.get("run_id")
                and receipt.get("native_session_id") == manager._target(manager.bindings[device]).session_id
            ):
                return (
                    "Ready to test: the fixed checks passed and artifact hashes are recorded in Workspace. "
                    "Owner testing is still needed."
                )
            return (
                f"Native project work from this surface is {run.get('status', 'not submitted')}. "
                "It is not verified ready to test. Workspace has the actual run details."
            )
        if not re.search(
            r"(?i)^\s*(?:(?:can|could|would|will) you )?"
            r"(?:implement|build|fix|refactor|work on|let[’\']?s (?:work|do|build|implement)|"
            r"do (?:it|this|that|the)|go ahead|run (?:the tests|tests|checks|the build)|"
            r"implementeer|bouw|werk aan|laten we|ga aan de slag)\b",
            text,
        ):
            return None  # Companion/home requests and roadmap discussion stay on the bounded path.
        try:
            async with self.agent_broker.project_conversation(device, parsed) as lease:
                selected = manager._target(manager._binding(device)).project_id
                if lease.selected_project_id and lease.selected_project_id != selected:
                    return (
                        "The discussed project differs from the selected native session. "
                        "Select the matching project in Workspace. No work has started."
                    )
                discussion = list(lease.project_dialogue) if lease.selected_project_id == selected else []
                await manager.prepare(device, text, discussion=discussion)
        except WorkspaceError as exc:
            return str(exc)
        return (
            "I’ve prepared this request for the selected project session. "
            "Review the exact scope in Workspace to approve it. No work has started."
        )
