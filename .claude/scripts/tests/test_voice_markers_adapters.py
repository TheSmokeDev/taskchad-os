"""PRD-8 Phase 4 — all 6 adapters consume voice_markers.parse_send_markers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))


_ADAPTERS = ("telegram", "discord", "slack", "whatsapp", "web", "cli_adapter")


def _read(name: str) -> str:
    return (SCRIPTS_DIR.parent / "chat" / "adapters" / f"{name}.py").read_text()


@pytest.mark.parametrize("adapter", _ADAPTERS)
def test_all_adapters_use_marker_parser(adapter):
    """AST scan: each adapter imports and uses parse_send_markers."""
    src = _read(adapter)
    assert "from voice_markers import" in src
    assert "parse_send_markers" in src
    assert "strip_send_markers" in src
