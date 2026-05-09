"""PRD-8 Phase 4 — CLI adapter --voice / --voice-out flags + marker dispatch."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))


def _read_adapter_source() -> str:
    chat_dir = SCRIPTS_DIR.parent / "chat" / "adapters"
    return (chat_dir / "cli_adapter.py").read_text()


def test_voice_flag_input():
    """CLI accepts voice_path arg, calls cascade transcribe."""
    src = _read_adapter_source()
    assert "voice_path" in src
    assert "_voice_path" in src
    assert "voice_mod.transcribe_audio_file" in src or "voice.transcribe_audio_file" in src


def test_voice_out_flag():
    """CLI accepts voice_out_path arg, writes synthesize() result."""
    src = _read_adapter_source()
    assert "voice_out_path" in src
    assert "_voice_out_path" in src
    assert "voice_mod.synthesize" in src or "voice.synthesize" in src


def test_marker_dispatch_cli():
    """CLI marker dispatch emits path lines."""
    src = _read_adapter_source()
    assert "parse_send_markers" in src
    assert "strip_send_markers" in src


def test_cli_imports_voice_markers():
    src = _read_adapter_source()
    assert "from voice_markers import" in src
    assert "import voice as voice_mod" in src


def test_cli_adapter_constructor_accepts_voice_args():
    """CLIAdapter() constructor takes voice_path and voice_out_path."""
    import inspect
    from adapters.cli_adapter import CLIAdapter

    sig = inspect.signature(CLIAdapter.__init__)
    assert "voice_path" in sig.parameters
    assert "voice_out_path" in sig.parameters
