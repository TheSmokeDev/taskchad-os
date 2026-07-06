"""Tests for cofounder v2 WS1 — the persona seeder, the portfolio digest,
and the cabinet portfolio-context injection seam.

Path map (one test per distinct path, adversarial first):
  Seeder (cofounder/persona.py)
  - persona_mutation kill switch = refused + counted, ZERO filesystem writes
  - fresh seed = profile created via lifecycle (identity inventory + config
    with persona/cabinet/learning blocks), cofounder SOUL/MEMORY authored
  - second run = unchanged (idempotent)
  - operator-edited SOUL preserved; --force overwrites
  - operator config values WIN (portfolio_context: false stays false;
    unknown operator keys preserved)
  - malformed config.yaml = error outcome, file NOT wiped
  - dry run = reports changes, writes nothing
  - roster integration: seeded profile is cabinet-eligible with zero tools
  Digest (cofounder/briefing.build_portfolio_digest)
  - full vault = agenda (NEWEST picked) + projects + repos, propose-only line
  - empty vault = "" (no digest block)
  - truncation respects max_chars
  Injection (cabinet/text_orchestrator._profile_execution_context)
  - portfolio_context: true = digest block in system_prompt
  - flag absent = no digest block
  - digest builder raising = bare turn, no exception
  Validator (personas/services.validate_config_dict)
  - cabinet.portfolio_context must be bool; bool accepted
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cofounder import briefing as briefing_mod
from cofounder import persona as persona_mod
from personas import services as personas_services
from security import kill_switches


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_COFOUNDER", raising=False)
    monkeypatch.delenv("COFOUNDER_PROJECTS_DIR", raising=False)
    yield


@pytest.fixture(autouse=True)
def reset_counters():
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()
    yield
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()


@pytest.fixture
def homie_root(tmp_path, monkeypatch):
    root = tmp_path / ".homie"
    monkeypatch.setenv("HOMIE_HOME", str(root))
    return root


def _profile_root(homie_root: Path) -> Path:
    return homie_root / "profiles" / persona_mod.COFOUNDER_PERSONA_ID


def _config_path(homie_root: Path) -> Path:
    return _profile_root(homie_root) / "config.yaml"


def _read_config(homie_root: Path) -> dict:
    return yaml.safe_load(_config_path(homie_root).read_text(encoding="utf-8"))


# =============================================================================
# Seeder
# =============================================================================


def test_kill_switch_refuses_and_writes_nothing(monkeypatch, homie_root):
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    result = persona_mod.seed_cofounder_persona()
    assert result.outcome == persona_mod.OUTCOME_REFUSED
    assert result.exit_code == 0
    assert kill_switches.get_refusal_counters()["persona_mutation"] == 1
    assert not homie_root.exists()


def test_fresh_seed_creates_profile_config_and_identity(homie_root):
    result = persona_mod.seed_cofounder_persona()
    assert result.outcome == persona_mod.OUTCOME_CREATED
    assert result.profile_created is True

    cfg = _read_config(homie_root)
    assert cfg["persona"]["id"] == "cofounder"
    assert cfg["persona"]["display_name"] == persona_mod.COFOUNDER_DISPLAY_NAME
    assert cfg["persona"]["role"] == persona_mod.COFOUNDER_ROLE
    assert isinstance(cfg["cabinet"], dict)  # presence == cabinet-eligible
    assert cfg["cabinet"]["tools"] == []  # default-deny cabinet tools
    assert cfg["cabinet"]["portfolio_context"] is True
    assert cfg["learning"]["enabled"] is True

    memory = _profile_root(homie_root) / "memory"
    soul = (memory / "SOUL.md").read_text(encoding="utf-8")
    assert soul == persona_mod.COFOUNDER_SOUL
    assert "Propose first" in soul
    assert (memory / "MEMORY.md").read_text(encoding="utf-8") == (
        persona_mod.COFOUNDER_MEMORY
    )
    # Lifecycle scaffold inventory came along (spot-check).
    assert (memory / "GOALS.md").exists()


def test_second_run_is_unchanged(homie_root):
    persona_mod.seed_cofounder_persona()
    result = persona_mod.seed_cofounder_persona()
    assert result.outcome == persona_mod.OUTCOME_UNCHANGED
    assert result.config_changes == []
    assert result.identity_written == []


def test_operator_soul_preserved_unless_forced(homie_root):
    persona_mod.seed_cofounder_persona()
    soul_path = _profile_root(homie_root) / "memory" / "SOUL.md"
    soul_path.write_text("# My hand-tuned cofounder\n", encoding="utf-8")

    result = persona_mod.seed_cofounder_persona()
    assert result.identity_written == []
    assert soul_path.read_text(encoding="utf-8") == "# My hand-tuned cofounder\n"

    result = persona_mod.seed_cofounder_persona(force=True)
    assert "SOUL.md" in result.identity_written
    assert soul_path.read_text(encoding="utf-8") == persona_mod.COFOUNDER_SOUL


def test_operator_config_values_win(homie_root):
    persona_mod.seed_cofounder_persona()
    config_path = _config_path(homie_root)
    cfg = _read_config(homie_root)
    cfg["cabinet"]["portfolio_context"] = False
    cfg["operator_custom"] = {"keep": "me"}
    config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    result = persona_mod.seed_cofounder_persona()
    assert result.config_changes == []
    after = _read_config(homie_root)
    assert after["cabinet"]["portfolio_context"] is False
    assert after["operator_custom"] == {"keep": "me"}


def test_malformed_config_is_error_never_wiped(homie_root):
    persona_mod.seed_cofounder_persona()
    config_path = _config_path(homie_root)
    config_path.write_text("cabinet: [", encoding="utf-8")  # broken yaml

    result = persona_mod.seed_cofounder_persona()
    assert result.outcome == persona_mod.OUTCOME_ERROR
    assert result.exit_code == 1
    assert config_path.read_text(encoding="utf-8") == "cabinet: ["


def test_dry_run_reports_but_writes_nothing(homie_root):
    result = persona_mod.seed_cofounder_persona(dry_run=True)
    assert result.outcome == persona_mod.OUTCOME_CREATED
    assert result.config_changes  # would-change report
    assert not homie_root.exists()


def test_seeded_profile_is_cabinet_eligible_with_zero_tools(homie_root):
    persona_mod.seed_cofounder_persona()
    from cabinet import text_orchestrator

    roster = text_orchestrator._roster_from_personas()
    cofounder = [a for a in roster if a.id == "cofounder"]
    assert len(cofounder) == 1
    assert cofounder[0].name == persona_mod.COFOUNDER_DISPLAY_NAME
    assert cofounder[0].tools == []


# =============================================================================
# Digest
# =============================================================================


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    agendas = vault / "cofounder" / "agendas"
    agendas.mkdir(parents=True)
    (agendas / "AGENDA-2026-07-04.md").write_text(
        "---\ndate: 2026-07-04\n---\n# old agenda\nOLD_MARKER", encoding="utf-8"
    )
    (agendas / "AGENDA-2026-07-05.md").write_text(
        "---\ndate: 2026-07-05\n---\n# Co-Founder Agenda\nNEW_MARKER",
        encoding="utf-8",
    )
    (vault / "cofounder" / "proj-a.md").write_text(
        "---\nstatus: building\niterations: 2\nmax_iterations: 50\n---\n"
        "# proj-a\n\n## Spec\ns\n\n## Plan / Working Memory\np\n\n"
        "## Activity Log\na\n",
        encoding="utf-8",
    )
    (vault / "REPOSITORIES.md").write_text(
        "# Index\n\n## Active Repositories\n\n"
        "| Slug | GitHub | Visibility | Default branch | Local path | Archon | Page |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| alpha-repo | x | private | master | C:\\r\\alpha | yes | p |\n",
        encoding="utf-8",
    )
    return vault


def test_digest_carries_newest_agenda_projects_and_repos(tmp_path):
    vault = _vault(tmp_path)
    digest = briefing_mod.build_portfolio_digest(
        vault, projects_dir=vault / "cofounder"
    )
    assert digest.startswith("## Portfolio Digest")
    assert "PROPOSALS" in digest  # propose-only reminder
    assert "NEW_MARKER" in digest
    assert "OLD_MARKER" not in digest  # newest agenda only
    assert "date: 2026-07-05" not in digest  # frontmatter stripped
    assert "proj-a" in digest
    assert "alpha-repo" in digest


def test_digest_empty_vault_is_empty_string(tmp_path):
    nowhere = tmp_path / "nowhere"
    assert (
        briefing_mod.build_portfolio_digest(
            nowhere, projects_dir=nowhere / "cofounder"
        )
        == ""
    )


def test_digest_truncates_to_max_chars(tmp_path):
    vault = _vault(tmp_path)
    digest = briefing_mod.build_portfolio_digest(
        vault, projects_dir=vault / "cofounder", max_chars=120
    )
    assert "[digest truncated]" in digest
    # header + capped body stays bounded (cap applies to the body only)
    assert len(digest) < 400


def test_digest_follows_projects_dir_knob_like_the_writer(tmp_path, monkeypatch):
    """Writer/reader agreement: with COFOUNDER_PROJECTS_DIR overridden, the
    digest reads the SAME dir the agenda pass writes — never the legacy
    <memory_dir>/cofounder derivation (the two-parallel-readers bug)."""
    vault = _vault(tmp_path)  # has a decoy agenda under <vault>/cofounder/
    custom = tmp_path / "elsewhere" / "projects"
    (custom / "agendas").mkdir(parents=True)
    (custom / "agendas" / "AGENDA-2026-07-06.md").write_text(
        "---\ndate: 2026-07-06\n---\n# agenda\nCUSTOM_DIR_MARKER",
        encoding="utf-8",
    )
    monkeypatch.setenv("COFOUNDER_PROJECTS_DIR", str(custom))

    import config

    assert config.get_cofounder_settings().projects_dir == custom  # the writer
    digest = briefing_mod.build_portfolio_digest(vault)  # reader, no override
    assert "CUSTOM_DIR_MARKER" in digest
    assert "NEW_MARKER" not in digest  # decoy vault agenda NOT read


# =============================================================================
# Injection seam
# =============================================================================


def _make_profile(homie_root: Path, persona_id: str, *, extra_cabinet: str = "") -> None:
    profile_root = homie_root / "profiles" / persona_id
    for subdir in ("run", "skills", "memory"):
        (profile_root / subdir).mkdir(parents=True, exist_ok=True)
    (profile_root / "config.yaml").write_text(
        "\n".join(
            [
                "persona:",
                f"  display_name: {persona_id.title()}",
                "  role: test role",
                "cabinet:",
                "  tools: []",
                *([extra_cabinet] if extra_cabinet else []),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (profile_root / "memory" / "SOUL.md").write_text(
        f"# {persona_id} soul", encoding="utf-8"
    )


def test_portfolio_context_true_injects_digest(homie_root, monkeypatch):
    _make_profile(homie_root, "ceo", extra_cabinet="  portfolio_context: true")
    import cofounder.briefing as cb
    from cabinet import text_orchestrator

    monkeypatch.setattr(
        cb, "build_portfolio_digest", lambda memory_dir, **kw: "## Portfolio Digest\nDIGEST_MARKER"
    )
    ctx = text_orchestrator._profile_execution_context("ceo")
    assert ctx.error is None
    assert ctx.system_prompt is not None
    assert "DIGEST_MARKER" in ctx.system_prompt


def test_no_flag_means_no_digest(homie_root, monkeypatch):
    _make_profile(homie_root, "sales")
    import cofounder.briefing as cb
    from cabinet import text_orchestrator

    monkeypatch.setattr(
        cb,
        "build_portfolio_digest",
        lambda memory_dir, **kw: pytest.fail("digest built without the flag"),
    )
    ctx = text_orchestrator._profile_execution_context("sales")
    assert ctx.error is None
    assert "Portfolio Digest" not in (ctx.system_prompt or "")


def test_digest_failure_is_a_bare_turn(homie_root, monkeypatch):
    _make_profile(homie_root, "ceo", extra_cabinet="  portfolio_context: true")
    import cofounder.briefing as cb
    from cabinet import text_orchestrator

    def explode(memory_dir, **kw):
        raise RuntimeError("vault offline")

    monkeypatch.setattr(cb, "build_portfolio_digest", explode)
    ctx = text_orchestrator._profile_execution_context("ceo")
    assert ctx.error is None  # turn proceeds
    assert "Portfolio Digest" not in (ctx.system_prompt or "")


# =============================================================================
# Validator
# =============================================================================


def test_validator_rejects_non_bool_portfolio_context():
    with pytest.raises(personas_services.ConfigShapeError):
        personas_services.validate_config_dict(
            {"cabinet": {"portfolio_context": "yes"}}
        )


def test_validator_accepts_bool_portfolio_context():
    personas_services.validate_config_dict({"cabinet": {"portfolio_context": True}})
    personas_services.validate_config_dict({"cabinet": {"portfolio_context": False}})
