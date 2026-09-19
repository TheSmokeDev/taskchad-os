"""Evolve compatibility entrypoints and retained decision-artifact readers.

Belief proposals enter the shared persona learning journal and queue. Source
support, behavioral qualification, provider retries and publication have one
owner there. The historical artifact helpers remain readable for prior runs;
new automatic belief admission never grants direct amendment authority.

Recall commands retain their compatibility surface and delegate to their
existing evaluator/tuning owners.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# B3 — cross-slice sys.path bridge (MANDATORY, before any cognition.* import).
# evolve_loop.py lives at .claude/scripts/evolve/evolve_loop.py — ONE level
# DEEPER than memory_reflect.py (.claude/scripts/). The producer uses
# parent.parent (scripts -> .claude -> /chat, 2 hops); this file needs
# parent.parent.parent (evolve -> scripts -> .claude -> /chat, 3 hops). Copying
# the producer's 2-parent pattern verbatim would point at scripts/chat (WRONG —
# empirically verified). judge.py carries the identical header.
_EVOLVE_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _EVOLVE_DIR.parent
_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"  # NOT parent.parent (off-by-one)
# De-shadow: a bare `python evolve/evolve_loop.py` puts THIS file's dir (evolve/)
# on sys.path[0], where `evolve/statistics.py` SHADOWS the stdlib `statistics`
# (replay.py:25 imports the stdlib `statistics.mean`). Drop the evolve/ entry so
# stdlib resolves; all intra-evolve imports use the `evolve.` package prefix
# (resolved via scripts/ below), so this is safe. Idempotent for -m / pytest
# invocations where evolve/ is not on the path.
sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != _EVOLVE_DIR]
for _p in (_SCRIPTS_DIR, _CHAT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Boot-shim: must run BEFORE any framework imports (config, cognition, etc.).
from personas import apply_persona_override  # noqa: E402

apply_persona_override()


def _proposal_from(candidate: dict) -> Any:
    """B2 — build an ``AmendmentProposal`` via the field-filter, NEVER raw.

    The Archon researcher's candidate dict carries an EXTRA ``prediction`` field
    (the falsifiable belief-regression entry, Task 8) that ``AmendmentProposal``
    has no slot for — raw ``AmendmentProposal(**candidate)`` raises ``TypeError``.
    ``_coerce_dataclass`` (``amendments.py:793-798``) keeps ONLY dataclass fields,
    dropping ``prediction``/unknown keys. (The same field-filter is the right tool
    anywhere a test/loop builds an ``InferenceRecord`` from a candidate-shaped
    dict — ``self_model.py:114``'s ``InferenceRecord(**r)`` has the identical
    extra-key trap.)
    """
    from cognition.amendments import AmendmentProposal, _coerce_dataclass

    # Incoming evolve candidates must cite evidence explicitly. The amendment
    # ledger coercer intentionally honors dataclass defaults for legacy rows,
    # but that compatibility behavior must not silently turn malformed live
    # candidates into evidence-free proposals.
    if "evidence_paths" not in candidate:
        raise ValueError(f"candidate is missing required evidence_paths: keys={sorted(candidate)}")

    prop = _coerce_dataclass(AmendmentProposal, candidate)
    if prop is None:
        raise ValueError(
            f"candidate is not a valid AmendmentProposal shape: keys={sorted(candidate)}"
        )
    return prop


def _belief_corpus_with_prediction(candidate: dict, settings: Any) -> list:
    """N1 — the seed corpus PLUS the candidate's OWN falsifiable prediction.

    The Archon researcher ships a ``prediction`` the candidate claims its evidence
    will satisfy. ``_proposal_from`` drops it (to avoid the B2 crash), so we feed
    it back as an extra ``BeliefRegressionEntry`` (``kind="prediction"``) appended
    to the seed corpus — so the candidate is actually HELD to its own claim, not
    only the fixed seed checks. Empty/absent prediction -> just the seed corpus.
    """
    from evolve.belief_regression import (
        BeliefRegressionEntry,
        load_belief_regression_corpus,
    )

    corpus = list(load_belief_regression_corpus(settings.corpus_path))
    prediction = (candidate.get("prediction") or "").strip()
    if prediction:
        corpus.append(
            BeliefRegressionEntry(
                check_id="candidate-prediction",
                kind="prediction",
                description="The candidate's own falsifiable prediction (Archon-proposed).",
                params={"prediction": prediction, "min_overlap": settings.min_overlap},
            )
        )
    return corpus


def _write_belief_decision(
    proposal: Any,
    candidate: dict,
    ev_ok: bool,
    ev_reason: str,
    verdict: dict,
    outcome: str,
    *,
    outcome_reason: str = "",
    retryable: bool = False,
    attempts: int = 0,
    max_attempts: int = 0,
) -> Path:
    """M1 — the belief SIBLING decision artifact (NO recall ReportDelta).

    ``write_decision_artifact`` (``io.py:99``) REQUIRES ``delta: ReportDelta`` and
    calls ``delta.to_dict()`` — there is NO delta for a belief. The belief decision
    is a DIFFERENT shape under ``BELIEF_EVOLVE_DECISION_DIR``, keyed by the STABLE
    ``proposal.id`` (B1) so the artifact and the ledger row share the id. N1: the
    candidate's ``prediction`` is RECORDED so the audit shows what the candidate
    predicted vs what the floor/judge found. Do NOT route a belief failure through
    ``veto.format_verdict_table`` (m1 — it reads recall-only fields).

    F2 — ``outcome`` is the REAL result, NOT a pre-gate prediction:
      - ``"adopt"``   — on the live path, the apply RETURNED applied (the ledger
                        row flipped + SELF.md changed); on a dry-run, what WOULD
                        happen (the apply did not run).
      - ``"reject"``  — the floor/judge said no, OR the live apply's policy gate
                        REJECTED it (``outcome_reason`` carries the real
                        ``policy_reason`` so a low-confidence/oversized reject is
                        not mislabelled "adopt"). SELF.md/MEMORY.md is untouched
                        for this outcome.
      - ``"error"``   — the live apply RAISED (``outcome_reason`` = the repr),
                        OR the judge infra call raised (``outcome_reason`` =
                        ``"judge_failed"``, ``retryable=True``; SELF.md is
                        untouched, no lying "adopt" is written), OR the live
                        apply returned ``AmendmentApplyResult.status ==
                        "apply_pending"`` (``outcome_reason`` = the real
                        ``policy_reason``/status, ``retryable=True``) — the
                        target bytes ARE on disk but the ledger flip to
                        "applied" did not confirm; NOT a policy reject (the
                        belief was not declined), self-heals on the next
                        ``apply_amendment_if_allowed`` pass.
    ``outcome_reason`` records WHY (Rule 2 — physical), distinct from the pre-gate
    ``evidence_reason`` so the audit shows the floor/judge verdict AND the real
    apply-gate verdict. Note the deliberate asymmetry inside ``"error"``: the
    apply-RAISED path stays ``retryable=False`` (a raise may indicate a
    deterministic content failure and a partial write — the rollback snapshot
    covers it), while judge-infra and apply_pending are ``retryable=True``
    (transient, safe to re-pick). Consumers must branch on ``retryable``, not
    on ``outcome == "error"`` alone.
    """
    from datetime import UTC, datetime

    from config import BELIEF_EVOLVE_DECISION_DIR

    out = Path(BELIEF_EVOLVE_DECISION_DIR)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"decision-{proposal.id}.json"
    path.write_text(
        json.dumps(
            {
                "timestamp_utc": datetime.now(UTC).isoformat(),
                "proposal_id": proposal.id,
                "target_file": proposal.target_file,
                "candidate": {
                    "summary": proposal.summary,
                    "rationale": proposal.rationale,  # audit trail parity with `prediction`
                    "proposed_content": proposal.proposed_content,
                    "evidence_paths": proposal.evidence_paths,
                    "confidence_score": proposal.confidence_score,
                    "prediction": (candidate.get("prediction") or ""),  # N1 — recorded
                },
                "evidence_ok": ev_ok,
                "evidence_reason": ev_reason,
                "judge": verdict,
                "outcome": outcome,  # F2 — REAL outcome, not a pre-gate prediction
                "outcome_reason": outcome_reason,
                "retryable": retryable,  # F2 (#169) — judge outage is re-pickable
                # #170 — the retry budget for this candidate id. A retry updates
                # this SAME artifact (keyed by proposal.id), so `attempts` is the
                # running count across nights; when it reaches `max_attempts`,
                # propose_belief downgrades `retryable` to False (terminal).
                "attempts": attempts,
                "max_attempts": max_attempts,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _malformed_candidate_decision(candidate: dict, reason: str) -> dict:
    """F1 — write a reject artifact for a malformed candidate, NEVER crash.

    A candidate that cannot coerce into an ``AmendmentProposal`` (a missing
    required field — e.g. no ``evidence_paths``, which ``_proposal_from`` now
    rejects explicitly with ``ValueError``) is a REJECT, not a crash.
    The fail-open contract: a bad-shape
    candidate writes a reject decision artifact + a visible distinct print +
    returns the conservative reject dict, so the Archon bash node sees exit 0 and a
    reject artifact instead of a raw traceback. The artifact is keyed by a stable
    synthetic id (the candidate has no valid proposal id to mint one from).
    """
    from datetime import UTC, datetime

    from config import BELIEF_EVOLVE_DECISION_DIR

    synthetic_id = (
        "malformed-"
        + hashlib.sha1(
            json.dumps(candidate, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:12]
    )
    out = Path(BELIEF_EVOLVE_DECISION_DIR)
    try:
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"decision-{synthetic_id}.json"
        path.write_text(
            json.dumps(
                {
                    "timestamp_utc": datetime.now(UTC).isoformat(),
                    "proposal_id": synthetic_id,
                    "target_file": str(candidate.get("target_file", "")),
                    "candidate": {
                        "summary": str(candidate.get("summary", "")),
                        "proposed_content": str(candidate.get("proposed_content", "")),
                        "evidence_paths": candidate.get("evidence_paths"),
                        "confidence_score": candidate.get("confidence_score"),
                        "prediction": (candidate.get("prediction") or ""),
                    },
                    "evidence_ok": False,
                    "evidence_reason": reason,
                    "judge": {},
                    "outcome": "reject",
                    "outcome_reason": reason,
                    "retryable": False,  # F2 (#169) — malformed is terminal, never retryable
                    # #170 — a malformed candidate is terminal on sight; it never
                    # accrues attempts (schema-consistent with the real artifact).
                    "attempts": 0,
                    "max_attempts": 0,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # even artifact-write failure must not crash the loop
        print(
            f"[evolve.loop] propose-belief: malformed candidate ({reason}); "
            f"reject artifact write also failed (non-fatal): {exc!r}",
            flush=True,
        )
    print(
        f"[evolve.loop] propose-belief: outcome=reject reason={reason} "
        "(malformed candidate — fail-open, no mutation)",
        flush=True,
    )
    return {
        "adopt": False,
        "evidence_ok": False,
        "evidence_reason": reason,
        "supported": False,
        "correctness": 0.0,
        "evidence_fidelity": 0.0,
        "reason": reason,
    }


async def propose_belief(
    candidate: dict,
    *,
    dry_run: bool = True,
    memory_dir: Path | str | None = None,
    reasoning: Any | None = None,
    attempts: int = 0,
    service: Any | None = None,
) -> dict:
    """Compatibility admission adapter to the shared learning journal/queue.

    The old support-only judge cannot publish standing behavior. No provider
    runs here; shared evaluation owns retries and typed infrastructure deferral.
    Dry runs preserve the old call shape but spend no inference or retry budget.
    """
    from config import get_belief_evolve_settings
    from personas.learning.authority import submit_proposal
    from personas.learning.errors import LearningDeferredError, LearningUnavailableError
    from personas.learning.models import LearningError, content_hash

    settings = get_belief_evolve_settings()
    base = {
        "adopt": False,
        "supported": False,
        "correctness": 0.0,
        "evidence_fidelity": 0.0,
        "attempts": attempts,
        "max_attempts": settings.max_attempts,
    }
    if not settings.enabled:
        return {
            **base,
            "outcome": "disabled",
            "reason": "evolve_disabled",
            "evidence_ok": False,
            "evidence_reason": "evolve_disabled",
        }
    try:
        proposal = _proposal_from(candidate)
        if not isinstance(proposal.summary, str) or not proposal.summary.strip():
            raise ValueError("belief summary is required")
        if not isinstance(proposal.proposed_content, str) or not proposal.proposed_content.strip():
            raise ValueError("belief content is required")
    except (ValueError, TypeError):
        return {
            **base,
            "outcome": "reject",
            "reason": "malformed_candidate",
            "evidence_ok": False,
            "evidence_reason": "malformed_candidate",
        }
    if dry_run:
        return {
            **base,
            "outcome": "pending",
            "reason": "shared_evaluation_required",
            "retryable": True,
            "evidence_ok": False,
            "evidence_reason": "not_evaluated",
            "dry_run": True,
        }
    if service is None:
        from personas import get_active_profile_name
        from personas.learning.service import get_learning_service

        service = get_learning_service(get_active_profile_name())
    if memory_dir is not None and Path(memory_dir).resolve() != service.target.memory_dir.resolve():
        raise LearningError("belief_target_does_not_match_persona")
    # A stable payload fingerprint coalesces repeated nightly output even when
    # the old producer supplied a new UUID. Paths remain provenance, never fake
    # real-world observations. Missing root IDs leave an inspectable pending row.
    payload = {
        "candidate_type": "self_model",
        "title": proposal.summary,
        "content": proposal.proposed_content,
        "applicability": candidate.get("applicability") or proposal.summary,
        "evidence_ids": candidate.get("evidence_ids", []),
        "counterevidence_ids": candidate.get("counterevidence_ids", []),
        "changes_behavior": candidate.get("changes_behavior", False),
        "target_file": proposal.target_file,
        "producer": "evolve_belief",
        "source_manifest": candidate.get(
            "source_manifest",
            [
                {"ref": str(path), "kind": "legacy_citation", "unverified": True}
                for path in proposal.evidence_paths
            ],
        ),
        "derived_input_ids": candidate.get("derived_input_ids", []),
        "producer_runtime": candidate.get("producer_runtime", {}),
        **({"cycle_id": candidate["cycle_id"]} if candidate.get("cycle_id") else {}),
    }
    try:
        change = submit_proposal(
            service, payload, source_key="evolve-belief:" + content_hash(payload)
        )
    except LearningDeferredError:
        raise
    except (TimeoutError, ConnectionError, OSError) as exc:
        raise LearningUnavailableError("belief_admission_unavailable") from exc
    return {
        **base,
        "outcome": "pending",
        "reason": change.get("reason", "queued_for_evaluation"),
        "retryable": True,
        "evidence_ok": False,
        "evidence_reason": "not_evaluated",
        "change_proposal_id": change["id"],
        "candidate_id": change.get("candidate_id"),
        "shared_queue": True,
    }


async def propose(
    *,
    dry_run: bool = True,
    memory_dir: Path | str | None = None,
    candidate_overrides: dict | None = None,
    run_replay_fn: Any | None = None,
) -> int:
    """Legacy scheduled recall wake now admits the shared tuning queue.

    Explicit overrides or an injected replay retain the old offline diagnostic
    comparison. Diagnostic acceptance never installs a recall policy.
    """
    from config import get_belief_evolve_settings

    s = get_belief_evolve_settings()
    if not s.enabled:  # m6 — EVOLVE_ENABLED enforcement point (BOTH subcommands)
        print(
            "[evolve.loop] EVOLVE_ENABLED=false — propose disabled; no artifact, no mutation.",
            flush=True,
        )
        return 0

    if candidate_overrides is None and run_replay_fn is None:
        from evolve import tuning
        from personas import get_active_profile_name
        from personas.learning.service import LearningService

        service = LearningService.for_persona(get_active_profile_name())
        if (
            memory_dir is not None
            and Path(memory_dir).resolve() != service.target.memory_dir.resolve()
        ):
            raise ValueError("scheduled tuning vault must match the active persona")
        receipt = tuning.tuning_status(service) if dry_run else await tuning.tune(service)
        print(
            json.dumps({"operation": "recall_tuning_admission", "dry_run": dry_run, **receipt}),
            flush=True,
        )
        return 0

    from evolve.compare import compare_reports
    from evolve.goldens import load_regression_queries
    from evolve.io import write_decision_artifact
    from evolve.regression import evaluate_regression_corpus, load_regression_entries
    from evolve.replay import run_replay
    from evolve.veto import DEFAULT_VETO_RULESET, compute_exit_code, evaluate_veto

    replay = run_replay_fn or run_replay

    # M3 — load the regression corpus and replay its EXACT query list so the
    # per-query results are index-aligned with the entries.
    raw_regression = load_regression_queries()
    regression_entries = load_regression_entries(raw_regression)
    regression_query_texts = [r["query"] for r in raw_regression]

    overrides = dict(candidate_overrides or {})

    baseline = await replay(
        regression_query_texts,
        None,
        memory_dir,
        experiment_id="evolve-propose-baseline",
        caller="evolve_loop.propose",
    )
    candidate_report = await replay(
        regression_query_texts,
        overrides,
        memory_dir,
        experiment_id="evolve-propose-candidate",
        baseline_experiment_id="evolve-propose-baseline",
        caller="evolve_loop.propose",
    )

    delta = compare_reports(baseline, candidate_report)
    regression_summary = evaluate_regression_corpus(candidate_report.per_query, regression_entries)
    verdict = evaluate_veto(delta, DEFAULT_VETO_RULESET, regression_summary=regression_summary)
    exit_code = compute_exit_code(verdict, force=False)

    from config import DATA_DIR

    reports_dir = Path(DATA_DIR) / "evolve" / "reports"
    write_decision_artifact(
        reports_dir,
        baseline_experiment_id=baseline.experiment_id,
        candidate_experiment_id=candidate_report.experiment_id,
        ruleset_name=DEFAULT_VETO_RULESET.name,
        delta=delta,
        verdict=verdict,
        force=False,
        exit_code=int(exit_code),
        overrides=overrides,
    )
    print(
        f"[evolve.loop] propose (recall): accepted={verdict.accepted} "
        f"exit_code={int(exit_code)} dry_run={dry_run} (offline diagnostic; no policy adoption)",
        flush=True,
    )
    return int(exit_code)


def _load_candidate(value: str) -> dict:
    """Load a candidate from a JSON file path OR an inline JSON string."""
    p = Path(value)
    if p.exists():
        raw = json.loads(p.read_text(encoding="utf-8"))
    else:
        raw = json.loads(value)
    if not isinstance(raw, dict):
        raise ValueError(f"--candidate must be a JSON object, got {type(raw).__name__}")
    return raw


def extract_belief_candidates(text: str) -> list[dict[str, Any]]:
    """Pull ``kind: belief_candidate`` JSON blocks out of the nightly
    consolidation LLM response (Living Self Act 4, dream-cycle autonomy — #170).

    Reuses ``cognition.amendments._iter_json_records`` — no new parser was written.
    This is a NEW dependency for this module (``_proposal_from`` already reaches into
    ``amendments.py`` for the DIFFERENT private helper ``_coerce_dataclass``; this is
    the first call site here for ``_iter_json_records``). Returns RAW dicts with
    ``kind`` stripped; ``propose_belief``'s own ``_proposal_from`` does the
    ``AmendmentProposal`` coercion later, so this function's only job is to find +
    tag-filter the blocks.
    """
    from cognition.amendments import _iter_json_records

    out: list[dict[str, Any]] = []
    for record in _iter_json_records(text):
        if isinstance(record, dict) and record.get("kind") == "belief_candidate":
            # Strip `kind` AND any LLM-supplied `id` (Kimi gate MINOR on PR
            # #181): a hallucinated/injected id on a FRESH candidate would
            # collide with a queued retry's decision-artifact/ledger identity
            # (one file per id, overwritten) and clobber its audit/retry state.
            # Only load_retryable_belief_candidates mints trusted ids; a fresh
            # block gets a new uuid via AmendmentProposal.__post_init__.
            out.append({k: v for k, v in record.items() if k not in ("kind", "id")})
    return out


def load_retryable_belief_candidates(
    *, decision_dir: Path | str | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Reconstruct re-postable candidate dicts from prior retryable decision
    artifacts (#170).

    Preserves the ORIGINAL proposal id (``AmendmentProposal.__post_init__`` only
    mints a new uuid when id is falsy) so a retry updates the SAME decision artifact
    + ledger row instead of duplicating it. ``decision-*.json`` is ONE file per
    ``proposal.id``, OVERWRITTEN on every write — every file IS the latest state for
    its id (no "latest of many" ambiguity). ``_malformed_candidate_decision``'s
    synthetic ``malformed-*`` ids are ALWAYS ``retryable=False`` so they never
    appear here. ``_attempts`` carries the running count so the caller can thread it
    back into ``propose_belief(attempts=...)``.

    Returns ``(candidates, skipped_count)`` — a file that fails to read/parse
    (truncated write, transient lock, malformed shape) is logged and skipped
    rather than silently dropped or crashing the whole retry queue; the count
    lets the caller surface corruption in the Phase-5 receipt instead of it
    being invisible outside ``dream_runs.log``.
    """
    from config import BELIEF_EVOLVE_DECISION_DIR

    base = Path(decision_dir) if decision_dir is not None else Path(BELIEF_EVOLVE_DECISION_DIR)
    out: list[dict[str, Any]] = []
    if not base.exists():
        return out, 0
    skipped = 0
    for f in sorted(base.glob("decision-*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if not d.get("retryable"):
                continue
            cand = dict(d.get("candidate") or {})
            cand["id"] = d.get("proposal_id", "")
            cand["target_file"] = d.get("target_file", "")
            cand["_attempts"] = int(d.get("attempts", 0))
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            skipped += 1
            print(
                f"[evolve.loop] load_retryable_belief_candidates: skipping "
                f"unreadable/malformed {f.name}: {exc!r}",
                flush=True,
            )
            continue
        out.append(cand)
    return out, skipped


def main() -> None:
    """CLI — ``uv run python evolve_loop.py <propose|propose-belief> [flags]``.

    Mirrors ``memory_reflect.main()`` (argparse + ``asyncio.run``).
    """
    parser = argparse.ArgumentParser(
        description="Living Self Act 4 — evolve loop (recall safe-first + belief rail)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_propose = sub.add_parser("propose", help="Admit recall tuning to the shared learning queue")
    p_propose.add_argument(
        "--dry-run", action="store_true", help="Inspect readiness without enqueueing"
    )

    p_belief = sub.add_parser(
        "propose-belief", help="The identity rail (evidence-read -> floor -> judge)"
    )
    p_belief.add_argument(
        "--candidate", required=True, help="Candidate JSON (file path or inline string)"
    )
    p_belief.add_argument(
        "--dry-run", action="store_true", help="Write artifact only; do NOT mutate SELF.md"
    )

    args = parser.parse_args()

    if args.command == "propose":
        exit_code = asyncio.run(propose(dry_run=args.dry_run))
        sys.exit(int(exit_code))
    elif args.command == "propose-belief":
        candidate = _load_candidate(args.candidate)
        result = asyncio.run(propose_belief(candidate, dry_run=args.dry_run))
        # Exit 0 on a clean run regardless of adopt/reject (the artifact carries
        # the outcome); a crash already raised.
        print(json.dumps(result, indent=2), flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
