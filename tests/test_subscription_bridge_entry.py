"""Runtime bootstrap can re-exec, but must not interpret/discard bridge profile args."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_bridge_args_survive_bootstrap_reexec(tmp_path):
    source = tmp_path / "sdk"
    source.mkdir()
    (source / "hermes_bootstrap.py").write_text(
        'import os,sys\n'
        'assert len(sys.argv)==1, "bridge flags reached the Hermes bootstrap"\n'
        'if not os.environ.get("FIXTURE_REEXEC"):\n'
        ' os.environ["FIXTURE_REEXEC"]="1"\n'
        ' os.execv(sys.executable,[sys.executable,sys.argv[0]])\n'
    )
    entry = tmp_path / "entry.py"
    shutil.copy2(Path("companion/subscription_bridge_entry.py"), entry)
    (tmp_path / "hermes_reachy_bridge.py").write_text(
        'import os,sys,json\n'
        'def main():\n'
        ' assert "HOMEBODY_BOOTSTRAP_BRIDGE_ARGS" not in os.environ\n'
        ' print(json.dumps(sys.argv[1:]))\n'
    )
    args = ["--profile", "reachy", "--host", "127.0.0.1", "--port", "8643"]
    env = {**os.environ, "HOMEBODY_HERMES_SOURCE": str(source)}
    env.pop("HOMEBODY_BOOTSTRAP_BRIDGE_ARGS", None)
    env.pop("FIXTURE_REEXEC", None)
    result = subprocess.run([sys.executable, str(entry), *args], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == args
