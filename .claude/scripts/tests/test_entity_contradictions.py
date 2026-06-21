"""Tests for entity_contradictions — contradiction detection (WS2, issue #83).

The shared sys.path insertion lives in ``tests/conftest.py:11-14``, which pytest
applies to every test under ``tests/`` automatically — so this file imports
``entity_contradictions`` directly with no bootstrap of its own.
"""

from __future__ import annotations

import textwrap

import pytest

from entity_contradictions import (
    Contradiction,
    check_contradictions,
    insert_contradiction_callouts,
)


# ---------------------------------------------------------------------------
# Moved tests (formerly TestContradictions in test_entity_extractor.py)
# ---------------------------------------------------------------------------


class TestContradictions:
    def test_detects_negation_contradiction(self, tmp_path):
        page = tmp_path / "TEST.md"
        page.write_text(textwrap.dedent("""\
        ---
        aliases: []
        ---

        # Test Concept

        ## From [[Source-A]] (2026-01-01)

        - The system always uses SQLite as the default database
        - Caching is enabled by default

        ## From [[Source-B]] (2026-02-01)

        - The system does not use SQLite as the default database
        - Caching is disabled for performance reasons
        """))

        contras = check_contradictions(page)
        assert len(contras) >= 1
        assert any(c.severity == "direct" for c in contras)

    def test_detects_opposite_pairs(self, tmp_path):
        page = tmp_path / "TEST.md"
        page.write_text(textwrap.dedent("""\
        ---
        aliases: []
        ---

        # Config Pattern

        ## From [[Source-A]] (2026-01-01)

        - Feature flags are enabled by default in production

        ## From [[Source-B]] (2026-02-01)

        - Feature flags are disabled by default in production
        """))

        contras = check_contradictions(page)
        assert len(contras) >= 1

    def test_no_contradictions_single_source(self, tmp_path):
        page = tmp_path / "TEST.md"
        page.write_text(textwrap.dedent("""\
        ---
        aliases: []
        ---

        # Test

        ## From [[Only-Source]] (2026-01-01)

        - Claim one
        - Claim two
        """))

        contras = check_contradictions(page)
        assert contras == []

    def test_inserts_callouts(self, tmp_path):
        page = tmp_path / "TEST.md"
        page.write_text("# Test\n\nSome content.\n")

        contra = Contradiction(
            concept_page="TEST",
            claim_a="uses SQLite",
            source_a="Source-A",
            claim_b="does not use SQLite",
            source_b="Source-B",
            severity="direct",
        )
        insert_contradiction_callouts(page, [contra])

        content = page.read_text()
        assert "[!warning] Contradiction" in content
        assert "Source-A" in content
        assert "Source-B" in content

    def test_no_duplicate_callouts(self, tmp_path):
        page = tmp_path / "TEST.md"
        page.write_text("# Test\n\nContent.\n")

        contra = Contradiction(
            concept_page="TEST",
            claim_a="X",
            source_a="A",
            claim_b="Y",
            source_b="B",
        )
        insert_contradiction_callouts(page, [contra])
        insert_contradiction_callouts(page, [contra])

        content = page.read_text()
        assert content.count("[!warning]") == 1


# ---------------------------------------------------------------------------
# Isolation tests (logic now independently callable — paths the moved tests
# did not exercise). Each maps to ONE distinct code path.
# ---------------------------------------------------------------------------


class TestContradictionIsolation:
    def test_fewer_than_two_sections_returns_empty(self, tmp_path):
        """Early return at ``len(sections) < 2`` — a page with a single
        ``## From`` section (and one with zero) yields no contradictions."""
        page = tmp_path / "ONE.md"
        page.write_text(textwrap.dedent("""\
        # Solo

        ## From [[Only]] (2026-01-01)

        - The system always uses SQLite as the default database
        - The system does not use SQLite as the default database
        """))
        # Even though the two claims would contradict, a single source section
        # never reaches the cross-source comparison.
        assert check_contradictions(page) == []

        empty = tmp_path / "NONE.md"
        empty.write_text("# Nothing here\n\nNo source sections at all.\n")
        assert check_contradictions(empty) == []

    def test_severity_direct_from_negation_asymmetry(self, tmp_path):
        """Negation asymmetry path → severity 'direct'. Distinct fixture with
        NO opposite-word pair so only the negation branch can fire."""
        page = tmp_path / "NEG.md"
        page.write_text(textwrap.dedent("""\
        # Negation Case

        ## From [[Src-A]] (2026-01-01)

        - The router handles slash commands instantly

        ## From [[Src-B]] (2026-02-01)

        - The router does not handle slash commands instantly
        """))
        contras = check_contradictions(page)
        assert len(contras) == 1
        c = contras[0]
        assert (c.claim_a, c.claim_b, c.severity) == (
            "The router handles slash commands instantly",
            "The router does not handle slash commands instantly",
            "direct",
        )

    def test_severity_tension_from_opposite_pair(self, tmp_path):
        """Opposite-word-pair path (enabled/disabled) with NO negation asymmetry
        → severity 'tension'. Distinct fixture from the direct case."""
        page = tmp_path / "OPP.md"
        page.write_text(textwrap.dedent("""\
        # Opposite Case

        ## From [[Src-A]] (2026-01-01)

        - Feature caching is enabled by configuration

        ## From [[Src-B]] (2026-02-01)

        - Feature caching is disabled by configuration
        """))
        contras = check_contradictions(page)
        assert len(contras) == 1
        c = contras[0]
        assert (c.claim_a, c.claim_b, c.severity) == (
            "Feature caching is enabled by configuration",
            "Feature caching is disabled by configuration",
            "tension",
        )

    def test_shared_significant_words_floor(self, tmp_path):
        """Two claims with < 2 shared content words (after stopword removal)
        produce NO contradiction even with negation asymmetry."""
        page = tmp_path / "FLOOR.md"
        page.write_text(textwrap.dedent("""\
        # Floor Case

        ## From [[Src-A]] (2026-01-01)

        - Postgres scales horizontally beautifully

        ## From [[Src-B]] (2026-02-01)

        - The cron never triggers reliably overnight
        """))
        # "not"/negation present on B, but the two claims share no significant
        # content words, so the < 2 shared-words gate skips the pair.
        assert check_contradictions(page) == []

    def test_insert_callouts_byte_for_byte(self, tmp_path):
        """M1 — full-page expected-output assertion with a frozen ``today``.
        Proves the callout header, both quote lines, the flagged-on line, the
        blank-line spacing, and the ``## Contradictions`` append are unchanged."""
        page = tmp_path / "EXACT.md"
        page.write_text("# Test\n\nSome content.\n")

        contra = Contradiction(
            concept_page="TEST",
            claim_a="uses SQLite",
            source_a="Source-A",
            claim_b="does not use SQLite",
            source_b="Source-B",
            severity="direct",
        )
        insert_contradiction_callouts(page, [contra], today="2026-01-01")

        expected = (
            "# Test\n"
            "\n"
            "Some content.\n"
            "\n"
            "## Contradictions\n"
            "\n"
            "> [!warning] Contradiction (direct)\n"
            "> **[[Source-A]]** says: \"uses SQLite\"\n"
            "> **[[Source-B]]** says: \"does not use SQLite\"\n"
            "> *Flagged during compilation on 2026-01-01*\n"
            "\n"
        )
        assert page.read_text() == expected

    def test_insert_callouts_empty_list_noop(self, tmp_path):
        """Empty-list early return — the page is left byte-for-byte unchanged."""
        page = tmp_path / "NOOP.md"
        original = "# Untouched\n\nNo contradictions here.\n"
        page.write_text(original)
        insert_contradiction_callouts(page, [])
        assert page.read_text() == original


# ---------------------------------------------------------------------------
# B2 — compile/report smoke: contradictions_found still FLOWS through
# compile_entities() into the CompilationReport (covers the 6+ downstream
# consumers at once and the entity_extractor -> entity_contradictions re-export).
# ---------------------------------------------------------------------------


class TestCompileReportFlow:
    def test_contradictions_found_populated_through_compile(self, tmp_path):
        import entity_contradictions as ec
        from entity_extractor import CompilationReport, ExtractedEntity, compile_entities

        vault = tmp_path / "vault"
        concepts = vault / "concepts"
        concepts.mkdir(parents=True)

        # Pre-existing concept page with one source section that makes a claim
        # the new source will directly negate (shared significant words +
        # negation asymmetry → severity 'direct').
        existing = concepts / "DATABASE.md"
        existing.write_text(textwrap.dedent("""\
        ---
        aliases: ["Database"]
        compiled_from:
          - "[[OLD]]"
        related:
          - "[[OLD]]"
        ---

        # Database

        ## From [[OLD]] (2026-01-01)

        - The system always uses SQLite as the default database
        """))

        entities = [
            ExtractedEntity(
                name="Database",
                confidence=0.9,
                source_claims=[
                    "The system does not use SQLite as the default database",
                ],
            ),
        ]

        report = compile_entities(entities, "NEW-SOURCE.md", vault)

        assert isinstance(report, CompilationReport)
        assert len(report.pages_updated) == 1
        assert report.contradictions_found, "expected contradictions_found populated"
        assert all(
            isinstance(c, ec.Contradiction) for c in report.contradictions_found
        )
        assert any(c.severity == "direct" for c in report.contradictions_found)
        # Callout was written into the page on the update path.
        assert "[!warning] Contradiction" in existing.read_text()
