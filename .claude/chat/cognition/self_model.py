"""Inference confidence tracking for user-model self-awareness.

Tracks inferences about the user with confidence scores that decay
over time if not reinforced and strengthen when confirmed. Runs
during daily reflection (same schedule as promotion).

Pattern: continuity.py — dataclass + JSON persistence with load/save.
Pattern: staging.py — file rewrite for updates.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4


@dataclass
class InferenceRecord:
    """A single user-model inference with confidence tracking."""

    id: str
    inference: str
    observation: str
    confidence: float
    evidence_count: int = 1
    contradiction_count: int = 0
    # Act 2 audit: ["{winner_id}:{reason}", ...] — populated by contradict(by=...).
    contradicted_by: list[str] = field(default_factory=list)
    first_seen: str = ""
    last_updated: str = ""
    source: str = "auto_capture"  # auto_capture | explicit | reflection
    status: str = "active"  # active | decayed | confirmed


@dataclass
class SelfModelState:
    """First-class psyche snapshot built from active inference evidence."""

    generated_at: str
    operator_beliefs: list[str] = field(default_factory=list)
    homie_beliefs: list[str] = field(default_factory=list)
    drives: list[str] = field(default_factory=list)
    recurring_mistakes: list[str] = field(default_factory=list)
    open_loops: list[str] = field(default_factory=list)
    evidence: dict[str, list[str]] = field(default_factory=dict)


def _normalize_text(text: str) -> str:
    """Lowercase + whitespace-collapse for the exact-match fallback path."""
    return re.sub(r"\s+", " ", text.strip().lower())


def _exact_similar(a: str, b: str) -> bool:
    """Legacy normalized exact-string match — the B3 fail-open fallback."""
    return _normalize_text(a) == _normalize_text(b)


def _cosine_similar(
    a: str,
    b: str,
    *,
    threshold: float | None = None,
    embed=None,
) -> bool:
    """Embedding cosine dedup with a B3 fail-open to exact-match (Living Self Act 1).

    Replaces the old ``_similar`` exact-string match so paraphrases converge,
    ``evidence_count`` climbs, and the ``>=2`` promotion gate fires.

    - ``threshold`` None -> ``get_inference_extraction_settings().dedup_threshold``
      resolved at CALL TIME (Rule 1).
    - ``embed`` None -> ``embeddings.embed_text`` (lazy import; injectable for tests).
    - Returns ``float(va @ vb) >= threshold`` (both vectors are L2-normalized by
      the embeddings contract, so cosine == dot) — Rule 2, decided by live vectors.

    FAIL-OPEN (B3): FastEmbed downloads ~130MB on first call and needs network;
    offline it RAISES. On ANY embed exception this falls back to the legacy
    normalized exact-string comparison — exact dups still merge, the offline
    standing suite stays green, and no caller ever crashes.
    """
    if threshold is None:
        from config import get_inference_extraction_settings

        threshold = get_inference_extraction_settings().dedup_threshold
    try:
        if embed is None:
            from embeddings import embed_text as embed  # lazy; injectable in tests
        va = embed(a)
        vb = embed(b)
        return float(va @ vb) >= threshold
    except Exception:
        # B3 fail-open: embeddings unavailable -> exact normalized match.
        return _exact_similar(a, b)


class InferenceTracker:
    """JSON-backed inference confidence tracker."""

    def __init__(self, state_file: Path) -> None:
        self._path = state_file

    def load(self) -> list[InferenceRecord]:
        """Load all inferences from state file."""
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [InferenceRecord(**r) for r in data]
        except (json.JSONDecodeError, TypeError, KeyError):
            pass
        return []

    def save(self, records: list[InferenceRecord]) -> None:
        """Write all inferences to state file ATOMICALLY (M5).

        tmp + ``os.replace`` so a crash mid-write never leaves a truncated
        corpus, and the corpus migration (which now mutates this file) inherits
        real atomicity. Every caller (add_inference / decay / contradict /
        migration) shares this guarantee.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)  # atomic on win32 + posix

    def add_inference(
        self,
        inference: str,
        observation: str,
        confidence: float,
        source: str = "auto_capture",
    ) -> InferenceRecord:
        """Add new inference or strengthen existing one if similar."""
        confirm_boost = 0.1
        try:
            from config import INFERENCE_CONFIRM_BOOST

            confirm_boost = INFERENCE_CONFIRM_BOOST
        except ImportError:
            pass

        records = self.load()
        now_iso = datetime.now(UTC).isoformat()

        # M1 skip-decayed: never compare a fresh belief against a decayed
        # poisoned record — a cosine match there would merge the new belief into
        # the decayed id/observation and resurrect poison. load() returns the
        # FULL file (incl. decayed rows); filter them out of the dedup set.
        actives = [r for r in records if r.status != "decayed"]

        # Find a similar existing active record.
        hit = self._find_similar_active(inference, actives)
        if hit is not None:
            hit.confidence = min(1.0, hit.confidence + confirm_boost)
            hit.evidence_count += 1
            hit.last_updated = now_iso
            if hit.evidence_count >= 3:
                hit.status = "confirmed"
            self.save(records)
            return hit

        # New inference
        record = InferenceRecord(
            id=str(uuid4()),
            inference=inference,
            observation=observation,
            confidence=confidence,
            evidence_count=1,
            first_seen=now_iso,
            last_updated=now_iso,
            source=source,
            status="active",
        )
        records.append(record)
        self.save(records)
        return record

    @staticmethod
    def _find_similar_active(
        inference: str,
        actives: list[InferenceRecord],
    ) -> InferenceRecord | None:
        """Return the first active record that dedup-matches ``inference``, else None.

        M1 cost fix: embed the incoming claim ONCE and the active-record texts
        ONCE via ``embeddings.embed_batch`` (a single batched ONNX pass), then
        take dot-products in numpy — instead of 2N ``embed_text`` calls
        re-embedding the same record texts per claim. The active set is bounded
        (``INFERENCE_PROMPT_CAP``-scale after the corpus migration), so this is
        genuinely cheap and amortized in the once-a-day reflection loop.

        Fail-open at two layers: if ``embed_batch`` raises (FastEmbed offline /
        no network), fall back to per-pair ``_cosine_similar`` — which ITSELF
        fail-opens to the normalized exact-string match. So the offline standing
        suite never triggers a 130MB download and no caller crashes.
        """
        if not actives:
            return None
        from config import get_inference_extraction_settings

        threshold = get_inference_extraction_settings().dedup_threshold
        try:
            from embeddings import embed_batch

            vecs = embed_batch([inference] + [r.inference for r in actives])
            v0 = vecs[0]
            for r, v in zip(actives, vecs[1:], strict=False):
                if float(v0 @ v) >= threshold:
                    return r
            return None
        except Exception as exc:
            # Fail-open: per-pair compare (which fail-opens to exact-match).
            # Make the degradation VISIBLE — a silent drop to exact-match means
            # paraphrases stop converging (the whole point of embedding dedup)
            # with no signal. One diagnostic line turns that invisible.
            print(
                f"[self_model] embed_batch unavailable, semantic dedup degraded "
                f"to exact-match (non-fatal): {exc!r}",
                flush=True,
            )
            for r in actives:
                if _cosine_similar(r.inference, inference, threshold=threshold):
                    return r
            return None

    def decay_old_inferences(
        self,
        decay_days: int = 14,
        decay_rate: float = 0.05,
        min_confidence: float = 0.3,
    ) -> int:
        """Decay inferences not updated in decay_days. Returns count decayed."""
        records = self.load()
        cutoff = datetime.now(UTC) - timedelta(days=decay_days)
        cutoff_iso = cutoff.isoformat()
        decayed = 0

        for r in records:
            if r.status == "active" and r.last_updated and r.last_updated < cutoff_iso:
                old_confidence = r.confidence
                r.confidence = max(min_confidence, r.confidence - decay_rate)
                if r.confidence <= min_confidence:
                    r.status = "decayed"
                if r.confidence != old_confidence:
                    decayed += 1

        if decayed > 0:
            self.save(records)
        return decayed

    def contradict(
        self,
        inference_id: str,
        *,
        by: str | None = None,
        held: bool = False,
    ) -> bool:
        """Record a contradiction. Lowers confidence; demotes confirmed beliefs.

        Living Self Act 2 adds two OPTIONAL keyword-only args (zero-arg callers —
        the 6 standing unit tests — are unaffected: ``by=None, held=False`` is
        identical to the pre-Act-2 behavior):

        - ``held=False`` (normal disconfirmation): the EXISTING ``-0.15`` / floor
          ``0.1`` / demote-confirmed-below-0.7 math VERBATIM. This branch is the
          ONLY home of the ``-0.15`` math.
        - ``held=True`` (B1 explicit-vs-explicit hold): ``contradiction_count``
          and ``last_updated`` only — confidence is UNCHANGED and the demote is
          NOT run. Records the tension on an operator-stated belief the judge
          flagged but is not entitled to lower; surfaced as held-under-tension.
        - ``by`` (either branch): when provided, appended to ``contradicted_by``
          as the audit (``"{winner_id}:{reason}"``). The audit travels WITH the
          belief (Rule 2 — physical, inspectable, the surface the renderer reads
          and the B2 cross-run dedup key reads fresh).
        """
        records = self.load()
        for r in records:
            if r.id == inference_id:
                r.contradiction_count += 1  # count in BOTH branches
                if not held:  # normal disconfirmation — the -0.15 math (UNCHANGED)
                    r.confidence = max(0.1, r.confidence - 0.15)
                    if r.status == "confirmed" and r.confidence < 0.7:
                        r.status = "active"
                # held=True (B1): confidence UNCHANGED, demote NOT run.
                if by:  # audit — only when provided
                    r.contradicted_by.append(by)
                r.last_updated = datetime.now(UTC).isoformat()
                self.save(records)
                return True
        return False

    def get_active(self, min_confidence: float = 0.3) -> list[InferenceRecord]:
        """Return active inferences above min_confidence threshold."""
        return [
            r for r in self.load()
            if r.status != "decayed" and r.confidence >= min_confidence
        ]


def build_self_model_state(
    state_file: Path,
    *,
    min_confidence: float = 0.3,
) -> SelfModelState:
    """Build a structured psyche state from active inference records."""

    records = InferenceTracker(state_file).get_active(min_confidence=min_confidence)
    state = SelfModelState(generated_at=datetime.now(UTC).isoformat())
    for record in records:
        text = record.inference.strip()
        if not text:
            continue
        category = _classify_self_model_record(text)
        getattr(state, category).append(text)
        state.evidence.setdefault(category, []).append(record.id)
    return state


def render_self_model_state_section(state: SelfModelState) -> str:
    """Render the psyche state for prompts/status debug surfaces."""

    sections = ["## Active Self-Model / Psyche State"]
    groups = (
        ("Operator Beliefs", state.operator_beliefs),
        ("Homie Self-Beliefs", state.homie_beliefs),
        ("Current Drives", state.drives),
        ("Recurring Mistakes", state.recurring_mistakes),
        ("Open Loops", state.open_loops),
    )
    for title, items in groups:
        if not items:
            continue
        sections.append(f"### {title}")
        sections.extend(f"- {item}" for item in items[:8])
    if len(sections) == 1:
        sections.append("No active self-model inferences above threshold.")
    return "\n\n".join(sections)


def _classify_self_model_record(text: str) -> str:
    normalized = text.lower()
    if any(token in normalized for token in ("mistake", "failure", "wrong", "timeout", "broke")):
        return "recurring_mistakes"
    if any(token in normalized for token in ("follow up", "open loop", "needs", "next", "pending")):
        return "open_loops"
    if any(token in normalized for token in ("priority", "goal", "focus", "drive", "p0", "lane")):
        return "drives"
    if any(token in normalized for token in ("homie", "assistant", "self", "i should", "i need")):
        return "homie_beliefs"
    return "operator_beliefs"


# ===========================================================================
# Corpus migration (Living Self Act 1, B1 + M5) — one-time, reversible,
# audited, operator-run, atomic. Quarantines the ENTIRE source=auto_capture
# provenance class (the audit's ground truth: 100% of that class is poison —
# bot self-quotes OR raw user-fragments-that-aren't-beliefs, 0 legitimate
# operator beliefs). This is CLEAN-GARBAGE removal by structurally-untrustworthy
# ORIGIN — it forms/asserts/synthesizes NO belief (belief formation is the
# separate LLM extractor over verbatim operator words). NOT the forbidden
# band-aid heuristic.
# ===========================================================================

# The bot-self-quote regex survives ONLY as a decision-table reason annotation,
# NEVER as the quarantine criterion (the criterion is source == "auto_capture").
_BOT_QUOTE_RES = [
    re.compile(p, re.I)
    for p in (
        r"^\s*-\s+(end by asking|respond|ask whether)\b",
        r"variants or approval prep|want sharper variants|here'?s the (founder|tight|launch)",
        r"^\s*send me .+ and i'?ll|^\s*send the next|^\s*try this:|founder-announcement version",
    )
]


@dataclass
class MigrationReport:
    """Audit receipt for a corpus-quarantine run."""

    total: int
    kept: int
    quarantined: int
    decisions: list[tuple[str, str]] = field(default_factory=list)
    backup_path: str | None = None
    dry_run: bool = False


def _annotate_reason(rec: InferenceRecord) -> str:
    """Label a quarantined row for the human-audit table — decides NOTHING."""
    if any(r.search(rec.inference or "") for r in _BOT_QUOTE_RES):
        return "bot UX/offer line"
    return "auto_capture provenance"


def quarantine_auto_capture(
    state_file: Path,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> MigrationReport:
    """Quarantine every ``source == "auto_capture"`` record (B1 — provenance criterion).

    Reversible + audited + atomic:
      - reads the PHYSICAL records (Rule 2 — provenance, never a meta/cache claim);
      - on a real run, writes a timestamped ``<stem>.<UTC>.bak.json`` containing
        ALL original records BEFORE any mutation (reversal = restore the backup);
      - rewrites the live file ATOMICALLY via the now-atomic ``tracker.save`` (M5);
      - returns + (via the CLI) prints a per-record decision table.

    Records with ``source in {reflection, explicit}`` are KEPT (there are none
    today; fresh reflection runs accrue them going forward). The 236 already
    ``decayed`` records are still quarantined (they are ``auto_capture``) so no
    future re-derive can resurrect them as fresh beliefs.
    """
    now = now or datetime.now(UTC)
    tracker = InferenceTracker(state_file)
    records = tracker.load()

    kept: list[InferenceRecord] = []
    quarantined: list[InferenceRecord] = []
    for r in records:
        # B1: the criterion is PROVENANCE, not the regex.
        if r.source == "auto_capture":
            quarantined.append(r)
        else:
            kept.append(r)

    report = MigrationReport(
        total=len(records),
        kept=len(kept),
        quarantined=len(quarantined),
        decisions=[((q.inference or "")[:80], _annotate_reason(q)) for q in quarantined[:50]],
        dry_run=dry_run,
    )

    if not dry_run and quarantined:
        # Back up ALL originals BEFORE mutation (Rule 2 reversibility).
        bak = state_file.with_suffix(f".{now:%Y%m%dT%H%M%SZ}.bak.json")
        bak.write_text(
            json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tracker.save(kept)  # M5: genuinely atomic (tmp + os.replace)
        report.backup_path = str(bak)

    return report


def _console_safe(text: str) -> str:
    """Make a string printable on ANY console encoding (win32 cp1252-safe).

    The corpus carries smart-quotes / em-dashes / emoji that crash a cp1252
    stdout. Round-trip through the active stdout encoding with ``errors=replace``
    so the operator-run audit table never dies mid-print on a non-ASCII row.
    """
    import sys as _sys

    enc = getattr(_sys.stdout, "encoding", None) or "utf-8"
    try:
        return text.encode(enc, errors="replace").decode(enc, errors="replace")
    except Exception:
        return text.encode("ascii", errors="replace").decode("ascii")


def _print_migration_report(report: MigrationReport) -> None:
    """Human-legible audit table to stdout."""
    verb = "would quarantine" if report.dry_run else "quarantined"
    print(
        f"corpus migration: total {report.total} | "
        f"{verb} {report.quarantined} | keep {report.kept}"
    )
    if report.backup_path:
        print(f"backup (reversible): {_console_safe(report.backup_path)}")
    if report.decisions:
        print("--- decision table (first 50) ---")
        for text, reason in report.decisions:
            print(_console_safe(f"  [QUARANTINE] {reason:<24} | {text}"))


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Self-model corpus tools")
    sub = parser.add_subparsers(dest="command")

    mig = sub.add_parser(
        "migrate-corpus",
        help="Quarantine every source=auto_capture record (reversible, audited).",
    )
    mig.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the decision table only; write nothing.",
    )
    mig.add_argument(
        "--state-file",
        default=None,
        help="Override the inference state file path (default: config.INFERENCE_STATE_FILE).",
    )

    args = parser.parse_args(argv)
    if args.command != "migrate-corpus":
        parser.print_help()
        return 1

    if args.state_file:
        state_file = Path(args.state_file)
    else:
        from config import INFERENCE_STATE_FILE

        state_file = INFERENCE_STATE_FILE

    report = quarantine_auto_capture(state_file, dry_run=args.dry_run)
    _print_migration_report(report)
    return 0


if __name__ == "__main__":
    import sys as _sys

    _CHAT_DIR = Path(__file__).resolve().parent.parent
    _SCRIPTS_DIR = _CHAT_DIR.parent / "scripts"
    for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)

    raise SystemExit(_main())
