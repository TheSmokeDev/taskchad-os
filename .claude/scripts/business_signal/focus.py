"""Channel focus profile — weighted keyword scoring for business signal triage."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(slots=True)
class ChannelFocus:
    """Keyword-based relevance profile for signal triage.

    Scoring:
        HIGH keywords  → weight 2
        MEDIUM keywords → weight 1
        SKIP keywords  → weight -10 (forces near-zero score)

    Normalized to 0.0-1.0 via ``score_relevance()``.
    """

    high_keywords: set[str] = field(default_factory=set)
    medium_keywords: set[str] = field(default_factory=set)
    skip_keywords: set[str] = field(default_factory=set)

    def score_relevance(self, text: str) -> tuple[float, list[str]]:
        """Score *text* against this focus profile.

        Returns (score_0_to_1, matched_keywords).
        """
        lowered = text.lower()
        matched: list[str] = []

        for kw in self.skip_keywords:
            if kw in lowered:
                return 0.0, [kw]

        raw = 0
        for kw in self.high_keywords:
            if kw in lowered:
                raw += 2
                matched.append(kw)
        for kw in self.medium_keywords:
            if kw in lowered:
                raw += 1
                matched.append(kw)

        if raw <= 0:
            return 0.0, matched

        max_possible = (len(self.high_keywords) * 2) + len(self.medium_keywords)
        if max_possible <= 0:
            return 0.0, matched

        return min(raw / max_possible, 1.0), matched


# ---------------------------------------------------------------------------
# Default focus: Smoke's business verticals
# ---------------------------------------------------------------------------

def default_focus() -> ChannelFocus:
    """Return the default business-signal focus profile."""
    return ChannelFocus(
        high_keywords={
            "ai agent", "ai agents", "ai employee", "ai employees",
            "insurance", "insurtech", "insuretech",
            "small business automation", "business automation",
            "ai receptionist", "ai phone", "voice agent", "voice ai",
            "content marketing", "content strategy",
            "crypto", "defi", "bitcoin", "web3",
            "seo", "geo", "ai visibility",
            "lead generation", "speed to lead",
        },
        medium_keywords={
            "saas", "b2b", "startup", "founder",
            "llm", "large language model", "gpt", "claude",
            "automation", "workflow", "no-code",
            "marketing", "social media", "linkedin",
            "customer acquisition", "churn", "retention",
            "api", "integration", "webhook",
            "machine learning", "neural network",
            "embedding", "rag", "retrieval",
        },
        skip_keywords={
            "docker", "dockerfile", "kubernetes", "k8s",
            "ci/cd", "github actions",
            "i18n", "translation", "localization",
            "typo", "changelog", "readme",
            "logo", "branding refresh",
            "internal tooling", "developer experience",
        },
    )


@dataclass(slots=True)
class AuthorityFocus:
    """Evidence-first GEO authority scoring profile.

    Matches are found longest-first and overlapping spans are consumed, so a
    phrase such as ``ai agents`` is not scored again as ``ai agent``.  A normal
    web item needs at least one high-confidence concept; verified events from
    the configured repository allowlist may pass that gate, but do not receive
    an invented score boost.
    """

    high_keywords: set[str] = field(default_factory=set)
    medium_keywords: set[str] = field(default_factory=set)
    skip_keywords: set[str] = field(default_factory=set)
    high_weight: float = 0.45
    medium_weight: float = 0.15

    def score_relevance(
        self,
        text: str,
        *,
        verified_repository_event: bool = False,
    ) -> tuple[float, list[str]]:
        lowered = " ".join(str(text or "").lower().split())
        for keyword in sorted(self.skip_keywords, key=lambda item: (-len(item), item)):
            if _keyword_spans(lowered, keyword):
                return 0.0, [keyword]

        occupied: list[tuple[int, int]] = []
        high_matches = _longest_non_overlapping_matches(
            lowered, self.high_keywords, occupied=occupied
        )
        medium_matches = _longest_non_overlapping_matches(
            lowered, self.medium_keywords, occupied=occupied
        )

        if not high_matches and not verified_repository_event:
            return 0.0, medium_matches

        score = min(
            (len(high_matches) * self.high_weight)
            + (len(medium_matches) * self.medium_weight),
            1.0,
        )
        return score, high_matches + medium_matches


def _keyword_spans(text: str, keyword: str) -> list[tuple[int, int]]:
    """Return boundary-aware spans for one canonical phrase."""

    normalized = " ".join(keyword.lower().split())
    if not normalized:
        return []
    pattern = re.compile(rf"(?<!\w){re.escape(normalized)}(?!\w)")
    return [match.span() for match in pattern.finditer(text)]


def _longest_non_overlapping_matches(
    text: str,
    keywords: set[str],
    *,
    occupied: list[tuple[int, int]],
) -> list[str]:
    matched: list[str] = []
    canonical_seen: set[str] = set()
    for keyword in sorted(keywords, key=lambda item: (-len(item), item)):
        canonical = _canonical_concept(keyword)
        if canonical in canonical_seen:
            continue
        for start, end in _keyword_spans(text, keyword):
            if any(start < used_end and used_start < end for used_start, used_end in occupied):
                continue
            occupied.append((start, end))
            canonical_seen.add(canonical)
            matched.append(keyword)
            break
    return matched


def _canonical_concept(keyword: str) -> str:
    normalized = " ".join(keyword.lower().split())
    # Known singular/plural topic aliases are one editorial concept.
    aliases = {
        "ai agents": "ai agent",
        "personal assistants": "personal assistant",
        "voice agents": "voice agent",
        "taskchad-os": "taskchad os",
        "hermes-talk": "hermes talk",
    }
    return aliases.get(normalized, normalized)


def authority_focus() -> AuthorityFocus:
    """Return the locked GEO/personal-agent authority topic profile."""

    return AuthorityFocus(
        high_keywords={
            "ai agent",
            "ai agents",
            "ai assistant",
            "personal assistant",
            "self-hosted ai",
            "locally hosted ai",
            "local ai",
            "generative engine optimization",
            "answer engine optimization",
            "ai search",
            "ai citation",
            "ai visibility",
            "geo",
            "voice agent",
            "voice agents",
            "voice ai",
            "dark factory",
            "agent factory",
            "taskchad os",
            "taskchad-os",
            "hermes talk",
            "hermes-talk",
            "geo-skills",
        },
        medium_keywords={
            "llm",
            "retrieval",
            "rag",
            "structured data",
            "schema.org",
            "knowledge graph",
            "open source",
            "github release",
            "multi-agent",
            "orchestration",
            "persistent memory",
            "telegram bot",
            "discord bot",
            "realtime voice",
            "search visibility",
            "citation",
        },
        skip_keywords={
            "coupon",
            "giveaway",
            "sponsored placement",
            "buy backlinks",
            "guest post marketplace",
            "crypto price prediction",
            "celebrity ai",
            "ai girlfriend",
            "prompt injection",
        },
    )
