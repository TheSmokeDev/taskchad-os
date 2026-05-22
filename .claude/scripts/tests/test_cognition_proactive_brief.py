"""Tests for unified proactive brief assembly."""

from __future__ import annotations

from pathlib import Path
import sys

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for path in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from cognitive_loop_test_harness import (  # noqa: E402
    ACTIVE_INFERENCE_SENTINEL,
    IDENTITY_SENTINELS,
    get_cognitive_loop_inference_state_file,
    seed_cognitive_loop_temp_vault,
)
from cognition.proactive_brief import build_proactive_brief  # noqa: E402


def test_proactive_brief_renders_shared_living_context(tmp_path: Path) -> None:
    vault = seed_cognitive_loop_temp_vault(tmp_path / "TheHomie" / "Memory")

    brief = build_proactive_brief(
        vault,
        inference_state_file=get_cognitive_loop_inference_state_file(vault),
        include_identity=True,
    )

    assert "## Proactive Brief" in brief.section
    assert IDENTITY_SENTINELS["SOUL"] in brief.section
    assert IDENTITY_SENTINELS["WORKING"] in brief.section
    assert ACTIVE_INFERENCE_SENTINEL in brief.section
    assert "Validation heartbeat checklist" in brief.section
    assert brief.source_paths["working_file"].endswith("WORKING.md")


def test_proactive_brief_can_omit_identity_for_session_bootstrap(tmp_path: Path) -> None:
    vault = seed_cognitive_loop_temp_vault(tmp_path / "TheHomie" / "Memory")

    brief = build_proactive_brief(
        vault,
        inference_state_file=get_cognitive_loop_inference_state_file(vault),
        include_identity=False,
    )

    assert IDENTITY_SENTINELS["SOUL"] not in brief.section
    assert IDENTITY_SENTINELS["WORKING"] in brief.section
    assert ACTIVE_INFERENCE_SENTINEL in brief.section
