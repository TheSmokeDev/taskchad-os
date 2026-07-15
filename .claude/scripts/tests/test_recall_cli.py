"""WS1 — unit tests for the `thehomie recall` CLI command (pure path).

Mocks the single recall entrypoint (``recall_service.recall``) so no index or
runtime lane is touched. Asserts (a) the CLI plumbs args into that one entrypoint
correctly — including the load-bearing ``search_mode=HYBRID`` / ``is_slash_command
=False`` that keep the haiku rerank reachable — and (b) the ``--json`` machine
contract the /vault-ops skill consumes, including fail-open on error.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from click.testing import CliRunner

# Mirror the flat sys.path convention (conftest also does this).
_CHAT_DIR = str(Path(__file__).parent.parent.parent / "chat")
_SCRIPTS_DIR = str(Path(__file__).parent.parent)
for _p in (_CHAT_DIR, _SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

import cli as cli_mod  # noqa: E402
import config  # noqa: E402
import recall_service  # noqa: E402
from recall_service import SearchMode  # noqa: E402
from cli import main as cli_main  # noqa: E402


@pytest.fixture(autouse=True)
def _no_update_banner(monkeypatch):
    """The startup update-check banner goes to stderr (stdout-clean contract),
    but CliRunner mixes stderr into ``res.output`` — suppress it so JSON parses
    stay deterministic regardless of the installed version."""
    monkeypatch.setattr(cli_mod, "check_for_update", lambda: None)


def _fake_response():
    log = SimpleNamespace(tier="tier_1", reranked=True, results_returned=1, latency_ms=12.5)
    result = SimpleNamespace(
        path="MEMORY.md",
        start_line=1,
        end_line=3,
        score=0.42,
        match_type="hybrid",
        section_title="Active Projects",
        text="snippet",
    )
    return SimpleNamespace(results=[result], formatted_text="## recall\n- snippet", log=log)


def test_recall_cli_arg_plumbing_and_json_shape(monkeypatch):
    fake = AsyncMock(return_value=_fake_response())
    monkeypatch.setattr(recall_service, "recall", fake)

    res = CliRunner().invoke(
        cli_main, ["recall", "test query", "--json", "--mode", "hybrid", "-n", "4"]
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)

    # JSON shape — the machine contract the skill consumes.
    assert set(payload) >= {
        "query", "vault", "memory_dir", "mode", "brief",
        "formatted_text", "results", "log",
    }
    assert payload["query"] == "test query"
    assert payload["vault"] == "thehomie"
    assert payload["mode"] == "hybrid"
    assert payload["formatted_text"] == "## recall\n- snippet"
    assert payload["results"][0]["path"] == "MEMORY.md"
    assert payload["results"][0]["match_type"] == "hybrid"
    assert payload["log"] == {
        "tier": "tier_1", "reranked": True, "results_returned": 1, "latency_ms": 12.5,
    }

    # Arg plumbing into the ONE recall entrypoint (Invariant I-3).
    fake.assert_awaited_once()
    kwargs = fake.await_args.kwargs
    assert kwargs["search_mode"] is SearchMode.HYBRID  # hybrid -> TIER_1 -> reaches rerank
    assert kwargs["is_slash_command"] is False         # load-bearing: do NOT flip
    assert kwargs["max_results"] == 4
    assert kwargs["caller"] == "vault-ops"
    assert Path(kwargs["memory_dir"]) == config.MEMORY_DIR  # default --vault thehomie


def test_recall_cli_fails_open_on_exception(monkeypatch):
    boom = AsyncMock(side_effect=RuntimeError("index exploded"))
    monkeypatch.setattr(recall_service, "recall", boom)

    res = CliRunner().invoke(cli_main, ["recall", "q", "--json", "--mode", "keyword"])

    # Fail-open: the skill shells with `|| true`, so empty + exit 0 = "no augmentation".
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["results"] == []
    assert payload["formatted_text"] == ""
    assert payload["log"]["tier"] == "error"
    assert payload["log"]["reranked"] is False


def test_recall_cli_mode_maps_to_search_mode(monkeypatch):
    fake = AsyncMock(return_value=_fake_response())
    monkeypatch.setattr(recall_service, "recall", fake)
    runner = CliRunner()
    for mode, expected in (("auto", SearchMode.AUTO), ("keyword", SearchMode.KEYWORD)):
        fake.reset_mock()
        res = runner.invoke(cli_main, ["recall", "q", "--json", "--mode", mode])
        assert res.exit_code == 0, res.output
        assert fake.await_args.kwargs["search_mode"] is expected


# ── --brief semantics (fixed 2026-07-15: terse OUTPUT, not the proactive brief) ──


def test_brief_is_terse_output_without_proactive_brief(monkeypatch):
    """The 07-05/07-15 miswire regression: --brief must NEVER emit the
    proactive-brief/beliefs block the vault-ops skill never asked for."""
    fake = AsyncMock(return_value=_fake_response())
    monkeypatch.setattr(recall_service, "recall", fake)

    res = CliRunner().invoke(cli_main, ["recall", "q", "--brief"])

    assert res.exit_code == 0, res.output
    assert "## Proactive Brief" not in res.output
    assert "Active Beliefs" not in res.output
    # Terse format: header marker + per-hit line with location and score.
    assert "[recall] 1 hit(s)" in res.output
    assert "untrusted historical data" in res.output
    assert "MEMORY.md:1-3" in res.output
    assert "[Active Projects]" in res.output
    assert "score=0.42" in res.output
    assert "snippet" in res.output
    # The verbose <recalled-memory> wrapper is the non-brief rendering.
    assert "## recall" not in res.output


def test_with_proactive_brief_prepends_brief(monkeypatch):
    fake = AsyncMock(return_value=_fake_response())
    monkeypatch.setattr(recall_service, "recall", fake)
    import cognition.proactive_brief as pb_mod

    monkeypatch.setattr(
        pb_mod, "build_proactive_brief_section", lambda memory_dir: "## Proactive Brief\n\n- x"
    )

    res = CliRunner().invoke(cli_main, ["recall", "q", "--with-proactive-brief"])

    assert res.exit_code == 0, res.output
    assert "## Proactive Brief" in res.output
    assert "## recall" in res.output  # full formatted_text still follows
    assert res.output.index("Proactive Brief") < res.output.index("## recall")


def test_json_brief_key_only_populated_under_with_proactive_brief(monkeypatch):
    fake = AsyncMock(return_value=_fake_response())
    monkeypatch.setattr(recall_service, "recall", fake)
    import cognition.proactive_brief as pb_mod

    monkeypatch.setattr(
        pb_mod, "build_proactive_brief_section", lambda memory_dir: "## Proactive Brief"
    )
    runner = CliRunner()

    plain = json.loads(runner.invoke(cli_main, ["recall", "q", "--json", "--brief"]).output)
    assert plain["brief"] == ""

    with_pb = json.loads(
        runner.invoke(cli_main, ["recall", "q", "--json", "--with-proactive-brief"]).output
    )
    assert with_pb["brief"] == "## Proactive Brief"


def test_brief_empty_results_prints_nothing(monkeypatch):
    log = SimpleNamespace(tier="tier_2", reranked=False, results_returned=0, latency_ms=1.0)
    empty = SimpleNamespace(results=[], formatted_text="", log=log)
    fake = AsyncMock(return_value=empty)
    monkeypatch.setattr(recall_service, "recall", fake)

    res = CliRunner().invoke(cli_main, ["recall", "q", "--brief"])

    # Fail-open contract: empty output + exit 0 = "no augmentation".
    assert res.exit_code == 0, res.output
    assert res.output.strip() == ""


def test_console_smoke_brief_keyword_is_clean_and_fast():
    """Real console path (cli_entry sets THEHOMIE_CONSOLE_ENTRY): the exact
    invocation shape vault-ops shells must return terse-or-empty within budget
    and never lead with the proactive brief. Catches the >120s wedge class a
    CliRunner test cannot (no _console_hard_exit in-process)."""
    import subprocess

    scripts_dir = Path(__file__).parent.parent
    proc = subprocess.run(
        [sys.executable, str(scripts_dir / "cli_entry.py"), "recall",
         "heartbeat", "--vault", "thehomie", "--mode", "keyword", "--brief"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(scripts_dir),
    )
    assert proc.returncode == 0, proc.stderr
    assert "## Proactive Brief" not in proc.stdout
    assert "Active Beliefs" not in proc.stdout
