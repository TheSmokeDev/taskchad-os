"""PRD-8 Phase 4 — WhatsApp adapter Cloud-API media in/out + marker dispatch."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))


def _read_adapter_source() -> str:
    chat_dir = SCRIPTS_DIR.parent / "chat" / "adapters"
    return (chat_dir / "whatsapp.py").read_text()


def test_media_receive():
    """WhatsApp Cloud-API media-receive: GET /v21.0/{media_id} + bearer download."""
    src = _read_adapter_source()
    assert "_download_media" in src
    assert "graph.facebook.com/v21.0" in src or "GRAPH_API_BASE" in src
    assert "Authorization" in src and "Bearer" in src
    # Audio mime detection in webhook handler
    assert '"audio"' in src or "type.*audio" in src or "voice" in src.lower()
    # Cascade entrypoint (R4 NB2)
    assert "voice_mod.transcribe_audio_file" in src or "voice.transcribe_audio_file" in src


def test_media_send():
    """WhatsApp Cloud-API media-send: upload_media + send audio message."""
    src = _read_adapter_source()
    assert "_upload_media" in src
    assert "_send_audio_message" in src or "type.*audio" in src
    # POST to /{phone_id}/media
    assert "/media" in src
    # Audio reply via voice.synthesize
    assert "voice_mod.synthesize" in src or "voice.synthesize" in src


def test_marker_dispatch_whatsapp():
    """WhatsApp marker dispatch via Cloud-API upload + send."""
    src = _read_adapter_source()
    assert "_dispatch_send_markers" in src
    assert "parse_send_markers" in src


def test_whatsapp_imports_voice_markers():
    src = _read_adapter_source()
    assert "from voice_markers import" in src
    assert "import voice as voice_mod" in src
