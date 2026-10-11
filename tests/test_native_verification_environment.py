import asyncio
import sys
from dataclasses import replace

from test_native_workspace import NativeFixture, configured

from companion.native_workspace import NativeWorkspace


def test_fixed_verifiers_do_not_inherit_bridge_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_NATIVE_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("BRIDGE_DLP_SENTINEL", "synthetic-test-token")

    async def scenario():
        projects, targets = configured(tmp_path)
        target = targets["photo-hermes"]
        check = (
            "import os; assert not any(k in os.environ for k in "
            "('TEST_NATIVE_TOKEN','OPENCLAW_GATEWAY_TOKEN','BRIDGE_DLP_SENTINEL')); "
            "assert os.environ.get('HOME') == os.getcwd()"
        )
        targets["photo-hermes"] = replace(target, verification_commands=((sys.executable, "-c", check),))
        native = NativeFixture()
        native.release.set()
        manager = NativeWorkspace(projects, targets, {"hermes": native}, tmp_path / "state.json")
        await manager.bind("robot", "photo-hermes")
        draft = await manager.prepare("robot", "Run the registered acceptance checks")
        await manager.approve("robot", draft["approval_id"])
        await manager.wait_idle()
        assert manager.bindings["robot"]["run"]["ready_to_test"] is True
        await manager.close()

    asyncio.run(scenario())
