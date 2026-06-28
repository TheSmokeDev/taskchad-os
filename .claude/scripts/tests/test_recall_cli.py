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

import config  # noqa: E402
import recall_service  # noqa: E402
from recall_service import SearchMode  # noqa: E402
from cli import main as cli_main  # noqa: E402


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
