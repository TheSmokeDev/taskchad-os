"""Shared helpers for the framework-owned agent-browser surface."""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, parse, request

import adb_control

DEFAULT_CDP_PORT = 9222
DEFAULT_TIMEOUT_SECONDS = 20
_HTTP_URL_PATTERN = re.compile(r"https?://[^\s]+")

# ── P3.0 PhoneOps + P4.0 Ghost — the browser target dimension ───────────────
# desktop | phone | ghost. The phone's and the ghost's Chrome both speak the
# same CDP protocol; `adb forward tcp:<port> localabstract:chrome_devtools_remote`
# makes each indistinguishable from 127.0.0.1:<port>, so every helper below
# works against any target. 18223/18224 are adjacent to the desktop's 18222 and
# outside the WSL-reserved 9188-9787 band. The target is resolved SERVER-SIDE
# from a strict enum — a raw CDP port or serial is never accepted from a client.
# The ghost (P4.0) is "another adb device": same transport, its own serial + CDP
# port + isolated daemon session, behind its own HOMIE_GHOST_ENABLED gate.

BROWSER_TARGETS = ("desktop", "phone", "ghost")
PHONE_CDP_DEFAULT_PORT = 18223
PHONE_CDP_ENV_NAMES = ("HOMIE_PHONE_CDP_PORT",)
GHOST_CDP_DEFAULT_PORT = 18224
GHOST_CDP_ENV_NAMES = ("HOMIE_GHOST_CDP_PORT",)

# Isolated daemon sessions (spike evidence 2026-07-06): after an on-device
# freeze event, agent-browser's DEFAULT cached session goes permanently stale
# (10060 on every op) even once the device recovers, while a named session
# reconnects instantly. Each adb target rides its OWN isolated session; desktop
# keeps the default session (byte-identical M12).
PHONE_AGENT_BROWSER_SESSION = "homie-phone"
GHOST_AGENT_BROWSER_SESSION = "homie-ghost"

# adb-transport targets (phone + ghost) vs the local desktop target.
_ADB_TARGETS = frozenset({"phone", "ghost"})

# Per-target adb serial env var. Phone may resolve None (adb_control autodetects
# a single attached device); the ghost MUST resolve its own serial and NEVER
# fall back to the phone (enforced in _resolve_adb_serial_or_raise).
_TARGET_SERIAL_ENV: dict[str, str] = {
    "phone": "HOMIE_PHONE_ADB_SERIAL",
    "ghost": "HOMIE_GHOST_ADB_SERIAL",
}


def is_adb_target(target: str | None) -> bool:
    """True when a target rides the adb / CDP-forward transport (phone or ghost)."""

    return target in _ADB_TARGETS


def resolve_target_serial(
    target: str | None, *, environ: dict[str, str] | None = None
) -> str | None:
    """The adb serial for an adb target (call-time env read, Rule 1).

    Phone / ghost read their OWN env var; a non-adb or unknown target -> None.
    Empty / whitespace -> None (treated as unset).
    """

    env = environ if environ is not None else os.environ
    name = _TARGET_SERIAL_ENV.get(target or "")
    if not name:
        return None
    return (env.get(name) or "").strip() or None


def _resolve_adb_serial_or_raise(
    target: str | None, *, environ: dict[str, str] | None = None
) -> str | None:
    """Resolve the serial for an adb action, REFUSING wrong-device fallback.

    The ghost must drive its OWN device: if HOMIE_GHOST_ADB_SERIAL is unset we
    raise rather than let the adb call fall back to the phone's serial (which
    would silently drive the operator's personal phone — a wrong-target leak).

    Symmetrically (PhoneOps review F1, issue #89): once a ghost serial is
    configured, phone-target actions must NOT ride single-device autodetect —
    with only the ghost attached, an un-scoped adb call under the phone label
    silently drives the ghost. Phone autodetect stays allowed only while no
    ghost serial is configured (byte-identical for ghost-less deployments).
    """

    serial = resolve_target_serial(target, environ=environ)
    if target == "ghost" and not serial:
        raise RuntimeError(
            "HOMIE_GHOST_ADB_SERIAL is not set — cannot reach the ghost device"
        )
    if target == "phone" and not serial and resolve_target_serial("ghost", environ=environ):
        raise RuntimeError(
            "HOMIE_PHONE_ADB_SERIAL is not set while a ghost serial is configured — "
            "phone autodetect could drive the ghost; set HOMIE_PHONE_ADB_SERIAL to "
            "the phone's adb serial"
        )
    # Misconfig guard: if the ghost and the personal phone resolve to the SAME
    # serial, a ghost device power would drive the operator's real phone — the
    # exact thing the structural invariant exists to prevent. Refuse rather than
    # let a config typo bypass it (adversarial-review LOW, 2026-07-07).
    if target == "ghost" and serial:
        phone_serial = resolve_target_serial("phone", environ=environ)
        if phone_serial and phone_serial == serial:
            raise RuntimeError(
                "HOMIE_GHOST_ADB_SERIAL equals HOMIE_PHONE_ADB_SERIAL — the ghost "
                "and the personal phone must be different devices; refusing so a "
                "ghost power can never drive the real phone"
            )
    return serial


def session_for_target(target: str | None) -> str | None:
    """The agent-browser daemon session a target's commands must ride."""

    if target == "phone":
        return PHONE_AGENT_BROWSER_SESSION
    if target == "ghost":
        return GHOST_AGENT_BROWSER_SESSION
    return None


# Internal alias (kept for the module's own call sites).
_session_for_target = session_for_target


@dataclass(frozen=True)
class AgentBrowserResolution:
    command: tuple[str, ...]
    source: str

    @property
    def label(self) -> str:
        return " ".join(self.command)


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    command_label: str

    @property
    def output(self) -> str:
        text = self.stdout.strip()
        err = self.stderr.strip()
        if text and err:
            return f"{text}\n{err}"
        return text or err


def resolve_agent_browser_command(
    *,
    environ: dict[str, str] | None = None,
    platform_name: str | None = None,
) -> AgentBrowserResolution:
    """Resolve the agent-browser command without launching a browser."""

    env = environ or os.environ
    override = env.get("HOMIE_AGENT_BROWSER_BIN") or env.get("AGENT_BROWSER_BIN")
    if override:
        return AgentBrowserResolution((override,), "env")

    system = platform_name or platform.system()
    path_env = env.get("PATH")
    for executable in ("agent-browser", "agent-browser.cmd"):
        found = shutil.which(executable, path=path_env)
        if found:
            # npm's Windows shim forwards ``%*`` through cmd.exe. That shell
            # can reinterpret URL query separators such as ``&`` even though
            # callers supplied a proper argv list. Prefer the package's native
            # executable when available so URLs stay one literal argument.
            found_path = Path(found)
            if system == "Windows" and found_path.suffix.lower() == ".cmd":
                native = (
                    found_path.parent
                    / "node_modules"
                    / "agent-browser"
                    / "bin"
                    / "agent-browser-win32-x64.exe"
                )
                if native.exists():
                    return AgentBrowserResolution((str(native),), "path-native")
            return AgentBrowserResolution((found,), "path")

    if system == "Windows":
        appdata = env.get("APPDATA")
        userprofile = env.get("USERPROFILE")
        candidates = []
        if appdata:
            candidates.append(Path(appdata) / "npm" / "agent-browser.cmd")
        if userprofile:
            candidates.append(
                Path(userprofile) / "AppData" / "Roaming" / "npm" / "agent-browser.cmd"
            )
        for candidate in candidates:
            if candidate.exists():
                return AgentBrowserResolution((str(candidate),), "windows-npm")

    return AgentBrowserResolution(("agent-browser",), "fallback")


def resolve_cdp_port(
    *,
    environ: dict[str, str] | None = None,
    env_names: tuple[str, ...] = ("HOMIE_BROWSER_CDP_PORT", "AGENT_BROWSER_CDP_PORT"),
    default: int = DEFAULT_CDP_PORT,
) -> int:
    env = environ or os.environ
    for name in env_names:
        raw = env.get(name)
        if not raw:
            continue
        try:
            port = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer port") from exc
        if not 0 < port < 65536:
            raise ValueError(f"{name} must be between 1 and 65535")
        return port
    return default


def resolve_target_port(
    target: str | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> int:
    """Resolve the CDP port for a browser target (Rule 1 — call-time env reads).

    ``desktop`` keeps the exact legacy resolution (byte-identical default);
    ``phone`` reuses the same parser against HOMIE_PHONE_CDP_PORT / 18223;
    ``ghost`` against HOMIE_GHOST_CDP_PORT / 18224.
    """

    resolved = target if target is not None else "desktop"
    if resolved == "desktop":
        return resolve_cdp_port(environ=environ)
    if resolved == "phone":
        return resolve_cdp_port(
            environ=environ,
            env_names=PHONE_CDP_ENV_NAMES,
            default=PHONE_CDP_DEFAULT_PORT,
        )
    if resolved == "ghost":
        return resolve_cdp_port(
            environ=environ,
            env_names=GHOST_CDP_ENV_NAMES,
            default=GHOST_CDP_DEFAULT_PORT,
        )
    raise ValueError(f"unknown browser target: {resolved}")


def resolve_target(
    target: str | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Target registry — transport config for ``desktop``, ``phone``, ``ghost``."""

    resolved = target if target is not None else "desktop"
    if resolved not in BROWSER_TARGETS:
        raise ValueError(f"unknown browser target: {resolved}")
    return {
        "target": resolved,
        "transport": "adb" if is_adb_target(resolved) else "local",
        "port": resolve_target_port(resolved, environ=environ),
    }


def resolve_linkedin_profile_url(*, environ: dict[str, str] | None = None) -> str | None:
    env = environ or os.environ
    for name in ("HOMIE_LINKEDIN_PROFILE_URL", "LINKEDIN_PROFILE_URL"):
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def redact_url(raw_url: str) -> str:
    """Hide query strings and fragments before tab URLs hit chat surfaces."""

    try:
        parsed = parse.urlsplit(raw_url)
    except ValueError:
        return raw_url
    if not parsed.scheme or not parsed.netloc:
        return raw_url
    return parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def redact_text_urls(text: str) -> str:
    """Redact URLs embedded in titles or CLI text."""

    return _HTTP_URL_PATTERN.sub(lambda match: redact_url(match.group(0)), text)


def _read_json_url(url: str, *, timeout: float) -> Any:
    with request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8", errors="replace"))


def get_cdp_version(port: int, *, timeout: float = 2.0) -> dict[str, Any]:
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        payload = _read_json_url(url, timeout=timeout)
    except (OSError, error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"reachable": False, "port": port, "error": str(exc)}

    return {
        "reachable": True,
        "port": port,
        "browser": payload.get("Browser", "unknown"),
        "protocol_version": payload.get("Protocol-Version", "unknown"),
        "websocket_debugger_url": bool(payload.get("webSocketDebuggerUrl")),
    }


def list_cdp_tabs(port: int, *, timeout: float = 2.0) -> dict[str, Any]:
    url = f"http://127.0.0.1:{port}/json/list"
    try:
        payload = _read_json_url(url, timeout=timeout)
    except (OSError, error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"reachable": False, "port": port, "tabs": [], "error": str(exc)}

    tabs: list[dict[str, str]] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        tabs.append(
            {
                "id": str(item.get("id", "")),
                "type": str(item.get("type", "")),
                "title": redact_text_urls(str(item.get("title", "")).strip()),
                "url": redact_url(str(item.get("url", ""))),
            }
        )

    return {"reachable": True, "port": port, "tabs": tabs}


def chrome_visibility_guard(
    port: int,
    *,
    platform_name: str | None = None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Best-effort guard that checks for visible, non-headless Chrome."""

    system = platform_name or platform.system()

    if system == "Windows":
        ps = (
            "$ErrorActionPreference = 'SilentlyContinue'; "
            "$port = 'remote-debugging-port="
            + str(port)
            + "'; "
            "Get-CimInstance Win32_Process | "
            "Where-Object { "
            "($_.Name -match '^(chrome|msedge)\\.exe$') -and "
            "$_.CommandLine -and "
            "$_.CommandLine.Contains($port) "
            "} | Select-Object -First 5 -ExpandProperty CommandLine"
        )
        cmd = ["powershell", "-NoProfile", "-Command", ps]
    elif system == "Linux":
        cmd = ["pgrep", "-af", f"remote-debugging-port={port}"]
    else:
        return {
            "status": "unknown",
            "ok": False,
            "detail": f"visibility guard unsupported on {system}",
        }

    try:
        result = runner(cmd, capture_output=True, text=True, timeout=5)
    except Exception as exc:  # pragma: no cover - platform/process dependent
        return {"status": "unknown", "ok": False, "detail": str(exc)}

    output = (result.stdout or "").strip()
    if result.returncode not in (0, None) and not output:
        return {"status": "not_found", "ok": False, "detail": "no matching Chrome process"}
    if not output:
        return {"status": "not_found", "ok": False, "detail": "no matching Chrome process"}

    lowered = output.lower()
    if "--headless" in lowered or "headless=" in lowered:
        return {"status": "headless", "ok": False, "detail": "headless flag detected"}

    return {"status": "visible", "ok": True, "detail": "visible CDP browser process found"}


def build_agent_browser_argv(
    args: list[str],
    *,
    port: int,
    session: str | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[list[str], AgentBrowserResolution]:
    resolution = resolve_agent_browser_command(environ=environ)
    session_args = ["--session", session] if session else []
    return [*resolution.command, "--cdp", str(port), *session_args, *args], resolution


def build_agent_browser_global_argv(
    args: list[str],
    *,
    environ: dict[str, str] | None = None,
) -> tuple[list[str], AgentBrowserResolution]:
    """Build an agent-browser command that is not tied to a CDP session."""

    resolution = resolve_agent_browser_command(environ=environ)
    return [*resolution.command, *args], resolution


# ── Scoped client reaping — the Windows Job Object kill scope ───────────────
# A timed-out agent-browser client must not leave descendants behind. On
# Windows the command frequently resolves through npm's `.cmd` shim, so the
# process we hold is `cmd.exe` and the real work is a Node grandchild:
# `proc.kill()` reaps the shim and the Node child survives, holding the named
# session or an in-flight CDP operation open indefinitely (gate finding, 2026-
# 07-24).
#
# `taskkill /T` walks the LIVE process tree, which is exactly the wrong tool
# near an operator-owned browser: anything that has been re-parented onto the
# client at kill time gets swept up with it. A Job Object cannot do that. Job
# membership is decided at CREATION — only the client we spawned and processes
# it goes on to create are ever members — so terminating the job is
# structurally incapable of touching the operator's pre-existing Chrome, which
# was launched independently and is reached over CDP, never as a child.
#
# The job is NOT created with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: on a
# successful command the background daemon the client may have started has to
# outlive the invocation (that daemon IS the named session). The job is only
# ever terminated on the timeout path.

#
# Membership at creation is only worth something if the client cannot RUN
# before it is a member. Assigning after `Popen` returns leaves a window in
# which a fast shim spawns its node child, and that grandchild is born outside
# the job: terminating the job then reports success while the descendant is
# still alive holding the session (re-gate 2026-07-24 — reproduced with an
# 0.8s assignment delay). The client is therefore created SUSPENDED, assigned,
# and only then resumed, so nothing it spawns can predate its membership.
# Python's `subprocess` exposes no `lpAttributeList`, so the atomic
# PROC_THREAD_ATTRIBUTE_JOB_LIST route is unavailable without reimplementing
# CreateProcess; suspend-assign-resume closes the same window.
#
# SCOPE. All of this — the job, the suspended start, the thread resume — runs
# for `reap_on_timeout=True` callers ONLY. Everything below is machinery in
# service of terminating the scope on a timeout, and a caller that never
# terminates its scope gains nothing from being inside one. The named desktop
# WRITE sessions (Upwork, the social X poster) therefore keep the exact
# pre-existing path: plain Popen, plain `proc.kill()` on timeout, no ctypes
# anywhere near them.

_JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_ERROR_MORE_DATA = 234
_REAP_VERIFY_TIMEOUT_S = 10.0

# Windows creation/thread constants subprocess does not re-export.
_CREATE_SUSPENDED = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_TH32CS_SNAPTHREAD = 0x00000004
_INVALID_HANDLE_VALUE = -1


class _WindowsKillScope:
    """A kill-scoped Windows Job Object for ONE agent-browser client run."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPDWORD,
        ]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k = kernel32
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        self._handle = handle

    def assign(self, pid: int) -> bool:
        """Put a freshly spawned client into the scope. Best effort."""

        process = self._k.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid)
        )
        if not process:
            return False
        try:
            return bool(self._k.AssignProcessToJobObject(self._handle, process))
        finally:
            self._k.CloseHandle(process)

    def terminate(self) -> None:
        self._k.TerminateJobObject(self._handle, 1)

    def live_pids(self) -> list[int]:
        """PIDs still alive inside the scope (empty == fully reaped)."""

        ctypes = self._ctypes
        from ctypes import wintypes

        class _ProcessIdList(ctypes.Structure):
            _fields_ = [
                ("NumberOfAssignedProcesses", wintypes.DWORD),
                ("NumberOfProcessIdsInList", wintypes.DWORD),
                ("ProcessIdList", ctypes.c_size_t * 256),
            ]

        payload = _ProcessIdList()
        returned = wintypes.DWORD(0)
        ok = self._k.QueryInformationJobObject(
            self._handle,
            _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
            ctypes.byref(payload),
            ctypes.sizeof(payload),
            ctypes.byref(returned),
        )
        if not ok and ctypes.get_last_error() != _ERROR_MORE_DATA:
            # Unknowable state: report a non-empty scope so callers never
            # claim a clean reap they cannot prove.
            return [-1]
        count = min(int(payload.NumberOfProcessIdsInList), 256)
        return [int(payload.ProcessIdList[index]) for index in range(count)]

    def close(self) -> None:
        handle, self._handle = getattr(self, "_handle", None), None
        if handle:
            self._k.CloseHandle(handle)


def _open_kill_scope() -> Any:
    """A kill scope for one client run, or ``None`` when unavailable."""

    if platform.system() != "Windows":
        return None
    try:
        return _WindowsKillScope()
    except Exception:  # pragma: no cover - kernel/ctypes dependent
        return None


def _resume_process(pid: int) -> tuple[int, int]:
    """Resume every thread of a CREATE_SUSPENDED process.

    Returns ``(threads_found, threads_resumed)``. The client is spawned
    suspended so it can be put inside the kill scope before it is able to
    spawn anything; `subprocess` does not hand back the primary thread handle,
    so the threads are enumerated by owner pid and resumed.

    The two numbers are different failures. ``found == 0`` means the OS has no
    live process under that pid — there is nothing frozen and nothing to do.
    ``found > resumed`` means a real suspended client could NOT be woken, and
    the caller must kill it: a suspended agent-browser would otherwise sit
    there burning the whole timeout.
    """

    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # pragma: no cover - non-Windows
        return (0, 0)

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
        kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == ctypes.c_void_p(_INVALID_HANDLE_VALUE).value:
            # Cannot enumerate: report a frozen thread we failed to resume so
            # the caller kills rather than waiting on a possibly-suspended
            # client.
            return (1, 0)
        try:
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(_ThreadEntry32)
            found = 0
            resumed = 0
            more = kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == int(pid):
                    found += 1
                    handle = kernel32.OpenThread(
                        _THREAD_SUSPEND_RESUME, False, entry.th32ThreadID
                    )
                    if handle:
                        try:
                            if kernel32.ResumeThread(handle) != 0xFFFFFFFF:
                                resumed += 1
                        finally:
                            kernel32.CloseHandle(handle)
                more = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
            return (found, resumed)
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:  # pragma: no cover - kernel/ctypes dependent
        return (1, 0)


def _reap_kill_scope(scope: Any, pid: int) -> bool:
    """Terminate the scope and VERIFY every member is gone. Returns success."""

    try:
        scope.terminate()
    except Exception:  # pragma: no cover - kernel dependent
        return False
    deadline = time.monotonic() + _REAP_VERIFY_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if not scope.live_pids():
                return True
        except Exception:  # pragma: no cover - kernel dependent
            return False
        time.sleep(0.05)
    print(
        f"[browser_control] client scope for pid {pid} did not fully exit "
        "within the reap deadline",
        flush=True,
    )
    return False


def _captured_agent_browser_run(
    argv: list[str], *, reap_on_timeout: bool, **kwargs: Any
) -> Any:
    """Bound a Windows client without waiting on daemon-owned pipe handles."""

    timeout = kwargs.pop("timeout", None)
    if timeout is None or platform.system() != "Windows":
        return subprocess.run(argv, timeout=timeout, **kwargs)
    kwargs.pop("capture_output", None)
    text_mode = bool(kwargs.pop("text", False) or kwargs.pop("universal_newlines", False))
    encoding = str(kwargs.pop("encoding", None) or "utf-8")
    errors = str(kwargs.pop("errors", None) or "strict")

    def read_capture(handle: Any) -> str | bytes:
        handle.flush()
        handle.seek(0)
        raw = handle.read()
        if text_mode:
            return raw.decode(encoding, errors=errors)
        return raw

    # The kill scope exists ONLY to serve the timeout reap, so it is opened
    # ONLY for callers that opt into reaping. Opening it for a `reap_on_timeout
    # =False` caller — the named desktop WRITE sessions behind Upwork and the
    # social X poster — bought them nothing (their scope is never terminated)
    # while handing them three new ways to fail before the browser is even
    # touched: a job-assignment refusal, a thread-enumeration miss, and the
    # `could not resume the suspended agent-browser client` OSError. Those
    # callers keep the frozen M12 path: plain Popen, plain `proc.kill()`.
    scope = _open_kill_scope() if reap_on_timeout else None
    with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
        mode="w+b"
    ) as stderr_file:
        # Suspended FIRST when there is a scope to join: a client that has not
        # run yet cannot have spawned a descendant outside the job.
        popen_kwargs = dict(kwargs)
        if scope is not None:
            popen_kwargs["creationflags"] = (
                int(popen_kwargs.get("creationflags", 0)) | _CREATE_SUSPENDED
            )
        proc = subprocess.Popen(
            argv,
            stdout=stdout_file,
            stderr=stderr_file,
            **popen_kwargs,
        )
        if scope is not None:
            assigned = False
            try:
                assigned = bool(scope.assign(proc.pid))
            except Exception:  # pragma: no cover - kernel dependent
                assigned = False
            if not assigned:
                with contextlib.suppress(Exception):
                    scope.close()
                scope = None
            # Resume LAST, whether or not the assignment landed — a client left
            # suspended would hang until the timeout and reap nothing useful.
            found, resumed = _resume_process(proc.pid)
            if found and not resumed:
                with contextlib.suppress(Exception):
                    proc.kill()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=10)
                if scope is not None:
                    with contextlib.suppress(Exception):
                        scope.close()
                raise OSError(
                    "could not resume the suspended agent-browser client"
                )
        try:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_timed_out_client(proc, scope, reap_on_timeout=reap_on_timeout)
                stdout = read_capture(stdout_file)
                stderr = read_capture(stderr_file)
                raise subprocess.TimeoutExpired(
                    argv, timeout, output=stdout, stderr=stderr
                )
            stdout = read_capture(stdout_file)
            stderr = read_capture(stderr_file)
            return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
        finally:
            if scope is not None:
                # Closing WITHOUT kill-on-close: a daemon started by a
                # successful client owns the named session and must survive.
                scope.close()


def _kill_timed_out_client(proc: Any, scope: Any, *, reap_on_timeout: bool) -> None:
    """Reap a hung client. Never reaches the operator's own Chrome."""

    if reap_on_timeout:
        reaped = scope is not None and _reap_kill_scope(scope, proc.pid)
        if not reaped:
            # No job scope available (non-Windows kernel surface or an
            # assignment refusal): fall back to the documented tree kill of
            # THIS client pid. Chrome is not a descendant of the client — the
            # client attaches over CDP and never launches a browser.
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
            )
    else:
        # Opt-out callers (named desktop write sessions) keep the frozen M12
        # semantics: kill only the timed-out CLI client so an in-flight write
        # its child is finishing is never severed mid-action.
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _tree_kill_run(argv: list[str], **kwargs: Any) -> Any:
    """Phone/ghost runner that reaps the full client scope on timeout.

    Live E2E finding (2026-07-06, phone navigate): agent-browser resolves to a
    .CMD wrapper on Windows; subprocess.run's timeout kills cmd.exe but the
    node child underneath survives holding the inherited stdout/stderr pipes,
    so the post-kill communicate() blocks until node exits on its own — a 20s
    timeout became a 2-minute API stall while the phone's renderer crawled.
    The native v0.32 binary has the same pipe-inheritance shape when it starts
    its background daemon: the one-shot client exits successfully, but the
    daemon keeps PIPE handles open and ``communicate()`` waits forever for EOF.
    Capture through temporary files and wait on the client PID instead. A real
    timeout terminates the client's kill scope and surfaces on time.
    """

    return _captured_agent_browser_run(argv, reap_on_timeout=True, **kwargs)


def _reaping_session_run(argv: list[str], **kwargs: Any) -> Any:
    """Named desktop-session runner for READ-ONLY callers that opt into reaping.

    Same scope-terminate semantics as the adb runner. Safe here precisely
    because the opting-in caller performs no writes: severing a hung read can
    never leave a half-typed post behind, and the scope cannot reach the
    operator's Chrome.
    """

    return _captured_agent_browser_run(argv, reap_on_timeout=True, **kwargs)


def _desktop_session_run(argv: list[str], **kwargs: Any) -> Any:
    """Named desktop-session runner that never reaps shared Chrome."""

    return _captured_agent_browser_run(argv, reap_on_timeout=False, **kwargs)


def run_agent_browser(
    args: list[str],
    *,
    port: int,
    session: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    environ: dict[str, str] | None = None,
    runner: Any = None,
    reap_on_timeout: bool = False,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Run one agent-browser command against a CDP target.

    ``reap_on_timeout`` is opt-in and belongs to READ-ONLY callers: on timeout
    the client's whole kill scope is terminated so no descendant survives
    holding a named session. Write callers leave it False — their in-flight
    child is finishing a real action and must not be severed mid-write.

    ``env`` is the CHILD process environment. Its reason for existing is
    ``AGENT_BROWSER_DEFAULT_TIMEOUT``: agent-browser exposes no per-command
    timeout flag (``wait --timeout`` is download-only in 0.33.0), so the only
    way to make the CLI's own bound fire BEFORE ``timeout`` — the ordering that
    keeps our layer from severing a live client and orphaning its descendants —
    is to hand the child a smaller default. ``None`` (the default) inherits the
    parent environment exactly as before; nothing else changes shape.
    """

    if runner is None:
        # Tree-kill ONLY on adb-target sessions and explicit opt-ins: slow or
        # freezable renderers are what make a client outlive its timeout. A
        # named desktop WRITE session (for example Upwork or X on shared CDP
        # 18222) keeps plain desktop timeout semantics.
        if session in {PHONE_AGENT_BROWSER_SESSION, GHOST_AGENT_BROWSER_SESSION}:
            runner = _tree_kill_run
        elif reap_on_timeout:
            runner = _reaping_session_run
        elif session is not None:
            runner = _desktop_session_run
        else:
            runner = subprocess.run
    argv, resolution = build_agent_browser_argv(
        args, port=port, session=session, environ=environ
    )
    session_label = f" --session {session}" if session else ""
    # `env` is passed ONLY when a caller supplied one: omitting the kwarg keeps
    # every pre-existing call byte-identical (subprocess treats env=None as
    # "inherit", but the runners are also monkeypatched in tests that assert on
    # the exact kwargs).
    run_kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if env is not None:
        run_kwargs["env"] = env
    result = runner(argv, **run_kwargs)
    return CommandResult(
        ok=result.returncode == 0,
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
        command_label=f"{resolution.label} --cdp {port}{session_label} {' '.join(args)}",
    )


def run_agent_browser_global(
    args: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    environ: dict[str, str] | None = None,
    runner: Any = subprocess.run,
) -> CommandResult:
    """Run an agent-browser command that should not receive ``--cdp``."""

    argv, resolution = build_agent_browser_global_argv(args, environ=environ)
    result = runner(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )
    return CommandResult(
        ok=result.returncode == 0,
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
        command_label=f"{resolution.label} {' '.join(args)}",
    )


def _stream_status_payload(
    *,
    enabled: bool = False,
    connected: bool = False,
    port: int | None = None,
    screencasting: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "connected": connected,
        "port": port,
        "screencasting": screencasting,
        "reason": redact_text_urls(reason),
    }


def _parse_stream_status_result(result: CommandResult) -> dict[str, Any]:
    if not result.ok:
        return _stream_status_payload(reason=result.output or "agent-browser stream command failed")

    try:
        payload = json.loads((result.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return _stream_status_payload(reason=result.output or "agent-browser stream returned invalid JSON")

    if not isinstance(payload, dict):
        return _stream_status_payload(reason="agent-browser stream returned an unexpected payload")

    data = payload.get("data")
    if not payload.get("success", False):
        error_text = payload.get("error") or result.output or "agent-browser stream command failed"
        return _stream_status_payload(reason=str(error_text))
    if not isinstance(data, dict):
        return _stream_status_payload(reason="agent-browser stream returned no data")

    raw_port = data.get("port")
    stream_port = raw_port if isinstance(raw_port, int) and 0 < raw_port < 65536 else None
    return _stream_status_payload(
        enabled=bool(data.get("enabled")),
        connected=bool(data.get("connected")),
        port=stream_port,
        screencasting=bool(data.get("screencasting")),
        reason="ready",
    )


def browser_stream_status(
    *,
    port: int | None = None,
    target: str = "desktop",
    runner: Any = None,
) -> dict[str, Any]:
    """Return the read-only agent-browser stream state for the target CDP browser."""

    try:
        resolved_port = port if port is not None else resolve_target_port(target)
    except ValueError as exc:
        return _stream_status_payload(reason=str(exc))

    try:
        result = run_agent_browser(
            ["--json", "stream", "status"],
            port=resolved_port,
            session=_session_for_target(target),
            timeout=8,
            runner=runner,
        )
    except Exception as exc:  # pragma: no cover - subprocess/runtime dependent
        return _stream_status_payload(reason=str(exc))
    return _parse_stream_status_result(result)


def browser_stream_enable(
    *,
    port: int | None = None,
    stream_port: int | None = None,
    target: str = "desktop",
    runner: Any = None,
) -> dict[str, Any]:
    """Enable the observation stream only; does not grant browser input control."""

    resolved_port = port if port is not None else resolve_target_port(target)
    if is_adb_target(target):
        # Every adb action self-heals the forward first (spec invariant).
        _ensure_phone_transport(
            resolved_port, serial=_resolve_adb_serial_or_raise(target), runner=runner
        )
    session = _session_for_target(target)
    args = ["--json", "stream", "enable"]
    if stream_port is not None:
        if not 0 < stream_port < 65536:
            raise ValueError("stream_port must be between 1 and 65535")
        args.extend(["--port", str(stream_port)])
    result = run_agent_browser(
        args, port=resolved_port, session=session, timeout=12, runner=runner
    )
    if not result.ok:
        if "already enabled" in (result.output or "").lower():
            return browser_stream_status(port=resolved_port, target=target, runner=runner)
        raise RuntimeError(redact_text_urls(result.output or "agent-browser stream enable failed"))
    return browser_stream_status(port=resolved_port, target=target, runner=runner)


def browser_stream_disable(
    *,
    port: int | None = None,
    target: str = "desktop",
    runner: Any = None,
) -> dict[str, Any]:
    """Disable the observation stream only; no browser state is persisted."""

    resolved_port = port if port is not None else resolve_target_port(target)
    if is_adb_target(target):
        _ensure_phone_transport(
            resolved_port, serial=_resolve_adb_serial_or_raise(target), runner=runner
        )
    result = run_agent_browser(
        ["--json", "stream", "disable"],
        port=resolved_port,
        session=_session_for_target(target),
        timeout=12,
        runner=runner,
    )
    if not result.ok:
        output = (result.output or "").lower()
        if any(
            phrase in output
            for phrase in (
                "already disabled",
                "not enabled",
                "not running",
                "no stream",
                "stream disabled",
                "streaming is not enabled",
            )
        ):
            return browser_stream_status(port=resolved_port, target=target, runner=runner)
        raise RuntimeError(redact_text_urls(result.output or "agent-browser stream disable failed"))
    return browser_stream_status(port=resolved_port, target=target, runner=runner)


def capture_browser_screenshot_png(
    *,
    port: int | None = None,
    target: str = "desktop",
    session: str | None = None,
    runner: Any = None,
) -> bytes:
    """Capture a transient PNG from the requested visible browser session."""

    resolved_port = port if port is not None else resolve_target_port(target)
    if is_adb_target(target):
        _ensure_phone_transport(
            resolved_port, serial=_resolve_adb_serial_or_raise(target), runner=runner
        )
    tmp = tempfile.NamedTemporaryFile(prefix="homie-browser-viewer-", suffix=".png", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        result = run_agent_browser(
            ["screenshot", str(tmp_path)],
            port=resolved_port,
            session=session if session is not None else _session_for_target(target),
            # adb grabs ride a mobile renderer: cold-session + capture regularly
            # exceeds the desktop's 20s (measured 2026-07-06, phone).
            timeout=45 if is_adb_target(target) else 20,
            runner=runner,
        )
        if not result.ok:
            raise RuntimeError(redact_text_urls(result.output or "agent-browser screenshot failed"))
        if not tmp_path.exists():
            raise RuntimeError("agent-browser screenshot did not create an output file")
        data = tmp_path.read_bytes()
        if not data:
            raise RuntimeError("agent-browser screenshot output was empty")
        return data
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


# ── M12 — phone-drive interaction primitives ────────────────────────────────
# Operator-initiated remote driving of the visible browser (the human pushes
# each button from the phone). NOT an agent surface: agent-side writes stay
# behind the social-write approval-phrase gates; these helpers are consumed
# only by the gated dashboard endpoints (browser.viewer.act / .elements /
# .navigate workflows), which audit every attempt.

_SNAPSHOT_ELEMENT_RE = re.compile(
    r"^\s*-\s+(?P<role>[a-zA-Z]+)\s+(?:\"(?P<name>[^\"]*)\"\s*)?\[(?P<attrs>[^\]]*)\]"
)
_SNAPSHOT_REF_RE = re.compile(r"\bref=(?P<ref>e\d+)\b")
_ACT_REF_RE = re.compile(r"^e\d{1,5}$")
_ACT_KEY_RE = re.compile(r"^[A-Za-z0-9+]{1,32}$")
_MAX_SNAPSHOT_ELEMENTS = 120

BROWSER_ACT_KINDS = ("click", "fill", "press", "scroll", "back", "forward", "reload")
_SCROLL_DIRECTIONS = frozenset({"up", "down", "left", "right"})


def parse_snapshot_elements(text: str) -> list[dict[str, str]]:
    """`snapshot -i -c` lines -> [{ref, role, name}] for the phone element list.

    Format observed live (agent-browser 0.31.x):
        - button "Google Search" [ref=e20]
        - combobox "Search" [expanded=false, ref=e24]
    Unnamed `generic` rows are clutter (clickable wrappers) and are dropped.
    """

    elements: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = _SNAPSHOT_ELEMENT_RE.match(line)
        if not match:
            continue
        ref_match = _SNAPSHOT_REF_RE.search(match.group("attrs") or "")
        if not ref_match:
            continue
        role = match.group("role")
        name = (match.group("name") or "").strip()
        if role == "generic" and not name:
            continue
        ref = ref_match.group("ref")
        if ref in seen:
            continue
        seen.add(ref)
        elements.append({"ref": ref, "role": role, "name": name[:120]})
        if len(elements) >= _MAX_SNAPSHOT_ELEMENTS:
            break
    return elements


def browser_snapshot_elements(
    *,
    port: int | None = None,
    target: str = "desktop",
    runner: Any = None,
) -> list[dict[str, str]]:
    """Interactive-element snapshot of the target browser's active tab."""

    resolved_port = port if port is not None else resolve_target_port(target)
    if is_adb_target(target):
        _ensure_phone_transport(
            resolved_port, serial=_resolve_adb_serial_or_raise(target), runner=runner
        )
    result = run_agent_browser(
        ["snapshot", "-i", "-c"],
        port=resolved_port,
        session=_session_for_target(target),
        timeout=20,
        runner=runner,
    )
    if not result.ok:
        raise RuntimeError(redact_text_urls(result.output or "agent-browser snapshot failed"))
    return parse_snapshot_elements(result.stdout)


def build_browser_act_args(
    kind: str,
    *,
    ref: str | None = None,
    text: str | None = None,
    key: str | None = None,
    direction: str | None = None,
    amount: int | None = None,
) -> list[str]:
    """Map a validated phone action onto an agent-browser argv. ValueError on
    anything malformed — refs/keys/directions are shape-checked here so the
    endpoint never shells arbitrary operator strings as commands."""

    if kind == "click":
        if not ref or not _ACT_REF_RE.match(ref):
            raise ValueError("click requires a snapshot ref like e12")
        return ["click", f"@{ref}"]
    if kind == "fill":
        if not ref or not _ACT_REF_RE.match(ref):
            raise ValueError("fill requires a snapshot ref like e12")
        if text is None:
            raise ValueError("fill requires text")
        return ["fill", f"@{ref}", text]
    if kind == "press":
        if not key or not _ACT_KEY_RE.match(key):
            raise ValueError("press requires a key like Enter or Control+a")
        return ["press", key]
    if kind == "scroll":
        resolved_direction = direction or "down"
        if resolved_direction not in _SCROLL_DIRECTIONS:
            raise ValueError("scroll direction must be up/down/left/right")
        resolved_amount = amount if amount is not None else 600
        if not 1 <= resolved_amount <= 5000:
            raise ValueError("scroll amount must be 1-5000 px")
        return ["scroll", resolved_direction, str(resolved_amount)]
    if kind in ("back", "forward", "reload"):
        return [kind]
    raise ValueError(f"unknown browser action kind: {kind}")


def ensure_browser_window_restored(
    *,
    port: int | None = None,
    runner: Any = None,
) -> bool:
    """Best-effort un-minimize of the CDP Chrome window (Windows only).

    agent-browser input goes through the compositor hit-test, which silently
    DROPS clicks while the window is minimized — the CLI reports Done and
    nothing happens (proven live 2026-07-05: ref click dead on a minimized
    window, identical click lands after restore; JS eval clicks work either
    way). Reads/snapshots are unaffected. Fail-open: any error returns False
    and the action proceeds against whatever window state exists.
    """

    if platform.system() != "Windows":
        return False
    if runner is None:
        runner = subprocess.run
    resolved_port = port if port is not None else resolve_cdp_port()
    ps = (
        "Add-Type 'using System; using System.Runtime.InteropServices; "
        'public class W { [DllImport("user32.dll")] public static extern bool '
        "ShowWindow(IntPtr h, int n); "
        '[DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h); }\'; '
        "$procs = Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*remote-debugging-port={resolved_port}*' }}; "
        "foreach ($cim in $procs) { "
        "$p = Get-Process -Id $cim.ProcessId -ErrorAction SilentlyContinue; "
        "if ($p -and $p.MainWindowHandle -ne 0 -and [W]::IsIconic($p.MainWindowHandle)) { "
        "[W]::ShowWindow($p.MainWindowHandle, 9) | Out-Null; 'restored' } }"
    )
    try:
        result = runner(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return False
    return "restored" in (getattr(result, "stdout", "") or "")


def _ensure_phone_transport(
    local_port: int, *, serial: str | None = None, runner: Any = None
) -> bool:
    """adb READ-path prehook (phone OR ghost): heal the forward, then wake the
    screen and dismiss a non-secure keyguard — WITHOUT foregrounding Chrome
    (fail-open).

    ``serial`` selects the device: None keeps the phone's single-device
    autodetect (byte-identical M12), the ghost passes its own resolved serial so
    it can never fall back to the phone. (Name kept from P3.0 for call-site and
    test stability; it now serves every adb target.)

    E2E evidence (2026-07-06): a dozing device freezes Chrome's renderer, so a
    read against a sleeping device times out even though `/json/*` answers.
    Waking the screen resumes whatever app was foreground — when that's Chrome
    (the common drive-session case) reads recover; when the operator is in
    another app, reads fail honestly rather than hijacking their foreground.
    """

    if runner is None:
        runner = subprocess.run
    try:
        forward_ok = adb_control.ensure_forward(local_port, serial=serial, runner=runner)
        adb_control.wake_screen(serial=serial, runner=runner)
        adb_control.dismiss_keyguard(serial=serial, runner=runner)
        return forward_ok
    except Exception:
        return False


def ensure_phone_chrome_ready(
    *,
    local_port: int | None = None,
    serial: str | None = None,
    runner: Any = None,
) -> bool:
    """Best-effort adb pre-ACTION hook (phone OR ghost): heal the forward, wake
    the screen, dismiss a non-secure keyguard, and bring Chrome to the
    foreground. Fail-open — any error returns False and the action proceeds.

    ``serial`` selects the device (None = phone single-device autodetect; the
    ghost passes its own resolved serial). Name kept from P3.0.

    Policy decided by the live freezer spike (2026-07-06, S24 over wireless
    adb): a backgrounded/dozing device freezes Chrome's RENDERER within seconds
    (browser-process HTTP keeps answering; page-level CDP ops time out), and
    this exact wake -> dismiss-keyguard -> am start sequence recovered a
    locked, dozing phone into a drivable Chrome. Foregrounding Chrome is
    therefore REQUIRED for acts. Consequence for same-device driving: an act
    pulls Chrome over whatever is foreground — best-effort by design; driving
    from another device is the primary mode (the ghost, by design, never fights
    the operator for a foreground). Read paths must NOT use this hook (they heal
    the forward only and never hijack the device's foreground).
    """

    if runner is None:
        runner = subprocess.run
    try:
        resolved_port = local_port if local_port is not None else resolve_target_port("phone")
        forward_ok = adb_control.ensure_forward(resolved_port, serial=serial, runner=runner)
        adb_control.wake_screen(serial=serial, runner=runner)
        adb_control.dismiss_keyguard(serial=serial, runner=runner)
        adb_control.chrome_to_foreground(serial=serial, runner=runner)
        return forward_ok
    except Exception:
        return False


def browser_act(
    kind: str,
    *,
    ref: str | None = None,
    text: str | None = None,
    key: str | None = None,
    direction: str | None = None,
    amount: int | None = None,
    port: int | None = None,
    target: str = "desktop",
    runner: Any = None,
) -> CommandResult:
    """Run one operator-driven action against the target browser."""

    args = build_browser_act_args(
        kind, ref=ref, text=text, key=key, direction=direction, amount=amount
    )
    resolved_port = port if port is not None else resolve_target_port(target)
    if is_adb_target(target):
        # ADB pre-hooks replace the desktop window restore (meaningless on-device).
        ensure_phone_chrome_ready(
            local_port=resolved_port,
            serial=_resolve_adb_serial_or_raise(target),
            runner=runner,
        )
    else:
        # Input is dropped by minimized windows (see ensure_browser_window_restored)
        # — restore first so phone-drive works while the operator is away.
        ensure_browser_window_restored(port=resolved_port, runner=runner)
    return run_agent_browser(
        args,
        port=resolved_port,
        session=_session_for_target(target),
        timeout=20,
        runner=runner,
    )


def _viewer_readiness(readiness: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": readiness.get("status", "attention"),
        "cdp_port": readiness.get("cdp_port"),
        "cdp_reachable": bool(readiness.get("cdp_reachable")),
        "browser": readiness.get("browser", "unknown"),
        "visible_guard": readiness.get("visible_guard", "unknown"),
        "tab_count": readiness.get("tab_count", 0),
        "reason": redact_text_urls(str(readiness.get("reason") or "")),
    }


def browser_viewer_status(
    *, port: int | None = None, target: str = "desktop"
) -> dict[str, Any]:
    """Return the stable read-only dashboard viewer envelope.

    ``target`` is echoed back so clients can assert the request drove the
    browser they asked for (defends against a proxy dropping the param).
    """

    readiness = browser_readiness(port=port, target=target)
    cdp_port = readiness.get("cdp_port")
    stream = (
        browser_stream_status(port=int(cdp_port), target=target)
        if isinstance(cdp_port, int)
        else _stream_status_payload(reason=str(readiness.get("reason") or "CDP unavailable"))
    )
    return {
        "mode": "read_only",
        "target": target,
        "readiness": _viewer_readiness(readiness),
        "stream": stream,
        "controls": {
            "browser_input": False,
            "navigation": False,
        },
    }


def browser_status(*, port: int | None = None) -> dict[str, Any]:
    resolved_port = port if port is not None else resolve_cdp_port()
    resolution = resolve_agent_browser_command()
    version = get_cdp_version(resolved_port)
    guard = chrome_visibility_guard(resolved_port)
    tabs = list_cdp_tabs(resolved_port) if version.get("reachable") else {
        "reachable": False,
        "tabs": [],
        "error": version.get("error"),
    }
    return {
        "port": resolved_port,
        "agent_browser": {
            "command": resolution.label,
            "source": resolution.source,
        },
        "cdp": version,
        "visibility": guard,
        "tabs": tabs,
    }


def adb_readiness(
    *,
    target: str = "phone",
    local_port: int | None = None,
    serial: str | None = None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """adb-Chrome readiness for an adb target (phone or ghost) — mirrors
    ``browser_readiness`` key-for-key so ``_viewer_readiness``, the audit
    plumbing, and clients work unchanged.

    ``visible_guard`` carries the adb transport-guard status (device / offline /
    unauthorized / no_device / no_forward / unknown); ``target`` + ``transport``
    are additive. ``ready`` = guard ok AND CDP reachable through the forward.
    ``serial`` selects the device (None = phone single-device autodetect).
    """

    device = target  # operator-facing device noun in the reason strings
    try:
        resolved_port = (
            local_port if local_port is not None else resolve_target_port(target)
        )
    except ValueError as exc:
        return {
            "enabled": False,
            "status": "attention",
            "cdp_port": None,
            "cdp_reachable": False,
            "browser": "unknown",
            "visible_guard": "unknown",
            "tab_count": 0,
            "agent_browser_command_source": "unknown",
            "reason": str(exc),
            "target": target,
            "transport": "adb",
        }

    resolution = resolve_agent_browser_command()
    # Guard first: it self-heals the forward the CDP probe depends on.
    guard = adb_control.adb_transport_guard(resolved_port, serial=serial, runner=runner)
    guard_status = str(guard.get("status") or "unknown")
    version = get_cdp_version(resolved_port)
    cdp_reachable = bool(version.get("reachable"))
    tabs = list_cdp_tabs(resolved_port) if cdp_reachable else {"reachable": False, "tabs": []}
    tab_count = len(tabs.get("tabs", [])) if tabs.get("reachable") else 0

    ready = bool(guard.get("ok")) and cdp_reachable
    reason = "ready"
    if not guard.get("ok"):
        detail = str(guard.get("detail") or f"adb guard is {guard_status}")
        if device != "phone":
            # adb_transport_guard's messages are phone-worded; rewrite the device
            # noun so a ghost never reports "phone" (cosmetic — the serial/port
            # actually targeted are already the ghost's, never the phone's).
            detail = re.sub(r"\bphone\b", device, detail)
        reason = redact_text_urls(detail)
    elif not cdp_reachable:
        error_text = str(version.get("error") or "").lower()
        # "closed connection" = the adb forward accepted on the PC but the
        # device-side abstract socket is gone — Chrome killed/fully frozen
        # (seen live 2026-07-06 while the operator held the phone in the app).
        if "refused" in error_text or "closed connection" in error_text:
            reason = f"{device} Chrome devtools socket unavailable — open Chrome on the {device}"
        elif "timed out" in error_text or "timeout" in error_text:
            reason = f"{device} Chrome unresponsive (backgrounded or frozen)"
        else:
            reason = redact_text_urls(str(version.get("error") or "CDP unreachable"))

    return {
        "enabled": ready,
        "status": "ready" if ready else "attention",
        "cdp_port": resolved_port,
        "cdp_reachable": cdp_reachable,
        "browser": version.get("browser", "unknown") if cdp_reachable else "unknown",
        "visible_guard": guard_status,
        "tab_count": tab_count,
        "agent_browser_command_source": resolution.source,
        "reason": reason,
        "target": target,
        "transport": "adb",
    }


def phone_readiness(
    *,
    local_port: int | None = None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Phone-Chrome readiness (P3.0). Byte-identical M12 envelope.

    PhoneOps review F1 (issue #89): the probe is serial-scoped when
    HOMIE_PHONE_ADB_SERIAL is set, and REFUSES to autodetect once a ghost
    serial is configured — an un-scoped guard probe (ensure_forward /
    wake_screen) with only the ghost attached would silently drive the ghost
    under the phone label and report it ready. Pure autodetect survives only
    for ghost-less deployments (byte-identical M12 behavior there).
    """

    serial = resolve_target_serial("phone")
    if not serial and resolve_target_serial("ghost"):
        try:
            resolved_port = (
                local_port if local_port is not None else resolve_target_port("phone")
            )
        except ValueError:
            resolved_port = None
        return {
            "enabled": False,
            "status": "attention",
            "cdp_port": resolved_port,
            "cdp_reachable": False,
            "browser": "unknown",
            "visible_guard": "unknown",
            "tab_count": 0,
            "agent_browser_command_source": resolve_agent_browser_command().source,
            "reason": (
                "HOMIE_PHONE_ADB_SERIAL not set while a ghost serial is configured — "
                "set it so phone probes can never autodetect onto the ghost"
            ),
            "target": "phone",
            "transport": "adb",
        }
    return adb_readiness(target="phone", local_port=local_port, serial=serial, runner=runner)


def ghost_readiness(
    *,
    local_port: int | None = None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Ghost-Chrome readiness (P4.0). Refuses to probe adb without the ghost's
    OWN serial — a None serial would autodetect / fall back to the phone, so it
    reports attention instead of ever touching the operator's personal device.
    """

    serial = resolve_target_serial("ghost")
    if not serial:
        try:
            resolved_port = (
                local_port if local_port is not None else resolve_target_port("ghost")
            )
        except ValueError:
            resolved_port = None
        return {
            "enabled": False,
            "status": "attention",
            "cdp_port": resolved_port,
            "cdp_reachable": False,
            "browser": "unknown",
            "visible_guard": "unknown",
            "tab_count": 0,
            "agent_browser_command_source": resolve_agent_browser_command().source,
            "reason": "HOMIE_GHOST_ADB_SERIAL not set — set it to the ghost's adb serial",
            "target": "ghost",
            "transport": "adb",
        }
    return adb_readiness(target="ghost", local_port=local_port, serial=serial, runner=runner)


def browser_readiness(
    *, port: int | None = None, target: str = "desktop"
) -> dict[str, Any]:
    """Return a stable, URL-free browser readiness envelope for operator surfaces."""

    if target == "phone":
        return phone_readiness(local_port=port)
    if target == "ghost":
        return ghost_readiness(local_port=port)
    try:
        resolved_port = port if port is not None else resolve_cdp_port()
    except ValueError as exc:
        return {
            "enabled": False,
            "status": "attention",
            "cdp_port": None,
            "cdp_reachable": False,
            "browser": "unknown",
            "visible_guard": "unknown",
            "tab_count": 0,
            "agent_browser_command_source": "unknown",
            "reason": str(exc),
        }

    resolution = resolve_agent_browser_command()
    version = get_cdp_version(resolved_port)
    cdp_reachable = bool(version.get("reachable"))
    guard = chrome_visibility_guard(resolved_port)
    guard_status = str(guard.get("status") or "unknown")
    tabs = list_cdp_tabs(resolved_port) if cdp_reachable else {"reachable": False, "tabs": []}
    tab_count = len(tabs.get("tabs", [])) if tabs.get("reachable") else 0

    ready = cdp_reachable and guard_status == "visible"
    reason = "ready"
    if not cdp_reachable:
        reason = redact_text_urls(str(version.get("error") or "CDP unreachable"))
    elif guard_status != "visible":
        reason = redact_text_urls(str(guard.get("detail") or f"visible guard is {guard_status}"))

    return {
        "enabled": ready,
        "status": "ready" if ready else "attention",
        "cdp_port": resolved_port,
        "cdp_reachable": cdp_reachable,
        "browser": version.get("browser", "unknown") if cdp_reachable else "unknown",
        "visible_guard": guard_status,
        "tab_count": tab_count,
        "agent_browser_command_source": resolution.source,
        "reason": reason,
    }


def format_browser_readiness(readiness: dict[str, Any], *, label: str = "Browser") -> str:
    status = str(readiness.get("status", "unknown"))
    cdp_state = "reachable" if readiness.get("cdp_reachable") else "unreachable"
    lines = [f"{label}: {status}"]
    lines.append(
        "  CDP: "
        f"{cdp_state} on {readiness.get('cdp_port') or 'unknown'} "
        f"({readiness.get('browser') or 'unknown'})"
    )
    lines.append(f"  Chrome guard: {readiness.get('visible_guard') or 'unknown'}")
    lines.append(f"  Tabs: {readiness.get('tab_count', 0)}")
    lines.append(
        "  agent-browser source: "
        f"{readiness.get('agent_browser_command_source') or 'unknown'}"
    )
    reason = str(readiness.get("reason") or "").strip()
    if status != "ready" and reason:
        lines.append(f"  Attention: {redact_text_urls(reason)}")
    return "\n".join(lines)


def format_browser_status(status: dict[str, Any], *, label: str = "Browser") -> str:
    cdp = status["cdp"]
    visibility = status["visibility"]
    tabs = status["tabs"]
    lines = [f"*{label} Status*"]
    lines.append(f"  CDP port: {status['port']}")
    lines.append(
        "  agent-browser: "
        f"{status['agent_browser']['command']} ({status['agent_browser']['source']})"
    )
    if cdp.get("reachable"):
        lines.append(f"  CDP: reachable ({cdp.get('browser', 'unknown')})")
    else:
        lines.append(f"  CDP: unreachable ({cdp.get('error', 'unknown error')})")
    lines.append(f"  Chrome guard: {visibility.get('status')} - {visibility.get('detail')}")
    if tabs.get("reachable"):
        lines.append(f"  Tabs: {len(tabs.get('tabs', []))}")
    else:
        lines.append(f"  Tabs: unavailable ({tabs.get('error', 'CDP unreachable')})")
    if not cdp.get("reachable"):
        lines.append("")
        lines.append(
            "Start real visible Chrome with --remote-debugging-port="
            f"{status['port']} and retry. Do not use a headless/test browser."
        )
    return "\n".join(lines)


def format_tabs(tabs_result: dict[str, Any]) -> str:
    if not tabs_result.get("reachable"):
        return f"*Browser Tabs*\n  unavailable: {tabs_result.get('error', 'CDP unreachable')}"

    tabs = tabs_result.get("tabs", [])
    if not tabs:
        return "*Browser Tabs*\n  no tabs reported by CDP"

    lines = ["*Browser Tabs*"]
    for index, tab in enumerate(tabs, start=1):
        title = tab.get("title") or "(untitled)"
        url = tab.get("url") or "(no url)"
        lines.append(f"  {index}. {title} - {url}")
    return "\n".join(lines)


def validate_web_url(url: str) -> str:
    parsed = parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL must be an absolute http(s) URL")
    return url
