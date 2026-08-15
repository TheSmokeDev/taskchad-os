"""Persona-scoped skill assignment executor (issue #429, epic #419).

``assign_skill_to_persona`` is the ONLY path that puts a vetted skill in
front of ONE persona. One case per distinct code path through it:

* the install path and both no-op paths (already-installed byte-identical,
  default profile reachable centrally)
* the re-install path (same name, changed content)
* every refusal branch (no persona, no skill name, incomplete operator turn,
  non-admin role, kill switch, invalid name, unknown persona, unreadable
  source, traversal-shaped skill name)
* the ledger contract (schema, target-persona keying, hostile trigger text,
  refusal-cannot-be-audited)
* the failure path that must not cost the persona a skill it already had

Physical state is asserted throughout: a refusal must leave the persona's
skills directory byte-identical (or absent), never merely return an error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import config  # noqa: E402
from personas import skill_assignment as sa  # noqa: E402

OPERATOR = {
    "actor": "owner",
    "actor_role": "admin",
    "trigger_text": "/skills link https://example.com/skill",
    "surface": "discord",
    "channel_id": "9001",
}

_SKILL_MD = (
    "---\n"
    "name: {name}\n"
    "description: A perfectly safe helper skill\n"
    "version: 1.0.0\n"
    "category: ops\n"
    "promoted: true\n"
    "---\n\n"
    "# {name}\n\n"
    "{body}\n"
)


# ── Fixtures / helpers ───────────────────────────────────────────────────


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A physical named-profile tree at ``<tmp>/.homie/profiles/sales``.

    ``HOMIE_HOME`` points at the fake ROOT (not at a profile), so named
    profiles resolve under ``<root>/profiles/<name>/`` — same shape as the
    #426 fixture.

    ``CLAUDE_DIR``/``DATA_DIR`` are redirected as a CONTAINMENT guard, not
    because the executor reads them: an install that regressed to keying off
    the install dir would otherwise write into the operator's real
    ``.claude/skills`` during a test run (observed while mutation-testing
    this suite). Redirected, that regression fails loudly in tmp instead.
    """
    homie = tmp_path / ".homie"
    profile_dir = homie / "profiles" / "sales"
    (profile_dir / "state").mkdir(parents=True)
    claude_dir = tmp_path / ".claude"
    (claude_dir / "skills").mkdir(parents=True)
    (claude_dir / "data").mkdir()
    monkeypatch.setattr(config, "CLAUDE_DIR", claude_dir, raising=False)
    monkeypatch.setattr(config, "DATA_DIR", claude_dir / "data", raising=False)
    monkeypatch.setenv("HOMIE_HOME", str(homie))
    monkeypatch.delenv("HOMIE_VAULT_DIR", raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", raising=False)
    return profile_dir


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    return tmp_path / "ledger.jsonl"


@pytest.fixture
def promoted(tmp_path: Path) -> Path:
    """A promoted skill directory, as ``skill_promotion.promote`` leaves one."""
    return _make_source(tmp_path / "promoted", "deploy-checklist", "Run the checklist.")


def _make_source(root: Path, name: str, body: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        _SKILL_MD.format(name=name, body=body), encoding="utf-8"
    )
    return skill_dir


def rows(ledger_path: Path) -> list[dict]:
    if not ledger_path.exists():
        return []
    return [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def skills_root(profile_dir: Path) -> Path:
    return profile_dir / "skills"


# ── Install paths ────────────────────────────────────────────────────────


def test_install_writes_the_skill_into_the_personas_own_dir_and_audits(
    profile: Path, promoted: Path, ledger: Path
):
    result = sa.assign_skill_to_persona(
        "sales",
        promoted / "SKILL.md",
        skill_name="deploy-checklist",
        audit_path=ledger,
        **OPERATOR,
    )

    installed = skills_root(profile) / "deploy-checklist" / "SKILL.md"
    assert installed.is_file()
    assert installed.read_bytes() == (promoted / "SKILL.md").read_bytes()
    assert result.outcome == sa.OUTCOME_ASSIGNED
    assert result.changed is True
    assert result.install_path == installed

    (row,) = rows(ledger)
    assert row["operation"] == sa.OPERATION_ASSIGN
    assert row["outcome"] == sa.OUTCOME_ASSIGNED
    assert row["persona_id"] == "sales"
    assert row["skill_name"] == "deploy-checklist"
    assert row["actor"] == "owner"
    assert row["actor_role"] == "admin"
    assert row["surface"] == "discord"
    assert row["channel_id"] == "9001"
    assert row["trigger_text"] == OPERATOR["trigger_text"]
    assert row["install_path"] == str(installed)


def test_installed_skill_is_reachable_only_from_that_personas_index(
    profile: Path, promoted: Path, ledger: Path, tmp_path: Path
):
    """Q5 by construction: the install lands in the extra dir ONE persona reads.

    Drives the REAL ``build_skill_index`` with the same argument shape every
    persona runtime uses (``chat/engine.py:407``) — central dir + that
    persona's own skills dir — and proves a second persona's index (its own
    extra dir) does not see it.
    """
    from cognition.skills import build_skill_index

    central = tmp_path / "central-skills"
    central.mkdir()
    other = tmp_path / "other-profile" / "skills"
    other.mkdir(parents=True)

    sa.assign_skill_to_persona(
        "sales",
        promoted,
        skill_name="deploy-checklist",
        audit_path=ledger,
        **OPERATOR,
    )

    mine = build_skill_index(
        central, allowlist=frozenset(), extra_skill_dirs=[skills_root(profile)]
    )
    theirs = build_skill_index(central, allowlist=frozenset(), extra_skill_dirs=[other])
    assert "deploy-checklist" in mine
    assert "deploy-checklist" not in theirs


def test_reinstalling_identical_content_is_a_no_op_and_says_so(
    profile: Path, promoted: Path, ledger: Path
):
    sa.assign_skill_to_persona(
        "sales", promoted, skill_name="deploy-checklist", audit_path=ledger, **OPERATOR
    )
    installed = skills_root(profile) / "deploy-checklist" / "SKILL.md"
    before = installed.stat().st_mtime_ns

    result = sa.assign_skill_to_persona(
        "sales", promoted, skill_name="deploy-checklist", audit_path=ledger, **OPERATOR
    )

    assert result.outcome == sa.OUTCOME_ALREADY_ASSIGNED
    assert result.changed is False
    assert installed.stat().st_mtime_ns == before
    assert [row["outcome"] for row in rows(ledger)] == [
        sa.OUTCOME_ASSIGNED,
        sa.OUTCOME_ALREADY_ASSIGNED,
    ]


def test_reinstalling_changed_content_replaces_the_installed_copy(
    profile: Path, promoted: Path, ledger: Path, tmp_path: Path
):
    sa.assign_skill_to_persona(
        "sales", promoted, skill_name="deploy-checklist", audit_path=ledger, **OPERATOR
    )
    updated = _make_source(tmp_path / "v2", "deploy-checklist", "Run the NEW checklist.")

    result = sa.assign_skill_to_persona(
        "sales", updated, skill_name="deploy-checklist", audit_path=ledger, **OPERATOR
    )

    installed = skills_root(profile) / "deploy-checklist" / "SKILL.md"
    assert result.outcome == sa.OUTCOME_ASSIGNED
    assert result.changed is True
    assert "NEW checklist" in installed.read_text(encoding="utf-8")


def test_default_profile_reports_already_reachable_without_writing(
    tmp_path: Path, promoted: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """The default profile indexes the central dir with an unrestricted allowlist.

    A profile-local copy would list the same skill twice in one index, so the
    executor short-circuits. Asserted against the REAL default paths — no
    write may land in the repo tree.
    """
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", raising=False)
    from personas.core import get_default_paths

    central = get_default_paths()["skills"]
    before = sorted(p.name for p in central.iterdir()) if central.is_dir() else []

    result = sa.assign_skill_to_persona(
        "default",
        promoted,
        skill_name="deploy-checklist",
        audit_path=ledger,
        **OPERATOR,
    )

    assert result.outcome == sa.OUTCOME_ALREADY_REACHABLE
    assert result.changed is False
    after = sorted(p.name for p in central.iterdir()) if central.is_dir() else []
    assert after == before
    assert rows(ledger)[0]["reason"] == sa.REASON_DEFAULT_PROFILE_CENTRAL


# ── Refusals — each must leave the skills dir untouched ──────────────────


def test_non_admin_role_is_refused_and_installs_nothing(
    profile: Path, promoted: Path, ledger: Path
):
    """A Discord stranger's link never reaches the persona's surface."""
    turn = {**OPERATOR, "actor": "stranger-77", "actor_role": "viewer"}
    with pytest.raises(sa.SkillAssignmentRefusedError) as exc:
        sa.assign_skill_to_persona(
            "sales", promoted, skill_name="deploy-checklist", audit_path=ledger, **turn
        )

    assert exc.value.reason == sa.REASON_NOT_AUTHORIZED
    assert not skills_root(profile).exists()
    (row,) = rows(ledger)
    assert row["outcome"] == sa.OUTCOME_REFUSED
    assert row["reason"] == sa.REASON_NOT_AUTHORIZED
    assert row["actor"] == "stranger-77"


@pytest.mark.parametrize("blank", ["actor", "trigger_text", "surface", "channel_id"])
def test_incomplete_operator_turn_is_refused(
    profile: Path, promoted: Path, ledger: Path, blank: str
):
    turn = {**OPERATOR, blank: ""}
    with pytest.raises(sa.SkillAssignmentRefusedError) as exc:
        sa.assign_skill_to_persona(
            "sales", promoted, skill_name="deploy-checklist", audit_path=ledger, **turn
        )

    assert exc.value.reason == sa.REASON_MISSING_OPERATOR_TURN
    assert not skills_root(profile).exists()
    assert rows(ledger)[0]["reason"] == sa.REASON_MISSING_OPERATOR_TURN


def test_kill_switch_refuses_before_any_write(
    profile: Path, promoted: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    from security import kill_switches

    with pytest.raises(kill_switches.KillSwitchDisabled):
        sa.assign_skill_to_persona(
            "sales",
            promoted,
            skill_name="deploy-checklist",
            audit_path=ledger,
            **OPERATOR,
        )

    assert not skills_root(profile).exists()
    assert rows(ledger)[0]["reason"] == sa.REASON_KILL_SWITCH


def test_unknown_persona_is_refused_and_conjures_no_ghost_profile(
    profile: Path, promoted: Path, ledger: Path, tmp_path: Path
):
    with pytest.raises(sa.SkillAssignmentRefusedError) as exc:
        sa.assign_skill_to_persona(
            "sles", promoted, skill_name="deploy-checklist", audit_path=ledger, **OPERATOR
        )

    assert exc.value.reason == sa.REASON_UNKNOWN_PERSONA
    assert not (tmp_path / ".homie" / "profiles" / "sles").exists()
    assert rows(ledger)[0]["reason"] == sa.REASON_UNKNOWN_PERSONA


def test_invalid_persona_name_is_refused(profile: Path, promoted: Path, ledger: Path):
    with pytest.raises(sa.SkillAssignmentRefusedError) as exc:
        sa.assign_skill_to_persona(
            "../escape",
            promoted,
            skill_name="deploy-checklist",
            audit_path=ledger,
            **OPERATOR,
        )

    assert exc.value.reason == sa.REASON_INVALID_PERSONA
    assert rows(ledger)[0]["reason"] == sa.REASON_INVALID_PERSONA


def test_invalid_persona_name_never_derives_a_filesystem_path_for_its_own_audit(
    profile: Path, promoted: Path, tmp_path: Path,
):
    """M3 (#429 round-2 MAJOR): the test above injects an explicit
    ``audit_path``, which masks this bug entirely — the refusal audits fine
    regardless of what ``get_persona_paths("../escape")`` would resolve to.
    Here NO ``audit_path`` override is given, so the refusal must derive its
    OWN ledger path the way a real caller would — and that derivation must
    never touch anything outside the profiles tree, even to record its own
    refusal. Before the fix, ``get_persona_paths()`` joined the hostile name
    straight onto ``<root>/profiles/<name>/`` with no containment check, and
    ``append_audit_record`` then ``mkdir(parents=True)``'d there."""
    hostile = "../../escaped-target"
    escaped_root = (tmp_path / ".homie" / "profiles" / hostile).resolve()

    with pytest.raises(sa.SkillAssignmentRefusedError) as exc:
        sa.assign_skill_to_persona(
            hostile, promoted, skill_name="deploy-checklist", **OPERATOR,
        )

    assert exc.value.reason == sa.REASON_INVALID_PERSONA
    # Nothing was created OUTSIDE the profiles tree...
    assert not escaped_root.exists()
    # ...and the refusal was still audited — to the SAFE ambient ledger
    # (`profile`'s fixture points config.DATA_DIR at <tmp>/.claude/data).
    safe_ledger = tmp_path / ".claude" / "data" / sa.LEDGER_FILENAME
    assert safe_ledger.is_file()
    assert rows(safe_ledger)[-1]["reason"] == sa.REASON_INVALID_PERSONA


def test_missing_source_is_refused(profile: Path, ledger: Path, tmp_path: Path):
    with pytest.raises(sa.SkillAssignmentRefusedError) as exc:
        sa.assign_skill_to_persona(
            "sales",
            tmp_path / "nope",
            skill_name="deploy-checklist",
            audit_path=ledger,
            **OPERATOR,
        )

    assert exc.value.reason == sa.REASON_MISSING_SOURCE
    assert not skills_root(profile).exists()


def test_blank_persona_and_blank_skill_name_are_refused(promoted: Path, ledger: Path):
    with pytest.raises(sa.SkillAssignmentRefusedError) as exc:
        sa.assign_skill_to_persona(
            "  ", promoted, skill_name="deploy-checklist", audit_path=ledger, **OPERATOR
        )
    assert exc.value.reason == sa.REASON_INVALID_PERSONA

    with pytest.raises(sa.SkillAssignmentRefusedError) as exc:
        sa.assign_skill_to_persona(
            "sales", promoted, skill_name="  ", audit_path=ledger, **OPERATOR
        )
    assert exc.value.reason == sa.REASON_INVALID_SKILL


@pytest.mark.parametrize(
    "hostile",
    ["../../evil", "nested/evil", "..", ".hidden", "bad\nname"],
)
def test_traversal_shaped_skill_names_are_refused_before_any_write(
    profile: Path, promoted: Path, ledger: Path, hostile: str
):
    """The skill name is LLM-authored, so it is hostile input at this seam."""
    with pytest.raises(sa.SkillAssignmentRefusedError) as exc:
        sa.assign_skill_to_persona(
            "sales", promoted, skill_name=hostile, audit_path=ledger, **OPERATOR
        )

    assert exc.value.reason == sa.REASON_INVALID_SKILL
    assert not skills_root(profile).exists()
    assert not (profile.parent / "evil").exists()


# ── Ledger contract ──────────────────────────────────────────────────────


def test_ledger_defaults_to_the_target_personas_data_dir(profile: Path, promoted: Path):
    """Rule 4: the ledger is keyed to the persona the rows are ABOUT.

    No ``audit_path`` anywhere, so the resolver runs for real. #426 shipped
    the ambient-``config.DATA_DIR`` version of this and had to fix it; this
    module must never regress into it.
    """
    sa.assign_skill_to_persona(
        "sales", promoted, skill_name="deploy-checklist", **OPERATOR
    )

    target_ledger = profile / "data" / sa.LEDGER_FILENAME
    assert target_ledger.is_file()
    assert rows(target_ledger)[0]["persona_id"] == "sales"


def test_hostile_trigger_text_is_collapsed_and_capped(
    profile: Path, promoted: Path, ledger: Path
):
    """A pasted document must not turn one install into a log dump."""
    turn = {**OPERATOR, "trigger_text": "line one\nline two\n" + ("x" * 2000)}
    sa.assign_skill_to_persona(
        "sales", promoted, skill_name="deploy-checklist", audit_path=ledger, **turn
    )

    text = rows(ledger)[0]["trigger_text"]
    assert "\n" not in text
    assert text.startswith("line one line two")
    assert len(text) <= 400


def test_refusal_that_cannot_be_audited_raises_instead_of_a_polished_no(
    profile: Path, promoted: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """An unwritable ledger must not come back as an audited-looking refusal."""
    unwritable = tmp_path / "not-a-dir" / "ledger.jsonl"
    unwritable.parent.write_text("i am a file", encoding="utf-8")

    turn = {**OPERATOR, "actor_role": "viewer"}
    with pytest.raises(sa.SkillAssignmentAuditError):
        sa.assign_skill_to_persona(
            "sales",
            promoted,
            skill_name="deploy-checklist",
            audit_path=unwritable,
            **turn,
        )
    assert not skills_root(profile).exists()


def test_failed_install_audits_an_error_and_keeps_the_previous_skill(
    profile: Path, promoted: Path, ledger: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A failed re-assign must not cost the persona a skill it already had."""
    sa.assign_skill_to_persona(
        "sales", promoted, skill_name="deploy-checklist", audit_path=ledger, **OPERATOR
    )
    installed = skills_root(profile) / "deploy-checklist" / "SKILL.md"
    original = installed.read_bytes()

    updated = _make_source(tmp_path / "v2", "deploy-checklist", "Run the NEW checklist.")

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(sa.shutil, "copytree", boom)

    with pytest.raises(OSError, match="disk full"):
        sa.assign_skill_to_persona(
            "sales", updated, skill_name="deploy-checklist", audit_path=ledger, **OPERATOR
        )

    assert installed.read_bytes() == original
    assert rows(ledger)[-1]["reason"] == sa.REASON_INSTALL_FAILED


def test_restore_failure_preserves_the_backup_instead_of_deleting_it(
    profile: Path, promoted: Path, ledger: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """M2 (#429 round-2 MAJOR): the OLD test above raises from ``copytree``,
    BEFORE the previous install is ever displaced to ``backup`` — it cannot
    reach this path. Here the staging->target swap AND the restore
    (backup->target) BOTH fail, which is exactly the state where the old
    unconditional ``finally`` deleted ``backup`` regardless of whether the
    restore it was supposed to guard actually succeeded — leaving NEITHER
    the new skill NOR the old one installed. The old bytes must remain
    recoverable in ``backup`` when this happens."""
    sa.assign_skill_to_persona(
        "sales", promoted, skill_name="deploy-checklist", audit_path=ledger, **OPERATOR
    )
    installed = skills_root(profile) / "deploy-checklist" / "SKILL.md"
    original = installed.read_bytes()

    updated = _make_source(tmp_path / "v2", "deploy-checklist", "Run the NEW checklist.")

    real_replace = sa.os.replace
    calls: list[tuple[str, str]] = []

    def flaky_replace(src, dst, *args, **kwargs):
        calls.append((str(src), str(dst)))
        if len(calls) == 1:
            # Call #1: displace the existing install to `backup` — let this
            # one genuinely succeed so the state matches a real failure mid-swap.
            return real_replace(src, dst, *args, **kwargs)
        # Call #2 (staging -> target) and call #3 (the restore, backup ->
        # target) BOTH fail — the double-failure this fix targets.
        raise OSError("simulated double replace failure")

    monkeypatch.setattr(sa.os, "replace", flaky_replace)

    with pytest.raises(OSError, match="simulated double replace failure"):
        sa.assign_skill_to_persona(
            "sales", updated, skill_name="deploy-checklist", audit_path=ledger, **OPERATOR
        )

    assert len(calls) == 3, "expected displace + failed swap + failed restore"
    stage_root = skills_root(profile).parent
    backups = list(stage_root.glob(".skill-replaced-*"))
    assert backups, "backup directory was deleted despite the restore failing"
    assert (backups[0] / "SKILL.md").read_bytes() == original


# ── Physical-state readers ───────────────────────────────────────────────


def test_installed_skill_names_reads_the_directory_not_a_sidecar(
    profile: Path, promoted: Path, ledger: Path
):
    assert sa.installed_skill_names("sales") == ()

    sa.assign_skill_to_persona(
        "sales", promoted, skill_name="deploy-checklist", audit_path=ledger, **OPERATOR
    )
    assert sa.installed_skill_names("sales") == ("deploy-checklist",)

    # Rule 2: delete the tree behind the executor's back — the reader must
    # report the physical truth, not a remembered install.
    import shutil as _shutil

    _shutil.rmtree(skills_root(profile) / "deploy-checklist")
    assert sa.installed_skill_names("sales") == ()


def test_an_audit_failure_after_install_removes_the_persona_local_copy(
    profile: Path, promoted: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """#429 codex R7 BLOCKER: _install_tree committed the persona-local copy
    BEFORE append_audit_record; an audit failure raised SkillAssignmentAuditError
    while the skill stayed live in the persona's own index — the operator was
    told "could not be installed" over an active, unaudited skill. The local
    copy is now taken back before the raise, and the message says so."""
    def _boom(**kwargs):
        raise OSError("ledger unwritable")

    monkeypatch.setattr(sa, "append_audit_record", _boom)

    with pytest.raises(sa.SkillAssignmentAuditError) as exc_info:
        sa.assign_skill_to_persona(
            "sales",
            promoted / "SKILL.md",
            skill_name="deploy-checklist",
            audit_path=ledger,
            **OPERATOR,
        )

    # Nothing is live: the persona-local copy was removed with the failure.
    assert not (skills_root(profile) / "deploy-checklist").exists()
    assert "removed again" in str(exc_info.value)
    assert "nothing is installed" in str(exc_info.value)


def test_an_audit_failure_after_REPLACEMENT_says_the_prior_version_was_lost(
    profile: Path, promoted: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
):
    """The replacement arm of the same blocker: the prior local version's
    backup is already gone when the audit fails, so the receipt must SAY the
    previous version could not be retained — never imply a clean rollback."""
    # Pre-install a different version of the same skill.
    prior = _make_source(profile.parents[3] / "prior-src", "deploy-checklist", "OLD body.")
    sa.assign_skill_to_persona(
        "sales", prior / "SKILL.md", skill_name="deploy-checklist",
        audit_path=ledger, **OPERATOR,
    )
    assert "Run the checklist." not in (
        skills_root(profile) / "deploy-checklist" / "SKILL.md"
    ).read_text(encoding="utf-8")

    def _boom(**kwargs):
        raise OSError("ledger unwritable")

    monkeypatch.setattr(sa, "append_audit_record", _boom)

    with pytest.raises(sa.SkillAssignmentAuditError) as exc_info:
        sa.assign_skill_to_persona(
            "sales", promoted / "SKILL.md", skill_name="deploy-checklist",
            audit_path=ledger, **OPERATOR,
        )

    msg = str(exc_info.value)
    assert "removed again" in msg
    assert "could not be retained" in msg
    assert not (skills_root(profile) / "deploy-checklist").exists()
