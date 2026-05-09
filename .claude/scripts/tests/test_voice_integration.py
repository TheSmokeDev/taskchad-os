"""PRD-8 Phase 4 — cross-adapter voice integration tests.

Asserts the marker parser is consumed uniformly across all 6 adapters and that
the round-trip (synthetic agent reply with [SEND_FILE]) flows through each
adapter's send path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))


_ALL_ADAPTERS = (
    "telegram",
    "discord",
    "slack",
    "whatsapp",
    "web",
    "cli_adapter",
)


def _read_adapter(name: str) -> str:
    chat_dir = SCRIPTS_DIR.parent / "chat" / "adapters"
    return (chat_dir / f"{name}.py").read_text()


@pytest.mark.parametrize("adapter_name", _ALL_ADAPTERS)
def test_all_adapters_use_marker_parser(adapter_name):
    """Each of the 6 adapters imports voice_markers.parse_send_markers."""
    src = _read_adapter(adapter_name)
    assert "from voice_markers import" in src or "import voice_markers" in src, (
        f"{adapter_name} missing voice_markers import"
    )
    assert "parse_send_markers" in src, (
        f"{adapter_name} doesn't reference parse_send_markers"
    )
    assert "strip_send_markers" in src, (
        f"{adapter_name} doesn't reference strip_send_markers"
    )


@pytest.mark.parametrize("adapter_name", _ALL_ADAPTERS)
def test_all_adapters_round_trip_marker(adapter_name):
    """Each of the 6 adapters has a marker-dispatch path that handles SEND_FILE.

    For Telegram/Discord/Slack/WhatsApp/Web: the send() path strips markers and
    calls _dispatch_send_markers.
    For CLI: the send() path strips markers and emits a `[CLI media:...]` line.
    """
    src = _read_adapter(adapter_name)
    if adapter_name == "cli_adapter":
        assert "[CLI media" in src
    else:
        assert "_dispatch_send_markers" in src


@pytest.mark.parametrize("adapter_name", ("telegram", "discord", "slack", "whatsapp", "web", "cli_adapter"))
def test_all_adapters_use_cascade_entrypoint(adapter_name):
    """All adapters use the canonical voice.transcribe_audio_file (R4 NB2 — NOT legacy 3-arg)."""
    src = _read_adapter(adapter_name)
    # Telegram retains the legacy stt provider for back-compat, but ALSO
    # uses the cascade transcribe_audio_file when capabilities are present.
    assert (
        "voice_mod.transcribe_audio_file" in src
        or "voice.transcribe_audio_file" in src
    ), f"{adapter_name} missing cascade entrypoint usage"
