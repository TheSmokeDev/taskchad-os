"""Homie Evolve — self-improvement loop for framework tuning.

ASI-Evolve inspired. Phase 2 scope: recall-only replay harness.
See: PRDs/homie-evolve-recall.md (planned)

Entry points:
- run_replay(queries, overrides, memory_dir) → ReplayReport
- override_config(**kwargs) → contextmanager for safe param injection
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure scripts/ and chat/ on path. scripts/ holds config + memory_search;
# chat/ holds recall_service. Both are dependencies of the replay harness
# and must resolve regardless of how evolve is invoked (module, CLI, test).
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (_SCRIPTS_DIR, _CHAT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evolve.compare import (  # noqa: E402
    QueryDelta,
    ReportDelta,
    compare_reports,
    format_delta_table,
)
from evolve.config_override import override_config  # noqa: E402
from evolve.goldens import load_golden_queries, load_goldens_metadata  # noqa: E402
from evolve.models import (  # noqa: E402
    ReplayQueryResult,
    ReplayReport,
    ReplaySummary,
)
from evolve.replay import run_replay, run_replay_sync, write_report  # noqa: E402

__all__ = [
    "override_config",
    "run_replay",
    "run_replay_sync",
    "write_report",
    "ReplayQueryResult",
    "ReplayReport",
    "ReplaySummary",
    "QueryDelta",
    "ReportDelta",
    "compare_reports",
    "format_delta_table",
    "load_golden_queries",
    "load_goldens_metadata",
]
