"""Independent process sandbox exercising the production state machine and pip slot installer."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from homebody.update_worker import SystemdSlots, run_job
from homebody.updates import Store


class Sandbox(SystemdSlots):
    def preflight(self):
        pass

    def stop(self):
        pass

    def start(self):
        pass

    def healthy(self, identity, deadline=90):
        if identity["version"] == "0.4.0":
            return (self.current / "retained.txt").exists()
        env = {"PYTHONPATH": str(self.current)}
        result = subprocess.run([sys.executable, "-m", "homebody.main"], env=env, cwd=self.slots,
                                capture_output=True, text=True)
        actual = json.loads((self.current / "homebody/release.json").read_text())
        return result.returncode == 0 and all(actual[k] == identity[k] for k in actual)


state, current, slots, artifact = map(Path, sys.argv[1:])
driver = Sandbox({"current": str(current), "slots": str(slots), "python": sys.executable})
run_job(Store(state), driver, download=lambda dest: shutil.copyfile(artifact, dest))
