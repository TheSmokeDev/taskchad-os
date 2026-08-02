"""Recall-index acceptance tests for private curriculum seed imports."""

from __future__ import annotations

from pathlib import Path

import pytest

from curriculum.config import CurriculumSettings, CurriculumSource
from curriculum.paths import CurriculumPaths

_CONCEPT_TOKEN = "ZelphoraConceptKernel"
_SOURCE_TOKEN = "QuorvexSourceEvidence"


def _paths(root: Path) -> CurriculumPaths:
    profile = root / "ai-engineer"
    data = profile / "data"
    memory = profile / "memory"
    curriculum_data = data / "curricula"
    bundle = memory / "curricula" / "ai-engineering"
    return CurriculumPaths(
        persona_id="ai-engineer",
        profile_root=profile,
        data_root=data,
        memory_root=memory,
        curriculum_data=curriculum_data,
        bundle_root=bundle,
        artifacts_root=curriculum_data / "artifacts",
        raw_root=curriculum_data / "raw",
        vendor_root=curriculum_data / "vendor",
        ledger_path=curriculum_data / "curriculum.db",
        staging_path=profile / "state" / "memory-candidates.jsonl",
    )


def _settings() -> CurriculumSettings:
    return CurriculumSettings(
        persona_id="ai-engineer",
        enabled=True,
        domain="ai-engineering",
        sources=(
            CurriculumSource(
                id="cole-medin",
                url="https://www.youtube.com/@ColeMedin",
                policy="full",
            ),
        ),
    )


def _seed(root: Path) -> Path:
    seed_root = root / "vendor"
    (seed_root / "concepts").mkdir(parents=True)
    (seed_root / "sources").mkdir()
    (seed_root / "concepts" / "agent-kernels.md").write_text(
        "---\n"
        "type: concept\n"
        "title: Agent Kernels\n"
        "---\n\n"
        "# Agent Kernels\n\n"
        f"The private concept codeword is {_CONCEPT_TOKEN}.\n",
        encoding="utf-8",
    )
    (seed_root / "sources" / "seed-video.md").write_text(
        "---\n"
        "type: source\n"
        "title: Seed Video\n"
        "sources:\n"
        "  - id: youtube:seedvideo123\n"
        "    url: https://www.youtube.com/watch?v=seedvideo123\n"
        "---\n\n"
        "# Seed Video\n\n"
        f"The private source codeword is {_SOURCE_TOKEN}.\n",
        encoding="utf-8",
    )
    return seed_root


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    paths: CurriculumPaths,
) -> None:
    import curriculum.seed as seed_module

    monkeypatch.setattr(seed_module, "get_curriculum_settings", lambda _persona: _settings())
    monkeypatch.setattr(
        seed_module,
        "resolve_curriculum_paths",
        lambda _persona, _domain: paths,
    )


def _query(token: str, memory_root: Path) -> list[str]:
    from memory_search import search_keyword

    return [result.path for result in search_keyword(token, limit=10, memory_dir=memory_root)]


def test_seed_import_indexes_concepts_and_sources_for_persona_recall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import curriculum.seed as seed_module

    paths = _paths(tmp_path)
    seed_root = _seed(tmp_path)
    _configure(monkeypatch, paths)

    result = seed_module.import_okf_seed(
        "ai-engineer",
        seed_root,
        source_id="cole-medin",
        generate_embeddings=False,
    )

    assert result["success"] is True
    assert capsys.readouterr().out == ""
    assert result["index_stats"] is not None
    assert result["index_stats"]["files_indexed"] >= 2
    assert any(
        path.endswith("concepts/agent-kernels.md")
        for path in _query(_CONCEPT_TOKEN, paths.memory_root)
    )
    assert any(
        path.endswith("sources/seed-video.md") for path in _query(_SOURCE_TOKEN, paths.memory_root)
    )


def test_unchanged_seed_rerun_repairs_an_emptied_persona_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import config
    import curriculum.seed as seed_module
    from db import get_memory_db

    paths = _paths(tmp_path)
    seed_root = _seed(tmp_path)
    _configure(monkeypatch, paths)
    first = seed_module.import_okf_seed(
        "ai-engineer",
        seed_root,
        source_id="cole-medin",
        generate_embeddings=False,
    )
    assert first["success"] is True

    db = get_memory_db(db_path=config.resolve_db_path(paths.memory_root))
    db.init_schema()
    db.bulk_clear()
    db.close()
    assert _query(_CONCEPT_TOKEN, paths.memory_root) == []

    repaired = seed_module.import_okf_seed(
        "ai-engineer",
        seed_root,
        source_id="cole-medin",
        generate_embeddings=False,
    )

    assert repaired["success"] is True
    assert repaired["imported"] == 0
    assert repaired["index_stats"]["files_indexed"] >= 2
    assert _query(_CONCEPT_TOKEN, paths.memory_root)
    assert _query(_SOURCE_TOKEN, paths.memory_root)


def test_pruned_seed_source_is_removed_from_persona_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import curriculum.seed as seed_module

    paths = _paths(tmp_path)
    seed_root = _seed(tmp_path)
    _configure(monkeypatch, paths)
    first = seed_module.import_okf_seed(
        "ai-engineer",
        seed_root,
        source_id="cole-medin",
        generate_embeddings=False,
    )
    assert first["success"] is True
    assert _query(_SOURCE_TOKEN, paths.memory_root)

    (seed_root / "sources" / "seed-video.md").unlink()
    pruned = seed_module.import_okf_seed(
        "ai-engineer",
        seed_root,
        source_id="cole-medin",
        generate_embeddings=False,
    )

    assert pruned["success"] is True
    assert pruned["pruned"] == 1
    assert pruned["index_stats"]["files_removed"] == 1
    assert _query(_SOURCE_TOKEN, paths.memory_root) == []


def test_seed_index_failure_is_a_failed_import_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import curriculum.seed as seed_module
    import memory_index

    paths = _paths(tmp_path)
    seed_root = _seed(tmp_path)
    _configure(monkeypatch, paths)

    def _fail_sync(**_kwargs: object) -> dict[str, int]:
        raise RuntimeError("synthetic index failure")

    monkeypatch.setattr(memory_index, "sync_index", _fail_sync)
    result = seed_module.import_okf_seed(
        "ai-engineer",
        seed_root,
        source_id="cole-medin",
        generate_embeddings=False,
    )

    assert result["success"] is False
    assert result["index_stats"] is None
    assert any("synthetic index failure" in error for error in result["errors"])
