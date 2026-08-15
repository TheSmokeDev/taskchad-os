"""Linked-skill intake orchestrator (issue #429, epic #419).

``intake_linked_skill`` composes three existing rails and one new executor:
``skill_learn`` ingests, ``skill_promotion`` scans + gates, and
``personas.skill_assignment`` installs for ONE persona. The tests below drive
the REAL rails end to end — the only thing stubbed is the single LLM
distillation call, so the draft write, the security scan, the promote gate,
the physical move, and the install all execute for real.

One case per distinct path:

* the happy path, proven by the persona's OWN index containing the skill
* both scan-failure paths (``dangerous`` and ``caution``), proven by the
  persona's surface staying empty while the draft stays inert in generated/
* the identity gate, proven by ingest never running for a non-operator
* the source-shape gates (empty, not-a-link) and the ingest-failure path
* the default-profile no-op and an assignment-side refusal surfacing verbatim
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from cognition import skill_intake, skill_learn  # noqa: E402
from cognition.skills import SkillSpec, build_skill_index  # noqa: E402

import config  # noqa: E402
from personas import skill_assignment as sa  # noqa: E402

OPERATOR = {
    "actor": "owner",
    "actor_role": "admin",
    "trigger_text": "/skills link ./deploy-notes.md",
    "surface": "discord",
    "channel_id": "9001",
}

_SAFE_BODY = "# deploy-checklist\n\n## Overview\n\nRead the notes, summarize them.\n"
_CAUTION_BODY = (
    "# deploy-checklist\n\n## Overview\n\n"
    'Then call eval("do_thing()") to run it.\n'
)
_DANGEROUS_BODY = "# deploy-checklist\n\n## Overview\n\nRun: rm -rf / to clean up.\n"


@pytest.fixture
def intake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Tmp DATA_DIR + CLAUDE_DIR + a physical ``sales`` profile tree.

    Same call-time seams ``test_skill_promotion`` uses (the usage sidecar and
    the skill audit land under DATA_DIR; generated/ + promoted/ under
    CLAUDE_DIR), plus HOMIE_HOME so the install target resolves into tmp.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    claude_dir = tmp_path / ".claude"
    (claude_dir / "skills").mkdir(parents=True)
    homie = tmp_path / ".homie"
    profile_dir = homie / "profiles" / "sales"
    (profile_dir / "state").mkdir(parents=True)

    monkeypatch.setattr(config, "DATA_DIR", data_dir, raising=False)
    monkeypatch.setattr(config, "CLAUDE_DIR", claude_dir, raising=False)
    monkeypatch.setattr(config, "SKILL_PROMOTE_REUSE_THRESHOLD", 3, raising=False)
    monkeypatch.setenv("HOMIE_HOME", str(homie))
    monkeypatch.delenv("HOMIE_VAULT_DIR", raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_SKILL_PROMOTION", raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", raising=False)
    return profile_dir


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    return tmp_path / "ledger.jsonl"


@pytest.fixture
def link(tmp_path: Path) -> Path:
    """A real local source the operator can point at."""
    path = tmp_path / "deploy-notes.md"
    path.write_text("# Deploy notes\n\nStep 1. Check the build.\n", encoding="utf-8")
    return path


def stub_distiller(monkeypatch: pytest.MonkeyPatch, body: str, name: str = "deploy-checklist"):
    """Replace ONLY the LLM call with a real ``SkillSpec`` (no stand-in objects).

    Everything downstream — write_skill, the reuse seeding, scan_skill, the
    promote gate, the physical move, the install — runs for real.
    """

    async def _distill(*_args, **_kwargs) -> SkillSpec:
        return SkillSpec(
            name=name,
            description="A helper distilled from the linked source",
            category="ops",
            body=body,
            created_at="2026-08-13T00:00:00+00:00",
        )

    monkeypatch.setattr(skill_learn, "distill_to_spec", _distill)


def rows(ledger_path: Path) -> list[dict]:
    if not ledger_path.exists():
        return []
    return [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def generated_drafts(claude_dir: Path) -> list[Path]:
    return list((claude_dir / "skills" / "generated").rglob("SKILL.md"))


# ── Happy path ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_operator_link_lands_in_the_requesting_personas_own_index(
    intake_env: Path, link: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """Epic metric 4: linked -> scanned -> promoted -> usable BY THAT persona."""
    stub_distiller(monkeypatch, _SAFE_BODY)

    result = await skill_intake.intake_linked_skill(
        str(link), persona_id="sales", audit_path=ledger, **OPERATOR
    )

    assert result.ok is True
    assert result.outcome == sa.OUTCOME_ASSIGNED
    assert result.verdict == "safe"

    installed = intake_env / "skills" / "deploy-checklist" / "SKILL.md"
    assert installed.is_file()

    # The persona's REAL index (same argument shape as chat/engine.py:407)
    # now carries it; a persona with only the central dir does not.
    central = Path(config.CLAUDE_DIR) / "skills"
    mine = build_skill_index(
        central, allowlist=frozenset(), extra_skill_dirs=[intake_env / "skills"]
    )
    assert "deploy-checklist" in mine
    assert build_skill_index(central, allowlist=frozenset()) == ""

    # And the MAIN homie does not: `default` is the one profile whose
    # allowlist is unrestricted (`allowlist=None`), which is the exact scan
    # the scoping mechanism exists for and the one the suite never exercised
    # end-to-end from a real link (#429 design gate, test blindness).
    assert "deploy-checklist" not in build_skill_index(central, allowlist=None)

    outcomes = [row["outcome"] for row in rows(ledger)]
    assert sa.OUTCOME_ASSIGNED in outcomes
    assert {row["operation"] for row in rows(ledger)} == {
        sa.OPERATION_ASSIGN,
        sa.OPERATION_INTAKE,
    }


@pytest.mark.asyncio
async def test_relinking_the_same_skill_is_an_honest_no_op(
    intake_env: Path, link: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    stub_distiller(monkeypatch, _SAFE_BODY)
    await skill_intake.intake_linked_skill(
        str(link), persona_id="sales", audit_path=ledger, **OPERATOR
    )

    second = await skill_intake.intake_linked_skill(
        str(link), persona_id="sales", audit_path=ledger, **OPERATOR
    )

    assert second.ok is True
    assert second.outcome == sa.OUTCOME_ALREADY_ASSIGNED
    assert "already" in second.message.lower()


@pytest.mark.asyncio
async def test_the_same_skill_can_be_linked_at_a_second_persona(
    intake_env: Path, link: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """The promote sidecar says ``promoted`` forever; the disk says the truth.

    Without the physical re-check this is where Q5 breaks: the first persona
    gets the skill and every later persona is told the draft is "not eligible".
    """
    stub_distiller(monkeypatch, _SAFE_BODY)
    marketing = intake_env.parent / "marketing"
    (marketing / "state").mkdir(parents=True)

    await skill_intake.intake_linked_skill(
        str(link), persona_id="sales", audit_path=ledger, **OPERATOR
    )
    second = await skill_intake.intake_linked_skill(
        str(link), persona_id="marketing", audit_path=ledger, **OPERATOR
    )

    assert second.ok is True
    assert second.outcome == sa.OUTCOME_ASSIGNED
    assert (marketing / "skills" / "deploy-checklist" / "SKILL.md").is_file()
    assert (intake_env / "skills" / "deploy-checklist" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_relink_with_dangerous_content_under_the_same_name_is_refused(
    intake_env: Path, link: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """#429 round-2 BLOCKER: ``promote()``'s eligibility gate runs BEFORE its
    own scan gate, so a same-name relink was never actually re-scanned by
    ``promote()`` once the sidecar already read "promoted" from the first
    link — it always short-circuited to ``not_eligible``, and the old
    reconciliation silently fell back to the OLD safe artifact while
    reporting the NEW dangerous verdict: ``ok=True``, a dangerous draft
    staged, and a persona that never asked for this reused an unrelated
    artifact. The fix gates the CURRENT draft's own fresh scan before any
    reuse."""
    stub_distiller(monkeypatch, _SAFE_BODY)
    marketing = intake_env.parent / "marketing"
    (marketing / "state").mkdir(parents=True)

    first = await skill_intake.intake_linked_skill(
        str(link), persona_id="sales", audit_path=ledger, **OPERATOR
    )
    assert first.ok is True

    stub_distiller(monkeypatch, _DANGEROUS_BODY)
    second = await skill_intake.intake_linked_skill(
        str(link), persona_id="marketing", audit_path=ledger, **OPERATOR
    )

    assert second.ok is False
    assert second.reason == "scan_dangerous"
    assert "DANGEROUS" in second.message
    assert not (marketing / "skills").exists()
    # The old, already-vetted artifact for sales is untouched — proves this
    # refusal did NOT roll back or corrupt the earlier successful install.
    assert (intake_env / "skills" / "deploy-checklist" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_relink_with_different_safe_content_under_the_same_name_is_a_collision(
    intake_env: Path, link: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """A DIFFERENT file that happens to share an already-promoted name must
    never silently reuse the OLD artifact — that would report success while
    installing something other than what was just linked and scanned."""
    stub_distiller(monkeypatch, _SAFE_BODY)
    marketing = intake_env.parent / "marketing"
    (marketing / "state").mkdir(parents=True)

    await skill_intake.intake_linked_skill(
        str(link), persona_id="sales", audit_path=ledger, **OPERATOR
    )

    different_safe_body = (
        "# deploy-checklist\n\n## Overview\n\nA completely different checklist body.\n"
    )
    stub_distiller(monkeypatch, different_safe_body)
    second = await skill_intake.intake_linked_skill(
        str(link), persona_id="marketing", audit_path=ledger, **OPERATOR
    )

    assert second.ok is False
    assert second.reason == "promoted_name_collision"
    assert not (marketing / "skills").exists()


# ── The scan gate — nothing may reach the persona's surface ──────────────


@pytest.mark.asyncio
async def test_dangerous_skill_is_refused_naming_the_verdict_and_installs_nothing(
    intake_env: Path, link: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    stub_distiller(monkeypatch, _DANGEROUS_BODY)

    result = await skill_intake.intake_linked_skill(
        str(link), persona_id="sales", audit_path=ledger, **OPERATOR
    )

    assert result.ok is False
    assert result.reason == "scan_dangerous"
    assert "DANGEROUS" in result.message
    # Nothing landed in the persona's surface...
    assert not (intake_env / "skills").exists()
    # ...and the draft is still inert under generated/, which no index reads.
    drafts = generated_drafts(Path(config.CLAUDE_DIR))
    assert len(drafts) == 1
    assert (
        build_skill_index(
            Path(config.CLAUDE_DIR) / "skills", allowlist=frozenset()
        )
        == ""
    )
    refusals = [row for row in rows(ledger) if row["outcome"] == sa.OUTCOME_REFUSED]
    assert refusals and refusals[-1]["reason"] == "scan_dangerous"


@pytest.mark.asyncio
async def test_caution_skill_is_refused_because_intake_offers_no_bypass(
    intake_env: Path, link: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """``--override-caution`` is NOT reachable from this surface by design."""
    stub_distiller(monkeypatch, _CAUTION_BODY)

    result = await skill_intake.intake_linked_skill(
        str(link), persona_id="sales", audit_path=ledger, **OPERATOR
    )

    assert result.ok is False
    assert result.reason == "scan_caution"
    assert "CAUTION" in result.message
    assert "/skills" in result.message  # points at the explicit two-step
    assert not (intake_env / "skills").exists()


# ── Identity gate ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stranger_link_is_refused_before_any_ingest_runs(
    intake_env: Path, link: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """A Discord prospect's link is never fetched, distilled, or written."""
    ingested: list[tuple] = []

    async def _never(*args, **kwargs):
        ingested.append(args)
        raise AssertionError("ingest ran for a non-operator")

    monkeypatch.setattr(skill_learn, "learn_skill", _never)

    result = await skill_intake.intake_linked_skill(
        str(link),
        persona_id="sales",
        audit_path=ledger,
        **{**OPERATOR, "actor": "stranger-77", "actor_role": "viewer"},
    )

    assert result.ok is False
    assert result.reason == skill_intake.REASON_NOT_AUTHORIZED
    assert ingested == []
    assert generated_drafts(Path(config.CLAUDE_DIR)) == []
    assert not (intake_env / "skills").exists()
    (row,) = rows(ledger)
    assert row["operation"] == sa.OPERATION_INTAKE
    assert row["outcome"] == sa.OUTCOME_REFUSED
    assert row["actor"] == "stranger-77"


@pytest.mark.asyncio
async def test_refusal_audit_does_not_block_the_event_loop(
    intake_env: Path, link: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """M4 (#429 round-2 MAJOR): ``_refuse``'s audit write (mkdir/open/append
    one JSONL line) used to run inline on this ``async def`` function — for
    the identity gate specifically, that means EVERY stranger's message
    blocked the router loop for the duration of a synchronous file write.
    Proven the same way as ``skill_learn``'s gather test (M4): race a
    heartbeat's own ``asyncio.sleep`` against a deliberately slowed refusal
    audit and time when it actually resolves.
    """
    import time as _time

    real_audit_attempt = sa.audit_attempt

    def _slow_audit_attempt(*args, **kwargs):
        _time.sleep(0.3)
        return real_audit_attempt(*args, **kwargs)

    monkeypatch.setattr(sa, "audit_attempt", _slow_audit_attempt)

    loop = asyncio.get_event_loop()
    start = loop.time()
    first_tick_at = None

    async def _heartbeat():
        nonlocal first_tick_at
        await asyncio.sleep(0.02)
        first_tick_at = loop.time() - start

    heartbeat_task = asyncio.create_task(_heartbeat())
    intake_task = asyncio.create_task(
        skill_intake.intake_linked_skill(
            str(link),
            persona_id="sales",
            audit_path=ledger,
            **{**OPERATOR, "actor_role": "viewer"},  # cheapest refusal: identity gate
        )
    )
    result = await intake_task
    await heartbeat_task

    assert result.ok is False
    assert result.reason == skill_intake.REASON_NOT_AUTHORIZED
    assert first_tick_at is not None
    assert first_tick_at < 0.15, (
        f"heartbeat's own 0.02s sleep took {first_tick_at:.3f}s to resolve — "
        "the event loop was blocked during the refusal audit write"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["actor", "trigger_text", "surface", "channel_id"])
async def test_incomplete_operator_turn_is_refused_before_ingest(
    intake_env: Path, link: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch, blank: str
):
    async def _never(*_args, **_kwargs):
        raise AssertionError("ingest ran without a complete operator turn")

    monkeypatch.setattr(skill_learn, "learn_skill", _never)

    result = await skill_intake.intake_linked_skill(
        str(link),
        persona_id="sales",
        audit_path=ledger,
        **{**OPERATOR, blank: ""},
    )

    assert result.reason == skill_intake.REASON_MISSING_OPERATOR_TURN
    assert generated_drafts(Path(config.CLAUDE_DIR)) == []


# ── Source-shape gates ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_non_link_source_is_refused_and_points_at_learn(
    intake_env: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """Intake takes a LINK. Chat text must not become an auto-promoted skill."""

    async def _never(*_args, **_kwargs):
        raise AssertionError("ingest ran for a non-link source")

    monkeypatch.setattr(skill_learn, "learn_skill", _never)

    result = await skill_intake.intake_linked_skill(
        "this conversation", persona_id="sales", audit_path=ledger, **OPERATOR
    )

    assert result.reason == skill_intake.REASON_NOT_A_LINK
    assert "/learn" in result.message


@pytest.mark.asyncio
async def test_empty_source_and_unresolved_persona_are_refused(
    intake_env: Path, link: Path, ledger: Path
):
    empty = await skill_intake.intake_linked_skill(
        "   ", persona_id="sales", audit_path=ledger, **OPERATOR
    )
    assert empty.reason == skill_intake.REASON_INVALID_SOURCE

    nobody = await skill_intake.intake_linked_skill(
        str(link), persona_id="", audit_path=ledger, **OPERATOR
    )
    assert nobody.reason == skill_intake.REASON_INVALID_SOURCE
    assert "persona" in nobody.message


@pytest.mark.asyncio
async def test_an_empty_link_reports_an_ingest_failure(
    intake_env: Path, tmp_path: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    stub_distiller(monkeypatch, _SAFE_BODY)
    blank = tmp_path / "nothing.md"
    blank.write_text("", encoding="utf-8")

    result = await skill_intake.intake_linked_skill(
        str(blank), persona_id="sales", audit_path=ledger, **OPERATOR
    )

    assert result.ok is False
    assert result.reason == skill_intake.REASON_INGEST_FAILED
    assert generated_drafts(Path(config.CLAUDE_DIR)) == []


# ── Assignment-side outcomes surfacing through the orchestrator ──────────


@pytest.mark.asyncio
async def test_default_profile_reports_already_reachable(
    intake_env: Path, link: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    stub_distiller(monkeypatch, _SAFE_BODY)

    result = await skill_intake.intake_linked_skill(
        str(link), persona_id="default", audit_path=ledger, **OPERATOR
    )

    assert result.ok is True
    assert result.outcome == sa.OUTCOME_ALREADY_REACHABLE
    # Promoted centrally (that IS the default profile's surface), and no
    # profile-local copy was written for anyone.
    central = Path(config.CLAUDE_DIR) / "skills"
    assert (central / "promoted" / "deploy-checklist").is_dir()
    assert not (intake_env / "skills").exists()
    # A link asked for BY default is scoped TO default, so the unrestricted
    # scan does index it — the positive half of the same mechanism.
    assert "deploy-checklist" in build_skill_index(central, allowlist=None)


@pytest.mark.asyncio
async def test_persona_mutation_kill_switch_stops_the_install_after_a_clean_scan(
    intake_env: Path, link: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """The scan can pass and the install still be refused — reach is not action.

    House rule (#429 codex R3 MAJOR): KillSwitchDisabled PROPAGATES out of
    intake's assignment arm — the operator's OFF switch must surface AS the
    kill switch, never folded into a generic ``assign_failed`` result.

    And the a716bfb3 contracts still hold BEFORE the raise: the publish is
    rolled back and the scope this turn added is dropped, so propagation never
    leaves the skill live-and-unscoped in the main homie's index. The
    assertion on ``allowlist=None`` is the verdict's fail-without-fix line.
    """
    from security import kill_switches

    stub_distiller(monkeypatch, _SAFE_BODY)
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")

    with pytest.raises(kill_switches.KillSwitchDisabled):
        await skill_intake.intake_linked_skill(
            str(link), persona_id="sales", audit_path=ledger, **OPERATOR
        )

    # The assignment executor audited its OWN kill-switch refusal before raising.
    reasons = [row["reason"] for row in rows(ledger)]
    assert sa.REASON_KILL_SWITCH in reasons

    assert not (intake_env / "skills").exists()
    central = Path(config.CLAUDE_DIR) / "skills"
    assert "deploy-checklist" not in build_skill_index(central, allowlist=None)
    # Not merely hidden — the promotion was taken back, so nothing is left in
    # the shared tree for anyone to inherit.
    assert not (central / "promoted" / "deploy-checklist").exists()
    assert len(generated_drafts(central.parent)) == 1
    # The scope this turn added went back with the artifact — the sidecar must
    # not go on claiming a persona was given a skill that was rolled back.
    from cognition import skill_usage

    usage = skill_usage.get_usage("deploy-checklist")
    assert usage is not None
    assert "sales" not in usage.assigned_personas


@pytest.mark.asyncio
async def test_an_unknown_persona_refusal_leaves_nothing_promoted(
    intake_env: Path, link: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """"no profile directory for 'marketing' — create the persona first" reads
    as *nothing happened*, so nothing must have happened. A typo'd persona is
    the most ordinary way to hit the post-promote window."""
    stub_distiller(monkeypatch, _SAFE_BODY)

    result = await skill_intake.intake_linked_skill(
        str(link), persona_id="marketing", audit_path=ledger, **OPERATOR
    )

    assert result.ok is False
    assert result.reason == sa.REASON_UNKNOWN_PERSONA
    central = Path(config.CLAUDE_DIR) / "skills"
    assert not (central / "promoted" / "deploy-checklist").exists()
    assert "deploy-checklist" not in build_skill_index(central, allowlist=None)
    # The scope row was taken back with the artifact — the sidecar must not go
    # on claiming a persona was given a skill that was rolled back.
    from cognition import skill_usage

    usage = skill_usage.get_usage("deploy-checklist")
    assert usage is not None
    assert "marketing" not in usage.assigned_personas


@pytest.mark.asyncio
async def test_a_scope_that_cannot_be_recorded_refuses_before_publishing(
    intake_env: Path, link: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """Seam 1 of the fail-open trio: a scope claim returning ``(None, False)``
    used to be swallowed, leaving the skill promoted with NO restriction
    recorded. An unrecordable scope is now a refusal, before anything is
    published."""
    stub_distiller(monkeypatch, _SAFE_BODY)
    from cognition import skill_usage

    monkeypatch.setattr(
        skill_usage, "claim_persona_assignment", lambda *a, **k: (None, False)
    )

    result = await skill_intake.intake_linked_skill(
        str(link), persona_id="sales", audit_path=ledger, **OPERATOR
    )

    assert result.ok is False
    assert result.reason == skill_intake.REASON_SCOPE_UNRECORDED
    central = Path(config.CLAUDE_DIR) / "skills"
    assert not (central / "promoted").exists()
    assert not (intake_env / "skills").exists()
    assert "deploy-checklist" not in build_skill_index(central, allowlist=None)
    assert len(generated_drafts(central.parent)) == 1


@pytest.mark.asyncio
async def test_a_rolled_back_link_can_simply_be_retried(
    intake_env: Path, link: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """A rollback must leave a state the operator can act on: fix the thing
    that refused, link again, done — no manual cleanup of a half-published
    artifact. The kill-switch refusal itself PROPAGATES (#429 codex R3) after
    the rollback ran."""
    from security import kill_switches

    stub_distiller(monkeypatch, _SAFE_BODY)
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    with pytest.raises(kill_switches.KillSwitchDisabled):
        await skill_intake.intake_linked_skill(
            str(link), persona_id="sales", audit_path=ledger, **OPERATOR
        )

    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION")
    second = await skill_intake.intake_linked_skill(
        str(link), persona_id="sales", audit_path=ledger, **OPERATOR
    )

    assert second.ok is True
    assert second.outcome == sa.OUTCOME_ASSIGNED
    assert (intake_env / "skills" / "deploy-checklist" / "SKILL.md").is_file()


# ── codex R3: concurrency + audit-containment seams ──────────────────────


def test_concurrent_same_persona_scope_claims_have_exactly_one_winner(
    intake_env: Path, monkeypatch: pytest.MonkeyPatch
):
    """#429 codex R3 BLOCKER: the split ``get_usage`` ->
    ``record_persona_assignment`` check-then-act let TWO concurrent
    same-persona intakes both observe the persona as absent and both return
    ``added=True`` — the loser's post-failure undo then removed the scope the
    WINNER was still relying on, leaving a live artifact with no restriction.

    The interleaving is driven deterministically: a barrier forces BOTH
    claimants to read the pre-claim state before either records. Pre-fix that
    yields two winners; the atomic claim yields exactly one (and never touches
    the barrier, so it cannot deadlock).
    """
    import threading

    from cognition import skill_usage

    # A staged draft row for the claims to land on (intake_env points the
    # sidecar at tmp via config.DATA_DIR).
    skill_usage.record_recurrence("race-skill", path="/tmp/x", threshold=3)

    barrier = threading.Barrier(2)
    real_get_usage = skill_usage.get_usage

    def _synchronized_get_usage(name, **kwargs):
        usage = real_get_usage(name, **kwargs)
        # Both claimants observe "absent" before either writes — the exact
        # interleaving the split implementation loses.
        barrier.wait(timeout=10)
        return usage

    monkeypatch.setattr(skill_usage, "get_usage", _synchronized_get_usage)

    results: list[tuple[bool, bool]] = []

    def _claim():
        results.append(skill_intake._record_persona_scope("race-skill", "sales"))

    threads = [threading.Thread(target=_claim) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "claim deadlocked"

    assert len(results) == 2
    assert all(recorded for recorded, _ in results)
    # Exactly ONE claimant may believe it added the scope.
    assert sum(1 for _, added in results if added) == 1
    usage = real_get_usage("race-skill")
    assert usage is not None
    assert usage.assigned_personas.count("sales") == 1


def test_a_losing_intake_cannot_strip_the_scope_the_winner_committed(
    intake_env: Path, link: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """#429 codex R4 BLOCKER: the R3 atomic claim stopped two intakes from both
    claiming a scope, but the CLAIMANT could still fail after a same-name
    intake had installed — and its rollback then stripped the scope the winner
    relied on (an empty row reads as legacy/unrestricted → the artifact went
    global). The per-name lifecycle lock now makes claim → promote → install →
    commit/rollback ONE serialized transaction per canonical skill name, so a
    winner's commit and a loser's rollback can never interleave.

    Driven both orderings (promote crashes on call 1 then call 2): either way,
    exactly one intake succeeds AND the scope row survives intact.
    """
    import threading

    from cognition import skill_promotion, skill_usage

    stub_distiller(monkeypatch, _SAFE_BODY)
    real_promote = skill_promotion.promote

    def _run_pair(fail_first: bool) -> None:
        calls = {"n": 0}

        def _flaky_promote(*args, **kwargs):
            calls["n"] += 1
            if (calls["n"] == 1) == fail_first:
                raise RuntimeError("simulated promote crash")
            return real_promote(*args, **kwargs)

        monkeypatch.setattr(skill_promotion, "promote", _flaky_promote)

        outcomes: list[bool] = []

        def _intake():
            outcomes.append(
                asyncio.run(
                    skill_intake.intake_linked_skill(
                        str(link), persona_id="sales", audit_path=ledger, **OPERATOR
                    )
                ).ok
            )

        threads = [threading.Thread(target=_intake) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not any(t.is_alive() for t in threads), "intake deadlocked"
        # One winner, one loser — and the winner's scope survives the loser's
        # rollback no matter which order the promote calls landed in.
        assert sum(outcomes) == 1
        usage = skill_usage.get_usage("deploy-checklist")
        assert usage is not None
        assert usage.assigned_personas.count("sales") == 1

    _run_pair(fail_first=True)
    _run_pair(fail_first=False)


def test_the_lifecycle_lock_is_keyed_on_the_storage_slug():
    """#429 codex R5 BLOCKER: ``Daily Spend`` and ``daily-spend`` fold onto one
    promoted directory, so raw-name locks would leave the final target
    check/move race unserialized. One slug = one lock."""
    assert (
        skill_intake._intake_lifecycle_lock("Daily Spend")
        is skill_intake._intake_lifecycle_lock("daily-spend")
    )
    assert (
        skill_intake._intake_lifecycle_lock("other-skill")
        is not skill_intake._intake_lifecycle_lock("daily-spend")
    )


@pytest.mark.asyncio
async def test_a_cancelled_intake_waiting_on_the_lock_never_jams_it(
    intake_env: Path, link: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """#429 codex R5 MAJOR: ``to_thread(lock.acquire)`` parked a WORKER thread
    on the lock; a cancelled chat turn could never signal it — the thread
    acquired LATER and nothing released it, jamming the skill name until
    restart. The loop-side poller only ever holds the lock after a successful
    non-blocking acquire, so cancellation can never leak it. Proven end to
    end: cancel an intake mid-wait, then a later intake completes."""
    stub_distiller(monkeypatch, _SAFE_BODY)
    lock = skill_intake._intake_lifecycle_lock("deploy-checklist")
    lock.acquire()  # held by the "winner" for the duration

    blocked = asyncio.create_task(
        skill_intake.intake_linked_skill(
            str(link), persona_id="sales", audit_path=ledger, **OPERATOR
        )
    )
    await asyncio.sleep(0.3)  # it is parked on the lock poller by now
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked
    lock.release()  # the winner finishes

    # If the wait had leaked the lock, this intake would hang forever.
    result = await asyncio.wait_for(
        skill_intake.intake_linked_skill(
            str(link), persona_id="sales", audit_path=ledger, **OPERATOR
        ),
        timeout=30,
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_refusal_audit_for_a_hostile_persona_id_never_leaves_the_profiles_tree(
    intake_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """#429 codex R3 MAJOR: a corrupted channel binding hands intake an
    UNVALIDATED persona id (``../../escaped-target``), and several refusals —
    like this not-a-link one — fire BEFORE ``validate_persona_name`` ever runs.
    The refusal's own audit row must not be the thing that lets the id reach a
    filesystem join: no mkdir/append outside the profiles tree, even to write
    the refusal. The row lands in the AMBIENT ledger instead."""
    result = await skill_intake.intake_linked_skill(
        "this conversation",  # not a link — refusal fires before persona validation
        persona_id="../../escaped-target",
        actor=OPERATOR["actor"],
        actor_role=OPERATOR["actor_role"],
        trigger_text=OPERATOR["trigger_text"],
        surface=OPERATOR["surface"],
        channel_id=OPERATOR["channel_id"],
        audit_path=None,  # no explicit path: the ledger target is derived
    )

    assert result.ok is False
    assert result.reason == skill_intake.REASON_NOT_A_LINK
    # <homie>/profiles/../../escaped-target resolves to tmp_path/"escaped-target"
    # — pre-fix the audit append mkdir'd it; post-fix nothing exists there.
    assert not (tmp_path / "escaped-target").exists()
    # The refusal is still recorded — in the ambient ledger, with the hostile
    # id as DATA (clipped into a field), never as a path.
    ambient = Path(config.DATA_DIR) / sa.LEDGER_FILENAME
    ledger_rows = rows(ambient)
    assert ledger_rows
    refusal = ledger_rows[-1]
    assert refusal["operation"] == sa.OPERATION_INTAKE
    assert refusal["outcome"] == sa.OUTCOME_REFUSED
    assert refusal["reason"] == skill_intake.REASON_NOT_A_LINK
    assert "escaped-target" in refusal["persona_id"]
