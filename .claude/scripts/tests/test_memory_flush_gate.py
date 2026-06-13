from __future__ import annotations

import importlib.util
import json
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

import memory_flush


def _load_session_end_flush_module():
    hook_path = Path(__file__).resolve().parents[2] / "hooks" / "session-end-flush.py"
    spec = importlib.util.spec_from_file_location("session_end_flush_test", hook_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_transcript(path: Path, turns: list[tuple[str, str]]) -> None:
    rows = [
        {"message": {"role": role, "content": content}}
        for role, content in turns
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def test_two_turn_high_value_session_reaches_semantic_flush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = _load_session_end_flush_module()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [
            ("user", "Decision: ship issue #42 on branch codex/issue-42."),
            ("assistant", "Recorded that #42 owns memory flush and frontmatter gates."),
        ],
    )

    events: list[tuple[str, str]] = []
    popen_calls: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append(list(cmd))
        context_path = Path(cmd[-1])
        assert context_path.exists()
        context = context_path.read_text(encoding="utf-8")
        assert "Decision: ship issue #42" in context
        assert "frontmatter gates" in context
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(hook, "STATE_DIR", state_dir)
    monkeypatch.setattr(hook, "MEMORY_DIR", memory_dir)
    monkeypatch.setattr(hook, "SCRIPTS_DIR", tmp_path / "scripts")
    monkeypatch.setattr(hook, "ensure_directories", lambda: None)
    monkeypatch.setattr(hook.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        hook,
        "log_hook_execution",
        lambda _name, _source, status, _duration, detail: events.append((status, detail)),
    )
    monkeypatch.setitem(
        sys.modules,
        "living_memory",
        SimpleNamespace(append_open_threads_from_flush=lambda _memory_dir, _context: 0),
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        StringIO(json.dumps({
            "session_id": "session-42",
            "source": "clear",
            "transcript_path": str(transcript),
        })),
    )

    hook.main()

    assert popen_calls
    assert popen_calls[0][-3:] == [
        "memory_flush.py",
        "--context-file",
        popen_calls[0][-1],
    ]
    assert ("OK", "spawned flush") in events
    assert not any(status == "SKIP" for status, _detail in events)


@pytest.mark.asyncio
async def test_low_signal_session_can_still_drop_after_semantic_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_file = tmp_path / "session-flush-low-signal-20260608-120000.md"
    context_file.write_text(
        "**User:** hi\n\n**Assistant:** hey\n\n**User:** ok thanks\n",
        encoding="utf-8",
    )
    state_file = tmp_path / "flush-state.json"
    daily_entries: list[tuple[str, str]] = []

    async def fake_runtime(_request):
        return SimpleNamespace(
            text="FLUSH_OK",
            provider="test-provider",
            model="test-model",
            cost_usd=0.0,
        )

    monkeypatch.setattr(memory_flush, "FLUSH_STATE_FILE", state_file)
    monkeypatch.setattr(memory_flush, "run_with_runtime_lanes", fake_runtime)
    monkeypatch.setattr(
        memory_flush,
        "append_to_daily_log",
        lambda text, section: daily_entries.append((text, section)),
    )

    result = await memory_flush.run_flush(context_file)

    assert result is None
    assert not context_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["result"] == "FLUSH_OK"
    assert daily_entries == [
        ("FLUSH_OK - Nothing worth saving from this session", "Pre-Compaction Flush")
    ]


# =============================================================================
# Living Mind Act 3 — episode-integration cases (PRP test category 7)
# =============================================================================

_STRUCTURED_RESPONSE = """## Summary

The session shipped the episode writer and proved the win32 flush fix.

## Key Decisions

- Key decision: episodes carry the LLM summary, never the transcript.

## Open Threads

- TODO: run the dream consumer against the new episode.
"""


def _wire_flush(tmp_path: Path, monkeypatch, *, response_text: str) -> dict:
    """Shared harness for episode-integration flush tests."""
    vault = tmp_path / "vault"
    vault.mkdir()
    harness = {
        "vault": vault,
        "state_file": tmp_path / "flush-state.json",
        "daily": [],
        "events": [],
        "reindexed": [],
    }

    async def fake_runtime(_request):
        return SimpleNamespace(
            text=response_text,
            provider="test-provider",
            model="test-model",
            cost_usd=0.0,
        )

    def record_daily(text, section):
        harness["daily"].append((text, section))
        harness["events"].append("daily_log")

    monkeypatch.setattr(memory_flush, "FLUSH_STATE_FILE", harness["state_file"])
    monkeypatch.setattr(memory_flush, "MEMORY_DIR", vault)
    monkeypatch.setattr(memory_flush, "run_with_runtime_lanes", fake_runtime)
    monkeypatch.setattr(memory_flush, "append_to_daily_log", record_daily)
    monkeypatch.setattr(
        memory_flush,
        "_reindex_episode",
        lambda path: harness["reindexed"].append(path),
    )
    return harness


@pytest.mark.asyncio
async def test_flush_ok_produces_no_episode(tmp_path: Path, monkeypatch) -> None:
    harness = _wire_flush(tmp_path, monkeypatch, response_text="FLUSH_OK")
    context_file = tmp_path / "session-flush-telegram-1111111111-2222222222-20260612-100000.md"
    context_file.write_text("**User:** hi\n", encoding="utf-8")

    result = await memory_flush.run_flush(context_file)

    assert result is None
    episodes_dir = harness["vault"] / "episodes"
    assert not episodes_dir.exists() or not list(episodes_dir.glob("*.md"))


@pytest.mark.asyncio
async def test_flushed_session_writes_episode_after_daily_log(
    tmp_path: Path, monkeypatch
) -> None:
    harness = _wire_flush(tmp_path, monkeypatch, response_text=_STRUCTURED_RESPONSE)

    import episodes as episodes_mod

    real_writer = episodes_mod.write_episode_from_flush

    def recording_writer(*args, **kwargs):
        harness["events"].append("episode")
        return real_writer(*args, **kwargs)

    monkeypatch.setattr(episodes_mod, "write_episode_from_flush", recording_writer)

    context_file = tmp_path / "session-flush-telegram-1111111111-2222222222-20260612-100000.md"
    context_file.write_text("**User:** decision turn\n", encoding="utf-8")

    result = await memory_flush.run_flush(context_file)

    assert result is not None
    assert harness["daily"], "daily-log consumer must run"
    episode_files = list((harness["vault"] / "episodes").glob("*.md"))
    assert len(episode_files) == 1
    # Daily-log consumer stays FIRST; the episode write follows it.
    assert harness["events"].index("daily_log") < harness["events"].index("episode")
    # Best-effort reindex ran for the written episode.
    assert harness["reindexed"] == [episode_files[0]]


@pytest.mark.asyncio
async def test_test_mode_writes_no_episode(tmp_path: Path, monkeypatch) -> None:
    harness = _wire_flush(tmp_path, monkeypatch, response_text=_STRUCTURED_RESPONSE)
    context_file = tmp_path / "session-flush-telegram-1111111111-2222222222-20260612-100000.md"
    context_file.write_text("**User:** decision turn\n", encoding="utf-8")

    result = await memory_flush.run_flush(context_file, test_mode=True)

    assert result is not None
    episodes_dir = harness["vault"] / "episodes"
    assert not episodes_dir.exists() or not list(episodes_dir.glob("*.md"))
    assert not harness["reindexed"]


@pytest.mark.asyncio
async def test_episode_writer_failure_is_fail_open(
    tmp_path: Path, monkeypatch
) -> None:
    """Daily log already written; a raising writer never breaks the flush."""
    harness = _wire_flush(tmp_path, monkeypatch, response_text=_STRUCTURED_RESPONSE)

    import episodes as episodes_mod

    def exploding_writer(*_args, **_kwargs):
        raise RuntimeError("episode writer down")

    monkeypatch.setattr(episodes_mod, "write_episode_from_flush", exploding_writer)

    context_file = tmp_path / "session-flush-telegram-1111111111-2222222222-20260612-100000.md"
    context_file.write_text("**User:** decision turn\n", encoding="utf-8")

    result = await memory_flush.run_flush(context_file)

    assert result is not None  # flush returned normally
    assert harness["daily"], "daily-log entry must still be written"
    assert harness["daily"][0][0] == _STRUCTURED_RESPONSE.strip()


@pytest.mark.asyncio
async def test_reindex_failure_is_fail_open(tmp_path: Path, monkeypatch) -> None:
    harness = _wire_flush(tmp_path, monkeypatch, response_text=_STRUCTURED_RESPONSE)

    def exploding_reindex(_path):
        raise RuntimeError("index down")

    monkeypatch.setattr(memory_flush, "_reindex_episode", exploding_reindex)

    context_file = tmp_path / "session-flush-telegram-1111111111-2222222222-20260612-100000.md"
    context_file.write_text("**User:** decision turn\n", encoding="utf-8")

    result = await memory_flush.run_flush(context_file)

    assert result is not None
    # Episode itself still landed; only the reindex step failed.
    assert list((harness["vault"] / "episodes").glob("*.md"))
    assert harness["daily"]


@pytest.mark.asyncio
async def test_flush_state_schema_unchanged_no_episode_keys(
    tmp_path: Path, monkeypatch
) -> None:
    harness = _wire_flush(tmp_path, monkeypatch, response_text=_STRUCTURED_RESPONSE)
    context_file = tmp_path / "session-flush-telegram-1111111111-2222222222-20260612-100000.md"
    context_file.write_text("**User:** decision turn\n", encoding="utf-8")

    await memory_flush.run_flush(context_file)

    state = json.loads(harness["state_file"].read_text(encoding="utf-8"))
    assert set(state.keys()) == {
        "last_flush",
        "context_file",
        "last_flushed_session_id",
        "result",
    }
