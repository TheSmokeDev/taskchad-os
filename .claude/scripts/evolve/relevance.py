"""Human-grounded relevance cases and score-independent paired evaluation."""

from __future__ import annotations

import hashlib
import math
import random
import statistics
from datetime import datetime
from pathlib import Path

from personas.learning.models import LearningError, canonical_json

MIN_CASES = 60


def _text(value, field, maximum=8000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise LearningError(f"{field} requires nonempty bounded text")
    return value.strip()


def relative_path(value) -> str:
    value = _text(value, "path", 1024).replace("\\", "/")
    path = Path(value)
    if path.is_absolute() or ":" in value or ".." in path.parts or value.startswith("/"):
        raise LearningError("case evidence must remain inside its persona vault")
    return path.as_posix()


def _evidence(root, value, field):
    if not isinstance(value, dict) or set(value) != {"path", "sha256", "excerpt"}:
        raise LearningError(f"{field} needs path, sha256, and original evidence excerpt")
    path = relative_path(value["path"])
    root = Path(root).resolve()
    physical = root / path
    if not physical.resolve().is_relative_to(root):
        raise LearningError("case evidence escaped persona vault")
    # Even links that currently land inside the vault are mutable provenance.
    for node in [physical, *physical.parents]:
        if node == root:
            break
        if node.is_symlink() or (
            node.exists() and getattr(node.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise LearningError("case evidence cannot traverse links")
    raw = physical.read_bytes()
    if len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != value["sha256"]:
        raise LearningError("case evidence revision does not match physical source")
    excerpt = _text(value["excerpt"], "evidence excerpt")
    if excerpt not in raw.decode("utf-8"):
        raise LearningError("case evidence excerpt not found in source revision")
    return {"path": path, "sha256": value["sha256"], "excerpt": excerpt}


def validate_case(case, memory_dir):
    """Admission verifies provenance, never generates or infers relevance labels.

    Input is an operator-prepared judgment, not a top-result export. Every
    judgment includes an original document revision and excerpt plus a human
    rationale. A validator attests independence from retrieval engine scores.
    """
    allowed = {
        "case_key",
        "query",
        "source_family",
        "source",
        "labels",
        "validation",
        "request_budget",
        "protected",
        "forbidden_paths",
    }
    if not isinstance(case, dict) or set(case) - allowed:
        raise LearningError("unknown fields in relevance case")
    result = {
        key: _text(case.get(key), key, 4000) for key in ("case_key", "query", "source_family")
    }
    result["source"] = _evidence(memory_dir, case.get("source"), "source")
    validation = case.get("validation")
    if not isinstance(validation, dict) or set(validation) != {
        "method",
        "actor",
        "at",
        "independent_of_retrieval_scores",
    }:
        raise LearningError("relevance labels require explicit independent operator validation")
    if (
        validation["method"] != "operator"
        or validation["independent_of_retrieval_scores"] is not True
    ):
        raise LearningError("engine scores and generated labels are not relevance ground truth")
    _text(validation["actor"], "validator", 200)
    try:
        at = datetime.fromisoformat(validation["at"].replace("Z", "+00:00"))
        if at.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise LearningError("validation timestamp must include timezone") from exc
    result["validation"] = dict(validation)
    labels = case.get("labels")
    if not isinstance(labels, list) or not 1 <= len(labels) <= 100:
        raise LearningError("case requires bounded relevance judgments")
    judged = []
    for label in labels:
        if not isinstance(label, dict) or set(label) != {"evidence", "relevance", "rationale"}:
            raise LearningError("each relevance judgment needs evidence, relevance, rationale")
        grade = label["relevance"]
        if type(grade) is not int or not 0 <= grade <= 3:
            raise LearningError("relevance must be an integer from zero through three")
        judged.append(
            {
                "evidence": _evidence(memory_dir, label["evidence"], "label"),
                "relevance": grade,
                "rationale": _text(label["rationale"], "rationale"),
            }
        )
    paths = [label["evidence"]["path"] for label in judged]
    if len(set(paths)) != len(paths) or not any(label["relevance"] > 0 for label in judged):
        raise LearningError("case needs distinct judgments and at least one relevant document")
    result["labels"] = judged
    budget = case.get("request_budget")
    if not isinstance(budget, dict) or set(budget) != {
        "max_results",
        "context_chars",
        "search_mode",
    }:
        raise LearningError("case requires a frozen request and context budget")
    if type(budget["max_results"]) is not int or not 1 <= budget["max_results"] <= 20:
        raise LearningError("invalid case result count")
    if type(budget["context_chars"]) is not int or not 100 <= budget["context_chars"] <= 100000:
        raise LearningError("invalid case context budget")
    if budget["search_mode"] not in {"auto", "hybrid", "keyword"}:
        raise LearningError("invalid case search mode")
    result["request_budget"] = dict(budget)
    if type(case.get("protected", False)) is not bool:
        raise LearningError("protected must be boolean")
    result["protected"] = case.get("protected", False)
    forbidden = case.get("forbidden_paths", [])
    if not isinstance(forbidden, list) or len(forbidden) > 100:
        raise LearningError("forbidden paths must be a bounded list")
    result["forbidden_paths"] = [relative_path(path) for path in forbidden]
    canonical_json(result)
    return result


def split_cases(cases):
    """Freeze source families together; refuse aliases and repeated queries."""
    owners, queries, families = {}, set(), {}
    for case in cases:
        family = case["source_family"]
        source = case["source"]["path"]
        if source in owners and owners[source] != family:
            raise LearningError("source-family leakage: one source has multiple family names")
        owners[source] = family
        query = " ".join(case["query"].casefold().split())
        if query in queries:
            raise LearningError("duplicate queries cannot count as independent relevance cases")
        queries.add(query)
        families.setdefault(family, []).append(case)
    if len(cases) < MIN_CASES or len(families) < 2:
        return [], []
    # Membership depends on family identity, never labels, retrieval or outcomes.
    heldout, development = [], []
    for family in sorted(families):
        group = families[family]
        # Permanent family assignment prevents yesterday's held-out labels
        # moving into development when tomorrow's material is admitted.
        if int(hashlib.sha256(family.encode()).hexdigest(), 16) % 4 == 0:
            heldout.extend(group)
        else:
            development.extend(group)
    if len(heldout) < 12 or len(development) < 30:
        return [], []
    return development, heldout


def ndcg(paths, labels, k):
    grades = {item["evidence"]["path"]: item["relevance"] for item in labels}
    ideal = sum(
        (2**grade - 1) / math.log2(rank + 2)
        for rank, grade in enumerate(sorted(grades.values(), reverse=True)[:k])
    )
    seen = set()
    actual = 0.0
    for rank, path in enumerate(paths[:k]):
        if path not in seen:
            actual += (2 ** grades.get(path, 0) - 1) / math.log2(rank + 2)
        seen.add(path)
    return actual / ideal if ideal else 0.0


def score_result(case, raw, memory_dir):
    if not isinstance(raw, dict) or set(raw) - {
        "paths",
        "latency_ms",
        "error",
        "error_count",
        "context_chars",
        "scores",
        "model_calls",
    }:
        raise LearningError("invalid recall evaluation result")
    latency = raw.get("latency_ms")
    if type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0:
        raise LearningError("evaluation latency must be finite and nonnegative")
    normalized, violation = [], False
    for value in raw.get("paths", []):
        path = Path(value)
        if path.is_absolute():
            try:
                value = path.resolve().relative_to(Path(memory_dir).resolve()).as_posix()
            except ValueError:
                violation = True
                continue
        try:
            normalized.append(relative_path(value))
        except LearningError:
            violation = True
    budget = case["request_budget"]
    if (
        len(normalized) > budget["max_results"]
        or raw.get("context_chars", 0) > budget["context_chars"]
    ):
        raise LearningError("evaluation changed the frozen request/context budget")
    violation |= bool(set(normalized) & set(case["forbidden_paths"]))
    return {
        "case_id": case["id"],
        "source_family": case["source_family"],
        "paths": normalized,
        "ndcg": ndcg(normalized, case["labels"], budget["max_results"]),
        "latency_ms": float(latency),
        "error": bool(raw.get("error")),
        "error_count": int(raw.get("error_count", bool(raw.get("error")))),
        "isolation_violation": violation,
        "protected": case["protected"],
        "model_calls": raw.get("model_calls", []),
    }


def percentile(values, quantile=0.95):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)] if ordered else 0.0


def compare(baseline, candidate, *, require_gain=True):
    if not baseline or [r["case_id"] for r in baseline] != [r["case_id"] for r in candidate]:
        raise LearningError("paired relevance evaluation requires identical ordered cases")
    deltas = [c["ndcg"] - b["ndcg"] for b, c in zip(baseline, candidate, strict=True)]
    families = {}
    for result, delta in zip(baseline, deltas, strict=True):
        families.setdefault(result.get("source_family", result["case_id"]), []).append(delta)
    clusters = list(families.values())
    rng = random.Random(73019)
    bootstrap = [
        statistics.mean(
            value for group in rng.choices(clusters, k=len(clusters)) for value in group
        )
        for _ in range(2000)
    ]
    interval = [percentile(bootstrap, 0.025), percentile(bootstrap, 0.975)]
    failures = []
    if require_gain and interval[0] <= 0:
        failures.append("no_positive_paired_95pct_relevance_gain")
    for b, c in zip(baseline, candidate, strict=True):
        if c["protected"] and c["ndcg"] < b["ndcg"]:
            failures.append("protected_regression")
        if c["isolation_violation"]:
            failures.append("isolation_violation")
        if (c["error"] and not b["error"]) or c.get("error_count", int(c["error"])) > b.get(
            "error_count", int(b["error"])
        ):
            failures.append("added_error")
    if not require_gain and statistics.mean(deltas) < -1e-12:
        failures.append("frozen_relevance_regression")
    baseline_p95 = percentile([r["latency_ms"] for r in baseline])
    candidate_p95 = percentile([r["latency_ms"] for r in candidate])
    if candidate_p95 > baseline_p95 * 1.25:
        failures.append("p95_latency_regression")
    return {
        "accepted": not failures,
        "mean_gain": statistics.mean(deltas),
        "paired_ci95": interval,
        "baseline_p95_ms": baseline_p95,
        "candidate_p95_ms": candidate_p95,
        "failures": sorted(set(failures)),
        "metric": "ndcg",
        "cases": len(baseline),
    }
