"""ADB device + CDP-forward lifecycle for PhoneOps (phone Chrome transport).

Owns ALL adb interaction for the framework (P3.0). Pure device + forward
lifecycle: imports NOTHING from browser_control — the composition
(phone_readiness / resolve_target / ensure_phone_chrome_ready) lives in
browser_control.py and calls into this module.

Every function takes an injectable ``runner=subprocess.run`` seam for tests.
Rule 1: serial / binary values resolve inside the body from a ``None``
sentinel, never a def-time default.

The forward is LAZY and SELF-HEALING (Rule 2): its real existence is
``adb forward --list`` right now, not a cached claim — forwards live in the
PC adb server and evaporate on adb-server death, phone sleep, or wifi drop,
so ``ensure_forward`` re-checks before every phone action.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

# Stock com.android.chrome DevTools socket. Keep it HERE as the single named
# constant — never hardcode a non-stock socket name at call sites.
CHROME_DEVTOOLS_SOCKET = "localabstract:chrome_devtools_remote"
DEFAULT_ADB_FALLBACK = r"C:\Android\Sdk\platform-tools\adb.exe"
DEFAULT_ADB_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class AdbResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        text = self.stdout.strip()
        err = self.stderr.strip()
        if text and err:
            return f"{text}\n{err}"
        return text or err


def resolve_adb(*, environ: dict[str, str] | None = None) -> str:
    """HOMIE_ADB_BIN env -> PATH -> platform-tools fallback. FileNotFoundError if none."""

    env = environ if environ is not None else os.environ
    override = (env.get("HOMIE_ADB_BIN") or "").strip()
    if override:
        return override
    found = shutil.which("adb", path=env.get("PATH"))
    if found:
        return found
    if os.path.exists(DEFAULT_ADB_FALLBACK):
        return DEFAULT_ADB_FALLBACK
    raise FileNotFoundError("adb executable not found")


def resolve_phone_serial(
    serial: str | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> str | None:
    """Explicit serial wins; else HOMIE_PHONE_ADB_SERIAL; else None (autodetect)."""

    if serial is not None:
        return serial
    env = environ if environ is not None else os.environ
    value = (env.get("HOMIE_PHONE_ADB_SERIAL") or "").strip()
    return value or None


def _run(
    argv: list[str],
    *,
    runner: Any = subprocess.run,
    timeout: int = DEFAULT_ADB_TIMEOUT_SECONDS,
) -> AdbResult:
    result = runner(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )
    return AdbResult(
        ok=result.returncode == 0,
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


def run_adb_global(
    args: list[str],
    *,
    runner: Any = subprocess.run,
    environ: dict[str, str] | None = None,
    timeout: int = DEFAULT_ADB_TIMEOUT_SECONDS,
) -> AdbResult:
    """adb command that must NOT be device-scoped (devices, connect, pair, forward --list)."""

    adb = resolve_adb(environ=environ)
    return _run([adb, *args], runner=runner, timeout=timeout)


def run_adb(
    args: list[str],
    *,
    serial: str | None = None,
    runner: Any = subprocess.run,
    environ: dict[str, str] | None = None,
    timeout: int = DEFAULT_ADB_TIMEOUT_SECONDS,
) -> AdbResult:
    """Device-scoped adb command — always passes ``-s <serial>`` when a serial is
    known so "more than one device" is structurally impossible on scoped calls."""

    adb = resolve_adb(environ=environ)
    resolved_serial = resolve_phone_serial(serial, environ=environ)
    argv = [adb]
    if resolved_serial:
        argv.extend(["-s", resolved_serial])
    argv.extend(args)
    return _run(argv, runner=runner, timeout=timeout)


def adb_exec_out(
    args: list[str],
    *,
    serial: str | None = None,
    runner: Any = subprocess.run,
    environ: dict[str, str] | None = None,
    timeout: int = DEFAULT_ADB_TIMEOUT_SECONDS,
) -> bytes:
    """Device-scoped adb command returning RAW stdout BYTES (Rule: binary-safe).

    ``run_adb`` decodes stdout as UTF-8, which corrupts binary payloads like
    ``exec-out screencap -p`` (PNG bytes). This variant runs with ``text=False``
    and returns stdout untouched. Always ``-s <serial>``-scoped when a serial is
    known. Raises ``RuntimeError`` on a non-zero exit (stderr decoded lossily
    for the message only).
    """

    adb = resolve_adb(environ=environ)
    resolved_serial = resolve_phone_serial(serial, environ=environ)
    argv = [adb]
    if resolved_serial:
        argv.extend(["-s", resolved_serial])
    argv.extend(args)
    result = runner(argv, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(detail or f"adb exec-out failed (exit {result.returncode})")
    return result.stdout or b""


def parse_devices_output(text: str) -> list[dict[str, str]]:
    """``adb devices -l`` lines -> [{serial, state, detail}]."""

    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("*") or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        rows.append({"serial": parts[0], "state": parts[1], "detail": " ".join(parts[2:])})
    return rows


def adb_device_state(
    serial: str | None = None,
    *,
    runner: Any = subprocess.run,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """{state, serial, detail} — states: device/offline/unauthorized/none/multiple/unknown."""

    try:
        result = run_adb_global(["devices", "-l"], runner=runner, environ=environ)
    except FileNotFoundError:
        return {"state": "unknown", "serial": serial, "detail": "adb executable not found"}
    except Exception as exc:  # pragma: no cover - subprocess/runtime dependent
        return {"state": "unknown", "serial": serial, "detail": str(exc)}
    if not result.ok:
        return {"state": "unknown", "serial": serial, "detail": result.output or "adb devices failed"}

    rows = parse_devices_output(result.stdout)
    resolved = resolve_phone_serial(serial, environ=environ)
    if resolved:
        for row in rows:
            if row["serial"] == resolved:
                return {"state": row["state"], "serial": resolved, "detail": row["detail"]}
        return {"state": "none", "serial": resolved, "detail": f"{resolved} not present in adb devices"}
    if not rows:
        return {"state": "none", "serial": None, "detail": "no adb devices attached"}
    if len(rows) > 1:
        return {
            "state": "multiple",
            "serial": None,
            "detail": f"{len(rows)} adb devices attached — set HOMIE_PHONE_ADB_SERIAL",
        }
    return {"state": rows[0]["state"], "serial": rows[0]["serial"], "detail": rows[0]["detail"]}


def _forward_tuple_present(list_output: str, local_port: int, serial: str | None) -> bool:
    for line in list_output.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        if parts[1] != f"tcp:{local_port}" or parts[2] != CHROME_DEVTOOLS_SOCKET:
            continue
        if serial is None or parts[0] == serial:
            return True
    return False


def ensure_forward_outcome(
    local_port: int,
    serial: str | None = None,
    *,
    runner: Any = subprocess.run,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """{ok, status, detail} — status: present/added/bind_failed/add_failed/no_adb."""

    resolved_serial = resolve_phone_serial(serial, environ=environ)
    try:
        listing = run_adb_global(["forward", "--list"], runner=runner, environ=environ)
        if listing.ok and _forward_tuple_present(listing.stdout, local_port, resolved_serial):
            return {"ok": True, "status": "present", "detail": f"forward tcp:{local_port} present"}
        add = run_adb(
            ["forward", f"tcp:{local_port}", CHROME_DEVTOOLS_SOCKET],
            serial=resolved_serial,
            runner=runner,
            environ=environ,
        )
    except FileNotFoundError:
        return {"ok": False, "status": "no_adb", "detail": "adb executable not found"}
    except Exception as exc:  # pragma: no cover - subprocess/runtime dependent
        return {"ok": False, "status": "add_failed", "detail": str(exc)}
    if add.ok:
        return {"ok": True, "status": "added", "detail": f"forward tcp:{local_port} added"}
    if "cannot bind" in (add.output or "").lower():
        return {"ok": False, "status": "bind_failed", "detail": f"local port {local_port} unavailable"}
    return {
        "ok": False,
        "status": "add_failed",
        "detail": f"could not establish adb forward tcp:{local_port}",
    }


def ensure_forward(
    local_port: int,
    serial: str | None = None,
    *,
    runner: Any = subprocess.run,
    environ: dict[str, str] | None = None,
) -> bool:
    """Lazy, self-healing CDP forward — re-add only when the exact tuple is missing."""

    return bool(
        ensure_forward_outcome(local_port, serial, runner=runner, environ=environ)["ok"]
    )


def wake_screen(
    serial: str | None = None,
    *,
    runner: Any = subprocess.run,
    environ: dict[str, str] | None = None,
) -> bool:
    try:
        result = run_adb(
            ["shell", "input", "keyevent", "KEYCODE_WAKEUP"],
            serial=serial,
            runner=runner,
            environ=environ,
        )
    except Exception:
        return False
    return result.ok


def dismiss_keyguard(
    serial: str | None = None,
    *,
    runner: Any = subprocess.run,
    environ: dict[str, str] | None = None,
) -> bool:
    """Best-effort keyguard dismiss (non-secure lock). Proven live 2026-07-06:
    a dozing, locked S24 recovered to a drivable Chrome via wake -> dismiss ->
    am start. A secure keyguard ignores this — fail-open."""

    try:
        result = run_adb(
            ["shell", "wm", "dismiss-keyguard"],
            serial=serial,
            runner=runner,
            environ=environ,
        )
    except Exception:
        return False
    return result.ok


CHROME_MAIN_ACTIVITY = "com.android.chrome/com.google.android.apps.chrome.Main"


def chrome_to_foreground(
    serial: str | None = None,
    *,
    runner: Any = subprocess.run,
    environ: dict[str, str] | None = None,
) -> bool:
    """Bring stock Chrome to the phone's foreground (act-path prehook)."""

    try:
        result = run_adb(
            ["shell", "am", "start", "-n", CHROME_MAIN_ACTIVITY],
            serial=serial,
            runner=runner,
            environ=environ,
        )
    except Exception:
        return False
    return result.ok


def adb_transport_guard(
    local_port: int,
    serial: str | None = None,
    *,
    runner: Any = subprocess.run,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Phone analog of ``chrome_visibility_guard`` — same {status, ok, detail} shape.

    ``ok`` requires adb state ``device`` AND the CDP forward present (the forward
    is self-healed here, so a passing guard means the bridge is live right now).
    Statuses: device / offline / unauthorized / no_device / no_forward / unknown.
    """

    try:
        resolve_adb(environ=environ)
    except FileNotFoundError:
        return {"status": "unknown", "ok": False, "detail": "adb executable not found"}

    state = adb_device_state(serial, runner=runner, environ=environ)
    resolved_serial = state.get("serial") or resolve_phone_serial(serial, environ=environ)

    # Wireless serials (ip:port) can be re-attached without operator action —
    # one reconnect retry before reporting the device gone or stuck offline.
    if state["state"] in ("none", "offline") and resolved_serial and ":" in resolved_serial:
        try:
            connect = adb_connect(resolved_serial, runner=runner, environ=environ)
        except Exception:
            # `adb connect` to an off/asleep phone blocks on TCP connect past
            # the subprocess timeout — the feature's DEFAULT failure state must
            # map to the readiness table, never escape as a 500.
            return {
                "status": "no_device",
                "ok": False,
                "detail": f"phone unreachable over adb — connect to {resolved_serial} failed",
            }
        state = adb_device_state(resolved_serial, runner=runner, environ=environ)
        if state["state"] == "none":
            if "refused" in (connect.output or "").lower():
                # Android's fixed :5555 listener only exists after `adb tcpip 5555`
                # and does not survive a reboot or a wireless-debugging toggle.
                return {
                    "status": "no_device",
                    "ok": False,
                    "detail": "wireless adb reset by reboot — re-run 'adb tcpip 5555' over USB or re-pair",
                }
            return {
                "status": "no_device",
                "ok": False,
                "detail": f"phone unreachable over adb — connect to {resolved_serial} failed",
            }

    if state["state"] == "offline":
        return {
            "status": "offline",
            "ok": False,
            "detail": "adb device stuck offline — toggle wireless debugging on the phone",
        }
    if state["state"] == "unauthorized":
        return {
            "status": "unauthorized",
            "ok": False,
            "detail": "adb not authorized — accept the debugging prompt on the phone",
        }
    if state["state"] == "none":
        return {
            "status": "no_device",
            "ok": False,
            "detail": "no adb device attached — pair the phone and set HOMIE_PHONE_ADB_SERIAL",
        }
    if state["state"] != "device":
        return {"status": "unknown", "ok": False, "detail": state.get("detail") or state["state"]}

    forward = ensure_forward_outcome(local_port, resolved_serial, runner=runner, environ=environ)
    if not forward["ok"]:
        return {"status": "no_forward", "ok": False, "detail": forward["detail"]}
    return {"status": "device", "ok": True, "detail": f"adb device ready; {forward['detail']}"}


# ── One-time pairing helpers (operator-driven setup; keep thin) ──────────────


def adb_pair(
    host_port: str,
    pairing_code: str,
    *,
    runner: Any = subprocess.run,
    environ: dict[str, str] | None = None,
) -> AdbResult:
    return run_adb_global(["pair", host_port, pairing_code], runner=runner, environ=environ)


def adb_tcpip(
    port: int = 5555,
    serial: str | None = None,
    *,
    runner: Any = subprocess.run,
    environ: dict[str, str] | None = None,
) -> AdbResult:
    return run_adb(["tcpip", str(port)], serial=serial, runner=runner, environ=environ)


def adb_connect(
    host_port: str,
    *,
    runner: Any = subprocess.run,
    environ: dict[str, str] | None = None,
) -> AdbResult:
    return run_adb_global(["connect", host_port], runner=runner, environ=environ)
