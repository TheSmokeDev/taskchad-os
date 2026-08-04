"""Discord voice sidecar lifecycle — Python-owned single-session supervisor.

Mirrors the cabinet voice lifecycle: state file under the profile state
dir, log file under the profile log dir, subprocess tracked by pid, ready
probe against the sidecar's loopback control server. The sidecar itself
(py-cord + DAVE) lives in ``.claude/scripts/discord_voice/`` with its own
venv — this module only spawns, probes, and reaps it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

import config
import personas.services as _persona_services
import shared
import talk_flush
from discord_voice.transcript import SID_RE
from runtime.subprocess_env import get_scrubbed_sdk_env
from security import kill_switches

_log = logging.getLogger(__name__)

SIDECAR_DIR = Path(__file__).resolve().parent / "discord_voice"
CONTROL_BASE = "http://127.0.0.1:7861"

_JOIN_TIMEOUT_S = 60.0
#: POSIX teardown only: how long the sidecar's process group gets to honor
#: SIGTERM before the SIGKILL escalation. Windows has no equivalent step
#: (``taskkill /F`` is unconditional), so this cost is POSIX-side.
_POSIX_TERM_GRACE_S = 1.5

#: The vault-debrief transcript contract with the sidecar (see
#: discord_voice/transcript.py for the format and naming scheme).
_TRANSCRIPT_NAME = "discord-voice-transcript.jsonl"
_TRANSCRIPT_ORIGIN = "discord voice channel"
#: A `.claimed` older than this is a crashed flush — purge it.
_CLAIM_STALE_S = 60 * 60
#: `.disabled` transcripts (kill-switch parked) age out unflushed.
_DISABLED_MAX_AGE_S = 7 * 24 * 60 * 60
_DISABLED_MAX_COUNT = 5


class DiscordVoiceError(Exception):
    """Lifecycle failure (spawn, ready probe, or sidecar control call)."""


def _state_path() -> Path:
    return Path(config.STATE_DIR) / "discord-voice-session.json"


def _lock_path() -> Path:
    # shared.file_lock appends ".lock" -> <state>/discord-voice-session.lock
    return Path(config.STATE_DIR) / "discord-voice-session"


def _log_dir() -> Path:
    return _persona_services.get_log_dir() / "discord-voice"


def _base_state() -> dict[str, Any]:
    return {
        "status": "stopped",  # stopped | starting | ready | failed | stale
        "pid": None,
        "guildId": None,
        "channelId": None,
        "startedAt": None,
        "readyAt": None,
        "stoppedAt": None,
        "lastError": None,
        "logPath": None,
    }


def _read_state() -> dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return _base_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = _base_state()
        state["status"] = "stale"
        state["lastError"] = "state file unreadable"
        return state
    state = _base_state()
    if isinstance(raw, dict):
        state.update(raw)
    pid = state.get("pid")
    if pid is not None and not _is_alive(pid):
        if state.get("status") in {"starting", "ready"}:
            state["status"] = "stale"
        state["pid"] = None
    return state


def _write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


#: The ``Popen`` handle from THIS process's ``_spawn``, kept for one reason:
#: reaping our own child. A killed child stays a zombie until its parent
#: waits on it, and ``os.kill(pid, 0)`` (what ``shared.is_pid_alive`` uses on
#: POSIX) answers "alive" for a zombie, so an unreaped sidecar would read as
#: a survivor after every successful kill. ``None`` after an API restart,
#: where the sidecar is no longer our child and no zombie can exist.
_SIDECAR_PROC: subprocess.Popen | None = None


def _reap_sidecar(pid: int) -> None:
    """Reap OUR sidecar child, and only ours. Matters on POSIX (zombies);
    harmless and handle-releasing on Windows, where ``_spawn`` calls it on
    respawn regardless of platform.

    ``Popen.poll()`` waits on the handle we own, so it cannot consume the
    exit status of some other subprocess the orchestration API launched. A
    bare ``os.waitpid(pid, os.WNOHANG)`` can: a stale state-file pid that
    the OS has recycled onto another of our children is still a child, so
    the wait succeeds and steals that process's exit status out from under
    its real owner.
    """

    proc = _SIDECAR_PROC
    if proc is None or proc.pid != pid:
        return
    try:
        proc.poll()
    except Exception:  # noqa: BLE001 - reaping is best-effort
        pass


def _is_alive(pid: object) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        # Not a real sidecar pid. On POSIX ``os.kill(0, 0)`` probes OUR OWN
        # process group and reports "alive", which would aim the teardown at
        # the API process itself; ``-1`` is worse. Corrupt state file in,
        # dead sidecar out.
        return False
    if sys.platform != "win32":
        _reap_sidecar(pid_int)
    return shared.is_pid_alive(pid_int)


#: STRICT pending shape — the naive ``.pending-*`` glob also matched
#: ``.pending-1.claimed`` (double-flush) and ``.pending-1.disabled``
#: (privacy violation: parked files re-flushed after the switch flips on).
#: The optional ``-rN`` tail is the bounded-retry counter for requeued
#: error-receipt files.
_PENDING_RE = re.compile(r"\.pending-\d+(-[0-9a-f]{6})?(-r(?P<retry>\d))?$")
#: An error-receipt transcript retries at most this many sweeps, then parks
#: as ``.failed`` (operator-recoverable, aged out with the disabled files)
#: — an unbounded requeue would ping-pong plaintext forever.
_REQUEUE_MAX = 3


def _transcript_path() -> Path:
    return Path(config.STATE_DIR) / _TRANSCRIPT_NAME


def _pending_files(base: Path) -> list[Path]:
    return sorted(
        p
        for p in base.parent.glob(base.name + ".pending-*")
        if _PENDING_RE.search(p.name)
    )


def _sweep_transcripts(*, include_live: bool) -> list[dict[str, Any]]:
    """The ONE flush mechanism: claim-by-rename → flush → delete.

    ``include_live`` gates the live-named file — sweep it ONLY when the
    sidecar is known dead (stop_session post-kill; start_session's spawn
    branch), or a live/rotating session gets destroyed mid-write.
    ``.pending-*`` files are always fair game: the bridge never appends to
    one after rotation. Fully fail-open; returns per-file receipts.
    """

    receipts: list[dict[str, Any]] = []
    try:
        base = _transcript_path()
        _purge_stale_transcripts(base)
        candidates = _pending_files(base)
        if include_live and base.exists():
            candidates.append(base)
        if not candidates:
            return receipts
        disabled = kill_switches.is_disabled("voice")
        for path in candidates:
            try:
                if disabled:
                    # Privacy-first: the operator turned voice OFF. Park the
                    # plaintext transcript; it is NEVER auto-flushed and ages
                    # out unflushed (_purge_stale_transcripts). Flushing it
                    # weeks later would invert the operator's intent.
                    os.replace(path, path.with_name(path.name + ".disabled"))
                    receipts.append({"file": path.name, "status": "kept-disabled"})
                    continue
                claimed = path.with_name(path.name + ".claimed")
                try:
                    # rename, NOT replace: an existing .claimed means another
                    # sweep owns this file — losing the race must SKIP.
                    os.rename(path, claimed)
                except OSError:
                    continue
                receipt = _flush_claimed(claimed)
                receipts.append({"file": path.name, **receipt})
                if receipt.get("status") in ("started", "skipped"):
                    # Terminal outcomes only: `started` spawned the flush,
                    # `skipped` is a semantic gate (retrying would skip
                    # forever). Anything else keeps the ONLY copy.
                    try:
                        claimed.unlink()
                    except OSError:
                        # Delete failed (AV scan, handle lag): .claimed is
                        # never re-swept, so no double-flush; the stale-claim
                        # purge removes it later.
                        pass
                else:
                    # error/unreadable: transient — requeue as a pending so
                    # the NEXT sweep retries instead of destroying the
                    # transcript's only copy. BOUNDED: past _REQUEUE_MAX the
                    # file parks as .failed (operator-recoverable, aged out
                    # with the disabled files) instead of ping-ponging
                    # between pending and claimed forever.
                    match = _PENDING_RE.search(path.name)
                    attempt = int((match.group("retry") if match else None) or 0) + 1
                    try:
                        if attempt >= _REQUEUE_MAX:
                            os.replace(
                                claimed, path.with_name(path.name + ".failed")
                            )
                            receipts[-1]["status"] = "parked-failed"
                        else:
                            os.replace(
                                claimed,
                                path.with_name(
                                    f"{base.name}.pending-{int(time.time() * 1000)}"
                                    f"-{hashlib.sha1(path.name.encode()).hexdigest()[:6]}"
                                    f"-r{attempt}"
                                ),
                            )
                    except OSError:
                        pass  # stays .claimed; stale purge is the backstop
            except Exception as exc:  # noqa: BLE001 — one bad file costs itself
                _log.warning("transcript sweep failed for %s: %s", path.name, exc)
    except Exception as exc:  # noqa: BLE001 — sweeping is best-effort, always
        _log.warning("transcript sweep failed: %s", exc)
    return receipts


def _flush_claimed(path: Path) -> dict[str, Any]:
    """Parse a claimed transcript file and hand it to the talk flush."""

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"status": "unreadable", "reason": str(exc)[:120]}
    header: dict[str, Any] = {}
    rows: list[dict[str, str]] = []
    for line in raw.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # torn/garbage line costs itself only
        if not isinstance(rec, dict):
            continue
        if rec.get("type") == "header" and not header:
            header = rec
        elif rec.get("type") == "turn":
            rows.append(
                {"role": str(rec.get("role") or ""), "text": str(rec.get("text") or "")}
            )
    sid = str(header.get("sessionId") or "")
    if not SID_RE.match(sid):
        # Headerless/torn-header file: derive a DETERMINISTIC id from the
        # content so a re-sweep of the same bytes dedups instead of minting
        # a fresh wall-clock id (which would defeat every dedup layer).
        sid = "dv-x" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    started = header.get("startedAt")
    return talk_flush.start_session_flush(
        rows,
        session_id=sid,
        started_at=str(started) if started else None,
        origin=_TRANSCRIPT_ORIGIN,
    )


def _purge_stale_transcripts(base: Path) -> None:
    """Purge crashed claims (>1h) and age/count-cap parked `.disabled` files."""

    try:
        now = time.time()
        for claimed in base.parent.glob(base.name + "*.claimed"):
            try:
                if now - claimed.stat().st_mtime > _CLAIM_STALE_S:
                    # A crash in the claim→flush window left this orphan.
                    # REQUEUE rather than destroy (K3 design-gate
                    # refinement): the deterministic content-hash fallback
                    # sid makes a re-flush dedup-safe, so the only copy of
                    # a conversation survives a mid-sweep crash.
                    os.replace(
                        claimed,
                        base.parent
                        / (
                            f"{base.name}.pending-{int(now * 1000)}"
                            f"-{hashlib.sha1(claimed.name.encode()).hexdigest()[:6]}"
                        ),
                    )
            except OSError:
                pass
        parked_files = sorted(
            [
                *base.parent.glob(base.name + "*.disabled"),
                *base.parent.glob(base.name + "*.failed"),
            ],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for index, parked in enumerate(parked_files):
            try:
                too_old = now - parked.stat().st_mtime > _DISABLED_MAX_AGE_S
                if too_old or index >= _DISABLED_MAX_COUNT:
                    parked.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass


def sweep_orphan_transcripts() -> list[dict[str, Any]]:
    """Startup-time sweep (orchestration API boot): bounds the
    stranded-forever paths (crash / kicked / WS-drop with no later
    lifecycle touch) at "until the next API restart". Sweeps the live file
    only when the sidecar is verifiably dead."""

    if "PYTEST_CURRENT_TEST" in os.environ:
        # Inert under pytest (the talk_runs._history_enabled precedent):
        # TestClient context managers fire startup hooks, and a test-run
        # sweep against the operator's REAL state dir would flush real
        # transcripts and spawn real memory_flush subprocesses. Tests that
        # want the sweep call _sweep_transcripts directly.
        return []
    try:
        # Under the SAME lifecycle lock start/stop hold: an unlocked sweep
        # could evaluate "sidecar dead", lose the CPU to a locked
        # start_session that spawns and joins, then claim the brand-new
        # live transcript. The lock serializes the decision with the spawn.
        with shared.file_lock(_lock_path()):
            state = _read_state()
            sidecar_dead = (
                not _is_alive(state.get("pid")) and _sidecar_status() is None
            )
            return _sweep_transcripts(include_live=sidecar_dead)
    except Exception as exc:  # noqa: BLE001
        _log.warning("orphan transcript sweep failed: %s", exc)
        return []


def _sidecar_group(pid: int) -> int | None:
    """The pid's process group when a group kill is PROVABLY safe, else ``None``.

    A group kill is strictly wider than the single-process kill it replaces,
    so it fires only when this pid is verifiably OUR live sidecar: the
    ``Popen`` handle from this process's own ``_spawn``, still unreaped.
    That ownership test is the one that actually proves identity, and it
    also closes pid reuse outright, because the OS cannot recycle the pid of
    a child its parent has not reaped.

    Shape alone is not identity. ``getpgid(pid) == pid`` proves only that
    SOME process leads its own group, which a recycled pid can satisfy by
    accident, and killing that group would take an unrelated tree down. It
    is kept as a second gate, next to the two self-harm gates: never our own
    pid, and never our own group (a sidecar spawned BEFORE
    ``start_new_session`` shipped shares our group, so ``killpg`` there
    would take the orchestration API down with the sidecar).

    Without the handle, which is the state after an api restart, identity
    cannot be established. Be precise about what happens then, because it is
    NOT fail-closed: the caller still signals the single unverified pid, and
    only the GROUP escalation is withheld. So an unprovable pid keeps
    exactly the blast radius this code had before the group kill existed,
    one process, and gains nothing wider.

    That is deliberate, not an oversight. Refusing to signal at all would
    strand teardown after every api restart: the state-file pid is the ONLY
    handle a restarted process has on a running sidecar, so a fully closed
    gate would leave it alive through every later ``/talk leave``. Narrowing
    the radius is the win available here; eliminating it is not.
    """

    proc = _SIDECAR_PROC
    if proc is None or proc.pid != pid:
        return None
    try:
        if proc.poll() is not None:
            return None  # already exited, so the pid is recyclable again
    except Exception:  # noqa: BLE001 - unprovable identity fails closed
        return None
    if pid == os.getpid():
        return None
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return None  # gone, or not ours to look at
    if pgid != pid or pgid == os.getpgid(0):
        return None
    return pgid


def _group_alive(group: int) -> bool:
    """Does ANY process remain in the group? Signal 0 is the POSIX probe."""

    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True  # EPERM: someone is there, we just may not signal it


def _posix_kill_tree(pid: int) -> int | None:
    """SIGTERM then SIGKILL the sidecar's process group: the POSIX ``taskkill /T``.

    ``_spawn`` starts the sidecar in its own session, so its process-group id
    equals its pid and signalling the group takes the tool subprocesses it
    spawned with it. That is the tree half; a bare ``os.kill(pid, ...)``
    reaps only the bridge and orphans whatever it launched.

    One documented hole, so nobody reads this as "takes everything with it":
    a grandchild that calls ``setsid()`` (or ``start_new_session``) of its
    own accord LEAVES this group and survives both signals. It is also
    invisible to the caller's surviving-member warning, which probes the
    group it just signalled and cannot see a process that left. Windows has
    the same class of hole, where a deliberately detached process escapes
    ``taskkill /T``. Nothing the sidecar launches today does this; the note
    is here so a future tool runner that does gets caught in review.

    Escalation reads the GROUP, not the leader. The bridge honors SIGTERM
    (it installs an asyncio handler), so the leader is usually the FIRST
    thing to go while a busy tool child is still running; deciding on the
    leader alone would skip SIGKILL and leave exactly the orphan this
    function exists to prevent.

    When ``_sidecar_group`` refuses the group, this degrades to signalling
    the single pid. That is not a tree kill, and the caller's postcondition
    says so.

    Returns the group it operated on so the caller can re-probe it AFTER the
    leader dies; ``_sidecar_group`` cannot answer then, because looking a
    dead pid up raises.

    Accepted residual: the group id is a number, so once the leader is dead
    and reaped, escalating still targets that number. Reuse would need the
    whole group to empty AND the pid space to wrap onto a fresh session
    leader inside the sub-second grace window. Closing that needs per-process
    start-time identity, which has no portable stdlib answer on macOS.
    """

    group = _sidecar_group(pid)

    def _signal(sig: int) -> None:
        try:
            if group is not None:
                os.killpg(group, sig)
            else:
                os.kill(pid, sig)
        except OSError:
            pass  # already dead between probe and signal

    def _still_running() -> bool:
        alive = _is_alive(pid)  # reaps our own leader before the group probe
        return _group_alive(group) if group is not None else alive

    _signal(signal.SIGTERM)
    # monotonic, not time(): a wall-clock step (ntp correction, dst, manual
    # set) must not stretch or collapse a kill deadline.
    deadline = time.monotonic() + _POSIX_TERM_GRACE_S
    while time.monotonic() < deadline and _still_running():
        time.sleep(0.1)
    if _still_running():
        _signal(signal.SIGKILL)
    return group


def _kill_tree(pid: int) -> bool:
    """Kill the sidecar process tree, then wait briefly for actual death.

    Returns whether the pid is verifiably dead — a surviving sidecar means
    its live transcript file must NOT be swept (it may still be writing).

    THE regression this replaces: the inline kill called
    ``subprocess.Popen(argv, capture_output=True, timeout=10)`` — kwargs
    ``Popen`` does not accept — so a ``TypeError`` was raised before any
    process spawned, swallowed by the bare except, and the taskkill NEVER
    executed. The sidecar survived every ``/talk leave``. Module-level and
    ``subprocess.run``-based so a test can pin the argv that actually runs.

    The post-kill poll gives callers a real postcondition: when this
    returns, the pid is dead (or we waited ~2s trying) — which is what
    makes reading sidecar-owned files after the kill safe.

    Windows takes the tree with ``taskkill /T``; POSIX takes it with a
    process-group kill (see ``_posix_kill_tree``). The old POSIX branch was
    a bare ``os.kill(pid, 9)``: no tree, no SIGTERM step, and no reap, so
    the sidecar's own children survived and the corpse still read as alive.

    Scope of the boolean, stated precisely because it is load-bearing: it
    reports the LEADER's death, which is the whole question the transcript
    sweep asks. Only the bridge process owns ``TranscriptWriter``; a tool
    grandchild that outlives it cannot write to the transcript. A surviving
    group member is a separate defect and gets its own log line rather than
    riding this return value.
    """

    if pid <= 0:
        return True  # nothing to kill; _is_alive already treats these as dead
    if pid == os.getpid():
        # A stale state file naming the CURRENT api process, most plausibly
        # after a restart recycled the old sidecar's pid onto us. Every kill
        # path here is fatal to the caller: taskkill /T /F on Windows, and
        # SIGTERM through the single-pid fallback on POSIX (killpg is
        # already refused by _sidecar_group). Refuse, and report unverified
        # so the live transcript is left alone.
        _log.error("discord voice state names this process (pid %s); refusing to kill", pid)
        return False
    group: int | None = None
    try:
        if sys.platform == "win32":
            subprocess.run(  # noqa: S603 — fixed argv
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            group = _posix_kill_tree(pid)
    except Exception:
        pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and _is_alive(pid):
        time.sleep(0.1)
    dead = not _is_alive(pid)
    if dead and group is not None and _group_alive(group):
        # The group must be probed with the id captured BEFORE the kill: a
        # dead leader cannot be looked up any more.
        _log.warning(
            "discord voice sidecar %s is dead but process group %s still has "
            "members; a tool child outlived the SIGKILL",
            pid,
            group,
        )
    return dead


def _sidecar_python() -> Path:
    """The sidecar venv's interpreter, in that platform's venv layout.

    ``uv sync`` writes ``.venv/Scripts/python.exe`` on Windows and
    ``.venv/bin/python`` everywhere else. Hardcoding the Windows layout made
    every Mac/Linux ``/talk join`` fail the ``_spawn`` existence check with
    "sidecar venv missing" no matter how many times the operator ran the
    documented ``uv sync``.
    """

    venv = SIDECAR_DIR / ".venv"
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _active_profile_root() -> Path:
    from personas import get_active_profile_name  # noqa: PLC0415
    from personas.core import get_default_paths  # noqa: PLC0415
    from personas.lifecycle import resolve_profile_root  # noqa: PLC0415

    active = get_active_profile_name()
    if active == "default":
        return get_default_paths()["memory"].parent.parent
    return resolve_profile_root(active)


def _sidecar_status() -> dict[str, Any] | None:
    try:
        resp = httpx.get(f"{CONTROL_BASE}/status", timeout=2.0)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def status() -> dict[str, Any]:
    state = _read_state()
    bridge = _sidecar_status() if _is_alive(state.get("pid")) else None
    return {
        **state,
        "ok": True,
        "sidecarDirExists": SIDECAR_DIR.is_dir(),
        "sidecarPythonExists": _sidecar_python().is_file(),
        "bridge": bridge,
    }


def _directive_violated_by(bridge: dict[str, Any]) -> bool:
    """Is this LIVE session metering a key while the billing directive is on?

    ``TALK_PREFER_CODEX_OAUTH`` is enforced where auth is resolved — inside the
    sidecar's join. A session that was started BEFORE the directive was turned
    on never passed that gate, so the already-joined shortcut would quietly keep
    a metered key running and answer "already live". Treat that as not-joined so
    the caller re-joins under the directive, which either switches to the
    subscription or refuses. Fail OPEN on any resolution error: a broken check
    must not block joining voice.
    """

    try:
        import talk_session

        if not talk_session.talk_prefer_codex_oauth():
            return False
        from runtime import openai_platform_auth

        return bridge.get("authSource") != openai_platform_auth.SOURCE_CODEX_OAUTH
    except Exception:
        return False


def start_session(guild_id: int, channel_id: int, text_channel_id: int | None = None) -> dict[str, Any]:
    """Ensure the sidecar is running and joined to the given voice channel."""

    with shared.file_lock(_lock_path()):
        state = _read_state()
        if _is_alive(state.get("pid")):
            # Sidecar alive: the live transcript belongs to a running or
            # rotating session — NEVER sweep it here. Rotated .pending
            # files are finished predecessors and always safe.
            _sweep_transcripts(include_live=False)
            bridge = _sidecar_status()
            if (
                bridge
                and bridge.get("connected")
                and bridge.get("channelId") == channel_id
                and not _directive_violated_by(bridge)
            ):
                return {**state, "bridge": bridge, "alreadyJoined": True}
        else:
            # Pid dead AND control port dead (zombie probe): a leftover
            # live file is an orphaned session — sweep before spawning so
            # the new session starts on a clean name.
            _sweep_transcripts(include_live=_sidecar_status() is None)
            state = _spawn(state)

        result = _control_post(
            "/join",
            {"guildId": guild_id, "channelId": channel_id, "textChannelId": text_channel_id},
            timeout=_JOIN_TIMEOUT_S,
        )
        if not result.get("ok", False) and result.get("error"):
            raise DiscordVoiceError(str(result["error"]))
        state.update(
            {
                "status": "ready",
                "guildId": guild_id,
                "channelId": channel_id,
                "readyAt": time.time(),
                "lastError": None,
            }
        )
        _write_state(state)
        return {**state, "bridge": result}


def stop_session() -> dict[str, Any]:
    with shared.file_lock(_lock_path()):
        state = _read_state()
        pid = state.get("pid")
        sidecar_dead = True
        if _is_alive(pid):
            try:
                _control_post("/leave", {}, timeout=10.0)
            except DiscordVoiceError:
                pass
            sidecar_dead = _kill_tree(int(pid))
        # Vault debrief: sweep the live transcript ONLY with the sidecar
        # verifiably dead (a survived kill may still be writing into it);
        # rotated pendings are always safe. This chokepoint covers explicit
        # leave AND crash-then-leave, the likeliest operator recovery.
        debrief = _sweep_transcripts(include_live=sidecar_dead)
        state.update(
            {
                "status": "stopped",
                "pid": None,
                "guildId": None,
                "channelId": None,
                "stoppedAt": time.time(),
            }
        )
        if not sidecar_dead:
            state["lastError"] = "sidecar survived the kill; live transcript not swept"
        _write_state(state)
        return {**state, "debrief": debrief}


def _spawn(state: dict[str, Any]) -> dict[str, Any]:
    python = _sidecar_python()
    if not python.is_file():
        raise DiscordVoiceError(
            f"sidecar venv missing: {python} — run `uv sync` in .claude/scripts/discord_voice"
        )
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "discord-voice.log"

    env = get_scrubbed_sdk_env(profile_root=_active_profile_root())
    # The transcript path is a spawn contract, set AFTER the scrub (the
    # bridge snapshots it before its own load_dotenv so .env can't shadow).
    env["DISCORD_VOICE_TRANSCRIPT_PATH"] = str(_transcript_path())
    log_handle = open(log_path, "a", encoding="utf-8")
    popen_kwargs: dict[str, Any] = {
        "cwd": str(SIDECAR_DIR),
        "env": env,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        # The POSIX counterpart: own session, so the sidecar's process-group
        # id is its own pid and the teardown can killpg the whole tree
        # without the signal reaching the orchestration API that spawned it.
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen([str(python), "bridge.py"], **popen_kwargs)
    global _SIDECAR_PROC
    if _SIDECAR_PROC is not None:
        # Reap the PREVIOUS sidecar before dropping its handle. A respawn
        # (crash then rejoin) is the live case: overwriting unreaped would
        # orphan that handle, and its zombie would then read as alive for
        # the rest of the process's life with nothing left able to reap it.
        _reap_sidecar(_SIDECAR_PROC.pid)
    _SIDECAR_PROC = proc  # the only handle that can safely reap this child

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log_handle.close()
            raise DiscordVoiceError(
                f"sidecar exited during boot (code {proc.returncode}) — see {log_path}"
            )
        if _sidecar_status() is not None:
            state.update(
                {
                    "status": "starting",
                    "pid": proc.pid,
                    "startedAt": time.time(),
                    "logPath": str(log_path),
                }
            )
            _write_state(state)
            return state
        time.sleep(0.25)

    log_handle.close()
    try:
        proc.kill()
    except Exception:
        pass
    raise DiscordVoiceError(f"sidecar control server did not come up in 30s — see {log_path}")


def _control_post(path: str, body: dict, timeout: float) -> dict[str, Any]:
    try:
        resp = httpx.post(f"{CONTROL_BASE}{path}", json=body, timeout=timeout)
    except Exception as exc:
        raise DiscordVoiceError(f"sidecar control call {path} failed: {exc}") from exc
    try:
        return resp.json()
    except Exception as exc:
        raise DiscordVoiceError(f"sidecar control call {path} returned HTTP {resp.status_code}") from exc


__all__ = [
    "DiscordVoiceError",
    "start_session",
    "status",
    "stop_session",
    "sweep_orphan_transcripts",
]
