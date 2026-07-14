"""Tests for living_memory (WORKING.md scratchpad) — behavior + Langfuse spans.

Behavior tests + Langfuse span tests + flush subject-quality regressions.
Plan: PRPs/active/enumerated-marinating-pillow.md (Living Mind Phase 1) +
PRPs/active/PRP-living-mind-act1-heartbeat-blocker-escalation.md (Act 1:
flush subject quality + Rule-3 accessor re-target).
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

from runtime import langfuse_setup  # noqa: E402

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
# TestFlushSubjectQuality — Living Mind Act 1 behavior 6
#
# Regression fixtures drawn from the real 2026-06-11 WORKING.md garbage. The
# OLD tail-group extractor produced fragments like "the verdict — open" and
# "** line (so it surfaces even in a fresh session) — open"; these tests FAIL
# against the old extractor and prove the full-line rewrite.
# =============================================================================


class TestFlushSubjectQuality:
    def test_bold_decorated_line_extracts_full_subject_not_fragment(self, tmp_path):
        """Real garbage class: `** line (so it surfaces even in a fresh session)`.

        The 'next up' signal matched inside `**Next Up**` and tail-captured
        from the closing `**`. New behavior: the full line, markdown-stripped.
        """
        from living_memory import append_open_threads_from_flush

        flush_md = (
            "The handoff includes a **Next Up** line "
            "(so it surfaces even in a fresh session).\n"
        )
        count = append_open_threads_from_flush(tmp_path, flush_md)
        assert count == 1

        content = (tmp_path / "WORKING.md").read_text(encoding="utf-8")
        # Old fragment must not appear as a subject
        assert "] ** line (so it surfaces" not in content
        # Full readable line, decoration stripped
        assert (
            "The handoff includes a Next Up line "
            "(so it surfaces even in a fresh session)"
        ) in content

    def test_waiting_on_pronoun_tail_keeps_full_sentence(self, tmp_path):
        """Real garbage class: `it (his 00:39 message: ...)`.

        'waiting on' tail-captured a bare-pronoun fragment. New behavior keeps
        the whole readable line (which does NOT start with a pronoun).
        """
        from living_memory import append_open_threads_from_flush

        flush_md = (
            'Smoke is waiting on it (his 00:39 message: *"please second-look the '
            'dashboard chat reliability spec reset before Codex continues"*).\n'
        )
        count = append_open_threads_from_flush(tmp_path, flush_md)
        assert count == 1

        content = (tmp_path / "WORKING.md").read_text(encoding="utf-8")
        # Old fragment subject started with the bare pronoun
        assert "] it (his 00:39 message" not in content
        assert "Smoke is waiting on it (his 00:39 message" in content

    def test_tail_fragment_replaced_by_full_line(self, tmp_path):
        """Real garbage class: `the verdict`.

        'waiting on the verdict.' tail-captured just 'the verdict'. New
        behavior extracts the full line.
        """
        from living_memory import append_open_threads_from_flush

        flush_md = "Codex is still grinding; Smoke is waiting on the verdict.\n"
        count = append_open_threads_from_flush(tmp_path, flush_md)
        assert count == 1

        content = (tmp_path / "WORKING.md").read_text(encoding="utf-8")
        assert "] the verdict" not in content
        assert "Codex is still grinding; Smoke is waiting on the verdict" in content

    def test_bare_pronoun_led_line_rejected(self, tmp_path):
        """A line that (even in full) leads with a bare pronoun is rejected."""
        from living_memory import append_open_threads_from_flush

        flush_md = "It still needs review — waiting on the Codex verdict run.\n"
        count = append_open_threads_from_flush(tmp_path, flush_md)
        assert count == 0
        # Nothing was written at all (file may not even exist)
        path = tmp_path / "WORKING.md"
        if path.exists():
            assert "waiting on the Codex verdict run" not in path.read_text(
                encoding="utf-8"
            )

    def test_under_four_word_subject_rejected(self, tmp_path):
        """Subjects under 4 words are fragments — rejected."""
        from living_memory import append_open_threads_from_flush

        assert append_open_threads_from_flush(tmp_path, "TODO: fix tests\n") == 0

    def test_markdown_fragment_rejected_after_stripping(self, tmp_path):
        """A line that strips to nothing/punctuation-only is rejected."""
        from living_memory import append_open_threads_from_flush

        assert append_open_threads_from_flush(tmp_path, "- [ ] **\n") == 0
        assert append_open_threads_from_flush(tmp_path, "TODO: ----\n") == 0

    def test_long_subject_trimmed_at_word_boundary(self, tmp_path):
        """Subjects trim to 140 chars at a word boundary (no mid-word cuts)."""
        from living_memory import _extract_thread_candidates

        long_tail = " ".join(["verify"] * 40)
        candidates = _extract_thread_candidates(f"TODO: {long_tail}\n")
        assert len(candidates) == 1
        subject = candidates[0]
        assert len(subject) <= 140
        # Word-boundary trim: never ends mid-word (all words are "verify")
        assert subject.split()[-1] == "verify"

    def test_max_threads_knob_resolves_at_call_time(self, tmp_path, monkeypatch):
        """WORKING_MEMORY_MAX_FLUSH_THREADS resolves per call (Rule 1) —
        monkeypatch.setenv takes effect without any module reload."""
        from living_memory import _extract_thread_candidates

        flush_md = (
            "TODO: first thread subject for the cap test\n"
            "TODO: second thread subject for the cap test\n"
            "TODO: third thread subject for the cap test\n"
        )
        monkeypatch.setenv("WORKING_MEMORY_MAX_FLUSH_THREADS", "1")
        assert len(_extract_thread_candidates(flush_md)) == 1
        monkeypatch.setenv("WORKING_MEMORY_MAX_FLUSH_THREADS", "3")
        assert len(_extract_thread_candidates(flush_md)) == 3
        # Explicit arg wins over env
        assert len(_extract_thread_candidates(flush_md, max_threads=2)) == 2

    def test_dedup_window_knob_resolves_at_call_time(self, monkeypatch):
        """WORKING_MEMORY_DEDUP_DAYS resolves per call (Rule 1) — this failed
        before the 2026-07-07 refactor because the window was bound as a
        function default arg at def time (the PR #7/#21 bug class)."""
        from living_memory import _dedup_match

        five_days_ago = (date.today() - timedelta(days=5)).isoformat()
        section = [f"- [{five_days_ago}] stale subject for the window test — open"]

        # Default 3-day window: a 5-day-old bullet is NOT a duplicate.
        monkeypatch.delenv("WORKING_MEMORY_DEDUP_DAYS", raising=False)
        assert _dedup_match(section, "stale subject for the window test") is False
        # Widen the window via env AFTER import — must take effect immediately.
        monkeypatch.setenv("WORKING_MEMORY_DEDUP_DAYS", "7")
        assert _dedup_match(section, "stale subject for the window test") is True
        # Explicit arg wins over env
        assert (
            _dedup_match(section, "stale subject for the window test", window_days=2)
            is False
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


def _make_fake_client():
    """Build a MagicMock client whose start_as_current_observation yields a span.

    Living Mind Act 1 (R2 NB1): living_memory reaches Langfuse ONLY through
    langfuse_setup.get_observation_client() via module-attribute lookup, so
    these tests patch the accessor on the module — proving the call site
    observes the patch (Rule 3 propagation).
    """
    fake_client = MagicMock()
    fake_span = MagicMock()
    fake_client.start_as_current_observation.return_value.__enter__.return_value = fake_span
    fake_client.start_as_current_observation.return_value.__exit__.return_value = False
    return fake_client, fake_span


class TestLangfuseSpans:
    def test_read_emits_langfuse_span_when_enabled(self, tmp_path, monkeypatch):
        """#14 patch accessor -> fake client; assert span name + metadata."""
        (tmp_path / "WORKING.md").write_text(SAMPLE_WM, encoding="utf-8")
        fake_client, fake_span = _make_fake_client()
        monkeypatch.setattr(
            langfuse_setup, "get_observation_client", lambda: fake_client
        )

        from living_memory import read_working_memory
        result = read_working_memory(tmp_path)

        assert result.exists is True
        fake_client.start_as_current_observation.assert_called_once()
        call_kwargs = fake_client.start_as_current_observation.call_args.kwargs
        assert call_kwargs["name"] == "living_memory_read"
        fake_span.update.assert_called()
        metadata = fake_span.update.call_args.kwargs["metadata"]
        assert "threads_count" in metadata
        assert "bytes_read" in metadata
        assert metadata["threads_count"] == 2

    def test_write_emits_span_with_dedup_metadata(self, tmp_path, monkeypatch):
        """#15 write two items where second dedups; assert threads_appended=1 + skipped=1."""
        fake_client, fake_span = _make_fake_client()
        monkeypatch.setattr(
            langfuse_setup, "get_observation_client", lambda: fake_client
        )

        from living_memory import append_open_thread
        append_open_thread(tmp_path, subject="dedup test", status="open")
        append_open_thread(tmp_path, subject="dedup test", status="open")

        obs_calls = fake_client.start_as_current_observation.call_args_list
        # Each call was named living_memory_write
        assert all(c.kwargs.get("name") == "living_memory_write" for c in obs_calls)

        # Collect all metadata dicts across span.update calls
        metadata_dicts = [
            c.kwargs["metadata"] for c in fake_span.update.call_args_list
        ]
        appended = [m.get("threads_appended") for m in metadata_dicts]
        skipped = [m.get("threads_skipped_dedup") for m in metadata_dicts]
        assert 1 in appended
        assert 1 in skipped

    def test_archive_emits_span_with_correct_counts(self, tmp_path, monkeypatch):
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

        fake_client, fake_span = _make_fake_client()
        monkeypatch.setattr(
            langfuse_setup, "get_observation_client", lambda: fake_client
        )

        from living_memory import archive_stale_working_items
        archive_stale_working_items(tmp_path, days=7)

        # Find the update call that carried the archive metadata
        md_dicts = [c.kwargs["metadata"] for c in fake_span.update.call_args_list]
        archived = [m for m in md_dicts if "archived_count" in m]
        assert archived, "no span update included archived_count"
        md = archived[-1]
        assert md["archived_count"] == 3
        assert md["sections_touched"] == 2
        assert md["days_threshold"] == 7

    def test_no_span_when_langfuse_disabled(self, tmp_path, monkeypatch):
        """#17 accessor returns None (disabled) -> ops work; raw client never built."""
        accessor_calls: list[int] = []

        def _none_accessor():
            accessor_calls.append(1)
            return None

        monkeypatch.setattr(langfuse_setup, "get_observation_client", _none_accessor)
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

            # living_memory never constructs a raw langfuse client itself
            mock_get_client.assert_not_called()
        # Every span site consulted the (patched) accessor — module-attribute
        # lookup propagation proof.
        assert len(accessor_calls) >= 3

    def test_span_exception_does_not_break_runtime(self, tmp_path, monkeypatch):
        """#18 accessor raising (and span ctor raising) never breaks the write path."""
        def _raising_accessor():
            raise RuntimeError("boom")

        monkeypatch.setattr(langfuse_setup, "get_observation_client", _raising_accessor)

        from living_memory import append_open_thread, read_working_memory

        # Writing should still succeed (falls back to _NoOpSpan)
        count = append_open_thread(
            tmp_path, subject="resilience check", status="open"
        )
        assert count == 1

        # Client returned but span constructor raising is also survivable
        fake_client = MagicMock()
        fake_client.start_as_current_observation.side_effect = RuntimeError("boom")
        monkeypatch.setattr(
            langfuse_setup, "get_observation_client", lambda: fake_client
        )
        data = read_working_memory(tmp_path)
        assert data.exists is True
        assert any("resilience check" in b for b in data.open_threads)


# =============================================================================
# Heartbeat observations primitive (Living Mind Act 2)
# =============================================================================


def _wm_with_observations(obs_bullets, threads=(), hypotheses=()):
    """Build a full WORKING.md with given Heartbeat Observations bullets."""
    today = date.today().isoformat()
    threads_txt = "\n".join(threads)
    hyps_txt = "\n".join(hypotheses)
    obs_txt = "\n".join(obs_bullets)
    return f"""---
tags: [system, memory, working]
status: current
date: {today}
summary: "test"
priority: P1
---

# WORKING.md

## Open Threads

{threads_txt}

## Active Hypotheses

{hyps_txt}

## Unresolved Questions

## Heartbeat Observations

{obs_txt}

## Archived (Cold)
"""


class TestObservationSanitizer:
    def test_strips_newlines_controls_backticks_comments(self):
        from living_memory import _sanitize_observation_text

        dirty = "a\nb\tc\x07d `code` <!-- hidden --> e"
        out = _sanitize_observation_text(dirty, 120)
        assert "\n" not in out and "\t" not in out and "\x07" not in out
        assert "`" not in out
        assert "<!--" not in out and "-->" not in out
        assert "  " not in out  # whitespace collapsed
        assert out == "a b c d code hidden e"

    def test_trims_at_word_boundary(self):
        from living_memory import _sanitize_observation_text

        text = "alpha bravo charlie delta echo"
        out = _sanitize_observation_text(text, 14)  # cuts inside "charlie"
        assert out == "alpha bravo"
        assert len(out) <= 14

    def test_empty_and_control_only_become_empty(self):
        from living_memory import _sanitize_observation_text

        assert _sanitize_observation_text("", 80) == ""
        assert _sanitize_observation_text("\x01\x02\n\t", 80) == ""


class TestObservationPrimitive:
    def test_bullet_shape_with_and_without_detail(self, tmp_path):
        from living_memory import (
            ObservationAppendStatus,
            append_heartbeat_observation,
            read_working_memory,
        )

        today = date.today().isoformat()
        status = append_heartbeat_observation(
            tmp_path, "calendar", "meeting within 4h", "1 upcoming, 3 today"
        )
        assert status is ObservationAppendStatus.WRITTEN
        status2 = append_heartbeat_observation(tmp_path, "tasks", "overdue Asana tasks")
        assert status2 is ObservationAppendStatus.WRITTEN

        data = read_working_memory(tmp_path)
        assert (
            f"- [{today}] [calendar] meeting within 4h — 1 upcoming, 3 today"
            in data.heartbeat_observations
        )
        assert f"- [{today}] [tasks] overdue Asana tasks" in data.heartbeat_observations

    def test_stable_subject_dedup_when_only_detail_changes(self, tmp_path):
        from living_memory import (
            ObservationAppendStatus,
            append_heartbeat_observation,
            read_working_memory,
        )

        s1 = append_heartbeat_observation(
            tmp_path, "email", "urgent email waiting", "2 urgent, 41 unread"
        )
        s2 = append_heartbeat_observation(
            tmp_path, "email", "urgent email waiting", "5 urgent, 99 unread"
        )
        assert s1 is ObservationAppendStatus.WRITTEN
        assert s2 is ObservationAppendStatus.DEDUP
        data = read_working_memory(tmp_path)
        assert len(data.heartbeat_observations) == 1
        assert "41 unread" in data.heartbeat_observations[0]

    def test_empty_after_sanitize_writes_nothing(self, tmp_path):
        from living_memory import (
            ObservationAppendStatus,
            append_heartbeat_observation,
        )

        status = append_heartbeat_observation(tmp_path, "email", "\x01\n\t ", "detail")
        assert status is ObservationAppendStatus.EMPTY_AFTER_SANITIZE
        # Decided before any file I/O — no bootstrap, no write
        assert not (tmp_path / "WORKING.md").exists()

    def test_file_bootstrap_when_working_missing(self, tmp_path):
        from living_memory import append_heartbeat_observation, read_working_memory

        assert not (tmp_path / "WORKING.md").exists()
        append_heartbeat_observation(tmp_path, "finance", "bills due within 3 days", "1 bill(s)")
        data = read_working_memory(tmp_path)
        assert data.exists
        assert len(data.heartbeat_observations) == 1
        # Reserved-phase wording is gone from the bootstrapped template
        assert "RESERVED for Phase 3" not in data.raw_content

    def test_section_cap_overflow_archives_oldest_insert_only(self, tmp_path):
        from living_memory import append_heartbeat_observation, read_working_memory

        for i in range(4):
            append_heartbeat_observation(
                tmp_path, "tasks", f"distinct subject number {i}", "d", cap=3
            )
        data = read_working_memory(tmp_path)
        assert len(data.heartbeat_observations) == 3
        # Oldest (first-inserted) moved to archive, never deleted
        assert any("distinct subject number 0" in b for b in data.archived)
        assert all("number 0" not in b for b in data.heartbeat_observations)

    def test_in_write_aging_moves_stale_bullets_before_append(self, tmp_path):
        from living_memory import append_heartbeat_observation, read_working_memory

        old = (date.today() - timedelta(days=9)).isoformat()
        fresh = (date.today() - timedelta(days=1)).isoformat()
        content = _wm_with_observations(
            [
                f"- [{old}] [calendar] busy calendar day — 6 events today",
                f"- [{fresh}] [tasks] overdue Asana tasks — 2 overdue, 1 due soon",
            ]
        )
        (tmp_path / "WORKING.md").write_text(content, encoding="utf-8")

        append_heartbeat_observation(
            tmp_path, "email", "unread backlog high", "60 unread", age_days=7
        )
        data = read_working_memory(tmp_path)
        # Stale bullet aged to Archived (Cold) in the same write
        assert any(
            "[calendar] busy calendar day" in b and "[archived" in b
            for b in data.archived
        )
        assert all(
            "busy calendar day" not in b for b in data.heartbeat_observations
        )
        # Fresh bullet + new append both live
        assert any("overdue Asana tasks" in b for b in data.heartbeat_observations)
        assert any("unread backlog high" in b for b in data.heartbeat_observations)

    def test_in_write_aging_persists_even_on_dedup_skip(self, tmp_path):
        from living_memory import (
            ObservationAppendStatus,
            append_heartbeat_observation,
            read_working_memory,
        )

        old = (date.today() - timedelta(days=9)).isoformat()
        today = date.today().isoformat()
        content = _wm_with_observations(
            [
                f"- [{old}] [calendar] busy calendar day — 6 events today",
                f"- [{today}] [email] unread backlog high — 60 unread",
            ]
        )
        (tmp_path / "WORKING.md").write_text(content, encoding="utf-8")

        status = append_heartbeat_observation(
            tmp_path, "email", "unread backlog high", "70 unread", age_days=7
        )
        assert status is ObservationAppendStatus.DEDUP
        data = read_working_memory(tmp_path)
        # Aging still landed despite the dedup skip
        assert any("busy calendar day" in b for b in data.archived)
        assert all("busy calendar day" not in b for b in data.heartbeat_observations)

    def test_manual_bullets_in_other_sections_untouched(self, tmp_path):
        from living_memory import append_heartbeat_observation, read_working_memory

        old = (date.today() - timedelta(days=30)).isoformat()
        content = _wm_with_observations(
            [],
            threads=(f"- [{old}] manual thread kept by hand — open",),
            hypotheses=(f"- [{old}] manual hypothesis — evidence: [[X]]",),
        )
        (tmp_path / "WORKING.md").write_text(content, encoding="utf-8")

        append_heartbeat_observation(
            tmp_path, "calendar", "meeting within 4h", "1 upcoming"
        )
        data = read_working_memory(tmp_path)
        # In-write aging touches ONLY the observations section — a 30-day-old
        # manual thread/hypothesis survives an observation write untouched.
        assert any("manual thread kept by hand" in b for b in data.open_threads)
        assert any("manual hypothesis" in b for b in data.active_hypotheses)
        assert data.archived == []

    def test_existing_appends_unchanged_no_aging_int_return(self, tmp_path):
        from living_memory import (
            append_hypothesis,
            append_open_thread,
            append_question,
            read_working_memory,
        )

        old = (date.today() - timedelta(days=9)).isoformat()
        content = _wm_with_observations(
            [], threads=(f"- [{old}] nine day old thread — open",)
        )
        (tmp_path / "WORKING.md").write_text(content, encoding="utf-8")

        result = append_open_thread(tmp_path, "fresh thread subject", "open")
        assert result == 1 and isinstance(result, int)
        assert isinstance(append_hypothesis(tmp_path, "some hypothesis", "x"), int)
        assert isinstance(append_question(tmp_path, "some question?"), int)

        data = read_working_memory(tmp_path)
        # No in-write aging on the existing appends (age_days=None default):
        # the 9-day-old thread is still active, not archived.
        assert any("nine day old thread" in b for b in data.open_threads)
        assert data.archived == []

    def test_knobs_resolve_env_at_call_time_without_reload(self, tmp_path, monkeypatch):
        from living_memory import (
            ObservationAppendStatus,
            append_heartbeat_observation,
            read_working_memory,
        )

        # dedup window knob: a 2-day-old bullet dedups at the default (3) but
        # writes when the env narrows the window to 1 — no module reload.
        two_days = (date.today() - timedelta(days=2)).isoformat()
        content = _wm_with_observations(
            [f"- [{two_days}] [email] urgent email waiting — 1 urgent, 5 unread"]
        )
        (tmp_path / "WORKING.md").write_text(content, encoding="utf-8")
        monkeypatch.setenv("HEARTBEAT_OBSERVATION_DEDUP_DAYS", "3")
        assert (
            append_heartbeat_observation(
                tmp_path, "email", "urgent email waiting", "2 urgent"
            )
            is ObservationAppendStatus.DEDUP
        )
        monkeypatch.setenv("HEARTBEAT_OBSERVATION_DEDUP_DAYS", "1")
        assert (
            append_heartbeat_observation(
                tmp_path, "email", "urgent email waiting", "2 urgent"
            )
            is ObservationAppendStatus.WRITTEN
        )

        # cap knob
        monkeypatch.setenv("HEARTBEAT_OBSERVATION_CAP", "2")
        append_heartbeat_observation(tmp_path, "tasks", "cap probe subject", "d")
        data = read_working_memory(tmp_path)
        assert len(data.heartbeat_observations) == 2

        # age knob: narrow to 1 day → the 2-day-old bullet ages on next write
        monkeypatch.setenv("HEARTBEAT_OBSERVATION_AGE_DAYS", "1")
        append_heartbeat_observation(tmp_path, "calendar", "age probe subject", "d")
        data = read_working_memory(tmp_path)
        assert all(two_days not in b for b in data.heartbeat_observations)


class TestObservationDreamAging:
    def test_archive_ages_observations_with_own_window(self, tmp_path):
        from living_memory import archive_stale_working_items, read_working_memory

        d3 = (date.today() - timedelta(days=3)).isoformat()
        content = _wm_with_observations(
            [f"- [{d3}] [calendar] busy calendar day — 6 events today"],
            threads=(f"- [{d3}] three day old thread — open",),
        )
        (tmp_path / "WORKING.md").write_text(content, encoding="utf-8")

        report = archive_stale_working_items(tmp_path, days=7, observation_days=2)
        assert report.archived_count == 1
        assert report.sections_touched == ["Heartbeat Observations"]
        data = read_working_memory(tmp_path)
        # Observation aged at its own (2d) window; thread kept by the 7d window
        assert data.heartbeat_observations == []
        assert any("three day old thread" in b for b in data.open_threads)
        assert any("busy calendar day" in b for b in data.archived)

    def test_observation_days_env_resolved_when_none(self, tmp_path, monkeypatch):
        from living_memory import archive_stale_working_items, read_working_memory

        d3 = (date.today() - timedelta(days=3)).isoformat()
        content = _wm_with_observations(
            [f"- [{d3}] [tasks] overdue Asana tasks — 1 overdue"]
        )
        (tmp_path / "WORKING.md").write_text(content, encoding="utf-8")
        monkeypatch.setenv("HEARTBEAT_OBSERVATION_AGE_DAYS", "2")
        report = archive_stale_working_items(tmp_path, days=7)
        assert report.archived_count == 1
        assert "Heartbeat Observations" in report.sections_touched
        data = read_working_memory(tmp_path)
        assert data.heartbeat_observations == []

    def test_act1_caller_signature_still_works(self, tmp_path):
        """memory_dream.py's exact call shape (days= only) needs zero changes."""
        from living_memory import archive_stale_working_items

        path = tmp_path / "WORKING.md"
        path.write_text(_fresh_sample_wm(), encoding="utf-8")
        report = archive_stale_working_items(tmp_path, days=7)
        assert report.archived_count == 0  # all bullets fresh
        assert report.sections_touched == []

    def test_fresh_observations_survive_dream_aging(self, tmp_path):
        from living_memory import archive_stale_working_items, read_working_memory

        d1 = (date.today() - timedelta(days=1)).isoformat()
        content = _wm_with_observations(
            [f"- [{d1}] [finance] bills due within 3 days — 2 bill(s)"]
        )
        (tmp_path / "WORKING.md").write_text(content, encoding="utf-8")
        archive_stale_working_items(tmp_path, days=7, observation_days=7)
        data = read_working_memory(tmp_path)
        assert len(data.heartbeat_observations) == 1


class TestObservationBriefing:
    def test_briefing_includes_observations_line(self, tmp_path):
        from living_memory import append_heartbeat_observation, build_briefing_section

        append_heartbeat_observation(
            tmp_path, "calendar", "meeting within 4h", "1 upcoming, 3 today"
        )
        section = build_briefing_section(tmp_path)
        assert "Heartbeat observations:" in section
        assert "[calendar] meeting within 4h" in section

    def test_briefing_omits_line_when_section_empty(self, tmp_path):
        from living_memory import append_open_thread, build_briefing_section

        append_open_thread(tmp_path, "some open thread", "open")
        section = build_briefing_section(tmp_path)
        assert "Heartbeat observations:" not in section


# =============================================================================
# Living Mind Act 4 — deferred Act 2 pickup: living_memory_read span carries
# observations_count in BOTH branches (accessor-patch pattern, Rule 3).
# =============================================================================


WM_WITH_OBSERVATION = """---
tags: [system, memory, working]
status: current
date: 2026-06-12
summary: "test"
---

# WORKING.md

## Open Threads

- [2026-06-12] thread alpha

## Active Hypotheses

## Unresolved Questions

## Heartbeat Observations

- [2026-06-12] [calendar] busy day: 5 events

## Archived (Cold)
"""


class TestReadSpanObservationsCount:
    def test_read_span_carries_observations_count(self, tmp_path, monkeypatch):
        (tmp_path / "WORKING.md").write_text(
            WM_WITH_OBSERVATION, encoding="utf-8"
        )
        fake_client, fake_span = _make_fake_client()
        monkeypatch.setattr(
            langfuse_setup, "get_observation_client", lambda: fake_client
        )

        from living_memory import read_working_memory
        result = read_working_memory(tmp_path)

        assert result.exists is True
        assert len(result.heartbeat_observations) == 1
        metadata = fake_span.update.call_args.kwargs["metadata"]
        assert metadata["observations_count"] == 1

    def test_missing_file_branch_reports_zero_observations(
        self, tmp_path, monkeypatch
    ):
        fake_client, fake_span = _make_fake_client()
        monkeypatch.setattr(
            langfuse_setup, "get_observation_client", lambda: fake_client
        )

        from living_memory import read_working_memory
        result = read_working_memory(tmp_path)

        assert result.exists is False
        metadata = fake_span.update.call_args.kwargs["metadata"]
        assert metadata["observations_count"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
