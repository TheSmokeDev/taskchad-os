"""Tests for the co-founder v2 morning agenda pass (cofounder/agenda.py).

Path map (one test per distinct path, adversarial first):
  Gates
  - kill switch disabled = refused + counted, zero scan, zero LLM
  - COFOUNDER_AGENDA_ENABLED default false = disabled, zero scan
  - before agenda hour = not-due; already produced today = not-due
  - failed-attempt cap reached = attempts-capped, zero LLM
  - --force bypasses the due check but NEVER the enabled flag
  Scan (fail-open)
  - empty scan (no repos AND no personas) = scan-empty, zero LLM
  - list_tracked_repos: missing index / missing section / happy rows
  - _available_personas: valid parsed, broken config skipped,
    persona-less config skipped
  Parse (strict object, fail-closed lines)
  - garbage output / unknown top-level key / items-not-a-list = parse error
  - unknown persona dropped; unknown repo dropped; null repo kept
  - empty task dropped; all-invalid = parse error; cap truncates;
    bad priority defaults to 2
  Pass outcomes
  - proposal failure = proposal-failed + attempt recorded, NO artifact,
    NO card; cap reached across passes = attempts-capped
  - happy pass = artifact in agendas/ subdir (banner + lines), state
    stamped, ONE card without buttons
  - v1 project discovery NEVER sees an agenda artifact
  - dry run = LLM called, zero writes, zero card, zero state change
  - card fail-open; COFOUNDER_AGENDA_NOTIFY=false mutes card;
    empty COFOUNDER_NOTIFY_LEVELS is the global mute and wins
  - whole-pass wrap: unexpected failure = error outcome, exit code 1
  Notify seam (additive param)
  - with_buttons=False drops reply_markup; default keeps v1 buttons
  Identity + operator model + live context (T2)
  - parity: every new source absent = the pre-T2 prompt, byte for byte
  - section order; GOALS last, labeled with its own date when it has one
  - per source (identity / operator model / live context / recent sessions
    / active tracker): present, absent, capped, broken-fails-open
  - empty self-model corpus = absent source, never a "nothing here" line
  - tracker resolves both the documented ## Now and today's nested ### Now
  Config (Rule 1)
  - COFOUNDER_AGENDA_* resolved from env at call time
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import config
from cofounder import agenda as agenda_mod
from cofounder import notify as notify_mod
from cofounder import project_model, repos
from cofounder import state as state_mod
from security import kill_switches

AGENDA_ENV_KEYS = (
    "COFOUNDER_AGENDA_ENABLED",
    "COFOUNDER_AGENDA_HOUR",
    "COFOUNDER_AGENDA_MAX_ITEMS",
    "COFOUNDER_AGENDA_MAX_ATTEMPTS",
    "COFOUNDER_AGENDA_NOTIFY",
    "COFOUNDER_ENABLED",
    "COFOUNDER_PROJECTS_DIR",
    "COFOUNDER_NOTIFY_LEVELS",
    "HOMIE_KILLSWITCH_COFOUNDER",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_IDS",
)

MORNING = datetime(2026, 7, 5, 9, 30)  # local, past the default hour 7
TODAY = "2026-07-05"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No agenda/kill-switch/Telegram env leaks from the operator .env."""
    for key in AGENDA_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture(autouse=True)
def reset_counters():
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()
    yield
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()


def _settings(tmp_path: Path, **overrides):
    """Real CofounderSettings with a tmp projects dir (env already clean)."""
    return config.get_cofounder_settings(
        projects_dir=tmp_path / "cofounder", **overrides
    )


def _agenda_settings(**overrides):
    defaults = dict(enabled=True, agenda_hour=7, max_items=5, max_attempts=3, notify=True)
    defaults.update(overrides)
    return config.get_cofounder_agenda_settings(**defaults)


def _scan(personas=("sales",), repos_=("YourProduct",)):
    return {
        "repos": list(repos_),
        "repo_pages": {},
        "goals": "",
        "projects": [],
        "personas": [
            {"id": p, "name": p.title(), "role": "dept head"} for p in personas
        ],
    }


def _valid_raw(items=None, summary="Portfolio looks healthy."):
    if items is None:
        items = [
            {
                "persona": "sales",
                "repo": "YourProduct",
                "task": "Follow up the three open demo leads",
                "why": "Two go stale tomorrow",
                "priority": 1,
            }
        ]
    return json.dumps({"summary": summary, "items": items})


def _recorder():
    calls: list[dict] = []

    def notify(project, text, level, *, settings=None, with_buttons=True):
        calls.append(
            {
                "slug": getattr(project, "slug", None),
                "text": text,
                "level": level,
                "with_buttons": with_buttons,
                "settings": settings,
            }
        )
        return True

    return calls, notify


def _run(tmp_path, monkeypatch=None, scan=None, **kwargs):
    """run_agenda_pass with canned scan + injected seams (happy defaults)."""
    if monkeypatch is not None:
        monkeypatch.setattr(
            agenda_mod, "build_portfolio_scan", lambda settings: scan or _scan()
        )
    kwargs.setdefault("settings", _settings(tmp_path))
    kwargs.setdefault("agenda_settings", _agenda_settings())
    kwargs.setdefault("state_file", tmp_path / "state.json")
    kwargs.setdefault("now", MORNING)
    kwargs.setdefault("propose", lambda prompt: _valid_raw())
    if "notify" not in kwargs:
        _, kwargs["notify"] = _recorder()
    return agenda_mod.run_agenda_pass(**kwargs)


def _forbid(reason):
    def hook(*args, **kwargs):
        pytest.fail(reason)

    return hook


# =============================================================================
# Gates
# =============================================================================


def test_kill_switch_refuses_counts_and_skips_scan(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER", "disabled")
    monkeypatch.setattr(
        agenda_mod, "build_portfolio_scan", _forbid("scan ran past the kill switch")
    )
    result = agenda_mod.run_agenda_pass(
        settings=_settings(tmp_path),
        agenda_settings=_agenda_settings(),
        state_file=tmp_path / "state.json",
        propose=_forbid("LLM ran past the kill switch"),
    )
    assert result.outcome == agenda_mod.OUTCOME_REFUSED
    assert result.exit_code == 0
    assert kill_switches.get_refusal_counters()["cofounder"] == 1


def test_disabled_by_default_no_scan(monkeypatch, tmp_path):
    monkeypatch.setattr(
        agenda_mod, "build_portfolio_scan", _forbid("scan ran while disabled")
    )
    result = agenda_mod.run_agenda_pass(
        settings=_settings(tmp_path),
        state_file=tmp_path / "state.json",
        propose=_forbid("LLM ran while disabled"),
    )
    assert result.outcome == agenda_mod.OUTCOME_DISABLED


def test_before_agenda_hour_not_due(monkeypatch, tmp_path):
    result = _run(
        tmp_path,
        monkeypatch,
        now=datetime(2026, 7, 5, 6, 59),
        propose=_forbid("LLM ran before the agenda hour"),
    )
    assert result.outcome == agenda_mod.OUTCOME_NOT_DUE


def test_already_produced_today_not_due(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    state_mod.save_state({"agenda": {"last_date": TODAY}}, state_file)
    result = _run(
        tmp_path,
        monkeypatch,
        state_file=state_file,
        propose=_forbid("LLM ran twice in one day"),
    )
    assert result.outcome == agenda_mod.OUTCOME_NOT_DUE


def test_attempt_cap_blocks_the_llm(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    state_mod.save_state({"agenda": {"attempts": {TODAY: 3}}}, state_file)
    result = _run(
        tmp_path,
        monkeypatch,
        state_file=state_file,
        propose=_forbid("LLM ran past the attempt cap"),
    )
    assert result.outcome == agenda_mod.OUTCOME_ATTEMPTS_CAPPED


def test_force_bypasses_due_check_not_enabled_flag(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    state_mod.save_state({"agenda": {"last_date": TODAY}}, state_file)
    calls, notify = _recorder()
    result = _run(
        tmp_path, monkeypatch, state_file=state_file, force=True, notify=notify
    )
    assert result.outcome == agenda_mod.OUTCOME_COMPLETED

    # force can never override the enabled flag (dormant stays dormant).
    result = agenda_mod.run_agenda_pass(
        force=True,
        settings=_settings(tmp_path),
        state_file=state_file,
        propose=_forbid("LLM ran while disabled despite force"),
    )
    assert result.outcome == agenda_mod.OUTCOME_DISABLED


# =============================================================================
# Scan (fail-open)
# =============================================================================


def test_empty_scan_skips_the_llm(monkeypatch, tmp_path):
    result = _run(
        tmp_path,
        monkeypatch,
        scan=_scan(personas=(), repos_=()),
        propose=_forbid("LLM ran on an empty scan"),
    )
    assert result.outcome == agenda_mod.OUTCOME_SCAN_EMPTY


def test_list_tracked_repos_missing_index_is_empty(tmp_path):
    assert repos.list_tracked_repos(memory_dir=tmp_path) == []


def test_list_tracked_repos_missing_section_is_empty(tmp_path):
    (tmp_path / "REPOSITORIES.md").write_text("# Index\n\nno table\n", encoding="utf-8")
    assert repos.list_tracked_repos(memory_dir=tmp_path) == []


def test_list_tracked_repos_reads_table_rows(tmp_path):
    (tmp_path / "REPOSITORIES.md").write_text(
        "# Index\n\n## Active Repositories\n\n"
        "| Slug | GitHub | Visibility | Default branch | Local path | Archon | Page |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| YourProduct | x | private | master | C:\\r\\YourProduct | yes | p |\n"
        "| YourBusiness | x | private | main | C:\\r\\YourBusiness | yes | p |\n",
        encoding="utf-8",
    )
    assert repos.list_tracked_repos(memory_dir=tmp_path) == ["YourProduct", "YourBusiness"]


def _write_profile(profiles: Path, name: str, config_obj) -> None:
    directory = profiles / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.yaml").write_text(
        config_obj if isinstance(config_obj, str) else yaml.safe_dump(config_obj),
        encoding="utf-8",
    )


def _write_index(tmp_path: Path, *rows: str) -> None:
    (tmp_path / "REPOSITORIES.md").write_text(
        "# Index\n\n## Active Repositories\n\n"
        "| Slug | GitHub | Visibility | Default branch | Local path | Archon | Page |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n" + "".join(rows),
        encoding="utf-8",
    )


def test_table_rows_strip_wikilinks_from_every_cell(tmp_path):
    """A vault autolink sweep wikilinked slugs and paths in place (2026-07-13)
    and silently broke every consumer for a month — the parse boundary now
    canonicalizes the representation so a recurrence is harmless."""
    _write_index(
        tmp_path,
        "| [[thehomie]] | x | private | master "
        "| C:\\Users\\YourUser\\[[thehomie]] | yes | p |\n",
    )
    assert repos.list_tracked_repos(memory_dir=tmp_path) == ["thehomie"]
    resolved = repos.resolve_repo("thehomie", memory_dir=tmp_path)
    assert resolved.local_path == Path("C:\\Users\\YourUser\\thehomie")
    assert resolved.default_branch == "master"


def test_table_rows_keep_columns_aligned_through_aliased_wikilinks(tmp_path):
    """An aliased wikilink carries the column separator INSIDE the cell — a
    per-cell strip would run after the split had already shifted the row."""
    _write_index(
        tmp_path,
        "| [[repositories/YourProduct\\|YourProduct]] | x | private "
        "| [[master]] | [[C:\\r\\YourProduct|C:\\r\\YourProduct]] | yes | p |\n",
    )
    assert repos.list_tracked_repos(memory_dir=tmp_path) == ["YourProduct"]
    resolved = repos.resolve_repo("YourProduct", memory_dir=tmp_path)
    assert resolved.local_path == Path("C:\\r\\YourProduct")
    assert resolved.default_branch == "master"


def test_table_rows_leave_unlinked_cells_untouched(tmp_path):
    _write_index(tmp_path, "| YourProduct | x | private | master | C:\\r\\t | yes | p |\n")
    assert repos.list_tracked_repos(memory_dir=tmp_path) == ["YourProduct"]
    assert repos.resolve_repo("YourProduct", memory_dir=tmp_path).local_path == Path(
        "C:\\r\\t"
    )


def test_available_personas_parses_and_skips_broken(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    _write_profile(
        profiles,
        "sales",
        {
            "persona": {"id": "sales", "display_name": "Sales Homie", "role": "closer"},
            "delegation": {"repos": ["YourProduct"]},
        },
    )
    _write_profile(profiles, "outbound", "persona: [")  # bad yaml
    _write_profile(profiles, "chrome-cdp", {"ports": {}})  # no persona section

    from personas import core as personas_core

    monkeypatch.setattr(personas_core, "get_default_homie_root", lambda: tmp_path)
    found = agenda_mod._available_personas()
    assert found == [
        {
            "id": "sales",
            "name": "Sales Homie",
            "role": "closer",
            "repos": ["YourProduct"],
        }
    ]


def test_available_personas_skips_profiles_without_a_delegation_grant(
    monkeypatch, tmp_path
):
    """The send-side scope check refuses an ungranted persona fail-closed, so
    proposing one only burns an agenda line."""
    profiles = tmp_path / "profiles"
    _write_profile(
        profiles,
        "granted",
        {"persona": {"role": "dept head"}, "delegation": {"repos": ["YourProduct"]}},
    )
    _write_profile(profiles, "ungranted", {"persona": {"role": "dept head"}})
    _write_profile(
        profiles,
        "malformed-grant",
        {"persona": {"role": "dept head"}, "delegation": ["YourProduct"]},
    )

    from personas import core as personas_core

    monkeypatch.setattr(personas_core, "get_default_homie_root", lambda: tmp_path)
    assert [p["id"] for p in agenda_mod._available_personas()] == ["granted"]


def test_available_personas_keeps_draft_only_grants(monkeypatch, tmp_path):
    """``delegation.repos: []`` is a real grant — non-repo (null-repo) work."""
    profiles = tmp_path / "profiles"
    _write_profile(
        profiles,
        "operations",
        {"persona": {"role": "ops"}, "delegation": {"repos": []}},
    )
    _write_profile(
        profiles,
        "finance_admin",
        {"persona": {"role": "books"}, "delegation": {"repos": ["", "  "]}},
    )

    from personas import core as personas_core

    monkeypatch.setattr(personas_core, "get_default_homie_root", lambda: tmp_path)
    found = agenda_mod._available_personas()
    assert [(p["id"], p["repos"]) for p in found] == [
        ("finance_admin", []),
        ("operations", []),
    ]


# =============================================================================
# Parse (strict object, fail-closed lines)
# =============================================================================

PERSONAS = frozenset({"sales", "seo_geo"})
REPOS = frozenset({"YourProduct", "YourBusiness"})


def _parse(raw, max_items=5):
    return agenda_mod.parse_agenda(
        raw, persona_ids=PERSONAS, repo_slugs=REPOS, max_items=max_items
    )


def test_parse_rejects_garbage():
    with pytest.raises(agenda_mod.AgendaParseError):
        _parse("I think the team should focus on sales today!")


def test_parse_rejects_unknown_top_level_key():
    raw = json.dumps({"summary": "s", "items": [], "execute": True})
    with pytest.raises(agenda_mod.AgendaParseError, match="unknown keys"):
        _parse(raw)


def test_parse_rejects_non_list_items():
    with pytest.raises(agenda_mod.AgendaParseError, match="items must be a list"):
        _parse(json.dumps({"summary": "s", "items": "do stuff"}))


def test_parse_drops_unknown_persona_keeps_valid():
    raw = _valid_raw(
        items=[
            {"persona": "hr_homie", "repo": None, "task": "hire", "why": "", "priority": 2},
            {"persona": "sales", "repo": "YourProduct", "task": "close", "why": "w", "priority": 1},
        ]
    )
    summary, items = _parse(raw)
    assert [i["persona"] for i in items] == ["sales"]


def test_parse_drops_unknown_repo_keeps_null_repo():
    raw = _valid_raw(
        items=[
            {"persona": "sales", "repo": "example-client", "task": "audit", "why": "", "priority": 2},
            {"persona": "sales", "repo": None, "task": "outreach", "why": "", "priority": 2},
        ]
    )
    _, items = _parse(raw)
    assert len(items) == 1
    assert items[0]["repo"] is None


def test_parse_drops_empty_task():
    raw = _valid_raw(
        items=[{"persona": "sales", "repo": None, "task": "  ", "why": "", "priority": 2}]
    )
    with pytest.raises(agenda_mod.AgendaParseError, match="no valid agenda items"):
        _parse(raw)


def test_parse_all_items_invalid_raises():
    raw = _valid_raw(
        items=[{"persona": "ghost", "repo": None, "task": "x", "why": "", "priority": 2}]
    )
    with pytest.raises(agenda_mod.AgendaParseError):
        _parse(raw)


def test_parse_caps_item_count():
    items = [
        {"persona": "sales", "repo": None, "task": f"task {n}", "why": "", "priority": 2}
        for n in range(6)
    ]
    _, parsed = _parse(_valid_raw(items=items), max_items=2)
    assert len(parsed) == 2


def test_parse_bad_priority_defaults_to_2():
    for bad in (True, 0, 7, "high", None):
        raw = _valid_raw(
            items=[{"persona": "sales", "repo": None, "task": "t", "why": "", "priority": bad}]
        )
        _, items = _parse(raw)
        assert items[0]["priority"] == 2


# =============================================================================
# Pass outcomes
# =============================================================================


def test_proposal_failure_records_attempt_no_artifact_no_card(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"

    def broken(prompt):
        raise RuntimeError("provider down")

    result = _run(
        tmp_path,
        monkeypatch,
        state_file=state_file,
        propose=broken,
        notify=_forbid("card sent for a failed proposal"),
    )
    assert result.outcome == agenda_mod.OUTCOME_PROPOSAL_FAILED
    assert result.exit_code == 0
    state = state_mod.load_state(state_file)
    assert state["agenda"]["attempts"][TODAY] == 1
    assert not (tmp_path / "cofounder" / "agendas").exists()


def test_garbage_output_hits_attempt_cap_across_passes(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    for expected in (1, 2, 3):
        result = _run(
            tmp_path, monkeypatch, state_file=state_file, propose=lambda p: "nope"
        )
        assert result.outcome == agenda_mod.OUTCOME_PROPOSAL_FAILED
        assert state_mod.load_state(state_file)["agenda"]["attempts"][TODAY] == expected
    result = _run(
        tmp_path,
        monkeypatch,
        state_file=state_file,
        propose=_forbid("LLM ran past the attempt cap"),
    )
    assert result.outcome == agenda_mod.OUTCOME_ATTEMPTS_CAPPED


def test_artifact_write_failure_counts_toward_attempt_cap(monkeypatch, tmp_path):
    """A billed proposal followed by a disk failure must burn an attempt —
    otherwise a locked vault folder re-buys a quality-tier call every tick."""
    state_file = tmp_path / "state.json"

    def broken_write(*args, **kwargs):
        raise PermissionError("vault folder locked")

    monkeypatch.setattr(agenda_mod, "_write_artifact", broken_write)
    result = _run(
        tmp_path,
        monkeypatch,
        state_file=state_file,
        notify=_forbid("card sent for an unwritten agenda"),
    )
    assert result.outcome == agenda_mod.OUTCOME_WRITE_FAILED
    assert result.exit_code == 0
    state = state_mod.load_state(state_file)
    assert state["agenda"]["attempts"][TODAY] == 1
    assert "last_date" not in state["agenda"]


def test_list_tracked_repos_skips_malformed_short_rows(tmp_path):
    (tmp_path / "REPOSITORIES.md").write_text(
        "# Index\n\n## Active Repositories\n\n"
        "| Slug | GitHub | Visibility | Default branch | Local path | Archon | Page |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| stub-row |\n"
        "| YourProduct | x | private | master | C:\\r\\YourProduct | yes | p |\n",
        encoding="utf-8",
    )
    assert repos.list_tracked_repos(memory_dir=tmp_path) == ["YourProduct"]


def test_happy_pass_writes_artifact_stamps_state_sends_one_card(
    monkeypatch, tmp_path
):
    state_file = tmp_path / "state.json"
    calls, notify = _recorder()
    result = _run(tmp_path, monkeypatch, state_file=state_file, notify=notify)

    assert result.outcome == agenda_mod.OUTCOME_COMPLETED
    artifact = tmp_path / "cofounder" / "agendas" / f"AGENDA-{TODAY}.md"
    assert result.artifact_path == artifact
    assert result.items == 1
    content = artifact.read_text(encoding="utf-8")
    assert "PROPOSE-ONLY" in content
    assert "**sales** → `YourProduct`" in content
    assert "status: proposed" in content

    state = state_mod.load_state(state_file)
    assert state["agenda"]["last_date"] == TODAY
    assert state["agenda"]["attempts"] == {}
    assert state["agenda"]["last_artifact"] == str(artifact)

    assert len(calls) == 1
    card = calls[0]
    assert card["slug"] == f"agenda-{TODAY}"
    assert card["level"] == agenda_mod.AGENDA_LEVEL
    assert card["with_buttons"] is False
    assert agenda_mod.AGENDA_LEVEL in card["settings"].notify_levels
    assert "Proposed agenda" in card["text"]
    assert "sales -> YourProduct" in card["text"]


def test_agenda_artifact_never_enters_project_discovery(monkeypatch, tmp_path):
    result = _run(tmp_path, monkeypatch)
    assert result.outcome == agenda_mod.OUTCOME_COMPLETED
    assert project_model.discover_projects(tmp_path / "cofounder") == []


def test_dry_run_calls_llm_but_writes_nothing(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    seen = []

    def propose(prompt):
        seen.append(prompt)
        return _valid_raw()

    result = _run(
        tmp_path,
        monkeypatch,
        state_file=state_file,
        dry_run=True,
        propose=propose,
        notify=_forbid("card sent on a dry run"),
    )
    assert result.outcome == agenda_mod.OUTCOME_COMPLETED
    assert result.dry_run is True
    assert result.items == 1
    assert seen, "dry run must still exercise the proposal step"
    assert not (tmp_path / "cofounder" / "agendas").exists()
    assert not state_file.exists()


def test_dry_run_proposal_failure_records_no_attempt(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    result = _run(
        tmp_path, monkeypatch, state_file=state_file, dry_run=True, propose=lambda p: "?"
    )
    assert result.outcome == agenda_mod.OUTCOME_PROPOSAL_FAILED
    assert not state_file.exists()


def test_card_failure_is_fail_open(monkeypatch, tmp_path):
    def exploding(project, text, level, *, settings=None, with_buttons=True):
        raise RuntimeError("telegram down")

    result = _run(tmp_path, monkeypatch, notify=exploding)
    assert result.outcome == agenda_mod.OUTCOME_COMPLETED
    assert result.artifact_path is not None and result.artifact_path.exists()


def test_agenda_notify_false_mutes_the_card(monkeypatch, tmp_path):
    result = _run(
        tmp_path,
        monkeypatch,
        agenda_settings=_agenda_settings(notify=False),
        notify=_forbid("card sent while COFOUNDER_AGENDA_NOTIFY=false"),
    )
    assert result.outcome == agenda_mod.OUTCOME_COMPLETED


def test_empty_notify_levels_is_the_global_mute(monkeypatch, tmp_path):
    result = _run(
        tmp_path,
        monkeypatch,
        settings=config.get_cofounder_settings(
            projects_dir=tmp_path / "cofounder", notify_levels=""
        ),
        notify=_forbid("card sent while COFOUNDER_NOTIFY_LEVELS is empty"),
    )
    assert result.outcome == agenda_mod.OUTCOME_COMPLETED


def test_unexpected_failure_is_error_outcome(monkeypatch, tmp_path):
    monkeypatch.setattr(
        state_mod,
        "_resolve_state_file",
        lambda sf: (_ for _ in ()).throw(OSError("disk gone")),
    )
    result = agenda_mod.run_agenda_pass(
        settings=_settings(tmp_path), agenda_settings=_agenda_settings()
    )
    assert result.outcome == agenda_mod.OUTCOME_ERROR
    assert result.exit_code == 1


def test_prompt_carries_portfolio_and_the_autonomy_contract(tmp_path):
    scan = _scan()
    scan["repo_pages"] = {"YourProduct": {"Recent Activity": "shipped voice demo"}}
    scan["goals"] = "Close 3 clients"
    scan["projects"] = [
        {"slug": "mc-ui", "status": "building", "repo": "mission-control", "iterations": 2}
    ]
    prompt = agenda_mod.build_agenda_prompt(scan, MORNING, max_items=5)
    assert "sales" in prompt
    assert "YourProduct" in prompt
    assert "shipped voice demo" in prompt
    assert "Close 3 clients" in prompt
    assert "mc-ui" in prompt
    assert "2026-07-05" in prompt
    # The prompt must not promise an approval step the autopilot does not
    # honor — an under-committed proposal is what a lying prompt buys.
    assert "the operator approves before anything executes" not in prompt
    assert "autonomously delegated within caps" in prompt
    assert "stake compute on" in prompt


def test_prompt_roster_names_each_persona_s_granted_repos(tmp_path):
    scan = _scan()
    scan["personas"] = [
        {"id": "sales", "name": "Sales", "role": "closer", "repos": ["YourProduct"]},
        {"id": "operations", "name": "Ops", "role": "ops", "repos": []},
    ]
    prompt = agenda_mod.build_agenda_prompt(scan, MORNING, max_items=5)
    assert "sales — Sales — closer — repos: YourProduct" in prompt
    assert "operations — Ops — ops — repos: none (non-repo tasks only)" in prompt


def test_card_names_the_typed_commands(monkeypatch, tmp_path):
    """The card is buttonless, and post-autonomy its commands are the brake.

    The propose-era "Approve: /cofounder run <n>" hint promised a gate the
    autopilot no longer waits on — the card has to name the pause instead.
    """
    calls, notify = _recorder()
    result = _run(tmp_path, monkeypatch, notify=notify)
    assert result.outcome == agenda_mod.OUTCOME_COMPLETED
    assert "Self-delegating within caps" in calls[0]["text"]
    assert "/cofounder pause <slug>" in calls[0]["text"]
    assert "/cofounder run <n>" in calls[0]["text"]
    assert "Approve: /cofounder run <n>" not in calls[0]["text"]


# =============================================================================
# Notify seam (additive with_buttons param)
# =============================================================================


def _capture_send(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"result": {"message_id": 7}}).encode("utf-8")

    def fake_urlopen(req, timeout=10):
        captured["params"] = dict(urllib.parse.parse_qsl(req.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", fake_urlopen)
    return captured


def _notify_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "42")


def test_notify_without_buttons_omits_reply_markup(monkeypatch, tmp_path):
    _notify_env(monkeypatch)
    captured = _capture_send(monkeypatch)
    settings = config.get_cofounder_settings(notify_levels=("agenda",))
    ok = notify_mod.notify(
        SimpleNamespace(slug="agenda-2026-07-05", path=None),
        "card",
        "agenda",
        settings=settings,
        audit_path=tmp_path / "audit.jsonl",
        with_buttons=False,
    )
    assert ok is True
    assert "reply_markup" not in captured["params"]


def test_notify_default_keeps_v1_buttons(monkeypatch, tmp_path):
    _notify_env(monkeypatch)
    captured = _capture_send(monkeypatch)
    settings = config.get_cofounder_settings(notify_levels=("done",))
    ok = notify_mod.notify(
        SimpleNamespace(slug="proj", path=None),
        "done!",
        "done",
        settings=settings,
        audit_path=tmp_path / "audit.jsonl",
    )
    assert ok is True
    markup = json.loads(captured["params"]["reply_markup"])
    callbacks = [b["callback_data"] for b in markup["inline_keyboard"][0]]
    assert callbacks == ["cofounder:pause:proj", "cofounder:approve:proj"]


# =============================================================================
# Config (Rule 1)
# =============================================================================


def test_agenda_settings_resolve_env_at_call_time(monkeypatch):
    defaults = config.get_cofounder_agenda_settings()
    assert defaults.enabled is False
    assert defaults.agenda_hour == 7
    assert defaults.max_items == 5
    assert defaults.max_attempts == 3
    assert defaults.notify is True

    monkeypatch.setenv("COFOUNDER_AGENDA_ENABLED", "true")
    monkeypatch.setenv("COFOUNDER_AGENDA_HOUR", "5")
    monkeypatch.setenv("COFOUNDER_AGENDA_MAX_ITEMS", "9")
    monkeypatch.setenv("COFOUNDER_AGENDA_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("COFOUNDER_AGENDA_NOTIFY", "false")
    live = config.get_cofounder_agenda_settings()
    assert live == config.CofounderAgendaSettings(
        enabled=True, agenda_hour=5, max_items=9, max_attempts=1, notify=False
    )


# =============================================================================
# Identity + operator model + live context in the prompt (T2)
#
# Path map: parity (every new source absent = the pre-T2 prompt, byte for
# byte) - one test per source for present / absent / capped / broken -
# section order - the GOALS staleness label.
# =============================================================================

_CONTEXT_KEYS = (
    "identity",
    "operator_model",
    "live_context",
    "recent_sessions",
    "tracker",
)


def test_prompt_parity_when_every_new_source_is_absent():
    """A scan with no identity/context keys must produce the pre-T2 prompt —
    an unreadable vault can never change what the model is asked."""
    old_shape = _scan()
    baseline = agenda_mod.build_agenda_prompt(old_shape, MORNING, max_items=5)

    empty_shape = {
        **old_shape,
        "goals_updated": "",
        **{key: "" for key in _CONTEXT_KEYS},
    }
    assert agenda_mod.build_agenda_prompt(empty_shape, MORNING, max_items=5) == baseline
    for label, _ in agenda_mod._CONTEXT_SECTIONS:
        assert label not in baseline


def test_prompt_renders_the_sections_in_the_decided_order():
    scan = {
        **_scan(),
        "goals": "Close 3 clients",
        "goals_updated": "2026-06-26",
        "identity": "# SOUL — Co-Founder",
        "operator_model": "- The operator ships daily.",
        "live_context": "Open threads: ship the agenda",
        "recent_sessions": "### 2026-07-04-telegram",
        "tracker": "- [ ] P1 crypto wave",
    }
    prompt = agenda_mod.build_agenda_prompt(scan, MORNING, max_items=5)
    order = [
        "# SOUL — Co-Founder",
        "- The operator ships daily.",
        "Open threads: ship the agenda",
        "- [ ] P1 crypto wave",
        "Delegable personas",
        "Tracked repos:",
        "Close 3 clients",
    ]
    positions = [prompt.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_prompt_labels_goals_with_their_own_date():
    scan = {**_scan(), "goals": "Close 3 clients", "goals_updated": "2026-06-26"}
    prompt = agenda_mod.build_agenda_prompt(scan, MORNING, max_items=5)
    assert "Operator goals (last updated 2026-06-26 — weight the current" in prompt

    undated = {**_scan(), "goals": "Close 3 clients", "goals_updated": ""}
    assert "Operator goals:" in agenda_mod.build_agenda_prompt(
        undated, MORNING, max_items=5
    )


def test_scan_exposes_every_context_key(monkeypatch, tmp_path):
    """build_portfolio_scan is the ONE seam the pass calls — a helper that
    silently stops being wired would leave the prompt quietly bare."""
    wiring = {
        "_identity_text": "identity",
        "_operator_model_text": "operator_model",
        "_live_context_text": "live_context",
        "_recent_sessions_text": "recent_sessions",
        "_tracker_now_text": "tracker",
        "_goals_updated": "goals_updated",
    }
    for helper, key in wiring.items():
        monkeypatch.setattr(agenda_mod, helper, lambda key=key: f"<{key}>")

    scan = agenda_mod.build_portfolio_scan(_settings(tmp_path))
    assert {key: scan[key] for key in wiring.values()} == {
        key: f"<{key}>" for key in wiring.values()
    }


# --- identity ----------------------------------------------------------------


def _cofounder_memory(monkeypatch, tmp_path: Path) -> Path:
    """Point the cofounder profile's memory dir at tmp_path."""
    from personas import core as personas_core

    monkeypatch.setattr(
        personas_core, "get_persona_paths", lambda name: {"memory": tmp_path}
    )
    return tmp_path


def test_identity_reads_the_cofounder_soul(monkeypatch, tmp_path):
    _cofounder_memory(monkeypatch, tmp_path)
    (tmp_path / "SOUL.md").write_text(
        "# SOUL — Co-Founder\n\nYou run the day.\n", encoding="utf-8"
    )
    assert "You run the day." in agenda_mod._identity_text()


def test_identity_absent_when_the_profile_is_unseeded(monkeypatch, tmp_path):
    _cofounder_memory(monkeypatch, tmp_path)
    assert agenda_mod._identity_text() == ""


def test_identity_is_capped(monkeypatch, tmp_path):
    _cofounder_memory(monkeypatch, tmp_path)
    (tmp_path / "SOUL.md").write_text("x" * 5000, encoding="utf-8")
    text = agenda_mod._identity_text()
    assert text.endswith("[truncated]")
    assert len(text) < agenda_mod.IDENTITY_SECTION_CAP + 40


def test_identity_fails_open_when_the_profile_root_explodes(monkeypatch):
    from personas import core as personas_core

    def boom(name):
        raise OSError("homie root gone")

    monkeypatch.setattr(personas_core, "get_persona_paths", boom)
    assert agenda_mod._identity_text() == ""


# --- operator model ----------------------------------------------------------


def _inference_state(monkeypatch, tmp_path: Path, inferences: list[str]) -> Path:
    """Write a real self-model corpus through the tracker's own writer."""
    agenda_mod._ensure_chat_on_path()
    from cognition.self_model import InferenceRecord, InferenceTracker

    path = tmp_path / "self-model-inferences.json"
    InferenceTracker(path).save(
        [
            InferenceRecord(
                id=f"inf-{index}",
                inference=text,
                observation="the operator said so",
                confidence=0.9,
                source="explicit",
            )
            for index, text in enumerate(inferences)
        ]
    )
    monkeypatch.setattr(config, "INFERENCE_STATE_FILE", path)
    return path


def test_operator_model_renders_active_inferences(monkeypatch, tmp_path):
    _inference_state(monkeypatch, tmp_path, ["The operator ships every day."])
    assert "The operator ships every day." in agenda_mod._operator_model_text()


def test_operator_model_absent_when_the_state_file_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "INFERENCE_STATE_FILE", tmp_path / "nope.json")
    assert agenda_mod._operator_model_text() == ""


def test_operator_model_absent_on_an_empty_corpus(monkeypatch, tmp_path):
    """An empty corpus is an absent SOURCE — the renderer's own "no active
    inferences" line must never reach the prompt (the S2 fail-open-empty)."""
    _inference_state(monkeypatch, tmp_path, [])
    assert agenda_mod._operator_model_text() == ""


def test_operator_model_fails_open_on_a_corrupt_corpus(monkeypatch, tmp_path):
    path = tmp_path / "self-model-inferences.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(config, "INFERENCE_STATE_FILE", path)
    assert agenda_mod._operator_model_text() == ""


def test_operator_model_is_capped(monkeypatch, tmp_path):
    _inference_state(
        monkeypatch,
        tmp_path,
        [f"The operator believes thing {index} " * 20 for index in range(8)],
    )
    text = agenda_mod._operator_model_text()
    assert text.endswith("[truncated]")
    assert len(text) < agenda_mod.OPERATOR_MODEL_CAP + 40


# --- live context ------------------------------------------------------------


def test_live_context_reads_working_memory(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    (tmp_path / "WORKING.md").write_text(
        "---\ntags: [system, memory, working]\ndate: 2026-07-05\n---\n"
        "# WORKING.md\n\n## Open Threads\n\n- [2026-07-05] ship the agenda — open\n",
        encoding="utf-8",
    )
    assert "ship the agenda" in agenda_mod._live_context_text()


def test_live_context_absent_without_working_md(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    assert agenda_mod._live_context_text() == ""


def test_live_context_fails_open_when_the_reader_explodes(monkeypatch, tmp_path):
    import living_memory

    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(
        living_memory,
        "build_briefing_section",
        lambda memory_dir: (_ for _ in ()).throw(OSError("vault locked")),
    )
    assert agenda_mod._live_context_text() == ""


# --- recent sessions ---------------------------------------------------------


def _write_episode(memory_dir: Path, name: str, *, date: str, status: str, body: str):
    episodes_dir = memory_dir / "episodes"
    episodes_dir.mkdir(parents=True, exist_ok=True)
    (episodes_dir / f"{name}.md").write_text(
        "---\n"
        "tags: [system, memory, living-mind]\n"
        f"date: {date}\n"
        f"status: {status}\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )


def test_recent_sessions_digests_open_episodes(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    today = datetime.now().date().isoformat()
    _write_episode(
        tmp_path,
        f"{today}-telegram-abc-090000",
        date=today,
        status="open",
        body="## Summary\n\n- shipped the delegation gate",
    )
    _write_episode(
        tmp_path,
        f"{today}-telegram-old-080000",
        date="2020-01-01",
        status="open",
        body="## Summary\n\n- ancient history",
    )
    text = agenda_mod._recent_sessions_text()
    assert "shipped the delegation gate" in text
    assert "ancient history" not in text


def test_recent_sessions_absent_without_episodes(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    assert agenda_mod._recent_sessions_text() == ""


def test_recent_sessions_uses_agenda_sized_caps(monkeypatch, tmp_path):
    """The dream cycle's digest caps are far larger; the agenda gets its own."""
    import episodes as episodes_mod

    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    seen: dict[str, int] = {}

    def fake_digest(paths, *, settings=None):
        seen.update(
            files=settings.dream_max_files,
            per=settings.dream_max_chars_per,
            total=settings.dream_max_total_chars,
        )
        return "digest"

    monkeypatch.setattr(
        episodes_mod, "list_open_episodes", lambda memory_dir, days: [tmp_path / "e.md"]
    )
    monkeypatch.setattr(episodes_mod, "render_episodes_digest", fake_digest)
    assert agenda_mod._recent_sessions_text() == "digest"
    assert seen == {"files": 5, "per": 300, "total": 1200}


def test_recent_sessions_fails_open_on_a_broken_episode_dir(monkeypatch, tmp_path):
    import episodes as episodes_mod

    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(
        episodes_mod,
        "list_open_episodes",
        lambda memory_dir, days: (_ for _ in ()).throw(OSError("episodes gone")),
    )
    assert agenda_mod._recent_sessions_text() == ""


# --- active tracker ----------------------------------------------------------


def _write_tracker(monkeypatch, tmp_path: Path, body: str) -> None:
    tracker = tmp_path.joinpath(*agenda_mod.TRACKER_RELPATH)
    tracker.parent.mkdir(parents=True, exist_ok=True)
    tracker.write_text(body, encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)


def test_tracker_reads_the_now_h3_under_open_items(monkeypatch, tmp_path):
    """Today's TRACKER.md nests Now as an H3 under Open Items."""
    _write_tracker(
        monkeypatch,
        tmp_path,
        "# Tracker\n\n## Open Items\n\n### Now (next session)\n\n"
        "- [ ] P1 crypto wave\n\n### Later (backlog)\n\n- [ ] someday\n\n"
        "## Recently Completed\n\n- shipped last week\n",
    )
    text = agenda_mod._tracker_now_text()
    assert "P1 crypto wave" in text
    assert "someday" not in text
    assert "shipped last week" not in text


def test_tracker_reads_a_top_level_now_h2(monkeypatch, tmp_path):
    """The architecture's documented shape still resolves if the tracker is
    ever restructured."""
    _write_tracker(
        monkeypatch,
        tmp_path,
        "# Tracker\n\n## Now\n\n- [ ] P1 crypto wave\n\n## Later\n\n- [ ] someday\n",
    )
    text = agenda_mod._tracker_now_text()
    assert "P1 crypto wave" in text
    assert "someday" not in text


def test_tracker_absent_when_the_file_or_the_section_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    assert agenda_mod._tracker_now_text() == ""

    _write_tracker(monkeypatch, tmp_path, "# Tracker\n\n## Session Log\n\n- nope\n")
    assert agenda_mod._tracker_now_text() == ""


def test_tracker_is_capped(monkeypatch, tmp_path):
    _write_tracker(
        monkeypatch,
        tmp_path,
        "# Tracker\n\n## Now\n\n" + ("- [ ] a long queued item\n" * 200),
    )
    text = agenda_mod._tracker_now_text()
    assert text.endswith("[truncated]")
    assert len(text) < agenda_mod.TRACKER_CAP + 40


# --- GOALS staleness ---------------------------------------------------------


def test_goals_updated_reads_the_frontmatter_date(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    (tmp_path / "GOALS.md").write_text(
        "---\ntags: [system, goals]\ndate: 2026-06-26\n---\n\n"
        "# Goals\n\ndate: 2020-01-01\n",
        encoding="utf-8",
    )
    assert agenda_mod._goals_updated() == "2026-06-26"


def test_goals_updated_absent_without_frontmatter(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    (tmp_path / "GOALS.md").write_text("# Goals\n\nno frontmatter\n", encoding="utf-8")
    assert agenda_mod._goals_updated() == ""
