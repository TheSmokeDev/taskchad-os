"""
Memory Search for The Homie.

Three search modes: keyword (FTS5/tsvector), semantic (FastEmbed + vector),
and hybrid (weighted combination).

Usage:
    uv run python memory_search.py "query"                    # Hybrid search (default)
    uv run python memory_search.py "query" --mode keyword     # Keyword/BM25 only
    uv run python memory_search.py "query" --mode semantic    # Vector similarity only
    uv run python memory_search.py "query" --mode hybrid      # Weighted combination
    uv run python memory_search.py --test                     # Run test queries
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from config import (
    SEARCH_DEFAULT_LIMIT,
    SEARCH_KEYWORD_WEIGHT,
    SEARCH_MIN_SCORE,
    SEARCH_VECTOR_WEIGHT,
)
from db import get_memory_db


@dataclass
class SearchResult:
    """A single search result with metadata."""

    path: str
    start_line: int
    end_line: int
    text: str
    score: float
    match_type: str  # "keyword" | "semantic" | "hybrid"
    section_title: str = ""


def search_keyword(
    query: str,
    limit: int = SEARCH_DEFAULT_LIMIT,
    path_prefix: str = "",
) -> list[SearchResult]:
    """Keyword search (FTS5 for SQLite, tsvector for Postgres)."""
    if not query.strip():
        return []

    db = get_memory_db()
    db.init_schema()
    rows = db.keyword_search(query, limit, path_prefix=path_prefix)
    db.close()

    return [
        SearchResult(
            path=r["file_path"],
            start_line=r["start_line"],
            end_line=r["end_line"],
            text=r["content"],
            score=r["score"],
            match_type="keyword",
            section_title=r.get("section_title", ""),
        )
        for r in rows
    ]


def search_semantic(
    query: str,
    limit: int = SEARCH_DEFAULT_LIMIT,
    min_score: float = SEARCH_MIN_SCORE,
    path_prefix: str = "",
) -> list[SearchResult]:
    """Semantic search using vector similarity."""
    if not query.strip():
        return []

    from embeddings import embed_text

    query_embedding = embed_text(query)

    db = get_memory_db()
    db.init_schema()
    rows = db.vector_search(query_embedding, limit, path_prefix=path_prefix)
    db.close()

    return [
        SearchResult(
            path=r["file_path"],
            start_line=r["start_line"],
            end_line=r["end_line"],
            text=r["content"],
            score=r["score"],
            match_type="semantic",
            section_title=r.get("section_title", ""),
        )
        for r in rows
        if r["score"] >= min_score
    ]


def search_hybrid(
    query: str,
    limit: int = SEARCH_DEFAULT_LIMIT,
    min_score: float = SEARCH_MIN_SCORE,
    vector_weight: float = SEARCH_VECTOR_WEIGHT,
    keyword_weight: float = SEARCH_KEYWORD_WEIGHT,
    path_prefix: str = "",
    graph_scores: dict[str, float] | None = None,
) -> list[SearchResult]:
    """Hybrid search combining keyword and semantic with weighted scoring."""
    if not query.strip():
        return []

    from embeddings import embed_text

    query_embedding = embed_text(query)

    db = get_memory_db()
    db.init_schema()
    keyword_rows = db.keyword_search(query, limit * 2, path_prefix=path_prefix)
    semantic_rows = db.vector_search(query_embedding, limit * 2, path_prefix=path_prefix)
    db.close()

    # Merge by chunk key (path:start_line-end_line)
    merged: dict[str, dict] = {}
    scores: dict[str, dict[str, float]] = {}

    for r in keyword_rows:
        key = f"{r['file_path']}:{r['start_line']}-{r['end_line']}"
        if key not in merged:
            merged[key] = r
            scores[key] = {"keyword": 0.0, "semantic": 0.0}
        scores[key]["keyword"] = r["score"]

    for r in semantic_rows:
        key = f"{r['file_path']}:{r['start_line']}-{r['end_line']}"
        if key not in merged:
            merged[key] = r
            scores[key] = {"keyword": 0.0, "semantic": 0.0}
        scores[key]["semantic"] = r["score"]

    # Compute weighted scores and sort
    results: list[SearchResult] = []
    for key, data in merged.items():
        combined_score = (
            vector_weight * scores[key]["semantic"] + keyword_weight * scores[key]["keyword"]
        )

        # Graph hub boost: up to 20% bonus for highest-connectivity notes
        if graph_scores:
            from pathlib import Path as _Path

            stem = _Path(data["file_path"]).stem.lower()
            hub_score = graph_scores.get(stem, 0.0)
            combined_score *= 1.0 + 0.2 * hub_score

        if combined_score < min_score:
            continue
        results.append(
            SearchResult(
                path=data["file_path"],
                start_line=data["start_line"],
                end_line=data["end_line"],
                text=data["content"],
                score=combined_score,
                match_type="hybrid",
                section_title=data.get("section_title", ""),
            )
        )

    results.sort(key=lambda r: r.score, reverse=True)
    return results[:limit]


def search(
    query: str,
    mode: str = "hybrid",
    limit: int = SEARCH_DEFAULT_LIMIT,
    min_score: float = SEARCH_MIN_SCORE,
    path_prefix: str = "",
) -> list[SearchResult]:
    """Main search entry point. Dispatches to mode function."""
    if mode == "keyword":
        return search_keyword(query, limit, path_prefix=path_prefix)
    elif mode == "semantic":
        return search_semantic(query, limit, min_score, path_prefix=path_prefix)
    elif mode == "hybrid":
        return search_hybrid(query, limit, min_score, path_prefix=path_prefix)
    else:
        print(f"Unknown search mode: {mode}")
        return []


def format_results(results: list[SearchResult]) -> str:
    """Pretty-print search results with file paths, scores, and text snippets."""
    if not results:
        return "No results found."

    lines: list[str] = []
    lines.append(f"Found {len(results)} result(s):\n")

    for i, r in enumerate(results, 1):
        # Truncate text to 200 chars for display
        snippet = r.text.replace("\n", " ").strip()
        if len(snippet) > 200:
            snippet = snippet[:197] + "..."

        section = f" [{r.section_title}]" if r.section_title else ""
        lines.append(f"{i}. {r.path}:{r.start_line}-{r.end_line}{section}")
        lines.append(f"   Score: {r.score:.3f} ({r.match_type})")
        lines.append(f"   {snippet}")
        lines.append("")

    return "\n".join(lines)


def _run_test_queries() -> None:
    """Run predefined test queries across all modes."""
    test_queries = [
        ("heartbeat", "keyword"),
        ("proactive assistant", "semantic"),
        ("tasks overdue", "hybrid"),
    ]

    for query_text, mode in test_queries:
        print(f"\n{'=' * 60}")
        print(f"Query: '{query_text}' (mode: {mode})")
        print(f"{'=' * 60}")
        results = search(query_text, mode=mode, limit=3)
        output = format_results(results)
        try:
            print(output)
        except UnicodeEncodeError:
            print(output.encode("ascii", errors="replace").decode("ascii"))


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Search memory files")
    parser.add_argument("query", nargs="?", default="", help="Search query")
    parser.add_argument(
        "--mode",
        choices=["keyword", "semantic", "hybrid"],
        default="hybrid",
        help="Search mode (default: hybrid)",
    )
    parser.add_argument("--limit", type=int, default=SEARCH_DEFAULT_LIMIT, help="Max results")
    parser.add_argument("--min-score", type=float, default=SEARCH_MIN_SCORE, help="Min score")
    parser.add_argument("--path-prefix", default="", help="Filter results to files under this path prefix (e.g. 'drafts/sent')")
    parser.add_argument("--test", action="store_true", help="Run test queries")
    args = parser.parse_args()

    if args.test:
        _run_test_queries()
        return

    if not args.query:
        parser.error("query is required (or use --test)")

    results = search(args.query, mode=args.mode, limit=args.limit, min_score=args.min_score, path_prefix=args.path_prefix)
    output = format_results(results)
    # Handle Windows console encoding issues with Unicode characters
    try:
        print(output)
    except UnicodeEncodeError:
        print(output.encode("ascii", errors="replace").decode("ascii"))


if __name__ == "__main__":
    main()
