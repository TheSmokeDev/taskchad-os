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


def test_available_personas_parses_and_skips_broken(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    good = profiles / "sales"
    good.mkdir(parents=True)
    (good / "config.yaml").write_text(
        yaml.safe_dump(
            {"persona": {"id": "sales", "display_name": "Sales Homie", "role": "closer"}}
        ),
        encoding="utf-8",
    )
    broken = profiles / "outbound"
    broken.mkdir()
    (broken / "config.yaml").write_text("persona: [", encoding="utf-8")  # bad yaml
    no_persona = profiles / "chrome-cdp"
    no_persona.mkdir()
    (no_persona / "config.yaml").write_text("ports: {}\n", encoding="utf-8")

    from personas import core as personas_core

    monkeypatch.setattr(personas_core, "get_default_homie_root", lambda: tmp_path)
    found = agenda_mod._available_personas()
    assert found == [{"id": "sales", "name": "Sales Homie", "role": "closer"}]


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


def test_prompt_carries_portfolio_and_propose_only_contract(tmp_path):
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
    assert "PROPOSE" in prompt
    assert "2026-07-05" in prompt


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
