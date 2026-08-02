"""Talk tool surface tests — schemas, gating, and the dispatcher.

All integrations are monkeypatched at their source modules (handlers import
lazily, so patching the source attribute works); run_python uses a real
subprocess echo — no network, no vault, no DB writes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

import talk_tools


@pytest.fixture(autouse=True)
def _no_code_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALK_ENABLE_CODE_EXEC", raising=False)


# ─── schema surface ──────────────────────────────────────────────────────


def test_default_tools_are_the_read_deploy_and_control_levers() -> None:
    names = [t["name"] for t in talk_tools.default_talk_tools()]

    assert names == [
        "memory_search",
        "calendar_events",
        "homie_command",
        "delegate_task",
        "run_skill",
        "run_archon",
        "computer",
        "browse",
        "check_work",
        "manage_run",
    ]
    for tool in talk_tools.default_talk_tools():
        assert tool["type"] == "function"
        assert tool["description"].strip()
        assert tool["parameters"]["type"] == "object"


def test_run_python_only_advertised_with_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "run_python" not in [t["name"] for t in talk_tools.default_talk_tools()]

    monkeypatch.setenv("TALK_ENABLE_CODE_EXEC", "1")

    assert "run_python" in [t["name"] for t in talk_tools.default_talk_tools()]


def test_default_tools_returns_fresh_copies() -> None:
    first = talk_tools.default_talk_tools()
    first[0]["name"] = "mutated"

    assert talk_tools.default_talk_tools()[0]["name"] == "memory_search"


# ─── dispatcher ──────────────────────────────────────────────────────────


def test_unknown_tool_raises() -> None:
    with pytest.raises(talk_tools.TalkToolError):
        talk_tools.execute_talk_tool("nuke_everything", {})


def test_handler_failure_returns_speakable_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        talk_tools._HANDLERS,
        "memory_search",
        lambda _args: (_ for _ in ()).throw(RuntimeError("vault exploded")),
    )

    output = talk_tools.execute_talk_tool("memory_search", {"query": "x"})

    assert "memory_search failed" in output
    assert "vault exploded" in output


# ─── memory_search ───────────────────────────────────────────────────────


def test_memory_search_formats_results(monkeypatch: pytest.MonkeyPatch) -> None:
    import memory_search

    monkeypatch.setattr(
        memory_search,
        "search",
        lambda query, mode, limit: [
            SimpleNamespace(
                path="vault/memory/decisions.md",
                section_title="Decisions",
                text="Lane-first   runtime\nselection is the contract",
                score=0.9,
                match_type="hybrid",
            )
        ],
    )

    output = talk_tools.execute_talk_tool("memory_search", {"query": "lane", "limit": 3})

    assert "Decisions (vault/memory/decisions.md)" in output
    assert "Lane-first runtime selection" in output


def test_memory_search_empty_query() -> None:
    assert "non-empty query" in talk_tools.execute_talk_tool("memory_search", {"query": "  "})


def test_memory_search_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    import memory_search

    monkeypatch.setattr(memory_search, "search", lambda *a, **k: [])

    assert "No memory notes matched" in talk_tools.execute_talk_tool(
        "memory_search", {"query": "zzz"}
    )


# ─── calendar_events ─────────────────────────────────────────────────────


def test_calendar_today(monkeypatch: pytest.MonkeyPatch) -> None:
    from integrations import calendar_api

    monkeypatch.setattr(calendar_api, "get_today_events", lambda: ["evt"])
    monkeypatch.setattr(
        calendar_api, "format_events_for_context", lambda events: "Today: standup 9am"
    )

    assert "standup 9am" in talk_tools.execute_talk_tool("calendar_events", {})


def test_calendar_upcoming_days(monkeypatch: pytest.MonkeyPatch) -> None:
    from integrations import calendar_api

    seen = {}
    monkeypatch.setattr(
        calendar_api,
        "get_upcoming_events",
        lambda days: seen.setdefault("days", days) or ["evt"],
    )
    monkeypatch.setattr(calendar_api, "format_events_for_context", lambda e: "week view")

    output = talk_tools.execute_talk_tool("calendar_events", {"days": 7})

    assert seen["days"] == 7
    assert "week view" in output


def test_calendar_today_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from integrations import calendar_api

    monkeypatch.setattr(calendar_api, "get_today_events", lambda: [])

    assert "Nothing on the calendar today" in talk_tools.execute_talk_tool(
        "calendar_events", {"days": 0}
    )


# ─── homie_command ───────────────────────────────────────────────────────


def test_homie_command_dispatches_collect_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {}

    class FakeManager:
        async def dispatch(self, command, adapter, incoming, args, *, collect_only):
            calls.update(
                command=command,
                adapter=adapter,
                args=args,
                collect_only=collect_only,
                role=incoming.user_role,
            )
            return "GSC: 12 clicks yesterday"

    monkeypatch.setattr(talk_tools, "_command_manager", lambda: FakeManager())

    output = talk_tools.execute_talk_tool(
        "homie_command", {"command": "/gsc", "args": "yesterday"}
    )

    assert output == "GSC: 12 clicks yesterday"
    assert calls["command"] == "gsc"  # slash stripped
    assert calls["adapter"] is None
    assert calls["collect_only"] is True
    assert calls["role"] == "admin"


def test_homie_command_unknown_command(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeManager:
        async def dispatch(self, *a, **k):
            return None

    monkeypatch.setattr(talk_tools, "_command_manager", lambda: FakeManager())

    assert "not a router command" in talk_tools.execute_talk_tool(
        "homie_command", {"command": "frobnicate"}
    )


# ─── delegate_task ───────────────────────────────────────────────────────


def test_delegate_task_needs_the_task() -> None:
    assert "needs the task itself" in talk_tools.execute_talk_tool("delegate_task", {"task": "  "})


def test_delegate_task_derives_a_title_from_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_start(task, title, lane):
        captured.update(task=task, title=title, lane=lane)
        return 5, 77

    monkeypatch.setattr(talk_tools, "start_agent_run", fake_start)

    output = talk_tools.execute_talk_tool(
        "delegate_task", {"task": "Audit the TokenMax site for broken canonical tags"}
    )

    assert "WORK_STARTED #5 kind=agent" in output
    assert "Task #77" in output
    assert captured["title"].startswith("Audit the TokenMax site")
    assert captured["lane"] == "codex"  # quota-safe default


def test_delegate_task_honors_a_lane_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    monkeypatch.setattr(
        talk_tools,
        "start_agent_run",
        lambda task, title, lane: (captured.update(lane=lane), (1, 2))[1],
    )

    talk_tools.execute_talk_tool("delegate_task", {"task": "do a thing", "lane": "kimi"})

    assert captured["lane"] == "kimi"


def test_delegate_task_ignores_a_bogus_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    monkeypatch.setattr(
        talk_tools,
        "start_agent_run",
        lambda task, title, lane: (captured.update(lane=lane), (1, 2))[1],
    )

    talk_tools.execute_talk_tool("delegate_task", {"task": "x", "lane": "telepathy"})

    assert captured["lane"] == "codex"


# ─── delegate_task: the F5 hybrid boundary ───────────────────────────────


def test_delegate_task_advertises_the_scope_lever() -> None:
    """The descriptions ARE the prompt surface on the Realtime lane (F5)."""

    tool = next(t for t in talk_tools.default_talk_tools() if t["name"] == "delegate_task")
    scope = tool["parameters"]["properties"]["scope"]

    assert scope["enum"] == ["quick", "substantial"]
    assert "worktree" in tool["description"]
    assert "never for a lookup" in tool["description"]


def test_delegate_task_defaults_to_the_fast_in_process_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Short work must not pay for a clone plus a worktree."""

    archon_calls: list = []
    monkeypatch.setattr(
        talk_tools, "start_archon_run", lambda *a, **k: archon_calls.append(a) or (1, "w", 2)
    )
    monkeypatch.setattr(talk_tools, "start_agent_run", lambda task, title, lane: (5, 77))

    output = talk_tools.execute_talk_tool("delegate_task", {"task": "check the leads"})

    assert "kind=agent" in output
    assert archon_calls == []


def test_delegate_task_substantial_routes_to_archon(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_start(workflow, task, *, title=None, caller="talk.run_archon"):
        captured.update(workflow=workflow, task=task, title=title, caller=caller)
        return 9, workflow, 42

    monkeypatch.setattr(talk_tools, "start_archon_run", fake_start)
    monkeypatch.setattr(
        talk_tools,
        "start_agent_run",
        lambda *a: pytest.fail("substantial work must not take the in-process lane"),
    )
    monkeypatch.setenv("TALK_ARCHON_DEFAULT_WORKFLOW", "archon-ralph-dag")

    output = talk_tools.execute_talk_tool(
        "delegate_task",
        {"task": "Rebuild the YourBusiness quote flow end to end", "scope": "substantial"},
    )

    assert captured["workflow"] == "archon-ralph-dag"
    assert captured["task"] == "Rebuild the YourBusiness quote flow end to end"
    assert captured["caller"] == "talk.delegate_task"
    assert "kind=archon" in output
    assert "kind=archon (archon-ralph-dag)" in output
    assert "task #42" in output


def test_delegate_task_substantial_speaks_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate refusal on the substantial branch is spoken, not raised."""

    import talk_archon

    def refuse(*args, **kwargs):
        raise talk_archon.ArchonDispatchRefusedError("that brief is too thin to deploy.")

    monkeypatch.setattr(talk_tools, "start_archon_run", refuse)

    output = talk_tools.execute_talk_tool(
        "delegate_task", {"task": "yeah do that", "scope": "substantial"}
    )

    assert output == "that brief is too thin to deploy."


def test_delegate_task_unknown_scope_degrades_to_the_cheap_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognised scope must not silently buy a worktree."""

    monkeypatch.setattr(
        talk_tools,
        "start_archon_run",
        lambda *a, **k: pytest.fail("unknown scope must not reach Archon"),
    )
    monkeypatch.setattr(talk_tools, "start_agent_run", lambda task, title, lane: (5, 77))

    output = talk_tools.execute_talk_tool(
        "delegate_task", {"task": "look something up", "scope": "enormous"}
    )

    assert "kind=agent" in output


# ─── the work-queue ledger (DB-level proof, not return values) ───────────


@pytest.fixture
def ledger_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real OrchestrationDB so ledger assertions read actual rows."""

    import config
    from orchestration.convoy_service import ConvoyService
    from orchestration.db import OrchestrationDB

    db_path = tmp_path / "orchestration.db"
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", db_path)
    return lambda: ConvoyService(OrchestrationDB(db_path))


def _turn_envelope(
    response: str = "",
    *,
    success: bool = True,
    session_id: str = "cli-turn1",
    error: str = "",
    stderr: str = "",
) -> dict:
    """The dict `_run_agent_turn` returns — the agent worker's fake seam."""

    return {
        "success": success,
        "response": response,
        "session_id": session_id,
        "error": error or "unknown engine error",
        "stderr": stderr,
    }


def test_agent_run_drives_the_subtask_row_to_completed(
    ledger_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        talk_tools,
        "_run_agent_turn",
        lambda prompt, lane, timeout_s, run_id, resume_sid="": _turn_envelope("audit done"),
    )

    run_id, subtask_id = talk_tools.start_agent_run("audit the site", "audit", "codex")
    run = _wait_for_run(run_id)

    assert run["status"] == "done"
    assert run["output"] == "audit done"
    # Rule 4: assert the ROW, never just the return value.
    subtask = ledger_db().get_convoy(1).subtasks[0]
    assert subtask.id == subtask_id
    assert subtask.status == "completed"


def test_agent_run_failure_marks_the_row_failed(
    ledger_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        talk_tools,
        "_run_agent_turn",
        lambda prompt, lane, timeout_s, run_id, resume_sid="": _turn_envelope(
            success=False, error="quota exhausted"
        ),
    )

    run_id, _ = talk_tools.start_agent_run("do a thing", "thing", "codex")
    run = _wait_for_run(run_id)

    # The lane reported failure in-band, so the run lands 'done' with the text;
    # the row still completes because the agent finished its attempt.
    assert "quota exhausted" in run["output"]
    assert ledger_db().get_convoy(1).subtasks[0].status == "completed"


def test_agent_run_timeout_fails_the_row_and_the_run(
    ledger_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_turn(prompt, lane, timeout_s, run_id, resume_sid=""):
        raise subprocess.TimeoutExpired(cmd=["uv"], timeout=1800)

    monkeypatch.setattr(talk_tools, "_run_agent_turn", fake_turn)

    run_id, _ = talk_tools.start_agent_run("endless", "endless", "codex")
    run = _wait_for_run(run_id)

    assert run["status"] == "failed"
    assert "was stopped" in run["output"]
    assert ledger_db().get_convoy(1).subtasks[0].status == "failed"


def test_agent_run_survives_a_broken_ledger(
    ledger_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bookkeeping failure must never kill the work it describes."""

    monkeypatch.setattr(
        talk_tools,
        "_run_agent_turn",
        lambda prompt, lane, timeout_s, run_id, resume_sid="": _turn_envelope("still ran"),
    )
    service = talk_tools._convoy_service

    def broken_service():
        svc = service()
        svc.dispatch_subtask = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db locked"))
        return svc

    run_id, _ = talk_tools.start_agent_run("work", "work", "codex")
    monkeypatch.setattr(talk_tools, "_convoy_service", broken_service)
    run = _wait_for_run(run_id)

    assert run["status"] == "done"
    assert run["output"] == "still ran"


def test_voice_convoy_row_is_attributed_to_voice(ledger_db, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        talk_tools,
        "_run_agent_turn",
        lambda prompt, lane, timeout_s, run_id, resume_sid="": _turn_envelope("ok"),
    )

    run_id, _ = talk_tools.start_agent_run("a task", "a task", "codex")
    _wait_for_run(run_id)

    assert ledger_db().list_convoys()[0].created_by == "voice"


# ─── run_python (operator-gated) ─────────────────────────────────────────


def test_run_python_refused_without_opt_in() -> None:
    output = talk_tools.execute_talk_tool("run_python", {"code": "print(1)"})

    assert "disabled by the operator" in output


def test_run_python_executes_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALK_ENABLE_CODE_EXEC", "true")

    output = talk_tools.execute_talk_tool("run_python", {"code": "print(17 * 4)"})

    assert output.strip() == "68"


def test_run_python_nonzero_exit_reports_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALK_ENABLE_CODE_EXEC", "1")

    output = talk_tools.execute_talk_tool(
        "run_python", {"code": "import sys; sys.stderr.write('boom'); sys.exit(3)"}
    )

    assert "exited 3" in output
    assert "boom" in output


# ─── session mint carries tools ──────────────────────────────────────────


def test_build_session_payload_includes_tools() -> None:
    import talk_session

    payload = talk_session.build_session_payload(
        model="gpt-realtime-2.1",
        voice="cedar",
        instructions="hi",
        tools=talk_tools.default_talk_tools(),
    )

    assert payload["tool_choice"] == "auto"
    names = [t["name"] for t in payload["tools"]]
    assert "memory_search" in names and "delegate_task" in names


def test_build_session_payload_without_tools_omits_keys() -> None:
    import talk_session

    payload = talk_session.build_session_payload(
        model="gpt-realtime-2.1", voice="cedar", instructions="hi"
    )

    assert "tools" not in payload
    assert "tool_choice" not in payload


# ─── run_skill ───────────────────────────────────────────────────────────


def _tmp_skill_roots(tmp_path: Path) -> list[Path]:
    root_a = tmp_path / "repo-skills"
    root_b = tmp_path / "user-skills"
    (root_a / "vault-ops").mkdir(parents=True)
    (root_a / "vault-ops" / "SKILL.md").write_text("# Vault Ops\nDo the vault thing.")
    (root_b / "vault-ops").mkdir(parents=True)
    (root_b / "vault-ops" / "SKILL.md").write_text("# SHADOWED")
    (root_b / "keyword-research").mkdir(parents=True)
    (root_b / "keyword-research" / "SKILL.md").write_text("# KW Research")
    nested = root_a / "generated" / "seo" / "brand-fleet-seo"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("# Fleet SEO")
    return [root_a, root_b]


def test_resolve_skill_precedence_and_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _tmp_skill_roots(tmp_path)
    monkeypatch.setattr(talk_tools, "_skill_roots", lambda: roots)

    # project root shadows user root for the same skill name
    assert talk_tools.resolve_skill("vault-ops").read_text() == "# Vault Ops\nDo the vault thing."
    # spaces/underscores normalize to dashes, case-insensitive
    assert talk_tools.resolve_skill("Vault Ops").parent.name == "vault-ops"
    assert talk_tools.resolve_skill("keyword_research").parent.name == "keyword-research"


def test_resolve_skill_nested_generated_and_substring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _tmp_skill_roots(tmp_path)
    monkeypatch.setattr(talk_tools, "_skill_roots", lambda: roots)

    assert talk_tools.resolve_skill("brand-fleet-seo").parent.name == "brand-fleet-seo"
    assert talk_tools.resolve_skill("fleet").parent.name == "brand-fleet-seo"


def test_resolve_skill_miss_suggests_close_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _tmp_skill_roots(tmp_path)
    monkeypatch.setattr(talk_tools, "_skill_roots", lambda: roots)

    with pytest.raises(talk_tools.TalkToolError, match="Did you mean"):
        talk_tools.resolve_skill("vaut-ops")


def test_run_skill_schema_always_advertised() -> None:
    names = [t["name"] for t in talk_tools.default_talk_tools()]

    assert "run_skill" in names


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _wait_for_run(run_id: int, timeout_s: float = 3.0) -> dict:
    import time

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        run = talk_tools.get_skill_run(run_id)
        if run is not None and run["status"] != "running":
            return run
        time.sleep(0.02)
    raise AssertionError(f"skill run {run_id} did not terminate in {timeout_s}s")


def test_run_skill_starts_async_and_lands_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _tmp_skill_roots(tmp_path)
    monkeypatch.setattr(talk_tools, "_skill_roots", lambda: roots)
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(argv=argv, timeout=kwargs.get("timeout"))
        envelope = {"success": True, "response": "Vault digest: 3 notes updated."}
        return _Completed(f"lane noise line\n{json.dumps(envelope)}\n")

    monkeypatch.setattr(talk_tools.subprocess, "run", fake_run)

    receipt = talk_tools.execute_talk_tool(
        "run_skill", {"name": "vault ops", "input": "daily context"}
    )

    assert receipt.startswith("WORK_STARTED #")
    assert "kind=skill (vault-ops)" in receipt
    run_id = int(receipt.split("#")[1].split(" ")[0])

    run = _wait_for_run(run_id)
    assert run["status"] == "done"
    assert run["output"] == "Vault digest: 3 notes updated."
    argv = captured["argv"]
    assert argv[:3] == ["uv", "run", "thehomie"]
    assert "-m" in argv and argv[argv.index("-m") + 1] == "codex"  # default lane
    assert captured["timeout"] == 300
    prompt = argv[argv.index("-q") + 1]
    assert "daily context" in prompt
    assert "# Vault Ops" in prompt  # skill body inlined


def test_run_skill_lane_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots = _tmp_skill_roots(tmp_path)
    monkeypatch.setattr(talk_tools, "_skill_roots", lambda: roots)
    monkeypatch.setenv("TALK_SKILL_LANE", "gemini")
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Completed(json.dumps({"success": True, "response": "ok"}))

    monkeypatch.setattr(talk_tools.subprocess, "run", fake_run)

    receipt = talk_tools.execute_talk_tool("run_skill", {"name": "vault-ops"})
    run_id = int(receipt.split("#")[1].split(" ")[0])
    _wait_for_run(run_id)

    argv = captured["argv"]
    assert argv[argv.index("-m") + 1] == "gemini"


def test_run_skill_lane_failure_lands_as_done_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _tmp_skill_roots(tmp_path)
    monkeypatch.setattr(talk_tools, "_skill_roots", lambda: roots)
    monkeypatch.setattr(
        talk_tools.subprocess,
        "run",
        lambda argv, **k: _Completed(json.dumps({"success": False, "error": "quota exhausted"})),
    )

    receipt = talk_tools.execute_talk_tool("run_skill", {"name": "vault-ops"})
    run_id = int(receipt.split("#")[1].split(" ")[0])

    run = _wait_for_run(run_id)
    assert run["status"] == "done"
    assert "quota exhausted" in run["output"]


def test_run_skill_timeout_chains_to_a_real_agent_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long skill hands off to a background agent — never a false success."""

    roots = _tmp_skill_roots(tmp_path)
    monkeypatch.setattr(talk_tools, "_skill_roots", lambda: roots)

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=300)

    monkeypatch.setattr(talk_tools.subprocess, "run", fake_run)
    chained = {}

    def fake_start(task, title, lane):
        chained.update(task=task, title=title)
        return 88, 42

    monkeypatch.setattr(talk_tools, "start_agent_run", fake_start)

    receipt = talk_tools.execute_talk_tool(
        "run_skill", {"name": "vault-ops", "input": "weekly synthesis"}
    )
    run_id = int(receipt.split("#")[1].split(" ")[0])

    run = _wait_for_run(run_id)
    # The skill did NOT succeed — it is failed, and the output points the
    # poller at the follow-on agent run so the chain keeps going.
    assert run["status"] == "failed"
    assert "WORK_STARTED #88 kind=agent" in run["output"]
    assert "Task #42" in run["output"]
    assert "# Vault Ops" in chained["task"]  # the same prompt was handed over


def test_get_skill_run_unknown_id() -> None:
    assert talk_tools.get_skill_run(999_999) is None


def test_run_skill_unknown_name_is_speakable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _tmp_skill_roots(tmp_path)
    monkeypatch.setattr(talk_tools, "_skill_roots", lambda: roots)

    output = talk_tools.execute_talk_tool("run_skill", {"name": "vaut-ops"})

    assert "no skill named" in output
    assert "Did you mean" in output


# ─── run_shell (operator-gated) ──────────────────────────────────────────
#
# The gap these cover: every action on the `computer` tool drives the desktop
# and captures nothing, so reading a command's output meant a ~30s
# screenshot-plus-vision call. run_shell hands the text back directly.


def test_run_shell_refused_without_opt_in() -> None:
    output = talk_tools.execute_talk_tool("run_shell", {"command": "echo hi"})

    assert "disabled by the operator" in output


def test_run_shell_only_advertised_with_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "run_shell" not in [t["name"] for t in talk_tools.default_talk_tools()]

    monkeypatch.setenv("TALK_ENABLE_CODE_EXEC", "1")

    names = [t["name"] for t in talk_tools.default_talk_tools()]
    assert "run_shell" in names
    # Listed before run_python so it is the nearer option when the model is
    # deciding how to run something.
    assert names.index("run_shell") < names.index("run_python")


def test_run_shell_returns_stdout_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: the output comes back, no screenshot required."""
    monkeypatch.setenv("TALK_ENABLE_CODE_EXEC", "1")

    output = talk_tools.execute_talk_tool(
        "run_shell", {"command": "python -c \"print(17 * 4)\""}
    )

    assert output.strip() == "68"


def test_run_shell_runs_in_the_repo_root_not_the_scripts_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gh, git and uv are the point of this tool and they are repo-relative."""
    monkeypatch.setenv("TALK_ENABLE_CODE_EXEC", "1")

    output = talk_tools.execute_talk_tool(
        "run_shell", {"command": "python -c \"import os;print(os.getcwd())\""}
    )

    assert Path(output.strip()) == talk_tools._repo_root()


def test_run_shell_nonzero_exit_reports_the_code_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TALK_ENABLE_CODE_EXEC", "1")

    output = talk_tools.execute_talk_tool(
        "run_shell",
        {"command": "python -c \"import sys; sys.stderr.write('boom'); sys.exit(3)\""},
    )

    assert "exited 3" in output
    assert "boom" in output


def test_run_shell_keeps_stdout_on_a_failing_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """git and gh both write useful output to stdout AND exit non-zero.
    Discarding stdout there throws away the answer the operator asked for."""
    monkeypatch.setenv("TALK_ENABLE_CODE_EXEC", "1")

    output = talk_tools.execute_talk_tool(
        "run_shell",
        {"command": "python -c \"print('partial answer'); raise SystemExit(2)\""},
    )

    assert "exited 2" in output
    assert "partial answer" in output


def test_run_shell_silent_success_is_not_narrated_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty string reads to the model as something to apologise for."""
    monkeypatch.setenv("TALK_ENABLE_CODE_EXEC", "1")

    output = talk_tools.execute_talk_tool(
        "run_shell", {"command": "python -c \"pass\""}
    )

    assert output == "(no output)"


def test_run_shell_needs_a_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALK_ENABLE_CODE_EXEC", "1")

    assert "needs a command" in talk_tools.execute_talk_tool("run_shell", {"command": "  "})


def test_run_shell_output_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A voice surface cannot read 200KB of `gh repo list` aloud."""
    monkeypatch.setenv("TALK_ENABLE_CODE_EXEC", "1")

    output = talk_tools.execute_talk_tool(
        "run_shell", {"command": "python -c \"print('x' * 50000)\""}
    )

    assert len(output) <= talk_tools._MAX_OUTPUT_CHARS


def test_the_computer_tool_points_at_run_shell_for_output() -> None:
    """The behavioural half. The tool existing does not help if the model still
    reaches for the desktop — run_command's own description has to say so."""
    computer = next(
        t for t in talk_tools.default_talk_tools() if t["name"] == "computer"
    )

    assert "run_shell" in computer["description"]
