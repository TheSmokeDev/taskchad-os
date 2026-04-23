"""Golden query loader — Phase 2.2.

Reads `golden_queries.json` next to this module. Lightweight — no pydantic,
no schema validation beyond key presence. The JSON is curated by hand and
small enough that strict validation would be more costly than informative.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_GOLDENS_PATH = Path(__file__).resolve().parent / "golden_queries.json"


def load_golden_queries(path: Path | str | None = None) -> list[str]:
    """Return the ordered list of golden query strings.

    Replay uses positional alignment in compare_reports, so preserving
    ordering here is load-bearing.
    """
    p = Path(path) if path else _GOLDENS_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    queries = data.get("queries", [])
    return [q["query"] for q in queries if isinstance(q, dict) and q.get("query")]


def load_goldens_metadata(path: Path | str | None = None) -> dict[str, Any]:
    """Return the full JSON — used by CLI to display version + description."""
    p = Path(path) if path else _GOLDENS_PATH
    return json.loads(p.read_text(encoding="utf-8"))
