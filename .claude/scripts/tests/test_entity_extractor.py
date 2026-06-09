"""Tests for entity_extractor — extraction, compilation, contradiction detection."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from entity_extractor import (
    CONFIDENCE_THRESHOLD,
    CompilationReport,
    Contradiction,
    DetectedConnection,
    ExtractedEntity,
    VaultFrontmatterError,
    _classify_connection,
    _rotate_build_log_if_needed,
    archive_concept,
    backfill_vault,
    check_contradictions,
    compile_entities,
    create_concept_page,
    create_connection_article,
    extract_entities_heuristic,
    find_existing_concept,
    generate_index,
    insert_contradiction_callouts,
    load_schema,
    sweep_uncompiled,
    update_concept_page,
    update_source_frontmatter,
)


# ---------------------------------------------------------------------------
# ExtractedEntity
# ---------------------------------------------------------------------------


class TestExtractedEntity:
    def test_slug_basic(self):
        e = ExtractedEntity(name="Transformer Attention")
        assert e.slug == "TRANSFORMER-ATTENTION"

    def test_slug_special_chars(self):
        e = ExtractedEntity(name="Claude's Agent SDK (v2)")
        assert e.slug == "CLAUDES-AGENT-SDK-V2"

    def test_slug_underscores(self):
        e = ExtractedEntity(name="memory_search")
        assert e.slug == "MEMORY-SEARCH"

    def test_slug_strips_heading_numbers(self):
        assert ExtractedEntity(name="1. System Architecture").slug == "SYSTEM-ARCHITECTURE"
        assert ExtractedEntity(name="3. The Monorepo").slug == "THE-MONOREPO"
        assert ExtractedEntity(name="10. Advanced Config").slug == "ADVANCED-CONFIG"
        assert ExtractedEntity(name="01. Intro Section").slug == "INTRO-SECTION"
        assert ExtractedEntity(name="1- Dash Separated").slug == "DASH-SEPARATED"
        assert ExtractedEntity(name="1 Space Only").slug == "SPACE-ONLY"
        # No number prefix — unchanged
        assert ExtractedEntity(name="HERMES-AGENT").slug == "HERMES-AGENT"


# ---------------------------------------------------------------------------
# Heuristic extraction
# ---------------------------------------------------------------------------


class TestExtractHeuristic:
    def test_extracts_headings(self):
        content = textwrap.dedent("""\
        # Main Title

        ## Transformer Architecture

        Some text about transformers.

        ## Self-Attention Mechanism

        Details about self-attention.
        """)
        entities = extract_entities_heuristic(content, "test-doc.md")
        names = [e.name.lower() for e in entities]
        assert "transformer architecture" in names
        assert "self-attention mechanism" in names

    def test_extracts_bold(self):
        content = "The **Langfuse SDK** provides observability for **LLM applications**."
        entities = extract_entities_heuristic(content)
        names = [e.name for e in entities]
        assert "Langfuse SDK" in names
        assert "LLM applications" in names

    def test_extracts_wikilinks(self):
        content = "See [[Recall Pipeline]] and [[Memory Search]] for details."
        entities = extract_entities_heuristic(content)
        names = [e.name for e in entities]
        assert "Recall Pipeline" in names
        assert "Memory Search" in names

    def test_wikilinks_highest_confidence(self):
        content = "The [[Recall Pipeline]] is important."
        entities = extract_entities_heuristic(content)
        recall = [e for e in entities if e.name == "Recall Pipeline"][0]
        assert recall.confidence >= 0.8

    def test_extracts_frontmatter_related(self):
        content = textwrap.dedent("""\
        ---
        related:
          - "[[Langfuse]]"
          - "[[Observability]]"
        ---

        # Doc Title

        Some body text.
        """)
        entities = extract_entities_heuristic(content)
        names = [e.name for e in entities]
        assert "Langfuse" in names
        assert "Observability" in names

    def test_skips_noise_headings(self):
        content = textwrap.dedent("""\
        # Overview

        ## Introduction

        ## Getting Started

        ## Real Concept Here
        """)
        entities = extract_entities_heuristic(content)
        names = [e.name.lower() for e in entities]
        assert "overview" not in names
        assert "introduction" not in names
        assert "getting started" not in names
        assert "real concept here" in names

    def test_max_15_entities(self):
        lines = [f"## Entity Number {i}" for i in range(30)]
        content = "\n\n".join(lines)
        entities = extract_entities_heuristic(content)
        assert len(entities) <= 15

    def test_sorted_by_confidence_desc(self):
        content = textwrap.dedent("""\
        See [[High Confidence Link]].

        ## Medium Heading

        Text about **Low Bold**.
        """)
        entities = extract_entities_heuristic(content)
        confidences = [e.confidence for e in entities]
        assert confidences == sorted(confidences, reverse=True)

    def test_extracts_claims(self):
        content = textwrap.dedent("""\
        ## Transformer

        The Transformer model uses self-attention to process sequences in parallel.
        Transformer architectures dominate modern NLP tasks.
        """)
        entities = extract_entities_heuristic(content)
        transformer = [e for e in entities if "transformer" in e.name.lower()][0]
        assert len(transformer.source_claims) >= 1

    def test_skips_source_filename(self):
        content = "# My Document\n\nSome content."
        entities = extract_entities_heuristic(content, "MY-DOCUMENT.md")
        names = [e.name.lower() for e in entities]
        assert "my document" not in names

    def test_empty_content(self):
        entities = extract_entities_heuristic("")
        assert entities == []

    def test_deduplicates(self):
        content = textwrap.dedent("""\
        ## Langfuse

        The **Langfuse** SDK. See [[Langfuse]].
        """)
        entities = extract_entities_heuristic(content)
        langfuse_entities = [e for e in entities if "langfuse" in e.name.lower()]
        assert len(langfuse_entities) == 1
        # Confidence should be boosted from multiple mentions
        assert langfuse_entities[0].confidence > 0.7


# ---------------------------------------------------------------------------
# Find existing concept
# ---------------------------------------------------------------------------


class TestFindExistingConcept:
    def test_exact_match(self, tmp_path):
        concepts = tmp_path / "concepts"
        concepts.mkdir()
        page = concepts / "LANGFUSE.md"
        page.write_text("---\naliases: []\n---\n# Langfuse\n")
        assert find_existing_concept("Langfuse", tmp_path) == page

    def test_alias_match(self, tmp_path):
        concepts = tmp_path / "concepts"
        concepts.mkdir()
        page = concepts / "LLM-OBSERVABILITY.md"
        page.write_text('---\naliases: ["Langfuse", "LLM Tracing"]\n---\n# LLM Observability\n')
        assert find_existing_concept("Langfuse", tmp_path) == page

    def test_not_found(self, tmp_path):
        concepts = tmp_path / "concepts"
        concepts.mkdir()
        assert find_existing_concept("Nonexistent", tmp_path) is None

    def test_no_concepts_dir(self, tmp_path):
        assert find_existing_concept("Anything", tmp_path) is None


# ---------------------------------------------------------------------------
# Create / update concept pages
# ---------------------------------------------------------------------------


class TestCreateConceptPage:
    def test_creates_file(self, tmp_path):
        entity = ExtractedEntity(
            name="Recall Pipeline",
            entity_type="concept",
            description="The recall system for memory retrieval.",
            source_claims=["Recall uses hybrid search", "Tier 1 triggers graph traversal"],
            confidence=0.9,
        )
        page = create_concept_page(entity, "SOURCE-DOC.md", tmp_path)
        assert page.exists()
        assert page.name == "RECALL-PIPELINE.md"

        content = page.read_text()
        assert "Recall Pipeline" in content
        assert "auto-compiled" in content
        assert "[[SOURCE-DOC]]" in content
        assert "Recall uses hybrid search" in content

    def test_creates_concepts_dir(self, tmp_path):
        entity = ExtractedEntity(name="Test")
        create_concept_page(entity, "src.md", tmp_path)
        assert (tmp_path / "concepts").exists()


class TestUpdateConceptPage:
    def test_appends_section(self, tmp_path):
        concepts = tmp_path / "concepts"
        concepts.mkdir()
        page = concepts / "LANGFUSE.md"
        page.write_text(textwrap.dedent("""\
        ---
        aliases: ["Langfuse"]
        compiled_from:
          - "[[OLD-SOURCE]]"
        related:
          - "[[OLD-SOURCE]]"
        ---

        # Langfuse

        ## From [[OLD-SOURCE]] (2026-01-01)

        - Langfuse provides LLM observability
        """))

        entity = ExtractedEntity(
            name="Langfuse",
            source_claims=["Langfuse supports nested spans"],
        )
        update_concept_page(entity, "NEW-SOURCE.md", page)

        content = page.read_text()
        assert "From [[NEW-SOURCE]]" in content
        assert "Langfuse supports nested spans" in content

    def test_no_duplicate_sections(self, tmp_path):
        concepts = tmp_path / "concepts"
        concepts.mkdir()
        page = concepts / "TEST.md"
        page.write_text("---\ncompiled_from:\n  - \"[[SRC]]\"\nrelated:\n  - \"[[SRC]]\"\n---\n# Test\n\n## From [[SRC]] (2026-01-01)\n\n- claim\n")

        entity = ExtractedEntity(name="Test")
        update_concept_page(entity, "SRC.md", page)

        content = page.read_text()
        assert content.count("From [[SRC]]") == 1


# ---------------------------------------------------------------------------
# Contradiction detection
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
# Compilation pipeline
# ---------------------------------------------------------------------------


class TestCompileEntities:
    def test_creates_and_updates(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        # Create a source file
        source = vault / "SOURCE.md"
        source.write_text(textwrap.dedent("""\
        ---
        tags: [documentation]
        date: 2026-04-07
        related:
          - "[[Existing]]"
        ---

        # Source Document
        """))

        entities = [
            ExtractedEntity(name="New Concept", confidence=0.9, description="A new concept."),
            ExtractedEntity(name="Low Confidence", confidence=0.3),  # below threshold
        ]

        report = compile_entities(entities, str(source), vault)

        assert report.entities_processed == 1
        assert report.entities_skipped == 1
        assert len(report.pages_created) == 1
        assert (vault / "concepts" / "NEW-CONCEPT.md").exists()

    def test_updates_existing_page(self, tmp_path):
        vault = tmp_path / "vault"
        concepts = vault / "concepts"
        concepts.mkdir(parents=True)

        existing = concepts / "LANGFUSE.md"
        existing.write_text(textwrap.dedent("""\
        ---
        aliases: ["Langfuse"]
        compiled_from:
          - "[[OLD]]"
        related:
          - "[[OLD]]"
        ---

        # Langfuse

        ## From [[OLD]] (2026-01-01)

        - Old claim
        """))

        entities = [
            ExtractedEntity(name="Langfuse", confidence=0.9, source_claims=["New claim"]),
        ]

        report = compile_entities(entities, "NEW-SOURCE.md", vault)
        assert len(report.pages_updated) == 1
        assert len(report.pages_created) == 0

    def test_confidence_threshold(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        entities = [
            ExtractedEntity(name=f"Entity-{i}", confidence=0.3 + i * 0.1)
            for i in range(8)
        ]

        report = compile_entities(entities, "src.md", vault)
        # Only entities with confidence >= 0.6 should be compiled
        expected_processed = len([e for e in entities if e.confidence >= CONFIDENCE_THRESHOLD])
        assert report.entities_processed == expected_processed

    def test_updates_source_frontmatter(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()

        source = vault / "SOURCE.md"
        source.write_text(
            "---\ntags: [documentation]\ndate: 2026-04-07\n"
            "related:\n  - \"[[Existing]]\"\n---\n# Doc\n"
        )

        entities = [ExtractedEntity(name="New Concept", confidence=0.9)]
        compile_entities(entities, str(source), vault)

        content = source.read_text()
        assert "[[NEW-CONCEPT]]" in content

    def test_blocks_existing_source_missing_frontmatter_before_writes(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        source = vault / "SOURCE.md"
        source.write_text("# Source\n\n## Durable Memory Gate\n", encoding="utf-8")

        entities = [ExtractedEntity(name="Durable Memory Gate", confidence=0.9)]

        with pytest.raises(VaultFrontmatterError) as excinfo:
            compile_entities(entities, str(source), vault)

        assert "compile blocked by source frontmatter gate" in str(excinfo.value)
        assert "SOURCE.md: Missing frontmatter block" in str(excinfo.value)
        assert not (vault / "concepts").exists()

    def test_blocks_existing_source_missing_date_before_writes(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        source = vault / "SOURCE.md"
        source.write_text(
            "---\ntags: [documentation]\n---\n# Source\n\n## Durable Memory Gate\n",
            encoding="utf-8",
        )

        entities = [ExtractedEntity(name="Durable Memory Gate", confidence=0.9)]

        with pytest.raises(VaultFrontmatterError) as excinfo:
            compile_entities(entities, str(source), vault)

        assert "SOURCE.md: Missing required field: date" in str(excinfo.value)
        assert not (vault / "concepts").exists()


class TestVaultCompilationFrontmatterGate:
    def _write_note(self, vault: Path, rel_path: str, content: str) -> Path:
        path = vault / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_backfill_refuses_invalid_source_before_partial_writes(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        self._write_note(
            vault,
            "GOOD.md",
            textwrap.dedent("""\
            ---
            tags: [documentation]
            date: 2026-04-07
            related:
            ---
            # Good Source

            ## Durable Memory Gate

            The **Durable Memory Gate** keeps compile output trustworthy.
            """),
        )
        self._write_note(
            vault,
            "BAD.md",
            "# Bad Source\n\n## Frontmatter Enforcement\n\n"
            "This source is long enough to enter backfill and must block writes.\n"
            "The **Frontmatter Enforcement** concept should not compile.\n",
        )

        with pytest.raises(VaultFrontmatterError) as excinfo:
            backfill_vault(vault, skip_compiled=False)

        assert "backfill blocked by source frontmatter gate" in str(excinfo.value)
        assert "BAD.md: Missing frontmatter block" in str(excinfo.value)
        assert not (vault / "concepts").exists()

    def test_sweep_reports_its_own_frontmatter_gate(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        self._write_note(
            vault,
            "BAD.md",
            "# Bad Source\n\n## Sweep Frontmatter Gate\n\n"
            "This source is long enough to enter sweep and must block writes.\n"
            "The **Sweep Frontmatter Gate** concept should not compile.\n",
        )

        with pytest.raises(VaultFrontmatterError) as excinfo:
            sweep_uncompiled(vault)

        assert "sweep blocked by source frontmatter gate" in str(excinfo.value)
        assert "BAD.md: Missing frontmatter block" in str(excinfo.value)
        assert not (vault / "concepts").exists()


# ---------------------------------------------------------------------------
# Source frontmatter update
# ---------------------------------------------------------------------------


class TestUpdateSourceFrontmatter:
    def test_adds_concept_links(self, tmp_path):
        source = tmp_path / "SOURCE.md"
        source.write_text("---\nrelated:\n  - \"[[Existing]]\"\n---\n# Doc\n")

        update_source_frontmatter(source, ["New Concept", "Another"])

        content = source.read_text()
        assert "[[NEW-CONCEPT]]" in content
        assert "[[ANOTHER]]" in content
        assert "[[Existing]]" in content

    def test_no_duplicate_links(self, tmp_path):
        source = tmp_path / "SOURCE.md"
        source.write_text("---\nrelated:\n  - \"[[NEW-CONCEPT]]\"\n---\n# Doc\n")

        update_source_frontmatter(source, ["New Concept"])

        content = source.read_text()
        assert content.count("NEW-CONCEPT") == 1


# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------


class TestLoadSchema:
    def test_parses_tags(self, tmp_path):
        schema_file = tmp_path / "SCHEMA.md"
        schema_file.write_text(textwrap.dedent("""\
            ---
            tags: [system, schema]
            ---
            # Vault Schema

            ## Scope

            This vault covers AI agent frameworks and memory pipelines.

            ## Tag Taxonomy

            ### Note Types

            | Tag | Usage |
            |-----|-------|
            | `daily` | Daily log entries |
            | `concept` | Auto-compiled concept pages |
            | `moc` | Maps of Content |

            ### Entity Types (for concept pages)

            | Type | When to Use |
            |------|-------------|
            | `concept` | Ideas and patterns |
            | `tool` | Software tools |
        """))

        result = load_schema(tmp_path)
        assert "daily" in result["tag_taxonomy"]
        assert "concept" in result["tag_taxonomy"]
        assert "moc" in result["tag_taxonomy"]
        assert "tool" in result["entity_types"]
        assert "agent" in result["scope_keywords"]
        assert "memory" in result["scope_keywords"]

    def test_missing_file_returns_empty(self, tmp_path):
        result = load_schema(tmp_path)
        assert result == {}

    def test_schema_boosts_in_domain(self, tmp_path):
        """Entities matching scope keywords get a confidence boost."""
        schema = {"scope_keywords": {"agent", "memory", "pipeline"}, "tag_taxonomy": set(), "entity_types": set()}
        content = "# Agent Architecture\n\nThe **memory pipeline** handles recall."

        entities = extract_entities_heuristic(content, "test.md", schema=schema)
        agent_ent = next((e for e in entities if "agent" in e.name.lower()), None)
        assert agent_ent is not None
        # Heading baseline is 0.7, schema boost adds 0.1 = 0.8
        assert agent_ent.confidence >= 0.79

    def test_schema_does_not_break_existing(self, tmp_path):
        """Extraction without schema works identically to before."""
        content = "# Test Heading\n\n**Bold Entity**\n\n[[Wiki Link]]"
        with_schema = extract_entities_heuristic(content, "test.md", schema=None)
        without_schema = extract_entities_heuristic(content, "test.md")
        assert len(with_schema) == len(without_schema)
        for a, b in zip(with_schema, without_schema):
            assert a.name == b.name
            assert a.confidence == b.confidence


# ---------------------------------------------------------------------------
# Index generation
# ---------------------------------------------------------------------------


class TestGenerateIndex:
    def _make_concept(self, concepts_dir, slug, entity_type="concept", summary="Test concept"):
        """Helper to create a minimal concept page."""
        page = concepts_dir / f"{slug}.md"
        page.write_text(
            f'---\ntags: [concept, auto-compiled, {entity_type}]\ndate: 2026-04-07\n'
            f'summary: "{summary}"\ncompiled_from:\n  - "[[SOURCE]]"\n---\n# {slug}\n',
            encoding="utf-8",
        )
        return page

    def test_basic(self, tmp_path):
        concepts = tmp_path / "concepts"
        concepts.mkdir()
        self._make_concept(concepts, "ALPHA", summary="Alpha concept")
        self._make_concept(concepts, "BETA", summary="Beta concept")

        idx = generate_index(tmp_path)
        content = idx.read_text()
        assert "**2 concepts**" in content
        assert "[[ALPHA]]" in content
        assert "[[BETA]]" in content

    def test_groups_by_type(self, tmp_path):
        concepts = tmp_path / "concepts"
        concepts.mkdir()
        self._make_concept(concepts, "MYLIB", entity_type="tool", summary="A tool")
        self._make_concept(concepts, "MYIDEA", entity_type="concept", summary="A concept")

        idx = generate_index(tmp_path)
        content = idx.read_text()
        assert "## Tool" in content
        assert "## Concept" in content

    def test_skips_buildlog(self, tmp_path):
        concepts = tmp_path / "concepts"
        concepts.mkdir()
        (concepts / "BUILD-LOG.md").write_text("---\ntags: [build-log]\n---\n# Log\n")
        self._make_concept(concepts, "ALPHA")

        idx = generate_index(tmp_path)
        content = idx.read_text()
        assert "BUILD-LOG" not in content
        assert "[[ALPHA]]" in content

    def test_empty_vault(self, tmp_path):
        idx = generate_index(tmp_path)
        content = idx.read_text()
        assert "**0 concepts**" in content

    def test_split_at_50(self, tmp_path):
        concepts = tmp_path / "concepts"
        concepts.mkdir()
        for i in range(55):
            slug = f"CONCEPT-{i:03d}"
            self._make_concept(concepts, slug, summary=f"Concept number {i}")

        idx = generate_index(tmp_path)
        content = idx.read_text()
        assert "**55 concepts**" in content
        # Should have alphabetical sub-sections
        assert "###" in content


# ---------------------------------------------------------------------------
# BUILD-LOG rotation
# ---------------------------------------------------------------------------


class TestBuildLogRotation:
    def _make_build_log(self, vault_dir, entry_count):
        """Create a BUILD-LOG.md with N entries."""
        concepts = vault_dir / "concepts"
        concepts.mkdir(parents=True, exist_ok=True)
        log = concepts / "BUILD-LOG.md"
        header = "---\ntags: [build-log]\n---\n\n# Build Log\n\n"
        entries = "".join(
            f"## [2026-04-07 12:{i:02d}] Source: [[test-{i}]]\n- Created: CONCEPT-{i}\n\n"
            for i in range(entry_count)
        )
        log.write_text(header + entries, encoding="utf-8")
        return log

    def test_rotation_triggers_at_501(self, tmp_path):
        self._make_build_log(tmp_path, 501)
        _rotate_build_log_if_needed(tmp_path, max_entries=500)

        # Original should be a fresh file
        log = tmp_path / "concepts" / "BUILD-LOG.md"
        assert log.exists()
        content = log.read_text()
        assert "Rotated 501 entries" in content

        # Rotated file should exist
        rotated_files = list((tmp_path / "concepts").glob("BUILD-LOG-*.md"))
        assert len(rotated_files) == 1
        rotated_content = rotated_files[0].read_text()
        assert "## [2026-04-07" in rotated_content

    def test_no_rotation_under_500(self, tmp_path):
        log = self._make_build_log(tmp_path, 499)
        original_content = log.read_text()
        _rotate_build_log_if_needed(tmp_path, max_entries=500)

        # Should be unchanged
        assert log.read_text() == original_content
        rotated_files = list((tmp_path / "concepts").glob("BUILD-LOG-2*.md"))
        assert len(rotated_files) == 0

    def test_rotation_preserves_content(self, tmp_path):
        self._make_build_log(tmp_path, 510)
        _rotate_build_log_if_needed(tmp_path, max_entries=500)

        rotated_files = list((tmp_path / "concepts").glob("BUILD-LOG-*.md"))
        assert len(rotated_files) == 1
        content = rotated_files[0].read_text()
        # All 510 entries should be in the rotated file
        assert content.count("## [2026-04-07") == 510


# ---------------------------------------------------------------------------
# Connection type classification
# ---------------------------------------------------------------------------


class TestConnectionTypes:
    def test_comparison_same_type(self):
        a = ExtractedEntity(name="Redis", entity_type="tool", source_claims=["Redis stores data fast"])
        b = ExtractedEntity(name="Memcached", entity_type="tool", source_claims=["Memcached caches data fast"])
        result = _classify_connection(a, b, ["Redis stores data fast <-> Memcached caches data fast"])
        assert result == "comparison"

    def test_dependency_keyword(self):
        a = ExtractedEntity(name="Engine", entity_type="concept", source_claims=["Engine uses Redis for caching"])
        b = ExtractedEntity(name="Redis", entity_type="tool", source_claims=["Redis provides fast lookups"])
        result = _classify_connection(a, b, ["Engine uses Redis for caching <-> Redis provides fast lookups"])
        assert result == "dependency"

    def test_comparison_article_has_dimensions(self, tmp_path):
        a = ExtractedEntity(name="Redis", entity_type="tool", source_claims=["fast"])
        b = ExtractedEntity(name="Memcached", entity_type="tool", source_claims=["also fast"])
        path = create_connection_article(a, b, ["shared speed"], "test.md", tmp_path, connection_type="comparison")
        assert path is not None
        content = path.read_text()
        assert "Dimensions of Comparison" in content
        assert "| Redis | Memcached |" in content

    def test_shared_context_backward_compatible(self, tmp_path):
        a = ExtractedEntity(name="Alpha", entity_type="concept", source_claims=["alpha does stuff"])
        b = ExtractedEntity(name="Beta", entity_type="concept", source_claims=["beta does stuff"])
        path = create_connection_article(a, b, ["shared context"], "test.md", tmp_path)
        assert path is not None
        content = path.read_text()
        assert "connection_type: shared-context" in content
        assert "Dimensions of Comparison" not in content


# ---------------------------------------------------------------------------
# Archival lifecycle
# ---------------------------------------------------------------------------


class TestArchival:
    def _setup_vault(self, tmp_path):
        """Create a minimal vault with concept pages and a referencing doc."""
        concepts = tmp_path / "concepts"
        concepts.mkdir()
        (concepts / "ALPHA.md").write_text(
            '---\ntags: [concept, auto-compiled]\ndate: 2026-04-07\nsummary: "Alpha"\n---\n# Alpha\n',
            encoding="utf-8",
        )
        (concepts / "BETA.md").write_text(
            '---\ntags: [concept, auto-compiled]\ndate: 2026-04-07\nsummary: "Beta"\n---\n# Beta\n',
            encoding="utf-8",
        )
        # INDEX.md
        (concepts / "INDEX.md").write_text(
            "# Index\n\n- [[ALPHA]] — Alpha\n- [[BETA]] — Beta\n",
            encoding="utf-8",
        )
        # Doc that references ALPHA
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "DOC.md").write_text(
            '---\ntags: [documentation]\ndate: 2026-04-07\n---\n# Doc\n\nSee [[ALPHA]] and [[BETA]].\n',
            encoding="utf-8",
        )
        return tmp_path

    def test_archive_moves_file(self, tmp_path):
        vault = self._setup_vault(tmp_path)
        page = vault / "concepts" / "ALPHA.md"
        archive_concept(page, vault)

        assert not (vault / "concepts" / "ALPHA.md").exists()
        assert (vault / "_archive" / "concepts" / "ALPHA.md").exists()

    def test_archive_removes_from_index(self, tmp_path):
        vault = self._setup_vault(tmp_path)
        page = vault / "concepts" / "ALPHA.md"
        archive_concept(page, vault)

        index_content = (vault / "concepts" / "INDEX.md").read_text()
        assert "[[ALPHA]]" not in index_content
        assert "[[BETA]]" in index_content

    def test_archive_updates_backlinks(self, tmp_path):
        vault = self._setup_vault(tmp_path)
        page = vault / "concepts" / "ALPHA.md"
        archive_concept(page, vault)

        doc_content = (vault / "docs" / "DOC.md").read_text()
        assert "ALPHA (archived)" in doc_content
        assert "[[ALPHA]]" not in doc_content
        # BETA should be unchanged
        assert "[[BETA]]" in doc_content

    def test_archive_dry_run_via_find(self, tmp_path):
        """find_archivable only returns stale + orphan pages."""
        vault = self._setup_vault(tmp_path)
        # Both ALPHA and BETA are referenced from docs/DOC.md, so neither should be archivable
        from entity_extractor import find_archivable
        archivable = find_archivable(vault, days_threshold=0)
        assert len(archivable) == 0


# ---------------------------------------------------------------------------
# Karpathy LLM Wiki port — preserve_raw, append_vault_log, generate_root_index,
# _collect_concept_entries
# ---------------------------------------------------------------------------


class TestPreserveRaw:
    """preserve_raw() — copy source into {vault}/raw/ with collision handling."""

    def test_happy_path_name_preserved(self, tmp_path):
        from entity_extractor import preserve_raw

        vault = tmp_path / "vault"
        vault.mkdir()
        src = tmp_path / "article.md"
        src.write_text("original body", encoding="utf-8")

        dest = preserve_raw(src, vault)

        assert dest == vault / "raw" / "article.md"
        assert dest.read_text(encoding="utf-8") == "original body"
        # Raw directory was auto-created
        assert (vault / "raw").is_dir()
        # Original is untouched
        assert src.read_text(encoding="utf-8") == "original body"

    def test_collision_falls_back_to_date_prefix(self, tmp_path):
        from entity_extractor import _today, preserve_raw

        vault = tmp_path / "vault"
        (vault / "raw").mkdir(parents=True)
        src = tmp_path / "article.md"
        src.write_text("second version", encoding="utf-8")
        # Pre-existing file at target → collision
        (vault / "raw" / "article.md").write_text("first version", encoding="utf-8")

        dest = preserve_raw(src, vault)

        assert dest.name == f"{_today()}-article.md"
        assert dest.read_text(encoding="utf-8") == "second version"
        # Original collision file untouched
        assert (vault / "raw" / "article.md").read_text(encoding="utf-8") == "first version"

    def test_always_date_prefix_true(self, tmp_path):
        from entity_extractor import _today, preserve_raw

        vault = tmp_path / "vault"
        vault.mkdir()
        src = tmp_path / "statement.pdf"
        src.write_text("bank data", encoding="utf-8")

        dest = preserve_raw(src, vault, always_date_prefix=True)

        # Even without collision, unconditional date prefix applies
        assert dest.name == f"{_today()}-statement.pdf"
        assert not (vault / "raw" / "statement.pdf").exists()

    def test_preserves_file_metadata(self, tmp_path):
        import os
        import time

        from entity_extractor import preserve_raw

        vault = tmp_path / "vault"
        vault.mkdir()
        src = tmp_path / "article.md"
        src.write_text("body", encoding="utf-8")
        # Set a deterministic mtime well in the past
        past = time.time() - 86400  # 1 day ago
        os.utime(src, (past, past))

        dest = preserve_raw(src, vault)

        # shutil.copy2 should preserve mtime (allow 1s tolerance for FS granularity)
        assert abs(dest.stat().st_mtime - past) < 2

    def test_date_prefixed_target_already_exists_raises(self, tmp_path):
        """Same-day rerun where BOTH {name} and {YYYY-MM-DD}-{name} exist must raise.

        Pre-Fix-B: shutil.copy2 silently overwrites the date-prefixed file.
        Post-Fix-B: FileExistsError, original archive untouched.
        """
        from entity_extractor import _today, preserve_raw

        vault = tmp_path / "vault"
        (vault / "raw").mkdir(parents=True)
        original_at_name = vault / "raw" / "article.md"
        original_at_name.write_text("FIRST archived version", encoding="utf-8")
        original_at_date = vault / "raw" / f"{_today()}-article.md"
        original_at_date.write_text("SECOND archived version (also locked)",
                                    encoding="utf-8")

        src = tmp_path / "article.md"
        src.write_text("THIRD version - must NOT land", encoding="utf-8")

        with pytest.raises(FileExistsError) as excinfo:
            preserve_raw(src, vault)
        assert "raw/ is immutable" in str(excinfo.value)

        assert original_at_name.read_text(encoding="utf-8") == "FIRST archived version"
        assert original_at_date.read_text(encoding="utf-8") == \
            "SECOND archived version (also locked)"

    def test_always_date_prefix_collision_also_raises(self, tmp_path):
        """always_date_prefix=True path with existing date-prefixed file must raise.

        Tailored message branch - the operator can't fix this by renaming the
        source (dest depends only on date + source.name).
        """
        from entity_extractor import _today, preserve_raw

        vault = tmp_path / "vault"
        (vault / "raw").mkdir(parents=True)
        existing = vault / "raw" / f"{_today()}-statement.pdf"
        existing.write_text("today's statement, already archived", encoding="utf-8")

        src = tmp_path / "statement.pdf"
        src.write_text("re-run attempt - must NOT overwrite", encoding="utf-8")

        with pytest.raises(FileExistsError) as excinfo:
            preserve_raw(src, vault, always_date_prefix=True)
        # Message is tailored for the always_date_prefix path
        assert "wait until tomorrow" in str(excinfo.value) or \
               "remove the existing date-prefixed target" in str(excinfo.value)

        assert existing.read_text(encoding="utf-8") == \
            "today's statement, already archived"

    def test_on_collision_skip_identical_bytes_returns_existing(self, tmp_path):
        """R3-fix: on_collision='skip' is byte-aware — identical bytes are a true idempotent skip."""
        from entity_extractor import _today, preserve_raw

        vault = tmp_path / "vault"
        (vault / "raw").mkdir(parents=True)
        existing = vault / "raw" / f"{_today()}-statement.pdf"
        existing.write_text("identical content", encoding="utf-8")

        src = tmp_path / "statement.pdf"
        src.write_text("identical content", encoding="utf-8")

        dest = preserve_raw(src, vault, always_date_prefix=True, on_collision="skip")

        assert dest == existing
        assert dest.read_text(encoding="utf-8") == "identical content"

    def test_on_collision_skip_divergent_bytes_raises(self, tmp_path):
        """R3-fix: on_collision='skip' RAISES when bytes differ — protects provenance.

        This is the critical case Codex caught at R3: the prior iteration would
        silently keep the old archive while finance_ingest processed new source
        bytes, breaking the raw-archive-is-source-of-truth contract.
        """
        from entity_extractor import _today, preserve_raw

        vault = tmp_path / "vault"
        (vault / "raw").mkdir(parents=True)
        existing = vault / "raw" / f"{_today()}-statement.pdf"
        existing.write_text("ORIGINAL bytes", encoding="utf-8")

        src = tmp_path / "statement.pdf"
        src.write_text("DIVERGENT bytes — must raise, must NOT skip", encoding="utf-8")

        with pytest.raises(FileExistsError) as excinfo:
            preserve_raw(src, vault, always_date_prefix=True, on_collision="skip")
        assert "skip-on-divergent-bytes" in str(excinfo.value) or \
               "break provenance" in str(excinfo.value)

        # Existing archive untouched — provenance preserved
        assert existing.read_text(encoding="utf-8") == "ORIGINAL bytes"

    def test_on_collision_overwrite_replaces_existing(self, tmp_path):
        """on_collision='overwrite' replicates legacy silent-overwrite behavior (escape hatch)."""
        from entity_extractor import _today, preserve_raw

        vault = tmp_path / "vault"
        (vault / "raw").mkdir(parents=True)
        existing = vault / "raw" / f"{_today()}-statement.pdf"
        existing.write_text("OLD content", encoding="utf-8")

        src = tmp_path / "statement.pdf"
        src.write_text("NEW content", encoding="utf-8")

        dest = preserve_raw(src, vault, always_date_prefix=True, on_collision="overwrite")

        assert dest == existing
        assert dest.read_text(encoding="utf-8") == "NEW content"

    def test_on_collision_raise_with_finance_path_message(self, tmp_path):
        """Bug B regression - message must NOT say 'rename source' when always_date_prefix=True."""
        from entity_extractor import _today, preserve_raw

        vault = tmp_path / "vault"
        (vault / "raw").mkdir(parents=True)
        (vault / "raw" / f"{_today()}-statement.pdf").write_text("blocker", encoding="utf-8")

        src = tmp_path / "statement.pdf"
        src.write_text("attempt", encoding="utf-8")

        with pytest.raises(FileExistsError) as excinfo:
            preserve_raw(src, vault, always_date_prefix=True, on_collision="raise")
        # The finance-path message must NOT advise renaming the source -
        # rename doesn't change the {today}-{name} dest.
        assert "rename source" not in str(excinfo.value)

    def test_preserve_raw_with_subdir_lands_in_clipped(self, tmp_path):
        """subdir parameter routes archive into {vault}/raw/{subdir}/ (gap-4 URL ingest).

        Used by url_fetch.fetch_and_archive() to land web clips in raw/clipped/
        while keeping all collision/idempotency semantics identical to top-level raw/.
        """
        from entity_extractor import preserve_raw

        vault = tmp_path / "vault"
        vault.mkdir()
        src = tmp_path / "page.md"
        src.write_text("clipped content", encoding="utf-8")

        dest = preserve_raw(src, vault, subdir="clipped")

        assert dest == vault / "raw" / "clipped" / "page.md"
        assert dest.read_text(encoding="utf-8") == "clipped content"
        # Subdir auto-created
        assert (vault / "raw" / "clipped").is_dir()
        # Top-level raw/ does NOT contain a stray copy
        assert not (vault / "raw" / "page.md").exists()


class TestAppendVaultLog:
    """append_vault_log() — LOG.md timeline, grep-friendly."""

    def test_first_write_creates_file_with_frontmatter(self, tmp_path):
        from entity_extractor import append_vault_log

        vault = tmp_path / "vault"
        log_path = append_vault_log(
            vault, "ingest", "Test Source", bullets=["entities: 3 processed"]
        )

        assert log_path == vault / "LOG.md"
        content = log_path.read_text(encoding="utf-8")
        # Frontmatter with system tag
        assert content.startswith("---")
        assert "tags: [system]" in content
        # Header exists
        assert "# Vault Log" in content
        # Entry header matches grep pattern "^## ["
        assert "\n## [" in content
        assert "] ingest | Test Source" in content
        # Bullet rendered
        assert "- entities: 3 processed" in content

    def test_subsequent_writes_append(self, tmp_path):
        from entity_extractor import append_vault_log

        vault = tmp_path / "vault"
        append_vault_log(vault, "reflect", "First entry")
        append_vault_log(vault, "weekly", "Second entry", bullets=["days: 7"])

        content = (vault / "LOG.md").read_text(encoding="utf-8")
        assert "] reflect | First entry" in content
        assert "] weekly | Second entry" in content
        # Header only once (first-write check worked)
        assert content.count("# Vault Log") == 1
        assert content.count("tags: [system]") == 1

    def test_grep_pattern_matches_each_entry(self, tmp_path):
        """Karpathy's documented use case: grep '^## \\[' LOG.md | tail -5."""
        import re

        from entity_extractor import append_vault_log

        vault = tmp_path / "vault"
        for i in range(3):
            append_vault_log(vault, "compile", f"Run {i}")

        content = (vault / "LOG.md").read_text(encoding="utf-8")
        matches = re.findall(r"^## \[", content, re.MULTILINE)
        assert len(matches) == 3

    def test_empty_bullets_still_renders_header(self, tmp_path):
        from entity_extractor import append_vault_log

        vault = tmp_path / "vault"
        append_vault_log(vault, "archive", "Stale Concept", bullets=None)

        content = (vault / "LOG.md").read_text(encoding="utf-8")
        assert "] archive | Stale Concept" in content
        # No phantom bullets
        lines_after_header = content.split("] archive | Stale Concept", 1)[1]
        assert "\n- " not in lines_after_header.split("\n\n", 1)[0]


class TestCollectConceptEntries:
    """_collect_concept_entries() — shared parsing helper for both indices."""

    def _write_concept(
        self,
        concepts_dir: Path,
        slug: str,
        entity_type: str = "concept",
        summary: str = "test summary",
    ) -> None:
        concepts_dir.mkdir(parents=True, exist_ok=True)
        (concepts_dir / f"{slug}.md").write_text(
            f"---\ntags: [concept, auto-compiled, {entity_type}]\n"
            f'summary: "{summary}"\n---\n\n# {slug}\n',
            encoding="utf-8",
        )

    def test_basic_grouping_by_entity_type(self, tmp_path):
        from entity_extractor import _collect_concept_entries

        concepts = tmp_path / "concepts"
        self._write_concept(concepts, "ALPHA", entity_type="framework", summary="alpha sum")
        self._write_concept(concepts, "BETA", entity_type="framework", summary="beta sum")
        self._write_concept(concepts, "GAMMA", entity_type="tool", summary="gamma sum")

        entries = _collect_concept_entries(concepts)

        assert set(entries.keys()) == {"framework", "tool"}
        assert ("ALPHA", "alpha sum") in entries["framework"]
        assert ("BETA", "beta sum") in entries["framework"]
        assert ("GAMMA", "gamma sum") in entries["tool"]

    def test_skips_index_and_build_log(self, tmp_path):
        from entity_extractor import _collect_concept_entries

        concepts = tmp_path / "concepts"
        self._write_concept(concepts, "REAL", entity_type="concept")
        # Files that must be skipped
        (concepts / "INDEX.md").write_text("# index\n", encoding="utf-8")
        (concepts / "BUILD-LOG.md").write_text("# build log\n", encoding="utf-8")
        (concepts / "BUILD-LOG-2026.md").write_text("# rotated\n", encoding="utf-8")

        entries = _collect_concept_entries(concepts)

        all_slugs = [slug for group in entries.values() for slug, _ in group]
        assert all_slugs == ["REAL"]

    def test_missing_concepts_dir_returns_empty(self, tmp_path):
        from entity_extractor import _collect_concept_entries

        entries = _collect_concept_entries(tmp_path / "does-not-exist")
        assert entries == {}

    def test_falls_back_to_concept_type_when_no_specific_tag(self, tmp_path):
        from entity_extractor import _collect_concept_entries

        concepts = tmp_path / "concepts"
        concepts.mkdir()
        # Only generic tags, no entity-type tag
        (concepts / "VANILLA.md").write_text(
            '---\ntags: [concept, auto-compiled]\nsummary: "plain"\n---\n',
            encoding="utf-8",
        )

        entries = _collect_concept_entries(concepts)

        assert "concept" in entries
        assert ("VANILLA", "plain") in entries["concept"]


class TestGenerateRootIndex:
    """generate_root_index() — whole-wiki catalog at {vault}/INDEX.md."""

    def test_empty_vault_produces_valid_file(self, tmp_path):
        from entity_extractor import generate_root_index

        vault = tmp_path / "vault"
        idx = generate_root_index(vault)

        assert idx == vault / "INDEX.md"
        content = idx.read_text(encoding="utf-8")
        # Proper frontmatter
        assert "tags: [system, auto-compiled]" in content
        assert "# Wiki Index" in content
        # Zero counts
        assert "**0 concepts | 0 canonical | 0 directories**" in content

    def test_identity_files_included_when_present(self, tmp_path):
        from entity_extractor import generate_root_index

        vault = tmp_path / "vault"
        vault.mkdir()
        # Only a subset — missing ones should be silently skipped (fail-open)
        (vault / "SOUL.md").write_text(
            '---\nsummary: "AI personality from file"\n---\n# SOUL\n', encoding="utf-8"
        )
        (vault / "MEMORY.md").write_text(
            '---\nsummary: "long-term memory"\n---\n# MEMORY\n', encoding="utf-8"
        )

        content = generate_root_index(vault).read_text(encoding="utf-8")

        assert "## Identity" in content
        assert "[[SOUL]] — AI personality from file" in content
        assert "[[MEMORY]] — long-term memory" in content
        # Missing identity files don't appear
        assert "[[USER]]" not in content
        # canonical count = 2
        assert "2 canonical" in content

    def test_missing_identity_files_fail_open(self, tmp_path):
        """No identity files at all → function still succeeds, no crash."""
        from entity_extractor import generate_root_index

        vault = tmp_path / "vault"
        idx = generate_root_index(vault)

        assert idx.exists()
        # No Identity section if zero canonical
        content = idx.read_text(encoding="utf-8")
        assert "## Identity" not in content

    def test_concepts_grouped_by_type_with_cap(self, tmp_path):
        from entity_extractor import _ROOT_INDEX_MAX_PER_TYPE, generate_root_index

        vault = tmp_path / "vault"
        concepts = vault / "concepts"
        concepts.mkdir(parents=True)

        # Write enough to trip the overflow cap (>25 of a single type)
        for i in range(_ROOT_INDEX_MAX_PER_TYPE + 5):
            (concepts / f"CONCEPT-{i:03d}.md").write_text(
                f"---\ntags: [concept, auto-compiled, framework]\n"
                f'summary: "concept {i}"\n---\n',
                encoding="utf-8",
            )

        content = generate_root_index(vault).read_text(encoding="utf-8")

        assert "## Concepts by Type" in content
        assert f"### Framework ({_ROOT_INDEX_MAX_PER_TYPE + 5})" in content
        # Overflow pointer rendered
        assert "(+5 more — see [[INDEX|concepts/INDEX]])" in content
        # Total counter reflects full count, not capped
        assert f"**{_ROOT_INDEX_MAX_PER_TYPE + 5} concepts" in content

    def test_directories_excludes_private_and_concepts(self, tmp_path):
        from entity_extractor import generate_root_index

        vault = tmp_path / "vault"
        vault.mkdir()
        # Public directories (should appear)
        (vault / "daily").mkdir()
        (vault / "drafts").mkdir()
        # Concepts dir (covered separately — should NOT appear in Directories section)
        (vault / "concepts").mkdir()
        # Leading-underscore private dirs (should NOT appear)
        (vault / "_archive").mkdir()
        (vault / "_canvas").mkdir()
        (vault / "_state").mkdir()
        # Leading-dot hidden dir (should NOT appear)
        (vault / ".obsidian").mkdir()

        content = generate_root_index(vault).read_text(encoding="utf-8")

        assert "## Directories" in content
        assert "`daily/`" in content
        assert "`drafts/`" in content
        # Excluded
        assert "`_archive/`" not in content
        assert "`_canvas/`" not in content
        assert "`.obsidian/`" not in content
        # concepts has a dedicated section — not in directories list either
        dirs_section = content.split("## Directories", 1)[1]
        assert "`concepts/`" not in dirs_section

    def test_moc_files_discovered_via_glob(self, tmp_path):
        from entity_extractor import generate_root_index

        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "MOC-operations.md").write_text(
            '---\nsummary: "ops hub"\n---\n# MOC-operations\n', encoding="utf-8"
        )
        (vault / "MOC-thehomie.md").write_text(
            '---\nsummary: "framework hub"\n---\n', encoding="utf-8"
        )
        # Non-MOC file at root — should NOT appear in MOC section
        (vault / "RANDOM.md").write_text("# random\n", encoding="utf-8")

        content = generate_root_index(vault).read_text(encoding="utf-8")

        assert "## Maps of Content" in content
        assert "[[MOC-operations]] — ops hub" in content
        assert "[[MOC-thehomie]] — framework hub" in content
        assert "[[RANDOM]]" not in content


# ---------------------------------------------------------------------------
# v3 Plan: prose-heavy heuristic upgrades
# (closes Codex round-1 + round-2 adversarial review findings)
# ---------------------------------------------------------------------------


# Karpathy LLM Wiki gist excerpt — the actual reference document for our
# entity compilation engine. Used by TestKarpathyGistByteExact and
# TestExtractionRuntime. Trimmed to the prose sections that exercise the
# prose-heavy patterns the v3 fix targets.
KARPATHY_EXCERPT = textwrap.dedent("""\
    ---
    source_url: "https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f"
    fetched_at: "2026-04-26T20:35:05+00:00"
    date: 2026-04-26
    ---

    # llm-wiki

    A pattern for building personal knowledge bases using LLMs.

    The idea here is different. Instead of just retrieving from raw documents at query time, the LLM **incrementally builds and maintains a persistent wiki** — a structured, interlinked collection of markdown files that sits between you and the raw sources.

    This is the key difference: **the wiki is a persistent, compounding artifact.** The cross-references are already there.

    **Personal**: tracking your own goals, health, psychology, self-improvement — filing journal entries.**Research**: going deep on a topic over weeks or months — reading papers, articles, reports, and incrementally building a comprehensive wiki with an evolving thesis.**Reading a book**: filing each chapter as you go, building out pages for characters, themes, plot threads.**Business/team**: an internal wiki maintained by LLMs, fed by Slack threads, meeting transcripts, project documents, customer calls.

    There are three layers:

    **Raw sources** — your curated collection of source documents. Articles, papers, images, data files.

    **The wiki** — a directory of LLM-generated markdown files. Summaries, entity pages, concept pages.

    **The schema** — a document describing the structure and conventions of the wiki.

    **Ingest.** You drop a new source into the raw collection and tell the LLM to process it.

    **Query.** You ask questions against the wiki. The LLM searches for relevant pages.

    **Lint.** Periodically, ask the LLM to health-check the wiki.

    **Marp** is a markdown-based slide deck format. Obsidian has a plugin for it.

    **Dataview** is an Obsidian plugin that runs queries over page frontmatter.
""")


class TestBoldWithDefinition:
    """Bold-with-definition pattern detection — boost to 0.75 when **X**
    is followed by a definition marker AT a structurally-leading position."""

    def test_em_dash_marker_boosts(self):
        text = "**Raw sources** — your curated collection of source documents."
        ents = {e.name: e for e in extract_entities_heuristic(text)}
        assert "Raw sources" in ents
        assert ents["Raw sources"].confidence >= 0.75

    def test_en_dash_marker_boosts(self):
        text = "**The Engine** – a deterministic state machine."
        ents = {e.name: e for e in extract_entities_heuristic(text)}
        assert "The Engine" in ents
        assert ents["The Engine"].confidence >= 0.75

    def test_double_hyphen_marker_boosts(self):
        text = "**Token Budget** -- the per-region cap in chars."
        ents = {e.name: e for e in extract_entities_heuristic(text)}
        assert "Token Budget" in ents
        assert ents["Token Budget"].confidence >= 0.75

    def test_colon_marker_boosts(self):
        text = "**Personal**: tracking your own goals and habits."
        ents = {e.name: e for e in extract_entities_heuristic(text)}
        assert "Personal" in ents
        assert ents["Personal"].confidence >= 0.75

    def test_period_inside_bold_boosts(self):
        # Karpathy `**Ingest.** You drop a new source...`
        text = "**Ingest.** You drop a new source into the raw collection."
        ents = {e.name: e for e in extract_entities_heuristic(text)}
        assert "Ingest" in ents
        assert ents["Ingest"].confidence >= 0.75

    def test_plain_bold_stays_low(self):
        # Inline emphasis must not be boosted (no def-marker, no leading position).
        text = "Some prose with **most important** in the middle of a sentence."
        ents = {e.name: e for e in extract_entities_heuristic(text)}
        if "most important" in ents:
            assert ents["most important"].confidence < 0.6

    def test_inline_emphasis_with_dash_does_not_boost(self):
        # Codex round-1 #3: mid-sentence bold-then-emdash must not be boosted.
        text = "He said **really** — everyone should know about it."
        ents = {e.name: e for e in extract_entities_heuristic(text)}
        # Either filtered entirely or stays at plain-bold confidence.
        assert "really" not in ents or ents["really"].confidence < 0.6

    def test_period_inside_lowercase_is_not_boosted(self):
        # Codex round-2 #5: `**wait.** Then walked away.` must not boost.
        text = "She started to walk. **wait.** Then walked away anyway."
        ents = {e.name: e for e in extract_entities_heuristic(text)}
        assert "wait" not in ents or ents["wait"].confidence < 0.6

    def test_meta_marker_pro_tip_rejected_outright(self):
        # Codex round-2 #6: meta-markers go into _SKIP_NAMES so dedup boost
        # cannot raise them past the threshold.
        text = "**Pro-tip** — always validate your input before processing."
        ents = {e.name: e for e in extract_entities_heuristic(text)}
        assert "Pro-tip" not in ents
        assert "pro-tip" not in ents

    def test_meta_marker_tldr_rejected_via_normalization(self):
        # Internal punctuation (TL;DR semicolon) is normalized to spaces
        # before SKIP_NAMES lookup — `tl dr` matches the entry.
        text = "**TL;DR**: Don't do this in production."
        ents = {e.name: e for e in extract_entities_heuristic(text)}
        assert "TL;DR" not in ents


class TestNameQualityFilter:
    """Sentence-fragment rule + word-count cap + punctuation strip."""

    def test_short_noun_phrase_keeps(self):
        text = "**Raw sources** — your curated collection of source documents."
        ents = {e.name for e in extract_entities_heuristic(text)}
        assert "Raw sources" in ents

    def test_seven_word_fragment_rejects(self):
        text = "**incrementally builds and maintains a persistent wiki** — a structured artifact."
        ents = {e.name for e in extract_entities_heuristic(text)}
        assert not any("incrementally" in n for n in ents)

    def test_six_word_keeps(self):
        # Six tokens (after the leading article is stripped) — at the cap, must keep.
        text = "**Karpathy LLM Wiki entity compilation engine** — the v3 design."
        ents = {e.name for e in extract_entities_heuristic(text)}
        assert "Karpathy LLM Wiki entity compilation engine" in ents

    def test_sentence_fragment_with_article_rejects(self):
        # `the wiki is` matches the anchored sentence-fragment regex.
        text = "**the wiki is a persistent compounding artifact** — and that matters."
        ents = {e.name for e in extract_entities_heuristic(text)}
        assert not any(n.lower().startswith("the wiki is") for n in ents)

    def test_imperative_title_keeps(self):
        # "How To Build A Wiki" — `Build` mid-name but no preceding article-noun.
        text = "## How To Build A Wiki"
        ents = {e.name for e in extract_entities_heuristic(text)}
        assert "How To Build A Wiki" in ents

    def test_interrogative_title_with_article_keeps(self):
        # Codex round-2 #4: "How The System Works" — interrogative anchor.
        text = "## How The System Works"
        ents = {e.name for e in extract_entities_heuristic(text)}
        assert "How The System Works" in ents

    def test_terminal_period_stripped_then_validated(self):
        # `Ingest.` cleans to `Ingest`, valid concept name.
        text = "**Ingest.** You drop a new source into the raw collection."
        ents = {e.name for e in extract_entities_heuristic(text)}
        assert "Ingest" in ents

    def test_data_use_agreement_keeps(self):
        # "Use" is in finite-verb list but no preceding article in name.
        text = "## Data Use Agreement"
        ents = {e.name for e in extract_entities_heuristic(text)}
        assert "Data Use Agreement" in ents

    def test_code_fence_bold_does_not_extract(self):
        # Bold inside a code block must not be picked up.
        text = textwrap.dedent("""\
            Some prose here.

            ```python
            # **Cache Key** -- stable identifier
            x = 1
            ```

            More prose.
        """)
        ents = {e.name for e in extract_entities_heuristic(text)}
        assert "Cache Key" not in ents

    def test_inline_code_bold_does_not_extract(self):
        text = "See `**fake**` example for details."
        ents = {e.name for e in extract_entities_heuristic(text)}
        assert "fake" not in ents


class TestKarpathyGistByteExact:
    """Byte-exact regression test on the actual Karpathy LLM Wiki gist.

    Closes Codex round-1 #6 + round-2 #1/#2: validates the v3 plan's
    expected yield (10 prose primitives + 1 H1) against the real source.
    """

    def test_must_compile_named_primitives(self):
        ents = {e.name for e in extract_entities_heuristic(KARPATHY_EXCERPT)}
        eligible = {
            e.name
            for e in extract_entities_heuristic(KARPATHY_EXCERPT)
            if e.confidence >= CONFIDENCE_THRESHOLD
        }
        # Three em-dash architectural primitives
        assert "Raw sources" in eligible
        assert "The wiki" in eligible
        assert "The schema" in eligible
        # Four colon-marker domain examples (line-23 inline chain — leading via sentence-boundary)
        assert "Personal" in eligible
        assert "Research" in eligible
        assert "Reading a book" in eligible
        assert "Business/team" in eligible
        # Three period-inside-bold workflow steps
        assert "Ingest" in eligible
        assert "Query" in eligible
        assert "Lint" in eligible

    def test_must_not_compile_sentence_fragments(self):
        ents = {e.name for e in extract_entities_heuristic(KARPATHY_EXCERPT)}
        for name in ents:
            assert not name.lower().startswith("incrementally builds")
            assert "is a persistent" not in name.lower()
            # Word-count rejection of the 6+ word inline list
            assert not name.lower().startswith("competitive analysis")

    def test_must_not_compile_paragraph_lead_is(self):
        # Marp / Dataview use `**X** is...` (no def-marker) — out of scope.
        eligible = {
            e.name
            for e in extract_entities_heuristic(KARPATHY_EXCERPT)
            if e.confidence >= CONFIDENCE_THRESHOLD
        }
        assert "Marp" not in eligible
        assert "Dataview" not in eligible


class TestCapAdversarial:
    """Cap-pressure tests: confirm the 15-cap behaves predictably under
    realistic bold-def + wikilink loads, and document the known limitation
    that under EXTREME cap pressure (16+ dedup-boosted bold-defs), wikilinks
    can be displaced — which is why meta-marker outright-rejection (per
    Codex round-2 #6) is the actual safety net."""

    def test_realistic_load_wikilinks_survive(self):
        # Realistic: 10 bold-defs (each mentioned once) + 5 wikilinks. Total
        # = 15, fits exactly in cap. All 5 wikilinks must appear because
        # single-mention bold-def (0.75) < single wikilink (0.8).
        bold_lines = [f"**Concept Number {i}** — definition for concept {i}." for i in range(10)]
        wikilinks = "\n".join(f"See [[Wikilink-{i}]] for more." for i in range(5))
        text = "\n\n".join(bold_lines) + "\n\n" + wikilinks

        results = extract_entities_heuristic(text)
        names = {e.name for e in results}
        survived = sum(1 for i in range(5) if f"Wikilink-{i}" in names)
        assert survived == 5, f"Expected all 5 wikilinks to survive, got {survived}"

    def test_extreme_cap_pressure_documents_displacement(self):
        # Extreme: 16 bold-defs each repeated twice (dedup -> 0.85). Sort
        # gives top 15 all to bold-defs (0.85 > 0.8 wikilink), wikilinks get
        # truncated. This is a KNOWN LIMITATION of the cap+score scheme;
        # the safety net is meta-marker outright rejection (TestMetaMarkerDedupRejection).
        # In practice a doc never has 16+ legitimate bold-def primitives —
        # if it does, increase the cap or add wikilink-priority sort.
        bold_lines = []
        for i in range(16):
            line = f"**Concept Number {i}** — definition for concept {i}.\n"
            bold_lines.append(line)
            bold_lines.append(line)
        wikilinks = "\n".join(f"See [[Wikilink-{i}]] for more." for i in range(5))
        text = "\n".join(bold_lines) + "\n\n" + wikilinks

        results = extract_entities_heuristic(text)
        # Document the cap behavior: 15 results, all confidence >= 0.75.
        assert len(results) == 15
        assert all(e.confidence >= 0.75 for e in results)
        # Bold-defs outrank wikilinks under dedup pressure — this is the
        # documented limitation, not a bug.


class TestSkipNamesAllPaths:
    """Meta-markers and existing skip names must be rejected from all
    extraction paths (heading, bold, wikilink) — not just the bold path."""

    @pytest.mark.parametrize("skip_name", [
        "note", "tip", "pro-tip", "warning", "important", "todo", "fixme",
        "see", "see also", "caveat", "rule of thumb", "disclaimer",
    ])
    def test_skip_name_rejected_in_heading(self, skip_name):
        text = f"## {skip_name.title()}\n\nSome content here."
        ents = {e.name.lower() for e in extract_entities_heuristic(text)}
        assert skip_name not in ents

    @pytest.mark.parametrize("skip_name", [
        "note", "tip", "pro-tip", "warning", "important", "caveat",
    ])
    def test_skip_name_rejected_in_bold_def(self, skip_name):
        # Even with definition marker, meta-marker is rejected outright.
        text = f"**{skip_name.title()}** — this is a meta-marker."
        ents = {e.name.lower() for e in extract_entities_heuristic(text)}
        assert skip_name not in ents

    @pytest.mark.parametrize("skip_name", [
        "note", "tip", "warning",
    ])
    def test_skip_name_rejected_in_wikilink(self, skip_name):
        text = f"See the [[{skip_name.title()}]] for details."
        ents = {e.name.lower() for e in extract_entities_heuristic(text)}
        assert skip_name not in ents


class TestMetaMarkerDedupRejection:
    """Closes Codex round-2 #6: dedup-boost cannot raise meta-markers
    past the threshold because they are rejected outright by _SKIP_NAMES."""

    def test_repeated_pro_tip_never_compiles(self):
        # Without outright rejection, 4 mentions × +0.1 dedup would lift
        # plain-bold 0.5 to 0.9 — past the 0.6 compile threshold.
        text = textwrap.dedent("""\
            **Pro-tip** — validate input.

            **Pro-tip** — validate input again.

            **Pro-tip** — and again.

            **Pro-tip** — one more time.
        """)
        ents = {e.name.lower() for e in extract_entities_heuristic(text)}
        assert "pro-tip" not in ents

    def test_repeated_note_never_compiles(self):
        text = textwrap.dedent("""\
            **Note** — first remark.

            **Note** — second remark.

            **Note** — third remark.

            **Note** — fourth remark.
        """)
        ents = {e.name.lower() for e in extract_entities_heuristic(text)}
        assert "note" not in ents


class TestLeadingBoldPrecondition:
    """Plan v3 Change 1 leading-bold algorithm — explicit per-case tests."""

    def test_doc_start_is_leading(self):
        text = "**Foo** — definition at the very start."
        ents = {e.name for e in extract_entities_heuristic(text)}
        eligible = [
            e for e in extract_entities_heuristic(text)
            if e.name == "Foo" and e.confidence >= 0.75
        ]
        assert len(eligible) == 1

    def test_paragraph_break_is_leading(self):
        text = "Some preceding prose.\n\n**Foo** — definition after blank line."
        eligible = [
            e for e in extract_entities_heuristic(text)
            if e.name == "Foo" and e.confidence >= 0.75
        ]
        assert len(eligible) == 1

    def test_sentence_boundary_is_leading(self):
        # Karpathy line 23 inline chain: `time.**Research**: going deep...`
        text = "tracking goals over time.**Research**: going deep on a topic over weeks."
        eligible = [
            e for e in extract_entities_heuristic(text)
            if e.name == "Research" and e.confidence >= 0.75
        ]
        assert len(eligible) == 1

    def test_list_item_is_leading(self):
        text = "- **Foo** — definition in a list item."
        eligible = [
            e for e in extract_entities_heuristic(text)
            if e.name == "Foo" and e.confidence >= 0.75
        ]
        assert len(eligible) == 1

    def test_numbered_list_is_leading(self):
        text = "1. **Foo** — definition in a numbered list."
        eligible = [
            e for e in extract_entities_heuristic(text)
            if e.name == "Foo" and e.confidence >= 0.75
        ]
        assert len(eligible) == 1

    def test_block_quote_is_leading(self):
        text = "> **Foo** — definition in a block quote."
        eligible = [
            e for e in extract_entities_heuristic(text)
            if e.name == "Foo" and e.confidence >= 0.75
        ]
        assert len(eligible) == 1

    def test_mid_sentence_is_not_leading(self):
        text = "He said **really** — everyone should hear it."
        # Either filtered out or stays at plain-bold confidence.
        ents = {e.name: e for e in extract_entities_heuristic(text)}
        assert "really" not in ents or ents["really"].confidence < 0.75

    def test_list_item_mid_line_is_not_leading(self):
        text = "- Some prose with **really** — embedded mid-item."
        ents = {e.name: e for e in extract_entities_heuristic(text)}
        assert "really" not in ents or ents["really"].confidence < 0.75

    def test_crlf_line_endings_handled(self):
        # CRLF normalized to LF before leading-bold check fires.
        text = "Some prose.\r\n\r\n**Foo** — definition after CRLF break."
        eligible = [
            e for e in extract_entities_heuristic(text)
            if e.name == "Foo" and e.confidence >= 0.75
        ]
        assert len(eligible) == 1


class TestExtractionRuntime:
    """Hot-path budget: the heuristic must stay sub-100ms on the bot's
    URL-ingest path. Locks the contract so future regex changes that
    introduce backtracking get caught early."""

    def test_extracts_50kb_doc_under_100ms(self):
        import time
        # ~10x the Karpathy excerpt = ~50KB, varied content.
        content = (KARPATHY_EXCERPT * 10)
        start = time.perf_counter()
        entities = extract_entities_heuristic(content)
        elapsed = time.perf_counter() - start
        # Generous budget — pure regex on 50KB should be << 100ms.
        assert elapsed < 0.1, f"Extraction took {elapsed:.3f}s (budget 0.1s)"
        # Sanity check: still produces results
        assert len(entities) > 0
