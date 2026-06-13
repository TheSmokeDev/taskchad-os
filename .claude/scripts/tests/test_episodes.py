"""Tests for episodes.py — Living Mind Act 3 (the self's autobiography).

Test design split by code path (categories map to the PRP's validation plan):
  1. Settings resolver — Rule 1 call-time resolution, locked defaults.
  3. derive_flush_meta — exact surface table, lifecycle parse, sha1 fallback.
  4. parse_flush_sections — provider-tolerant heading split.
  5. Writer lifecycle key (R1 B2 + M3) — fixtures name stable session_id /
     lifecycle_id / retry explicitly; EVERY case asserts PHYSICAL FILENAMES.
  6. Privacy (discriminating) — transcript sentinel never reaches episodes.
  +  list_open_episodes / render_episodes_digest / mark_episodes_consolidated.
 10. Index + recall reach (R1 M4) — identity-asserted real index + search.
  E2E — synthetic flush -> episode -> gather -> consolidate prompt -> flip.

No test touches live vault/state files — all paths are tmp_path-scoped;
clocks are injected via ``now=``. Born-clean fixtures (R2 NM2): all ids are
the synthetic ``telegram:1111111111:2222222222`` family.
"""

from __future__ import annotations

import json
import re
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config  # noqa: E402
import episodes as ep  # noqa: E402
from episodes import (  # noqa: E402
    EpisodeFlipError,
    EpisodeWriteStatus,
    derive_flush_meta,
    list_open_episodes,
    mark_episodes_consolidated,
    parse_flush_sections,
    read_episode_frontmatter,
    render_episodes_digest,
    write_episode_from_flush,
)

SYNTHETIC_SAFE_ID = "telegram-1111111111-2222222222"
SYNTHETIC_UUID = "11784e97-1111-2222-3333-444444444444"

# Lifecycle fixtures named explicitly (R1 M3): same stable session_id, two
# distinct lifecycle ids (hook-run stem timestamps), plus the retry case.
CTX_LIFECYCLE_A = f"session-flush-{SYNTHETIC_SAFE_ID}-20260612-100000.md"
CTX_LIFECYCLE_B = f"session-flush-{SYNTHETIC_SAFE_ID}-20260612-180000.md"
CTX_RETRY_A = CTX_LIFECYCLE_A  # identical context filename = same lifecycle
CTX_MIDNIGHT = f"session-flush-{SYNTHETIC_SAFE_ID}-20260612-235930.md"

GOOD_RESPONSE = """## Summary

We shipped the episodes module and fixed the win32 flush filename defect.
The session moved fast and every gate stayed green.

## Key Decisions

- Key decision: episodes contain the LLM summary, never the transcript.
- Lesson learned: sanitize at filename composition, compare raw with raw.

## Open Threads

- TODO: verify recall reach through the real index.

## Texture

Focused session with steady momentum.
"""

EPISODE_ENV_VARS = (
    "EPISODE_MIN_CHARS",
    "EPISODE_MAX_PER_DAY",
    "EPISODE_DREAM_MAX_FILES",
    "EPISODE_DREAM_MAX_CHARS_PER",
    "EPISODE_DREAM_MAX_TOTAL_CHARS",
)


def _sweep_episode_env(monkeypatch) -> None:
    for var in EPISODE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _dt(year, month, day, hour=12, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second)


def _sid8(session_id: str) -> str:
    import hashlib

    return hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:8]


# =============================================================================
# Category 1 — settings resolver (Rule 1)
# =============================================================================


class TestEpisodeSettings:
    def test_defaults_with_env_swept(self, monkeypatch):
        """Env deleted -> the documented locked defaults."""
        _sweep_episode_env(monkeypatch)
        s = config.get_episode_settings()
        assert s == config.EpisodeSettings(
            min_chars=80,
            max_per_day=20,
            dream_max_files=10,
            dream_max_chars_per=600,
            dream_max_total_chars=4000,
        )

    def test_env_overrides_resolve_at_call_time_without_reload(self, monkeypatch):
        _sweep_episode_env(monkeypatch)
        before = config.get_episode_settings()
        assert before.min_chars == 80
        monkeypatch.setenv("EPISODE_MIN_CHARS", "5")
        monkeypatch.setenv("EPISODE_MAX_PER_DAY", "3")
        monkeypatch.setenv("EPISODE_DREAM_MAX_FILES", "2")
        monkeypatch.setenv("EPISODE_DREAM_MAX_CHARS_PER", "50")
        monkeypatch.setenv("EPISODE_DREAM_MAX_TOTAL_CHARS", "120")
        after = config.get_episode_settings()
        assert after == config.EpisodeSettings(5, 3, 2, 50, 120)

    def test_explicit_args_win_over_env(self, monkeypatch):
        monkeypatch.setenv("EPISODE_MIN_CHARS", "5")
        s = config.get_episode_settings(min_chars=99)
        assert s.min_chars == 99

    def test_no_episode_import_time_globals(self):
        """Rule 1 — none of the knobs exist as config module constants."""
        for var in EPISODE_ENV_VARS:
            assert not hasattr(config, var)


# =============================================================================
# Category 3 — derive_flush_meta (surface table + lifecycle key)
# =============================================================================


class TestDeriveFlushMeta:
    @pytest.mark.parametrize(
        "platform", ["telegram", "discord", "slack", "whatsapp", "web", "cli"]
    )
    def test_platform_tokens_map_to_surface(self, platform):
        name = f"session-flush-{platform}-1111111111-2222222222-20260612-100000.md"
        meta = derive_flush_meta(name)
        assert meta.surface == platform
        assert meta.session_id == f"{platform}-1111111111-2222222222"

    def test_uuid_maps_to_code(self):
        meta = derive_flush_meta(
            f"session-flush-{SYNTHETIC_UUID}-20260612-100000.md"
        )
        assert meta.surface == "code"
        assert meta.session_id == SYNTHETIC_UUID

    def test_flush_context_prefix_maps_to_compact(self):
        meta = derive_flush_meta(
            f"flush-context-{SYNTHETIC_UUID}-20260612-100000.md"
        )
        assert meta.surface == "compact"

    def test_lifecycle_parse_exact(self):
        meta = derive_flush_meta(CTX_LIFECYCLE_A)
        assert meta.lifecycle_ts == "20260612-100000"
        assert meta.episode_date == "2026-06-12"
        assert meta.time_token == "100000"

    def test_sid8_stable_for_same_id_distinct_for_distinct(self):
        a1 = derive_flush_meta(CTX_LIFECYCLE_A)
        a2 = derive_flush_meta(CTX_LIFECYCLE_B)
        other = derive_flush_meta(
            "session-flush-telegram-1111111111-3333333333-20260612-100000.md"
        )
        assert a1.sid8 == a2.sid8  # same stable session_id, different lifecycle
        assert a1.sid8 != other.sid8
        assert re.fullmatch(r"[0-9a-f]{8}", a1.sid8)

    def test_malformed_stem_deterministic_fallback(self):
        now = _dt(2026, 6, 12)
        meta1 = derive_flush_meta("session-flush.md", now=now)
        meta2 = derive_flush_meta("session-flush.md", now=now)
        other = derive_flush_meta("garbage.md", now=now)
        assert meta1.session_id == "unknown"
        assert meta1.time_token.startswith("u")
        assert len(meta1.time_token) == 8
        assert meta1.episode_date == "2026-06-12"  # today via injected now
        assert meta1.lifecycle_ts == "unknown-" + meta1.time_token
        # Same stem -> same key; distinct stems -> distinct keys.
        assert meta1.time_token == meta2.time_token
        assert meta1.time_token != other.time_token

    def test_malformed_timestamp_tail_never_raises(self):
        meta = derive_flush_meta(
            "session-flush-telegram-1111111111-2222222222-notadate-badtime.md",
            now=_dt(2026, 6, 12),
        )
        assert meta.time_token.startswith("u")
        assert meta.episode_date == "2026-06-12"


# =============================================================================
# Category 4 — parse_flush_sections (provider tolerance)
# =============================================================================


class TestParseFlushSections:
    def test_all_four_headings(self):
        sections = parse_flush_sections(GOOD_RESPONSE)
        assert set(sections) == {"Summary", "Key Decisions", "Open Threads", "Texture"}
        assert "win32 flush filename defect" in sections["Summary"]
        assert "never the transcript" in sections["Key Decisions"]
        assert "recall reach" in sections["Open Threads"]
        assert "steady momentum" in sections["Texture"]

    def test_subset_of_headings(self):
        text = "## Summary\n\nShort session.\n\n## Open Threads\n\n- follow up\n"
        sections = parse_flush_sections(text)
        assert set(sections) == {"Summary", "Open Threads"}

    def test_no_headings_falls_back_to_summary(self):
        text = "- bullet one about a decision\n- bullet two about a lesson\n"
        sections = parse_flush_sections(text)
        assert set(sections) == {"Summary"}
        assert "bullet one" in sections["Summary"]

    def test_preamble_joins_summary(self):
        text = "Quick note before headings.\n\n## Summary\n\nThe real summary.\n"
        sections = parse_flush_sections(text)
        assert "Quick note before headings." in sections["Summary"]
        assert "The real summary." in sections["Summary"]

    def test_unrecognized_h2_kept_verbatim(self):
        text = (
            "## Summary\n\nMain story.\n\n## Random Provider Section\n\n"
            "- kept content\n\n## Open Threads\n\n- thread\n"
        )
        sections = parse_flush_sections(text)
        assert "## Random Provider Section" in sections["Summary"]
        assert "kept content" in sections["Summary"]
        assert "thread" in sections["Open Threads"]

    def test_case_insensitive_headings(self):
        text = "## SUMMARY\n\nLoud summary.\n\n## key decisions\n\n- quiet decision\n"
        sections = parse_flush_sections(text)
        assert sections["Summary"] == "Loud summary."
        assert "quiet decision" in sections["Key Decisions"]

    @pytest.mark.parametrize(
        "variant",
        [
            "**Summary:** bold pseudo-heading style\n- a bullet\n",
            "##   Summary   \n\nextra whitespace heading\n",
            "## Summary: with a colon suffix\ncontent line\n",
            "",
            "    \n\n  ",
            "## Texture\n",
        ],
    )
    def test_provider_variance_never_raises(self, variant):
        sections = parse_flush_sections(variant)
        assert isinstance(sections, dict)
        assert set(sections) <= {"Summary", "Key Decisions", "Open Threads", "Texture"}


# =============================================================================
# Category 5 — writer lifecycle key (R1 B2 + M3, physical filenames)
# =============================================================================


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir()
    return v


def _settings(**overrides) -> config.EpisodeSettings:
    base = dict(
        min_chars=40,
        max_per_day=20,
        dream_max_files=10,
        dream_max_chars_per=600,
        dream_max_total_chars=4000,
    )
    base.update(overrides)
    return config.EpisodeSettings(**base)


class TestWriterLifecycleKey:
    def test_lifecycle_a_written_with_exact_filename_and_frontmatter(self, vault):
        status, path = write_episode_from_flush(
            vault,
            context_filename=CTX_LIFECYCLE_A,
            response_text=GOOD_RESPONSE,
            now=_dt(2026, 6, 12, 10, 1),
            settings=_settings(),
        )
        assert status is EpisodeWriteStatus.WRITTEN
        expected = f"2026-06-12-telegram-{_sid8(SYNTHETIC_SAFE_ID)}-100000.md"
        assert path is not None and path.name == expected
        assert path.parent.name == "episodes"

        fm = read_episode_frontmatter(path)
        assert fm["status"] == "open"
        assert fm["date"] == "2026-06-12"
        assert fm["session_id"] == SYNTHETIC_SAFE_ID
        assert fm["surface"] == "telegram"
        assert fm["lifecycle"] == "20260612-100000"
        assert "consolidated_at" not in fm

        content = path.read_text(encoding="utf-8")
        # Lint contract: tags + date required; tags exactly the schema trio.
        assert "tags: [system, memory, living-mind]" in content
        tags_match = re.search(r"tags:\s*\[([^\]]*)\]", content)
        assert tags_match is not None
        assert [t.strip() for t in tags_match.group(1).split(",")] == [
            "system",
            "memory",
            "living-mind",
        ]
        assert "## Summary" in content
        assert "## Key Decisions" in content

    def test_second_lifecycle_same_channel_same_day_writes_second_file(self, vault):
        """The B2 discriminator: stable session key must NOT merge lifecycles."""
        now = _dt(2026, 6, 12, 18, 1)
        s1, p1 = write_episode_from_flush(
            vault,
            context_filename=CTX_LIFECYCLE_A,
            response_text=GOOD_RESPONSE,
            now=now,
            settings=_settings(),
        )
        s2, p2 = write_episode_from_flush(
            vault,
            context_filename=CTX_LIFECYCLE_B,
            response_text=GOOD_RESPONSE,
            now=now,
            settings=_settings(),
        )
        assert (s1, s2) == (EpisodeWriteStatus.WRITTEN, EpisodeWriteStatus.WRITTEN)
        sid8 = _sid8(SYNTHETIC_SAFE_ID)
        names = sorted(f.name for f in (vault / "episodes").glob("*.md"))
        assert names == [
            f"2026-06-12-telegram-{sid8}-100000.md",
            f"2026-06-12-telegram-{sid8}-180000.md",
        ]
        assert p1 is not None and p2 is not None and p1 != p2

    def test_retry_same_lifecycle_updates_single_file(self, vault):
        write_episode_from_flush(
            vault,
            context_filename=CTX_LIFECYCLE_A,
            response_text=GOOD_RESPONSE,
            now=_dt(2026, 6, 12, 10, 1),
            settings=_settings(),
        )
        # Flip it consolidated first so the retry proves re-open semantics.
        episode = next((vault / "episodes").glob("*.md"))
        assert mark_episodes_consolidated([episode], now=_dt(2026, 6, 12, 11, 0)) == 1
        assert read_episode_frontmatter(episode)["status"] == "consolidated"
        assert "consolidated_at" in read_episode_frontmatter(episode)

        status, path = write_episode_from_flush(
            vault,
            context_filename=CTX_RETRY_A,
            response_text="## Summary\n\nRetry distillation of the same lifecycle "
            "with fresh wording and enough length to clear the floor.\n\n"
            "## Key Decisions\n\n- retry decision\n",
            now=_dt(2026, 6, 13, 9, 30),
            settings=_settings(),
        )
        assert status is EpisodeWriteStatus.UPDATED
        files = list((vault / "episodes").glob("*.md"))
        assert len(files) == 1  # file count unchanged
        assert path == files[0] == episode

        content = episode.read_text(encoding="utf-8")
        fm = read_episode_frontmatter(episode)
        assert "## Update (09:30)" in content
        assert "### Summary" in content  # demoted headings in the update block
        assert "### Key Decisions" in content
        assert fm["date"] == "2026-06-13"  # refreshed
        assert fm["status"] == "open"  # re-opened
        assert "consolidated_at" not in fm  # removed
        assert "consolidated_at" not in content
        # Original first-write blocks survive above the update.
        assert "## Summary" in content

    def test_midnight_crossover_stays_in_one_file(self, vault):
        """Filename date = lifecycle START date, never write-time now."""
        status, path = write_episode_from_flush(
            vault,
            context_filename=CTX_MIDNIGHT,
            response_text=GOOD_RESPONSE,
            now=_dt(2026, 6, 13, 0, 5),  # post-midnight write
            settings=_settings(),
        )
        assert status is EpisodeWriteStatus.WRITTEN
        sid8 = _sid8(SYNTHETIC_SAFE_ID)
        assert path is not None
        assert path.name == f"2026-06-12-telegram-{sid8}-235930.md"

        # Post-midnight retry of the same stem lands in the SAME file.
        status2, path2 = write_episode_from_flush(
            vault,
            context_filename=CTX_MIDNIGHT,
            response_text=GOOD_RESPONSE,
            now=_dt(2026, 6, 13, 0, 20),
            settings=_settings(),
        )
        assert status2 is EpisodeWriteStatus.UPDATED
        assert path2 == path
        assert not list((vault / "episodes").glob("2026-06-13-*.md"))

    def test_min_chars_skip_writes_no_file(self, vault):
        status, path = write_episode_from_flush(
            vault,
            context_filename=CTX_LIFECYCLE_A,
            response_text="## Summary\n\ntiny\n",
            now=_dt(2026, 6, 12),
            settings=_settings(min_chars=80),
        )
        assert status is EpisodeWriteStatus.SKIPPED_MIN_CHARS
        assert path is None
        assert not (vault / "episodes").exists() or not list(
            (vault / "episodes").glob("*.md")
        )

    def test_day_cap_blocks_new_files_but_not_updates(self, vault):
        settings = _settings(max_per_day=2)
        write_episode_from_flush(
            vault,
            context_filename=CTX_LIFECYCLE_A,
            response_text=GOOD_RESPONSE,
            now=_dt(2026, 6, 12, 10, 1),
            settings=settings,
        )
        write_episode_from_flush(
            vault,
            context_filename=CTX_LIFECYCLE_B,
            response_text=GOOD_RESPONSE,
            now=_dt(2026, 6, 12, 18, 1),
            settings=settings,
        )
        # Third NEW key on the same lifecycle-date -> capped.
        status, path = write_episode_from_flush(
            vault,
            context_filename=f"session-flush-{SYNTHETIC_SAFE_ID}-20260612-200000.md",
            response_text=GOOD_RESPONSE,
            now=_dt(2026, 6, 12, 20, 1),
            settings=settings,
        )
        assert status is EpisodeWriteStatus.SKIPPED_DAY_CAP
        assert path is None
        assert len(list((vault / "episodes").glob("*.md"))) == 2
        # UPDATE of an existing key still lands at cap.
        status_u, _ = write_episode_from_flush(
            vault,
            context_filename=CTX_LIFECYCLE_A,
            response_text=GOOD_RESPONSE,
            now=_dt(2026, 6, 12, 20, 5),
            settings=settings,
        )
        assert status_u is EpisodeWriteStatus.UPDATED

    def test_atomic_write_leaves_no_tmp_residue(self, vault):
        write_episode_from_flush(
            vault,
            context_filename=CTX_LIFECYCLE_A,
            response_text=GOOD_RESPONSE,
            now=_dt(2026, 6, 12),
            settings=_settings(),
        )
        assert not list((vault / "episodes").glob("*.tmp"))

    def test_file_lock_timeout_raises_out_of_the_primitive(self, vault, monkeypatch):
        """The primitive does NOT swallow lock contention — the CALLER's
        try/except is what is fail-open (proven in the flush gate tests)."""

        @contextmanager
        def _busy_lock(_path, timeout=5.0):
            raise TimeoutError("lock busy")
            yield  # pragma: no cover

        monkeypatch.setattr(ep, "file_lock", _busy_lock)
        with pytest.raises(TimeoutError):
            write_episode_from_flush(
                vault,
                context_filename=CTX_LIFECYCLE_A,
                response_text=GOOD_RESPONSE,
                now=_dt(2026, 6, 12),
                settings=_settings(),
            )

    def test_writer_mkdirs_missing_episodes_dir(self, tmp_path):
        """Migration-free: existing vaults without episodes/ need zero setup."""
        fresh_vault = tmp_path / "fresh"
        fresh_vault.mkdir()
        status, path = write_episode_from_flush(
            fresh_vault,
            context_filename=CTX_LIFECYCLE_A,
            response_text=GOOD_RESPONSE,
            now=_dt(2026, 6, 12),
            settings=_settings(),
        )
        assert status is EpisodeWriteStatus.WRITTEN
        assert path is not None and path.exists()


# =============================================================================
# Category 6 — privacy (discriminating sentinel)
# =============================================================================


class TestPrivacy:
    def test_transcript_sentinel_never_reaches_episode(self, tmp_path, vault):
        """The context FILE carries the sentinel; the writer receives only the
        FILENAME — the sentinel must not appear in the episode."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        leftover = state_dir / CTX_LIFECYCLE_A
        leftover.write_text(
            "**User:** ZZTRANSCRIPT_SENTINEL secret raw transcript line\n",
            encoding="utf-8",
        )

        status, path = write_episode_from_flush(
            vault,
            context_filename=leftover.name,
            response_text=GOOD_RESPONSE,  # sentinel absent from the LLM response
            now=_dt(2026, 6, 12),
            settings=_settings(),
        )
        assert status is EpisodeWriteStatus.WRITTEN
        assert path is not None
        episode_content = path.read_text(encoding="utf-8")
        assert "ZZTRANSCRIPT_SENTINEL" not in episode_content
        # The raw leftover is untouched and unread (still exists verbatim).
        assert "ZZTRANSCRIPT_SENTINEL" in leftover.read_text(encoding="utf-8")


# =============================================================================
# list_open_episodes / render_episodes_digest / mark_episodes_consolidated
# =============================================================================


def _write_episode_file(
    vault: Path,
    name: str,
    *,
    status: str = "open",
    date: str = "2026-06-12",
    body: str = "## Key Decisions\n\n- lesson learned: probe\n",
    consolidated_at: str | None = None,
) -> Path:
    episodes_dir = vault / "episodes"
    episodes_dir.mkdir(parents=True, exist_ok=True)
    consolidated_line = (
        f"consolidated_at: {consolidated_at}\n" if consolidated_at else ""
    )
    path = episodes_dir / name
    path.write_text(
        "---\n"
        "tags: [system, memory, living-mind]\n"
        f"status: {status}\n"
        f"{consolidated_line}"
        f"date: {date}\n"
        f'session_id: "{SYNTHETIC_SAFE_ID}"\n'
        "surface: telegram\n"
        'lifecycle: "20260612-100000"\n'
        'summary: "fixture"\n'
        "---\n\n"
        "# Episode: fixture\n\n"
        f"{body}",
        encoding="utf-8",
    )
    return path


class TestListOpenEpisodes:
    def test_open_in_window_listed_newest_first(self, vault):
        old = _write_episode_file(vault, "2026-06-10-telegram-aaaa1111-090000.md", date="2026-06-10")
        new = _write_episode_file(vault, "2026-06-12-telegram-aaaa1111-100000.md", date="2026-06-12")
        listed = list_open_episodes(vault, days=7, now=_dt(2026, 6, 12))
        assert [p.name for p in listed] == [new.name, old.name]

    def test_consolidated_excluded(self, vault):
        _write_episode_file(
            vault,
            "2026-06-12-telegram-aaaa1111-100000.md",
            status="consolidated",
            consolidated_at="2026-06-12",
        )
        assert list_open_episodes(vault, days=7, now=_dt(2026, 6, 12)) == []

    def test_out_of_window_excluded(self, vault):
        _write_episode_file(
            vault, "2026-06-01-telegram-aaaa1111-100000.md", date="2026-06-01"
        )
        assert list_open_episodes(vault, days=7, now=_dt(2026, 6, 12)) == []

    def test_future_dated_excluded(self, vault):
        _write_episode_file(
            vault, "2026-06-20-telegram-aaaa1111-100000.md", date="2026-06-20"
        )
        assert list_open_episodes(vault, days=7, now=_dt(2026, 6, 12)) == []

    def test_missing_dir_returns_empty(self, tmp_path):
        assert list_open_episodes(tmp_path / "no-vault", days=7) == []


def _write_since_episode(
    vault: Path,
    name: str,
    *,
    status: str = "open",
    date: str = "2026-06-12",
    lifecycle: str = "20260612-100000",
) -> Path:
    """Since-query fixture writer — lifecycle is the discriminator here."""
    episodes_dir = vault / "episodes"
    episodes_dir.mkdir(parents=True, exist_ok=True)
    path = episodes_dir / name
    path.write_text(
        "---\n"
        "tags: [system, memory, living-mind]\n"
        f"status: {status}\n"
        f"date: {date}\n"
        f'session_id: "{SYNTHETIC_SAFE_ID}"\n'
        "surface: telegram\n"
        f'lifecycle: "{lifecycle}"\n'
        'summary: "fixture"\n'
        "---\n\n"
        "# Episode: fixture\n\n## Summary\n\nbody\n",
        encoding="utf-8",
    )
    return path


class TestListEpisodesSince:
    """Living Mind Act 4 category 7 — the status-AGNOSTIC since-query."""

    SINCE = datetime(2026, 6, 11, 22, 0)

    def test_strict_after_lifecycle_compare(self, vault):
        _write_since_episode(
            vault,
            "2026-06-11-telegram-aaaa1111-215959.md",
            date="2026-06-11",
            lifecycle="20260611-215959",
        )
        _write_since_episode(
            vault,
            "2026-06-11-telegram-aaaa1111-220000.md",
            date="2026-06-11",
            lifecycle="20260611-220000",
        )
        kept = _write_since_episode(
            vault,
            "2026-06-11-telegram-aaaa1111-220001.md",
            date="2026-06-11",
            lifecycle="20260611-220001",
        )
        listed = ep.list_episodes_since(vault, since=self.SINCE)
        assert [p.name for p in listed] == [kept.name]

    def test_status_agnostic_consolidated_counts(self, vault):
        kept = _write_since_episode(
            vault,
            "2026-06-12-telegram-aaaa1111-053000.md",
            status="consolidated",
            lifecycle="20260612-053000",
        )
        listed = ep.list_episodes_since(vault, since=self.SINCE)
        assert [p.name for p in listed] == [kept.name]

    def test_malformed_lifecycle_falls_back_to_date(self, vault):
        kept = _write_since_episode(
            vault,
            "2026-06-12-telegram-aaaa1111-100000.md",
            date="2026-06-12",
            lifecycle="unknown-20260612",
        )
        _write_since_episode(
            vault,
            "2026-06-10-telegram-aaaa1111-100000.md",
            date="2026-06-10",
            lifecycle="unknown-20260610",
        )
        listed = ep.list_episodes_since(vault, since=self.SINCE)
        # date >= since.date() keeps the 06-12 file; 06-10 is out. The
        # boundary-date file (06-11) is also kept by the day-floor fallback:
        assert [p.name for p in listed] == [kept.name]

    def test_date_fallback_day_floor_includes_boundary_date(self, vault):
        kept = _write_since_episode(
            vault,
            "2026-06-11-telegram-aaaa1111-100000.md",
            date="2026-06-11",
            lifecycle="not-a-lifecycle",
        )
        listed = ep.list_episodes_since(vault, since=self.SINCE)
        assert [p.name for p in listed] == [kept.name]

    def test_both_unparseable_skipped(self, vault):
        episodes_dir = vault / "episodes"
        episodes_dir.mkdir(parents=True, exist_ok=True)
        (episodes_dir / "garbage-frontmatter.md").write_text(
            "---\nstatus: open\nlifecycle: nope\ndate: also-nope\n---\n\nbody\n",
            encoding="utf-8",
        )
        assert ep.list_episodes_since(vault, since=self.SINCE) == []

    def test_missing_dir_returns_empty(self, tmp_path):
        assert (
            ep.list_episodes_since(tmp_path / "no-vault", since=self.SINCE) == []
        )

    def test_newest_first_ordering(self, vault):
        older = _write_since_episode(
            vault,
            "2026-06-12-telegram-aaaa1111-053000.md",
            lifecycle="20260612-053000",
        )
        newer = _write_since_episode(
            vault,
            "2026-06-12-telegram-aaaa1111-060000.md",
            lifecycle="20260612-060000",
        )
        listed = ep.list_episodes_since(vault, since=self.SINCE)
        assert [p.name for p in listed] == [newer.name, older.name]

    def test_non_episode_garbage_skipped_without_raising(self, vault):
        episodes_dir = vault / "episodes"
        episodes_dir.mkdir(parents=True, exist_ok=True)
        (episodes_dir / "not-an-episode.md").write_text(
            "no frontmatter at all\n", encoding="utf-8"
        )
        kept = _write_since_episode(
            vault,
            "2026-06-12-telegram-aaaa1111-053000.md",
            lifecycle="20260612-053000",
        )
        listed = ep.list_episodes_since(vault, since=self.SINCE)
        assert [p.name for p in listed] == [kept.name]

    def test_frontmatter_reader_exposes_summary(self, vault):
        """Act 4 additive key: the brief renders the frontmatter summary."""
        path = _write_since_episode(
            vault,
            "2026-06-12-telegram-aaaa1111-053000.md",
            lifecycle="20260612-053000",
        )
        fm = read_episode_frontmatter(path)
        assert fm["summary"] == "fixture"


class TestRenderEpisodesDigest:
    def test_empty_paths_empty_digest(self):
        assert render_episodes_digest([], settings=_settings()) == ""

    def test_caps_honored(self, vault):
        paths = [
            _write_episode_file(
                vault,
                f"2026-06-12-telegram-aaaa111{i}-10000{i}.md",
                body="## Key Decisions\n\n- " + ("x" * 500) + "\n",
            )
            for i in range(4)
        ]
        settings = _settings(
            dream_max_files=2, dream_max_chars_per=100, dream_max_total_chars=180
        )
        digest = render_episodes_digest(paths, settings=settings)
        assert len(digest) <= 180 + 2  # join separator tolerance
        assert digest.count("###") <= 2
        # Per-file excerpt cap: the 500-char body never lands whole.
        assert "x" * 200 not in digest

    def test_digest_strips_frontmatter(self, vault):
        path = _write_episode_file(vault, "2026-06-12-telegram-aaaa1111-100000.md")
        digest = render_episodes_digest([path], settings=_settings())
        assert "tags: [system" not in digest
        assert "lesson learned: probe" in digest
        assert digest.startswith(f"### {path.stem}")


class TestMarkEpisodesConsolidated:
    def test_flips_exactly_fed_paths_with_date(self, vault):
        fed = _write_episode_file(vault, "2026-06-12-telegram-aaaa1111-100000.md")
        unfed = _write_episode_file(vault, "2026-06-12-telegram-bbbb2222-110000.md")
        flipped = mark_episodes_consolidated([fed], now=_dt(2026, 6, 12))
        assert flipped == 1
        fm = read_episode_frontmatter(fed)
        assert fm["status"] == "consolidated"
        assert fm["consolidated_at"] == "2026-06-12"
        assert read_episode_frontmatter(unfed)["status"] == "open"

    def test_idempotent_second_call(self, vault):
        path = _write_episode_file(vault, "2026-06-12-telegram-aaaa1111-100000.md")
        assert mark_episodes_consolidated([path], now=_dt(2026, 6, 12)) == 1
        assert mark_episodes_consolidated([path], now=_dt(2026, 6, 13)) == 0
        # Original consolidated_at survives the idempotent second pass.
        assert read_episode_frontmatter(path)["consolidated_at"] == "2026-06-12"

    def test_missing_files_skipped_silently(self, vault):
        ghost = vault / "episodes" / "2026-06-12-telegram-dead0000-100000.md"
        assert mark_episodes_consolidated([ghost]) == 0

    def test_body_status_text_untouched(self, vault):
        """Frontmatter-scoped flip: 'status: open' in BODY text never flips."""
        path = _write_episode_file(
            vault,
            "2026-06-12-telegram-aaaa1111-100000.md",
            body="## Summary\n\nDiscussion of the literal text status: open here.\n",
        )
        mark_episodes_consolidated([path], now=_dt(2026, 6, 12))
        content = path.read_text(encoding="utf-8")
        assert "Discussion of the literal text status: open here." in content

    def test_no_frontmatter_benign_skip(self, vault):
        """A file without frontmatter is a benign skip — never a failure."""
        episodes_dir = vault / "episodes"
        episodes_dir.mkdir(parents=True, exist_ok=True)
        bare = episodes_dir / "2026-06-12-telegram-cccc3333-120000.md"
        bare.write_text(
            "# No frontmatter here\n\nstatus: open\n", encoding="utf-8"
        )
        assert mark_episodes_consolidated([bare], now=_dt(2026, 6, 12)) == 0
        # File untouched — the body 'status:' line is not frontmatter.
        assert "status: open" in bare.read_text(encoding="utf-8")

    def test_write_failure_raises_collected_flip_error(self, vault, monkeypatch):
        """F1: a real write failure must surface, not silently become 0."""
        path = _write_episode_file(vault, "2026-06-12-telegram-aaaa1111-100000.md")

        def exploding_write(_path, _content):
            raise PermissionError("disk says no")

        monkeypatch.setattr(ep, "_atomic_write", exploding_write)
        with pytest.raises(EpisodeFlipError) as excinfo:
            mark_episodes_consolidated([path], now=_dt(2026, 6, 12))
        assert excinfo.value.flipped == 0
        assert excinfo.value.failed_paths == [path]
        assert "disk says no" in str(excinfo.value)
        # The episode stays open — re-feedable on the next dream run.
        assert read_episode_frontmatter(path)["status"] == "open"

    def test_lock_failure_raises_collected_flip_error(self, vault, monkeypatch):
        """F1: a lock timeout is a real failure, not a benign skip."""
        path = _write_episode_file(vault, "2026-06-12-telegram-aaaa1111-100000.md")

        def timing_out_lock(_path, timeout=None):
            raise TimeoutError("lock held by another process")

        monkeypatch.setattr(ep, "file_lock", timing_out_lock)
        with pytest.raises(EpisodeFlipError) as excinfo:
            mark_episodes_consolidated([path], now=_dt(2026, 6, 12))
        assert excinfo.value.failed_paths == [path]
        assert read_episode_frontmatter(path)["status"] == "open"

    def test_partial_failure_flips_rest_then_raises(self, vault, monkeypatch):
        """Collect-then-raise: one bad file never blocks flipping the others,
        and the raised error carries the partial truth."""
        bad = _write_episode_file(vault, "2026-06-12-telegram-bbbb2222-110000.md")
        good = _write_episode_file(vault, "2026-06-12-telegram-aaaa1111-100000.md")
        real_write = ep._atomic_write

        def selective_write(path, content):
            if path == bad:
                raise PermissionError("locked by editor")
            return real_write(path, content)

        monkeypatch.setattr(ep, "_atomic_write", selective_write)
        with pytest.raises(EpisodeFlipError) as excinfo:
            # Bad path FIRST — proves the loop continues past a failure.
            mark_episodes_consolidated([bad, good], now=_dt(2026, 6, 12))
        assert excinfo.value.flipped == 1
        assert excinfo.value.failed_paths == [bad]
        assert bad.name in str(excinfo.value)
        assert read_episode_frontmatter(good)["status"] == "consolidated"
        assert read_episode_frontmatter(bad)["status"] == "open"

        # Retry after the failure clears: only the failed file flips —
        # the already-consolidated one is an idempotent benign skip.
        monkeypatch.setattr(ep, "_atomic_write", real_write)
        assert mark_episodes_consolidated([bad, good], now=_dt(2026, 6, 13)) == 1
        assert read_episode_frontmatter(bad)["status"] == "consolidated"


# =============================================================================
# Category 10 — index + recall reach (R1 M4, identity-asserted)
# =============================================================================


class TestIndexRecallReach:
    def test_sync_index_and_search_reach_episode(self, tmp_path, monkeypatch):
        import db as db_mod
        import memory_index
        import memory_search

        vault = tmp_path / "vault"
        (vault / "daily").mkdir(parents=True)
        sentinel = "zzepisodereachsentinel"
        episode = _write_episode_file(
            vault,
            "2026-06-12-telegram-aaaa1111-100000.md",
            body=f"## Key Decisions\n\n- the {sentinel} decision landed today\n",
        )
        (vault / "daily" / "2026-06-12.md").write_text(
            "- routine entries only\n", encoding="utf-8"
        )
        # Fixture identity: the sentinel exists ONLY in the episode body.
        for p in vault.rglob("*.md"):
            if p != episode:
                assert sentinel not in p.read_text(encoding="utf-8")

        monkeypatch.setattr(db_mod, "DATABASE_PATH", tmp_path / "memory.db")
        monkeypatch.setattr(db_mod, "DATABASE_URL", "")

        stats = memory_index.sync_index(vault, generate_embeddings=False)
        assert stats["chunks_total"] > 0
        assert stats["files_indexed"] >= 2  # episode + daily both rglob'd

        rows = memory_search.search_keyword(sentinel, limit=5)
        assert rows, "keyword search returned no rows for the episode sentinel"
        hit = rows[0]
        assert hit.path.startswith("episodes/")
        assert sentinel in hit.text

        prefixed = memory_search.search_keyword(
            sentinel, limit=5, path_prefix="episodes/"
        )
        assert prefixed
        assert prefixed[0].path.startswith("episodes/")
        assert sentinel in prefixed[0].text

    def test_reindex_file_single_episode_reachable(self, tmp_path, monkeypatch):
        import db as db_mod
        import memory_search
        from recall_service import reindex_file

        vault = tmp_path / "vault"
        sentinel = "zzreindexreachsentinel"
        episode = _write_episode_file(
            vault,
            "2026-06-12-telegram-bbbb2222-180000.md",
            body=f"## Key Decisions\n\n- the {sentinel} single-file path works\n",
        )

        monkeypatch.setattr(db_mod, "DATABASE_PATH", tmp_path / "memory.db")
        monkeypatch.setattr(db_mod, "DATABASE_URL", "")

        chunks = reindex_file(episode, vault, generate_embeddings=False)
        assert chunks > 0

        rows = memory_search.search_keyword(sentinel, limit=5)
        assert rows
        assert rows[0].path.startswith("episodes/")
        assert sentinel in rows[0].text


# =============================================================================
# Synthetic end-to-end — writer + both consumers + idempotency, one tmp story
# =============================================================================


def _make_llm_result(text: str):
    result = MagicMock()
    result.text = text
    result.provider = "mock"
    result.model = "mock-model"
    result.cost_usd = 0.0
    return result


class TestSyntheticEndToEnd:
    @pytest.mark.asyncio
    async def test_flush_to_dream_full_story(self, tmp_path, monkeypatch):
        import memory_dream as md
        import memory_flush

        vault = tmp_path / "vault"
        (vault / "daily").mkdir(parents=True)
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # --- Phase A: flush writes the episode (fresh-context fake runtime)
        context_file = tmp_path / CTX_LIFECYCLE_A
        context_file.write_text("**User:** raw transcript\n", encoding="utf-8")
        daily_entries: list[tuple[str, str]] = []

        async def fake_flush_runtime(_request):
            return SimpleNamespace(
                text=GOOD_RESPONSE,
                provider="test-provider",
                model="test-model",
                cost_usd=0.0,
            )

        monkeypatch.setattr(memory_flush, "FLUSH_STATE_FILE", state_dir / "flush-state.json")
        monkeypatch.setattr(memory_flush, "MEMORY_DIR", vault)
        monkeypatch.setattr(memory_flush, "run_with_runtime_lanes", fake_flush_runtime)
        monkeypatch.setattr(
            memory_flush,
            "append_to_daily_log",
            lambda text, section: daily_entries.append((text, section)),
        )
        monkeypatch.setattr(memory_flush, "_reindex_episode", lambda _p: None)

        result = await memory_flush.run_flush(context_file)
        assert result == GOOD_RESPONSE.strip()
        episode_files = list((vault / "episodes").glob("*.md"))
        assert len(episode_files) == 1
        episode = episode_files[0]
        assert episode.name == (
            f"2026-06-12-telegram-{_sid8(SYNTHETIC_SAFE_ID)}-100000.md"
        )
        assert daily_entries  # daily-log consumer stayed first

        # --- Phase B: gather counts it and returns its path
        # Episode date: is write-time today (real clock); orient() scans
        # yesterday-and-older logs, so the fixture log is dated yesterday.
        from datetime import timedelta

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        daily_log = vault / "daily" / f"{yesterday}.md"
        daily_log.write_text("- session ran\n", encoding="utf-8")

        with patch("memory_dream.STATE_DIR", state_dir), \
             patch("memory_dream.DREAM_SIGNAL_THRESHOLD", 1):
            signal = md.gather_signal([daily_log], days=7, memory_dir=vault)
        assert [p.name for p in signal.episode_paths] == [episode.name]
        assert signal.found is True  # the episode IS the signal (silence-breaker)

        # --- Phase C: consolidate prompt carries the digest; dream flips it
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("CONSOLIDATION_OK"),
            _make_llm_result("PRUNE_OK"),
        ])
        memory_md = vault / "MEMORY.md"
        memory_md.write_text("---\ntags: [system]\n---\n# MEMORY\n", encoding="utf-8")

        with patch("memory_dream.DREAM_STATE_FILE", state_dir / "dream-state.json"), \
             patch("memory_dream.MEMORY_FILE", memory_md), \
             patch("memory_dream.MEMORY_DIR", vault), \
             patch("memory_dream.DAILY_DIR", vault / "daily"), \
             patch("memory_dream.SELF_FILE", vault / "SELF.md"), \
             patch("memory_dream.GOALS_FILE", vault / "GOALS.md"), \
             patch("memory_dream.STATE_DIR", state_dir), \
             patch("memory_dream.AMENDMENT_LEDGER_FILE", state_dir / "ledger.jsonl"), \
             patch("memory_dream.DREAM_SIGNAL_THRESHOLD", 1), \
             patch("memory_dream.append_to_daily_log", lambda *_a, **_k: None), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"):
            dream_result = await md._run_dream_inner(
                test_mode=False, force=True, days=7
            )

        assert dream_result == "CONSOLIDATION_OK"
        consolidate_prompt = mock_rwf.call_args_list[0][0][0].prompt
        assert "## Recent Episodes (open)" in consolidate_prompt
        assert episode.stem in consolidate_prompt
        assert "Mine the open episodes" in consolidate_prompt

        fm = read_episode_frontmatter(episode)
        assert fm["status"] == "consolidated"
        assert "consolidated_at" in fm

        # --- Phase D: second gather finds nothing open (idempotency)
        with patch("memory_dream.STATE_DIR", state_dir), \
             patch("memory_dream.DREAM_SIGNAL_THRESHOLD", 1):
            second = md.gather_signal([daily_log], days=7, memory_dir=vault)
        assert second.episode_paths == []

    @pytest.mark.asyncio
    async def test_two_lifecycle_variant(self, tmp_path, monkeypatch):
        """A second flush of the SAME channel with a LATER stem timestamp
        lands a SECOND episode file that the next gather picks up as the
        only open one."""
        import memory_dream as md
        import memory_flush

        vault = tmp_path / "vault"
        (vault / "daily").mkdir(parents=True)
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        async def fake_flush_runtime(_request):
            return SimpleNamespace(
                text=GOOD_RESPONSE, provider="t", model="m", cost_usd=0.0
            )

        monkeypatch.setattr(memory_flush, "FLUSH_STATE_FILE", state_dir / "flush-state.json")
        monkeypatch.setattr(memory_flush, "MEMORY_DIR", vault)
        monkeypatch.setattr(memory_flush, "run_with_runtime_lanes", fake_flush_runtime)
        monkeypatch.setattr(memory_flush, "append_to_daily_log", lambda *_a: None)
        monkeypatch.setattr(memory_flush, "_reindex_episode", lambda _p: None)

        ctx_a = tmp_path / CTX_LIFECYCLE_A
        ctx_a.write_text("**User:** first\n", encoding="utf-8")
        await memory_flush.run_flush(ctx_a)

        first = next((vault / "episodes").glob("*.md"))
        assert mark_episodes_consolidated([first]) == 1

        # Same channel, later clear. Production spacing is >=60s (the dedup
        # gate); simulate it by aging the recorded dedup state.
        state_file = state_dir / "flush-state.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["last_flushed_session_id"] = ""
        state_file.write_text(json.dumps(state), encoding="utf-8")

        ctx_b = tmp_path / CTX_LIFECYCLE_B
        ctx_b.write_text("**User:** second\n", encoding="utf-8")
        await memory_flush.run_flush(ctx_b)

        all_files = sorted(f.name for f in (vault / "episodes").glob("*.md"))
        assert len(all_files) == 2

        from datetime import timedelta

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        daily_log = vault / "daily" / f"{yesterday}.md"
        daily_log.write_text("- ran\n", encoding="utf-8")
        with patch("memory_dream.STATE_DIR", state_dir), \
             patch("memory_dream.DREAM_SIGNAL_THRESHOLD", 1):
            signal = md.gather_signal([daily_log], days=7, memory_dir=vault)
        assert [p.name for p in signal.episode_paths] == [
            f"2026-06-12-telegram-{_sid8(SYNTHETIC_SAFE_ID)}-180000.md"
        ]


# =============================================================================
# flush-state schema guard (Rule 2 — no episode registry in any state file)
# =============================================================================


class TestNoEpisodeRegistryInState:
    @pytest.mark.asyncio
    async def test_flush_state_keys_unchanged(self, tmp_path, monkeypatch):
        import memory_flush

        vault = tmp_path / "vault"
        vault.mkdir()
        state_file = tmp_path / "flush-state.json"
        context_file = tmp_path / CTX_LIFECYCLE_A
        context_file.write_text("**User:** raw\n", encoding="utf-8")

        async def fake_runtime(_request):
            return SimpleNamespace(
                text=GOOD_RESPONSE, provider="t", model="m", cost_usd=0.0
            )

        monkeypatch.setattr(memory_flush, "FLUSH_STATE_FILE", state_file)
        monkeypatch.setattr(memory_flush, "MEMORY_DIR", vault)
        monkeypatch.setattr(memory_flush, "run_with_runtime_lanes", fake_runtime)
        monkeypatch.setattr(memory_flush, "append_to_daily_log", lambda *_a: None)
        monkeypatch.setattr(memory_flush, "_reindex_episode", lambda _p: None)

        await memory_flush.run_flush(context_file)

        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert set(state.keys()) == {
            "last_flush",
            "context_file",
            "last_flushed_session_id",
            "result",
        }
