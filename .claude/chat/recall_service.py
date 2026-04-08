"""Unified recall service — THE sole runtime entrypoint for all memory consumers.

Closes Invariant I-3: recall is one shared service.
Chat, heartbeat, reflection, and weekly all call this module.
Runtime consumers must NOT import from cognition.recall directly.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

# Add scripts dir for memory_search, memory_index, config
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Cognition imports — graceful degradation if unavailable
try:
    from cognition.observability import RecallLog, log_recall_event
    from cognition.recall import (
        RecallResult,
        RecallTier,
        classify_tier,
        format_recall_results,
        run_recall_pipeline,
    )

    _COGNITION_AVAILABLE = True
except ImportError:
    _COGNITION_AVAILABLE = False

# Injection defense — always available (no heavy cognition dependency)
try:
    from cognition.injection import sanitize_recalled_content, wrap_recalled_memory

    _INJECTION_AVAILABLE = True
except ImportError:
    _INJECTION_AVAILABLE = False


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


class SearchMode(StrEnum):
    AUTO = "auto"  # Full pipeline: tier → expand → dual search → graph (default)
    KEYWORD = "keyword"  # FTS5 keyword only — fastest, no ONNX model
    HYBRID = "hybrid"  # Force keyword + vector, skip tier classification


@dataclass
class _FallbackLog:
    """Minimal RecallLog-compatible dataclass when cognition is unavailable."""

    tier: str = ""
    caller: str = ""
    search_mode: str = ""
    queries_generated: list[str] = field(default_factory=list)
    results_returned: int = 0
    top_scores: list[float] = field(default_factory=list)
    graph_hops_traversed: int = 0
    graph_neighbors_found: int = 0
    captures_written: int = 0
    latency_ms: float = 0.0
    expansion_model: str = ""
    expansion_latency_ms: float = 0.0


@dataclass
class _FallbackResult:
    """Minimal RecallResult-compatible dataclass when cognition is unavailable."""

    path: str = ""
    start_line: int = 0
    end_line: int = 0
    text: str = ""
    score: float = 0.0
    match_type: str = "keyword"
    section_title: str = ""
    graph_hops: int = 0
    source_query: str = ""


@dataclass
class RecallResponse:
    """Complete recall response with results, formatted text, and observability log."""

    results: list  # list[RecallResult] or list[_FallbackResult]
    formatted_text: str
    log: object  # RecallLog or _FallbackLog


def _persist_log(log: object) -> None:
    """Best-effort persist to ring buffer. Called ONLY at recall() boundary."""
    try:
        from cognition.observability import RecallLogStore
        RecallLogStore().append(log)
    except Exception:
        pass


def _make_log(tier: str = "", caller: str = "", search_mode: str = "") -> object:
    """Create a RecallLog or _FallbackLog depending on cognition availability."""
    if _COGNITION_AVAILABLE:
        log = RecallLog(tier=tier, caller=caller, search_mode=search_mode)
        return log
    return _FallbackLog(tier=tier, caller=caller, search_mode=search_mode)


@_get_observe()(name="recall", as_type="span")
async def recall(
    query: str,
    memory_dir: Path,
    search_mode: SearchMode = SearchMode.AUTO,
    caller: str = "chat",
    max_results: int = 5,
    has_prefetched: bool = False,
    is_slash_command: bool = False,
) -> RecallResponse:
    """Single recall API for all consumers.

    search_mode controls the search strategy:
    - AUTO: full cognition pipeline (tier classify → expand → dual search → graph)
    - KEYWORD: FTS5 keyword only — use when you only need exact term matches
    - HYBRID: force dual search (keyword + vector), skip tier classification
    """
    from config import RECALL_ENABLED

    def _update_span(log_obj: object, results_list: list | None = None) -> None:
        """Bridge RecallLog fields to Langfuse span metadata (fail-safe).

        Uses metadata (not output) because @observe auto-captures the return
        value as output — writing to output here would be overwritten.
        """
        try:
            from runtime.langfuse_setup import is_langfuse_enabled
            if is_langfuse_enabled():
                from langfuse import get_client
                get_client().update_current_span(metadata={
                    "recall_tier": str(getattr(log_obj, "tier", "unknown")),
                    "results_count": len(results_list) if results_list else 0,
                    "top_scores": [
                        r.score for r in (results_list or [])[:3]
                    ],
                    "latency_ms": getattr(log_obj, "latency_ms", 0),
                    "search_mode": str(getattr(log_obj, "search_mode", "unknown")),
                    "caller": caller,
                })
        except Exception:
            pass

    if not RECALL_ENABLED:
        log = _make_log(tier="disabled", caller=caller, search_mode=search_mode.value)
        _persist_log(log)
        _update_span(log)
        return RecallResponse(results=[], formatted_text="", log=log)

    if not query or not query.strip():
        log = _make_log(tier="empty_query", caller=caller, search_mode=search_mode.value)
        _persist_log(log)
        _update_span(log)
        return RecallResponse(results=[], formatted_text="", log=log)

    # Explicit keyword mode — always available, no cognition needed
    if search_mode == SearchMode.KEYWORD:
        result = _keyword_only_recall(query, caller, max_results)
        _persist_log(result.log)
        _update_span(result.log, result.results)
        return result

    if _COGNITION_AVAILABLE:
        if search_mode == SearchMode.HYBRID:
            # Skip tier classification, go straight to Tier 1 pipeline
            tier = RecallTier.TIER_1
        else:
            # AUTO: let tier classification decide
            tier = classify_tier(query, has_prefetched, is_slash_command)

        if tier in (RecallTier.SKIP, RecallTier.TIER_0):
            log = RecallLog(tier=tier.value, caller=caller, search_mode=search_mode.value)
            log_recall_event(log)
            _persist_log(log)
            _update_span(log)
            return RecallResponse(results=[], formatted_text="", log=log)

        results, log = await run_recall_pipeline(query, tier, memory_dir, max_results=max_results)
        log.caller = caller
        log.search_mode = search_mode.value
        formatted = format_recall_results(results) if results else ""
        log_recall_event(log)
        _persist_log(log)
        _update_span(log, results)
        return RecallResponse(results=results, formatted_text=formatted, log=log)

    else:
        # FALLBACK: keyword-only when cognition unavailable
        result = _keyword_only_recall(query, caller, max_results)
        _persist_log(result.log)
        _update_span(result.log, result.results)
        return result


def _keyword_only_recall(query: str, caller: str, max_results: int) -> RecallResponse:
    """Fallback when cognition module unavailable or search_mode=KEYWORD."""
    start = time.monotonic()

    log = _make_log(tier="fallback", caller=caller, search_mode="keyword")

    try:
        from config import RECALL_MIN_SCORE
        from memory_search import search_keyword

        raw_results = search_keyword(query, limit=max_results)
        raw_results = [r for r in raw_results if r.score >= RECALL_MIN_SCORE]

        # Convert SearchResult → uniform result type
        if _COGNITION_AVAILABLE:
            results = [
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
                for r in raw_results
            ]
        else:
            results = [
                _FallbackResult(
                    path=r.path,
                    start_line=r.start_line,
                    end_line=r.end_line,
                    text=r.text,
                    score=r.score,
                    match_type="keyword",
                    section_title=r.section_title,
                    source_query=query,
                )
                for r in raw_results
            ]

        if not results:
            log.latency_ms = (time.monotonic() - start) * 1000
            return RecallResponse(results=[], formatted_text="", log=log)

        # Sanitize via injection defense
        if _INJECTION_AVAILABLE:
            sanitized_items = []
            for r in results:
                text = r.text[:500].strip()
                safe_text = sanitize_recalled_content(text)
                if not safe_text:
                    continue  # Injection detected — skip
                source = r.path.replace("\\", "/")
                if "Memory/" in source:
                    source = source.split("Memory/")[-1]
                title = f" ({r.section_title})" if r.section_title else ""
                sanitized_items.append(
                    f"**{source}{title}** (score: {r.score:.2f}):\n{safe_text}"
                )
            formatted = wrap_recalled_memory(sanitized_items)
        else:
            # No injection module — fail closed, never inject unsanitized content
            log.latency_ms = (time.monotonic() - start) * 1000
            return RecallResponse(results=[], formatted_text="", log=log)

        log.results_returned = len(sanitized_items)
        log.top_scores = [r.score for r in results[:3]]
        log.latency_ms = (time.monotonic() - start) * 1000
        return RecallResponse(results=results, formatted_text=formatted, log=log)

    except Exception:
        log.latency_ms = (time.monotonic() - start) * 1000
        return RecallResponse(results=[], formatted_text="", log=log)


def reindex_file(file_path: Path, memory_dir: Path, generate_embeddings: bool = True) -> int:
    """Reindex a single memory file. Returns chunk count."""
    from db import get_memory_db
    from memory_index import index_file as _index_file

    db = get_memory_db()
    db.init_schema()
    chunks = _index_file(db, file_path, memory_dir, generate_embeddings=generate_embeddings)
    db.close()
    return chunks


def reindex_changed(memory_dir: Path, generate_embeddings: bool = True) -> dict[str, int]:
    """Reindex all changed memory files. Returns sync stats dict."""
    from memory_index import sync_index

    return sync_index(memory_dir=memory_dir, generate_embeddings=generate_embeddings)
