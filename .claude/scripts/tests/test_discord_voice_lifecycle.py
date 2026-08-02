"""Discord voice lifecycle tests — state machine, pid probes, spawn guard.

``config.STATE_DIR`` is redirected to ``tmp_path`` so the real state file
never moves; sidecar control calls and pid liveness are monkeypatched — no
subprocess, no HTTP, no Discord.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
import discord_voice_lifecycle as lifecycle
import shared


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    return tmp_path


# ─── state-file state machine ──────────────────────────────────────────────


def test_read_state_missing_file_is_base(state_dir: Path) -> None:
    state = lifecycle._read_state()

    assert state["status"] == "stopped"
    assert state["pid"] is None
    assert state["channelId"] is None


def test_write_then_read_roundtrip(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "_is_alive", lambda _pid: True)
    state = lifecycle._base_state()
    state.update(status="ready", pid=1234, guildId=1, channelId=99)

    lifecycle._write_state(state)
    loaded = lifecycle._read_state()

    assert loaded["status"] == "ready"
    assert loaded["pid"] == 1234
    assert loaded["channelId"] == 99


def test_read_state_unreadable_file_is_stale(state_dir: Path) -> None:
    lifecycle._state_path().write_text("{not json", encoding="utf-8")

    state = lifecycle._read_state()

    assert state["status"] == "stale"
    assert state["lastError"] == "state file unreadable"


def test_read_state_dead_pid_marks_stale(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared, "is_pid_alive", lambda _pid: False)
    state = lifecycle._base_state()
    state.update(status="ready", pid=4242, channelId=77)
    lifecycle._write_state(state)

    loaded = lifecycle._read_state()

    assert loaded["status"] == "stale"
    assert loaded["pid"] is None


# ─── _is_alive ──────────────────────────────────────────────────────────────


def test_is_alive_converts_and_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    monkeypatch.setattr(shared, "is_pid_alive", lambda pid: seen.append(pid) or True)

    assert lifecycle._is_alive("123") is True
    assert seen == [123]
    assert lifecycle._is_alive(None) is False
    assert lifecycle._is_alive("not-a-pid") is False


# ─── stop_session ───────────────────────────────────────────────────────────


def test_stop_session_without_pid(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "_is_alive", lambda _pid: False)

    result = lifecycle.stop_session()

    assert result["status"] == "stopped"
    assert result["pid"] is None
    assert result["stoppedAt"] is not None
    on_disk = json.loads(lifecycle._state_path().read_text(encoding="utf-8"))
    assert on_disk["status"] == "stopped"


# ─── start_session ──────────────────────────────────────────────────────────


def test_start_session_already_joined_short_circuits(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shared, "is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        lifecycle,
        "_sidecar_status",
        lambda: {"connected": True, "channelId": 555, "authSource": "configured"},
    )

    def boom(*_args, **_kwargs) -> dict:  # pragma: no cover - must not run
        raise AssertionError("control call must not fire on the already-joined path")

    monkeypatch.setattr(lifecycle, "_control_post", boom)
    state = lifecycle._base_state()
    state.update(status="ready", pid=777, guildId=1, channelId=555)
    lifecycle._write_state(state)

    result = lifecycle.start_session(1, 555)

    assert result["alreadyJoined"] is True
    assert result["bridge"]["channelId"] == 555
    assert result["pid"] == 777


def test_start_session_join_via_control_post(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shared, "is_pid_alive", lambda _pid: True)
    # Bridge is alive but parked on a DIFFERENT channel -> re-join.
    monkeypatch.setattr(lifecycle, "_sidecar_status", lambda: {"connected": True, "channelId": 111})
    posts: list[tuple[str, dict]] = []

    def fake_post(path: str, body: dict, timeout: float) -> dict:
        posts.append((path, body))
        return {"ok": True, "channelId": body["channelId"], "authSource": "codex-oauth"}

    monkeypatch.setattr(lifecycle, "_control_post", fake_post)
    state = lifecycle._base_state()
    state.update(status="ready", pid=777)
    lifecycle._write_state(state)

    result = lifecycle.start_session(1, 222, text_channel_id=333)

    assert posts == [("/join", {"guildId": 1, "channelId": 222, "textChannelId": 333})]
    assert result["status"] == "ready"
    assert result["channelId"] == 222
    assert result["bridge"]["authSource"] == "codex-oauth"


def test_start_session_control_error_raises(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shared, "is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(lifecycle, "_sidecar_status", lambda: {"connected": False})
    monkeypatch.setattr(
        lifecycle, "_control_post", lambda *_a, **_k: {"ok": False, "error": "join refused"}
    )
    state = lifecycle._base_state()
    state.update(status="ready", pid=777)
    lifecycle._write_state(state)

    with pytest.raises(lifecycle.DiscordVoiceError, match="join refused"):
        lifecycle.start_session(1, 222)


def test_start_session_missing_sidecar_venv_raises(
    state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "_is_alive", lambda _pid: False)
    monkeypatch.setattr(
        lifecycle, "_sidecar_python", lambda: tmp_path / "nope" / "Scripts" / "python.exe"
    )

    with pytest.raises(lifecycle.DiscordVoiceError, match="sidecar venv missing"):
        lifecycle.start_session(1, 222)


# --- _kill_tree (the taskkill that had never executed) ---------------------


def test_kill_tree_actually_reaches_subprocess_run(monkeypatch):
    """Regression for the live bug: the old inline kill called
    subprocess.Popen(argv, capture_output=..., timeout=...) — kwargs Popen
    does not accept — so a swallowed TypeError meant the taskkill NEVER ran
    and the sidecar survived every /talk leave. This pins the argv that
    actually reaches subprocess.run."""

    import discord_voice_lifecycle as lifecycle

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))

        class _Done:
            returncode = 0

        return _Done()

    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)
    monkeypatch.setattr(lifecycle.sys, "platform", "win32")

    lifecycle._kill_tree(4242)

    assert calls == [["taskkill", "/T", "/F", "/PID", "4242"]]


def test_stop_session_uses_kill_tree(monkeypatch, tmp_path):
    """stop_session must route through the module-level _kill_tree seam."""

    import config
    import discord_voice_lifecycle as lifecycle

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    killed = []
    monkeypatch.setattr(lifecycle, "_kill_tree", killed.append)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: pid == 555)
    monkeypatch.setattr(lifecycle, "_control_post", lambda *a, **k: {"ok": True})
    (tmp_path / "discord-voice-session.json").write_text(
        '{"status": "ready", "pid": 555}', encoding="utf-8"
    )

    state = lifecycle.stop_session()

    assert killed == [555]
    assert state["status"] == "stopped"


# --- vault-debrief transcript sweep ----------------------------------------


import json as _json
import os
import time


def _lifecycle(monkeypatch, tmp_path):
    import config
    import discord_voice_lifecycle as lifecycle

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.delenv("HOMIE_KILLSWITCH_VOICE", raising=False)
    return lifecycle


def _write_transcript(path, sid="dv-abc123def456abc123def456", rows=3):
    lines = [
        _json.dumps(
            {
                "type": "header",
                "sessionId": sid,
                "startedAt": "2026-08-02T12:00:00",
                "guildId": 1,
                "channelId": 2,
            }
        )
    ]
    for i in range(rows):
        role = "user" if i % 2 == 0 else "assistant"
        lines.append(
            _json.dumps(
                {"type": "turn", "role": role, "text": f"turn {i} with real words", "ts": 1.0 + i}
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _flush_spy(monkeypatch, lifecycle):
    calls = []

    def fake_flush(rows, *, session_id, started_at=None, origin="dashboard /talk"):
        calls.append(
            {
                "rows": rows,
                "session_id": session_id,
                "started_at": started_at,
                "origin": origin,
            }
        )
        return {"status": "started", "contextFile": f"session-flush-talk-{session_id}-x.md"}

    monkeypatch.setattr(lifecycle.talk_flush, "start_session_flush", fake_flush)
    return calls


def test_stop_session_sweeps_and_returns_debrief(monkeypatch, tmp_path):
    lifecycle = _lifecycle(monkeypatch, tmp_path)
    calls = _flush_spy(monkeypatch, lifecycle)
    order = []
    monkeypatch.setattr(
        lifecycle, "_control_post", lambda *a, **k: order.append("leave") or {"ok": True}
    )
    monkeypatch.setattr(
        lifecycle, "_kill_tree", lambda pid: (order.append("kill"), True)[1]
    )
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: pid == 777)
    (tmp_path / "discord-voice-session.json").write_text(
        '{"status": "ready", "pid": 777}', encoding="utf-8"
    )
    _write_transcript(lifecycle._transcript_path())

    real_sweep = lifecycle._sweep_transcripts
    monkeypatch.setattr(
        lifecycle,
        "_sweep_transcripts",
        lambda **kw: order.append("sweep") or real_sweep(**kw),
    )

    state = lifecycle.stop_session()

    assert order == ["leave", "kill", "sweep"]  # flush strictly after the kill
    assert calls and calls[0]["session_id"] == "dv-abc123def456abc123def456"
    assert calls[0]["origin"] == "discord voice channel"
    assert calls[0]["started_at"] == "2026-08-02T12:00:00"
    assert state["debrief"] and state["debrief"][0]["status"] == "started"
    assert not lifecycle._transcript_path().exists()


def test_crash_then_leave_still_flushes(monkeypatch, tmp_path):
    """Dead pid + leftover transcript: the likeliest operator recovery
    (/talk leave after a crash) must still deliver the debrief."""

    lifecycle = _lifecycle(monkeypatch, tmp_path)
    calls = _flush_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)
    monkeypatch.setattr(
        lifecycle, "_kill_tree", lambda pid: (_ for _ in ()).throw(AssertionError("no kill"))
    )
    _write_transcript(lifecycle._transcript_path())

    state = lifecycle.stop_session()

    assert len(calls) == 1
    assert state["debrief"][0]["status"] == "started"


def test_multi_file_stop_flushes_each_with_own_header(monkeypatch, tmp_path):
    lifecycle = _lifecycle(monkeypatch, tmp_path)
    calls = _flush_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)
    base = lifecycle._transcript_path()
    _write_transcript(base, sid="dv-" + "a" * 24)
    _write_transcript(base.with_name(base.name + ".pending-100"), sid="dv-" + "b" * 24)
    _write_transcript(base.with_name(base.name + ".pending-200"), sid="dv-" + "c" * 24)

    state = lifecycle.stop_session()

    sids = {c["session_id"] for c in calls}
    assert sids == {"dv-" + "a" * 24, "dv-" + "b" * 24, "dv-" + "c" * 24}
    assert len(state["debrief"]) == 3
    leftovers = list(tmp_path.glob("discord-voice-transcript.jsonl*"))
    assert leftovers == []


def test_torn_tail_line_is_dropped(monkeypatch, tmp_path):
    lifecycle = _lifecycle(monkeypatch, tmp_path)
    calls = _flush_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)
    path = lifecycle._transcript_path()
    _write_transcript(path, rows=2)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"type": "turn", "role": "user", "te')  # torn mid-write

    lifecycle.stop_session()

    assert len(calls[0]["rows"]) == 2  # the torn line cost itself only


def test_headerless_file_gets_deterministic_fallback_sid(monkeypatch, tmp_path):
    lifecycle = _lifecycle(monkeypatch, tmp_path)
    calls = _flush_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)
    path = lifecycle._transcript_path()
    path.write_text(
        _json.dumps({"type": "turn", "role": "user", "text": "no header here", "ts": 1.0})
        + "\n",
        encoding="utf-8",
    )
    content = path.read_text(encoding="utf-8")

    lifecycle.stop_session()
    sid_first = calls[0]["session_id"]

    # Re-plant the SAME bytes: the fallback id must be identical so any
    # dedup layer keyed on session id can catch the re-sweep.
    path.write_text(content, encoding="utf-8")
    lifecycle.stop_session()

    assert sid_first.startswith("dv-x")
    assert calls[1]["session_id"] == sid_first


def test_killswitch_disabled_parks_without_flushing(monkeypatch, tmp_path):
    lifecycle = _lifecycle(monkeypatch, tmp_path)
    calls = _flush_spy(monkeypatch, lifecycle)
    monkeypatch.setenv("HOMIE_KILLSWITCH_VOICE", "disabled")
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)
    _write_transcript(lifecycle._transcript_path())

    state = lifecycle.stop_session()

    assert calls == []
    assert state["debrief"][0]["status"] == "kept-disabled"
    parked = list(tmp_path.glob("*.disabled"))
    assert len(parked) == 1
    assert state["status"] == "stopped"


def test_planted_claim_blocks_double_flush(monkeypatch, tmp_path):
    lifecycle = _lifecycle(monkeypatch, tmp_path)
    calls = _flush_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)
    base = lifecycle._transcript_path()
    _write_transcript(base)
    # Someone else already owns this file (existing .claimed target makes
    # the claim rename fail on Windows).
    base.with_name(base.name + ".claimed").write_text("owned", encoding="utf-8")

    lifecycle.stop_session()

    assert calls == []


def test_alive_session_live_file_untouched_pending_swept(monkeypatch, tmp_path):
    lifecycle = _lifecycle(monkeypatch, tmp_path)
    calls = _flush_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: True)
    monkeypatch.setattr(
        lifecycle,
        "_sidecar_status",
        lambda: {"connected": True, "channelId": 2},
    )
    monkeypatch.setattr(lifecycle, "_control_post", lambda *a, **k: {"ok": True})
    (tmp_path / "discord-voice-session.json").write_text(
        '{"status": "ready", "pid": 42}', encoding="utf-8"
    )
    base = lifecycle._transcript_path()
    _write_transcript(base, sid="dv-" + "d" * 24)  # LIVE session's file
    _write_transcript(base.with_name(base.name + ".pending-1"), sid="dv-" + "e" * 24)

    lifecycle.start_session(1, 2)

    assert [c["session_id"] for c in calls] == ["dv-" + "e" * 24]
    assert base.exists()  # the live file was never touched


def test_spawn_branch_sweeps_live_file_before_spawn(monkeypatch, tmp_path):
    lifecycle = _lifecycle(monkeypatch, tmp_path)
    calls = _flush_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)
    monkeypatch.setattr(lifecycle, "_sidecar_status", lambda: None)
    _write_transcript(lifecycle._transcript_path(), sid="dv-" + "f" * 24)

    def fake_spawn(state):
        assert calls, "sweep must run BEFORE spawn"
        raise lifecycle.DiscordVoiceError("stop here")

    monkeypatch.setattr(lifecycle, "_spawn", fake_spawn)

    try:
        lifecycle.start_session(1, 2)
    except lifecycle.DiscordVoiceError:
        pass

    assert calls[0]["session_id"] == "dv-" + "f" * 24


def test_hostile_header_sid_falls_back_deterministically(monkeypatch, tmp_path):
    lifecycle = _lifecycle(monkeypatch, tmp_path)
    calls = _flush_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)
    path = lifecycle._transcript_path()
    header = {"type": "header", "sessionId": "../../evil\nname", "startedAt": "x"}
    rows = [{"type": "turn", "role": "user", "text": "real words here", "ts": 1.0}]
    path.write_text(
        "\n".join(_json.dumps(r) for r in [header, *rows]) + "\n", encoding="utf-8"
    )

    lifecycle.stop_session()

    # Hostile sid fails the regex → deterministic content hash id, which is
    # filename-safe by construction.
    assert calls[0]["session_id"].startswith("dv-x")


def test_stale_claims_requeue_and_old_disabled_are_purged(monkeypatch, tmp_path):
    """A crash in the claim→flush window must not destroy the only copy:
    the stale .claimed requeues as a pending and gets flushed (with the
    deterministic fallback sid); week-old .disabled files age out."""

    lifecycle = _lifecycle(monkeypatch, tmp_path)
    calls = _flush_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)
    base = lifecycle._transcript_path()
    stale_claim = base.with_name(base.name + ".pending-9.claimed")
    stale_claim.write_text("crashed flush", encoding="utf-8")
    old = time.time() - 2 * 60 * 60
    os.utime(stale_claim, (old, old))
    old_disabled = base.with_name(base.name + ".pending-8.disabled")
    old_disabled.write_text("parked", encoding="utf-8")
    week_plus = time.time() - 8 * 24 * 60 * 60
    os.utime(old_disabled, (week_plus, week_plus))

    lifecycle.stop_session()

    assert not stale_claim.exists()
    # The requeued file was picked up in the SAME sweep and flushed under
    # the content-hash fallback sid.
    assert len(calls) == 1
    assert calls[0]["session_id"].startswith("dv-x")
    assert not old_disabled.exists()


def test_leave_route_passes_debrief_through(monkeypatch):
    import discord_voice_api
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        discord_voice_api.discord_voice_lifecycle,
        "stop_session",
        lambda: {"status": "stopped", "debrief": [{"file": "x", "status": "started"}]},
    )
    app = FastAPI()
    app.include_router(discord_voice_api.router)
    client = TestClient(app)

    resp = client.post("/api/discord/voice/leave")

    assert resp.status_code == 200
    assert resp.json()["debrief"][0]["status"] == "started"


# --- codex R1 fixes — each pins one found defect ---------------------------


def test_claimed_and_disabled_files_never_match_the_pending_sweep(
    monkeypatch, tmp_path
):
    """The naive .pending-* glob matched .pending-1.claimed (double flush)
    and .pending-1.disabled (privacy violation after switch re-enable)."""

    lifecycle = _lifecycle(monkeypatch, tmp_path)
    calls = _flush_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)
    base = lifecycle._transcript_path()
    _write_transcript(base.with_name(base.name + ".pending-1.claimed"), sid="dv-" + "1" * 24)
    _write_transcript(base.with_name(base.name + ".pending-2.disabled"), sid="dv-" + "2" * 24)
    _write_transcript(base.with_name(base.name + ".pending-3"), sid="dv-" + "3" * 24)

    lifecycle.stop_session()

    assert [c["session_id"] for c in calls] == ["dv-" + "3" * 24]
    # Parked/claimed files untouched by the sweep proper (the disabled one
    # is young enough to survive the purge).
    assert base.with_name(base.name + ".pending-1.claimed").exists()
    assert base.with_name(base.name + ".pending-2.disabled").exists()


def test_error_receipt_requeues_instead_of_deleting(monkeypatch, tmp_path):
    """A transient flush failure must never destroy the ONLY transcript
    copy — the file requeues as a pending and the next sweep retries."""

    lifecycle = _lifecycle(monkeypatch, tmp_path)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)
    outcomes = iter([{"status": "error", "reason": "spawn failed"}, {"status": "started"}])
    calls = []

    def flaky_flush(rows, *, session_id, started_at=None, origin="dashboard /talk"):
        calls.append(session_id)
        return next(outcomes)

    monkeypatch.setattr(lifecycle.talk_flush, "start_session_flush", flaky_flush)
    _write_transcript(lifecycle._transcript_path())

    first = lifecycle.stop_session()
    assert first["debrief"][0]["status"] == "error"
    survivors = [p.name for p in tmp_path.iterdir() if ".pending-" in p.name]
    assert len(survivors) == 1  # requeued, not deleted

    second = lifecycle.stop_session()
    assert second["debrief"][0]["status"] == "started"
    assert len(calls) == 2
    assert not any(".pending-" in p.name for p in tmp_path.iterdir())


def test_survived_kill_protects_the_live_transcript(monkeypatch, tmp_path):
    """taskkill failing (pid still alive after the poll) must NOT sweep the
    live file — the sidecar may still be writing into it."""

    lifecycle = _lifecycle(monkeypatch, tmp_path)
    calls = _flush_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: True)
    monkeypatch.setattr(lifecycle, "_control_post", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(lifecycle, "_kill_tree", lambda pid: False)  # survived
    (tmp_path / "discord-voice-session.json").write_text(
        '{"status": "ready", "pid": 999}', encoding="utf-8"
    )
    base = lifecycle._transcript_path()
    _write_transcript(base, sid="dv-" + "9" * 24)
    _write_transcript(base.with_name(base.name + ".pending-5"), sid="dv-" + "8" * 24)

    state = lifecycle.stop_session()

    assert [c["session_id"] for c in calls] == ["dv-" + "8" * 24]  # pending only
    assert base.exists()  # live file protected
    assert "survived" in (state.get("lastError") or "")


def test_startup_sweep_takes_the_lifecycle_lock(monkeypatch, tmp_path):
    lifecycle = _lifecycle(monkeypatch, tmp_path)
    _flush_spy(monkeypatch, lifecycle)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)
    monkeypatch.setattr(lifecycle, "_sidecar_status", lambda: None)
    locked = []

    class _Lock:
        def __enter__(self):
            locked.append(True)
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(lifecycle.shared, "file_lock", lambda path: _Lock())

    lifecycle.sweep_orphan_transcripts()

    assert locked == [True]


def test_rotation_failure_disables_session_and_recovers_next_start(tmp_path, monkeypatch):
    """A failed rotation must preserve the predecessor AND disable only
    THIS session — a process-lifetime disable would silently record
    nothing for every later session (codex r2)."""

    import discord_voice.transcript as transcript_mod
    from discord_voice.transcript import TranscriptWriter

    path = tmp_path / "t.jsonl"
    writer = TranscriptWriter(path)
    writer.start(1, 2)
    writer.append("user", "predecessor session words")
    before = path.read_text(encoding="utf-8")

    fail = {"on": True}
    real_replace = transcript_mod.os.replace

    def flaky_replace(src, dst):
        if fail["on"]:
            raise OSError("sharing violation")
        return real_replace(src, dst)

    monkeypatch.setattr(transcript_mod.os, "replace", flaky_replace)
    sid = writer.start(1, 3)

    assert sid == ""
    writer.append("user", "must go nowhere")
    # The predecessor was NEITHER truncated NOR appended into.
    assert path.read_text(encoding="utf-8") == before

    # Transient lock clears: the NEXT start recovers fully.
    fail["on"] = False
    sid2 = writer.start(1, 4)
    assert sid2.startswith("dv-")
    writer.append("user", "new session recording works")
    pendings = list(tmp_path.glob("t.jsonl.pending-*"))
    assert len(pendings) == 1  # the predecessor got rotated after all
    assert "predecessor session words" in pendings[0].read_text(encoding="utf-8")
    assert "new session recording works" in path.read_text(encoding="utf-8")


def test_error_requeue_is_bounded_and_parks_as_failed(monkeypatch, tmp_path):
    """An always-failing flush must not ping-pong the plaintext forever:
    after _REQUEUE_MAX attempts the file parks as .failed (codex r2)."""

    lifecycle = _lifecycle(monkeypatch, tmp_path)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)
    calls = []

    def always_error(rows, *, session_id, started_at=None, origin="dashboard /talk"):
        calls.append(session_id)
        return {"status": "error", "reason": "persistent failure"}

    monkeypatch.setattr(lifecycle.talk_flush, "start_session_flush", always_error)
    _write_transcript(lifecycle._transcript_path())

    for _ in range(6):  # far past the bound
        lifecycle.stop_session()

    assert len(calls) == lifecycle._REQUEUE_MAX  # attempts bounded
    parked = list(tmp_path.glob("*.failed"))
    assert len(parked) == 1
    assert not any(".pending-" in p.name and not p.name.endswith(".failed") for p in tmp_path.iterdir())


def test_same_instant_rotations_do_not_collide(tmp_path, monkeypatch):
    import discord_voice.transcript as transcript_mod
    from discord_voice.transcript import TranscriptWriter

    path = tmp_path / "t.jsonl"
    writer = TranscriptWriter(path)
    monkeypatch.setattr(transcript_mod.time, "time", lambda: 1000.0)  # frozen clock

    writer.start(1, 2)
    writer.append("user", "session one")
    writer.start(1, 3)  # rotation at t=1000
    writer.append("user", "session two")
    writer.start(1, 4)  # rotation ALSO at t=1000

    pendings = list(tmp_path.glob("t.jsonl.pending-*"))
    assert len(pendings) == 2  # uuid suffix keeps same-ms rotations apart
