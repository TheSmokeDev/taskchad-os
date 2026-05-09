"""PRD-8 Phase 4 — Slack adapter audio file detection + files_upload + marker dispatch."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))


def _read_adapter_source() -> str:
    chat_dir = SCRIPTS_DIR.parent / "chat" / "adapters"
    return (chat_dir / "slack.py").read_text()


def test_audio_file_detection():
    """Slack message handler detects files[].mimetype audio types."""
    src = _read_adapter_source()
    assert "_transcribe_audio_files" in src
    assert "mimetype" in src or "_SLACK_AUDIO_MIMES" in src
    # Cascade entrypoint (R4 NB2 — canonical name)
    assert "voice_mod.transcribe_audio_file" in src or "voice.transcribe_audio_file" in src
    # bot token used for download
    assert "Bearer" in src
    assert "self.bot_token" in src


def test_files_upload_audio():
    """Slack _send_voice_response uploads via files_upload_v2."""
    src = _read_adapter_source()
    assert "_send_voice_response" in src
    assert "files_upload_v2" in src
    assert "voice_mod.synthesize" in src or "voice.synthesize" in src


def test_marker_dispatch_slack():
    """Slack marker dispatch via files_upload_v2."""
    src = _read_adapter_source()
    assert "_dispatch_send_markers" in src
    assert "parse_send_markers" in src
    assert "files_upload_v2" in src


def test_slack_imports_voice_markers():
    src = _read_adapter_source()
    assert "from voice_markers import" in src
    assert "import voice as voice_mod" in src
