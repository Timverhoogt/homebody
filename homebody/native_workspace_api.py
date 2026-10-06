"""Owner-only native Workspace proxy; upstream credentials never reach the browser."""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


class NativeWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target_id: str | None = Field(default=None, max_length=48)
    text: str | None = Field(default=None, max_length=4000)
    approval_id: str | None = Field(default=None, max_length=128)


def install_native_workspace_routes(app, runtime_getter, config_loader, client_factory):
    @app.post("/api/agent/workspace/native/{action}")
    def native_workspace_action(action: str, request: NativeWorkspaceRequest):
        runtime = runtime_getter()  # actual paired-owner check, never the legacy adult header
        display_generation = runtime._workspace_lease()
        if display_generation is None:
            raise HTTPException(status_code=423, detail="Show the conversation before opening a native project")
        fields = {
            "read": set(),
            "show": set(),
            "bind": {"target_id"},
            "prepare": {"text"},
            "approve": {"approval_id"},
            "cancel": set(),
        }
        if action not in fields:
            raise HTTPException(status_code=404, detail="Unknown native Workspace action")
        values = request.model_dump(exclude_none=True)
        if set(values) != fields[action]:
            raise HTTPException(status_code=422, detail="Native action fields do not match the exact request")
        context = runtime.agent_broker_context(explicit_private_intent=True)
        if context.capability_profile != "agent" or context.kids_mode_active:
            raise HTTPException(status_code=423, detail="Select the adult Agent profile to open a native project")
        try:
            client = client_factory(config_loader())
            try:
                client.establish_agent_session(
                    context,
                    preserve_native=runtime._agent_native_preserved_generation == context.session_generation,
                )
                payload = client.native_workspace_action(action, context, **values)
            finally:
                client.close()
            if (
                not runtime.agent_session_is_current(context.session_generation)
                or runtime._workspace_lease() != display_generation
            ):
                raise HTTPException(status_code=423, detail="Native Workspace became stale")
            return JSONResponse(payload, headers={"Cache-Control": "no-store"})
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail="Native Workspace is unavailable; work outcome is unconfirmed"
            ) from exc
