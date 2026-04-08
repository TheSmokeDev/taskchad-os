"""Regression tests: verify no deployment-specific branching remains after identity unification.

PRP: PRPs/active/PRP-identity-unification.md
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path


# ── Source code regression checks ──

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "runtime"


def _read_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_engine_no_is_primo_branch() -> None:
    """engine.py must not contain any is_primo branching."""
    source = _read_source(_CHAT_DIR / "engine.py")
    assert "is_primo" not in source, "Found 'is_primo' in engine.py — identity branch not fully removed"


def test_engine_no_primo_context() -> None:
    """engine.py must not reference _primo_context."""
    source = _read_source(_CHAT_DIR / "engine.py")
    assert "_primo_context" not in source, "Found '_primo_context' in engine.py"


def test_bootstrap_no_primo_builder() -> None:
    """bootstrap.py must not contain build_primo_identity_context."""
    source = _read_source(_RUNTIME_DIR / "bootstrap.py")
    assert "build_primo_identity_context" not in source
    assert "_PRIMO_SERVER" not in source
    assert "_PRIMO_FALLBACK" not in source


def test_runtime_init_no_primo_export() -> None:
    """runtime/__init__.py must not export primo-related symbols."""
    source = _read_source(_RUNTIME_DIR / "__init__.py")
    assert "primo" not in source.lower()


def test_commands_no_primo() -> None:
    """commands.py must not register a /primo command."""
    source = _read_source(_CHAT_DIR / "commands.py")
    assert '"primo"' not in source, "Found primo command registration in commands.py"


def test_router_no_handle_primo() -> None:
    """router.py must not contain _handle_primo method."""
    source = _read_source(_CHAT_DIR / "router.py")
    assert "_handle_primo" not in source, "Found _handle_primo in router.py"


def test_models_default_agent_type_is_thehomie() -> None:
    """IncomingMessage.agent_type default must be 'thehomie'."""
    from models import IncomingMessage, Channel, Platform, User

    msg = IncomingMessage(
        text="test",
        user=User(platform=Platform.CLI, platform_id="test"),
        channel=Channel(platform=Platform.CLI, platform_id="test"),
        platform=Platform.CLI,
    )
    assert msg.agent_type == "thehomie", f"Expected 'thehomie', got '{msg.agent_type}'"


def test_graph_no_primo_soul_stem() -> None:
    """cognition/graph.py must not include primo-soul in identity stems."""
    source = _read_source(_CHAT_DIR / "cognition" / "graph.py")
    assert "primo-soul" not in source, "Found 'primo-soul' in graph.py _IDENTITY_STEMS"
