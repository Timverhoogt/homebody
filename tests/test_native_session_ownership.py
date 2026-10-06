import asyncio

import pytest
from test_native_workspace import NativeFixture, configured

from companion.native_workspace import NativeWorkspace, TargetConfig, WorkspaceError


def test_detached_unconfirmed_work_still_owns_native_session(tmp_path):
    class LostStop(NativeFixture):
        async def stop(self, target, run_id):
            raise OSError("simulated lost stop acknowledgement")

    async def scenario():
        projects, targets = configured(tmp_path)
        native = LostStop()
        manager = NativeWorkspace(projects, targets, {"hermes": native}, tmp_path / "state.json")
        await manager.bind("robot-one", "photo-hermes")
        draft = await manager.prepare("robot-one", "Implement the contract")
        await manager.approve("robot-one", draft["approval_id"])
        await manager.stop("robot-one", detach=True)
        with pytest.raises(WorkspaceError, match="another device"):
            await manager.bind("robot-two", "photo-hermes")
        native.release.set()
        await manager.wait_idle()
        await manager.close()

    asyncio.run(scenario())


def test_same_native_identity_cannot_have_two_target_aliases(tmp_path):
    projects, targets = configured(tmp_path)
    raw = targets["photo-hermes"].config_dict()
    with pytest.raises(WorkspaceError, match="duplicate native session"):
        TargetConfig.parse({"photo-hermes": raw, "another-name": raw}, projects)
