"""F1 — the dream-cycle stale-skill archival seam (Skill-From-Experience WS3).

``skill_promotion.archive_stale()`` is the production rail that flips staged
self-authored drafts to ``archived`` after ``SKILL_STALE_DAYS`` (plus one audit
row each). Before the F1 fix it had NO runtime caller, so stale drafts never
archived. The fix wires it into ``memory_dream``'s Phase-2.5 archival seam —
the same sibling spot as ``living_memory.archive_stale_working_items``.

These tests prove:
  1. the dream seam CALLS ``skill_promotion.archive_stale`` on a normal run, and
  2. a raise inside it is SWALLOWED (the dream still completes successfully —
     every cognition/cron hook is fire-and-forget).

The seam runs in Phase 2.5, which is reached BEFORE the signal gate, so a
``DREAM_SILENT`` run (no LLM) still exercises it — no LLM mock needed for the
call-through test. The swallow test uses a full happy-path mock to prove the
failure does not bubble into the LLM phases or the final state.

NOTE: ``memory_dream`` runs ``apply_persona_override()`` (which inspects
``sys.argv``) at IMPORT time, so — matching ``tests/test_memory_dream.py`` —
it is imported INSIDE each test, never at module top level.
"""

from __future__ import annotations

import contextlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def dream_memory_dir(tmp_path):
    """Minimal memory dir with one daily log (so orient() does not early-exit)."""
    memory_dir = tmp_path / "TheHomie" / "Memory"
    (memory_dir / "concepts").mkdir(parents=True)
    daily_dir = memory_dir / "daily"
    daily_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text(
        "---\ntags: [system]\n---\n# MEMORY.md\n\n## Key Decisions\n\n- x\n",
        encoding="utf-8",
    )
    (memory_dir / "SELF.md").write_text(
        "---\ntags: [system]\n---\n# SELF.md\n", encoding="utf-8"
    )
    (memory_dir / "GOALS.md").write_text("# GOALS\n", encoding="utf-8")

    tz = ZoneInfo("America/Chicago")
    yesterday = (datetime.now(tz).date() - timedelta(days=1)).strftime("%Y-%m-%d")
    (daily_dir / f"{yesterday}.md").write_text(
        "# Log\n\nNothing notable today.\n", encoding="utf-8"
    )
    return memory_dir


@contextlib.contextmanager
def _patch_dream_paths(memory_dir, tmp_path, threshold=999):
    """Patch memory_dream module constants at tmp_path.

    Default threshold=999 keeps the run SILENT (no LLM) for the call-through
    test; callers can lower it to drive the LLM phases.
    """
    with patch("memory_dream.DREAM_STATE_FILE", tmp_path / "dream-state.json"), \
         patch("memory_dream.MEMORY_FILE", memory_dir / "MEMORY.md"), \
         patch("memory_dream.MEMORY_DIR", memory_dir), \
         patch("memory_dream.DAILY_DIR", memory_dir / "daily"), \
         patch("memory_dream.SELF_FILE", memory_dir / "SELF.md"), \
         patch("memory_dream.GOALS_FILE", memory_dir / "GOALS.md"), \
         patch("memory_dream.STATE_DIR", tmp_path), \
         patch(
             "memory_dream.AMENDMENT_LEDGER_FILE",
             tmp_path / "amendment-proposals.jsonl",
         ), \
         patch("memory_dream.DREAM_SIGNAL_THRESHOLD", threshold):
        yield


@pytest.mark.asyncio
async def test_dream_seam_calls_archive_stale(dream_memory_dir, tmp_path):
    """The dream Phase-2.5 seam invokes skill_promotion.archive_stale()."""
    from cognition import skill_promotion
    import memory_dream

    spy = MagicMock(return_value=["dusty-draft"])
    with _patch_dream_paths(dream_memory_dir, tmp_path), \
         patch.object(skill_promotion, "archive_stale", spy):
        # Silent run (threshold=999) — no LLM, but Phase 2.5 still runs.
        result = await memory_dream._run_dream_inner(
            test_mode=False, force=True, days=7
        )

    assert result == memory_dream.DREAM_SILENT
    spy.assert_called_once_with()


@pytest.mark.asyncio
async def test_dream_seam_swallows_archive_failure(dream_memory_dir, tmp_path):
    """A raise inside archive_stale() never breaks the dream run (fail-open).

    Drives the full happy path (threshold=1, mocked LLM) so the failure has to
    survive all the way to a successful 'consolidated' final state.
    """
    from cognition import skill_promotion
    import memory_dream

    # Give the daily log real signal so threshold=1 drives the LLM phases — this
    # proves the archive_stale failure (Phase 2.5) survives all the way to a
    # 'consolidated' terminal state, not just to an early silent exit.
    tz = ZoneInfo("America/Chicago")
    yesterday = (datetime.now(tz).date() - timedelta(days=1)).strftime("%Y-%m-%d")
    pad = "x" * 200
    (dream_memory_dir / "daily" / f"{yesterday}.md").write_text(
        "# Log\n\nThe approach was wrong, we need to rethink.\n"
        f"{pad}\nKey decision: use run_with_fallback everywhere.\n"
        f"{pad}\nGot stuck on the provider abstraction for hours.\n",
        encoding="utf-8",
    )

    boom = MagicMock(side_effect=RuntimeError("audit log on fire"))
    mock_rwf = AsyncMock(side_effect=[
        MagicMock(text="Merged signal", provider="test", model="m", cost_usd=0.0),
        MagicMock(text="PRUNE_OK", provider="test", model="m", cost_usd=0.0),
    ])

    with _patch_dream_paths(dream_memory_dir, tmp_path, threshold=1), \
         patch.object(skill_promotion, "archive_stale", boom), \
         patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
         patch("memory_dream._run_entity_compilation"), \
         patch("memory_dream._run_reindex"):
        # Must NOT raise despite archive_stale raising.
        result = await memory_dream._run_dream_inner(
            test_mode=False, force=True, days=7
        )

    boom.assert_called_once_with()
    assert result is not None
    assert result != memory_dream.DREAM_SILENT
    state = json.loads((tmp_path / "dream-state.json").read_text(encoding="utf-8"))
    assert state["result"] == "consolidated"
