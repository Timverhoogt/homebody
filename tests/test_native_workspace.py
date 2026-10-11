"""Native work is separate from a voice lease; fixtures are not live coding evidence."""

import asyncio
from pathlib import Path

import pytest

from companion.native_workspace import NativeWorkspace, TargetConfig, WorkspaceError
from companion.reachy_projects import Project, ProjectCatalog


def configured(tmp_path, *, enabled=True):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "ROADMAP.md").write_text("Next: verify import readiness")
    (root / "result.txt").write_text("actual artifact")
    projects = ProjectCatalog((Project("photo", "Photo", root, Path("ROADMAP.md")),))
    targets = TargetConfig.parse(
        {
            "photo-hermes": {
                "backend": "hermes",
                "url": "http://127.0.0.1:8642",
                "token_env": "TEST_NATIVE_TOKEN",
                "project_id": "photo",
                "session_id": "native-existing",
                "execution_enabled": enabled,
                "verification_commands": [["/usr/bin/true"]],
                "artifacts": ["result.txt"],
            }
        },
        projects,
    )
    return projects, targets


class NativeFixture:
    def __init__(self):
        self.calls = []
        self.release = asyncio.Event()

    async def history(self, target):
        self.calls.append(("history", target.session_id))
        return [{"role": "user", "text": "real previous message"}]

    async def submit(self, target, text, request_id):
        self.calls.append(("submit", target.session_id, text, request_id))
        return "run-exact"

    async def events(self, target, run_id):
        yield {"event": "tool.started", "tool": "terminal", "reasoning": "hidden"}
        await self.release.wait()
        yield {"event": "run.completed", "output": "Ready to test!"}

    async def stop(self, target, run_id):
        self.calls.append(("stop", target.session_id, run_id))
        return "stopping"

    async def close(self):
        pass


def test_no_native_targets_by_default():
    assert TargetConfig.parse({}, ProjectCatalog()) == {}


@pytest.mark.parametrize(
    "change",
    [
        {"project_id": "unknown"},
        {"url": "http://evil.example"},
        {"url": "http://user:secret@localhost"},
        {"backend": "pretend"},
        {"execution_enabled": "true"},
        {"artifacts": ["../escape"]},
        {"verification_commands": [["true"]]},
        {"session_id": "agent:main:main"},
    ],
)
def test_target_config_fail_closed(tmp_path, change):
    projects, targets = configured(tmp_path)
    target = next(iter(targets.values()))
    raw = target.config_dict()
    raw.update(change)
    with pytest.raises(WorkspaceError):
        TargetConfig.parse({"example": raw}, projects)


def test_exact_single_use_approval_and_actual_verification(tmp_path):
    async def scenario():
        projects, targets = configured(tmp_path)
        upstream = NativeFixture()
        manager = NativeWorkspace(projects, targets, {"hermes": upstream}, tmp_path / "state.json")
        await manager.bind("robot", "photo-hermes")
        draft = await manager.prepare("robot", "Implement the discussed contract")
        assert not any(c[0] == "submit" for c in upstream.calls)
        assert draft["session_id"] == "native-existing"
        with pytest.raises(WorkspaceError):
            await manager.approve("other-device", draft["approval_id"])
        result = await manager.approve("robot", draft["approval_id"])
        assert result["status"] == "running"
        with pytest.raises(WorkspaceError):
            await manager.approve("robot", draft["approval_id"])
        assert upstream.calls[-1][1:3] == ("native-existing", draft["native_input"])
        upstream.release.set()
        await manager.wait_idle()
        snapshot = await manager.snapshot("robot")
        assert snapshot["run"]["status"] == "completed"
        assert snapshot["run"]["ready_to_test"] is True
        assert snapshot["run"]["receipt"]["checks"][0]["exit_code"] == 0
        assert snapshot["run"]["receipt"]["artifacts"][0]["sha256"]
        assert "hidden" not in str(snapshot)
        await manager.close()

    asyncio.run(scenario())


def test_disabled_execution_never_submits(tmp_path):
    async def scenario():
        projects, targets = configured(tmp_path, enabled=False)
        upstream = NativeFixture()
        manager = NativeWorkspace(projects, targets, {"hermes": upstream}, tmp_path / "state.json")
        await manager.bind("robot", "photo-hermes")
        with pytest.raises(WorkspaceError):
            await manager.prepare("robot", "Start work")
        assert not any(c[0] == "submit" for c in upstream.calls)
        await manager.close()

    asyncio.run(scenario())


def test_model_prose_is_not_a_verification_receipt(tmp_path):
    async def scenario():
        projects, targets = configured(tmp_path)
        (projects.projects["photo"].root / "result.txt").unlink()
        upstream = NativeFixture()
        manager = NativeWorkspace(projects, targets, {"hermes": upstream}, tmp_path / "state.json")
        await manager.bind("robot", "photo-hermes")
        draft = await manager.prepare("robot", "Implement")
        await manager.approve("robot", draft["approval_id"])
        upstream.release.set()
        await manager.wait_idle()
        snapshot = await manager.snapshot("robot")
        assert snapshot["run"]["ready_to_test"] is False
        assert snapshot["run"]["status"] == "verification_failed"
        await manager.close()

    asyncio.run(scenario())


def test_safety_stop_invalidates_approval_and_does_not_claim_cancelled(tmp_path):
    async def scenario():
        projects, targets = configured(tmp_path)
        upstream = NativeFixture()
        manager = NativeWorkspace(projects, targets, {"hermes": upstream}, tmp_path / "state.json")
        await manager.bind("robot", "photo-hermes")
        draft = await manager.prepare("robot", "Implement")
        await manager.stop("robot", detach=True)
        with pytest.raises(WorkspaceError):
            await manager.approve("robot", draft["approval_id"])
        await manager.bind("robot", "photo-hermes")
        draft = await manager.prepare("robot", "Implement")
        await manager.approve("robot", draft["approval_id"])
        result = await manager.stop("robot")
        assert result["status"] == "stopping"
        assert ("stop", "native-existing", "run-exact") in upstream.calls
        await manager.close()

    asyncio.run(scenario())
