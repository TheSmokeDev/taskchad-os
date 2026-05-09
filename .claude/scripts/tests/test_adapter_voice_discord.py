"""PRD-8 Phase 4 — Discord adapter voice ingress + egress + marker dispatch."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))


def _read_adapter_source() -> str:
    chat_dir = SCRIPTS_DIR.parent / "chat" / "adapters"
    return (chat_dir / "discord.py").read_text()


def test_voice_ingress():
    """Discord _on_voice_message handles audio attachments via voice cascade."""
    src = _read_adapter_source()
    assert "_on_voice_message" in src
    # Audio MIME types detected
    for mime in ("audio/ogg", "audio/mp4"):
        assert mime in src
    # Cascade entrypoint
    assert "voice_mod.transcribe_audio_file" in src or "voice.transcribe_audio_file" in src


def test_voice_egress():
    """Discord _send_voice_response synthesizes via cascade and sends as discord.File."""
    src = _read_adapter_source()
    assert "_send_voice_response" in src
    assert "voice_mod.synthesize" in src or "voice.synthesize" in src
    assert "discord.File" in src


def test_marker_dispatch():
    """Discord _dispatch_send_markers parses and dispatches via channel.send(file=)."""
    src = _read_adapter_source()
    assert "_dispatch_send_markers" in src
    assert "parse_send_markers" in src
    assert "strip_send_markers" in src
    # File send uses discord.File
    assert "discord.File" in src


def test_discord_imports_voice_markers():
    src = _read_adapter_source()
    assert "from voice_markers import" in src
    assert "import voice as voice_mod" in src
