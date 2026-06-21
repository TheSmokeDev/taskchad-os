"""Contradiction detection for the vault knowledge graph.

Extracted verbatim from ``entity_extractor.py`` (WS2, issue #83) so the
contradiction-detection concern lives in its own dedicated, independently
testable module. The compilation pipeline behaves byte-for-byte identically;
``entity_extractor`` imports these symbols and re-exports them so every existing
consumer keeps working unchanged.

Pure stdlib only (``re``, ``date``, ``Path``, ``dataclass``) — this module has
ZERO dependency on ``entity_extractor`` (the dependency arrow is one-way:
``entity_extractor`` -> ``entity_contradictions``), which avoids a circular
import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

__all__ = [
    "Contradiction",
    "check_contradictions",
    "insert_contradiction_callouts",
]


def _today() -> str:
    """Local date helper — kept here so this module has zero dependency on
    entity_extractor (avoids a circular import)."""
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Contradiction:
    """A potential contradiction between claims on a concept page."""

    concept_page: str
    claim_a: str
    source_a: str
    claim_b: str
    source_b: str
    severity: str = "tension"  # "direct" or "tension"


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------

def check_contradictions(page_path: Path) -> list[Contradiction]:
    """Scan a concept page for potentially conflicting claims across sources.

    Heuristic: look for negation patterns and opposing claim structures
    from different source sections.
    """
    content = page_path.read_text(encoding="utf-8")
    page_name = page_path.stem

    # Parse source sections: "## From [[source]] (date)"
    section_re = re.compile(
        r"## From \[\[([^\]]+)\]\] \(([^)]+)\)\n(.*?)(?=\n## |\Z)",
        re.DOTALL,
    )
    sections = section_re.findall(content)
    if len(sections) < 2:
        return []

    contradictions: list[Contradiction] = []

    # Extract claims per source
    source_claims: list[tuple[str, list[str]]] = []
    for source, _date, body in sections:
        claims = [
            line.lstrip("- ").strip()
            for line in body.strip().split("\n")
            if line.strip().startswith("-") and len(line.strip()) > 10
        ]
        if claims:
            source_claims.append((source, claims))

    # Compare claims across sources for contradiction signals
    negation_words = {"not", "no", "never", "neither", "don't", "doesn't", "isn't", "aren't", "won't", "shouldn't", "cannot"}
    opposite_pairs = [
        ("always", "never"), ("default", "optional"), ("required", "optional"),
        ("preferred", "deprecated"), ("recommended", "avoid"),
        ("enabled", "disabled"), ("true", "false"),
    ]

    for i in range(len(source_claims)):
        for j in range(i + 1, len(source_claims)):
            src_a, claims_a = source_claims[i]
            src_b, claims_b = source_claims[j]

            for ca in claims_a:
                ca_words = set(ca.lower().split())
                for cb in claims_b:
                    cb_words = set(cb.lower().split())

                    # Check for negation asymmetry
                    a_negated = bool(ca_words & negation_words)
                    b_negated = bool(cb_words & negation_words)

                    # Shared significant words (content overlap)
                    shared = (ca_words & cb_words) - {"the", "a", "is", "are", "in", "of", "to", "and", "for", "with", "it", "this", "that"}
                    if len(shared) < 2:
                        continue

                    # Contradiction signal: same topic, different negation
                    if a_negated != b_negated:
                        contradictions.append(Contradiction(
                            concept_page=page_name,
                            claim_a=ca, source_a=src_a,
                            claim_b=cb, source_b=src_b,
                            severity="direct",
                        ))
                        continue

                    # Check for opposite word pairs
                    for word_a, word_b in opposite_pairs:
                        if (word_a in ca.lower() and word_b in cb.lower()) or \
                           (word_b in ca.lower() and word_a in cb.lower()):
                            contradictions.append(Contradiction(
                                concept_page=page_name,
                                claim_a=ca, source_a=src_a,
                                claim_b=cb, source_b=src_b,
                                severity="tension",
                            ))
                            break

    return contradictions


def insert_contradiction_callouts(
    page_path: Path,
    contradictions: list[Contradiction],
    *,
    today: str | None = None,  # Rule 1: None sentinel, resolve at call time
) -> None:
    """Insert Obsidian callout blocks for detected contradictions."""
    if not contradictions:
        return

    resolved_today = _today() if today is None else today

    content = page_path.read_text(encoding="utf-8")
    callouts = []
    for c in contradictions:
        callout = (
            f"\n> [!warning] Contradiction ({c.severity})\n"
            f"> **[[{c.source_a}]]** says: \"{c.claim_a}\"\n"
            f"> **[[{c.source_b}]]** says: \"{c.claim_b}\"\n"
            f"> *Flagged during compilation on {resolved_today}*\n"
        )
        # Don't duplicate
        if callout.strip() not in content:
            callouts.append(callout)

    if callouts:
        content = content.rstrip() + "\n\n## Contradictions\n" + "\n".join(callouts) + "\n"
        page_path.write_text(content, encoding="utf-8")
