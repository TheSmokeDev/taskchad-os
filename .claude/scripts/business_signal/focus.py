"""Channel focus profile — weighted keyword scoring for business signal triage."""

from __future__ import annotations

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
