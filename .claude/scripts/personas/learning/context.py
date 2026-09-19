"""Pure learned-context compilation shared by deployment and frozen trials.

Storage selection and physical-application checks belong to the service. This
module never reads a profile, so qualification can replay exact frozen inputs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .models import LearningContext, LearningError, content_hash

CONTEXT_COMPILER_VERSION = "persona-learning-context-v1"
DEFAULT_CONTEXT_MAX_CHARS = 2000
_HEADER = (
    "\n[Persona learning: apply methods only within their stated "
    "conditions; provisional means real-world effectiveness remains "
    "unverified.]\n"
)


def method_snapshot(method: dict[str, Any]) -> dict[str, Any]:
    """Retain exactly the content, identity, conditions and ordering used at render."""
    candidate = method["candidate"]
    return {
        "id": method["id"],
        "method_status": method["method_status"],
        "candidate": {
            key: candidate.get(key, "")
            for key in (
                "id",
                "content_hash",
                "content",
                "title",
                "applicability",
                "domain",
                "created_at",
            )
        },
    }


def prospective_methods(methods: Iterable[dict], candidate: dict) -> tuple[dict, ...]:
    """Replace only explicit lineage; preserve every unrelated incumbent."""
    excluded = {candidate["id"], candidate.get("prior_candidate_id")}
    remaining = [method_snapshot(m) for m in methods if m["candidate"]["id"] not in excluded]
    # Activation IDs are receipt metadata and never appear in the model's text.
    trial = method_snapshot(
        {
            "id": "prospective:" + candidate["content_hash"],
            "method_status": "active_provisional",
            "candidate": candidate,
        }
    )
    return (*remaining, trial)


def compile_context(
    task: str, methods: Iterable[dict], *, max_chars: int = DEFAULT_CONTEXT_MAX_CHARS
) -> LearningContext:
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 0 <= max_chars <= 65536:
        raise LearningError("invalid learning context budget")
    words = set(re.findall(r"[\w-]{3,}", task.casefold()))
    ranked = []
    for method in methods:
        candidate = method["candidate"]
        terms = set(
            re.findall(
                r"[\w-]{3,}",
                (
                    candidate["title"]
                    + " "
                    + candidate["applicability"]
                    + " "
                    + candidate.get("domain", "")
                ).casefold(),
            )
        )
        overlap = len(words & terms)
        if overlap or candidate["applicability"].casefold() in {"always", "all tasks", "all turns"}:
            ranked.append((overlap, candidate.get("created_at", ""), candidate["id"], method))
    # Candidate creation identity is stable before and after physical activation.
    ranked.sort(key=lambda item: item[:3], reverse=True)
    text, versions = "", []
    for _, _, _, method in ranked:
        candidate = method["candidate"]
        block = (
            f"\nLearning method {candidate['id']} ({method['method_status']}).\n"
            f"{candidate['title']}\nApplies: {candidate['applicability']}\n"
            f"{candidate['content']}\n"
        )
        prefix = _HEADER if not text else ""
        if len(text) + len(prefix) + len(block) > max_chars:
            continue
        text += prefix + block
        versions.append(
            {
                "candidate_id": candidate["id"],
                "activation_id": method["id"],
                "content_hash": candidate["content_hash"],
                "content": candidate["content"],
                "title": candidate["title"],
                "status": method["method_status"],
                "rendered_block": block,
            }
        )
    return LearningContext(text, tuple(versions), content_hash(text))


def compile_cognitive_context(
    task: str,
    understanding: Iterable[dict],
    investigations: Iterable[dict],
    methods: LearningContext,
    *,
    max_chars: int = 4000,
) -> LearningContext:
    """Compile exact, typed retained versions without changing procedure trials."""
    from cognition import injection

    if type(max_chars) is not int or not 0 <= max_chars <= 65536:
        raise LearningError("invalid cognitive context budget")
    words = set(re.findall(r"[\w-]{3,}", task.casefold()))
    rows = []
    for record in (*understanding, *investigations):
        summary = " ".join(
            str(record.get(k, "")) for k in ("title", "scope", "domain", "question", "content")
        )
        score = len(words & set(re.findall(r"[\w-]{3,}", summary.casefold())))
        always = record.get("scope", "").casefold() in {"always", "all tasks", "all turns"}
        if score or always or record["kind"] == "investigation":
            rows.append((score, record.get("created_at", ""), record["id"], record))
    rows.sort(key=lambda row: row[:3], reverse=True)
    text = methods.text if len(methods.text) <= max_chars else ""
    versions = list(methods.versions) if text else []
    for _, _, _, record in rows:
        if record["kind"] == "understanding":
            body = (
                f"{record['title']}\nType: {record['understanding_type']}\n"
                f"Scope: {record['scope']}\n{record['content']}\n"
                f"Uncertainty: {record['uncertainty']}"
            )
        else:
            body = (
                f"Question: {record['question']}\nWhy: {record['why']}\n"
                f"Next observation: {record['trigger']}"
            )
        block = (
            f"\nRetained {record['kind']} {record['id']} ({record['status']}).\n{body}\n"
            "This is contextual understanding or an open question, "
            "not permission or a standing execution rule.\n"
        )
        sanitized = injection.sanitize_recalled_content(block)
        if not sanitized:
            continue
        block = "\n" + injection.wrap_recalled_memory([sanitized]) + "\n"
        if len(text) + len(block) > max_chars:
            continue
        text += block
        versions.append(
            {
                "record_kind": record["kind"],
                "record_id": record["id"],
                "content_hash": record["content_hash"],
                "content": body,
                "rendered_block": block,
                "status": record["status"],
                **(
                    {"understanding_type": record["understanding_type"]}
                    if record["kind"] == "understanding"
                    else {}
                ),
            }
        )
    return LearningContext(text, tuple(versions), content_hash(text))
