"""Tests for cognition.graph — wiki-link graph traversal."""

from __future__ import annotations

from pathlib import Path

from cognition.graph import (
    MemoryGraph,
    build_memory_graph,
    extract_links,
    get_hub_scores,
    get_neighbors,
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
