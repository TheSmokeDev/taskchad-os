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


def _is_alive(pid: object) -> bool:
    try:
        return shared.is_pid_alive(int(pid))
    except (TypeError, ValueError):
        return False


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
    """

    try:
        if sys.platform == "win32":
            subprocess.run(  # noqa: S603 — fixed argv
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            os.kill(pid, 9)
    except Exception:
        pass
    deadline = time.time() + 2.0
    while time.time() < deadline and _is_alive(pid):
        time.sleep(0.1)
    return not _is_alive(pid)


def _sidecar_python() -> Path:
    return SIDECAR_DIR / ".venv" / "Scripts" / "python.exe"


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
            if bridge and bridge.get("connected") and bridge.get("channelId") == channel_id:
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
            state["lastError"] = "sidecar survived taskkill — live transcript not swept"
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

    proc = subprocess.Popen([str(python), "bridge.py"], **popen_kwargs)

    deadline = time.time() + 30.0
    while time.time() < deadline:
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
