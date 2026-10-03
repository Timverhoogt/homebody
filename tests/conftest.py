import sys
from pathlib import Path
from types import ModuleType

from fastapi import FastAPI

# The bridge tests import ``companion.*`` from the checkout. The editable install maps that folder to
# ``homebody.bridge`` through an import hook, which does not put the repository root on sys.path.
_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Mock the entire reachy_mini SDK and its submodules so we don't need
# GStreamer or native libraries to run unit tests.
reachy_module = ModuleType("reachy_mini")
utils_module = ModuleType("reachy_mini.utils")
utils_module.create_head_pose = lambda **kwargs: kwargs

utils_module.interpolation = ModuleType("reachy_mini.utils.interpolation")
utils_module.interpolation.linear_pose_interpolation = lambda *args, **kwargs: []

motion_module = ModuleType("reachy_mini.motion")
motion_module.recorded_move = ModuleType("reachy_mini.motion.recorded_move")
class FakeRecordedMoves:
    def __init__(self, *args, **kwargs):
        pass
motion_module.recorded_move.RecordedMoves = FakeRecordedMoves

class FakeReachyMini:
    def __init__(self, *args, **kwargs):
        pass

class FakeReachyMiniApp:
    def __init__(self, *args, **kwargs):
        self.settings_app = FastAPI()

reachy_module.ReachyMini = FakeReachyMini
reachy_module.ReachyMiniApp = FakeReachyMiniApp

sys.modules["reachy_mini"] = reachy_module
sys.modules["reachy_mini.utils"] = utils_module
sys.modules["reachy_mini.utils.interpolation"] = utils_module.interpolation
sys.modules["reachy_mini.motion"] = motion_module
sys.modules["reachy_mini.motion.recorded_move"] = motion_module.recorded_move
