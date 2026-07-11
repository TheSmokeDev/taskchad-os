"""Tests for the link-economy guardrails in entity_extractor (default-OFF).

Feature A of the "link-economy guardrails + delta-lint" research port:
- ≥N-mention create gate (per-vault mention ledger under {vault}/_state/)
- per-run edit ceiling (counts real writes, ignores same-source no-ops)
- per-page / per-source related-link cap
- fail-open at every seam (lock timeout, config unavailable)

Guardrails are gated behind ENTITY_GUARDRAILS_ENABLED (default false); the
conftest autouse fixture delenvs the knobs so these tests drive the ON path
explicitly.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from entity_extractor import (
    CompilationReport,
    ExtractedEntity,
    _load_mention_ledger,
    _mention_ledger_path,
    backfill_vault,
    compile_entities,
    create_concept_page,
    find_existing_concept,
    sweep_uncompiled,
    update_concept_page,
    update_source_frontmatter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enable(monkeypatch, **knobs):
    """Turn guardrails ON and set any of the ENTITY_* knobs for a test."""
    monkeypatch.setenv("ENTITY_GUARDRAILS_ENABLED", "true")
    for key, val in knobs.items():
        monkeypatch.setenv(key, str(val))


def _seed_page(vault: Path, name: str) -> Path:
    """Create a concept page via the real writer (source stem 'SEED')."""
    return create_concept_page(
        ExtractedEntity(name=name, confidence=0.9, source_claims=["seed claim"]),
        "SEED.md",
        vault,
    )


def _entity(name: str, claims=("a claim",)) -> ExtractedEntity:
    return ExtractedEntity(name=name, confidence=0.9, source_claims=list(claims))


# ---------------------------------------------------------------------------
# OFF-parity — guardrails disabled must behave byte-identically to legacy
# ---------------------------------------------------------------------------


class TestGuardrailsOff:
    def test_guardrails_off_creates_first_sight(self, tmp_path):
        """Disabled → a single-mention entity creates its page immediately."""
        vault = tmp_path / "vault"
        vault.mkdir()
        report = compile_entities([_entity("Solo Concept")], "SRC-A.md", vault, enforce_guardrails=False)
        assert (vault / "concepts" / "SOLO-CONCEPT.md").exists()
        assert len(report.pages_created) == 1

    def test_guardrails_off_report_fields_empty(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        report = compile_entities([_entity("Solo Concept")], "SRC-A.md", vault, enforce_guardrails=False)
        assert report.entities_staged == []
        assert report.updates_skipped_ceiling == []
        assert report.links_skipped_cap == 0

    def test_off_no_state_dir(self, tmp_path):
        """Disabled → no mention ledger / _state dir is ever written."""
        vault = tmp_path / "vault"
        vault.mkdir()
        compile_entities([_entity("Solo Concept")], "SRC-A.md", vault, enforce_guardrails=False)
        assert not (vault / "_state").exists()

    def test_env_default_is_off(self, tmp_path):
        """With the knobs delenv'd (conftest), the env-resolved default stages nothing new."""
        vault = tmp_path / "vault"
        vault.mkdir()
        report = compile_entities([_entity("Solo Concept")], "SRC-A.md", vault)  # None → env → off
        assert (vault / "concepts" / "SOLO-CONCEPT.md").exists()
        assert report.entities_staged == []


# ---------------------------------------------------------------------------
# Staging + promotion (≥N-mention create gate)
# ---------------------------------------------------------------------------


class TestStagingPromotion:
    def test_single_mention_stages_no_page(self, tmp_path, monkeypatch):
        _enable(monkeypatch)  # default page_min_mentions=2
        vault = tmp_path / "vault"
        vault.mkdir()
        report = compile_entities([_entity("Widget")], "SRC-A.md", vault, enforce_guardrails=True)
        assert report.entities_staged == ["Widget"]
        assert report.pages_created == []
        assert not (vault / "concepts" / "WIDGET.md").exists()
        ledger = _load_mention_ledger(vault)
        assert "WIDGET" in ledger["entities"]
        assert list(ledger["entities"]["WIDGET"]["sources"].keys()) == ["SRC-A"]

    def test_second_different_source_promotes_with_both_sections(self, tmp_path, monkeypatch):
        _enable(monkeypatch)
        vault = tmp_path / "vault"
        vault.mkdir()
        compile_entities([_entity("Widget", ["claim from A"])], "SRC-A.md", vault, enforce_guardrails=True)
        report = compile_entities([_entity("Widget", ["claim from B"])], "SRC-B.md", vault, enforce_guardrails=True)

        page = vault / "concepts" / "WIDGET.md"
        assert page.exists()
        assert str(page) in report.pages_created
        content = page.read_text(encoding="utf-8")
        assert "## From [[SRC-A]]" in content
        assert "## From [[SRC-B]]" in content
        assert "claim from A" in content and "claim from B" in content
        # Ledger entry consumed on promotion.
        assert "WIDGET" not in _load_mention_ledger(vault)["entities"]

    def test_same_source_twice_stays_staged(self, tmp_path, monkeypatch):
        """The same source mentioning an entity twice is one distinct source → still staged."""
        _enable(monkeypatch)
        vault = tmp_path / "vault"
        vault.mkdir()
        compile_entities([_entity("Widget")], "SRC-A.md", vault, enforce_guardrails=True)
        report = compile_entities([_entity("Widget")], "SRC-A.md", vault, enforce_guardrails=True)
        assert report.entities_staged == ["Widget"]
        assert not (vault / "concepts" / "WIDGET.md").exists()
        ledger = _load_mention_ledger(vault)
        assert list(ledger["entities"]["WIDGET"]["sources"].keys()) == ["SRC-A"]

    def test_min_mentions_three(self, tmp_path, monkeypatch):
        _enable(monkeypatch, ENTITY_PAGE_MIN_MENTIONS=3)
        vault = tmp_path / "vault"
        vault.mkdir()
        compile_entities([_entity("Widget")], "SRC-A.md", vault, enforce_guardrails=True)
        compile_entities([_entity("Widget")], "SRC-B.md", vault, enforce_guardrails=True)
        assert not (vault / "concepts" / "WIDGET.md").exists()
        report = compile_entities([_entity("Widget")], "SRC-C.md", vault, enforce_guardrails=True)
        assert (vault / "concepts" / "WIDGET.md").exists()
        content = (vault / "concepts" / "WIDGET.md").read_text(encoding="utf-8")
        for stem in ("SRC-A", "SRC-B", "SRC-C"):
            assert f"## From [[{stem}]]" in content
        assert str(vault / "concepts" / "WIDGET.md") in report.pages_created


# ---------------------------------------------------------------------------
# Staged entities excluded from connections; vault stays lint-clean
# ---------------------------------------------------------------------------


class TestStagedExcludedFromConnections:
    def test_no_connections_and_clean_lint_after_staging(self, tmp_path, monkeypatch):
        _enable(monkeypatch)
        vault = tmp_path / "vault"
        vault.mkdir()
        source = vault / "SRC.md"
        source.write_text(
            "---\ntags: [documentation]\ndate: 2026-07-01\nrelated:\nkind: note\n---\n# Doc\n",
            encoding="utf-8",
        )
        # Two entities that share claim words — would connect if both had pages.
        ents = [
            ExtractedEntity(name="Alpha Tool", confidence=0.9, source_claims=["shared alpha beta gamma insight"]),
            ExtractedEntity(name="Beta Tool", confidence=0.9, source_claims=["shared alpha beta gamma insight"]),
        ]
        report = compile_entities(ents, str(source), vault, enforce_guardrails=True)

        assert sorted(report.entities_staged) == ["Alpha Tool", "Beta Tool"]
        assert report.connections_created == []
        assert not (vault / "connections").exists()
        # Source frontmatter must NOT reference the staged (page-less) slugs.
        src_text = source.read_text(encoding="utf-8")
        assert "[[ALPHA-TOOL]]" not in src_text
        assert "[[BETA-TOOL]]" not in src_text

        from vault_lint import check_broken_wikilinks
        assert check_broken_wikilinks(vault) == []


# ---------------------------------------------------------------------------
# Backfill bypasses; sweep respects the env gate
# ---------------------------------------------------------------------------


class TestBypassThreading:
    def test_backfill_cli_bypasses_guardrails(self, tmp_path, monkeypatch):
        _enable(monkeypatch)  # env ON, but backfill passes enforce_guardrails=False
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "notes" / "TOPIC.md"
        note.parent.mkdir(parents=True)
        note.write_text(textwrap.dedent("""\
        ---
        tags: [documentation]
        date: 2026-07-01
        ---

        # Big Important Topic

        This is a substantial note about a **Big Important Topic** with enough
        body text to clear the 100-byte floor and produce a heuristic entity.
        """), encoding="utf-8")

        totals = backfill_vault(vault, enforce_guardrails=False)
        # Bypass → pages created immediately, nothing staged.
        assert totals["pages_created"] >= 1
        assert totals["files_staged"] == 0
        assert not (vault / "_state" / "entity-mentions.json").exists()

    def test_sweep_respects_guardrails(self, tmp_path, monkeypatch):
        _enable(monkeypatch)  # env ON, sweep passes None → env-gated → enforced
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "notes" / "TOPIC.md"
        note.parent.mkdir(parents=True)
        note.write_text(textwrap.dedent("""\
        ---
        tags: [documentation]
        date: 2026-07-01
        ---

        # Big Important Topic

        This is a substantial note about a **Big Important Topic** with enough
        body text to clear the 100-byte floor and produce a heuristic entity.
        """), encoding="utf-8")

        totals = sweep_uncompiled(vault)
        # Enforced → first-sight single mention stages, no page created.
        assert totals["pages_created"] == 0
        assert totals["files_staged"] >= 1
        assert (vault / "_state" / "entity-mentions.json").exists()


# ---------------------------------------------------------------------------
# Edit ceiling
# ---------------------------------------------------------------------------


class TestEditCeiling:
    def test_ceiling_caps_and_reports_skipped(self, tmp_path, monkeypatch):
        _enable(monkeypatch, ENTITY_EDIT_CEILING=2)
        vault = tmp_path / "vault"
        vault.mkdir()
        for name in ("Alpha", "Beta", "Gamma", "Delta"):
            _seed_page(vault, name)

        skipped_before = {
            slug: (vault / "concepts" / f"{slug}.md").read_text(encoding="utf-8")
            for slug in ("GAMMA", "DELTA")
        }

        ents = [_entity(n) for n in ("Alpha", "Beta", "Gamma", "Delta")]
        report = compile_entities(ents, "NEWSRC.md", vault, enforce_guardrails=True)

        assert len(report.pages_updated) == 2
        assert report.updates_skipped_ceiling == ["GAMMA", "DELTA"]
        # Skipped pages are byte-unchanged.
        for slug, before in skipped_before.items():
            assert (vault / "concepts" / f"{slug}.md").read_text(encoding="utf-8") == before

    def test_ceiling_ignores_noop_duplicate_source(self, tmp_path, monkeypatch):
        """A same-source no-op update must NOT consume an edit-ceiling slot."""
        _enable(monkeypatch, ENTITY_EDIT_CEILING=1)
        vault = tmp_path / "vault"
        vault.mkdir()
        # ALPHA already carries the SEED section → updating from SEED is a no-op.
        _seed_page(vault, "Alpha")
        _seed_page(vault, "Beta")

        ents = [
            ExtractedEntity(name="Alpha", confidence=0.9, source_claims=["x"]),  # source SEED → no-op
            ExtractedEntity(name="Beta", confidence=0.9, source_claims=["y"]),   # source SEED → no-op too
        ]
        # Compile from SEED so both are no-ops; ceiling never consumed.
        report = compile_entities(ents, "SEED.md", vault, enforce_guardrails=True)
        assert report.pages_updated == []
        assert report.updates_skipped_ceiling == []

        # Now a genuinely-new source on BETA writes despite ceiling=1 having
        # "seen" two prior no-ops (proving no-ops didn't burn the slot).
        report2 = compile_entities(
            [ExtractedEntity(name="Beta", confidence=0.9, source_claims=["z"])],
            "FRESH.md", vault, enforce_guardrails=True,
        )
        assert len(report2.pages_updated) == 1
        assert report2.updates_skipped_ceiling == []


# ---------------------------------------------------------------------------
# Link cap (source frontmatter + concept page section still writes)
# ---------------------------------------------------------------------------


class TestLinkCap:
    def test_source_frontmatter_cap_counts_skips(self, tmp_path):
        source = tmp_path / "SRC.md"
        # related: needs a following line so the frontmatter regex keeps its \n.
        source.write_text("---\nrelated:\nkind: note\n---\n# Doc\n", encoding="utf-8")
        skipped = update_source_frontmatter(source, ["One", "Two", "Three"], related_link_cap=1)
        assert skipped == 2
        text = source.read_text(encoding="utf-8")
        assert "[[ONE]]" in text
        assert "[[TWO]]" not in text and "[[THREE]]" not in text

    def test_source_frontmatter_no_cap_inserts_all(self, tmp_path):
        source = tmp_path / "SRC.md"
        source.write_text("---\nrelated:\nkind: note\n---\n# Doc\n", encoding="utf-8")
        skipped = update_source_frontmatter(source, ["One", "Two", "Three"])
        assert skipped == 0
        text = source.read_text(encoding="utf-8")
        assert "[[ONE]]" in text and "[[TWO]]" in text and "[[THREE]]" in text

    def test_compile_threads_links_skipped_cap(self, tmp_path, monkeypatch):
        _enable(monkeypatch, ENTITY_LINK_CAP=1)
        vault = tmp_path / "vault"
        vault.mkdir()
        for name in ("Alpha", "Beta", "Gamma"):
            _seed_page(vault, name)
        source = vault / "SRC.md"
        source.write_text(
            "---\ntags: [documentation]\nrelated:\ndate: 2026-07-01\n---\n# Doc\n",
            encoding="utf-8",
        )
        ents = [_entity(n) for n in ("Alpha", "Beta", "Gamma")]
        report = compile_entities(ents, str(source), vault, enforce_guardrails=True)
        # 3 concept names, cap 1 → first inserts, other 2 skipped.
        assert report.links_skipped_cap == 2

    def test_capped_concept_page_still_appends_from_section(self, tmp_path):
        """The related: insertion is capped, but the ## From section + compiled_from still write."""
        vault = tmp_path / "vault"
        page = create_concept_page(
            ExtractedEntity(name="Widget", confidence=0.9, source_claims=["c"]),
            "SEED.md", vault,
        )
        # Page now has exactly one related entry ([[SEED]]). Cap=1 → related skip.
        wrote = update_concept_page(
            ExtractedEntity(name="Widget", source_claims=["new claim"]),
            "NEWSRC.md", page, related_link_cap=1,
        )
        assert wrote is True
        content = page.read_text(encoding="utf-8")
        assert "## From [[NEWSRC]]" in content
        assert "new claim" in content
        # compiled_from grows, related does not (still just SEED).
        fm = content.split("---")[1]
        assert '[[NEWSRC]]' in fm.split("compiled_from:")[1]
        assert '[[NEWSRC]]' not in fm.split("related:")[1].split("compiled_from:")[0]


# ---------------------------------------------------------------------------
# Fail-open + stale-ledger purge + confidence gate
# ---------------------------------------------------------------------------


class TestFailOpenAndEdges:
    def test_ledger_lock_timeout_fails_open(self, tmp_path, monkeypatch):
        """A lock-acquire failure falls open to the legacy immediate-create path."""
        _enable(monkeypatch)

        def _boom(*_a, **_k):
            raise TimeoutError("lock unavailable")

        monkeypatch.setattr("shared.file_lock", _boom)
        vault = tmp_path / "vault"
        vault.mkdir()
        report = compile_entities([_entity("Widget")], "SRC-A.md", vault, enforce_guardrails=True)
        # Fail-open → page created immediately, nothing staged, no ledger.
        assert (vault / "concepts" / "WIDGET.md").exists()
        assert report.entities_staged == []
        assert not (vault / "_state" / "entity-mentions.json").exists()

    def test_stale_ledger_purge(self, tmp_path, monkeypatch):
        """A page created out-of-band purges any lingering staging entry for its slug."""
        _enable(monkeypatch)
        vault = tmp_path / "vault"
        vault.mkdir()
        # 1) Stage Widget from one source.
        compile_entities([_entity("Widget")], "SRC-A.md", vault, enforce_guardrails=True)
        assert "WIDGET" in _load_mention_ledger(vault)["entities"]
        # 2) A bypassed backfill creates the page directly.
        create_concept_page(ExtractedEntity(name="Widget", source_claims=["x"]), "BYPASS.md", vault)
        assert find_existing_concept("Widget", vault) is not None
        # 3) A later mention updates the existing page and purges the stale entry.
        compile_entities([_entity("Widget", ["y"])], "SRC-B.md", vault, enforce_guardrails=True)
        assert "WIDGET" not in _load_mention_ledger(vault)["entities"]

    def test_daily_log_below_confidence_never_staged(self, tmp_path, monkeypatch):
        _enable(monkeypatch)
        vault = tmp_path / "vault"
        vault.mkdir()
        daily = str(vault / "daily" / "2026-07-11.md")  # daily → 0.85 threshold
        ent = ExtractedEntity(name="Fleeting", confidence=0.7, source_claims=["x"])
        report = compile_entities([ent], daily, vault, enforce_guardrails=True)
        assert report.entities_staged == []
        # No ledger entry — sub-threshold entities never reach staging.
        assert "FLEETING" not in _load_mention_ledger(vault)["entities"]


# ---------------------------------------------------------------------------
# Config resolver env roundtrip
# ---------------------------------------------------------------------------


class TestResolver:
    def test_guardrail_settings_env_roundtrip(self, monkeypatch):
        from config import get_entity_guardrail_settings, get_lint_delta_enabled

        s = get_entity_guardrail_settings()
        assert (s.enabled, s.page_min_mentions, s.edit_ceiling, s.link_cap) == (False, 2, 5, 8)
        assert get_lint_delta_enabled() is False

        monkeypatch.setenv("ENTITY_GUARDRAILS_ENABLED", "true")
        monkeypatch.setenv("ENTITY_PAGE_MIN_MENTIONS", "3")
        monkeypatch.setenv("ENTITY_EDIT_CEILING", "9")
        monkeypatch.setenv("ENTITY_LINK_CAP", "4")
        monkeypatch.setenv("LINT_DELTA_ENABLED", "true")

        s2 = get_entity_guardrail_settings()
        assert (s2.enabled, s2.page_min_mentions, s2.edit_ceiling, s2.link_cap) == (True, 3, 9, 4)
        assert get_lint_delta_enabled() is True

        # Explicit args win over env (Rule 1 None-sentinel).
        assert get_entity_guardrail_settings(enabled=False).enabled is False
        assert get_lint_delta_enabled(enabled=False) is False

    def test_report_defaults_are_empty(self):
        r = CompilationReport()
        assert r.entities_staged == []
        assert r.updates_skipped_ceiling == []
        assert r.links_skipped_cap == 0
