"""Tests for Phase 4: Memory Graph API layer.

Tests graph.py refactor (path-based), frontmatter parsing, node classification,
shortest_path, PageRank, betweenness, and RecallLogStore persistence.
"""

import sys
from pathlib import Path

import pytest

# Add chat dir for cognition imports
_CHAT_DIR = Path(__file__).resolve().parents[2] / "chat"
sys.path.insert(0, str(_CHAT_DIR))

from cognition.graph import (  # noqa: E402
    MemoryGraph,
    build_memory_graph,
    classify_node_type,
    compute_betweenness,
    compute_pagerank,
    get_hub_scores,
    get_neighbors,
    is_moc,
    normalize_link,
    parse_frontmatter,
    shortest_path,
)
from cognition.observability import RecallLog, RecallLogStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def vault_dir(tmp_path):
    """Create a mini vault with linked markdown files."""
    # SOUL.md links to USER.md and MEMORY.md
    (tmp_path / "SOUL.md").write_text(
        '---\ntags: [system, identity]\ndate: 2026-03-08\nsummary: "AI personality"\n---\n'
        "# SOUL\nSee [[USER]] and [[MEMORY]].\n",
        encoding="utf-8",
    )
    # USER.md links to SOUL.md
    (tmp_path / "USER.md").write_text(
        "---\ntags: [system, user]\n---\n# USER\nDefined in [[SOUL]].\n",
        encoding="utf-8",
    )
    # MEMORY.md links to SOUL.md and GOALS.md
    (tmp_path / "MEMORY.md").write_text(
        "# MEMORY\nPersonality in [[SOUL]]. Targets in [[GOALS]].\n",
        encoding="utf-8",
    )
    # GOALS.md — isolated from USER
    (tmp_path / "GOALS.md").write_text("# GOALS\nQ1 targets.\n", encoding="utf-8")
    # MOC-thehomie.md — many links (MOC)
    moc_links = " ".join(f"[[file{i}]]" for i in range(20))
    (tmp_path / "MOC-thehomie.md").write_text(f"# MOC\n{moc_links}\n", encoding="utf-8")
    # daily/2026-03-23.md
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / "2026-03-23.md").write_text("# Daily\nSession log.\n", encoding="utf-8")
    # weekly/2026-W12.md
    weekly_dir = tmp_path / "weekly"
    weekly_dir.mkdir()
    (weekly_dir / "2026-W12.md").write_text("# Weekly\nSynthesis.\n", encoding="utf-8")
    # drafts/draft-test.md
    drafts_dir = tmp_path / "drafts"
    drafts_dir.mkdir()
    (drafts_dir / "draft-test.md").write_text("# Draft\nContent.\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def graph(vault_dir):
    return build_memory_graph(vault_dir)


# ---------------------------------------------------------------------------
# normalize_link
# ---------------------------------------------------------------------------

def test_normalize_link_alias():
    assert normalize_link("SOUL|My Soul") == "soul"


def test_normalize_link_header():
    assert normalize_link("SOUL#Core Identity") == "soul"


def test_normalize_link_block_ref():
    assert normalize_link("SOUL^abc123") == "soul"


def test_normalize_link_combined():
    assert normalize_link("path/NOTE|display#heading") == "path/note"


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------

def test_parse_frontmatter_full():
    content = (
        '---\ntags: [system, identity]\ndate: 2026-03-08\n'
        'summary: "AI personality"\n---\n# Title'
    )
    meta = parse_frontmatter(content)
    assert meta["tags"] == ["system", "identity"]
    assert meta["date"] == "2026-03-08"
    assert meta["summary"] == "AI personality"


def test_parse_frontmatter_empty():
    assert parse_frontmatter("# No frontmatter\nJust text.") == {}


def test_parse_frontmatter_partial():
    content = "---\ntags: [a, b]\n---\n# Title"
    meta = parse_frontmatter(content)
    assert meta["tags"] == ["a", "b"]
    assert "date" not in meta
    assert "summary" not in meta


# ---------------------------------------------------------------------------
# classify_node_type
# ---------------------------------------------------------------------------

def test_classify_daily():
    assert classify_node_type("2026-03-23", "daily/2026-03-23.md") == "daily"


def test_classify_weekly():
    assert classify_node_type("2026-w12", "weekly/2026-W12.md") == "weekly"


def test_classify_draft():
    assert classify_node_type("draft-test", "drafts/draft-test.md") == "draft"


def test_classify_identity():
    assert classify_node_type("soul", "SOUL.md") == "identity"


def test_classify_moc():
    assert classify_node_type("moc-thehomie", "MOC-thehomie.md") == "moc"


def test_classify_doc_default():
    assert classify_node_type("some-note", "some-note.md") == "doc"


def test_classify_operational():
    assert classify_node_type("_dashboards", "_dashboards/overview.md") == "operational"


# ---------------------------------------------------------------------------
# build_memory_graph (path-based)
# ---------------------------------------------------------------------------

def test_graph_path_based_keys(graph):
    """All dicts should be keyed by rel_path, not stem."""
    assert "SOUL.md" in graph.path_to_stem
    assert "SOUL.md" in graph.forward_links
    assert "SOUL.md" in graph.link_counts


def test_graph_forward_links(graph):
    """SOUL links to USER and MEMORY."""
    targets = graph.forward_links.get("SOUL.md", [])
    assert "USER.md" in targets
    assert "MEMORY.md" in targets


def test_graph_backward_links(graph):
    """USER should have SOUL as a backlink."""
    backlinks = graph.backward_links.get("USER.md", [])
    assert "SOUL.md" in backlinks


def test_graph_no_self_links(graph):
    """SOUL references [[SOUL]] should not create self-loop."""
    # Actually SOUL.md doesn't link to itself in our fixture
    # But verify the general invariant
    for path, targets in graph.forward_links.items():
        assert path not in targets, f"Self-link found: {path}"


def test_graph_link_counts(graph):
    """SOUL has 2 outgoing (USER, MEMORY) + 2 incoming (USER, MEMORY) = 4."""
    assert graph.link_counts.get("SOUL.md", 0) == 4


def test_graph_stem_to_paths(graph):
    """stem_to_paths should map stem → list of rel_paths."""
    assert "soul" in graph.stem_to_paths
    assert "SOUL.md" in graph.stem_to_paths["soul"]


def test_graph_empty_dir(tmp_path):
    """Empty directory should return empty graph."""
    empty = tmp_path / "empty"
    empty.mkdir()
    g = build_memory_graph(empty)
    assert len(g.path_to_stem) == 0


def test_graph_nonexistent_dir(tmp_path):
    """Non-existent directory should return empty graph."""
    g = build_memory_graph(tmp_path / "nope")
    assert len(g.path_to_stem) == 0


# ---------------------------------------------------------------------------
# get_neighbors (backward compat: accepts stems)
# ---------------------------------------------------------------------------

def test_get_neighbors_by_path(graph):
    neighbors = get_neighbors(graph, ["SOUL.md"], max_hops=1)
    assert "USER.md" in neighbors
    assert "MEMORY.md" in neighbors


def test_get_neighbors_by_stem(graph):
    """Backward compat: stems should still work."""
    neighbors = get_neighbors(graph, ["soul"], max_hops=1)
    assert "USER.md" in neighbors


# ---------------------------------------------------------------------------
# is_moc
# ---------------------------------------------------------------------------

def test_is_moc_by_name(graph):
    assert is_moc("MOC-thehomie.md", graph) is True


def test_is_moc_by_stem(graph):
    assert is_moc("moc-thehomie", graph) is True


def test_is_moc_regular_note(graph):
    assert is_moc("SOUL.md", graph) is False


# ---------------------------------------------------------------------------
# get_hub_scores
# ---------------------------------------------------------------------------

def test_hub_scores_path_keyed(graph):
    scores = get_hub_scores(graph)
    assert "SOUL.md" in scores
    assert 0 <= scores["SOUL.md"] <= 1.0


# ---------------------------------------------------------------------------
# shortest_path
# ---------------------------------------------------------------------------

def test_shortest_path_direct(graph):
    """SOUL → USER is a direct link."""
    path = shortest_path(graph, "SOUL.md", "USER.md")
    assert path == ["SOUL.md", "USER.md"]


def test_shortest_path_multi_hop(graph):
    """USER → GOALS goes through SOUL → MEMORY → GOALS."""
    path = shortest_path(graph, "USER.md", "GOALS.md")
    assert len(path) >= 2
    assert path[0] == "USER.md"
    assert path[-1] == "GOALS.md"


def test_shortest_path_same_node(graph):
    assert shortest_path(graph, "SOUL.md", "SOUL.md") == ["SOUL.md"]


def test_shortest_path_disconnected(graph):
    """Daily note has no links — disconnected from SOUL."""
    path = shortest_path(graph, "SOUL.md", "daily/2026-03-23.md")
    assert path == []


def test_shortest_path_nonexistent(graph):
    assert shortest_path(graph, "SOUL.md", "nonexistent.md") == []


# ---------------------------------------------------------------------------
# compute_pagerank
# ---------------------------------------------------------------------------

def test_pagerank_returns_all_nodes(graph):
    pr = compute_pagerank(graph)
    assert "SOUL.md" in pr
    assert "USER.md" in pr


def test_pagerank_normalized(graph):
    pr = compute_pagerank(graph)
    max_val = max(pr.values())
    assert max_val == 1.0  # Normalized to 0-1


def test_pagerank_empty():
    g = MemoryGraph()
    assert compute_pagerank(g) == {}


# ---------------------------------------------------------------------------
# compute_betweenness
# ---------------------------------------------------------------------------

def test_betweenness_returns_all_nodes(graph):
    bc = compute_betweenness(graph)
    assert "SOUL.md" in bc


def test_betweenness_normalized(graph):
    bc = compute_betweenness(graph)
    vals = [v for v in bc.values() if v > 0]
    if vals:
        assert max(vals) == 1.0


def test_betweenness_empty():
    g = MemoryGraph()
    assert compute_betweenness(g) == {}


# ---------------------------------------------------------------------------
# RecallLogStore
# ---------------------------------------------------------------------------

def test_recall_log_store_roundtrip(tmp_path):
    log_path = tmp_path / "recall-log.json"
    store = RecallLogStore(path=log_path)

    log = RecallLog(
        tier="tier_1",
        caller="test",
        queries_generated=["hello"],
        results_returned=2,
        top_scores=[0.9, 0.7],
        graph_hops_traversed=1,
        graph_neighbors_found=5,
        latency_ms=123.456,
    )
    store.append(log)

    events = store.get_recent(5)
    assert len(events) == 1
    assert events[0]["caller"] == "test"
    assert events[0]["tier"] == "tier_1"
    assert events[0]["resultsCount"] == 2
    assert events[0]["latencyMs"] == 123.5


def test_recall_log_store_ring_buffer(tmp_path):
    log_path = tmp_path / "recall-log.json"
    store = RecallLogStore(path=log_path)

    for i in range(60):
        log = RecallLog(tier=f"tier_{i}", caller="test")
        store.append(log)

    events = store.get_recent(100)
    assert len(events) == 50  # Capped at MAX_EVENTS


def test_recall_log_store_empty(tmp_path):
    log_path = tmp_path / "recall-log.json"
    store = RecallLogStore(path=log_path)
    assert store.get_recent(5) == []
