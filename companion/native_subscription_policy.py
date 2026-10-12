"""Model/session policy for the dedicated native Hermes Workspace listener."""


def locked_workspace_options(options, *, session_id, model):
    if options.get("session_id") != session_id:
        raise ValueError("This Workspace listener only runs its registered native session")
    if options.get("requested_model") not in {None, model}:
        raise ValueError("Workspace model is host-locked")
    if options.get("requested_provider") not in {None, "openai-codex"}:
        raise ValueError("Workspace provider is subscription-only")
    return {
        **options,
        "requested_model": model,
        "requested_provider": "openai-codex",
        "confirmed_runtime_lock": True,
        "model_options": {"reasoning": {"enabled": False, "effort": "none"}},
    }
