"""Start the The Homie Dashboard server."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Add scripts dir for config imports
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", "4321"))
    print(f"Starting dashboard at http://localhost:{port}")
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=port,
        app_dir=str(Path(__file__).parent),
        reload=False,
    )
