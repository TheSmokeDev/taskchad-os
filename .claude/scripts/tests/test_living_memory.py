"""Tests for living_memory (WORKING.md scratchpad) — behavior + Langfuse spans.

13 behavior tests + 5 Langfuse span tests = 18 total.
Plan: PRPs/active/enumerated-marinating-pillow.md (Living Mind Phase 1).
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


SAMPLE_WM = """---
tags: [system, memory, working]
status: current
date: 2026-04-17
summary: "test"
priority: P1
---

# WORKING.md

## Open Threads

<!-- format comment -->

- [2026-04-16] thread alpha \u2014 pending review
- [2026-04-17] thread beta \u2014 in progress

## Active Hypotheses

- [2026-04-15] suspect lane router regression \u2014 evidence: [[DAILY-2026-04-15]]

## Unresolved Questions

- [2026-04-17] what is the plaid ETA?

## Heartbeat Observations

## Archived (Cold)
"""


def _backdate(bullet_template: str, days_ago: int) -> str:
    """Helper: produce a bullet with date `days_ago` days before today."""
    d = (date.today() - timedelta(days=days_ago)).isoformat()
    return bullet_template.format(d=d)


def _fresh_sample_wm() -> str:
    """Same shape as SAMPLE_WM but with bullet dates relative to today.

    SAMPLE_WM hardcodes 2026-04-15..17 — once "today" is more than 7 days past
    the latest of those, archive(days=7) sees every bullet as stale and the
    "no items archived" assertions in test_archive_idempotent and
    test_archive_emits_span_with_correct_counts break. Use this helper for
    any test that reasons about bullet age; SAMPLE_WM stays static for tests
    that only care about structural parsing.
    """
    today = date.today()
    d1 = (today - timedelta(days=1)).isoformat()
    d2 = (today - timedelta(days=2)).isoformat()
    d3 = (today - timedelta(days=3)).isoformat()
    return f"""---
tags: [system, memory, working]
status: current
date: {today.isoformat()}
summary: "test"
priority: P1
---

# WORKING.md

## Open Threads

<!-- format comment -->

- [{d2}] thread alpha — pending review
- [{d1}] thread beta — in progress

## Active Hypotheses

- [{d3}] suspect lane router regression — evidence: [[DAILY-{d3}]]

## Unresolved Questions

- [{d1}] what is the plaid ETA?

## Heartbeat Observations

## Archived (Cold)
"""


# =============================================================================
# TestReadWorkingMemory — behavior #1
# =============================================================================


class TestReadWorkingMemory:
    def test_read_missing_file_returns_empty(self, tmp_path):
        """#1 graceful on missing WORKING.md"""
        from living_memory import read_working_memory

        data = read_working_memory(tmp_path)
        assert data.exists is False
        assert data.open_threads == []
        assert data.active_hypotheses == []
        assert data.unresolved_questions == []
        assert data.raw_content == ""

    def test_read_parses_existing_file(self, tmp_path):
        """Read returns correct section bullets."""
        from living_memory import read_working_memory

        (tmp_path / "WORKING.md").write_text(SAMPLE_WM, encoding="utf-8")
        data = read_working_memory(tmp_path)

        assert data.exists is True
        assert len(data.open_threads) == 2
        assert "thread alpha" in data.open_threads[0]
        assert len(data.active_hypotheses) == 1
        assert len(data.unresolved_questions) == 1


# =============================================================================
# TestAppendOperations — behavior #2, #3, #4
# =============================================================================


class TestAppendOperations:
    def test_append_open_thread_creates_file(self, tmp_path):
        """#2 first write bootstraps the file with frontmatter."""
        from living_memory import append_open_thread

        count = append_open_thread(tmp_path, subject="wire engine region", status="in progress")
        assert count == 1

        path = tmp_path / "WORKING.md"
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert content.startswith("---\n")
        assert "## Open Threads" in content
        assert "wire engine region" in content
        assert "in progress" in content

    def test_append_preserves_manual_edits(self, tmp_path):
        """#3 add a manual section item, run writer, manual item still there."""
        from living_memory import append_open_thread

        path = tmp_path / "WORKING.md"
        # Seed with a manual bullet in Active Hypotheses
        path.write_text(SAMPLE_WM, encoding="utf-8")

        append_open_thread(tmp_path, subject="new thread", status="open")

        content = path.read_text(encoding="utf-8")
        # Manual hypothesis survived the write
        assert "suspect lane router regression" in content
        # Manual question survived
        assert "what is the plaid ETA?" in content
        # New thread appended
        assert "new thread" in content

    def test_dedup_within_3_days(self, tmp_path):
        """#4 append same subject twice within window → second is no-op."""
        from living_memory import append_open_thread

        first = append_open_thread(tmp_path, subject="dedup candidate", status="open")
        second = append_open_thread(tmp_path, subject="dedup candidate", status="open")

        assert first == 1
        assert second == 0

        content = (tmp_path / "WORKING.md").read_text(encoding="utf-8")
        # Only one bullet
        assert content.count("dedup candidate") == 1


# =============================================================================
# TestArchiveStale — behavior #5, #6, #7, #10
# =============================================================================


class TestArchiveStale:
    def test_archive_moves_not_deletes(self, tmp_path):
        """#5 stale item appears in Archived (Cold), NOT gone (Gary Tan invariant)."""
        from living_memory import archive_stale_working_items

        path = tmp_path / "WORKING.md"
        stale_bullet = _backdate("- [{d}] very old thread \u2014 pending", 10)
        path.write_text(
            SAMPLE_WM.replace(
                "- [2026-04-16] thread alpha \u2014 pending review",
                stale_bullet,
            ),
            encoding="utf-8",
        )

        report = archive_stale_working_items(tmp_path, days=7)

        content = path.read_text(encoding="utf-8")
        assert report.archived_count >= 1
        # Stale text is now in archive, not in active section
        assert "very old thread" in content
        # Extract the Open Threads section — stale bullet should NOT be in it
        ot_start = content.find("## Open Threads")
        ot_end = content.find("## Active Hypotheses")
        open_threads_block = content[ot_start:ot_end]
        assert "very old thread" not in open_threads_block
        # But in Archived (Cold)
        archive_start = content.find("## Archived (Cold)")
        archive_block = content[archive_start:]
        assert "very old thread" in archive_block

    def test_archive_preserves_original_date(self, tmp_path):
        """#6 archived bullet format `[archived YYYY-MM-DD] (was: YYYY-MM-DD) <content>`."""
        from living_memory import archive_stale_working_items

        path = tmp_path / "WORKING.md"
        original_date = (date.today() - timedelta(days=14)).isoformat()
        stale_bullet = f"- [{original_date}] very old thread \u2014 pending"
        path.write_text(
            SAMPLE_WM.replace(
                "- [2026-04-16] thread alpha \u2014 pending review",
                stale_bullet,
            ),
            encoding="utf-8",
        )

        archive_stale_working_items(tmp_path, days=7)

        content = path.read_text(encoding="utf-8")
        # Must have the archived format with (was: ORIGINAL_DATE)
        assert f"(was: {original_date})" in content
        today_str = date.today().isoformat()
        assert f"[archived {today_str}]" in content

    def test_archive_idempotent(self, tmp_path):
        """#7 running archive twice in a row with no new items is a no-op."""
        from living_memory import archive_stale_working_items

        path = tmp_path / "WORKING.md"
        # Use today-relative dates so all bullets are < 7 days old regardless
        # of when this test runs. SAMPLE_WM hardcodes 2026-04-15..17 which
        # decay into stale territory and break the "no items archived" claim.
        path.write_text(_fresh_sample_wm(), encoding="utf-8")

        report1 = archive_stale_working_items(tmp_path, days=7)
        content_after_first = path.read_text(encoding="utf-8")

        report2 = archive_stale_working_items(tmp_path, days=7)
        content_after_second = path.read_text(encoding="utf-8")

        # Nothing archived either time
        assert report1.archived_count == 0
        assert report2.archived_count == 0
        # File unchanged (idempotent)
        assert content_after_first == content_after_second

    def test_archive_empty_file_is_safe(self, tmp_path):
        """#10 archive on missing file returns empty report, no crash."""
        from living_memory import archive_stale_working_items

        report = archive_stale_working_items(tmp_path, days=7)
        assert report.archived_count == 0
        assert report.sections_touched == []


# =============================================================================
# TestFrontmatterAndCap — behavior #8, #9
# =============================================================================


class TestFrontmatterAndCap:
    def test_frontmatter_date_updated_on_write(self, tmp_path):
        """#8 `date:` field reflects write time."""
        from living_memory import append_open_thread

        path = tmp_path / "WORKING.md"
        # Pre-seed with an old date
        seeded = SAMPLE_WM.replace("date: 2026-04-17", "date: 2020-01-01")
        path.write_text(seeded, encoding="utf-8")

        append_open_thread(tmp_path, subject="trigger rewrite", status="open")

        content = path.read_text(encoding="utf-8")
        today_str = date.today().isoformat()
        assert f"date: {today_str}" in content
        assert "date: 2020-01-01" not in content

    def test_cap_10_open_threads(self, tmp_path):
        """#9 11th append evicts oldest to archive."""
        from living_memory import append_open_thread, read_working_memory

        # Fill up 11 distinct threads
        for i in range(11):
            append_open_thread(tmp_path, subject=f"thread number {i}", status="open")

        data = read_working_memory(tmp_path)
        assert len(data.open_threads) == 10, (
            f"Expected cap at 10, got {len(data.open_threads)}"
        )
        # One archived
        assert len(data.archived) >= 1


# =============================================================================
# TestFlushExtraction — behavior #11
# =============================================================================


class TestFlushExtraction:
    def test_append_from_flush_extracts_todos(self, tmp_path):
        """#11 given a session flush markdown, extracts TODO lines."""
        from living_memory import append_open_threads_from_flush

        flush_md = """
# Session Flush — 2026-04-17

- [ ] implement langfuse span tests
TODO: wire session-end hook for living_memory
We still need to verify the archive idempotency path.
The user was waiting for the plaid development approval.
"""
        count = append_open_threads_from_flush(tmp_path, flush_md)
        assert count >= 2  # capped at 3, at least several signals matched

        content = (tmp_path / "WORKING.md").read_text(encoding="utf-8")
        # At least one of the signals should appear
        assert any(
            s in content
            for s in (
                "implement langfuse span tests",
                "wire session-end hook",
                "plaid development approval",
            )
        )


# =============================================================================
# TestBriefingSection — behavior #12, #13
# =============================================================================


class TestBriefingSection:
    def test_build_briefing_section_populated(self, tmp_path):
        """#12 briefing section surfaces open threads + hypotheses."""
        from living_memory import build_briefing_section

        (tmp_path / "WORKING.md").write_text(SAMPLE_WM, encoding="utf-8")

        briefing = build_briefing_section(tmp_path)
        assert briefing.startswith("## Working Memory")
        assert "thread alpha" in briefing or "thread beta" in briefing
        assert "Active hypotheses:" in briefing
        assert "Unresolved:" in briefing

    def test_build_briefing_respects_empty_file(self, tmp_path):
        """#13 briefing returns empty string when WORKING.md missing."""
        from living_memory import build_briefing_section

        assert build_briefing_section(tmp_path) == ""


# =============================================================================
# TestLangfuseSpans — observability #14-18
# =============================================================================


def _install_fake_langfuse(mock_get_client):
    """Wire up a MagicMock chain for get_client().start_as_current_observation()."""
    fake_client = MagicMock()
    fake_span = MagicMock()
    fake_client.start_as_current_observation.return_value.__enter__.return_value = fake_span
    fake_client.start_as_current_observation.return_value.__exit__.return_value = False
    mock_get_client.return_value = fake_client
    return fake_client, fake_span


class TestLangfuseSpans:
    def test_read_emits_langfuse_span_when_enabled(self, tmp_path):
        """#14 mock get_client, call read_working_memory, assert span + metadata."""
        (tmp_path / "WORKING.md").write_text(SAMPLE_WM, encoding="utf-8")

        with patch("runtime.langfuse_setup.is_langfuse_enabled", return_value=True):
            with patch("langfuse.get_client") as mock_get_client:
                _fake_client, fake_span = _install_fake_langfuse(mock_get_client)

                from living_memory import read_working_memory
                result = read_working_memory(tmp_path)

                assert result.exists is True
                mock_get_client.return_value.start_as_current_observation.assert_called_once()
                call_kwargs = (
                    mock_get_client.return_value.start_as_current_observation.call_args.kwargs
                )
                assert call_kwargs["name"] == "living_memory_read"
                fake_span.update.assert_called()
                metadata = fake_span.update.call_args.kwargs["metadata"]
                assert "threads_count" in metadata
                assert "bytes_read" in metadata
                assert metadata["threads_count"] == 2

    def test_write_emits_span_with_dedup_metadata(self, tmp_path):
        """#15 write two items where second dedups; assert threads_appended=1 + skipped=1."""
        with patch("runtime.langfuse_setup.is_langfuse_enabled", return_value=True):
            with patch("langfuse.get_client") as mock_get_client:
                _install_fake_langfuse(mock_get_client)

                from living_memory import append_open_thread
                append_open_thread(tmp_path, subject="dedup test", status="open")
                append_open_thread(tmp_path, subject="dedup test", status="open")

                obs_calls = (
                    mock_get_client.return_value.start_as_current_observation.call_args_list
                )
                # Each call was named living_memory_write
                assert all(c.kwargs.get("name") == "living_memory_write" for c in obs_calls)

                # Collect all metadata dicts across span.update calls
                fake_span = (
                    mock_get_client.return_value.start_as_current_observation
                    .return_value.__enter__.return_value
                )
                metadata_dicts = [
                    c.kwargs["metadata"] for c in fake_span.update.call_args_list
                ]
                appended = [m.get("threads_appended") for m in metadata_dicts]
                skipped = [m.get("threads_skipped_dedup") for m in metadata_dicts]
                assert 1 in appended
                assert 1 in skipped

    def test_archive_emits_span_with_correct_counts(self, tmp_path):
        """#16 seed stale items, run archive, assert archived_count, sections_touched, days_threshold."""
        path = tmp_path / "WORKING.md"
        # Build a file with 3 stale items across 2 sections. The base file uses
        # today-relative fresh dates so the only stale bullets are the ones we
        # explicitly inject \u2014 keeps archived_count=3 stable as time advances.
        today = date.today()
        d2 = (today - timedelta(days=2)).isoformat()
        d3 = (today - timedelta(days=3)).isoformat()
        stale_ot = _backdate("- [{d}] stale thread A", 14)
        stale_hp = _backdate("- [{d}] stale hypothesis B \u2014 evidence: [[X]]", 14)
        stale_hp2 = _backdate("- [{d}] stale hypothesis C \u2014 evidence: [[Y]]", 20)
        content = _fresh_sample_wm()
        content = content.replace(
            f"- [{d2}] thread alpha \u2014 pending review",
            stale_ot,
        )
        content = content.replace(
            f"- [{d3}] suspect lane router regression \u2014 evidence: [[DAILY-{d3}]]",
            f"{stale_hp}\n{stale_hp2}",
        )
        path.write_text(content, encoding="utf-8")

        with patch("runtime.langfuse_setup.is_langfuse_enabled", return_value=True):
            with patch("langfuse.get_client") as mock_get_client:
                _install_fake_langfuse(mock_get_client)

                from living_memory import archive_stale_working_items
                archive_stale_working_items(tmp_path, days=7)

                fake_span = (
                    mock_get_client.return_value.start_as_current_observation
                    .return_value.__enter__.return_value
                )
                # Find the update call that carried the archive metadata
                md_dicts = [c.kwargs["metadata"] for c in fake_span.update.call_args_list]
                archived = [m for m in md_dicts if "archived_count" in m]
                assert archived, "no span update included archived_count"
                md = archived[-1]
                assert md["archived_count"] == 3
                assert md["sections_touched"] == 2
                assert md["days_threshold"] == 7

    def test_no_span_when_langfuse_disabled(self, tmp_path):
        """#17 when LF disabled, get_client is never called (zero-overhead disabled path)."""
        with patch("runtime.langfuse_setup.is_langfuse_enabled", return_value=False):
            with patch("langfuse.get_client") as mock_get_client:
                from living_memory import (
                    append_open_thread,
                    archive_stale_working_items,
                    read_working_memory,
                )
                # Ensure file exists for read
                append_open_thread(tmp_path, subject="disabled test", status="open")
                read_working_memory(tmp_path)
                archive_stale_working_items(tmp_path, days=7)

                mock_get_client.assert_not_called()

    def test_span_exception_does_not_break_runtime(self, tmp_path):
        """#18 make start_as_current_observation raise; function still works."""
        with patch("runtime.langfuse_setup.is_langfuse_enabled", return_value=True):
            with patch("langfuse.get_client") as mock_get_client:
                fake_client = MagicMock()
                fake_client.start_as_current_observation.side_effect = RuntimeError("boom")
                mock_get_client.return_value = fake_client

                from living_memory import append_open_thread, read_working_memory

                # Writing should still succeed (falls back to _NoOpSpan)
                count = append_open_thread(
                    tmp_path, subject="resilience check", status="open"
                )
                assert count == 1

                data = read_working_memory(tmp_path)
                assert data.exists is True
                assert any("resilience check" in b for b in data.open_threads)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
