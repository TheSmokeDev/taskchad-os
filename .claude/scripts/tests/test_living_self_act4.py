"""Tests for Living Self Act 4 — evolve/ -> identity (beliefs EARNED, not asserted).

Categories map to the PRP's Validation Loop (Level 2, categories 1-9). Every test
is tmp_path-scoped with injected fake ``reasoning`` / ``read_text`` / replay and a
born-clean synthetic corpus — NO live state (SELF.md, the amendment ledger,
self-model-inferences.json, memory.db) is ever touched, and only the judge needs a
provider (a fake-runtime; the deterministic floor + evidence-read run with NO
provider). win32 + provider-agnostic.

  1. Rule-1 settings resolver — env-swept defaults, monkeypatch flips on next call,
     explicit-arg passthrough (FAILS pre-fix: no resolver).
  2. The deterministic belief-regression floor — each kind discriminating, zero-LLM
     (FAILS pre-fix: no module).
  3. The floor is --force-proof via the UNCHANGED evaluate_veto (the inherited
     contract).
  4. The evidence-READ gate — open + verify + M4 SECURITY (traversal / absolute /
     oversized / missing all rejected + never read) (FAILS pre-fix: no module).
  5. Automatic amendment admission plus explicitly reviewed evidence/static gates.
  6. The scheduled LLM judge — INDEPENDENT prompt + typed infrastructure deferral +
     M5 OBJECT-tolerant parse (NOT _coerce_claim_list) (FAILS pre-fix: no module).
  7. The compatibility adapter — shared queue admission, scoped provenance,
     duplicate coalescing, read-only dry runs, and no inline provider execution.
  8. propose (recall safe-first) writes a decision artifact via a fake replay.
  9. Explicit operator review preserves the existing physical application owner.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cognition import amendments as am  # noqa: E402
from cognition import evidence_gate as eg  # noqa: E402

import config  # noqa: E402
from evolve import belief_regression as br  # noqa: E402
from evolve import evolve_loop as el  # noqa: E402
from evolve import judge as jd  # noqa: E402

# ===========================================================================
# Helpers — synthetic candidate + corpus (born-clean)
# ===========================================================================


def _candidate(
    *,
    proposed_content: str = "The Homie routes tasks by lane first, then provider.",
    evidence_paths: list[str] | None = None,
    confidence_score: float = 0.9,
    summary: str = "lane-first routing",
    source: str = "reflection",
    prediction: str | None = None,
) -> dict:
    c = {
        "source": source,
        "target_file": "SELF.md",
        "summary": summary,
        "rationale": "observed in the logs",
        "evidence_paths": list(evidence_paths or ["daily/2026-06-13.md"]),
        "proposed_content": proposed_content,
        "confidence_score": confidence_score,
        "status": "pending",
    }
    if prediction is not None:
        c["prediction"] = prediction
    return c


def _seed_corpus() -> list:
    """A 2-entry corpus (no_unread_claim + evidence_fidelity ON) for the floor."""
    return [
        br.BeliefRegressionEntry(
            check_id="no-unread-claim", kind="no_unread_claim", description="", params={}
        ),
        br.BeliefRegressionEntry(
            check_id="evidence-fidelity",
            kind="evidence_fidelity",
            description="",
            params={"min_overlap": 0.10},
        ),
    ]


def _dict_reader(mapping: dict[str, str], *, spy: list | None = None):
    """A fake ``read_text(Path) -> str`` over a {resolved-or-suffix: text} map.

    Matches by exact resolved path OR by filename suffix so tests can key on a
    bare name. Records every path it is asked for in ``spy`` (to assert a
    rejected path was NEVER read).
    """

    def _read(path: Path) -> str:
        if spy is not None:
            spy.append(str(path))
        key = str(path)
        norm = key.replace("\\", "/")  # win32: compare with forward-slash keys
        if key in mapping:
            return mapping[key]
        for k, v in mapping.items():
            kn = k.replace("\\", "/")
            if norm.endswith(kn) or Path(key).name == Path(k).name:
                return v
        return ""

    return _read


# ===========================================================================
# Category 1 — Rule-1 settings resolver
# ===========================================================================


def test_belief_evolve_settings_defaults(monkeypatch):
    for var in (
        "EVOLVE_ENABLED",
        "BELIEF_EVIDENCE_MIN_SUPPORTING_PATHS",
        "BELIEF_EVIDENCE_MIN_OVERLAP",
        "BELIEF_EVIDENCE_MAX_BYTES",
        "BELIEF_JUDGE_MIN_CORRECTNESS",
        "BELIEF_JUDGE_MIN_FIDELITY",
        "BELIEF_REGRESSION_CORPUS_PATH",
    ):
        monkeypatch.delenv(var, raising=False)
    s = config.get_belief_evolve_settings()
    assert s.enabled is True
    assert s.min_supporting_paths == 1
    assert s.min_overlap == 0.10
    assert s.max_bytes == 524288
    assert s.min_correctness == 0.6
    assert s.min_fidelity == 0.6
    assert s.corpus_path is None


def test_belief_evolve_settings_env_flips_on_next_call(monkeypatch):
    monkeypatch.setenv("EVOLVE_ENABLED", "false")
    monkeypatch.setenv("BELIEF_EVIDENCE_MIN_OVERLAP", "0.25")
    monkeypatch.setenv("BELIEF_EVIDENCE_MAX_BYTES", "1024")
    s = config.get_belief_evolve_settings()  # no module reload
    assert s.enabled is False
    assert s.min_overlap == 0.25
    assert s.max_bytes == 1024


def test_belief_evolve_settings_explicit_args_passthrough(monkeypatch):
    monkeypatch.setenv("EVOLVE_ENABLED", "false")  # ignored — explicit wins
    s = config.get_belief_evolve_settings(enabled=True, min_supporting_paths=2, min_correctness=0.9)
    assert s.enabled is True
    assert s.min_supporting_paths == 2
    assert s.min_correctness == 0.9


# ===========================================================================
# Category 2 — the deterministic belief-regression floor (zero-LLM)
# ===========================================================================


def test_floor_no_unread_claim_fails_on_empty_evidence():
    corpus = _seed_corpus()
    cand = {"proposed_content": "I verified the doc and it confirms lane-first routing."}
    # an EMPTY cited evidence text -> the doc-read floor fails
    summary = br.evaluate_belief_regression(cand, {"daily/x.md": ""}, corpus)
    reasons = {f.reason for f in summary.failed}
    assert "claims_read_but_evidence_empty_or_missing" in reasons


def test_floor_no_unread_claim_passes_with_nonempty_evidence():
    corpus = _seed_corpus()
    cand = {"proposed_content": "I verified the doc: routing is lane-first by provider."}
    texts = {"daily/x.md": "the system routes by lane first then provider, verified doc"}
    summary = br.evaluate_belief_regression(cand, texts, corpus)
    assert not any(f.reason == "claims_read_but_evidence_empty_or_missing" for f in summary.failed)


def test_floor_no_unread_claim_not_applicable_when_no_read_asserted():
    corpus = [
        br.BeliefRegressionEntry(check_id="c", kind="no_unread_claim", description="", params={})
    ]
    cand = {"proposed_content": "The operator prefers concise replies."}  # no read verb
    summary = br.evaluate_belief_regression(cand, {}, corpus)
    assert summary.failed == []  # N/A -> pass (no false positive)


def test_floor_evidence_fidelity_fails_on_zero_overlap():
    corpus = [
        br.BeliefRegressionEntry(
            check_id="c",
            kind="evidence_fidelity",
            description="",
            params={"min_overlap": 0.10},
        )
    ]
    cand = {"proposed_content": "Quantum chromodynamics governs gluon confinement."}
    texts = {"daily/x.md": "the operator prefers concise replies about routing"}
    summary = br.evaluate_belief_regression(cand, texts, corpus)
    assert any(f.reason == "claim_unsupported_by_cited_evidence" for f in summary.failed)


def test_floor_evidence_fidelity_passes_on_shared_tokens():
    corpus = [
        br.BeliefRegressionEntry(
            check_id="c",
            kind="evidence_fidelity",
            description="",
            params={"min_overlap": 0.10},
        )
    ]
    cand = {"proposed_content": "Routing is lane-first then provider."}
    texts = {"daily/x.md": "the system routing prefers lane-first provider selection"}
    summary = br.evaluate_belief_regression(cand, texts, corpus)
    assert summary.failed == []


def test_floor_summary_counts_and_to_dict_serializable():
    corpus = _seed_corpus()
    cand = {"proposed_content": "I reviewed the file confirming gluon confinement physics."}
    texts = {"daily/x.md": ""}  # empty -> no_unread fails; fidelity also fails (no overlap)
    summary = br.evaluate_belief_regression(cand, texts, corpus)
    assert summary.total == 2
    assert summary.passed + len(summary.failed) == 2
    # to_dict round-trips (veto.py reads f.to_dict()); JSON-serializable
    payload = summary.to_dict()
    json.dumps(payload)
    assert payload["failed"][0]["reason"]
    assert "entry" in payload["failed"][0]
    assert "observed" in payload["failed"][0]


def test_floor_prediction_kind_holds_candidate_to_its_own_claim():
    # N1 — the candidate's own prediction as an extra entry
    corpus = [
        br.BeliefRegressionEntry(
            check_id="candidate-prediction",
            kind="prediction",
            description="",
            params={"prediction": "the logs show a Stripe checkout failure", "min_overlap": 0.2},
        )
    ]
    cand = {"proposed_content": "Routing is lane-first."}
    texts = {"daily/x.md": "the system routes by lane first then provider"}  # no Stripe/checkout
    summary = br.evaluate_belief_regression(cand, texts, corpus)
    assert any(f.reason == "prediction_not_met_by_cited_evidence" for f in summary.failed)


def test_floor_unknown_kind_is_skipped_not_failed():
    corpus = [
        br.BeliefRegressionEntry(check_id="c", kind="nonexistent_kind", description="", params={})
    ]
    summary = br.evaluate_belief_regression({"proposed_content": "x"}, {}, corpus)
    assert summary.failed == []
    assert summary.passed == 1


def test_seed_corpus_loads_from_disk():
    corpus = br.load_belief_regression_corpus()
    kinds = {e.kind for e in corpus}
    assert "no_unread_claim" in kinds
    assert "evidence_fidelity" in kinds


# ===========================================================================
# Category 3 — the floor is --force-proof via the UNCHANGED evaluate_veto
# ===========================================================================


def test_floor_failure_is_force_proof_via_evaluate_veto():
    from evolve.compare import ReportDelta
    from evolve.veto import DEFAULT_VETO_RULESET, ExitCode, compute_exit_code, evaluate_veto

    # A clean recall delta (no rule failures) ...
    delta = ReportDelta(
        baseline_experiment_id="b",
        candidate_experiment_id="c",
        hit_rate_delta=0.0,
        avg_top_score_delta=0.0,
        p50_latency_delta_ms=0.0,
        p90_latency_delta_ms=0.0,
        tier_distribution_delta={},
        verdict_counts={},
        per_query=[],
        error_count_delta=0,
    )
    # ... plus a belief-regression summary with ONE failure ...
    entry = br.BeliefRegressionEntry(
        check_id="c", kind="no_unread_claim", description="", params={}
    )
    summary = br.BeliefRegressionSummary(
        total=1,
        passed=0,
        failed=[br.BeliefRegressionFailure(entry, reason="x", observed="y")],
    )
    verdict = evaluate_veto(delta, DEFAULT_VETO_RULESET, regression_summary=summary)
    # the belief summary plugs into the recall veto UNCHANGED -> not accepted...
    assert verdict.accepted is False
    # ...and --force cannot adopt it (regression failures are never softenable).
    assert compute_exit_code(verdict, force=True) != ExitCode.ADOPT
    # to_dict round-trips a belief failure through VetoVerdict.to_dict (m2)
    json.dumps(verdict.to_dict())


# ===========================================================================
# Category 4 — the evidence-READ gate + M4 SECURITY
# ===========================================================================


def test_gate_verifies_supporting_evidence(tmp_path):
    s = config.get_belief_evolve_settings()
    reader = _dict_reader({"daily/x.md": "the system routes by lane first then provider"})
    prop = am.AmendmentProposal(
        target_file="SELF.md",
        proposed_content="Routing is lane-first then provider.",
        evidence_paths=["daily/x.md"],
        confidence_score=0.9,
    )
    # make daily/x.md exist under tmp memory_dir so confinement passes
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "x.md").write_text(
        "the system routes by lane first then provider", encoding="utf-8"
    )
    ok, reason = eg.verify_evidence_support(
        prop, tmp_path, settings=s, read_text=reader, corpus=_seed_corpus()
    )
    assert ok is True
    assert reason == "evidence_verified"


def test_gate_rejects_empty_evidence(tmp_path):
    s = config.get_belief_evolve_settings()
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "x.md").write_text("", encoding="utf-8")
    prop = am.AmendmentProposal(
        target_file="SELF.md",
        proposed_content="Routing is lane-first.",
        evidence_paths=["daily/x.md"],
        confidence_score=0.9,
    )
    ok, reason = eg.verify_evidence_support(prop, tmp_path, settings=s, corpus=_seed_corpus())
    assert ok is False  # empty file -> non-supporting -> too few paths


def test_gate_rejects_zero_overlap(tmp_path):
    s = config.get_belief_evolve_settings()
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "x.md").write_text(
        "the operator prefers concise replies about routing", encoding="utf-8"
    )
    prop = am.AmendmentProposal(
        target_file="SELF.md",
        proposed_content="Quantum chromodynamics governs gluon confinement.",
        evidence_paths=["daily/x.md"],
        confidence_score=0.9,
    )
    ok, reason = eg.verify_evidence_support(prop, tmp_path, settings=s, corpus=_seed_corpus())
    assert ok is False


def test_gate_min_supporting_paths_two_with_one_nonempty(tmp_path):
    s = config.get_belief_evolve_settings(min_supporting_paths=2)
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "a.md").write_text(
        "lane-first routing provider selection", encoding="utf-8"
    )
    (tmp_path / "daily" / "b.md").write_text("", encoding="utf-8")  # empty
    prop = am.AmendmentProposal(
        target_file="SELF.md",
        proposed_content="Routing is lane-first provider selection.",
        evidence_paths=["daily/a.md", "daily/b.md"],
        confidence_score=0.9,
    )
    ok, reason = eg.verify_evidence_support(prop, tmp_path, settings=s, corpus=_seed_corpus())
    assert ok is False  # only 1 non-empty cited path, need 2


def test_gate_raising_reader_fails_open_visible(tmp_path, capsys):
    s = config.get_belief_evolve_settings()

    def _boom(_path):
        raise OSError("disk gone")

    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "x.md").write_text("lane first provider", encoding="utf-8")
    prop = am.AmendmentProposal(
        target_file="SELF.md",
        proposed_content="Routing is lane-first.",
        evidence_paths=["daily/x.md"],
        confidence_score=0.9,
    )
    ok, reason = eg.verify_evidence_support(prop, tmp_path, settings=s, read_text=_boom)
    assert ok is False  # conservative
    out = capsys.readouterr().out
    assert "[evolve.gate]" in out  # N2 — visible print


def test_gate_m4_traversal_rejected_never_read(tmp_path, capsys):
    """M4 — a ../traversal path resolving OUTSIDE the roots is non-supporting and
    the confined target is NEVER read."""
    s = config.get_belief_evolve_settings()
    # an outside secret the traversal would target
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("SECRET lane first provider routing tokens", encoding="utf-8")
    spy: list[str] = []
    reader = _dict_reader({"outside_secret.txt": "SECRET lane first provider"}, spy=spy)
    prop = am.AmendmentProposal(
        target_file="SELF.md",
        proposed_content="Routing is lane-first provider.",
        evidence_paths=["../outside_secret.txt", "../../outside_secret.txt"],
        confidence_score=0.9,
    )
    ok, reason = eg.verify_evidence_support(
        prop, tmp_path, settings=s, read_text=reader, corpus=_seed_corpus()
    )
    assert ok is False
    # the outside target was NEVER read (the dict reader/spy never saw it resolved)
    assert not any("outside_secret" in p for p in spy)


def test_gate_m4_absolute_system_path_rejected(tmp_path):
    """M4 — an absolute system path is non-supporting and never read."""
    s = config.get_belief_evolve_settings()
    abs_path = (
        "C:\\Windows\\System32\\drivers\\etc\\hosts" if sys.platform == "win32" else "/etc/passwd"
    )
    spy: list[str] = []
    reader = _dict_reader({"hosts": "x", "passwd": "x"}, spy=spy)
    prop = am.AmendmentProposal(
        target_file="SELF.md",
        proposed_content="Routing is lane-first.",
        evidence_paths=[abs_path],
        confidence_score=0.9,
    )
    ok, reason = eg.verify_evidence_support(
        prop, tmp_path, settings=s, read_text=reader, corpus=_seed_corpus()
    )
    assert ok is False
    assert spy == []  # never read


def test_gate_m4_oversized_bounded(tmp_path):
    """M4 — a real in-tree file with st_size > max_bytes is non-supporting (no read)."""
    s = config.get_belief_evolve_settings(max_bytes=64)
    (tmp_path / "daily").mkdir()
    big = tmp_path / "daily" / "big.md"
    big.write_text("lane first provider " * 100, encoding="utf-8")  # > 64 bytes
    prop = am.AmendmentProposal(
        target_file="SELF.md",
        proposed_content="Routing is lane-first provider.",
        evidence_paths=["daily/big.md"],
        confidence_score=0.9,
    )
    ok, reason = eg.verify_evidence_support(prop, tmp_path, settings=s, corpus=_seed_corpus())
    assert ok is False  # oversized -> non-supporting -> too few paths


def test_gate_m4_read_capped_to_max_bytes(tmp_path):
    """M4 — even an in-range file is read at most max_bytes; the injected reader
    return is also capped (the fake reader bypasses stat)."""
    s = config.get_belief_evolve_settings(max_bytes=20)
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "x.md").write_text("x", encoding="utf-8")  # tiny on disk
    # the reader returns a long string; the gate must cap it to 20 bytes BEFORE
    # the overlap floor sees it. Put the only overlapping token PAST byte 20.
    long_text = "aaaaaaaaaaaaaaaaaaaa routingprovider"  # token only after the cap
    reader = _dict_reader({"x.md": long_text})
    texts = eg.read_evidence_texts(
        prop_holder(["daily/x.md"]), tmp_path, settings=s, read_text=reader
    )
    # the captured text is bounded to <= max_bytes (after whitespace-collapse +
    # the read cap); the past-cap token is gone
    assert all(len(t) <= 20 for t in texts.values())


def prop_holder(paths):
    return SimpleNamespace(evidence_paths=paths, proposed_content="", summary="")


def test_gate_m4_missing_confined_path_fails(tmp_path):
    """M4 — a confined path that does NOT exist is non-supporting (not a silent OK)."""
    s = config.get_belief_evolve_settings()
    prop = am.AmendmentProposal(
        target_file="SELF.md",
        proposed_content="Routing is lane-first.",
        evidence_paths=["daily/does_not_exist.md"],
        confidence_score=0.9,
    )
    ok, reason = eg.verify_evidence_support(prop, tmp_path, settings=s, corpus=_seed_corpus())
    assert ok is False


@pytest.mark.skipif(
    sys.platform != "win32" and not hasattr(Path, "symlink_to"), reason="no symlink"
)
def test_gate_m4_symlink_escape_rejected(tmp_path):
    """M4 — a symlink INSIDE the vault pointing OUT is caught by resolve-FIRST."""
    s = config.get_belief_evolve_settings()
    outside = tmp_path.parent / "escape_target.txt"
    outside.write_text("lane first provider routing", encoding="utf-8")
    (tmp_path / "daily").mkdir()
    link = tmp_path / "daily" / "link.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not permitted on this platform/run")
    prop = am.AmendmentProposal(
        target_file="SELF.md",
        proposed_content="Routing is lane-first provider.",
        evidence_paths=["daily/link.md"],
        confidence_score=0.9,
    )
    ok, reason = eg.verify_evidence_support(prop, tmp_path, settings=s, corpus=_seed_corpus())
    assert ok is False  # resolve-first follows the symlink to the outside target


# ===========================================================================
# Category 5 — the additive amendment seam (PARITY off, REJECT on)
# ===========================================================================


def _ledger(tmp_path) -> am.ProposalLedger:
    return am.ProposalLedger(tmp_path / "ledger.jsonl")


def test_seam_without_qualification_stays_pending(tmp_path):
    """An ordinary automatic caller cannot publish a high-confidence belief."""
    led = _ledger(tmp_path)
    (tmp_path / "SELF.md").write_text("# SELF\n", encoding="utf-8")
    prop = am.AmendmentProposal(
        source="reflection",
        target_file="SELF.md",
        proposed_content="A valid durable belief about routing.",
        evidence_paths=["daily/x.md"],
        confidence_score=0.9,
    )
    led.append(prop)
    result = am.apply_amendment_if_allowed(prop, led, tmp_path, policy=am.AmendmentPolicy())
    assert result.status == "pending"
    assert result.policy_decision == "defer"
    assert result.policy_reason == "automatic_evaluation_required"
    assert (tmp_path / "SELF.md").read_text(encoding="utf-8") == "# SELF\n"
    assert not (tmp_path / "rollback").exists()


def test_seam_on_reject_blocks_even_at_high_confidence(tmp_path):
    """A failing evidence_check -> policy_rejected, SELF.md UNCHANGED, even at 0.99."""
    led = _ledger(tmp_path)
    (tmp_path / "SELF.md").write_text("# SELF\n", encoding="utf-8")
    before = (tmp_path / "SELF.md").read_text(encoding="utf-8")
    prop = am.AmendmentProposal(
        source="reflection",
        target_file="SELF.md",
        proposed_content="An asserted belief with bad evidence.",
        evidence_paths=["daily/missing.md"],
        confidence_score=0.99,
    )
    led.append(prop)
    assert led.mark_reviewed(prop.id, status="approved", reviewer="test-operator")
    policy = am.AmendmentPolicy(evidence_check=lambda p, m: (False, "evidence_unsupported"))
    result = am.apply_amendment_if_allowed(prop, led, tmp_path, policy=policy)
    assert result.status == "policy_rejected"
    assert result.policy_reason == "evidence_unsupported"
    assert (tmp_path / "SELF.md").read_text(encoding="utf-8") == before  # UNCHANGED
    # the ledger row reflects the rejection
    rows = led.read_all()
    assert rows[0].status == "policy_rejected"


def test_seam_on_pass_falls_through_to_unchanged_gate(tmp_path):
    """evidence_check=(True,...) -> the UNCHANGED policy gate still rejects a
    low-confidence proposal (the seam does not bypass existing checks)."""
    led = _ledger(tmp_path)
    (tmp_path / "SELF.md").write_text("# SELF\n", encoding="utf-8")
    prop = am.AmendmentProposal(
        source="reflection",
        target_file="SELF.md",
        proposed_content="A low-confidence belief.",
        evidence_paths=["daily/x.md"],
        confidence_score=0.10,  # below the 0.75 gate
    )
    led.append(prop)
    assert led.mark_reviewed(prop.id, status="approved", reviewer="test-operator")
    policy = am.AmendmentPolicy(evidence_check=lambda p, m: (True, "ok"))
    result = am.apply_amendment_if_allowed(prop, led, tmp_path, policy=policy)
    assert result.status == "policy_rejected"
    assert result.policy_reason == "low_confidence"  # the UNCHANGED gate fired


# ===========================================================================
# Category 6 — the scheduled LLM judge (independent prompt + fail-open + M5)
# ===========================================================================


def _fake_reasoning(parsed, *, captured: dict | None = None):
    async def _r(context, instruction, output_schema=None, cwd=None):
        if captured is not None:
            captured["context"] = context
            captured["instruction"] = instruction
        return SimpleNamespace(parsed=parsed, model="fake", cost_usd=0.0)

    return _r


def test_judge_returns_object_verdict():
    verdict = asyncio.run(
        jd.judge_belief_candidate(
            _candidate(),
            {"daily/x.md": "lane first provider"},
            cwd=Path.cwd(),
            reasoning=_fake_reasoning(
                {"supported": True, "correctness": 0.8, "evidence_fidelity": 0.7, "reason": "ok"}
            ),
        )
    )
    assert verdict["supported"] is True
    assert verdict["correctness"] == 0.8
    assert verdict["evidence_fidelity"] == 0.7


def test_judge_unwraps_single_key_wrap():
    verdict = asyncio.run(
        jd.judge_belief_candidate(
            _candidate(),
            {"daily/x.md": "lane first"},
            cwd=Path.cwd(),
            reasoning=_fake_reasoning(
                {"verdict": {"supported": True, "correctness": 0.9, "evidence_fidelity": 0.8}}
            ),
        )
    )
    assert verdict["supported"] is True
    assert verdict["correctness"] == 0.9


def test_judge_m5_list_result_raises_output_error_without_semantic_rejection():
    from personas.learning.errors import LearningOutputError

    with pytest.raises(LearningOutputError):
        asyncio.run(
            jd.judge_belief_candidate(
                _candidate(),
                {"daily/x.md": "lane first"},
                cwd=Path.cwd(),
                reasoning=_fake_reasoning([{"supported": True}]),
            )
        )


def test_judge_provider_outage_is_typed_deferral_not_negative_evidence(capsys):
    from personas.learning.errors import LearningUnavailableError

    async def _boom(context, instruction, output_schema=None, cwd=None):
        raise RuntimeError("provider down")

    with pytest.raises(LearningUnavailableError, match="belief_judge_unavailable"):
        asyncio.run(
            jd.judge_belief_candidate(
                _candidate(), {"daily/x.md": "lane first"}, cwd=Path.cwd(), reasoning=_boom
            )
        )
    out = capsys.readouterr().out
    assert "[evolve.judge] judge failed" in out


def test_judge_circularity_guard_prompt_excludes_producing_context():
    captured: dict = {}
    asyncio.run(
        jd.judge_belief_candidate(
            _candidate(proposed_content="The Homie routes by lane first."),
            {"daily/x.md": "EVIDENCE_BODY lane first provider"},
            cwd=Path.cwd(),
            reasoning=_fake_reasoning(
                {"supported": True, "correctness": 0.8, "evidence_fidelity": 0.8},
                captured=captured,
            ),
        )
    )
    blob = captured["context"] + captured["instruction"]
    assert "lane first" in blob  # the candidate claim IS present
    assert "EVIDENCE_BODY" in blob  # the read evidence IS present
    # but the PRODUCING reflection context is NOT
    assert "PRODUCING_REFLECTION_CONTEXT" not in blob


def test_judge_empty_evidence_skips_llm():
    # no evidence -> conservative not-supported WITHOUT an LLM call (reasoning that
    # would raise is never invoked)
    async def _must_not_call(*a, **k):
        raise AssertionError("LLM should not be called with empty evidence")

    verdict = asyncio.run(
        jd.judge_belief_candidate(_candidate(), {}, cwd=Path.cwd(), reasoning=_must_not_call)
    )
    assert verdict["supported"] is False
    assert verdict["reason"] == "no_evidence"


def test_judge_disabled_kill_switch(monkeypatch):
    monkeypatch.setenv("EVOLVE_ENABLED", "false")

    async def _must_not_call(*a, **k):
        raise AssertionError("LLM should not be called when disabled")

    verdict = asyncio.run(
        jd.judge_belief_candidate(
            _candidate(), {"daily/x.md": "x"}, cwd=Path.cwd(), reasoning=_must_not_call
        )
    )
    assert verdict["supported"] is False
    assert verdict["reason"] == "evolve_disabled"


# ===========================================================================
# Category 7 — the orchestrator propose-belief (adopt vs reject, B1, B2, N1)
# ===========================================================================


def _supporting_memory(tmp_path) -> Path:
    (tmp_path / "daily").mkdir(exist_ok=True)
    (tmp_path / "daily" / "x.md").write_text(
        "the system routes tasks by lane first then provider selection", encoding="utf-8"
    )
    (tmp_path / "SELF.md").write_text("# SELF\n", encoding="utf-8")
    return tmp_path


def _learning_service(tmp_path, mem):
    from personas.learning.models import LearningTarget
    from personas.learning.service import LearningService

    return LearningService(
        LearningTarget("demo", mem, tmp_path / "data", tmp_path / "state", tmp_path / "skills")
    )


async def _no_inline_reasoning(*args, **kwargs):
    raise AssertionError("Admission must never run the former support-only judge")


def test_propose_belief_dryrun_is_read_only_pending(tmp_path, monkeypatch):
    mem = _supporting_memory(tmp_path)
    decisions = tmp_path / "decisions"
    monkeypatch.setattr(config, "BELIEF_EVOLVE_DECISION_DIR", decisions)
    service = _learning_service(tmp_path, mem)
    before = (mem / "SELF.md").read_bytes()
    result = asyncio.run(
        el.propose_belief(
            _candidate(),
            dry_run=True,
            memory_dir=mem,
            service=service,
            reasoning=_no_inline_reasoning,
        )
    )
    assert result["outcome"] == "pending"
    assert result["reason"] == "shared_evaluation_required"
    assert result["adopt"] is result["supported"] is False
    assert result["attempts"] == 0
    assert (mem / "SELF.md").read_bytes() == before
    assert not service.target.data_dir.exists()
    assert not decisions.exists()


@pytest.mark.parametrize("path", ["daily/x.md", "daily/missing.md", "../../.env"])
def test_propose_belief_paths_are_unverified_provenance_not_support(tmp_path, path):
    mem = _supporting_memory(tmp_path)
    service = _learning_service(tmp_path, mem)
    before = (mem / "SELF.md").read_bytes()
    result = asyncio.run(
        el.propose_belief(
            _candidate(evidence_paths=[path]),
            dry_run=False,
            memory_dir=mem,
            service=service,
            reasoning=_no_inline_reasoning,
        )
    )
    assert result["outcome"] == "pending"
    assert result["reason"] == "missing_supporting_evidence"
    assert result["candidate_id"] is None
    assert result["adopt"] is result["evidence_ok"] is False
    change = service.get_record(result["change_proposal_id"])
    assert change["evidence_ids"] == []
    assert change["source_manifest"] == [
        {"ref": path, "kind": "legacy_citation", "unverified": True}
    ]
    assert service.store.all("experience") == []
    assert service.store.all("evaluation") == []
    assert service.store.all("activation") == []
    assert (mem / "SELF.md").read_bytes() == before


def test_propose_belief_original_evidence_routes_to_shared_candidate(tmp_path):
    from personas.learning.queue import LearningQueue

    mem = _supporting_memory(tmp_path)
    service = _learning_service(tmp_path, mem)
    experience = service.capture_experience("origin", "test", "Observed lane selection")
    execution = service.record_execution(experience["id"], {"success": True}, attempt_key="one")
    candidate = _candidate()
    candidate["evidence_ids"] = [experience["id"], execution["id"]]
    before = (mem / "SELF.md").read_bytes()
    result = asyncio.run(
        el.propose_belief(
            candidate,
            dry_run=False,
            memory_dir=mem,
            service=service,
            reasoning=_no_inline_reasoning,
        )
    )
    assert result["shared_queue"] is True and result["adopt"] is False
    actual = service.get_record(result["candidate_id"])
    assert set(actual["evidence_ids"]) == {experience["id"], execution["id"]}
    assert any(
        job["payload"].get("candidate_id") == actual["id"] for job in LearningQueue(service).list()
    )
    assert not service.store.all("evaluation")
    assert not service.store.all("activation")
    assert (mem / "SELF.md").read_bytes() == before


def test_propose_belief_duplicate_uuid_windows_coalesce(tmp_path):
    mem = _supporting_memory(tmp_path)
    service = _learning_service(tmp_path, mem)
    candidate = _candidate()
    first = asyncio.run(
        el.propose_belief(
            {**candidate, "id": "producer-one"},
            dry_run=False,
            memory_dir=mem,
            service=service,
        )
    )
    second = asyncio.run(
        el.propose_belief(
            {**candidate, "id": "producer-two"},
            dry_run=False,
            memory_dir=mem,
            service=service,
        )
    )
    assert first["change_proposal_id"] == second["change_proposal_id"]
    assert len(service.store.all("change_proposal")) == 1
    assert service.store.all("experience") == []


def test_propose_belief_rejects_cross_profile_target_before_admission(tmp_path):
    from personas.learning.models import LearningError

    mem = _supporting_memory(tmp_path)
    service = _learning_service(tmp_path, mem)
    foreign = tmp_path / "foreign-vault"
    foreign.mkdir()
    with pytest.raises(LearningError, match="belief_target_does_not_match_persona"):
        asyncio.run(
            el.propose_belief(
                _candidate(),
                dry_run=False,
                memory_dir=foreign,
                service=service,
            )
        )
    assert not service.target.data_dir.exists()


def test_propose_belief_unavailable_admission_is_typed_and_retryable(tmp_path, monkeypatch):
    from personas.learning import authority
    from personas.learning.errors import LearningUnavailableError

    mem = _supporting_memory(tmp_path)
    service = _learning_service(tmp_path, mem)
    before = (mem / "SELF.md").read_bytes()

    def unavailable(*args, **kwargs):
        raise OSError("temporary storage unavailable")

    monkeypatch.setattr(authority, "submit_proposal", unavailable)
    with pytest.raises(LearningUnavailableError, match="belief_admission_unavailable"):
        asyncio.run(
            el.propose_belief(
                _candidate(),
                dry_run=False,
                memory_dir=mem,
                service=service,
            )
        )
    assert not service.store.all("change_proposal")
    assert (mem / "SELF.md").read_bytes() == before


def test_propose_belief_disabled_stays_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOLVE_ENABLED", "false")
    mem = _supporting_memory(tmp_path)
    service = _learning_service(tmp_path, mem)
    result = asyncio.run(
        el.propose_belief(
            _candidate(),
            dry_run=False,
            memory_dir=mem,
            service=service,
            reasoning=_no_inline_reasoning,
        )
    )
    assert result["outcome"] == "disabled" and result["adopt"] is False
    assert not service.target.data_dir.exists()


@pytest.mark.parametrize("missing", ["evidence_paths", "proposed_content", "summary"])
def test_propose_belief_missing_required_field_rejects_without_state(tmp_path, missing):
    mem = _supporting_memory(tmp_path)
    service = _learning_service(tmp_path, mem)
    candidate = _candidate()
    candidate.pop(missing)
    result = asyncio.run(
        el.propose_belief(
            candidate,
            dry_run=False,
            memory_dir=mem,
            service=service,
            reasoning=_no_inline_reasoning,
        )
    )
    assert result["outcome"] == "reject"
    assert result["reason"] == "malformed_candidate"
    assert not service.target.data_dir.exists()


@pytest.mark.parametrize("prediction", [None, "Future replies will use the correct lane."])
def test_propose_belief_extra_prediction_never_bypasses_shared_evaluation(tmp_path, prediction):
    mem = _supporting_memory(tmp_path)
    service = _learning_service(tmp_path, mem)
    result = asyncio.run(
        el.propose_belief(
            _candidate(prediction=prediction),
            dry_run=False,
            memory_dir=mem,
            service=service,
            reasoning=_no_inline_reasoning,
        )
    )
    assert result["outcome"] == "pending" and result["adopt"] is False
    assert not service.store.all("evaluation")


def test_f3_env_outside_vault_rejected_never_read_never_in_judge_feed(tmp_path, monkeypatch):
    """F3 — a candidate citing ``.claude/scripts/.env`` (a repo path OUTSIDE the
    vault ``memory_dir``) is REJECTED at confinement, the secret file is NEVER
    read, and its bytes NEVER reach the judge feed.

    Pre-fix: confinement allowed ``memory_dir`` OR ``PROJECT_ROOT`` -> the in-repo
    ``.env`` confined under PROJECT_ROOT, was read, and was fed (up to 512 KiB) into
    the LLM judge prompt (PROBE10). The vault-only confinement closes it.
    """
    mem = tmp_path / "vault"  # the memory_dir (vault) — does NOT contain .env
    mem.mkdir()
    s = config.get_belief_evolve_settings()
    spy: list[str] = []
    # the reader would return a fake secret IF the gate ever resolved+read .env
    reader = _dict_reader(
        {".env": "OWNER_NAME=YourUser\nTELEGRAM_BOT_TOKEN=SECRET-lane-first-provider"},
        spy=spy,
    )
    prop = am.AmendmentProposal(
        target_file="SELF.md",
        proposed_content="Routing is lane-first provider.",
        evidence_paths=[".claude/scripts/.env"],
        confidence_score=0.9,
    )
    # (1) the gate rejects it (no confined+existing+non-empty supporting path)
    ok, reason = eg.verify_evidence_support(
        prop, mem, settings=s, read_text=reader, corpus=_seed_corpus()
    )
    assert ok is False
    # (2) the .env was NEVER read (the confinement rejected it BEFORE read_text)
    assert not any(".env" in p for p in spy)
    # (3) the judge feed (read_evidence_texts — the SAME resolver) contains NOTHING
    feed = eg.read_evidence_texts(prop, mem, settings=s, read_text=reader)
    assert feed == {}  # the .env path produced no judge-visible bytes
    assert not any("SECRET" in t for t in feed.values())


def test_f3_supporting_vault_file_still_passes_control(tmp_path):
    """F3 control — the legitimate case still works: a supporting file UNDER the
    vault ``memory_dir`` confines, reads, and supports (the fix did not break the
    documented vault-evidence path)."""
    mem = tmp_path / "vault"
    mem.mkdir()
    (mem / "daily").mkdir()
    (mem / "daily" / "x.md").write_text(
        "the system routes tasks by lane first then provider", encoding="utf-8"
    )
    s = config.get_belief_evolve_settings()
    prop = am.AmendmentProposal(
        target_file="SELF.md",
        proposed_content="Routing is lane-first then provider.",
        evidence_paths=["daily/x.md"],
        confidence_score=0.9,
    )
    ok, reason = eg.verify_evidence_support(prop, mem, settings=s, corpus=_seed_corpus())
    assert ok is True
    assert reason == "evidence_verified"


# ===========================================================================
# Category 8 — propose (recall safe-first) writes a decision artifact
# ===========================================================================


def test_propose_recall_writes_artifact_no_identity_mutation(tmp_path, monkeypatch):
    """The no-op-safe wake-the-loop proof — an injected fake replay so the
    embedding model is not needed. propose writes a decision artifact via
    write_decision_artifact and mutates NO identity file."""
    from evolve.goldens import load_regression_queries
    from evolve.models import ReplayQueryResult, ReplayReport, ReplaySummary

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    # how many regression queries exist -> build that many aligned per-query results
    raw = load_regression_queries()
    n = len(raw)

    def _per_query(i):
        # match the recorded expected_top_path + min_top_score so the regression
        # corpus passes (we are not testing veto failure here, just the wiring)
        entry = raw[i]
        return ReplayQueryResult(
            query=entry["query"],
            tier="TIER_1",
            results_count=1,
            result_paths=[entry["expected_top_path"]],
            top_scores=[max(0.99, float(entry["min_top_score"]))],
            latency_ms=1.0,
        )

    async def _fake_replay(queries, overrides, memory_dir, **kw):
        per_query = [_per_query(i) for i in range(n)]
        return ReplayReport(
            experiment_id=kw.get("experiment_id", "exp"),
            timestamp_utc="2026-06-13T00:00:00+00:00",
            overrides=dict(overrides or {}),
            config_snapshot={},
            per_query=per_query,
            summary=ReplaySummary(),
        )

    exit_code = asyncio.run(
        el.propose(dry_run=True, memory_dir=tmp_path, run_replay_fn=_fake_replay)
    )
    # a decision-<id>.json was written under the recall reports dir
    reports = list((tmp_path / "evolve" / "reports").glob("decision-*.json"))
    assert len(reports) == 1
    assert isinstance(exit_code, int)


# ===========================================================================
# Category 9 — the crux re-test (program acceptance — persist-only-if-earned)
# ===========================================================================


def test_manual_review_keeps_existing_evidence_and_static_gates(tmp_path):
    mem = _supporting_memory(tmp_path)
    ledger = _ledger(tmp_path)
    proposal = am.AmendmentProposal(
        source="reflection",
        target_file="SELF.md",
        proposed_content="The Homie routes tasks by lane first then provider.",
        evidence_paths=["daily/x.md"],
        confidence_score=0.9,
    )
    ledger.append(proposal)
    pending = am.apply_amendment_if_allowed(proposal, ledger, mem)
    assert pending.status == "pending"
    assert pending.policy_reason == "automatic_evaluation_required"
    assert ledger.mark_reviewed(proposal.id, status="approved", reviewer="test-operator")
    policy = am.AmendmentPolicy(
        evidence_check=lambda item, root: eg.verify_evidence_support(
            item, root, settings=config.get_belief_evolve_settings(), corpus=_seed_corpus()
        )
    )
    applied = am.apply_amendment_if_allowed(proposal, ledger, mem, policy=policy)
    assert applied.status == "applied"
    assert "lane first" in (mem / "SELF.md").read_text(encoding="utf-8")
    assert ledger.read_all()[0].status == "applied"
    assert (mem / "rollback").exists()


# ===========================================================================
# Category 10 — nightly dream-cycle autonomy (#170): retry budget, new knobs,
# candidate extraction, and the retryable-decision reload queue.
# ===========================================================================


def test_belief_evolve_settings_new_knobs_default(monkeypatch):
    for var in (
        "BELIEF_MAX_ATTEMPTS",
        "BELIEF_MAX_ADOPTIONS_PER_NIGHT",
        "BELIEF_MAX_CANDIDATES_PER_NIGHT",
        "BELIEF_CANDIDATE_MIN_CONFIDENCE",
    ):
        monkeypatch.delenv(var, raising=False)
    s = config.get_belief_evolve_settings()
    assert s.max_attempts == 3
    assert s.max_adoptions_per_night == 2
    assert s.max_candidates_per_night == 3
    assert s.candidate_min_confidence == 0.75


def test_belief_evolve_settings_new_knobs_env_override(monkeypatch):
    monkeypatch.setenv("BELIEF_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("BELIEF_MAX_ADOPTIONS_PER_NIGHT", "1")
    monkeypatch.setenv("BELIEF_MAX_CANDIDATES_PER_NIGHT", "7")
    monkeypatch.setenv("BELIEF_CANDIDATE_MIN_CONFIDENCE", "0.9")
    s = config.get_belief_evolve_settings()  # no module reload (Rule 1)
    assert s.max_attempts == 5
    assert s.max_adoptions_per_night == 1
    assert s.max_candidates_per_night == 7
    assert s.candidate_min_confidence == 0.9


@pytest.mark.parametrize("attempts", [0, 2, 100])
@pytest.mark.parametrize("dry_run", [True, False])
def test_compatibility_admission_never_exhausts_provider_retry_budget(tmp_path, attempts, dry_run):
    mem = _supporting_memory(tmp_path)
    service = _learning_service(tmp_path, mem)
    result = asyncio.run(
        el.propose_belief(
            _candidate(),
            dry_run=dry_run,
            memory_dir=mem,
            service=service,
            reasoning=_no_inline_reasoning,
            attempts=attempts,
        )
    )
    assert result["outcome"] == "pending" and result["retryable"] is True
    assert result["attempts"] == attempts
    assert result["adopt"] is False
    assert not service.store.all("execution")
    assert not service.store.all("evaluation")


def test_extract_belief_candidates_filters_by_kind():
    belief_block = json.dumps(
        {
            "kind": "belief_candidate",
            "target_file": "SELF.md",
            "summary": "lane-first",
            "evidence_paths": ["daily/x.md"],
            "proposed_content": "y",
            "confidence_score": 0.8,
        }
    )
    text = (
        "Some consolidation prose.\n"
        '{"target_file": "MEMORY.md", "summary": "a routine amendment", "proposed_content": "x"}\n'
        f"{belief_block}\n"
        "More prose. CONSOLIDATION_OK"
    )
    out = el.extract_belief_candidates(text)
    assert len(out) == 1
    assert "kind" not in out[0]
    assert out[0]["target_file"] == "SELF.md"
    assert out[0]["evidence_paths"] == ["daily/x.md"]


def test_extract_belief_candidates_empty_text():
    assert el.extract_belief_candidates("no json here, just CONSOLIDATION_OK") == []


def test_load_retryable_belief_candidates_preserves_id(tmp_path):
    decisions = tmp_path / "decisions"
    decisions.mkdir()
    (decisions / "decision-abc123.json").write_text(
        json.dumps(
            {
                "proposal_id": "abc123",
                "target_file": "SELF.md",
                "candidate": {
                    "summary": "s",
                    "proposed_content": "c",
                    "evidence_paths": ["daily/x.md"],
                    "confidence_score": 0.8,
                },
                "outcome": "error",
                "retryable": True,
                "attempts": 1,
                "max_attempts": 3,
            }
        ),
        encoding="utf-8",
    )
    out, skipped = el.load_retryable_belief_candidates(decision_dir=decisions)
    assert len(out) == 1
    assert skipped == 0
    assert out[0]["id"] == "abc123"  # ORIGINAL id preserved (retry updates same row)
    assert out[0]["target_file"] == "SELF.md"
    assert out[0]["_attempts"] == 1
    assert out[0]["evidence_paths"] == ["daily/x.md"]


def test_load_retryable_belief_candidates_skips_non_retryable(tmp_path):
    decisions = tmp_path / "decisions"
    decisions.mkdir()
    (decisions / "decision-done.json").write_text(
        json.dumps(
            {
                "proposal_id": "done",
                "target_file": "SELF.md",
                "candidate": {"evidence_paths": ["daily/x.md"]},
                "outcome": "reject",
                "retryable": False,
                "attempts": 3,
                "max_attempts": 3,
            }
        ),
        encoding="utf-8",
    )
    assert el.load_retryable_belief_candidates(decision_dir=decisions) == ([], 0)


def test_load_retryable_belief_candidates_skips_corrupted_file(tmp_path):
    """A corrupted decision file must not vanish the retry queue for the rest
    of the batch, and the skip must be counted so it's surfaceable in the
    Phase-5 receipt rather than only visible in stdout."""
    decisions = tmp_path / "decisions"
    decisions.mkdir()
    (decisions / "decision-good.json").write_text(
        json.dumps(
            {
                "proposal_id": "good",
                "target_file": "SELF.md",
                "candidate": {"evidence_paths": ["daily/x.md"]},
                "outcome": "error",
                "retryable": True,
                "attempts": 1,
                "max_attempts": 3,
            }
        ),
        encoding="utf-8",
    )
    (decisions / "decision-corrupt.json").write_text("{not valid json", encoding="utf-8")
    out, skipped = el.load_retryable_belief_candidates(decision_dir=decisions)
    assert len(out) == 1
    assert out[0]["id"] == "good"
    assert skipped == 1


def test_load_retryable_belief_candidates_skips_malformed_shape(tmp_path):
    """Valid JSON but a non-int-castable `attempts` field must be isolated to
    that ONE file, not raise out of the loader and take the whole batch down."""
    decisions = tmp_path / "decisions"
    decisions.mkdir()
    (decisions / "decision-good.json").write_text(
        json.dumps(
            {
                "proposal_id": "good",
                "target_file": "SELF.md",
                "candidate": {"evidence_paths": ["daily/x.md"]},
                "outcome": "error",
                "retryable": True,
                "attempts": 1,
                "max_attempts": 3,
            }
        ),
        encoding="utf-8",
    )
    (decisions / "decision-bad-shape.json").write_text(
        json.dumps(
            {
                "proposal_id": "bad-shape",
                "target_file": "SELF.md",
                "candidate": {"evidence_paths": ["daily/y.md"]},
                "outcome": "error",
                "retryable": True,
                "attempts": "three",
                "max_attempts": 3,
            }
        ),
        encoding="utf-8",
    )
    out, skipped = el.load_retryable_belief_candidates(decision_dir=decisions)
    assert len(out) == 1
    assert out[0]["id"] == "good"
    assert skipped == 1
