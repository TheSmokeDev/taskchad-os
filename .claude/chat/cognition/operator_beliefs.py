"""Reflection-time operator-belief extraction (Living Self Act 1, B2 + M2).

Forms operator beliefs from the operator's VERBATIM ``role == "user"`` words
(read from chat.db via ``session.read_operator_user_turns``) using a real LLM
claim-extraction step — NOT a regex/keyword filter (operator-forbidden band-aid)
and NOT the daily-log paraphrase (the bot's third-person restatement).

Hosted INSIDE the existing 8 AM daily reflection loop
(``memory_reflect._run_reflection_inner``): amortized once per reflection,
provider-agnostic (Claude -> Codex -> Gemini via ``cognition.steps.reasoning_step``),
never the chat hot path. The extractor emits the never-built ``reflection`` /
``explicit`` sources for ``self-model-inferences.json``.

Rule 1: knobs via ``get_inference_extraction_settings`` (call-time None-sentinel).
Rule 3: any Langfuse span via ``langfuse_setup.get_observation_client`` (module-
attribute lookup), fail-open None.
M2: tolerant JSON parse — unwrap the ``{"claims":[…]}`` provider-wrap before
filtering so Codex/Gemini variance doesn't silently yield zero claims.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _coerce_claim_list(parsed: Any) -> list:
    """Normalize a provider response into a list of claim objects (M2).

    ``reasoning_step`` returns ``result.parsed`` = ``_extract_json(text)`` which
    tolerates raw JSON and ```json fences. But Codex/Gemini frequently wrap a
    requested array as ``{"claims":[…]}`` / ``{"beliefs":[…]}`` even when asked
    for a bare array; the naive ``[c for c in parsed]`` would then iterate the
    DICT's KEYS (strings) and silently return zero dicts.

    - list -> passes through.
    - dict -> unwrap the first list under a known key (claims/beliefs/items/
      inferences/contradictions/conflicts), or, failing that, the SOLE
      list-valued field.
    - anything else (None / garbage / a dict with no usable list) -> ``[]``
      (fail-open: zero claims, never crash, never ingest garbage).

    Act 2 (M2): the key tuple is EXTENDED with ``contradictions``/``conflicts``
    so the belief-contradiction judge's wrap unwraps via a known key, NOT the
    fragile sole-list fallback. A multi-list wrap from Codex/Gemini like
    ``{"conflicts":[…], "reasoning":["because"]}`` (TWO list-valued keys) would
    otherwise hit ``len(lists) != 1`` and silently return ``[]`` — the
    silent-failure signature this project has been burned by. The extension only
    ADDS keys (harmless to Act-1 extraction: a ``claims`` response never carries
    the new keys, and the branch test's two-unknown-list case stays ``[]``).
    """
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("claims", "beliefs", "items", "inferences", "contradictions", "conflicts"):
            value = parsed.get(key)
            if isinstance(value, list):
                return value
        lists = [v for v in parsed.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    return []


async def extract_operator_beliefs(
    user_turns: list[str],
    cwd: Path,
    *,
    settings: Any | None = None,
    reasoning: Any | None = None,
) -> list[dict]:
    """Extract durable operator beliefs from VERBATIM operator turns (provider-agnostic).

    ``settings`` None -> ``get_inference_extraction_settings()`` (Rule 1).
    ``reasoning`` None -> ``cognition.steps.reasoning_step`` (the provider-agnostic
    single-turn, no-tools, ``TEXT_REASONING`` primitive; survives the lane
    router's Claude -> Codex -> Gemini fallback).

    Input is the operator's verbatim ``role == "user"`` messages (B2) — NO
    staging, NO daily-log paraphrase. Returns a list of
    ``{"claim": str, "confidence": float, "kind": "explicit"|"inferred"}`` dicts,
    each at least ``min_chars`` long, capped at ``max_claims``.

    Fail-open everywhere: disabled kill switch / empty turns -> ``[]`` without an
    LLM call; a raising ``reasoning`` -> ``[]`` (non-blocking); unparseable /
    dict-wrapped output -> tolerant parse then ``[]``. The Langfuse span is
    best-effort and never breaks the extract.
    """
    if settings is None:
        from config import get_inference_extraction_settings

        settings = get_inference_extraction_settings()
    if not settings.extraction_enabled or not user_turns:
        return []
    if reasoning is None:
        from cognition.steps import reasoning_step as reasoning  # provider-agnostic

    span = None
    try:
        from runtime import langfuse_setup  # Rule 3 — module-attribute lookup

        client = langfuse_setup.get_observation_client()
        if client is not None:
            span = client.start_span(name="operator_belief_extraction")
    except Exception:
        span = None

    instruction = (
        "Below are the operator's OWN verbatim messages. Extract durable "
        "beliefs/preferences ABOUT THE OPERATOR from them. Each item: a single "
        "declarative claim about what the operator prefers, believes, or how "
        "they want work done. "
        f"Return at most {settings.max_claims} as a JSON array of "
        '{"claim": str, "confidence": 0..1, "kind": "explicit"|"inferred"}. '
        '"explicit" = the operator stated it directly/imperatively; "inferred" '
        "= your read of a pattern across their messages. If nothing durable, "
        "return []."
    )
    context = "OPERATOR MESSAGES (verbatim):\n" + "\n".join(user_turns[:200])

    try:
        result = await reasoning(
            context,
            instruction,
            output_schema={"type": "array"},
            cwd=cwd,
        )
    except Exception:
        if span is not None:
            try:
                span.update(metadata={"claims": 0, "error": "reasoning_failed"})
                span.end()
            except Exception:
                pass
        return []

    items = _coerce_claim_list(getattr(result, "parsed", None))
    claims = [
        c
        for c in items
        if isinstance(c, dict)
        and len(str(c.get("claim", "")).strip()) >= settings.min_chars
    ]

    if span is not None:
        try:
            span.update(
                metadata={
                    "claims": len(claims),
                    "model": getattr(result, "model", ""),
                }
            )
            span.end()
        except Exception:
            pass

    return claims[: settings.max_claims]


def apply_operator_beliefs(claims: list[dict], state_file: Path) -> int:
    """Write extracted claims as inference records (source explicit|reflection).

    ``kind == "explicit"`` -> ``source="explicit"`` (strong, direct operator
    statement); anything else -> ``source="reflection"`` (synthesized from a
    pattern). The dedup is now embedding-based (paraphrases converge), so
    repeated reflections climb ``evidence_count`` toward the ``>=2`` promotion
    gate. Returns the count written. Each malformed claim is skipped, not fatal.
    """
    from cognition.self_model import InferenceTracker

    tracker = InferenceTracker(state_file)
    written = 0
    for c in claims:
        try:
            claim_text = str(c["claim"]).strip()
        except (KeyError, TypeError):
            continue
        if not claim_text:
            continue
        kind = c.get("kind", "inferred")
        source = "explicit" if kind == "explicit" else "reflection"
        try:
            confidence = float(c.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        tracker.add_inference(
            inference=claim_text,
            observation=claim_text,
            confidence=confidence,
            source=source,
        )
        written += 1
    return written
