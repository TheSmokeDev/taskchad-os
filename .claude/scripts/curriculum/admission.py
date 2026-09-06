"""Cheap admission judgment before any transcript or media work."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import get_background_models
from runtime.base import RuntimeRequest, RuntimeResult
from runtime.capabilities import TEXT_REASONING

from .model_runtime import run_curriculum_model

TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "harnesses-evals": (
        "eval",
        "benchmark",
        "harness",
        "simulation",
        "rollout",
        "test",
        "judge",
    ),
    "memory-context": (
        "memory",
        "context",
        "rag",
        "knowledge",
        "ontology",
        "graph",
        "retrieval",
    ),
    "tools-protocols": (
        "tool",
        "mcp",
        "skill",
        "browser",
        "agent protocol",
        "webmcp",
        "api",
    ),
    "production-security": (
        "production",
        "security",
        "verify",
        "reliability",
        "observability",
        "scale",
        "financial services",
    ),
    "models-data": (
        "model",
        "training",
        "inference",
        "data",
        "embedding",
        "multimodal",
        "synthetic",
    ),
    "product-fde": (
        "product",
        "forward deployed",
        "company",
        "consulting",
        "leadership",
        "ship",
        "developer experience",
    ),
}
NOISE_TERMS = (
    "vibe reel",
    "welcome to ",
    "announcing the ai engineer network",
    "sponsor",
    "giveaway",
    "after party",
    "track intro",
    "opening remarks",
    "closing remarks",
)


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    video_id: str
    decision: str
    score: float
    topic: str
    reason: str
    method: str = "deterministic"


@dataclass(frozen=True, slots=True)
class AdmissionBatchResult:
    decisions: list[AdmissionDecision]
    runtime: RuntimeResult | None
    execution_time_ms: int
    fallback_error: str = ""


def deterministic_admission(video: dict[str, Any]) -> AdmissionDecision:
    title = str(video.get("title") or "").strip()
    folded = title.casefold()
    if any(term in folded for term in NOISE_TERMS):
        return AdmissionDecision(
            str(video["video_id"]),
            "reject",
            5,
            "other",
            "event promo, sponsor, or channel-administration noise",
        )

    topic_scores = {
        topic: sum(1 for term in terms if term in folded) for topic, terms in TOPIC_KEYWORDS.items()
    }
    topic = max(topic_scores, key=topic_scores.get)
    matches = topic_scores[topic]
    duration = video.get("duration_s")
    score = 35 + matches * 18
    if isinstance(duration, (int, float)):
        if duration < 240:
            score -= 25
        elif duration >= 720:
            score += 8
    if re.search(r"\b(how|why|building|lessons|principles|architecture)\b", folded):
        score += 10
    if any(
        marker in folded
        for marker in ("openai", "anthropic", "google", "netflix", "uber", "notion")
    ):
        score += 5
    score = max(0, min(float(score), 100.0))
    decision = "deep" if score >= 72 else "skim" if score >= 48 else "reject"
    reason = f"{matches} domain signal(s), duration/actionability adjusted; topic={topic}"
    return AdmissionDecision(str(video["video_id"]), decision, score, topic, reason)


def curate_decisions(
    videos: list[dict[str, Any]],
    *,
    total_limit: int,
    per_topic_limit: int = 20,
) -> list[AdmissionDecision]:
    """Return diverse decisions; overflow deep candidates become skims."""
    ranked = sorted(
        (deterministic_admission(video) for video in videos),
        key=lambda decision: (-decision.score, decision.video_id),
    )
    selected = 0
    topic_counts: dict[str, int] = {}
    output: list[AdmissionDecision] = []
    for decision in ranked:
        if decision.decision != "deep":
            output.append(decision)
            continue
        count = topic_counts.get(decision.topic, 0)
        if selected >= total_limit or count >= per_topic_limit:
            output.append(
                AdmissionDecision(
                    decision.video_id,
                    "skim",
                    decision.score,
                    decision.topic,
                    "high signal but deferred by curated diversity/backfill cap",
                    decision.method,
                )
            )
            continue
        selected += 1
        topic_counts[decision.topic] = count + 1
        output.append(decision)
    return output


async def cognitive_admission_batch(
    videos: list[dict[str, Any]],
    *,
    persona_id: str,
    doctrine_index: str,
    workspace: Path,
    model_tier: str = "fast",
) -> AdmissionBatchResult:
    """Ask the fast reasoning lane to refine at most 50 metadata decisions.

    The deterministic result remains the fallback and the candidate set. Model
    output can only choose among known video IDs and allowed decisions/topics.
    """
    if len(videos) > 50:
        raise ValueError("Curriculum metadata batch exceeds the hard limit of 50.")
    fallback = {decision.video_id: decision for decision in map(deterministic_admission, videos)}
    payload = [
        {
            "video_id": row.get("video_id"),
            "title": row.get("title"),
            "duration_s": row.get("duration_s"),
            "upload_date": row.get("upload_date"),
        }
        for row in videos
    ]
    prompt = f"""You are the curriculum admission faculty for persona {persona_id}.

Judge metadata only. Do not claim to have watched a video. Compare it with the
bounded existing doctrine index and choose reject, skim, or deep. Reward
relevance, novelty, credible practitioners, durable mechanisms, and
actionability. Penalize promotions, repeated talks, stale tool demos, vague
inspiration, and redundant coverage.

Return a JSON array only. Each item must have exactly:
video_id, decision, score (0-100), topic, reason.
Allowed topics: {", ".join(TOPIC_KEYWORDS)} or other.

<UNTRUSTED_EXISTING_DOCTRINE>
{doctrine_index[:12_000]}
</UNTRUSTED_EXISTING_DOCTRINE>
<UNTRUSTED_VIDEO_METADATA>
{json.dumps(payload, ensure_ascii=False)}
</UNTRUSTED_VIDEO_METADATA>
"""
    started = time.monotonic()
    result: RuntimeResult | None = None
    try:
        models = get_background_models()
        result = await run_curriculum_model(
            RuntimeRequest(
                prompt=prompt,
                cwd=workspace,
                task_name="curriculum_admission",
                capability=TEXT_REASONING,
                model=models.get(model_tier, models.get("fast")),
                max_turns=1,
                allowed_tools=[],
                disallowed_tools=["*"],
                metadata={"persona_id": persona_id, "curriculum": True},
            )
        )
        text = result.text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise ValueError("admission output is not a list")
        refined: dict[str, AdmissionDecision] = {}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            video_id = str(item.get("video_id") or "")
            if video_id not in fallback:
                continue
            decision = str(item.get("decision") or "")
            if decision not in {"reject", "skim", "deep"}:
                continue
            topic = str(item.get("topic") or "other")
            if topic not in TOPIC_KEYWORDS and topic != "other":
                topic = "other"
            score = max(0.0, min(float(item.get("score", 0)), 100.0))
            refined[video_id] = AdmissionDecision(
                video_id=video_id,
                decision=decision,
                score=score,
                topic=topic,
                reason=str(item.get("reason") or "")[:500],
                method="cognitive",
            )
        guarded: list[AdmissionDecision] = []
        for row in videos:
            video_id = str(row["video_id"])
            baseline = fallback[video_id]
            proposed = refined.get(video_id, baseline)
            if baseline.decision == "reject" and baseline.score <= 10:
                # Obvious channel administration/promo is a hard metadata
                # veto. Credible affiliations in the title must not let a
                # model hallucinate technical content into a welcome reel.
                guarded.append(
                    AdmissionDecision(
                        video_id,
                        "reject",
                        baseline.score,
                        baseline.topic,
                        baseline.reason,
                        "deterministic-policy-veto",
                    )
                )
                continue
            if baseline.decision == "reject" and proposed.decision == "deep":
                # Metadata can justify a bounded transcript skim, never a
                # two-level leap from reject directly to expensive study.
                guarded.append(
                    AdmissionDecision(
                        video_id,
                        "skim",
                        proposed.score,
                        proposed.topic,
                        proposed.reason,
                        "cognitive-bounded-promotion",
                    )
                )
                continue
            guarded.append(proposed)
        return AdmissionBatchResult(
            decisions=guarded,
            runtime=result,
            execution_time_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:
        return AdmissionBatchResult(
            decisions=[
                AdmissionDecision(
                    decision.video_id,
                    decision.decision,
                    decision.score,
                    decision.topic,
                    decision.reason,
                    "deterministic-fallback",
                )
                for decision in fallback.values()
            ],
            runtime=result,
            execution_time_ms=int((time.monotonic() - started) * 1000),
            fallback_error=f"{type(exc).__name__}: {exc}",
        )
