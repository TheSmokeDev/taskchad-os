"""Tests for cognition.graph — wiki-link graph traversal."""

from __future__ import annotations

import functools
import os
import threading
import time
from pathlib import Path

import cognition.graph as graph_mod
from cognition.graph import (
    MemoryGraph,
    build_memory_graph,
    extract_links,
    get_cached_memory_graph,
    get_hub_scores,
    get_neighbors,
    invalidate_graph_cache,
)


def test_extract_links_basic():
    content = "See [[MEMORY]] and [[GOALS]] for details."
    links = extract_links(content)
    assert sorted(links) == ["GOALS", "MEMORY"]


def test_extract_links_empty():
    assert extract_links("No links here") == []


def test_extract_links_dedup():
    content = "Check [[SOUL]] then [[SOUL]] again."
    links = extract_links(content)
    assert links == ["SOUL"]


def test_build_graph_simple(tmp_path: Path):
    """3 files with cross-links -> correct forward/backward maps (path-based)."""
    (tmp_path / "SOUL.md").write_text("See [[USER]] and [[MEMORY]]", encoding="utf-8")
    (tmp_path / "USER.md").write_text("Links to [[SOUL]]", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("No links here", encoding="utf-8")

    graph = build_memory_graph(tmp_path)

    # stem_to_path backward-compat property still works
    assert "soul" in graph.stem_to_path
    assert "user" in graph.stem_to_path
    assert "memory" in graph.stem_to_path

    # Path-based keys: forward_links keyed by rel_path
    assert "USER.md" in graph.forward_links["SOUL.md"]
    assert "MEMORY.md" in graph.forward_links["SOUL.md"]

    # USER links to SOUL
    assert "SOUL.md" in graph.forward_links["USER.md"]

    # Backlinks: USER has backlink from SOUL (path-based)
    assert "SOUL.md" in graph.backward_links["USER.md"]


def test_build_graph_empty_dir(tmp_path: Path):
    graph = build_memory_graph(tmp_path)
    assert graph.forward_links == {}
    assert graph.stem_to_path == {}


def test_build_graph_nonexistent_dir():
    graph = build_memory_graph(Path("/nonexistent/path"))
    assert graph.forward_links == {}


def test_get_neighbors_one_hop(tmp_path: Path):
    """Start from hub -> returns connected notes."""
    (tmp_path / "hub.md").write_text("Links to [[a]] and [[b]]", encoding="utf-8")
    (tmp_path / "a.md").write_text("Links to [[c]]", encoding="utf-8")
    (tmp_path / "b.md").write_text("No links", encoding="utf-8")
    (tmp_path / "c.md").write_text("No links", encoding="utf-8")

    graph = build_memory_graph(tmp_path)
    neighbors = get_neighbors(graph, ["hub"], max_hops=1)

    # Should find a and b (1-hop), but not c (2-hop)
    neighbor_stems = [Path(p).stem.lower() for p in neighbors]
    assert "a" in neighbor_stems
    assert "b" in neighbor_stems
    assert "c" not in neighbor_stems


def test_get_neighbors_cap(tmp_path: Path):
    """Many connections -> capped at max_per_start."""
    content = " ".join(f"[[note{i}]]" for i in range(20))
    (tmp_path / "hub.md").write_text(content, encoding="utf-8")
    for i in range(20):
        (tmp_path / f"note{i}.md").write_text("content", encoding="utf-8")

    graph = build_memory_graph(tmp_path)
    neighbors = get_neighbors(graph, ["hub"], max_hops=1, max_per_start=3)

    assert len(neighbors) <= 3


def test_get_hub_scores(tmp_path: Path):
    """Hub scores are normalized 0-1 (path-keyed)."""
    (tmp_path / "hub.md").write_text("[[a]] [[b]] [[c]]", encoding="utf-8")
    (tmp_path / "a.md").write_text("[[hub]]", encoding="utf-8")
    (tmp_path / "b.md").write_text("no links", encoding="utf-8")
    (tmp_path / "c.md").write_text("no links", encoding="utf-8")

    graph = build_memory_graph(tmp_path)
    scores = get_hub_scores(graph)

    # Hub should have highest score (most connections) — path-keyed
    assert scores["hub.md"] == 1.0
    # b has only 1 connection (backlink from hub)
    assert 0.0 < scores["b.md"] < 1.0


def test_get_hub_scores_empty():
    graph = MemoryGraph()
    assert get_hub_scores(graph) == {}


# ---------------------------------------------------------------------------
# Graph cache (issue #129) — get_cached_memory_graph / invalidate_graph_cache
#
# Vaults are built as <root>/memory with a sibling <root>/data/, which is the
# profile layout resolve_db_path() maps to <root>/data/memory.db. That keeps
# the cache's index-DB signal entirely inside tmp_path — no monkeypatching of
# config.DATA_DIR, and no risk of touching the real .claude/data/ directory.
# ---------------------------------------------------------------------------


def _make_vault(tmp_path: Path) -> tuple[Path, Path]:
    """Build a small cross-linked vault. Returns (memory_dir, db_path)."""
    import config as cfg_mod

    memory_dir = tmp_path / "vault" / "memory"
    memory_dir.mkdir(parents=True)
    (tmp_path / "vault" / "data").mkdir()

    (memory_dir / "SOUL.md").write_text("See [[USER]] and [[MEMORY]]", encoding="utf-8")
    (memory_dir / "USER.md").write_text("Links to [[SOUL]]", encoding="utf-8")
    (memory_dir / "MEMORY.md").write_text("No links here", encoding="utf-8")
    # concepts/ must pre-exist so a later write INTO it leaves memory_dir's own
    # mtime untouched — that is what makes the invalidation test discriminating.
    (memory_dir / "concepts").mkdir()
    (memory_dir / "concepts" / "SEED.md").write_text("Seed [[SOUL]]", encoding="utf-8")

    db_path = cfg_mod.resolve_db_path(memory_dir)
    assert db_path == tmp_path / "vault" / "data" / "memory.db", (
        "test isolation broken: db must resolve inside tmp_path"
    )
    db_path.write_bytes(b"stub-index")
    return memory_dir, db_path


def _spy_on_build(monkeypatch) -> list[Path]:
    """Count real build_memory_graph() calls while preserving behavior."""
    calls: list[Path] = []
    real = graph_mod.build_memory_graph

    @functools.wraps(real)
    def _counting(memory_dir: Path) -> MemoryGraph:
        calls.append(memory_dir)
        return real(memory_dir)

    monkeypatch.setattr(graph_mod, "build_memory_graph", _counting)
    return calls


def _bump_db_mtime(db_path: Path) -> None:
    """Advance the index DB mtime deterministically.

    Real vault writes always reindex, which touches this file. An explicit
    utime jump avoids depending on filesystem mtime resolution.
    """
    current = db_path.stat().st_mtime_ns
    ahead = current + 1_000_000_000
    os.utime(db_path, ns=(ahead, ahead))


def test_cached_graph_second_call_is_a_cache_hit(tmp_path: Path, monkeypatch):
    """2nd call against an unchanged vault must NOT re-read the vault."""
    memory_dir, _db = _make_vault(tmp_path)
    invalidate_graph_cache()
    calls = _spy_on_build(monkeypatch)

    first = get_cached_memory_graph(memory_dir)
    second = get_cached_memory_graph(memory_dir)

    assert len(calls) == 1, f"expected 1 vault scan, got {len(calls)}"
    assert first.forward_links == second.forward_links
    assert second is first  # same cached object handed back


def test_cached_graph_invalidates_on_nested_subdirectory_write(
    tmp_path: Path, monkeypatch
):
    """A write into an EXISTING nested dir must invalidate.

    Guards against regressing to a directory-mtime signal. concepts/ already
    exists, so adding a file inside it changes concepts/'s mtime but NOT the
    vault root's — a root-directory-mtime signal would never fire here, and
    this test would catch that.
    """
    memory_dir, db_path = _make_vault(tmp_path)
    invalidate_graph_cache()
    calls = _spy_on_build(monkeypatch)

    root_mtime_before = memory_dir.stat().st_mtime_ns
    first = get_cached_memory_graph(memory_dir)
    assert "concepts/NEW-CONCEPT.md" not in first.forward_links

    (memory_dir / "concepts" / "NEW-CONCEPT.md").write_text(
        "Back to [[SOUL]]", encoding="utf-8"
    )
    _bump_db_mtime(db_path)
    assert memory_dir.stat().st_mtime_ns == root_mtime_before, (
        "precondition: a nested write must not bump the vault root's mtime — "
        "otherwise this test cannot catch a directory-mtime regression"
    )

    second = get_cached_memory_graph(memory_dir)

    assert len(calls) == 2, "vault change must trigger a rebuild"
    assert "concepts/NEW-CONCEPT.md" in second.forward_links
    assert "new-concept" in second.stem_to_paths


def test_cached_graph_kill_switch_bypasses_cache(tmp_path: Path, monkeypatch):
    """RECALL_GRAPH_CACHE_ENABLED=false => every call rebuilds."""
    import config as cfg_mod

    memory_dir, _db = _make_vault(tmp_path)
    invalidate_graph_cache()
    # config reads env at import time, so the module attribute — not the env
    # var — is what get_cached_memory_graph() actually resolves at call time.
    monkeypatch.setattr(cfg_mod, "RECALL_GRAPH_CACHE_ENABLED", False)
    calls = _spy_on_build(monkeypatch)

    get_cached_memory_graph(memory_dir)
    get_cached_memory_graph(memory_dir)

    assert len(calls) == 2, "kill switch must disable caching"


def test_cached_graph_separate_vaults_do_not_cross_pollinate(
    tmp_path: Path, monkeypatch
):
    """Multi-vault: each memory_dir keeps its own cache entry."""
    a_dir, _a_db = _make_vault(tmp_path / "a")
    b_dir, _b_db = _make_vault(tmp_path / "b")
    (b_dir / "ONLY-B.md").write_text("Unique to [[SOUL]]", encoding="utf-8")
    invalidate_graph_cache()

    a_graph = get_cached_memory_graph(a_dir)
    b_graph = get_cached_memory_graph(b_dir)

    assert "ONLY-B.md" in b_graph.forward_links
    assert "ONLY-B.md" not in a_graph.forward_links
    # Cache hits stay per-vault.
    assert get_cached_memory_graph(a_dir) is a_graph
    assert get_cached_memory_graph(b_dir) is b_graph


def test_cached_graph_without_index_db_always_rebuilds(tmp_path: Path, monkeypatch):
    """No index DB => signal 0 => never cache (matches pre-fix behavior)."""
    memory_dir, db_path = _make_vault(tmp_path)
    db_path.unlink()
    invalidate_graph_cache()
    calls = _spy_on_build(monkeypatch)

    get_cached_memory_graph(memory_dir)
    get_cached_memory_graph(memory_dir)

    assert len(calls) == 2, "an unindexed vault must not be cached"


def test_cached_graph_coalesces_concurrent_rebuilds(tmp_path: Path, monkeypatch):
    """N concurrent misses on the SAME vault must produce exactly ONE rebuild.

    This is the entire reason _GRAPH_CACHE_LOCK spans the rebuild itself, not
    just the check — release it early and this test starts failing with N
    rebuilds instead of 1 (issue #129's whole point).
    """
    memory_dir, _db = _make_vault(tmp_path)
    invalidate_graph_cache()
    real_build = graph_mod.build_memory_graph
    calls: list[Path] = []

    def _slow_build(md: Path) -> MemoryGraph:
        calls.append(md)
        time.sleep(0.05)  # widen the race window
        return real_build(md)

    monkeypatch.setattr(graph_mod, "build_memory_graph", _slow_build)

    results: list = [None] * 8

    def _call(i: int) -> None:
        results[i] = get_cached_memory_graph(memory_dir)

    threads = [threading.Thread(target=_call, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(calls) == 1, f"expected exactly 1 rebuild for 8 concurrent misses, got {len(calls)}"
    assert all(r is results[0] for r in results), (
        "every caller must share the identical cached object"
    )


def test_cached_graph_rebuild_for_one_vault_blocks_hit_for_another(
    tmp_path: Path, monkeypatch
):
    """Documents a known, accepted tradeoff: _GRAPH_CACHE_LOCK is process-global,
    not per-vault. A slow rebuild for vault A blocks an already-cached HIT for
    vault B until A releases the lock — see get_cached_memory_graph's docstring
    NOTE. With today's two vaults this window is narrow; if a per-key lock is
    ever introduced, this test should start failing and can be inverted.
    """
    a_dir, _a_db = _make_vault(tmp_path / "a")
    b_dir, _b_db = _make_vault(tmp_path / "b")
    invalidate_graph_cache()

    get_cached_memory_graph(b_dir)  # warm B — the timed call below is a guaranteed HIT

    real_build = graph_mod.build_memory_graph
    slow_seconds = 0.2

    def _slow_build(md: Path) -> MemoryGraph:
        if md == a_dir:
            time.sleep(slow_seconds)
        return real_build(md)

    monkeypatch.setattr(graph_mod, "build_memory_graph", _slow_build)

    a_thread = threading.Thread(target=lambda: get_cached_memory_graph(a_dir))
    a_thread.start()
    time.sleep(0.02)  # let A acquire the lock and enter the slow rebuild

    start = time.monotonic()
    b_graph = get_cached_memory_graph(b_dir)
    elapsed = time.monotonic() - start

    a_thread.join(timeout=5)

    assert b_graph is not None
    assert elapsed >= slow_seconds * 0.5, (
        f"cache HIT for vault B took only {elapsed:.3f}s while vault A's rebuild "
        f"was in flight (expected >= {slow_seconds * 0.5}s) — either the lock "
        "stopped serializing unrelated vaults (update the docstring NOTE and "
        "invert this assertion) or this test is flaky"
    )
