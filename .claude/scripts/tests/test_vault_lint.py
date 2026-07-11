"""Tests for vault_lint — 8 health checks, zero LLM cost."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

import vault_lint
from vault_lint import (
    LintIssue,
    check_broken_wikilinks,
    check_frontmatter_validation,
    check_index_completeness,
    check_orphan_pages,
    check_page_size,
    check_stale_content,
    check_tag_audit,
    run_lint,
)


def _make_concept(vault_dir, slug, content=None):
    """Create a concept page with standard frontmatter.

    The default date is always today — a hardcoded date silently crosses the
    stale_content 90-day cutoff as wall-clock time passes (bit us 2026-07-11).
    """
    from datetime import date as _date

    concepts = vault_dir / "concepts"
    concepts.mkdir(parents=True, exist_ok=True)
    page = concepts / f"{slug}.md"
    if content is None:
        content = (
            f'---\ntags: [concept, auto-compiled]\ndate: {_date.today().isoformat()}\n'
            f'summary: "{slug} concept"\n---\n# {slug}\n\nContent about {slug}.\n'
        )
    page.write_text(content, encoding="utf-8")
    return page


def _make_note(vault_dir, rel_path, content):
    """Create a note at an arbitrary path."""
    full = vault_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full


class TestOrphanPages:
    def test_detects_orphan(self, tmp_path):
        _make_concept(tmp_path, "ALPHA")
        _make_concept(tmp_path, "BETA")
        # Only ALPHA is referenced from a non-concept file
        _make_note(tmp_path, "docs/OVERVIEW.md",
                   "---\ntags: [documentation]\ndate: 2026-04-07\n---\n# Overview\n\nSee [[ALPHA]].\n")

        issues = check_orphan_pages(tmp_path)
        orphan_files = {i.file for i in issues}
        assert "concepts/BETA.md" in orphan_files
        assert "concepts/ALPHA.md" not in orphan_files

    def test_no_orphans(self, tmp_path):
        _make_concept(tmp_path, "ALPHA")
        _make_note(tmp_path, "docs/DOC.md",
                   "---\ntags: [documentation]\ndate: 2026-04-07\n---\n# Doc\n\n[[ALPHA]] is great.\n")
        issues = check_orphan_pages(tmp_path)
        assert len([i for i in issues if i.file == "concepts/ALPHA.md"]) == 0


class TestBrokenWikilinks:
    def test_detects_broken_link(self, tmp_path):
        _make_note(tmp_path, "docs/DOC.md",
                   "---\ntags: [documentation]\ndate: 2026-04-07\n---\n# Doc\n\n[[NONEXISTENT]] link.\n")
        issues = check_broken_wikilinks(tmp_path)
        assert any("NONEXISTENT" in i.message for i in issues)

    def test_valid_links_pass(self, tmp_path):
        _make_concept(tmp_path, "ALPHA")
        _make_note(tmp_path, "docs/DOC.md",
                   "---\ntags: [documentation]\ndate: 2026-04-07\n---\n# Doc\n\n[[ALPHA]] link.\n")
        issues = check_broken_wikilinks(tmp_path)
        assert not any("ALPHA" in i.message for i in issues)


class TestFrontmatterValidation:
    def test_missing_frontmatter(self, tmp_path):
        _make_note(tmp_path, "docs/BAD.md", "# No Frontmatter\n\nJust content.\n")
        issues = check_frontmatter_validation(tmp_path)
        assert any("Missing frontmatter" in i.message for i in issues)

    def test_missing_tags(self, tmp_path):
        _make_note(tmp_path, "docs/BAD.md", "---\ndate: 2026-04-07\n---\n# Missing tags\n")
        issues = check_frontmatter_validation(tmp_path)
        assert any("tags" in i.message for i in issues)

    def test_valid_frontmatter(self, tmp_path):
        _make_note(tmp_path, "docs/GOOD.md",
                   "---\ntags: [documentation]\ndate: 2026-04-07\n---\n# Good\n")
        issues = check_frontmatter_validation(tmp_path)
        assert len(issues) == 0


class TestTagAudit:
    def test_unknown_tag(self, tmp_path):
        _make_note(tmp_path, "docs/DOC.md",
                   "---\ntags: [documentation, not-in-schema]\ndate: 2026-04-07\n---\n# Doc\n")
        schema = {"tag_taxonomy": {"documentation", "concept", "daily"}}
        issues = check_tag_audit(tmp_path, schema=schema)
        assert any("not-in-schema" in i.message for i in issues)

    def test_no_schema_skips(self, tmp_path):
        _make_note(tmp_path, "docs/DOC.md",
                   "---\ntags: [anything]\ndate: 2026-04-07\n---\n# Doc\n")
        issues = check_tag_audit(tmp_path, schema=None)
        assert len(issues) == 0


class TestStaleContent:
    def test_old_page_flagged(self, tmp_path):
        _make_concept(tmp_path, "OLD",
                      '---\ntags: [concept]\ndate: 2025-01-01\nsummary: "old"\n---\n# OLD\n')
        issues = check_stale_content(tmp_path, days=90)
        assert len(issues) == 1
        assert "2025-01-01" in issues[0].message

    def test_recent_page_passes(self, tmp_path):
        _make_concept(tmp_path, "NEW")
        issues = check_stale_content(tmp_path, days=90)
        assert len(issues) == 0


class TestPageSize:
    def test_large_page_flagged(self, tmp_path):
        lines = ["---", "tags: [concept]", "date: 2026-04-07", 'summary: "big"', "---", "# Big Page"]
        lines.extend([f"Line {i}" for i in range(250)])
        _make_concept(tmp_path, "BIG", content="\n".join(lines))
        issues = check_page_size(tmp_path, max_lines=200)
        assert len(issues) == 1
        assert "lines" in issues[0].message


class TestIndexCompleteness:
    def test_missing_from_index(self, tmp_path):
        _make_concept(tmp_path, "ALPHA")
        _make_concept(tmp_path, "BETA")
        # INDEX only has ALPHA
        idx = tmp_path / "concepts" / "INDEX.md"
        idx.write_text("# Index\n\n- [[ALPHA]] — Alpha\n", encoding="utf-8")

        issues = check_index_completeness(tmp_path)
        assert any("BETA" in i.file for i in issues)
        assert not any("ALPHA" in i.file for i in issues)

    def test_no_index_file(self, tmp_path):
        _make_concept(tmp_path, "ALPHA")
        issues = check_index_completeness(tmp_path)
        assert any("INDEX.md does not exist" in i.message for i in issues)


class TestRunLint:
    def test_never_raises(self, tmp_path):
        """run_lint should never raise, even with bad input."""
        issues = run_lint(tmp_path)
        # Should complete without error — may have warnings about missing index etc.
        assert isinstance(issues, list)

    def test_json_output(self, tmp_path):
        """Issues should be JSON-serializable."""
        _make_concept(tmp_path, "TEST")
        issues = run_lint(tmp_path)
        from dataclasses import asdict
        data = json.dumps([asdict(i) for i in issues])
        parsed = json.loads(data)
        assert isinstance(parsed, list)


# ---------------------------------------------------------------------------
# Delta lint (Feature B) — incremental scan, output byte-identical to full
# ---------------------------------------------------------------------------

_LINT_STATE = Path("_state") / "lint-state.json"

_SCHEMA_TAGS = {"concept", "auto-compiled", "documentation", "system", "note"}


def _make_index(vault_dir, slugs):
    concepts = vault_dir / "concepts"
    concepts.mkdir(parents=True, exist_ok=True)
    body = "".join(f"- [[{s}]]\n" for s in slugs)
    (concepts / "INDEX.md").write_text(
        f"---\ntags: [system]\ndate: 2026-07-01\n---\n# Concept Index\n\n{body}",
        encoding="utf-8",
    )


def _make_schema(vault_dir, tags):
    rows = "".join(f"| `{t}` | ok |\n" for t in tags)
    (vault_dir / "SCHEMA.md").write_text(
        "---\ntags: [system]\ndate: 2026-07-01\n---\n# Schema\n\n"
        "## Tag Taxonomy\n\n| Tag | Meaning |\n|-----|---------|\n" + rows,
        encoding="utf-8",
    )


def _seed_vault(vault_dir):
    """A small mixed vault: concepts, notes, index, schema."""
    _make_concept(vault_dir, "ALPHA")
    _make_concept(vault_dir, "BETA")
    _make_note(
        vault_dir, "docs/OVERVIEW.md",
        "---\ntags: [documentation]\ndate: 2026-07-01\n---\n# Overview\n\nSee [[ALPHA]] and [[BETA]].\n",
    )
    _make_note(
        vault_dir, "docs/sub/DEEP.md",
        "---\ntags: [note]\ndate: 2026-07-01\n---\n# Deep\n\nLinks [[ALPHA]].\n",
    )
    _make_index(vault_dir, ["ALPHA", "BETA"])
    _make_schema(vault_dir, _SCHEMA_TAGS)


class TestDeltaLint:
    def test_delta_off_no_state_written(self, tmp_path):
        _make_concept(tmp_path, "ALPHA")
        out = run_lint(tmp_path, delta=False)
        assert isinstance(out, list)
        assert not (tmp_path / "_state").exists()

    def test_first_delta_run_matches_full_and_writes_state(self, tmp_path):
        _seed_vault(tmp_path)
        schema = {"tag_taxonomy": _SCHEMA_TAGS}
        full = run_lint(tmp_path, schema=schema, delta=False)
        dlt = run_lint(tmp_path, schema=schema, delta=True)
        assert dlt == full
        assert (tmp_path / _LINT_STATE).exists()

    def test_posix_keys_on_windows(self, tmp_path):
        _seed_vault(tmp_path)
        run_lint(tmp_path, schema={"tag_taxonomy": _SCHEMA_TAGS}, delta=True)
        state = json.loads((tmp_path / _LINT_STATE).read_text(encoding="utf-8"))
        assert "docs/sub/DEEP.md" in state["files"]
        assert all("\\" not in key for key in state["files"])

    def test_delta_parity_after_random_mutations(self, tmp_path):
        _seed_vault(tmp_path)
        schema = {"tag_taxonomy": _SCHEMA_TAGS}
        # Seed state.
        run_lint(tmp_path, schema=schema, delta=True)

        mutations = [
            lambda: _make_concept(tmp_path, "GAMMA"),                     # new concept (orphan)
            lambda: _make_note(tmp_path, "docs/NEW.md",
                               "---\ntags: [note]\ndate: 2026-07-01\n---\n# New\n\n[[GAMMA]] [[MISSING]].\n"),
            lambda: (tmp_path / "concepts" / "BETA.md").write_text(
                "---\ntags: [concept, auto-compiled]\ndate: 2026-07-01\n"
                'summary: "beta"\n---\n# BETA\n\n' + ("edited line\n" * 3), encoding="utf-8"),
            lambda: (tmp_path / "docs" / "sub" / "DEEP.md").unlink(),     # delete a linker
            lambda: _make_concept(tmp_path, "DELTA"),
            lambda: (tmp_path / "concepts" / "ALPHA.md").unlink(),        # delete a concept
            lambda: _make_note(tmp_path, "docs/RESOLVE.md",
                               "---\ntags: [note]\ndate: 2026-07-01\n---\n# R\n\n[[GAMMA]] [[DELTA]].\n"),
        ]
        for mutate in mutations:
            mutate()
            full = run_lint(tmp_path, schema=schema, delta=False)
            dlt = run_lint(tmp_path, schema=schema, delta=True)
            assert dlt == full, "delta output must equal full scan after every mutation"

    def test_deleted_linker_orphans_target_and_breaks_links(self, tmp_path):
        # BETA is referenced ONLY from LINKER; OTHER links [[LINKER]].
        _make_concept(tmp_path, "BETA")
        _make_note(tmp_path, "LINKER.md",
                   "---\ntags: [note]\ndate: 2026-07-01\n---\n# L\n\n[[BETA]].\n")
        _make_note(tmp_path, "OTHER.md",
                   "---\ntags: [note]\ndate: 2026-07-01\n---\n# O\n\nSee [[LINKER]].\n")
        schema = {"tag_taxonomy": _SCHEMA_TAGS}
        run_lint(tmp_path, schema=schema, delta=True)  # seed

        (tmp_path / "LINKER.md").unlink()
        full = run_lint(tmp_path, schema=schema, delta=False)
        dlt = run_lint(tmp_path, schema=schema, delta=True)
        assert dlt == full
        assert any(i.check == "orphan_pages" and i.file == "concepts/BETA.md" for i in dlt)
        assert any(i.check == "broken_wikilinks" and "[[LINKER]]" in i.message for i in dlt)

    def test_orphan_resolved_by_new_inbound_link(self, tmp_path):
        _make_concept(tmp_path, "BETA")
        schema = {"tag_taxonomy": _SCHEMA_TAGS}
        first = run_lint(tmp_path, schema=schema, delta=True)  # seed; BETA orphan
        assert any(i.check == "orphan_pages" and i.file == "concepts/BETA.md" for i in first)

        _make_note(tmp_path, "REF.md",
                   "---\ntags: [note]\ndate: 2026-07-01\n---\n# Ref\n\n[[BETA]].\n")
        full = run_lint(tmp_path, schema=schema, delta=False)
        dlt = run_lint(tmp_path, schema=schema, delta=True)
        assert dlt == full
        assert not any(i.check == "orphan_pages" and i.file == "concepts/BETA.md" for i in dlt)

    def test_corrupt_state_falls_back_to_full(self, tmp_path):
        _seed_vault(tmp_path)
        schema = {"tag_taxonomy": _SCHEMA_TAGS}
        (tmp_path / "_state").mkdir(parents=True, exist_ok=True)
        (tmp_path / _LINT_STATE).write_text("{not json", encoding="utf-8")
        full = run_lint(tmp_path, schema=schema, delta=False)
        dlt = run_lint(tmp_path, schema=schema, delta=True)
        assert dlt == full
        # Rebuilt into a valid state file.
        state = json.loads((tmp_path / _LINT_STATE).read_text(encoding="utf-8"))
        assert state["version"] == 1

    def test_version_mismatch_rebuilds(self, tmp_path):
        _seed_vault(tmp_path)
        schema = {"tag_taxonomy": _SCHEMA_TAGS}
        (tmp_path / "_state").mkdir(parents=True, exist_ok=True)
        (tmp_path / _LINT_STATE).write_text(
            json.dumps({"version": 999, "hash_algo": "sha256", "files": {}}), encoding="utf-8")
        full = run_lint(tmp_path, schema=schema, delta=False)
        dlt = run_lint(tmp_path, schema=schema, delta=True)
        assert dlt == full
        state = json.loads((tmp_path / _LINT_STATE).read_text(encoding="utf-8"))
        assert state["version"] == 1

    def test_schema_change_reflags_untouched_file(self, tmp_path):
        _make_schema(tmp_path, _SCHEMA_TAGS | {"weirdtag"})
        _make_note(tmp_path, "NOTE.md",
                   "---\ntags: [weirdtag]\ndate: 2026-07-01\n---\n# Note\n\nBody.\n")

        import entity_extractor
        schema1 = entity_extractor.load_schema(tmp_path)
        first = run_lint(tmp_path, schema=schema1, delta=True)  # seed; weirdtag valid → no tag issue
        assert not any(i.check == "tag_audit" and i.file == "NOTE.md" for i in first)

        # Rewrite SCHEMA.md WITHOUT weirdtag → its hash changes, cache invalidated.
        _make_schema(tmp_path, _SCHEMA_TAGS)
        schema2 = entity_extractor.load_schema(tmp_path)
        full = run_lint(tmp_path, schema=schema2, delta=False)
        dlt = run_lint(tmp_path, schema=schema2, delta=True)
        assert dlt == full
        # NOTE.md is byte-unchanged but must be re-flagged under the new taxonomy.
        assert any(i.check == "tag_audit" and i.file == "NOTE.md" for i in dlt)

    def test_stale_content_recomputed_every_run(self, tmp_path, monkeypatch):
        # date 2026-07-01 is recent "today"; becomes stale once "today" advances.
        _make_concept(tmp_path, "FRESH", content=(
            '---\ntags: [concept, auto-compiled]\ndate: 2026-07-01\n'
            'summary: "fresh"\n---\n# FRESH\n\nBody.\n'))
        schema = {"tag_taxonomy": _SCHEMA_TAGS}
        first = run_lint(tmp_path, schema=schema, delta=True)  # seed; not stale yet
        assert not any(i.check == "stale_content" for i in first)

        class _FixedDate(date):
            @classmethod
            def today(cls):
                return date(2026, 12, 1)  # >90 days after 2026-07-01

        monkeypatch.setattr(vault_lint, "date", _FixedDate)
        full = run_lint(tmp_path, schema=schema, delta=False)
        dlt = run_lint(tmp_path, schema=schema, delta=True)  # FRESH unchanged (hash cached)
        assert dlt == full
        # Recomputed from stored fm_date despite the cached record → now stale.
        assert any(i.check == "stale_content" and i.file.endswith("FRESH.md") for i in dlt)

    def test_check_subset_ignores_delta_and_leaves_state_untouched(self, tmp_path):
        _seed_vault(tmp_path)
        subset = run_lint(tmp_path, checks=["orphan_pages"], delta=True)
        assert all(i.check == "orphan_pages" for i in subset)
        assert not (tmp_path / _LINT_STATE).exists()

    def test_delta_never_raises_on_state_error(self, tmp_path, monkeypatch):
        _seed_vault(tmp_path)
        schema = {"tag_taxonomy": _SCHEMA_TAGS}

        def _boom(_vault):
            raise RuntimeError("state machinery exploded")

        monkeypatch.setattr(vault_lint, "_load_lint_state", _boom)
        out = run_lint(tmp_path, schema=schema, delta=True)
        assert isinstance(out, list)
        # Falls back to a full scan → identical to an explicit full run.
        assert out == run_lint(tmp_path, schema=schema, delta=False)

    def test_cli_delta_flag_parses(self, tmp_path, monkeypatch):
        _make_concept(tmp_path, "ALPHA")
        monkeypatch.setattr(sys, "argv",
                            ["vault_lint.py", "--vault-dir", str(tmp_path), "--delta"])
        vault_lint.main()
        assert (tmp_path / _LINT_STATE).exists()
