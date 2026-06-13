"""Hook filename regressions — Living Mind Act 3 win32 flush fix.

The chat lifecycle passes raw colon-bearing session keys
(``platform:channel_id:thread_id``) to the flush hooks, which embedded them
verbatim in a Windows filename — ``write_text`` raised, the hook exited 1,
and the chat slice's session-end flush was dead on win32.

DISCRIMINATING on all platforms: colons are LEGAL on POSIX, so "write
succeeded" would pass on Linux even without the fix — these tests assert the
composed FILENAME matches ``[A-Za-z0-9._-]+``, not just that no exception
escaped.

Born-clean fixtures (R2 NM2): every id below is the synthetic
``telegram:1111111111:2222222222`` family — never a real account id.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

import memory_flush

SYNTHETIC_CHAT_ID = "telegram:1111111111:2222222222"
SYNTHETIC_SAFE_ID = "telegram-1111111111-2222222222"
SYNTHETIC_UUID = "11784e97-1111-2222-3333-444444444444"

SESSION_END_NAME_RE = re.compile(r"^session-flush-[A-Za-z0-9._-]+-\d{8}-\d{6}\.md$")
PRE_COMPACT_NAME_RE = re.compile(r"^flush-context-[A-Za-z0-9._-]+-\d{8}-\d{6}\.md$")


def _load_hook_module(filename: str, module_name: str):
    hook_path = Path(__file__).resolve().parents[2] / "hooks" / filename
    spec = importlib.util.spec_from_file_location(module_name, hook_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def session_end_hook():
    return _load_hook_module("session-end-flush.py", "session_end_flush_act3_test")


@pytest.fixture
def pre_compact_hook():
    return _load_hook_module("pre-compact-flush.py", "pre_compact_flush_act3_test")


def _write_transcript(path: Path, turns: list[tuple[str, str]]) -> None:
    """message-nested JSONL — the shape a real clear transcript uses."""
    rows = [
        {"type": "message", "message": {"role": role, "content": content}}
        for role, content in turns
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def _wire_hook(hook, tmp_path: Path, monkeypatch, *, payload: dict) -> dict:
    """Standard harness: tmp STATE_DIR, captured Popen, no real spawn."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(exist_ok=True)

    harness: dict = {"popen": [], "events": [], "state_dir": state_dir}

    def fake_popen(cmd, **kwargs):
        harness["popen"].append(list(cmd))
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(hook, "STATE_DIR", state_dir)
    if hasattr(hook, "MEMORY_DIR"):
        monkeypatch.setattr(hook, "MEMORY_DIR", memory_dir)
    monkeypatch.setattr(hook, "SCRIPTS_DIR", tmp_path / "scripts")
    monkeypatch.setattr(hook, "ensure_directories", lambda: None)
    monkeypatch.setattr(hook.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        hook,
        "log_hook_execution",
        lambda _name, _source, status, _duration, detail="": harness["events"].append(
            (status, detail)
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "living_memory",
        SimpleNamespace(append_open_threads_from_flush=lambda _m, _c: 0),
    )
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    return harness


# =============================================================================
# _safe_filename_component policy (direct unit)
# =============================================================================


class TestSafeFilenameComponent:
    def test_colon_chat_key_becomes_dashes(self, session_end_hook):
        assert (
            session_end_hook._safe_filename_component(SYNTHETIC_CHAT_ID)
            == SYNTHETIC_SAFE_ID
        )

    def test_uuid_passes_through_byte_identical(self, session_end_hook):
        assert (
            session_end_hook._safe_filename_component(SYNTHETIC_UUID)
            == SYNTHETIC_UUID
        )

    def test_empty_and_symbol_only_become_unknown(self, session_end_hook):
        assert session_end_hook._safe_filename_component("") == "unknown"
        assert session_end_hook._safe_filename_component("::/\\:") == "unknown"

    def test_pre_compact_policy_identical(self, session_end_hook, pre_compact_hook):
        for value in (SYNTHETIC_CHAT_ID, SYNTHETIC_UUID, "", "a b:c"):
            assert session_end_hook._safe_filename_component(
                value
            ) == pre_compact_hook._safe_filename_component(value)


# =============================================================================
# Session-end hook — colon-bearing chat key (the live defect)
# =============================================================================


class TestSessionEndColonFix:
    def test_colon_session_id_completes_and_writes_safe_filename(
        self, session_end_hook, tmp_path, monkeypatch
    ):
        transcript = tmp_path / "clear-transcript.jsonl"
        _write_transcript(
            transcript,
            [
                ("user", "Decision: ship the episodes module today."),
                ("assistant", "Recorded. Key decision: episodes are summary-only."),
            ],
        )
        harness = _wire_hook(
            session_end_hook,
            tmp_path,
            monkeypatch,
            payload={
                "session_id": SYNTHETIC_CHAT_ID,
                "source": "clear",
                "transcript_path": str(transcript),
            },
        )

        # Pre-fix on win32 this raised OSError out of main() (exit 1).
        session_end_hook.main()

        context_files = list(harness["state_dir"].glob("session-flush-*.md"))
        assert len(context_files) == 1
        name = context_files[0].name
        # Assert the NAME, not write success — colons are legal on POSIX.
        assert SESSION_END_NAME_RE.fullmatch(name), name
        assert name.startswith(f"session-flush-{SYNTHETIC_SAFE_ID}-")
        assert ":" not in name
        assert harness["popen"], "background flush was not spawned"
        assert harness["popen"][0][-2:] == ["--context-file", str(context_files[0])]
        assert ("OK", "spawned flush") in harness["events"]

    def test_uuid_session_id_filename_byte_identical_to_today(
        self, session_end_hook, tmp_path, monkeypatch
    ):
        """Claude Code uuid ids must produce the exact pre-fix filenames."""
        transcript = tmp_path / "session.jsonl"
        _write_transcript(
            transcript,
            [
                ("user", "Two turns minimum to pass the admission gate."),
                ("assistant", "Acknowledged with a durable fact."),
            ],
        )
        harness = _wire_hook(
            session_end_hook,
            tmp_path,
            monkeypatch,
            payload={
                "session_id": SYNTHETIC_UUID,
                "source": "exit",
                "transcript_path": str(transcript),
            },
        )

        session_end_hook.main()

        context_files = list(harness["state_dir"].glob("session-flush-*.md"))
        assert len(context_files) == 1
        name = context_files[0].name
        # Byte-identity: the raw uuid appears verbatim in the composed name.
        assert name.startswith(f"session-flush-{SYNTHETIC_UUID}-")
        assert SESSION_END_NAME_RE.fullmatch(name), name

    def test_dedup_compares_raw_with_raw(
        self, session_end_hook, tmp_path, monkeypatch
    ):
        """Sanitize-at-composition only: the 60s dedup still keys the RAW id."""
        transcript = tmp_path / "session.jsonl"
        _write_transcript(
            transcript,
            [("user", "turn one decision"), ("assistant", "turn two answer")],
        )
        harness = _wire_hook(
            session_end_hook,
            tmp_path,
            monkeypatch,
            payload={
                "session_id": SYNTHETIC_CHAT_ID,
                "source": "clear",
                "transcript_path": str(transcript),
            },
        )
        dedup_path = harness["state_dir"] / "flush-dedup.json"
        dedup_path.write_text(
            json.dumps({"session_id": SYNTHETIC_CHAT_ID, "timestamp": time.time()}),
            encoding="utf-8",
        )

        with pytest.raises(SystemExit) as exc_info:
            session_end_hook.main()

        assert exc_info.value.code == 0
        assert ("SKIP", "dedup 60s") in harness["events"]
        assert not harness["popen"]
        assert not list(harness["state_dir"].glob("session-flush-*.md"))

    def test_dedup_state_written_with_raw_id_after_spawn(
        self, session_end_hook, tmp_path, monkeypatch
    ):
        transcript = tmp_path / "session.jsonl"
        _write_transcript(
            transcript,
            [("user", "decision: one"), ("assistant", "noted lesson")],
        )
        harness = _wire_hook(
            session_end_hook,
            tmp_path,
            monkeypatch,
            payload={
                "session_id": SYNTHETIC_CHAT_ID,
                "source": "clear",
                "transcript_path": str(transcript),
            },
        )

        session_end_hook.main()

        dedup = json.loads(
            (harness["state_dir"] / "flush-dedup.json").read_text(encoding="utf-8")
        )
        assert dedup["session_id"] == SYNTHETIC_CHAT_ID  # raw, not sanitized


# =============================================================================
# Pre-compact hook — symmetric fix
# =============================================================================


class TestPreCompactSymmetric:
    def test_colon_session_id_completes_and_writes_safe_filename(
        self, pre_compact_hook, tmp_path, monkeypatch
    ):
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript(
            transcript,
            [
                ("user", "Important fact before compaction."),
                ("assistant", "Saved the durable configuration detail."),
            ],
        )
        harness = _wire_hook(
            pre_compact_hook,
            tmp_path,
            monkeypatch,
            payload={
                "session_id": SYNTHETIC_CHAT_ID,
                "transcript_path": str(transcript),
            },
        )

        pre_compact_hook.main()

        context_files = list(harness["state_dir"].glob("flush-context-*.md"))
        assert len(context_files) == 1
        name = context_files[0].name
        assert PRE_COMPACT_NAME_RE.fullmatch(name), name
        assert name.startswith(f"flush-context-{SYNTHETIC_SAFE_ID}-")
        assert harness["popen"]

    def test_uuid_filename_byte_identical(
        self, pre_compact_hook, tmp_path, monkeypatch
    ):
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript(
            transcript,
            [("user", "pre-compact turn"), ("assistant", "kept context")],
        )
        harness = _wire_hook(
            pre_compact_hook,
            tmp_path,
            monkeypatch,
            payload={
                "session_id": SYNTHETIC_UUID,
                "transcript_path": str(transcript),
            },
        )

        pre_compact_hook.main()

        context_files = list(harness["state_dir"].glob("flush-context-*.md"))
        assert len(context_files) == 1
        assert context_files[0].name.startswith(f"flush-context-{SYNTHETIC_UUID}-")


# =============================================================================
# memory_flush._extract_session_id round-trips the sanitized shape (no code
# change needed — covered by test, per PRP behavior 1)
# =============================================================================


class TestExtractSessionIdRoundTrip:
    def test_sanitized_telegram_shape_round_trips(self):
        context = Path(f"session-flush-{SYNTHETIC_SAFE_ID}-20260612-100000.md")
        assert memory_flush._extract_session_id(context) == SYNTHETIC_SAFE_ID

    def test_uuid_shape_still_round_trips(self):
        context = Path(f"session-flush-{SYNTHETIC_UUID}-20260612-100000.md")
        assert memory_flush._extract_session_id(context) == SYNTHETIC_UUID

    def test_pre_compact_prefix_round_trips(self):
        context = Path(f"flush-context-{SYNTHETIC_SAFE_ID}-20260612-100000.md")
        assert memory_flush._extract_session_id(context) == SYNTHETIC_SAFE_ID
