"""Start the orchestration control API server."""

import uvicorn

from orchestration.api import API_HOST, API_PORT, app
from orchestration.observability import init_orchestration_observability

if __name__ == "__main__":
    init_orchestration_observability()
    uvicorn.run(app, host=API_HOST, port=API_PORT)
