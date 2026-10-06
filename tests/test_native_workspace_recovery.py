import asyncio
import json

import pytest
from test_native_workspace import NativeFixture, configured

from companion.native_workspace import NativeWorkspace, TargetConfig, WorkspaceError


def test_target_roundtrip_and_dedicated_session_on_main_agent(tmp_path):
    projects, targets = configured(tmp_path)
    raw = targets["photo-hermes"].config_dict()
    assert TargetConfig.parse({"roundtrip": raw}, projects)["roundtrip"].project_root == str(tmp_path / "repo")
    raw.update(backend="openclaw", url="ws://127.0.0.1:18789", session_id="agent:main:homebody-photo")
    assert not TargetConfig.parse({"dedicated": raw}, projects)["dedicated"].allow_primary


def test_approval_expiry_source_drift_and_matching_discussion(tmp_path):
    async def scenario():
        projects, targets = configured(tmp_path)
        clock = [0]
        native = NativeFixture()
        manager = NativeWorkspace(
            projects, targets, {"hermes": native}, tmp_path / "state.json", clock=lambda: clock[0]
        )
        await manager.bind("robot", "photo-hermes")
        draft = await manager.prepare(
            "robot",
            "Implement this",
            discussion=[
                {"role": "assistant", "content": "The discussed item is the import-readiness contract."},
                {"role": "tool", "content": "Never carry generic tool results"},
            ],
        )
        assert "import-readiness contract" in draft["native_input"]
        assert "generic tool results" not in draft["native_input"]
        assert draft["source"]["source_sha256"]
        assert draft["source"]["text"].startswith("1|")
        assert "import-readiness contract" not in manager.journal.read_text()
        clock[0] = 121
        with pytest.raises(WorkspaceError, match="expired"):
            await manager.approve("robot", draft["approval_id"])
        draft = await manager.prepare("robot", "Implement the newly discussed item")
        (tmp_path / "repo" / "ROADMAP.md").write_text("# Changed after scope review\n")
        with pytest.raises(WorkspaceError, match="roadmap changed"):
            await manager.approve("robot", draft["approval_id"])
        assert not any(c[0] == "submit" for c in native.calls)
        await manager.close()

    asyncio.run(scenario())


def test_restart_reconnects_same_run_without_resubmission_or_private_journal(tmp_path):
    async def scenario():
        projects, targets = configured(tmp_path)
        native = NativeFixture()
        journal = tmp_path / "state.json"
        first = NativeWorkspace(projects, targets, {"hermes": native}, journal)
        await first.bind("robot", "photo-hermes")
        draft = await first.prepare("robot", "Private exact request; do not journal this body")
        await first.approve("robot", draft["approval_id"])
        await first.close()
        assert "Private exact request" not in journal.read_text()
        assert "real previous message" not in journal.read_text()
        assert journal.stat().st_mode & 0o777 == 0o600
        second = NativeWorkspace(projects, targets, {"hermes": native}, journal)
        assert (await second.snapshot("robot"))["messages"] == []
        native.release.set()
        await second.resume()
        await second.wait_idle()
        state = await second.show("robot")
        assert state["run"]["run_id"] == "run-exact"
        assert state["run"]["ready_to_test"] is True
        assert state["run"]["receipt"]["native_session_id"] == "native-existing"
        assert state["run"]["receipt"]["native_run_id"] == "run-exact"
        assert len([c for c in native.calls if c[0] == "submit"]) == 1
        await second.close()

    asyncio.run(scenario())


def test_lost_admission_remains_unknown_and_is_never_replayed(tmp_path):
    class LostAdmission(NativeFixture):
        async def submit(self, target, text, request_id):
            await super().submit(target, text, request_id)
            raise TimeoutError("simulated lost acknowledgement")

    async def scenario():
        projects, targets = configured(tmp_path)
        native = LostAdmission()
        journal = tmp_path / "state.json"
        manager = NativeWorkspace(projects, targets, {"hermes": native}, journal)
        await manager.bind("robot", "photo-hermes")
        draft = await manager.prepare("robot", "Implement the contract")
        run = await manager.approve("robot", draft["approval_id"])
        await manager.wait_idle()
        assert run["status"] == "submission_unknown"
        assert run["ready_to_test"] is False
        await manager.close()
        restored = NativeWorkspace(projects, targets, {"hermes": native}, journal)
        await restored.resume()
        await restored.snapshot("robot")
        assert len([c for c in native.calls if c[0] == "submit"]) == 1
        assert json.loads(journal.read_text())["bindings"]["robot"]["run"]["status"] == "submission_unknown"
        with pytest.raises(WorkspaceError, match="reconcile"):
            await restored.bind("robot", "photo-hermes")
        await restored.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("stop_first", [False, True])
def test_reconnect_is_observation_only_and_lost_stop_never_becomes_ready(tmp_path, stop_first):
    class LostConnection(NativeFixture):
        first_stream = True

        async def events(self, target, run_id):
            if self.first_stream:
                self.first_stream = False
                raise OSError("simulated disconnect")
            yield {"event": "run.completed", "output": "worker says Ready to test"}

        async def stop(self, target, run_id):
            await super().stop(target, run_id)
            raise OSError("simulated lost stop acknowledgement")

    async def scenario():
        projects, targets = configured(tmp_path)
        native = LostConnection()
        manager = NativeWorkspace(projects, targets, {"hermes": native}, tmp_path / "state.json")
        await manager.bind("robot", "photo-hermes")
        draft = await manager.prepare("robot", "Implement the contract")
        await manager.approve("robot", draft["approval_id"])
        await manager.wait_idle()
        assert manager.bindings["robot"]["run"]["status"] == "disconnected"
        if stop_first:
            await manager.stop("robot")
        await manager.snapshot("robot")
        await manager.wait_idle()
        state = await manager.snapshot("robot")
        assert state["run"]["ready_to_test"] is (not stop_first)
        assert len([c for c in native.calls if c[0] == "submit"]) == 1
        await manager.close()

    asyncio.run(scenario())


def test_native_history_expiring_while_in_flight_is_not_returned(tmp_path):
    async def scenario():
        projects, targets = configured(tmp_path)
        clock = [0]
        native = NativeFixture()
        manager = NativeWorkspace(
            projects, targets, {"hermes": native}, tmp_path / "state.json", clock=lambda: clock[0]
        )
        await manager.bind("robot", "photo-hermes")
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_history(_target):
            started.set()
            await release.wait()
            return [{"role": "assistant", "text": "late private native history"}]

        native.history = slow_history
        task = asyncio.create_task(manager.snapshot("robot"))
        await started.wait()
        clock[0] = 3601
        release.set()
        result = await task
        assert result["messages"] == [] and result["events"] == []
        await manager.close()

    asyncio.run(scenario())
