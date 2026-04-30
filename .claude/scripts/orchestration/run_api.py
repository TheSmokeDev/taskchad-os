"""Start the orchestration control API server."""

import sys
from pathlib import Path

# Ensure scripts dir is on sys.path so personas + framework modules import
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

import uvicorn  # noqa: E402

from orchestration.api import API_HOST, API_PORT, app  # noqa: E402
from orchestration.observability import init_orchestration_observability  # noqa: E402

if __name__ == "__main__":
    init_orchestration_observability()
    uvicorn.run(app, host=API_HOST, port=API_PORT)
