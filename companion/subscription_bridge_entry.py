"""Launch the subscription-capable bridge in the installed Hermes managed runtime.

The ordinary bridge may use an older interpreter; importing its live SDK there
could re-exec halfway through a request. Bootstrap before any server imports.
HOMEBODY_HERMES_SOURCE is trusted host configuration, never a request parameter.
"""
import os
import sys
from pathlib import Path

source = Path(os.environ["HOMEBODY_HERMES_SOURCE"]).resolve()
if not (source / "hermes_bootstrap.py").is_file():
    raise RuntimeError("Installed Hermes bootstrap is unavailable")
sys.path.insert(0, str(source))
import hermes_bootstrap  # noqa: E402, F401

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hermes_reachy_bridge import main  # noqa: E402

if __name__ == "__main__":
    main()
