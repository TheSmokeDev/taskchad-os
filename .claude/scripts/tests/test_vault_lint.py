"""Tests for vault_lint — 8 health checks, zero LLM cost."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    """Create a concept page with standard frontmatter."""
    concepts = vault_dir / "concepts"
    concepts.mkdir(parents=True, exist_ok=True)
    page = concepts / f"{slug}.md"
    if content is None:
        content = (
            f'---\ntags: [concept, auto-compiled]\ndate: 2026-04-07\n'
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
