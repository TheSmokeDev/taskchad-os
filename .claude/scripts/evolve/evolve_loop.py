"""Living Self Act 4 — the evolve-loop orchestrator + the missing propose() seam.

``evolve/regression.py:16`` and ``veto.py:20`` reference a ``propose()`` that was
NEVER built (grep ``def propose`` evolve/ -> zero non-comment hits). This module
BUILDS it (the recall ``propose`` subcommand) and ADDS the new ``propose-belief``
identity rail — the fitness oracle Archon calls.

TWO subcommands, both gated by ``EVOLVE_ENABLED`` at their entrypoint (m6):
  - ``propose`` (recall safe-first, the no-op-safe wake-the-loop proof): run
    ``run_replay`` over the baseline + a candidate override-set, ``compare_reports``,
    the EXISTING recall ``evaluate_regression_corpus``, ``evaluate_veto(delta,
    ruleset, regression_summary=...)``, ``write_decision_artifact``. NO identity
    mutation — recall params only. M3: replay over the EXACT ``regression_queries
    .json`` ``query`` list (via ``goldens.load_regression_queries``) so the
    per-query results are index-aligned with the regression entries
    (``evaluate_regression_corpus`` raises ``ValueError`` on length mismatch).
  - ``propose-belief`` (the identity rail): construct the ``AmendmentProposal``
    ONCE via ``_proposal_from`` (B1 — reuse the SAME instance for ``led.append`` +
    ``apply`` so the ledger row flips to ``applied``), run ``verify_evidence_support``
    (the confined evidence-READ + deterministic floor, incl. the candidate's own
    N1 ``prediction``), then ``judge_belief_candidate`` (the scheduled LLM judge),
    then ``_write_belief_decision`` (M1 — the SIBLING artifact, NOT
    ``write_decision_artifact`` which needs a recall ReportDelta that does not
    exist for a belief). On ADOPT AND not ``--dry-run``, route the WINNER through
    the UNCHANGED ledger with the SAME deterministic gate bound (defense-in-depth).

Boundary (vertical-slice): the candidate-SEARCH loop is Archon; the FITNESS ORACLE
(``evolve/``) + the STORE (the ledger + memory.db) are The Homie. This module is
the contract surface Archon calls (writes a candidate JSON, reads a decision
artifact). The bare cron runs the SAFE recall ``propose``; the belief rail is
Archon-driven (provider-quota discipline).

Rule 1 (call-time settings), Rule 2 (physical reads + atomic artifact), Rule 3
(the judge rides reasoning_step -> run_with_runtime_lanes), fail-open VISIBLE.
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
                        not mislabelled "adopt").
      - ``"error"``   — the live apply RAISED (``outcome_reason`` = the repr);
                        SELF.md is untouched, no lying "adopt" is written.
    ``outcome_reason`` records WHY (Rule 2 — physical), distinct from the pre-gate
    ``evidence_reason`` so the audit shows the floor/judge verdict AND the real
    apply-gate verdict.
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
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _malformed_candidate_decision(candidate: dict, reason: str) -> dict:
    """F1 — write a reject artifact for a malformed candidate, NEVER crash.

    A candidate that cannot coerce into an ``AmendmentProposal`` (a missing
    required field — e.g. no ``evidence_paths`` -> ``_coerce_dataclass`` returns
    ``None``) is a REJECT, not a crash. The fail-open contract: a bad-shape
    candidate writes a reject decision artifact + a visible distinct print +
    returns the conservative reject dict, so the Archon bash node sees exit 0 and a
    reject artifact instead of a raw traceback. The artifact is keyed by a stable
    synthetic id (the candidate has no valid proposal id to mint one from).
    """
    from datetime import UTC, datetime

    from config import BELIEF_EVOLVE_DECISION_DIR

    synthetic_id = "malformed-" + hashlib.sha1(
        json.dumps(candidate, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
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
) -> dict:
    """The identity rail — evidence-READ -> floor -> judge -> decision artifact.

    On ADOPT (``supported and correctness >= min and evidence_fidelity >= min``)
    AND not ``dry_run``, route the WINNER through the UNCHANGED ledger
    (``led.append`` + ``apply_amendment_if_allowed`` with the SAME deterministic
    gate bound — defense-in-depth; the gate is the source of truth, not the loop's
    earlier pass). ``--dry-run`` writes the artifact + prints the verdict but does
    NOT mutate SELF.md. m6: ``EVOLVE_ENABLED`` is enforced at the entrypoint.
    """
    from cognition.evidence_gate import read_evidence_texts, verify_evidence_support

    from config import MEMORY_DIR, PROJECT_ROOT, get_belief_evolve_settings
    from evolve.judge import judge_belief_candidate

    s = get_belief_evolve_settings()
    if not s.enabled:  # m6 — EVOLVE_ENABLED enforcement point
        print(
            "[evolve.loop] EVOLVE_ENABLED=false — propose-belief disabled; "
            "no artifact, no mutation.",
            flush=True,
        )
        return {
            "adopt": False,
            "evidence_ok": False,
            "evidence_reason": "evolve_disabled",
            "supported": False,
            "correctness": 0.0,
            "evidence_fidelity": 0.0,
            "reason": "evolve_disabled",
        }

    mem = Path(memory_dir) if memory_dir is not None else MEMORY_DIR

    # F1 — a malformed candidate (missing required field, e.g. no evidence_paths)
    # is a REJECT, not a crash. _proposal_from raises ValueError when
    # _coerce_dataclass returns None; catch it, write a reject artifact, return the
    # conservative dict (the Archon bash node sees exit 0 + a reject artifact, not a
    # raw traceback — the fail-open contract).
    try:
        proposal = _proposal_from(candidate)  # B1 — construct ONCE, reuse instance
    except (ValueError, TypeError):
        return _malformed_candidate_decision(candidate, "malformed_candidate")

    # N1 — the floor sees the candidate's OWN prediction (extra entry) + the seed.
    corpus = _belief_corpus_with_prediction(candidate, s)

    # The confined evidence-READ + deterministic floor (incl. the prediction).
    ev_ok, ev_reason = verify_evidence_support(proposal, mem, settings=s, corpus=corpus)
    # The SAME confined+bounded resolver the gate uses — the judge never sees a
    # path the gate rejected (M4).
    evidence_texts = read_evidence_texts(proposal, mem, settings=s)

    verdict = await judge_belief_candidate(
        candidate, evidence_texts, cwd=PROJECT_ROOT, settings=s, reasoning=reasoning
    )
    # The pre-gate PREDICTION (floor + judge ONLY) — NOT the final outcome. The
    # UNCHANGED apply-time policy gate (confidence >= 0.75, content <= 1200 chars,
    # secret/destructive regex) is checked SEPARATELY below; the artifact's
    # `outcome` is reconciled to what ACTUALLY happened to SELF.md/the ledger (F2),
    # never to this prediction.
    predicted_adopt = (
        ev_ok
        and verdict["supported"]
        and verdict["correctness"] >= s.min_correctness
        and verdict["evidence_fidelity"] >= s.min_fidelity
    )

    # The REAL outcome + reason, reconciled from the apply result (F2). Defaults to
    # the prediction for the floor/judge-reject and dry-run paths (where no apply
    # runs); overwritten by the live apply's actual AmendmentApplyResult below.
    outcome = "adopt" if predicted_adopt else "reject"
    outcome_reason = "" if predicted_adopt else (ev_reason or verdict.get("reason", ""))
    applied = predicted_adopt  # what we REPORT as `adopt` — corrected on the live path

    if predicted_adopt and not dry_run:
        from cognition.amendments import (
            AmendmentPolicy,
            ProposalLedger,
            apply_amendment_if_allowed,
            ledger_file_lock,
        )

        from config import AMENDMENT_LEDGER_FILE

        # Defense-in-depth: the SAME deterministic gate (with the SAME N1-augmented
        # corpus) runs again at apply time — the gate is the source of truth.
        policy = AmendmentPolicy(
            evidence_check=lambda p, m: verify_evidence_support(
                p, m, settings=s, corpus=corpus
            )
        )
        # F2 — CAPTURE the AmendmentApplyResult and WRAP the apply. The apply re-runs
        # the UNCHANGED policy gate (which the loop's prediction does NOT mirror), so
        # a confidence=0.5 / >1200-char belief the loop "predicted adopt" can be
        # REJECTED here — the artifact must record that REALITY, not the prediction.
        # An apply exception (SELF.md unwritable / locked on win32) must NOT crash
        # the loop and must NOT leave a lying "adopt" artifact.
        try:
            with ledger_file_lock(AMENDMENT_LEDGER_FILE):  # reentrant, like producers
                led = ProposalLedger(AMENDMENT_LEDGER_FILE)
                led.append(proposal)  # B1 — append the SAME instance...
                result = apply_amendment_if_allowed(
                    proposal, led, mem, policy=policy
                )  # ...and apply the SAME id -> the ledger row flips to applied
            # Reconcile the artifact to the REAL apply result.
            if result.status == "applied":
                outcome, outcome_reason, applied = "adopt", result.policy_reason, True
            else:
                # policy_rejected (or any non-applied terminal) — the belief did NOT
                # land. Record the REAL policy_reason (low_confidence / content_too_large
                # / etc.) so the artifact does not lie.
                outcome = "reject"
                outcome_reason = result.policy_reason or result.status
                applied = False
        except Exception as exc:  # F2 — uncontained apply crash -> contained "error"
            outcome, outcome_reason, applied = "error", repr(exc), False
            print(
                f"[evolve.loop] propose-belief: apply RAISED (non-fatal): {exc!r} — "
                "outcome=error, SELF.md untouched, no lying adopt artifact.",
                flush=True,
            )

    # F2 — write the artifact AFTER the apply, from the REAL outcome (never the
    # pre-gate prediction). On dry-run / floor-reject, `outcome` is the prediction
    # (no apply ran); on the live path it is the actual applied/rejected/error truth.
    _write_belief_decision(
        proposal, candidate, ev_ok, ev_reason, verdict, outcome,
        outcome_reason=outcome_reason,
    )

    print(
        f"[evolve.loop] propose-belief: outcome={outcome}"
        + (f" ({outcome_reason})" if outcome_reason else "")
        + f" evidence_ok={ev_ok} ({ev_reason}) "
        f"supported={verdict['supported']} correctness={verdict['correctness']:.2f} "
        f"fidelity={verdict['evidence_fidelity']:.2f} dry_run={dry_run}",
        flush=True,
    )
    return {
        "adopt": applied,  # F2 — the REAL outcome (apply-reconciled), not the prediction
        "outcome": outcome,
        "outcome_reason": outcome_reason,
        "evidence_ok": ev_ok,
        "evidence_reason": ev_reason,
        **verdict,
    }


async def propose(
    *,
    dry_run: bool = True,
    memory_dir: Path | str | None = None,
    candidate_overrides: dict | None = None,
    run_replay_fn: Any | None = None,
) -> int:
    """The recall safe-first proof (the no-op-safe wake-the-loop). Returns an
    ``ExitCode`` int. NO identity mutation — recall params only.

    M3: ``run_replay`` over the EXACT ``regression_queries.json`` ``query`` list
    (via ``goldens.load_regression_queries``) so the per-query results are
    index-aligned with the regression entries (``evaluate_regression_corpus``
    raises ``ValueError`` on length mismatch). Keeps ``write_decision_artifact``
    (recall has a real ReportDelta). m6: ``EVOLVE_ENABLED`` enforced at entry.

    ``run_replay_fn`` is injectable for tests where the embedding model is offline
    (a fake replay proves the orchestration wiring without the ~130MB model).
    """
    from config import get_belief_evolve_settings

    s = get_belief_evolve_settings()
    if not s.enabled:  # m6 — EVOLVE_ENABLED enforcement point (BOTH subcommands)
        print(
            "[evolve.loop] EVOLVE_ENABLED=false — propose disabled; "
            "no artifact, no mutation.",
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
    regression_summary = evaluate_regression_corpus(
        candidate_report.per_query, regression_entries
    )
    verdict = evaluate_veto(
        delta, DEFAULT_VETO_RULESET, regression_summary=regression_summary
    )
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
        f"exit_code={int(exit_code)} dry_run={dry_run} (no identity mutation)",
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
        raise ValueError(
            f"--candidate must be a JSON object, got {type(raw).__name__}"
        )
    return raw


def main() -> None:
    """CLI — ``uv run python evolve_loop.py <propose|propose-belief> [flags]``.

    Mirrors ``memory_reflect.main()`` (argparse + ``asyncio.run``).
    """
    parser = argparse.ArgumentParser(
        description="Living Self Act 4 — evolve loop (recall safe-first + belief rail)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_propose = sub.add_parser("propose", help="Recall safe-first (no identity mutation)")
    p_propose.add_argument("--dry-run", action="store_true", help="Write artifact only")

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
