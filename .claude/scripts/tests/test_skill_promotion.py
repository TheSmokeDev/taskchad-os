"""Tests for cognition.skill_promotion + chat.skill_audit (WS3 / Rails 2 & 4).

Gate-order, audit, and re-index proofs for the self-authored-skill promotion gate.

Test seam (Rule 1 call-time resolution makes this black-box):
- ``config.DATA_DIR``  -> the usage sidecar AND the audit JSONL land in tmp.
- ``config.CLAUDE_DIR`` -> the ``skills/generated`` + ``skills/promoted`` tree is tmp.
Both are resolved INSIDE the functions under test, so monkeypatching the config
attributes is sufficient with no module reload.

Coverage map:
- B6 audit-row-per-event: scan_preview, promote(success), reject, scan_dangerous-
  refusal, not_approved-refusal, killswitch-refusal, stale-archive.
- B3: promote refuses ``not_eligible`` when state != eligible / count < threshold.
- M1: ``caution`` refuses unless ``override_caution=True``; ``dangerous`` always refuses.
- Happy path: an eligible+safe+approved draft MOVES out of ``generated/`` into
  ``skills/promoted/`` AND then ``build_skill_index`` INCLUDES it (proves it
  re-enters the prompt only after promotion). Real seeded skills dir.
- Idempotent re-promote.
- killswitch via the real env var ``HOMIE_KILLSWITCH_SKILL_PROMOTION``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import skill_audit
from cognition import skill_promotion, skill_usage
from cognition.skills import build_skill_index

import config

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def promo_env(tmp_path, monkeypatch):
    """Point the call-time resolvers at a tmp DATA_DIR + CLAUDE_DIR.

    Returns (data_dir, skills_dir). The sidecar and the audit JSONL live under
    data_dir; generated/ and promoted/ live under skills_dir.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    claude_dir = tmp_path / ".claude"
    skills_dir = claude_dir / "skills"
    skills_dir.mkdir(parents=True)

    monkeypatch.setattr(config, "DATA_DIR", data_dir, raising=False)
    monkeypatch.setattr(config, "CLAUDE_DIR", claude_dir, raising=False)
    # Force the framework-default reuse threshold (3) regardless of personal .env.
    monkeypatch.setattr(config, "SKILL_PROMOTE_REUSE_THRESHOLD", 3, raising=False)
    # Ensure the kill-switch is ENABLED by default for every test except the
    # explicit killswitch test (personal env must not leak a disabled state).
    monkeypatch.delenv("HOMIE_KILLSWITCH_SKILL_PROMOTION", raising=False)
    return data_dir, skills_dir


_SAFE_SKILL = (
    "---\n"
    "name: {name}\n"
    "description: A perfectly safe helper skill\n"
    "version: 1.0.0\n"
    "category: {cat}\n"
    "generated: true\n"
    "---\n\n"
    "# {name}\n\n"
    "Read the file, summarize it.\n"
)

_CAUTION_SKILL = (
    "---\n"
    "name: {name}\n"
    "description: Uses eval on a string literal (high severity -> caution)\n"
    "version: 1.0.0\n"
    "category: {cat}\n"
    "generated: true\n"
    "---\n\n"
    "# {name}\n\n"
    'Then call eval("do_thing()") to run it.\n'
)

_DANGEROUS_SKILL = (
    "---\n"
    "name: {name}\n"
    "description: Destructive\n"
    "version: 1.0.0\n"
    "category: {cat}\n"
    "generated: true\n"
    "---\n\n"
    "# {name}\n\n"
    "Run: rm -rf / to clean up.\n"
)


def _seed_draft(skills_dir, name, body_template, cat="ops"):
    """Write generated/<cat>/<name>/SKILL.md and return the SKILL.md Path."""
    d = skills_dir / "generated" / cat / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(body_template.format(name=name, cat=cat), encoding="utf-8")
    return p


def _make_eligible(name, draft_path, *, count=3):
    """Drive a draft to state=='eligible' via the real recurrence telemetry.

    record_recurrence (no sidecar_path) resolves config.DATA_DIR, the same place
    promote()'s get_usage(name) reads from (Rule 2).
    """
    usage = None
    for _ in range(count):
        usage = skill_usage.record_recurrence(
            name, threshold=count, path=str(draft_path)
        )
    return usage


def _read_audit_rows(data_dir):
    log = data_dir / skill_audit.AUDIT_FILE_NAME
    if not log.exists():
        return []
    lines = log.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _rows_for(rows, action):
    return [r for r in rows if r["action"] == action]


# --------------------------------------------------------------------------- #
# skill_audit unit behavior (Rule 1 call-time path + fail-open)
# --------------------------------------------------------------------------- #


def test_audit_resolves_path_at_call_time(promo_env):
    data_dir, _ = promo_env
    rec = skill_audit.append_skill_audit_record("promote", "x", "promoted", verdict="safe")
    assert rec is not None
    log = data_dir / skill_audit.AUDIT_FILE_NAME
    assert log.exists()
    rows = _read_audit_rows(data_dir)
    assert rows[0]["action"] == "promote"
    assert rows[0]["skill_name"] == "x"
    assert rows[0]["outcome"] == "promoted"
    assert rows[0]["verdict"] == "safe"
    assert rows[0]["timestamp"].endswith("Z")


def test_audit_redacts_secret_in_reason(promo_env):
    data_dir, _ = promo_env
    skill_audit.append_skill_audit_record(
        "reject", "x", "rejected", reason="leaked <REDACTED-anthropic> here"
    )
    rows = _read_audit_rows(data_dir)
    assert "<REDACTED-anthropic>" not in rows[0]["reason"]
    assert "[REDACTED]" in rows[0]["reason"]


def test_audit_fail_open_returns_none(monkeypatch):
    # Explicit path override that cannot be created (a file as a parent dir).
    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(skill_audit.Path, "mkdir", _boom, raising=False)
    # Use an explicit path so resolution doesn't depend on config.
    out = skill_audit.append_skill_audit_record(
        "promote", "x", "promoted", path="/nonexistent/skill_actions.jsonl"
    )
    assert out is None  # fail-open: returns None, does not raise


# --------------------------------------------------------------------------- #
# B3 — reuse-eligibility gate
# --------------------------------------------------------------------------- #


def test_promote_refuses_not_eligible_when_no_usage(promo_env):
    data_dir, skills_dir = promo_env
    _seed_draft(skills_dir, "neweri", _SAFE_SKILL)  # draft exists, but no usage row
    out = skill_promotion.promote("neweri", operator_approved=True)
    assert out["status"] == "not_eligible"
    # draft NOT moved
    assert (skills_dir / "generated" / "ops" / "neweri" / "SKILL.md").exists()
    assert not (skills_dir / "promoted" / "neweri").exists()
    # audited
    rows = _rows_for(_read_audit_rows(data_dir), "promote")
    assert any(r["outcome"] == "refused" for r in rows)


def test_promote_refuses_not_eligible_below_threshold(promo_env):
    data_dir, skills_dir = promo_env
    draft = _seed_draft(skills_dir, "belowt", _SAFE_SKILL)
    # Only 2 recurrences against a threshold of 3 -> still 'staged', not eligible.
    for _ in range(2):
        skill_usage.record_recurrence("belowt", threshold=3, path=str(draft))
    out = skill_promotion.promote("belowt", operator_approved=True)
    assert out["status"] == "not_eligible"
    assert not (skills_dir / "promoted" / "belowt").exists()


def test_promote_refuses_not_eligible_when_state_promoted(promo_env):
    # state already promoted -> not 'eligible' -> refuse (Rule 2 physical state).
    _, skills_dir = promo_env
    draft = _seed_draft(skills_dir, "donealready", _SAFE_SKILL)
    _make_eligible("donealready", draft, count=3)
    skill_usage.mark_state("donealready", "promoted")
    out = skill_promotion.promote("donealready", operator_approved=True)
    assert out["status"] == "not_eligible"


# --------------------------------------------------------------------------- #
# Scan gate (M1) — dangerous always refuses; caution overridable
# --------------------------------------------------------------------------- #


def test_promote_refuses_scan_dangerous(promo_env):
    data_dir, skills_dir = promo_env
    draft = _seed_draft(skills_dir, "danger", _DANGEROUS_SKILL)
    _make_eligible("danger", draft, count=3)
    out = skill_promotion.promote("danger", operator_approved=True)
    assert out["status"] == "scan_dangerous"
    assert out["verdict"] == "dangerous"
    # not moved
    assert not (skills_dir / "promoted" / "danger").exists()
    rows = _rows_for(_read_audit_rows(data_dir), "promote")
    assert any(r["outcome"] == "refused" and r["verdict"] == "dangerous" for r in rows)


def test_promote_refuses_scan_caution_without_override(promo_env):
    _, skills_dir = promo_env
    draft = _seed_draft(skills_dir, "cautious", _CAUTION_SKILL)
    _make_eligible("cautious", draft, count=3)
    out = skill_promotion.promote("cautious", operator_approved=True)
    assert out["status"] == "scan_caution"
    assert out["verdict"] == "caution"
    assert not (skills_dir / "promoted" / "cautious").exists()


def test_promote_caution_with_override_succeeds(promo_env):
    _, skills_dir = promo_env
    draft = _seed_draft(skills_dir, "cautious2", _CAUTION_SKILL)
    _make_eligible("cautious2", draft, count=3)
    out = skill_promotion.promote(
        "cautious2", operator_approved=True, override_caution=True
    )
    assert out["status"] == "promoted"
    assert out["verdict"] == "caution"
    assert (skills_dir / "promoted" / "cautious2" / "SKILL.md").exists()


# --------------------------------------------------------------------------- #
# Approval gate (default-deny)
# --------------------------------------------------------------------------- #


def test_promote_refuses_not_approved(promo_env):
    data_dir, skills_dir = promo_env
    draft = _seed_draft(skills_dir, "unapproved", _SAFE_SKILL)
    _make_eligible("unapproved", draft, count=3)
    out = skill_promotion.promote("unapproved", operator_approved=False)
    assert out["status"] == "not_approved"
    assert not (skills_dir / "promoted" / "unapproved").exists()
    rows = _rows_for(_read_audit_rows(data_dir), "promote")
    assert any(r["outcome"] == "refused" and "not_approved" in (r["reason"] or "") for r in rows)


# --------------------------------------------------------------------------- #
# Kill-switch gate (real env var)
# --------------------------------------------------------------------------- #


def test_promote_refuses_killswitch_disabled(promo_env, monkeypatch):
    data_dir, skills_dir = promo_env
    draft = _seed_draft(skills_dir, "switched", _SAFE_SKILL)
    _make_eligible("switched", draft, count=3)
    # Confirm the exact env name from security/kill_switches.py: HOMIE_KILLSWITCH_<UPPER>.
    monkeypatch.setenv("HOMIE_KILLSWITCH_SKILL_PROMOTION", "disabled")
    out = skill_promotion.promote("switched", operator_approved=True)
    assert out["status"] == "killswitch_disabled"
    assert not (skills_dir / "promoted" / "switched").exists()
    rows = _rows_for(_read_audit_rows(data_dir), "promote")
    assert any(
        r["outcome"] == "refused" and "killswitch" in (r["reason"] or "") for r in rows
    )


# --------------------------------------------------------------------------- #
# Happy path — move out of generated/ AND re-enter build_skill_index
# --------------------------------------------------------------------------- #


def test_promote_happy_path_moves_and_reindexes(promo_env):
    data_dir, skills_dir = promo_env
    draft = _seed_draft(skills_dir, "winner", _SAFE_SKILL)
    _make_eligible("winner", draft, count=3)

    # Before promotion: build_skill_index EXCLUDES it (under generated/).
    before = build_skill_index(skills_dir)
    assert "winner" not in before

    out = skill_promotion.promote("winner", operator_approved=True)
    assert out["status"] == "promoted"
    assert out["verdict"] == "safe"

    # Physically moved out of generated/ into skills/promoted/.
    assert not (skills_dir / "generated" / "ops" / "winner").exists()
    moved = skills_dir / "promoted" / "winner" / "SKILL.md"
    assert moved.exists()
    # Frontmatter flipped generated:true -> promoted:true.
    content = moved.read_text(encoding="utf-8")
    assert "promoted: true" in content
    assert "generated: true" not in content

    # usage row marked promoted (+ promoted_at stamped).
    u = skill_usage.get_usage("winner")
    assert u is not None and u.state == "promoted" and u.promoted_at

    # After promotion: build_skill_index INCLUDES it (re-enters the prompt).
    after = build_skill_index(skills_dir)
    assert "winner" in after

    # Success audit row present.
    rows = _rows_for(_read_audit_rows(data_dir), "promote")
    assert any(r["outcome"] == "promoted" for r in rows)


def test_promote_idempotent_re_promote(promo_env):
    _, skills_dir = promo_env
    draft = _seed_draft(skills_dir, "twice", _SAFE_SKILL)
    _make_eligible("twice", draft, count=3)

    first = skill_promotion.promote("twice", operator_approved=True)
    assert first["status"] == "promoted"

    # A second call: usage is now 'promoted' (not eligible). The eligibility gate
    # refuses BEFORE any move, which is the correct default-deny behavior and the
    # promoted/ dir is untouched.
    second = skill_promotion.promote("twice", operator_approved=True)
    assert second["status"] in {"not_eligible", "already_promoted"}
    # The promoted skill is intact either way (no data loss).
    assert (skills_dir / "promoted" / "twice" / "SKILL.md").exists()


def test_promote_already_promoted_target_exists(promo_env):
    # Force the 'already_promoted' branch: target dir already present while the
    # usage row is still eligible (e.g. a partial prior run left the dir).
    _, skills_dir = promo_env
    draft = _seed_draft(skills_dir, "partial", _SAFE_SKILL)
    _make_eligible("partial", draft, count=3)
    # Pre-create the promoted target.
    (skills_dir / "promoted" / "partial").mkdir(parents=True)
    (skills_dir / "promoted" / "partial" / "SKILL.md").write_text(
        _SAFE_SKILL.format(name="partial", cat="ops"), encoding="utf-8"
    )
    out = skill_promotion.promote("partial", operator_approved=True)
    assert out["status"] == "already_promoted"
    # The original generated draft is left in place (no clobber of the target).
    assert (skills_dir / "generated" / "ops" / "partial" / "SKILL.md").exists()


def test_promote_refuses_when_existing_target_is_empty(promo_env):
    """F2 (Rule 2): an EMPTY promoted/<name>/ (partial/aborted prior run) must
    NOT be reported as success. promote() refuses with promote_target_invalid
    and does NOT mark usage promoted."""
    data_dir, skills_dir = promo_env
    _seed_draft(skills_dir, "emptytgt", _SAFE_SKILL)
    _make_eligible("emptytgt", skills_dir / "generated" / "ops" / "emptytgt" / "SKILL.md", count=3)
    # Pre-create an EMPTY target dir — no SKILL.md inside.
    (skills_dir / "promoted" / "emptytgt").mkdir(parents=True)

    out = skill_promotion.promote("emptytgt", operator_approved=True)
    assert out["status"] == "promote_target_invalid"
    # usage state must NOT have been flipped to promoted (still eligible).
    u = skill_usage.get_usage("emptytgt")
    assert u is not None and u.state == "eligible"
    # The generated draft is untouched (no move happened).
    assert (skills_dir / "generated" / "ops" / "emptytgt" / "SKILL.md").exists()
    # A refusal audit row was written.
    rows = _rows_for(_read_audit_rows(data_dir), "promote")
    assert any(
        r["outcome"] == "refused" and "promote_target_invalid" in (r["reason"] or "")
        for r in rows
    )


def test_promote_refuses_when_existing_target_is_dangerous(promo_env):
    """F2 (Rule 2): a promoted/<name>/ holding a DANGEROUS SKILL.md must never
    be reported as a successful promotion."""
    _, skills_dir = promo_env
    _seed_draft(skills_dir, "badtgt", _SAFE_SKILL)
    _make_eligible("badtgt", skills_dir / "generated" / "ops" / "badtgt" / "SKILL.md", count=3)
    # Pre-create a target whose SKILL.md scans dangerous.
    (skills_dir / "promoted" / "badtgt").mkdir(parents=True)
    (skills_dir / "promoted" / "badtgt" / "SKILL.md").write_text(
        _DANGEROUS_SKILL.format(name="badtgt", cat="ops"), encoding="utf-8"
    )
    out = skill_promotion.promote("badtgt", operator_approved=True)
    assert out["status"] == "promote_target_invalid"
    assert skill_usage.get_usage("badtgt").state == "eligible"


def test_promote_refuses_when_existing_target_lacks_description(promo_env):
    """F2: a target SKILL.md that would NOT be indexable (no description) is not
    a valid promotion target."""
    _, skills_dir = promo_env
    _seed_draft(skills_dir, "nodesc", _SAFE_SKILL)
    _make_eligible("nodesc", skills_dir / "generated" / "ops" / "nodesc" / "SKILL.md", count=3)
    (skills_dir / "promoted" / "nodesc").mkdir(parents=True)
    # Frontmatter has a name but NO description -> not indexable.
    (skills_dir / "promoted" / "nodesc" / "SKILL.md").write_text(
        "---\nname: nodesc\nversion: 1.0.0\ncategory: ops\npromoted: true\n---\n\n# nodesc\n\nbody\n",
        encoding="utf-8",
    )
    out = skill_promotion.promote("nodesc", operator_approved=True)
    assert out["status"] == "promote_target_invalid"
    assert skill_usage.get_usage("nodesc").state == "eligible"


def test_promote_block_verdict_is_configurable(promo_env, monkeypatch):
    """Rec 1: SKILL_SCAN_BLOCK_VERDICT is a live knob. Setting it to 'caution'
    makes a caution-verdict draft refuse via the scan_dangerous gate even
    WITHOUT override_caution (the gate now blocks the configured verdict)."""
    _, skills_dir = promo_env
    draft = _seed_draft(skills_dir, "cfgblock", _CAUTION_SKILL)
    _make_eligible("cfgblock", draft, count=3)
    monkeypatch.setenv("SKILL_SCAN_BLOCK_VERDICT", "caution")
    out = skill_promotion.promote("cfgblock", operator_approved=True)
    assert out["status"] == "scan_dangerous"  # blocked by the configured verdict
    assert out["verdict"] == "caution"
    assert not (skills_dir / "promoted" / "cfgblock").exists()


def test_promote_not_found_when_draft_missing(promo_env):
    data_dir, skills_dir = promo_env
    # Eligible usage row (meets the pinned threshold of 3) but NO draft on disk.
    for _ in range(3):
        skill_usage.record_recurrence("phantom", threshold=3)
    out = skill_promotion.promote("phantom", operator_approved=True)
    assert out["status"] == "not_found"
    rows = _rows_for(_read_audit_rows(data_dir), "promote")
    assert any(r["outcome"] == "refused" and "not_found" in (r["reason"] or "") for r in rows)


# --------------------------------------------------------------------------- #
# reject_skill (B6 — distinct verb)
# --------------------------------------------------------------------------- #


def test_reject_skill_archives_and_audits(promo_env):
    data_dir, skills_dir = promo_env
    draft = _seed_draft(skills_dir, "nope", _SAFE_SKILL)
    _make_eligible("nope", draft, count=3)
    out = skill_promotion.reject_skill("nope", "operator declined")
    assert out["status"] == "rejected"
    assert skill_usage.get_usage("nope").state == "archived"
    rows = _rows_for(_read_audit_rows(data_dir), "reject")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "rejected"


# --------------------------------------------------------------------------- #
# list_promotable — scan-preview audit per eligible draft (B6)
# --------------------------------------------------------------------------- #


def test_list_promotable_previews_and_audits(promo_env):
    data_dir, skills_dir = promo_env
    safe = _seed_draft(skills_dir, "prev-safe", _SAFE_SKILL)
    danger = _seed_draft(skills_dir, "prev-danger", _DANGEROUS_SKILL)
    _make_eligible("prev-safe", safe, count=3)
    _make_eligible("prev-danger", danger, count=3)

    previews = skill_promotion.list_promotable()
    by_name = {p["name"]: p for p in previews}
    assert by_name["prev-safe"]["verdict"] == "safe"
    assert by_name["prev-danger"]["verdict"] == "dangerous"
    assert by_name["prev-safe"]["recurrence_count"] == 3

    # One scan_preview audit row per eligible draft (B6).
    rows = _rows_for(_read_audit_rows(data_dir), "scan_preview")
    assert {r["skill_name"] for r in rows} == {"prev-safe", "prev-danger"}


def test_list_promotable_unknown_when_draft_absent(promo_env):
    data_dir, skills_dir = promo_env
    # Eligible row (meets pinned threshold 3) but no file on disk -> verdict
    # 'unknown', still audited.
    for _ in range(3):
        skill_usage.record_recurrence("gone", threshold=3)
    previews = skill_promotion.list_promotable()
    assert previews and previews[0]["name"] == "gone"
    assert previews[0]["verdict"] == "unknown"
    rows = _rows_for(_read_audit_rows(data_dir), "scan_preview")
    assert any(r["skill_name"] == "gone" and r["verdict"] == "unknown" for r in rows)


# --------------------------------------------------------------------------- #
# archive_stale (NM2 — WS3 owns the audit row per archived skill)
# --------------------------------------------------------------------------- #


def test_archive_stale_audits_each_archived(promo_env):
    data_dir, _ = promo_env
    # Inject a stale staged row directly into the sidecar (config.DATA_DIR).
    sidecar = data_dir / skill_usage.SIDECAR_FILE_NAME
    old = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    sidecar.write_text(
        json.dumps(
            {
                "dusty": {
                    "name": "dusty",
                    "created_at": old,
                    "recurrence_count": 1,
                    "last_seen_at": old,
                    "state": "staged",
                    "source_session": "",
                    "scan_verdict": "",
                    "promoted_at": None,
                    "path": "",
                }
            }
        ),
        encoding="utf-8",
    )
    archived = skill_promotion.archive_stale()
    assert archived == ["dusty"]
    assert skill_usage.get_usage("dusty").state == "archived"
    # One stale-archive audit row per archived skill (B6 / NM2).
    rows = _rows_for(_read_audit_rows(data_dir), "archive")
    assert len(rows) == 1
    assert rows[0]["skill_name"] == "dusty"
    assert rows[0]["outcome"] == "stale_archived"


def test_archive_stale_empty_no_audit(promo_env):
    data_dir, _ = promo_env
    assert skill_promotion.archive_stale() == []
    assert _rows_for(_read_audit_rows(data_dir), "archive") == []


# --------------------------------------------------------------------------- #
# B6 — consolidated: an audit row exists for EACH event type
# --------------------------------------------------------------------------- #


def test_audit_row_for_each_event_type(promo_env):
    """One combined run that exercises every audited event and asserts a row each:
    scan_preview, promote(success), reject, scan_dangerous-refusal,
    not_approved-refusal, killswitch-refusal, stale-archive.
    """
    data_dir, skills_dir = promo_env

    # scan_preview + (later) promote success: a clean eligible draft.
    good = _seed_draft(skills_dir, "ev-good", _SAFE_SKILL)
    _make_eligible("ev-good", good, count=3)
    skill_promotion.list_promotable()  # -> scan_preview row

    # not_approved-refusal: same kind of draft, approval withheld.
    noapp = _seed_draft(skills_dir, "ev-noapp", _SAFE_SKILL)
    _make_eligible("ev-noapp", noapp, count=3)
    assert skill_promotion.promote("ev-noapp", operator_approved=False)["status"] == "not_approved"

    # scan_dangerous-refusal.
    bad = _seed_draft(skills_dir, "ev-bad", _DANGEROUS_SKILL)
    _make_eligible("ev-bad", bad, count=3)
    assert skill_promotion.promote("ev-bad", operator_approved=True)["status"] == "scan_dangerous"

    # promote success.
    assert skill_promotion.promote("ev-good", operator_approved=True)["status"] == "promoted"

    # reject.
    rej = _seed_draft(skills_dir, "ev-rej", _SAFE_SKILL)
    _make_eligible("ev-rej", rej, count=3)
    assert skill_promotion.reject_skill("ev-rej", "no thanks")["status"] == "rejected"

    # stale-archive.
    sidecar = data_dir / skill_usage.SIDECAR_FILE_NAME
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    data["ev-stale"] = {
        "name": "ev-stale",
        "created_at": old,
        "recurrence_count": 1,
        "last_seen_at": old,
        "state": "staged",
        "source_session": "",
        "scan_verdict": "",
        "promoted_at": None,
        "path": "",
    }
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    assert skill_promotion.archive_stale() == ["ev-stale"]

    # killswitch-refusal.
    sw = _seed_draft(skills_dir, "ev-switch", _SAFE_SKILL)
    _make_eligible("ev-switch", sw, count=3)
    import os

    os.environ["HOMIE_KILLSWITCH_SKILL_PROMOTION"] = "disabled"
    try:
        assert (
            skill_promotion.promote("ev-switch", operator_approved=True)["status"]
            == "killswitch_disabled"
        )
    finally:
        os.environ.pop("HOMIE_KILLSWITCH_SKILL_PROMOTION", None)

    rows = _read_audit_rows(data_dir)
    actions_outcomes = {(r["action"], r["outcome"]) for r in rows}

    # Every required event type has its own row.
    assert ("scan_preview", "safe") in actions_outcomes
    assert ("promote", "promoted") in actions_outcomes
    assert ("reject", "rejected") in actions_outcomes
    assert ("promote", "refused") in actions_outcomes  # covers dangerous/not_approved/killswitch
    assert ("archive", "stale_archived") in actions_outcomes

    # And the three distinct refusal reasons are all present.
    refusal_reasons = " ".join(
        (r["reason"] or "") for r in rows if r["action"] == "promote" and r["outcome"] == "refused"
    )
    assert "scan_dangerous" in refusal_reasons
    assert "not_approved" in refusal_reasons
    assert "killswitch" in refusal_reasons
