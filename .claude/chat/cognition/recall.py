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

from cognition.graph import (
    MemoryGraph,
    get_cached_memory_graph,
    get_hub_scores,
    get_neighbors,
    is_moc,
)
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
    match_type: str  # keyword | hybrid
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

    # Check the enabled flag OUTSIDE the try/except — if langfuse_setup itself
    # is broken (import error, flag check raises), we want that to surface as a
    # real traceback, not masquerade as intentionally disabled tracing.
    from runtime.langfuse_setup import is_langfuse_enabled
    if is_langfuse_enabled():
        # The client update itself is best-effort — swallow only this optional
        # side effect, not the setup check.
        try:
            from langfuse import get_client
            get_client().update_current_span(metadata={"tier": tier.value})
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
    memory_dir: "Path | None" = None,
) -> list[RecallResult]:
    """Run keyword + hybrid search with graceful fallback.

    ``memory_dir`` selects the per-vault DB (None => thehomie, unchanged).

    Each leg is floored on its OWN score scale before it reaches the merge: the
    keyword leg is raw FTS5 (``1/(1+|bm25|)``, ~0.05-0.17 for real hits — see
    38882d34) floored by ``RECALL_KEYWORD_MIN_SCORE``; the hybrid leg is a
    weighted merge floored by ``RECALL_MIN_SCORE`` (the documented merged-score
    knob). Wiring both floors here is what makes those two knobs real (#136).
    """
    import config as _cfg  # noqa: PLC0415 — Rule 2 module-attr so evolve/monkeypatch overrides propagate.

    results: list[RecallResult] = []

    try:
        from memory_search import search_hybrid, search_keyword

        # Keyword search (fast, no embeddings). Floor on the FTS5 scale.
        keyword_results = search_keyword(query, limit=limit, memory_dir=memory_dir)
        for r in keyword_results:
            if r.score < _cfg.RECALL_KEYWORD_MIN_SCORE:
                continue
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

        # Hybrid search (needs embeddings — may fail on first run). RECALL_MIN_SCORE
        # is the documented merged-score floor (hybrid/vector scale) — wire it
        # instead of letting search_hybrid fall back to its own SEARCH_MIN_SCORE.
        try:
            hybrid_results = search_hybrid(
                query, limit=limit, min_score=_cfg.RECALL_MIN_SCORE, memory_dir=memory_dir
            )
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

    except Exception as e:
        print(f"[Recall] _search_with_fallback failed (non-blocking): {e}")

    return results


# Position-aware retrieval/reranker blend (qmd pattern). A verbatim rerank
# lets one bad model call bury an exact match that retrieval put at #1; the
# blend keeps the reranker advisory for the retrieval head and decisive only
# for the tail. Bands scaled to our top_n=10 candidate pool (qmd's 1-3/4-10/11+
# bands assume ~30): original rank 0-2 -> 75% retrieval weight, 3-5 -> 60%,
# 6+ -> 40%. k=10 (not qmd's 60) so rank gaps stay meaningful over 10 items.
_RERANK_RETRIEVAL_WEIGHT_BANDS = ((3, 0.75), (6, 0.60))
_RERANK_RETRIEVAL_WEIGHT_TAIL = 0.40
_RERANK_RRF_K = 10


def _rerank_blend(
    candidates: list[RecallResult],
    llm_indices: list[int],
) -> list[RecallResult]:
    """Blend the original retrieval order with the LLM ordering, position-aware.

    Both orderings are converted to RRF-style scores (1/(k+rank)); each
    candidate's blend weight depends on its ORIGINAL retrieval rank per the
    band constants above. Candidates the LLM omitted rank behind everything it
    did rank, preserving their relative retrieval order. Deterministic
    tiebreak on retrieval position.
    """
    llm_rank = {idx: pos for pos, idx in enumerate(llm_indices)}
    unranked_base = len(llm_indices)
    scored = []
    for retr_pos, result in enumerate(candidates):
        weight = _RERANK_RETRIEVAL_WEIGHT_TAIL
        for band_end, band_weight in _RERANK_RETRIEVAL_WEIGHT_BANDS:
            if retr_pos < band_end:
                weight = band_weight
                break
        retr_score = 1.0 / (_RERANK_RRF_K + retr_pos)
        rr_pos = llm_rank.get(retr_pos, unranked_base + retr_pos)
        rr_score = 1.0 / (_RERANK_RRF_K + rr_pos)
        blended = weight * retr_score + (1.0 - weight) * rr_score
        scored.append((blended, retr_pos, result))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [r for _, _, r in scored]


async def _llm_rerank(
    results: list[RecallResult],
    query: str,
    top_n: int = 10,
    return_n: int = 5,
) -> list[RecallResult]:
    """LLM re-ranking: feed top_n results to a fast model, return blended top return_n.

    Ported from Karpathy's qmd pattern, including its position-aware blend —
    the LLM ordering is fused with the retrieval ordering rather than taken
    verbatim (see _rerank_blend). Only called for Tier 1 queries.
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
        response = await asyncio.wait_for(
            _run_rerank_request(prompt),
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
            return _rerank_blend(candidates, indices)[:return_n]

    except (asyncio.TimeoutError, Exception):
        pass  # Timeout or any error — return original ranking

    return results[:return_n]


async def _run_rerank_request(prompt: str) -> str:
    """Run the cheap rerank request through the shared runtime lane facade."""

    from runtime.base import RuntimeRequest
    from runtime.capabilities import TEXT_REASONING
    from runtime.lane_router import run_with_runtime_lanes

    result = await run_with_runtime_lanes(
        RuntimeRequest(
            prompt=prompt,
            cwd=Path.cwd(),
            task_name="recall_rerank",
            capability=TEXT_REASONING,
            model="haiku",
            max_turns=1,
            max_budget_usd=0.10,
            allowed_tools=[],
            system_prompt="You are a search result ranker. Output only comma-separated numbers.",
        )
    )
    return result.text


def _merge_and_rank(
    results: list[RecallResult],
    graph: MemoryGraph,
    top_n: int | None = None,
) -> list[RecallResult]:
    """Merge, deduplicate by path:line range, rank with graph hub boost.

    Keyword-leg scores are raw FTS5 (``1/(1+|bm25|)``, ~0.05-0.17); hybrid-leg
    scores are weighted merges (>= RECALL_MIN_SCORE). Scores are NEVER
    rewritten: evolve replay/compare/veto consume ABSOLUTE top_scores, and a
    per-leg max-normalization here flattened every leg's peak to 1.0 — blinding
    the veto's avg_top_score_delta floor and letting a lone floor-passing
    keyword hit outrank a strong hybrid (#136 gate, both vendors).

    Cross-scale fairness is a REPRESENTATION guarantee instead: with ``top_n``,
    each leg that survived dedup + the MOC filter keeps its best hit inside the
    first ``top_n`` slots, so an exact keyword/ID match is never blindly cut by
    the cap while ranking, display, and evolve fitness all stay on raw scores.
    """
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

    # Leg representation (#136): the legs score on different raw scales, so the
    # cap window can be starved of a whole leg by scale alone. AFTER the MOC
    # filter (an ineligible peak must not hold a slot), promote each absent
    # leg's best hit into the tail of the cap window — one slot per leg, never
    # ahead of the in-window results, scores untouched.
    if top_n is not None and top_n > 0 and len(merged) > top_n:
        slot = top_n - 1
        head_legs = {r.match_type for r in merged[:top_n]}
        for leg in sorted({r.match_type for r in merged} - head_legs):
            if slot < 0:
                break
            best = next(r for r in merged if r.match_type == leg)
            merged.remove(best)
            merged.insert(slot, best)
            slot -= 1
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
        loop.run_in_executor(None, _search_with_fallback, q, 5, memory_dir)
        for q in queries
    ]
    # The graph lookup depends only on memory_dir, not on the searches above,
    # so it rides the SAME gather — a cold rebuild overlaps with search latency
    # instead of stacking after it. Cached in cognition.graph (issue #129).
    graph_task = loop.run_in_executor(None, get_cached_memory_graph, memory_dir)

    gathered = await asyncio.gather(*search_tasks, graph_task, return_exceptions=True)
    *search_results, graph_result = gathered
    all_results: list[RecallResult] = []
    for result in search_results:
        if isinstance(result, list):
            all_results.extend(result)

    # Step 3: Graph neighbors (1-hop from matched notes)
    if isinstance(graph_result, MemoryGraph):
        graph = graph_result
    else:
        print(f"[cognition.recall] graph build failed (non-fatal): {graph_result!r}", flush=True)
        graph = MemoryGraph()
    matched_stems = list(
        {Path(r.path).stem.lower() for r in all_results}
    )
    neighbor_paths = get_neighbors(graph, matched_stems, max_hops=1)

    # Search neighbor content ONCE, not once per neighbor. The old loop re-ran
    # an identical message_text query per neighbor_path with no per-iteration
    # variation; _merge_and_rank() dedups those identical results by
    # path:line-range anyway, so one call yields the same merged output.
    if neighbor_paths:
        neighbor_stems = {Path(p).stem.lower() for p in neighbor_paths}
        neighbor_results = await loop.run_in_executor(
            None, _search_with_fallback, message_text, 2, memory_dir
        )
        for r in neighbor_results:
            if Path(r.path).stem.lower() in neighbor_stems:
                r.graph_hops = 1
                all_results.append(r)
    log.graph_neighbors_found = len(neighbor_paths)
    log.graph_hops_traversed = 1 if neighbor_paths else 0

    # Step 4: Merge, dedup, rank. top_n=max_results arms the leg-representation
    # guarantee at exactly the cap the caller will slice to (#136).
    merged = _merge_and_rank(all_results, graph, top_n=max_results)

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
