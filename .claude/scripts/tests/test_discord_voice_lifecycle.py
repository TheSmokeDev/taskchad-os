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


# --- cross-platform launch + teardown --------------------------------------
#
# The sidecar shipped Windows-only: `.venv/Scripts/python.exe` (so `uv sync`
# on a Mac produced a venv the launcher could not see) and a POSIX kill that
# was a bare `os.kill(pid, 9)`: no tree, no SIGTERM step, no reap. These
# pin the OS branch each seam takes, in both directions.


def _platform(monkeypatch, name):
    import discord_voice_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle.sys, "platform", name)
    return lifecycle


def _posix_host(monkeypatch, name="linux"):
    """Fake a POSIX host, including the primitives Windows does not define.

    ``os.killpg`` / ``os.getpgid`` / ``os.WNOHANG`` / ``signal.SIGKILL`` are
    POSIX-only names, so the branch tests would blow up with AttributeError
    on the Windows dev box. Install stand-ins ONLY where the real host lacks
    them; on a real POSIX runner the genuine values stay in place.
    """

    lifecycle = _platform(monkeypatch, name)
    for module, attr, stand_in in (
        (lifecycle.os, "getpgid", lambda _pid: 0),
        (lifecycle.os, "killpg", lambda _pgid, _sig: None),
        (lifecycle.signal, "SIGKILL", 9),
    ):
        if not hasattr(module, attr):
            monkeypatch.setattr(module, attr, stand_in, raising=False)
    return lifecycle


def _boom(message):
    def _raise(*_a, **_k):
        raise AssertionError(message)

    return _raise


def _group_spy(monkeypatch, lifecycle, *, members_after_term=False):
    """Record real signals; answer the signal-0 group-liveness probe.

    ``_group_alive`` probes with signal 0, so a spy that recorded it as an
    ordinary signal would make every assertion unreadable. Probes answer
    from ``members_after_term`` (ProcessLookupError meaning "group empty")
    and are kept out of the recorded list.
    """

    signals = []
    state = {"terminated": False}

    def fake_killpg(pgid, sig):
        if sig == 0:
            if state["terminated"] and not members_after_term:
                raise ProcessLookupError(pgid)
            return None
        signals.append(("killpg", pgid, sig))
        if sig == lifecycle.signal.SIGTERM:
            state["terminated"] = True

    monkeypatch.setattr(lifecycle.os, "killpg", fake_killpg)
    monkeypatch.setattr(
        lifecycle.os, "kill", lambda pid, sig: signals.append(("kill", pid, sig))
    )
    monkeypatch.setattr(
        lifecycle.subprocess, "run", _boom("taskkill must not run off Windows")
    )
    return signals


def test_sidecar_python_windows_layout(monkeypatch):
    lifecycle = _platform(monkeypatch, "win32")

    assert lifecycle._sidecar_python().parts[-2:] == ("Scripts", "python.exe")


@pytest.mark.parametrize("plat", ["linux", "darwin"])
def test_sidecar_python_posix_layout(monkeypatch, plat):
    """uv sync writes .venv/bin/python on POSIX. The hardcoded Windows
    layout made every Mac/Linux join die on "sidecar venv missing"."""

    lifecycle = _platform(monkeypatch, plat)

    assert lifecycle._sidecar_python().parts[-2:] == ("bin", "python")


def test_kill_tree_posix_signals_the_process_group(monkeypatch):
    """POSIX teardown must take the TREE (killpg), the way taskkill /T does
    on Windows, not just the bridge pid."""

    lifecycle = _posix_host(monkeypatch)
    monkeypatch.setattr(lifecycle, "_SIDECAR_PROC", _FakeProc(4242, returncode=None))
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 17)
    monkeypatch.setattr(lifecycle.os, "getpgid", lambda pid: 4242 if pid == 4242 else 17)
    signals = _group_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)  # died on TERM

    assert lifecycle._kill_tree(4242) is True
    assert signals == [("killpg", 4242, lifecycle.signal.SIGTERM)]


def test_kill_tree_posix_escalates_when_a_child_outlives_the_leader(monkeypatch):
    """The bridge installs a SIGTERM handler, so the LEADER is usually the
    first thing to go while a busy tool child keeps running. Escalating on
    leader liveness alone would skip SIGKILL and leave exactly the orphan
    the group kill exists to prevent, so the decision reads the GROUP."""

    lifecycle = _posix_host(monkeypatch)
    monkeypatch.setattr(lifecycle, "_SIDECAR_PROC", _FakeProc(4242, returncode=None))
    monkeypatch.setattr(lifecycle, "_POSIX_TERM_GRACE_S", 0.0)
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 17)
    monkeypatch.setattr(lifecycle.os, "getpgid", lambda pid: 4242 if pid == 4242 else 17)
    signals = _group_spy(monkeypatch, lifecycle, members_after_term=True)
    # Leader dead the whole time; only the group probe still reports members.
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)

    assert lifecycle._kill_tree(4242) is True  # the leader IS dead
    assert signals == [
        ("killpg", 4242, lifecycle.signal.SIGTERM),
        ("killpg", 4242, lifecycle.signal.SIGKILL),
    ]


def test_kill_tree_posix_never_signals_its_own_group(monkeypatch):
    """A sidecar spawned before start_new_session shipped shares OUR process
    group, and killpg on it would take the orchestration API down with the
    sidecar. That case must fall back to the single pid."""

    lifecycle = _posix_host(monkeypatch)
    monkeypatch.setattr(lifecycle, "_SIDECAR_PROC", _FakeProc(555, returncode=None))
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 17)
    monkeypatch.setattr(lifecycle.os, "getpgid", lambda _pid: 17)  # same group
    signals = _group_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle.os, "killpg", _boom("killpg on our own group"))
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)

    lifecycle._kill_tree(555)

    assert signals == [("kill", 555, lifecycle.signal.SIGTERM)]


def test_kill_tree_posix_refuses_the_group_for_a_non_leader(monkeypatch):
    """start_new_session guarantees a real sidecar leads its own group, so
    pgid != pid means this pid is NOT a sidecar we started (stale state, pid
    recycled). Signalling its group would take an unrelated tree down; the
    blast radius must stay at the one process the old code already risked."""

    lifecycle = _posix_host(monkeypatch)
    monkeypatch.setattr(lifecycle, "_SIDECAR_PROC", _FakeProc(555, returncode=None))
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 17)
    monkeypatch.setattr(lifecycle.os, "getpgid", lambda pid: 17 if pid == 0 else 99)
    signals = _group_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle.os, "killpg", _boom("killpg on a non-leader pid"))
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)

    lifecycle._kill_tree(555)

    assert signals == [("kill", 555, lifecycle.signal.SIGTERM)]


def test_kill_tree_posix_refuses_the_group_without_an_owned_handle(monkeypatch):
    """Shape is not identity. A pid can look like a perfect sidecar (leads
    its own group, not ours) and still be a stranger the OS recycled the
    number onto. Only the live Popen handle from THIS process's _spawn
    proves it is our sidecar, and an unreaped child's pid cannot be
    recycled at all. Without that handle (the state after an api restart)
    the group is refused and the blast radius falls back to one process."""

    lifecycle = _posix_host(monkeypatch)
    monkeypatch.setattr(lifecycle, "_SIDECAR_PROC", None)
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 17)
    monkeypatch.setattr(lifecycle.os, "getpgid", lambda pid: 4242 if pid == 4242 else 17)
    signals = _group_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle.os, "killpg", _boom("killpg on an unverified pid"))
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)

    lifecycle._kill_tree(4242)

    assert signals == [("kill", 4242, lifecycle.signal.SIGTERM)]


def test_kill_tree_posix_refuses_the_group_for_an_exited_handle(monkeypatch):
    """Once our child has exited and been reaped, its pid is recyclable
    again, so the handle stops proving anything about who holds it now."""

    lifecycle = _posix_host(monkeypatch)
    monkeypatch.setattr(lifecycle, "_SIDECAR_PROC", _FakeProc(4242, returncode=0))
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 17)
    monkeypatch.setattr(lifecycle.os, "getpgid", lambda pid: 4242 if pid == 4242 else 17)
    signals = _group_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle.os, "killpg", _boom("killpg on a reaped pid"))
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)

    lifecycle._kill_tree(4242)

    assert signals == [("kill", 4242, lifecycle.signal.SIGTERM)]


def test_kill_tree_refuses_to_kill_this_process(monkeypatch):
    """A restart can recycle the old sidecar's pid onto the API process
    itself. taskkill /T /F and the single-pid SIGTERM fallback are both
    fatal to the caller, so this pid is refused outright and reported
    unverified (the live transcript stays untouched)."""

    lifecycle = _posix_host(monkeypatch)
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 555)
    signals = _group_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle.os, "kill", _boom("signalled ourselves"))
    monkeypatch.setattr(lifecycle.os, "killpg", _boom("signalled our own group"))

    assert lifecycle._kill_tree(555) is False
    assert signals == []


def test_kill_tree_windows_still_taskkills(monkeypatch):
    """No Windows regression: the tree kill stays taskkill /T /F."""

    lifecycle = _platform(monkeypatch, "win32")
    argvs = []
    monkeypatch.setattr(lifecycle.os, "killpg", _boom("killpg on Windows"), raising=False)
    monkeypatch.setattr(
        lifecycle.subprocess, "run", lambda argv, **_k: argvs.append(list(argv))
    )
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)

    assert lifecycle._kill_tree(4242) is True
    assert argvs == [["taskkill", "/T", "/F", "/PID", "4242"]]


def test_kill_tree_refuses_non_positive_pid(monkeypatch):
    """os.kill(0, SIGKILL) signals OUR OWN process group and os.kill(-1, ...)
    signals everything we may signal, so a corrupt state file must never turn
    /talk leave into a self-kill."""

    lifecycle = _posix_host(monkeypatch)
    monkeypatch.setattr(lifecycle.os, "kill", _boom("signalled a non-pid"))
    monkeypatch.setattr(lifecycle.os, "killpg", _boom("signalled a non-pid"))

    assert lifecycle._kill_tree(0) is True
    assert lifecycle._kill_tree(-1) is True


class _FakeProc:
    """A stand-in for the _spawn Popen handle. ``returncode=None`` is a live
    child (unreaped, so its pid cannot be recycled); an int is one that has
    already exited."""

    def __init__(self, pid, returncode=0):
        self.pid = pid
        self.returncode = returncode
        self.polls = 0

    def poll(self):
        self.polls += 1
        return self.returncode


def test_is_alive_reaps_our_own_sidecar_zombie(monkeypatch):
    """A killed child stays a zombie until its parent waits on it, and
    os.kill(pid, 0) answers "alive" for a zombie. Unreaped, every POSIX
    /talk leave would report a survivor and skip the transcript sweep."""

    lifecycle = _posix_host(monkeypatch)
    proc = _FakeProc(4242)
    monkeypatch.setattr(lifecycle, "_SIDECAR_PROC", proc)
    monkeypatch.setattr(lifecycle.shared, "is_pid_alive", lambda _pid: False)

    assert lifecycle._is_alive(4242) is False
    assert proc.polls == 1


def test_is_alive_never_reaps_a_pid_we_do_not_own(monkeypatch):
    """The exit-status theft this avoids: a bare os.waitpid(pid, WNOHANG) on
    a stale state pid the OS recycled onto ANOTHER of the API's children
    still succeeds, consuming that process's exit status out from under its
    real Popen owner. Reaping goes through our own handle or not at all."""

    lifecycle = _posix_host(monkeypatch)
    other = _FakeProc(111)  # a different subprocess of this API process
    monkeypatch.setattr(lifecycle, "_SIDECAR_PROC", other)
    monkeypatch.setattr(lifecycle.os, "waitpid", _boom("waitpid on a foreign pid"))
    monkeypatch.setattr(lifecycle.shared, "is_pid_alive", lambda _pid: True)

    assert lifecycle._is_alive(4242) is True
    assert other.polls == 0


def test_is_alive_without_a_tracked_child(monkeypatch):
    """After an API restart the sidecar is no longer our child, so no zombie
    can exist and the plain liveness probe is the whole answer."""

    lifecycle = _posix_host(monkeypatch)
    monkeypatch.setattr(lifecycle, "_SIDECAR_PROC", None)
    monkeypatch.setattr(lifecycle.os, "waitpid", _boom("waitpid without a handle"))
    monkeypatch.setattr(lifecycle.shared, "is_pid_alive", lambda _pid: True)

    assert lifecycle._is_alive(4242) is True


def test_is_alive_does_not_reap_on_windows(monkeypatch):
    """Windows has no zombies; the ctypes probe is the whole answer there."""

    lifecycle = _platform(monkeypatch, "win32")
    proc = _FakeProc(4242)
    monkeypatch.setattr(lifecycle, "_SIDECAR_PROC", proc)
    monkeypatch.setattr(lifecycle.shared, "is_pid_alive", lambda _pid: True)

    assert lifecycle._is_alive(4242) is True
    assert proc.polls == 0


def test_is_alive_rejects_non_positive_pid(monkeypatch):
    lifecycle = _posix_host(monkeypatch)
    proc = _FakeProc(0)
    monkeypatch.setattr(lifecycle, "_SIDECAR_PROC", proc)
    monkeypatch.setattr(lifecycle.shared, "is_pid_alive", _boom("probed a non-pid"))

    assert lifecycle._is_alive(0) is False
    assert lifecycle._is_alive(-1) is False
    assert proc.polls == 0


def _spawn_kwargs(monkeypatch, tmp_path, plat, previous=None):
    """Run _spawn with every external seam faked; return the Popen kwargs."""

    lifecycle = _platform(monkeypatch, plat)
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(lifecycle, "_sidecar_python", lambda: python)
    monkeypatch.setattr(lifecycle, "_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(lifecycle, "_transcript_path", lambda: tmp_path / "t.jsonl")
    monkeypatch.setattr(lifecycle, "_active_profile_root", lambda: tmp_path)
    monkeypatch.setattr(lifecycle, "_write_state", lambda _state: None)
    monkeypatch.setattr(lifecycle, "_sidecar_status", lambda: {"connected": False})
    monkeypatch.setattr(
        lifecycle, "get_scrubbed_sdk_env", lambda **_k: {"PATH": "/usr/bin"}
    )
    captured = {}

    class _BootingProc:
        pid = 4242

        def poll(self):
            return None  # still booting, never exited

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        captured["argv"] = list(argv)
        return _BootingProc()

    monkeypatch.setattr(lifecycle.subprocess, "Popen", fake_popen)
    # _spawn assigns the module-global handle; route it through monkeypatch
    # so the fake does not outlive this test.
    monkeypatch.setattr(lifecycle, "_SIDECAR_PROC", previous)
    lifecycle._spawn(lifecycle._base_state())
    return captured


@pytest.mark.parametrize("plat", ["linux", "darwin"])
def test_spawn_posix_starts_a_new_session(monkeypatch, tmp_path, plat):
    """start_new_session is what makes the sidecar's process group its own;
    without it the teardown's killpg would hit the API process's group."""

    kwargs = _spawn_kwargs(monkeypatch, tmp_path, plat)

    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs


def test_spawn_windows_keeps_the_new_process_group_flag(monkeypatch, tmp_path):
    """Asserting against the real constant would be vacuous on a POSIX
    runner, where production and the assertion both resolve the missing name
    to 0 and the test passes without proving the flag is propagated at all.
    A sentinel makes the propagation observable on every host."""

    sentinel = 0xABCD
    monkeypatch.setattr(
        lifecycle.subprocess, "CREATE_NEW_PROCESS_GROUP", sentinel, raising=False
    )

    kwargs = _spawn_kwargs(monkeypatch, tmp_path, "win32")

    assert kwargs["creationflags"] == sentinel
    assert "start_new_session" not in kwargs


def test_spawn_reaps_the_previous_handle_before_replacing_it(monkeypatch, tmp_path):
    """A respawn (crash then rejoin) overwrites the module-global handle.
    Dropping the old one unreaped orphans that child: nothing else can wait
    on it, so its zombie would read as alive for the rest of this process's
    life and every later liveness answer about that pid would be wrong."""

    previous = _FakeProc(999, returncode=None)

    _spawn_kwargs(monkeypatch, tmp_path, "linux", previous=previous)

    assert previous.polls == 1


def test_kill_tree_deadlines_are_monotonic(monkeypatch):
    """Kill deadlines must survive a wall-clock step (ntp correction, dst, a
    manual set). time.time() jumping backwards would stretch the wait,
    forwards would collapse it into no wait at all."""

    lifecycle = _posix_host(monkeypatch)
    monkeypatch.setattr(lifecycle, "_SIDECAR_PROC", _FakeProc(4242, returncode=None))
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 17)
    monkeypatch.setattr(lifecycle.os, "getpgid", lambda pid: 4242 if pid == 4242 else 17)
    _group_spy(monkeypatch, lifecycle)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: False)
    seen = {"monotonic": 0, "wall": 0}
    real_monotonic, real_time = time.monotonic, time.time

    def spy_monotonic():
        seen["monotonic"] += 1
        return real_monotonic()

    def spy_time():
        seen["wall"] += 1
        return real_time()

    monkeypatch.setattr(lifecycle.time, "monotonic", spy_monotonic)
    monkeypatch.setattr(lifecycle.time, "time", spy_time)

    lifecycle._kill_tree(4242)

    assert seen["monotonic"] > 0
    assert seen["wall"] == 0


# --- TALK_PREFER_CODEX_OAUTH directive on a live session --------------------


def test_a_live_metered_session_is_rejoined_under_the_directive(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The already-joined shortcut must not keep a key-backed session alive
    once the billing directive is on — it would meter the operator while the
    reply says "already live"."""

    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "1")
    monkeypatch.setattr(shared, "is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        lifecycle,
        "_sidecar_status",
        lambda: {"connected": True, "channelId": 555, "authSource": "configured"},
    )
    posts: list[tuple[str, dict]] = []

    def fake_post(path: str, body: dict, timeout: float) -> dict:
        posts.append((path, body))
        return {"ok": True, "channelId": body["channelId"], "authSource": "codex-oauth"}

    monkeypatch.setattr(lifecycle, "_control_post", fake_post)
    state = lifecycle._base_state()
    state.update(status="ready", pid=777, guildId=1, channelId=555)
    lifecycle._write_state(state)

    result = lifecycle.start_session(1, 555)

    assert posts == [("/join", {"guildId": 1, "channelId": 555, "textChannelId": None})]
    assert result.get("alreadyJoined") is not True
    assert result["bridge"]["authSource"] == "codex-oauth"


def test_a_live_subscription_session_still_short_circuits(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directive only forces a re-join when the live session is metered."""

    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "1")
    monkeypatch.setattr(shared, "is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        lifecycle,
        "_sidecar_status",
        lambda: {"connected": True, "channelId": 555, "authSource": "codex-oauth"},
    )

    def boom(*_args, **_kwargs) -> dict:  # pragma: no cover - must not run
        raise AssertionError("a compliant live session must not be torn down")

    monkeypatch.setattr(lifecycle, "_control_post", boom)
    state = lifecycle._base_state()
    state.update(status="ready", pid=777, guildId=1, channelId=555)
    lifecycle._write_state(state)

    assert lifecycle.start_session(1, 555)["alreadyJoined"] is True


def test_the_directive_refusal_surfaces_instead_of_a_metered_session(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the re-join cannot meet the directive, the operator gets the
    both-doors refusal rather than a silently-metered "already live"."""

    from runtime import openai_platform_auth

    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "1")
    monkeypatch.setattr(shared, "is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        lifecycle,
        "_sidecar_status",
        lambda: {"connected": True, "channelId": 555, "authSource": "env"},
    )
    monkeypatch.setattr(
        lifecycle,
        "_control_post",
        lambda path, body, timeout: {
            "ok": False,
            "error": openai_platform_auth.PREFER_CODEX_UNAVAILABLE_MESSAGE,
        },
    )
    state = lifecycle._base_state()
    state.update(status="ready", pid=777, guildId=1, channelId=555)
    lifecycle._write_state(state)

    with pytest.raises(lifecycle.DiscordVoiceError) as caught:
        lifecycle.start_session(1, 555)

    assert "codex login" in str(caught.value)
    assert "TALK_PREFER_CODEX_OAUTH" in str(caught.value)


def test_a_broken_directive_check_never_blocks_joining(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail OPEN: the check is a billing guard, not a gate on voice itself."""

    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "1")
    monkeypatch.setattr(shared, "is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        lifecycle,
        "_sidecar_status",
        lambda: {"connected": True, "channelId": 555, "authSource": "configured"},
    )
    import talk_session

    def explode() -> bool:
        raise RuntimeError("resolver unavailable")

    monkeypatch.setattr(talk_session, "talk_prefer_codex_oauth", explode)

    def boom(*_args, **_kwargs) -> dict:  # pragma: no cover - must not run
        raise AssertionError("a failed check must not force a teardown")

    monkeypatch.setattr(lifecycle, "_control_post", boom)
    state = lifecycle._base_state()
    state.update(status="ready", pid=777, guildId=1, channelId=555)
    lifecycle._write_state(state)

    assert lifecycle.start_session(1, 555)["alreadyJoined"] is True


# ─── sidecar profile root — the identity round-trip ────────────────────────
# get_scrubbed_sdk_env forces the child's HOMIE_HOME to _active_profile_root().
# The child RE-DERIVES its profile from that value, so for the default profile
# it must round-trip back to "default" (install-dir vault/memory paths).
# The pre-fix value (the repo root) reclassified the child as "custom" and
# re-rooted MEMORY_DIR at <repo>/memory — nonexistent — collapsing the voice
# identity prompt to the bare preamble ("knows the name, denies everything").


def test_active_profile_root_default_roundtrips_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from personas import get_active_profile_name
    from personas.core import get_default_paths, get_persona_paths

    monkeypatch.delenv("HOMIE_HOME", raising=False)
    root = lifecycle._active_profile_root()

    assert root == (Path.home() / ".homie").resolve(strict=False)

    # Simulate the child: HOMIE_HOME forced to the spawn value.
    monkeypatch.setenv("HOMIE_HOME", str(root))
    child_profile = get_active_profile_name()
    assert child_profile == "default"
    # And the child's memory dir is the real install-dir vault, not <repo>/memory.
    assert (
        get_persona_paths(child_profile)["memory"]
        == get_default_paths()["memory"]
    )


def test_repo_root_homie_home_reclassifies_as_custom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Locks the failure mode itself: the OLD spawn value (repo root) makes the
    child resolve a "custom" profile — proving the round-trip test above
    guards the real bug, not a tautology."""
    from personas import get_active_profile_name
    from personas.core import get_default_paths

    monkeypatch.delenv("HOMIE_HOME", raising=False)
    old_value = get_default_paths()["memory"].parent.parent

    monkeypatch.setenv("HOMIE_HOME", str(old_value))
    assert get_active_profile_name() == "custom"


def test_active_profile_root_custom_roundtrips_same_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker/custom deployments (HOMIE_HOME outside ~/.homie): the child must
    receive the SAME root the parent resolved. resolve_profile_root("custom")
    would append profiles/custom and re-root the child's memory (codex r1)."""
    from personas import get_active_profile_name
    from personas.core import get_persona_paths

    custom_root = (tmp_path / "container-homie").resolve()
    custom_root.mkdir()
    monkeypatch.setenv("HOMIE_HOME", str(custom_root))
    assert get_active_profile_name() == "custom"
    parent_memory = get_persona_paths("custom")["memory"]

    root = lifecycle._active_profile_root()
    assert root == custom_root
    assert "profiles" not in root.parts

    # Simulate the child: same HOMIE_HOME -> same profile, same memory dir.
    monkeypatch.setenv("HOMIE_HOME", str(root))
    child_profile = get_active_profile_name()
    assert child_profile == "custom"
    assert get_persona_paths(child_profile)["memory"] == parent_memory
