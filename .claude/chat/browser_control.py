"""Shared helpers for the framework-owned agent-browser surface."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, parse, request

DEFAULT_CDP_PORT = 9222
DEFAULT_TIMEOUT_SECONDS = 20
_HTTP_URL_PATTERN = re.compile(r"https?://[^\s]+")


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

    path_env = env.get("PATH")
    for executable in ("agent-browser", "agent-browser.cmd"):
        found = shutil.which(executable, path=path_env)
        if found:
            return AgentBrowserResolution((found,), "path")

    system = platform_name or platform.system()
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
    environ: dict[str, str] | None = None,
) -> tuple[list[str], AgentBrowserResolution]:
    resolution = resolve_agent_browser_command(environ=environ)
    return [*resolution.command, "--cdp", str(port), *args], resolution


def build_agent_browser_global_argv(
    args: list[str],
    *,
    environ: dict[str, str] | None = None,
) -> tuple[list[str], AgentBrowserResolution]:
    """Build an agent-browser command that is not tied to a CDP session."""

    resolution = resolve_agent_browser_command(environ=environ)
    return [*resolution.command, *args], resolution


def run_agent_browser(
    args: list[str],
    *,
    port: int,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    environ: dict[str, str] | None = None,
    runner: Any = subprocess.run,
) -> CommandResult:
    argv, resolution = build_agent_browser_argv(args, port=port, environ=environ)
    result = runner(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )
    return CommandResult(
        ok=result.returncode == 0,
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
        command_label=f"{resolution.label} --cdp {port} {' '.join(args)}",
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
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Return the read-only agent-browser stream state for the visible CDP browser."""

    try:
        resolved_port = port if port is not None else resolve_cdp_port()
    except ValueError as exc:
        return _stream_status_payload(reason=str(exc))

    try:
        result = run_agent_browser(
            ["--json", "stream", "status"],
            port=resolved_port,
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
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Enable the observation stream only; does not grant browser input control."""

    resolved_port = port if port is not None else resolve_cdp_port()
    args = ["--json", "stream", "enable"]
    if stream_port is not None:
        if not 0 < stream_port < 65536:
            raise ValueError("stream_port must be between 1 and 65535")
        args.extend(["--port", str(stream_port)])
    result = run_agent_browser(args, port=resolved_port, timeout=12, runner=runner)
    if not result.ok:
        if "already enabled" in (result.output or "").lower():
            return browser_stream_status(port=resolved_port, runner=runner)
        raise RuntimeError(redact_text_urls(result.output or "agent-browser stream enable failed"))
    return browser_stream_status(port=resolved_port, runner=runner)


def browser_stream_disable(
    *,
    port: int | None = None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Disable the observation stream only; no browser state is persisted."""

    resolved_port = port if port is not None else resolve_cdp_port()
    result = run_agent_browser(
        ["--json", "stream", "disable"],
        port=resolved_port,
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
            return browser_stream_status(port=resolved_port, runner=runner)
        raise RuntimeError(redact_text_urls(result.output or "agent-browser stream disable failed"))
    return browser_stream_status(port=resolved_port, runner=runner)


def capture_browser_screenshot_png(
    *,
    port: int | None = None,
    runner: Any = subprocess.run,
) -> bytes:
    """Capture a transient PNG screenshot and remove the temporary file."""

    resolved_port = port if port is not None else resolve_cdp_port()
    tmp = tempfile.NamedTemporaryFile(prefix="homie-browser-viewer-", suffix=".png", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        result = run_agent_browser(
            ["screenshot", str(tmp_path)],
            port=resolved_port,
            timeout=20,
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
    runner: Any = subprocess.run,
) -> list[dict[str, str]]:
    """Interactive-element snapshot of the visible browser's active tab."""

    resolved_port = port if port is not None else resolve_cdp_port()
    result = run_agent_browser(
        ["snapshot", "-i", "-c"], port=resolved_port, timeout=20, runner=runner
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
    runner: Any = subprocess.run,
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


def browser_act(
    kind: str,
    *,
    ref: str | None = None,
    text: str | None = None,
    key: str | None = None,
    direction: str | None = None,
    amount: int | None = None,
    port: int | None = None,
    runner: Any = subprocess.run,
) -> CommandResult:
    """Run one operator-driven action against the visible browser."""

    args = build_browser_act_args(
        kind, ref=ref, text=text, key=key, direction=direction, amount=amount
    )
    resolved_port = port if port is not None else resolve_cdp_port()
    # Input is dropped by minimized windows (see ensure_browser_window_restored)
    # — restore first so phone-drive works while the operator is away.
    ensure_browser_window_restored(port=resolved_port, runner=runner)
    return run_agent_browser(args, port=resolved_port, timeout=20, runner=runner)


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


def browser_viewer_status(*, port: int | None = None) -> dict[str, Any]:
    """Return the stable read-only dashboard viewer envelope."""

    readiness = browser_readiness(port=port)
    cdp_port = readiness.get("cdp_port")
    stream = (
        browser_stream_status(port=int(cdp_port))
        if isinstance(cdp_port, int)
        else _stream_status_payload(reason=str(readiness.get("reason") or "CDP unavailable"))
    )
    return {
        "mode": "read_only",
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


def browser_readiness(*, port: int | None = None) -> dict[str, Any]:
    """Return a stable, URL-free browser readiness envelope for operator surfaces."""

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
