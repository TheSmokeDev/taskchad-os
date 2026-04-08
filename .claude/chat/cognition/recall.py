"""Tiered recall gate + query expansion + dual search + graph traversal.

This is the core cognitive upgrade: classifies incoming messages into
recall tiers, optionally expands queries via LLM, runs dual search
(keyword + hybrid), traverses wiki-link graph neighbors, and merges
results with injection defense.

Pattern: PRD tier patterns, thehomie query expansion, existing search_hybrid().
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from cognition.graph import MemoryGraph, build_memory_graph, get_hub_scores, get_neighbors, is_moc
from cognition.injection import sanitize_recalled_content, wrap_recalled_memory
from cognition.observability import RecallLog


def _get_observe():
    """Return a decorator factory that defers Langfuse binding to call time.

    Unlike a one-shot import-time check, this wrapper re-checks
    ``is_langfuse_enabled()`` on every invocation so that decorators
    applied at module level still trace correctly even when the module
    is imported before ``init_langfuse()`` runs (e.g. Telegram bot path).
    """
    def _deferred_observe(**decorator_kwargs):
        def _decorator(fn):
            import functools

            @functools.wraps(fn)
            async def _async_wrapper(*args, **kwargs):
                try:
                    from runtime.langfuse_setup import is_langfuse_enabled
                    if is_langfuse_enabled():
                        from langfuse import observe
                        decorated = observe(**decorator_kwargs)(fn)
                        return await decorated(*args, **kwargs)
                except Exception:
                    pass
                return await fn(*args, **kwargs)

            @functools.wraps(fn)
            def _sync_wrapper(*args, **kwargs):
                try:
                    from runtime.langfuse_setup import is_langfuse_enabled
                    if is_langfuse_enabled():
                        from langfuse import observe
                        decorated = observe(**decorator_kwargs)(fn)
                        return decorated(*args, **kwargs)
                except Exception:
                    pass
                return fn(*args, **kwargs)

            import asyncio
            if asyncio.iscoroutinefunction(fn):
                return _async_wrapper
            return _sync_wrapper
        return _decorator
    return _deferred_observe


class RecallTier(Enum):
    SKIP = "skip"       # Router handled (prefetched/slash command)
    TIER_0 = "tier_0"   # Greetings/acks — no vault recall
    TIER_1 = "tier_1"   # Ambiguous/memory — query expansion + hybrid + graph


@dataclass
class RecallResult:
    """Enriched search result with graph metadata."""

    path: str
    start_line: int
    end_line: int
    text: str
    score: float
    match_type: str  # keyword | semantic | hybrid
    section_title: str = ""
    graph_hops: int = 0
    source_query: str = ""


# Tier 0: short greetings/acks that don't need recall
_TIER_0_PATTERNS = [
    re.compile(
        r"^(hi|hey|hello|sup|yo|gm|good\s*(morning|afternoon|evening))[\s!.,]*$", re.I
    ),
    re.compile(
        r"^(thanks|ok|got it|cool|nice|sounds good|yep|nope|do it|go ahead|approved)[\s!.,]*$",
        re.I,
    ),
    re.compile(r"^(yes|no|yeah|nah)[\s!.,]*$", re.I),
]

# Signals that the user wants to recall past context
_MEMORY_SIGNALS = re.compile(
    r"remember|remind|what do we know|where are we at|how are we looking|"
    r"last time|before|recently|history|pattern|deadline|decision|status|"
    r"like we discussed|you mentioned|what happened with|update on",
    re.I,
)


@_get_observe()(name="classify_tier", as_type="span")
def classify_tier(
    text: str,
    has_prefetched: bool = False,
    is_slash_command: bool = False,
) -> RecallTier:
    """Rules-first tier classification. No LLM call."""
    if has_prefetched or is_slash_command:
        tier = RecallTier.SKIP
    elif len(text.strip()) < 30 and any(
        pat.match(text.strip()) for pat in _TIER_0_PATTERNS
    ):
        tier = RecallTier.TIER_0
    else:
        tier = RecallTier.TIER_1

    try:
        from langfuse import get_client
        get_client().update_current_span(output={"tier": tier.value})
    except Exception:
        pass
    return tier


def _heuristic_expand(message_text: str) -> list[str]:
    """Simple heuristic query expansion — no LLM call.

    Kept as fallback for when brainstorm is unavailable or fails.
    """
    queries = [message_text]

    words = message_text.split()
    if len(words) >= 4:
        mid = len(words) // 2
        queries.append(" ".join(words[:mid]))
        queries.append(" ".join(words[mid:]))

    if _MEMORY_SIGNALS.search(message_text):
        cleaned = _MEMORY_SIGNALS.sub("", message_text).strip()
        if cleaned and cleaned != message_text:
            queries.append(cleaned)

    seen: set[str] = set()
    unique: list[str] = []
    for q in queries:
        q_lower = q.strip().lower()
        if q_lower and q_lower not in seen:
            seen.add(q_lower)
            unique.append(q.strip())

    return unique[:3]


async def expand_queries(
    message_text: str,
    conversation_summary: str = "",
    use_llm: bool = False,
) -> list[str]:
    """Query expansion — heuristic by default, LLM-brainstorm opt-in.

    Move 5c: The brainstorm path exists but is NOT in the hot path.
    Heuristic runs in <1ms. LLM brainstorm adds ~8-10s latency — only
    use when explicitly requested (e.g., deep research mode).
    """
    if not use_llm:
        return _heuristic_expand(message_text)

    # LLM path: blank-context brainstorm (v1 withRagContext pattern)
    try:
        from cognition.working_memory import Memory, WorkingMemory

        wm = WorkingMemory(soul_name="recall_expander")
        wm = wm.with_memory(Memory(
            role="system",
            content=(
                "You are a search query expert. Generate 3 precise search queries "
                "that would find relevant information in a personal knowledge base. "
                "Think like 3 different domain experts approaching the topic."
            ),
            region="identity",
        ))

        from cognition.steps import brainstorm

        _new_wm, ideas = await brainstorm(
            wm,
            f"Generate 3 expert search queries for: {message_text}",
        )

        if isinstance(ideas, list) and len(ideas) > 0:
            expert_queries = [str(q).strip() for q in ideas if str(q).strip()]
            if expert_queries:
                queries = [message_text] + expert_queries[:2]
                seen: set[str] = set()
                unique: list[str] = []
                for q in queries:
                    q_lower = q.strip().lower()
                    if q_lower and q_lower not in seen:
                        seen.add(q_lower)
                        unique.append(q.strip())
                return unique[:3]

    except Exception:
        pass

    return _heuristic_expand(message_text)


def _search_with_fallback(
    query: str,
    limit: int = 5,
) -> list[RecallResult]:
    """Run keyword + hybrid search with graceful fallback."""
    results: list[RecallResult] = []

    try:
        from memory_search import search_hybrid, search_keyword

        # Keyword search (fast, no embeddings)
        keyword_results = search_keyword(query, limit=limit)
        for r in keyword_results:
            results.append(
                RecallResult(
                    path=r.path,
                    start_line=r.start_line,
                    end_line=r.end_line,
                    text=r.text,
                    score=r.score,
                    match_type="keyword",
                    section_title=r.section_title,
                    source_query=query,
                )
            )

        # Hybrid search (needs embeddings — may fail on first run)
        try:
            hybrid_results = search_hybrid(query, limit=limit)
            for r in hybrid_results:
                results.append(
                    RecallResult(
                        path=r.path,
                        start_line=r.start_line,
                        end_line=r.end_line,
                        text=r.text,
                        score=r.score,
                        match_type="hybrid",
                        section_title=r.section_title,
                        source_query=query,
                    )
                )
        except Exception:
            pass  # Hybrid search optional — keyword is sufficient

    except Exception:
        pass  # Total search failure — return empty

    return results


async def _llm_rerank(
    results: list[RecallResult],
    query: str,
    top_n: int = 10,
    return_n: int = 5,
) -> list[RecallResult]:
    """LLM re-ranking: feed top_n results to a fast model, return reordered top return_n.

    Ported from Karpathy's qmd pattern. Only called for Tier 1 queries.
    On any failure (parse error, timeout, quota), returns original results unchanged.
    """
    import asyncio

    from config import RECALL_RERANK_TIMEOUT_S

    if len(results) <= return_n:
        return results

    candidates = results[:top_n]

    # Format results for the LLM
    numbered = []
    for i, r in enumerate(candidates):
        source = r.path.replace("\\", "/")
        if "Memory/" in source:
            source = source.split("Memory/")[-1]
        preview = r.text[:200].replace("\n", " ").strip()
        numbered.append(f"{i + 1}. [{source}] score={r.score:.2f}: {preview}")

    prompt = (
        f"Rank these search results by relevance to the query: \"{query}\"\n"
        f"Return ONLY the numbers in order, most relevant first. Example: 3,1,5,2,4\n\n"
        + "\n".join(numbered)
    )

    try:
        from runtime.registry import run_with_fallback

        response = await asyncio.wait_for(
            run_with_fallback(
                prompt,
                system="You are a search result ranker. Output only comma-separated numbers.",
                model_hint="haiku",
                max_tokens=100,
            ),
            timeout=RECALL_RERANK_TIMEOUT_S,
        )

        # Parse response: expect comma-separated numbers like "3,1,5,2,4"
        text = response.strip().strip(".")
        indices = []
        for part in re.split(r"[,\s]+", text):
            part = part.strip()
            if part.isdigit():
                idx = int(part) - 1  # 1-indexed to 0-indexed
                if 0 <= idx < len(candidates) and idx not in indices:
                    indices.append(idx)

        if len(indices) >= 2:
            reranked = [candidates[i] for i in indices[:return_n]]
            # Append any remaining that weren't in the LLM's ranking
            remaining = [r for i, r in enumerate(candidates) if i not in indices]
            return (reranked + remaining)[:return_n]

    except (asyncio.TimeoutError, Exception):
        pass  # Timeout or any error — return original ranking

    return results[:return_n]


def _merge_and_rank(
    results: list[RecallResult],
    graph: MemoryGraph,
) -> list[RecallResult]:
    """Merge, deduplicate by path:line range, rank with graph hub boost."""
    # Dedup by path:start_line-end_line
    seen: dict[str, RecallResult] = {}
    for r in results:
        key = f"{r.path}:{r.start_line}-{r.end_line}"
        if key not in seen or r.score > seen[key].score:
            seen[key] = r

    merged = list(seen.values())

    # Apply graph hub boost (hub_scores keyed by rel_path)
    hub_scores = get_hub_scores(graph)
    for r in merged:
        rel = r.path.replace("\\", "/")
        if "Memory/" in rel:
            rel = rel.split("Memory/")[-1]
        hub_score = hub_scores.get(rel, 0.0)
        # Up to 20% bonus for highest-connectivity notes
        r.score *= 1.0 + 0.2 * hub_score

    # Filter out MOC notes from recall payloads
    # CRITICAL: MOCs are graph ANCHORS, not recall PAYLOADS
    merged = [r for r in merged if not is_moc(Path(r.path).stem.lower(), graph)]

    # Sort by score descending
    merged.sort(key=lambda r: r.score, reverse=True)
    return merged


def format_recall_results(results: list[RecallResult]) -> str:
    """Format recall results into sanitized prompt text."""
    sanitized_items: list[str] = []

    for r in results:
        text = r.text[:500].strip()
        safe_text = sanitize_recalled_content(text)
        if not safe_text:
            continue  # Injection detected — skip

        source = r.path.replace("\\", "/")
        if "Memory/" in source:
            source = source.split("Memory/")[-1]

        title = f" ({r.section_title})" if r.section_title else ""
        hop_info = f" [graph:{r.graph_hops}hop]" if r.graph_hops > 0 else ""
        sanitized_items.append(
            f"**{source}{title}** (score: {r.score:.2f}{hop_info}):\n{safe_text}"
        )

    return wrap_recalled_memory(sanitized_items)


@_get_observe()(name="recall_pipeline", as_type="span")
async def run_recall_pipeline(
    message_text: str,
    tier: RecallTier,
    memory_dir: Path,
    conversation_summary: str = "",
    max_results: int = 5,
) -> tuple[list[RecallResult], RecallLog]:
    """Execute the full recall pipeline for a given tier.

    Tier 0: Return empty results (no recall)
    Tier 1: expand_queries -> per-query dual search -> graph neighbors -> merge -> sanitize

    Returns (results, log) tuple for observability.
    """
    import time

    start_time = time.monotonic()

    log = RecallLog(tier=tier.value)

    if tier == RecallTier.TIER_0 or tier == RecallTier.SKIP:
        log.latency_ms = (time.monotonic() - start_time) * 1000
        return [], log

    # Step 1: Query expansion
    queries = await expand_queries(message_text, conversation_summary)
    log.queries_generated = queries

    # Step 2: Per-query dual search (parallel via asyncio — Move 5c)
    import asyncio

    loop = asyncio.get_event_loop()
    search_tasks = [
        loop.run_in_executor(None, _search_with_fallback, q, 5)
        for q in queries
    ]
    search_results = await asyncio.gather(*search_tasks, return_exceptions=True)
    all_results: list[RecallResult] = []
    for result in search_results:
        if isinstance(result, list):
            all_results.extend(result)

    # Step 3: Graph neighbors (1-hop from matched notes)
    graph = build_memory_graph(memory_dir)
    matched_stems = list(
        {Path(r.path).stem.lower() for r in all_results}
    )
    neighbor_paths = get_neighbors(graph, matched_stems, max_hops=1)

    # Search neighbor content too
    for rel_path in neighbor_paths:
        neighbor_results = _search_with_fallback(message_text, limit=2)
        for r in neighbor_results:
            if Path(r.path).stem.lower() in {Path(p).stem.lower() for p in neighbor_paths}:
                r.graph_hops = 1
                all_results.append(r)
    log.graph_neighbors_found = len(neighbor_paths)
    log.graph_hops_traversed = 1 if neighbor_paths else 0

    # Step 4: Merge, dedup, rank
    merged = _merge_and_rank(all_results, graph)

    # Step 4.5: Optional LLM re-ranking for Tier 1 queries
    from config import RECALL_RERANK_ENABLED, RECALL_RERANK_TOP_N

    if RECALL_RERANK_ENABLED and tier == RecallTier.TIER_1 and len(merged) > 3:
        try:
            merged = await _llm_rerank(merged, message_text, top_n=RECALL_RERANK_TOP_N, return_n=max_results)
            log.reranked = True
        except Exception:
            log.reranked = False

    # Step 5: Cap at configured limit
    final = merged[:max_results]
    log.results_returned = len(final)
    log.top_scores = [r.score for r in final[:3]]

    log.latency_ms = (time.monotonic() - start_time) * 1000
    return final, log
