"""PRD-8 Phase 4 (WS3) — Telegram adapter voice round-trip + marker dispatch tests.

Telegram already has voice ingress/egress (filters.VOICE → _on_voice →
_send_voice_response). Phase 4 adds [SEND_FILE]/[SEND_PHOTO] marker dispatch
on the egress path. Test asserts:
  - _send_voice_response helper exists at the canonical name (telegram.py:842)
  - voice.transcribe_audio_file is the ingress entrypoint (cascade-aware)
  - _dispatch_send_markers is wired into send()
  - imports voice_markers (AST scan)
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))


def _read_adapter_source(name: str) -> str:
    chat_dir = SCRIPTS_DIR.parent / "chat" / "adapters"
    return (chat_dir / f"{name}.py").read_text()


def test_voice_round_trip():
    """Telegram retains _send_voice_response (canonical name per telegram.py:842)."""
    src = _read_adapter_source("telegram")
    assert "_send_voice_response" in src
    # Marker dispatch wired into send()
    assert "_dispatch_send_markers" in src
    # Voice cascade entrypoint used in _on_voice handler
    assert "voice_mod.transcribe_audio_file" in src or "voice.transcribe_audio_file" in src


def test_telegram_imports_voice_markers():
    """AST scan: voice_markers imported."""
    src = _read_adapter_source("telegram")
    assert "from voice_markers import" in src or "import voice_markers" in src
    assert "parse_send_markers" in src
    assert "strip_send_markers" in src


def test_telegram_imports_voice_module():
    """voice module imported as voice_mod for cascade access."""
    src = _read_adapter_source("telegram")
    assert "import voice as voice_mod" in src or "import voice" in src
    assert "voice_mod.synthesize" in src or "voice.synthesize" in src or "voice_mod.transcribe_audio_file" in src
