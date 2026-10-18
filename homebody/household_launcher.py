"""Launch the packaged standalone companion without importing the robot or Hermes runtime."""
import runpy
import sys
from pathlib import Path


def main():
    here = Path(__file__).resolve().parent
    companion = here / "bridge"
    if not companion.is_dir():  # Source checkout layout.
        companion = here.parent / "companion"
    # Shared companion modules support direct-script imports. Keep that layout in
    # the wheel rather than depending on a separately installed `companion` package.
    sys.path.insert(0, str(companion))
    try:
        runpy.run_path(str(companion / "household_server.py"), run_name="__main__")
    finally:
        sys.path.remove(str(companion))
