"""discord_voice/transcript.py — the sidecar's rolling transcript writer.

STDLIB-ONLY module imported directly by the MAIN venv (that's the design:
no pytest-asyncio, no py-cord). Covers: header shape pinned to the SHARED
sid regex · rotation uniqueness with an existing pending · cap-once ·
fail-open on every filesystem precondition · reader-under-writer thread
smoke (whole rows only — validates the open-write-close-per-row contract).
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

from discord_voice import transcript as transcript_mod
from discord_voice.transcript import (
    MAX_FILE_BYTES,
    SID_RE,
    TRUNCATION_MARKER,
    TranscriptWriter,
)


def test_disabled_writer_is_a_total_noop(tmp_path: Path) -> None:
    writer = TranscriptWriter(None)
    assert writer.enabled is False
    assert writer.start(1, 2) == ""
    writer.append("user", "hello")  # must not raise
    assert list(tmp_path.iterdir()) == []


def test_start_writes_a_typed_header_matching_the_shared_regex(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    writer = TranscriptWriter(path)

    sid = writer.start(11, 22)

    assert SID_RE.match(sid), sid
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["type"] == "header"
    assert header["sessionId"] == sid
    assert header["guildId"] == 11
    assert header["channelId"] == 22
    assert header["startedAt"]


def test_append_writes_final_rows_and_filters_roles(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    writer = TranscriptWriter(path)
    writer.start(1, 2)

    writer.append("user", "  what's the plan  ")
    writer.append("assistant", "ship the debrief")
    writer.append("system", "forged role")  # dropped
    writer.append("user", "   ")  # blank dropped

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()[1:]]
    assert [(r["role"], r["text"]) for r in rows] == [
        ("user", "what's the plan"),
        ("assistant", "ship the debrief"),
    ]


def test_rotation_produces_unique_pending_names(tmp_path: Path, monkeypatch) -> None:
    """Double channel-switch: two rotations must coexist — a static
    sibling name clobbers (or FileExistsError-fails) on Windows."""

    path = tmp_path / "t.jsonl"
    writer = TranscriptWriter(path)
    clock = {"now": 1000.0}
    monkeypatch.setattr(transcript_mod.time, "time", lambda: clock["now"])

    writer.start(1, 2)
    writer.append("user", "session one words")
    clock["now"] = 2000.0
    writer.start(1, 3)  # rotation 1
    writer.append("user", "session two words")
    clock["now"] = 3000.0
    writer.start(1, 4)  # rotation 2

    pendings = sorted(tmp_path.glob("t.jsonl.pending-*"))
    assert len(pendings) == 2
    assert path.exists()  # the fresh live file
    headers = [
        json.loads(p.read_text(encoding="utf-8").splitlines()[0]) for p in pendings
    ]
    assert {h["channelId"] for h in headers} == {2, 3}


def test_cap_marker_is_write_once(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "t.jsonl"
    writer = TranscriptWriter(path)
    writer.start(1, 2)
    monkeypatch.setattr(transcript_mod, "MAX_FILE_BYTES", 200)

    for i in range(20):
        writer.append("user", f"row {i} with plenty of words to cross the cap")

    lines = path.read_text(encoding="utf-8").splitlines()
    markers = [line for line in lines if TRUNCATION_MARKER in line]
    assert len(markers) == 1
    assert lines[-1] == markers[0]  # nothing appended after the marker
    assert path.stat().st_size < 400  # bounded


def test_append_survives_deleted_file_and_missing_dir(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "t.jsonl"
    writer = TranscriptWriter(path)
    writer.start(1, 2)
    path.unlink()  # deleted underneath

    writer.append("user", "recreated by append")  # must not raise

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["text"] == "recreated by append"


def test_reader_under_writer_yields_whole_rows_only(tmp_path: Path) -> None:
    """Validates the open-write-close-per-row contract: a reader sampling
    mid-conversation never sees a torn row (final newline-framed lines)."""

    path = tmp_path / "t.jsonl"
    writer = TranscriptWriter(path)
    writer.start(1, 2)
    stop = threading.Event()

    def pump() -> None:
        i = 0
        while not stop.is_set():
            writer.append("user", f"concurrent row {i} with words")
            i += 1

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    try:
        for _ in range(50):
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[
                :-1
            ]:
                # Every COMPLETE line (all but a possibly-in-flight tail)
                # must parse as a whole row.
                record = json.loads(line)
                assert record["type"] in ("header", "turn")
    finally:
        stop.set()
        thread.join(timeout=3)
