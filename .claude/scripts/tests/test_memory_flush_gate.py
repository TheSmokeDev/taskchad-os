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
