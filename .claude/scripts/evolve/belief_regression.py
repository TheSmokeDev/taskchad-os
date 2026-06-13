"""Living Self Act 4 — the deterministic belief-regression floor.

A PURE, zero-LLM evaluator (mirrors ``evolve/regression.py``'s shape) that scores
a candidate self-amendment against a corpus of FALSIFIABLE behavior checks. Its
``.failed`` list plugs into the UNCHANGED ``evolve.veto.evaluate_veto(...,
regression_summary=...)`` so the never-softenable, ``--force``-proof floor
(``veto.py:312-327``) is INHERITED, not re-implemented.

The fitness signal for each entry is a pure function over PHYSICAL state — the
candidate's own fields + the READ evidence bytes (Rule 2) — so the whole floor is
testable with NO provider. There is NO ``reasoning_step`` and NO recall replay in
this module.

The three layers in ASCENDING strength (M2 — named, not hidden):
  1. the LLM judge (``evolve/judge.py``) — DECIDES support; the sufficient gate.
  2. the evidence-READ existence/confinement check (``cognition/evidence_gate``)
     — NECESSARY: the file must exist, be confined, be non-empty.
  3. this token-overlap floor — the CHEAPEST pre-filter: the claim's vocabulary
     must at least INTERSECT the cited evidence. It measures shared VOCABULARY,
     NOT genuine support (a CONTRADICTING file with overlapping nouns PASSES it).
     This floor ALONE does NOT earn a belief.

Seed checks (encoding the system's OWN documented failure modes):
  - ``no_unread_claim`` — the doc-read-truthfulness floor (SELF.md:211 incident):
    a candidate that asserts a document was READ/verified must cite evidence whose
    READ bytes are non-empty. Citing a path that does not exist / is empty /
    metadata-only FAILS. This is the REAL falsifiable check the crux REJECT fails
    on — never the weak overlap floor.
  - ``evidence_fidelity`` — at least one cited path's READ content must share a
    deterministic minimum token-overlap with the claim's salient terms.
  - ``explicit_provenance`` — (params-gated, OFF in the seed): a candidate that
    claims to encode an OPERATOR-stated belief must cite an ``explicit``-source
    record, not a ``reflection`` synthesis.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REGRESSION_CORPUS_PATH = Path(__file__).resolve().parent / "belief_regression_corpus.json"

# Claim-verb set for the doc-read-truthfulness floor. A candidate whose
# proposed_content asserts a document was READ/verified must back it with
# non-empty cited evidence. Deliberately small + specific so it does NOT fire on
# ordinary prose (Rule 2 — a falsifiable behavior check, not a keyword sweep).
_CLAIM_VERB_RE = re.compile(
    r"(?i)\b(?:read|reviewed|verified|confirmed|checked|inspected|audited)\b"
    r"[^.]{0,40}?\b(?:doc|docs|document|file|files|code|source|page|log|logs|"
    r"transcript|report|content)\b"
)

# Token normalization for the overlap primitive — lowercase word characters,
# length>=3 to drop short noise. Shared definition so the floor and the evidence
# gate measure the SAME vocabulary.
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")

# Common function words that survive the >=3 filter but carry no SALIENT meaning
# (they would inflate overlap — "the"/"and" appear in almost any text). Dropping
# them keeps overlap_ratio a measure of SALIENT vocabulary, not boilerplate. Kept
# deliberately small (high-frequency English function words only).
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "are", "was", "were", "has", "have", "had", "not",
        "but", "with", "from", "this", "that", "these", "those", "you", "your",
        "they", "them", "their", "its", "his", "her", "out", "all", "any", "can",
        "will", "would", "should", "could", "into", "onto", "than", "then",
        "there", "here", "when", "what", "which", "who", "how", "why", "about",
        "over", "under", "per", "via", "also", "such", "some", "more", "most",
        "been", "being", "does", "did", "done", "each", "only", "very", "just",
        "now",
    }
)


def _tokens(text: str) -> set[str]:
    return {
        t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOPWORDS
    }


def overlap_ratio(claim: str, evidence_union: str) -> float:
    """Containment ratio of the claim's salient tokens present in the evidence.

    ``|claim_tokens ∩ evidence_tokens| / |claim_tokens|`` — how much of the
    claim's vocabulary the cited evidence covers. Returns 0.0 when the claim has
    no salient tokens (a claim with nothing to support cannot be supported).

    M2: this is VOCABULARY coverage, NOT support. A contradicting evidence file
    sharing the claim's nouns scores high here. The LLM judge decides support.
    """
    claim_tokens = _tokens(claim)
    if not claim_tokens:
        return 0.0
    evidence_tokens = _tokens(evidence_union)
    if not evidence_tokens:
        return 0.0
    return len(claim_tokens & evidence_tokens) / len(claim_tokens)


# ── Dataclasses (mirror evolve/regression.py) ──────────────────────────────


@dataclass(frozen=True)
class BeliefRegressionEntry:
    """One falsifiable belief-behavior check — pure metadata.

    ``kind`` dispatches a pure check over (candidate, evidence_texts, params).
    ``params`` carries check-specific config (e.g. ``min_overlap``). The
    Archon-proposed per-candidate ``prediction`` (N1) is fed in as an extra entry
    so the candidate is actually held to its OWN falsifiable claim.
    """

    check_id: str
    kind: str
    description: str
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "kind": self.kind,
            "description": self.description,
            "params": self.params,
        }


@dataclass(frozen=True)
class BeliefRegressionFailure:
    """A single belief-regression failure with the property it violated.

    ``to_dict`` is read by ``VetoVerdict.to_dict()`` (``veto.py:181-184`` uses
    ``f.to_dict() if hasattr(f, "to_dict")``) so a belief failure round-trips
    cleanly into the decision artifact. NOTE (m1): do NOT route a belief failure
    through ``veto.format_verdict_table`` — that formatter reads recall-only
    fields (``entry.fixed_in`` / ``observed_top_score`` / ``entry.query``) a
    ``BeliefRegressionFailure`` does NOT carry; it would ``AttributeError``.
    """

    entry: BeliefRegressionEntry
    reason: str
    observed: str  # short physical evidence of the violation (Rule 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry": self.entry.to_dict(),
            "reason": self.reason,
            "observed": self.observed,
        }


@dataclass
class BeliefRegressionSummary:
    """Aggregated belief-regression verdict, JSON-serializable for the artifact.

    ``failed`` is the ONLY field ``evaluate_veto`` reads — any non-empty
    ``failed`` becomes an always-hard, ``--force``-proof veto.
    """

    total: int = 0
    passed: int = 0
    failed: list[BeliefRegressionFailure] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": [f.to_dict() for f in self.failed],
        }


# ── Loader ─────────────────────────────────────────────────────────────────


def load_belief_regression_corpus(
    path: Path | str | None = None,
) -> list[BeliefRegressionEntry]:
    """Load the falsifiable-check corpus from the sibling JSON (or an override).

    ``path`` None -> ``get_belief_evolve_settings().corpus_path`` ->
    ``belief_regression_corpus.json`` next to this module. Schema mirrors
    ``regression_queries.json``: ``{"version", "description", "entries": [...]}``.
    Fail-loud on a malformed file (a silent empty corpus would disable the floor).
    """
    if path is None:
        try:
            from config import get_belief_evolve_settings

            corpus_path = get_belief_evolve_settings().corpus_path
        except Exception:
            corpus_path = None
        p = Path(corpus_path) if corpus_path else _REGRESSION_CORPUS_PATH
    else:
        p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"belief_regression_corpus.json not found at {p}."
        )
    data = json.loads(p.read_text(encoding="utf-8"))
    raw_entries = data.get("entries", [])
    if not isinstance(raw_entries, list):
        raise ValueError(
            f"belief_regression_corpus.json 'entries' must be a list, got "
            f"{type(raw_entries).__name__}"
        )
    out: list[BeliefRegressionEntry] = []
    for i, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise ValueError(
                f"belief_regression_corpus entries[{i}] must be an object, "
                f"got {type(raw).__name__}"
            )
        for required in ("check_id", "kind"):
            if required not in raw:
                raise ValueError(
                    f"belief_regression_corpus entries[{i}] missing required "
                    f"field {required!r}"
                )
        out.append(
            BeliefRegressionEntry(
                check_id=str(raw["check_id"]),
                kind=str(raw["kind"]),
                description=str(raw.get("description", "")),
                params=dict(raw.get("params", {})),
            )
        )
    return out


# ── Pure checks (each: (candidate, evidence_texts, entry) -> Failure | None) ─


def _check_no_unread_claim(
    candidate: dict, evidence_texts: dict[str, str], entry: BeliefRegressionEntry
) -> BeliefRegressionFailure | None:
    """The doc-read-truthfulness floor (SELF.md:211 incident).

    If the proposed_content asserts a document was READ/verified, EVERY cited path
    must have non-empty READ content. A candidate citing only a missing/empty/
    metadata-only path FAILS. A candidate that does NOT assert a read is N/A ->
    pass (no false positive).
    """
    content = candidate.get("proposed_content") or ""
    if not _CLAIM_VERB_RE.search(content):
        return None  # claim asserts no read -> N/A -> pass
    if not evidence_texts:
        return BeliefRegressionFailure(
            entry,
            reason="claims_read_but_no_evidence_cited",
            observed="none cited",
        )
    empties = [p for p, t in evidence_texts.items() if not (t or "").strip()]
    if empties:
        return BeliefRegressionFailure(
            entry,
            reason="claims_read_but_evidence_empty_or_missing",
            observed=f"empty/missing: {empties}",
        )
    return None


def _check_evidence_fidelity(
    candidate: dict, evidence_texts: dict[str, str], entry: BeliefRegressionEntry
) -> BeliefRegressionFailure | None:
    """Cheap vocabulary pre-filter (M2 — NOT support).

    The claim's salient tokens must hit a minimum token-overlap against the UNION
    of cited evidence. Zero overlap -> the claim shares NO vocabulary with what it
    cites -> FAIL. A contradicting file with overlapping nouns PASSES — the LLM
    judge is the real support-decider.
    """
    min_overlap = float(entry.params.get("min_overlap", 0.10))
    union = " ".join(evidence_texts.values())
    ratio = overlap_ratio(candidate.get("proposed_content", ""), union)
    if ratio < min_overlap:
        return BeliefRegressionFailure(
            entry,
            reason="claim_unsupported_by_cited_evidence",
            observed=f"overlap={ratio:.3f} < {min_overlap:.3f}",
        )
    return None


def _check_explicit_provenance(
    candidate: dict, evidence_texts: dict[str, str], entry: BeliefRegressionEntry
) -> BeliefRegressionFailure | None:
    """Params-gated (OFF in the seed): a candidate claiming an OPERATOR-stated
    belief must cite an ``explicit``-source record, not a ``reflection`` synthesis.

    Pure: reads the candidate's ``source`` field. Only fires when
    ``params.require_explicit`` is truthy AND the proposed_content asserts an
    operator-given belief (a small marker regex over ``params.operator_markers``).
    """
    if not entry.params.get("require_explicit", False):
        return None
    content = (candidate.get("proposed_content") or "").lower()
    markers = entry.params.get(
        "operator_markers",
        ["operator stated", "operator said", "smoke stated", "smoke said", "explicitly"],
    )
    if not any(str(m).lower() in content for m in markers):
        return None  # not an operator-given claim -> N/A -> pass
    source = (candidate.get("source") or "").lower()
    if source != "explicit":
        return BeliefRegressionFailure(
            entry,
            reason="operator_belief_lacks_explicit_provenance",
            observed=f"source={source!r}",
        )
    return None


def _check_prediction(
    candidate: dict, evidence_texts: dict[str, str], entry: BeliefRegressionEntry
) -> BeliefRegressionFailure | None:
    """N1 — the candidate's OWN falsifiable prediction, fed in as an extra entry.

    The Archon researcher ships a ``prediction`` string the candidate claims its
    evidence will satisfy. We hold the candidate to it deterministically: the
    prediction's salient tokens must hit the token-overlap floor against the UNION
    of cited evidence (the prediction names what the evidence should show; if the
    evidence does not even share that vocabulary, the prediction is not met).
    Empty prediction -> N/A -> pass.
    """
    prediction = (entry.params.get("prediction") or "").strip()
    if not prediction:
        return None
    min_overlap = float(entry.params.get("min_overlap", 0.10))
    union = " ".join(evidence_texts.values())
    ratio = overlap_ratio(prediction, union)
    if ratio < min_overlap:
        return BeliefRegressionFailure(
            entry,
            reason="prediction_not_met_by_cited_evidence",
            observed=f"prediction_overlap={ratio:.3f} < {min_overlap:.3f}",
        )
    return None


_CHECKS: dict[
    str,
    Callable[[dict, dict[str, str], BeliefRegressionEntry], BeliefRegressionFailure | None],
] = {
    "no_unread_claim": _check_no_unread_claim,
    "evidence_fidelity": _check_evidence_fidelity,
    "explicit_provenance": _check_explicit_provenance,
    "prediction": _check_prediction,
}


# ── Pure evaluator ─────────────────────────────────────────────────────────


def evaluate_belief_regression(
    candidate: dict,
    evidence_texts: dict[str, str],
    corpus: list[BeliefRegressionEntry],
) -> BeliefRegressionSummary:
    """Compute pass/fail per falsifiable check. PURE — zero-LLM (Rule 2).

    Each ``entry.kind`` dispatches a pure check over the candidate's fields + the
    READ evidence bytes. An unknown kind is SKIPPED (counted as passed — the
    corpus is data and must never silently fail-CLOSED on a typo, nor open a hole;
    a skipped entry simply does not constrain). The returned ``.failed`` plugs
    into the UNCHANGED ``evaluate_veto`` for the ``--force``-proof floor.
    """
    summary = BeliefRegressionSummary(total=len(corpus))
    for entry in corpus:
        check = _CHECKS.get(entry.kind)
        if check is None:
            summary.passed += 1  # unknown kind: no constraint, never a silent fail
            continue
        failure = check(candidate, evidence_texts, entry)
        if failure is None:
            summary.passed += 1
        else:
            summary.failed.append(failure)
    return summary


__all__ = [
    "BeliefRegressionEntry",
    "BeliefRegressionFailure",
    "BeliefRegressionSummary",
    "load_belief_regression_corpus",
    "evaluate_belief_regression",
    "overlap_ratio",
]
