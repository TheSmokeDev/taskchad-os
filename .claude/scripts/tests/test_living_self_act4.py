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
  5. The additive amendment seam — PARITY off, REJECT on (FAILS pre-fix: no seam).
  6. The scheduled LLM judge — INDEPENDENT prompt (circularity) + fail-open +
     M5 OBJECT-tolerant parse (NOT _coerce_claim_list) (FAILS pre-fix: no module).
  7. The orchestrator propose-belief — end-to-end fake-runtime, adopt vs reject,
     B1 (ledger flips applied), B2 (extra prediction key), N1 (prediction wired).
  8. propose (recall safe-first) writes a decision artifact via a fake replay.
  9. The crux re-test (program acceptance — persist-only-if-earned -> audited act).
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
    s = config.get_belief_evolve_settings(
        enabled=True, min_supporting_paths=2, min_correctness=0.9
    )
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
    assert not any(
        f.reason == "claims_read_but_evidence_empty_or_missing" for f in summary.failed
    )


def test_floor_no_unread_claim_not_applicable_when_no_read_asserted():
    corpus = [
        br.BeliefRegressionEntry(
            check_id="c", kind="no_unread_claim", description="", params={}
        )
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
        "C:\\Windows\\System32\\drivers\\etc\\hosts"
        if sys.platform == "win32"
        else "/etc/passwd"
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


def test_seam_off_is_parity(tmp_path):
    """evidence_check=None (default) -> apply behaves byte-for-byte as pre-Act-4."""
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
    assert result.status == "applied"
    assert result.policy_decision == "apply"
    # the .bak rollback + the target mutation happened (parity)
    assert "routing" in (tmp_path / "SELF.md").read_text(encoding="utf-8")
    assert (tmp_path / "rollback").exists()


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
    policy = am.AmendmentPolicy(
        evidence_check=lambda p, m: (False, "evidence_unsupported")
    )
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


def test_judge_m5_list_result_does_not_silently_pass(capsys):
    """M5 — a LIST result (the shape _coerce_claim_list would pass) -> {} ->
    conservative not-supported + a VISIBLE parse-failure print."""
    verdict = asyncio.run(
        jd.judge_belief_candidate(
            _candidate(),
            {"daily/x.md": "lane first"},
            cwd=Path.cwd(),
            reasoning=_fake_reasoning([{"supported": True}]),  # a LIST
        )
    )
    assert verdict["supported"] is False  # the list coercer would WRONGLY adopt
    out = capsys.readouterr().out
    assert "[evolve.judge] unparseable verdict" in out


def test_judge_raising_reasoning_fails_open_visible(capsys):
    async def _boom(context, instruction, output_schema=None, cwd=None):
        raise RuntimeError("provider down")

    verdict = asyncio.run(
        jd.judge_belief_candidate(
            _candidate(), {"daily/x.md": "lane first"}, cwd=Path.cwd(), reasoning=_boom
        )
    )
    assert verdict["supported"] is False
    assert verdict["reason"] == "judge_failed"
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


def test_propose_belief_dryrun_writes_artifact_no_mutation(tmp_path, monkeypatch):
    mem = _supporting_memory(tmp_path)
    monkeypatch.setattr(config, "BELIEF_EVOLVE_DECISION_DIR", tmp_path / "decisions")
    before = (mem / "SELF.md").read_text(encoding="utf-8")
    cand = _candidate(
        proposed_content="The Homie routes tasks by lane first then provider.",
        evidence_paths=["daily/x.md"],
    )
    result = asyncio.run(
        el.propose_belief(
            cand,
            dry_run=True,
            memory_dir=mem,
            reasoning=_fake_reasoning(
                {"supported": True, "correctness": 0.8, "evidence_fidelity": 0.8}
            ),
        )
    )
    assert result["adopt"] is True
    assert (mem / "SELF.md").read_text(encoding="utf-8") == before  # dry-run: UNCHANGED
    # the belief-decision artifact was written
    decisions = list((tmp_path / "decisions").glob("decision-*.json"))
    assert len(decisions) == 1
    payload = json.loads(decisions[0].read_text(encoding="utf-8"))
    assert payload["outcome"] == "adopt"


def test_propose_belief_apply_b1_ledger_flips_applied(tmp_path, monkeypatch):
    """B1 — a candidate dict with NO id: the SINGLE ledger row is `applied` (not
    `pending`), proving the proposal was constructed once and the same id reached
    _update_record (a double-construct would leave the row pending)."""
    mem = _supporting_memory(tmp_path)
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(config, "AMENDMENT_LEDGER_FILE", ledger_file)
    monkeypatch.setattr(config, "BELIEF_EVOLVE_DECISION_DIR", tmp_path / "decisions")
    cand = _candidate(
        proposed_content="The Homie routes tasks by lane first then provider.",
        evidence_paths=["daily/x.md"],
    )
    assert "id" not in cand  # Archon-/LLM-proposed candidate has no id
    result = asyncio.run(
        el.propose_belief(
            cand,
            dry_run=False,
            memory_dir=mem,
            reasoning=_fake_reasoning(
                {"supported": True, "correctness": 0.8, "evidence_fidelity": 0.8}
            ),
        )
    )
    assert result["adopt"] is True
    led = am.ProposalLedger(ledger_file)
    rows = led.read_all()
    assert len(rows) == 1
    assert rows[0].status == "applied"  # NOT pending — the B1 break would leave pending
    # SELF.md gained the block
    assert "lane first" in (mem / "SELF.md").read_text(encoding="utf-8").lower()


def test_propose_belief_b2_extra_prediction_key_no_typeerror(tmp_path, monkeypatch):
    """B2 — a candidate carrying an Archon `prediction` key does NOT raise
    TypeError (the _coerce_dataclass field-filter drops it)."""
    mem = _supporting_memory(tmp_path)
    monkeypatch.setattr(config, "BELIEF_EVOLVE_DECISION_DIR", tmp_path / "decisions")
    cand = _candidate(
        proposed_content="The Homie routes tasks by lane first then provider.",
        evidence_paths=["daily/x.md"],
        prediction="the logs show lane-first provider routing",
    )
    # no TypeError raised
    result = asyncio.run(
        el.propose_belief(
            cand,
            dry_run=True,
            memory_dir=mem,
            reasoning=_fake_reasoning(
                {"supported": True, "correctness": 0.8, "evidence_fidelity": 0.8}
            ),
        )
    )
    assert "adopt" in result


def test_propose_belief_n1_prediction_recorded_in_artifact(tmp_path, monkeypatch):
    """N1 — the candidate's prediction is RECORDED in the decision artifact."""
    mem = _supporting_memory(tmp_path)
    monkeypatch.setattr(config, "BELIEF_EVOLVE_DECISION_DIR", tmp_path / "decisions")
    cand = _candidate(
        proposed_content="The Homie routes tasks by lane first then provider.",
        evidence_paths=["daily/x.md"],
        prediction="PREDICTION_SENTINEL routes by lane first provider",
    )
    asyncio.run(
        el.propose_belief(
            cand,
            dry_run=True,
            memory_dir=mem,
            reasoning=_fake_reasoning(
                {"supported": True, "correctness": 0.8, "evidence_fidelity": 0.8}
            ),
        )
    )
    decisions = list((tmp_path / "decisions").glob("decision-*.json"))
    payload = json.loads(decisions[0].read_text(encoding="utf-8"))
    assert payload["candidate"]["prediction"] == "PREDICTION_SENTINEL routes by lane first provider"


def test_propose_belief_n1_prediction_failure_blocks_adopt(tmp_path, monkeypatch):
    """N1 — a prediction the cited evidence does NOT satisfy fails the floor ->
    adopt=False even when the judge says supported."""
    mem = _supporting_memory(tmp_path)
    monkeypatch.setattr(config, "BELIEF_EVOLVE_DECISION_DIR", tmp_path / "decisions")
    cand = _candidate(
        proposed_content="The Homie routes tasks by lane first then provider.",
        evidence_paths=["daily/x.md"],
        prediction="the logs show a Stripe checkout failure and refund",  # not in evidence
    )
    result = asyncio.run(
        el.propose_belief(
            cand,
            dry_run=True,
            memory_dir=mem,
            reasoning=_fake_reasoning(
                {"supported": True, "correctness": 0.9, "evidence_fidelity": 0.9}
            ),
        )
    )
    assert result["adopt"] is False  # the floor (prediction) blocked it
    assert result["evidence_reason"] == "belief_regression_floor"


def test_propose_belief_missing_evidence_rejects(tmp_path, monkeypatch):
    mem = _supporting_memory(tmp_path)
    monkeypatch.setattr(config, "BELIEF_EVOLVE_DECISION_DIR", tmp_path / "decisions")
    cand = _candidate(
        proposed_content="I verified the doc: routing is lane-first.",
        evidence_paths=["daily/does_not_exist.md"],
        confidence_score=0.99,
    )
    result = asyncio.run(
        el.propose_belief(
            cand,
            dry_run=False,
            memory_dir=mem,
            reasoning=_fake_reasoning(
                {"supported": True, "correctness": 0.9, "evidence_fidelity": 0.9}
            ),
        )
    )
    assert result["adopt"] is False  # the floor, regardless of the judge
    assert (mem / "SELF.md").read_text(encoding="utf-8") == "# SELF\n"  # UNCHANGED


def test_propose_belief_judge_says_no_rejects(tmp_path, monkeypatch):
    mem = _supporting_memory(tmp_path)
    monkeypatch.setattr(config, "BELIEF_EVOLVE_DECISION_DIR", tmp_path / "decisions")
    cand = _candidate(
        proposed_content="The Homie routes tasks by lane first then provider.",
        evidence_paths=["daily/x.md"],
    )
    result = asyncio.run(
        el.propose_belief(
            cand,
            dry_run=False,
            memory_dir=mem,
            reasoning=_fake_reasoning(
                {"supported": False, "correctness": 0.2, "evidence_fidelity": 0.1, "reason": "no"}
            ),
        )
    )
    assert result["adopt"] is False  # evidence read OK, judge said no
    assert (mem / "SELF.md").read_text(encoding="utf-8") == "# SELF\n"


def test_propose_belief_disabled_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOLVE_ENABLED", "false")
    monkeypatch.setattr(config, "BELIEF_EVOLVE_DECISION_DIR", tmp_path / "decisions")
    mem = _supporting_memory(tmp_path)
    cand = _candidate(evidence_paths=["daily/x.md"])
    result = asyncio.run(el.propose_belief(cand, dry_run=False, memory_dir=mem))
    assert result["adopt"] is False
    assert result["evidence_reason"] == "evolve_disabled"
    # no artifact written, no mutation
    assert not (tmp_path / "decisions").exists()
    assert (mem / "SELF.md").read_text(encoding="utf-8") == "# SELF\n"


# ===========================================================================
# Post-build fixes — F1 (malformed -> reject, no crash), F2 (artifact reflects
# REALITY not a prediction; apply exception contained), F3 (.env confinement)
# ===========================================================================


def test_f1_malformed_candidate_missing_evidence_paths_rejects_no_crash(
    tmp_path, monkeypatch, capsys
):
    """F1 — a candidate with NO ``evidence_paths`` (-> ``_coerce_dataclass``
    returns ``None`` -> ``_proposal_from`` raises ``ValueError``) must NOT crash:
    it writes a reject artifact (``outcome="reject"``, ``malformed_candidate``),
    prints a distinct line, returns the conservative reject dict, mutates NOTHING.

    Pre-fix: ``propose_belief`` raised the unhandled ``ValueError`` (PROBE5/PROBE9)
    -> the Archon bash node dies on a raw traceback.
    """
    monkeypatch.setattr(config, "BELIEF_EVOLVE_DECISION_DIR", tmp_path / "decisions")
    (tmp_path / "SELF.md").write_text("# SELF\n", encoding="utf-8")
    # a candidate MISSING the required evidence_paths field
    malformed = {
        "source": "reflection",
        "target_file": "SELF.md",
        "summary": "no evidence_paths key at all",
        "proposed_content": "An asserted belief with a missing required field.",
        "confidence_score": 0.9,
    }
    assert "evidence_paths" not in malformed

    # must NOT raise (a reasoning that would raise proves the judge is never reached)
    async def _must_not_call(*a, **k):
        raise AssertionError("judge must not run on a malformed candidate")

    result = asyncio.run(
        el.propose_belief(
            malformed, dry_run=False, memory_dir=tmp_path, reasoning=_must_not_call
        )
    )
    assert result["adopt"] is False
    assert result["evidence_reason"] == "malformed_candidate"
    # a reject artifact was written (NOT a crash)
    decisions = list((tmp_path / "decisions").glob("decision-*.json"))
    assert len(decisions) == 1
    payload = json.loads(decisions[0].read_text(encoding="utf-8"))
    assert payload["outcome"] == "reject"
    assert payload["outcome_reason"] == "malformed_candidate"
    # SELF.md untouched
    assert (tmp_path / "SELF.md").read_text(encoding="utf-8") == "# SELF\n"
    # a distinct visible print
    out = capsys.readouterr().out
    assert "malformed candidate" in out


def test_f2_low_confidence_gate_reject_artifact_says_reject_not_adopt(
    tmp_path, monkeypatch
):
    """F2 — a confidence=0.5 belief: the loop's prediction says adopt (the gate +
    judge pass), but the UNCHANGED apply policy gate REJECTS on ``low_confidence``.
    The artifact MUST say ``outcome="reject"`` with the real ``low_confidence``
    reason, and SELF.md MUST be unchanged.

    Pre-fix: the artifact was written from the pre-gate prediction -> ``"adopt"``
    while the ledger said ``policy_rejected`` and SELF.md was unchanged (PROBE6) —
    a LYING artifact.
    """
    mem = _supporting_memory(tmp_path)
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(config, "AMENDMENT_LEDGER_FILE", ledger_file)
    monkeypatch.setattr(config, "BELIEF_EVOLVE_DECISION_DIR", tmp_path / "decisions")
    before = (mem / "SELF.md").read_text(encoding="utf-8")
    cand = _candidate(
        proposed_content="The Homie routes tasks by lane first then provider.",
        evidence_paths=["daily/x.md"],
        confidence_score=0.5,  # passes the gate (no confidence check) but < 0.75 policy
    )
    result = asyncio.run(
        el.propose_belief(
            cand,
            dry_run=False,
            memory_dir=mem,
            reasoning=_fake_reasoning(
                {"supported": True, "correctness": 0.9, "evidence_fidelity": 0.9}
            ),
        )
    )
    # the REAL outcome is reject — the artifact does NOT lie
    assert result["adopt"] is False
    assert result["outcome"] == "reject"
    assert result["outcome_reason"] == "low_confidence"
    decisions = list((tmp_path / "decisions").glob("decision-*.json"))
    assert len(decisions) == 1
    payload = json.loads(decisions[0].read_text(encoding="utf-8"))
    assert payload["outcome"] == "reject"  # NOT "adopt"
    assert payload["outcome_reason"] == "low_confidence"
    # SELF.md unchanged; the ledger row is policy_rejected (the apply DID run)
    assert (mem / "SELF.md").read_text(encoding="utf-8") == before
    rows = am.ProposalLedger(ledger_file).read_all()
    assert rows[0].status == "policy_rejected"


def test_f2_oversized_content_gate_reject_artifact_says_reject(tmp_path, monkeypatch):
    """F2 — a realistic rich belief > 1200 chars: prediction adopts, the UNCHANGED
    gate rejects on ``content_too_large``, the artifact says ``reject`` (PROBE7)."""
    mem = _supporting_memory(tmp_path)
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(config, "AMENDMENT_LEDGER_FILE", ledger_file)
    monkeypatch.setattr(config, "BELIEF_EVOLVE_DECISION_DIR", tmp_path / "decisions")
    # a long belief whose vocabulary overlaps the evidence (so the floor/judge pass)
    long_belief = (
        "The Homie routes tasks by lane first then provider, observed across "
        "sessions. " * 40
    )
    assert len(long_belief) > 1200
    cand = _candidate(
        proposed_content=long_belief,
        evidence_paths=["daily/x.md"],
        confidence_score=0.9,
    )
    result = asyncio.run(
        el.propose_belief(
            cand,
            dry_run=False,
            memory_dir=mem,
            reasoning=_fake_reasoning(
                {"supported": True, "correctness": 0.9, "evidence_fidelity": 0.9}
            ),
        )
    )
    assert result["adopt"] is False
    assert result["outcome"] == "reject"
    assert result["outcome_reason"] == "content_too_large"
    payload = json.loads(
        next((tmp_path / "decisions").glob("decision-*.json")).read_text(encoding="utf-8")
    )
    assert payload["outcome"] == "reject"
    assert (mem / "SELF.md").read_text(encoding="utf-8") == "# SELF\n"


def test_f2_apply_exception_contained_artifact_says_error(tmp_path, monkeypatch):
    """F2 — an apply-time exception (SELF.md unwritable / locked on win32) must NOT
    crash the loop and must NOT leave a lying ``adopt`` artifact: the loop catches
    it, the artifact says ``outcome="error"`` with the repr, SELF.md is untouched.

    Pre-fix: ``apply_amendment_if_allowed`` at :250 had no try/except -> the loop
    CRASHED with an unhandled exception AND the artifact was already persisted as
    ``"adopt"`` (PROBE8).
    """
    mem = _supporting_memory(tmp_path)
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(config, "AMENDMENT_LEDGER_FILE", ledger_file)
    monkeypatch.setattr(config, "BELIEF_EVOLVE_DECISION_DIR", tmp_path / "decisions")

    def _boom_apply(*a, **k):
        raise OSError("SELF.md is locked (simulated win32 apply failure)")

    # the loop imports apply_amendment_if_allowed from cognition.amendments at call
    # time -> patching the module attribute is seen
    monkeypatch.setattr(am, "apply_amendment_if_allowed", _boom_apply)

    cand = _candidate(
        proposed_content="The Homie routes tasks by lane first then provider.",
        evidence_paths=["daily/x.md"],
        confidence_score=0.9,
    )
    # must NOT raise
    result = asyncio.run(
        el.propose_belief(
            cand,
            dry_run=False,
            memory_dir=mem,
            reasoning=_fake_reasoning(
                {"supported": True, "correctness": 0.9, "evidence_fidelity": 0.9}
            ),
        )
    )
    assert result["adopt"] is False  # NOT a lying True
    assert result["outcome"] == "error"
    assert "locked" in result["outcome_reason"]
    payload = json.loads(
        next((tmp_path / "decisions").glob("decision-*.json")).read_text(encoding="utf-8")
    )
    assert payload["outcome"] == "error"  # NOT "adopt"
    # no partial write — SELF.md untouched
    assert (mem / "SELF.md").read_text(encoding="utf-8") == "# SELF\n"


def test_f2_happy_path_artifact_adopt_matches_applied_ledger_row(tmp_path, monkeypatch):
    """F2 — the happy path STILL adopts: a clean, in-policy belief -> the artifact
    says ``outcome="adopt"`` AND the ledger row is ``applied`` AND SELF.md changed.
    The artifact's ``adopt`` now means the belief ACTUALLY landed (apply-reconciled),
    not merely that the prediction was favourable."""
    mem = _supporting_memory(tmp_path)
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(config, "AMENDMENT_LEDGER_FILE", ledger_file)
    monkeypatch.setattr(config, "BELIEF_EVOLVE_DECISION_DIR", tmp_path / "decisions")
    cand = _candidate(
        proposed_content="The Homie routes tasks by lane first then provider.",
        evidence_paths=["daily/x.md"],
        confidence_score=0.9,
    )
    result = asyncio.run(
        el.propose_belief(
            cand,
            dry_run=False,
            memory_dir=mem,
            reasoning=_fake_reasoning(
                {"supported": True, "correctness": 0.8, "evidence_fidelity": 0.8}
            ),
        )
    )
    assert result["adopt"] is True
    assert result["outcome"] == "adopt"
    payload = json.loads(
        next((tmp_path / "decisions").glob("decision-*.json")).read_text(encoding="utf-8")
    )
    assert payload["outcome"] == "adopt"
    # the artifact's adopt MATCHES the real applied ledger row + SELF.md change
    rows = am.ProposalLedger(ledger_file).read_all()
    assert len(rows) == 1
    assert rows[0].status == "applied"
    assert rows[0].id == payload["proposal_id"]  # artifact + ledger share the id (B1)
    assert "lane first" in (mem / "SELF.md").read_text(encoding="utf-8").lower()


def test_f3_env_outside_vault_rejected_never_read_never_in_judge_feed(
    tmp_path, monkeypatch
):
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


def test_crux_persist_only_if_earned(tmp_path, monkeypatch):
    """The adoption ANCHOR — the single test the whole program exists to pass.

    FORM (Act 1) + HOLD (Act 2) are supplied as FIXTURES (a reflection-source
    candidate + an Act-2 contradicted_by note in the corpus context) — those acts
    own forming/disconfirming. This proves the PERSIST-only-if-EARNED leg LIVE:
      - candidate #1 (evidence SUPPORTS it + beats the floor + judge says yes) ->
        APPLIED (SELF.md gains it, ledger audited).
      - candidate #2 (cites an empty/missing file, the no_unread_claim floor) ->
        REJECTED (policy_rejected, belief_regression_floor), SELF.md UNCHANGED,
        EVEN at confidence_score=0.99.
    """
    mem = tmp_path / "vault"
    mem.mkdir()
    (mem / "daily").mkdir()
    # the REAL non-empty evidence file for candidate #1 (FORM: a reflection belief
    # synthesized from the system's own episodes)
    (mem / "daily" / "episode.md").write_text(
        "the system routes tasks by lane first then provider, observed across sessions",
        encoding="utf-8",
    )
    (mem / "SELF.md").write_text("# SELF\n", encoding="utf-8")
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(config, "AMENDMENT_LEDGER_FILE", ledger_file)
    monkeypatch.setattr(config, "BELIEF_EVOLVE_DECISION_DIR", tmp_path / "decisions")

    # candidate #1 — operator-NOT-given (reflection source), HOLD: carries an
    # Act-2 contradicted_by note (fixture) — Act 2's holding does not block Act 4.
    earned = _candidate(
        proposed_content="The Homie routes tasks by lane first then provider.",
        evidence_paths=["daily/episode.md"],
        confidence_score=0.85,
        source="reflection",
    )
    earned["contradicted_by"] = ["other-id:held-under-tension"]  # Act-2 state fixture

    # candidate #2 — asserted-but-unsupported: claims a read, cites an empty file,
    # at MAX confidence
    (mem / "daily" / "empty.md").write_text("", encoding="utf-8")
    asserted = _candidate(
        proposed_content="I verified the doc and it proves lane-first routing.",
        evidence_paths=["daily/empty.md"],
        confidence_score=0.99,
        source="reflection",
    )

    judge_yes = _fake_reasoning(
        {"supported": True, "correctness": 0.85, "evidence_fidelity": 0.8}
    )

    r1 = asyncio.run(
        el.propose_belief(earned, dry_run=False, memory_dir=mem, reasoning=judge_yes)
    )
    r2 = asyncio.run(
        el.propose_belief(asserted, dry_run=False, memory_dir=mem, reasoning=judge_yes)
    )

    # #1 EARNED -> applied, SELF.md gained it, ledger audited
    assert r1["adopt"] is True
    led = am.ProposalLedger(ledger_file)
    applied = [r for r in led.read_all() if r.status == "applied"]
    assert len(applied) == 1
    self_text = (mem / "SELF.md").read_text(encoding="utf-8")
    assert "lane first" in self_text.lower()

    # #2 asserted-but-unsupported -> rejected by the floor, SELF.md has ONLY #1
    assert r2["adopt"] is False
    assert r2["evidence_reason"] == "belief_regression_floor"
    # the asserted candidate's content never reached SELF.md
    assert "i verified the doc" not in self_text.lower()
