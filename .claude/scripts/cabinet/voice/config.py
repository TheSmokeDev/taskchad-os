"""Cabinet voice config — port of ClaudeClaw warroom/config.py.

VERBATIM port of upstream ``get_project_root()`` (lines 14-25). The
``load_voices()`` reader (upstream lines 33-46) is NOT ported per Q5 lock —
voice ids live in ``<profile>/config.yaml.cabinet.voice_id`` directly
(per-persona voice via config.yaml.cabinet.voice_id; Q5 lock — no separate
voices.json file). The voice subprocess reads voices through
:mod:`personas.services.load_persona_config`.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

# Default WebSocket port — matches ClaudeClaw WARROOM_PORT default verbatim.
DEFAULT_VOICE_PORT: int = 7860

# Default WebSocket bind — Phase 7a default-bind-loopback rule. Set
# CABINET_VOICE_BIND=0.0.0.0 to expose on LAN (operator opt-in).
DEFAULT_VOICE_BIND: str = "127.0.0.1"

# Phase 6 wire-id translation site (Q4 lock): ClaudeClaw's voice subprocess
# emits "main" in RTVI server-message frames + AgentRouteFrame, the Homie
# orchestrator stores the persona under id "default". The translation runs
# at the persona-config lookup boundary (see voice_router.py and
# personas.get_persona).
DEFAULT_AGENT_WIRE: str = "main"
DEFAULT_AGENT_INTERNAL: str = "default"


def get_project_root() -> Path:
    """VERBATIM port of warroom/config.py:14-25 — resolve project root.

    Tries ``git rev-parse --show-toplevel`` first; falls back to two parent
    levels from this file (cabinet/voice/config.py -> .claude/scripts ->
    .claude -> repo root).
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).parent,
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Fallback: cabinet/voice/config.py sits 4 dirs below repo root
        # (.claude/scripts/cabinet/voice/config.py -> repo root).
        return Path(__file__).resolve().parents[4]


PROJECT_ROOT: Path = get_project_root()


def voice_port() -> int:
    """Return the configured cabinet voice WebSocket port (Rule 1 — resolve at call time)."""
    raw = os.environ.get("CABINET_VOICE_PORT", "")
    if not raw:
        return DEFAULT_VOICE_PORT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_VOICE_PORT


def voice_bind() -> str:
    """Return the configured cabinet voice WebSocket bind host.

    Phase 7a default-bind-loopback: defaults to ``127.0.0.1``. Set
    ``CABINET_VOICE_BIND=0.0.0.0`` for explicit LAN exposure (operator opt-in).
    Rule 1 — resolve at call time, never bind a host string at import time.
    """
    return os.environ.get("CABINET_VOICE_BIND", DEFAULT_VOICE_BIND) or DEFAULT_VOICE_BIND


# File-IPC paths — renamed per Translation Boundary Audit:
#   /tmp/warroom-agents.json -> <tmp>/cabinet-roster.json
#   /tmp/warroom-pin.json    -> <tmp>/cabinet-voice-pin.json
#
# PRD-8 Phase 6 v2 fix-pass 2026-05-10 (M1 fix) — replace hard-coded
# `/tmp` (POSIX-only) with `tempfile.gettempdir()` so the file-IPC paths
# resolve correctly on Windows operator hosts (where /tmp is not a
# valid directory). Operators can still override via the env vars.
_DEFAULT_TMP: Path = Path(tempfile.gettempdir())
ROSTER_PATH: Path = Path(
    os.environ.get("CABINET_VOICE_ROSTER_PATH")
    or str(_DEFAULT_TMP / "cabinet-roster.json")
)
PIN_PATH: Path = Path(
    os.environ.get("CABINET_VOICE_PIN_PATH")
    or str(_DEFAULT_TMP / "cabinet-voice-pin.json")
)
