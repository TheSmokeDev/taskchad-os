"""Ghost Phone (P4.0) lifecycle — boot / status / shutdown for the Homie's own
background Android.

Owns ONLY the ghost DEVICE lifecycle (emulator process + adb connect + CDP
forward). The browser transport (CDP driving) stays in browser_control /
adb_control — to them the ghost is just "another adb device" behind its own
serial. Two backends, resolved from env:

  - AVD  (HOMIE_GHOST_AVD set): a headless Android emulator this module boots.
  - spare device (no AVD): a dedicated physical Android this module only
    connects + forwards, NEVER boots (there is nothing to boot).

Design (matches ensure_forward): lazy, self-healing, fail-open. Rule 1 — every
env value resolves at call time from a None sentinel. Rule 2 — "is the ghost
up?" is answered by physical state (`adb devices` / `getprop`), never a cached
"I started it" claim. ``ghost_status`` NEVER boots — auto-booting on a status
poll would pin ~3.2-3.5GB on a box with a known kernel-pool leak (landmine 2).
``ensure_ghost_running`` is the ONLY boot path; the operator drives it via the
CLI (``python ghost_control.py up``) and can tear it down with ``down``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from typing import Any

import adb_control
import browser_control

# Windows: detach the emulator so it outlives this process.
_DETACHED_PROCESS = 0x00000008

DEFAULT_EMULATOR_FALLBACK = r"C:\Android\Sdk\emulator\emulator.exe"
DEFAULT_BOOT_TIMEOUT_SECONDS = 180
_BOOT_POLL_INTERVAL_SECONDS = 3
# Proven headless-boot flags (spike 2026-07-06): software GPU dodges the
# documented headless GPU/driver quirks on this box.
_EMULATOR_HEADLESS_FLAGS = ("-no-window", "-no-audio", "-no-boot-anim", "-gpu", "swiftshader_indirect")


def resolve_emulator_bin(*, environ: dict[str, str] | None = None) -> str:
    """HOMIE_EMULATOR_BIN env -> PATH -> SDK fallback. FileNotFoundError if none."""

    env = environ if environ is not None else os.environ
    override = (env.get("HOMIE_EMULATOR_BIN") or "").strip()
    if override:
        return override
    found = shutil.which("emulator", path=env.get("PATH"))
    if found:
        return found
    if os.path.exists(DEFAULT_EMULATOR_FALLBACK):
        return DEFAULT_EMULATOR_FALLBACK
    raise FileNotFoundError("emulator executable not found")


def resolve_ghost_avd(*, environ: dict[str, str] | None = None) -> str | None:
    """The AVD name to boot, or None for a spare-physical-device backend.

    HOMIE_GHOST_AVD set -> AVD backend (this module may boot it); unset -> a
    dedicated physical device (connect + forward only, never boot).
    """

    env = environ if environ is not None else os.environ
    return (env.get("HOMIE_GHOST_AVD") or "").strip() or None


def _ghost_serial(environ: dict[str, str] | None = None) -> str | None:
    return browser_control.resolve_target_serial("ghost", environ=environ)


def _ghost_port(environ: dict[str, str] | None = None) -> int:
    return browser_control.resolve_target_port("ghost", environ=environ)


def ghost_status(
    *, runner: Any = subprocess.run, environ: dict[str, str] | None = None
) -> dict[str, Any]:
    """Physical ghost state (Rule 2): is its device present + booted RIGHT NOW?

    Reads ``adb devices`` + ``getprop sys.boot_completed``; NEVER boots.
    Returns {running, booted, serial, avd, detail}.
    """

    serial = _ghost_serial(environ)
    avd = resolve_ghost_avd(environ=environ)
    if not serial:
        return {
            "running": False,
            "booted": False,
            "serial": None,
            "avd": avd,
            "detail": "HOMIE_GHOST_ADB_SERIAL not set",
        }
    state = adb_control.adb_device_state(serial, runner=runner, environ=environ)
    present = state.get("state") == "device"
    booted = False
    if present:
        try:
            res = adb_control.run_adb(
                ["shell", "getprop", "sys.boot_completed"],
                serial=serial,
                runner=runner,
                environ=environ,
            )
            booted = res.ok and res.stdout.strip() == "1"
        except Exception:
            booted = False
    return {
        "running": present,
        "booted": booted,
        "serial": serial,
        "avd": avd,
        "detail": state.get("detail") or state.get("state") or "",
    }


def _spawn_emulator(emulator: str, avd: str) -> None:
    """Spawn a headless AVD DETACHED so it outlives this process."""

    argv = [emulator, "-avd", avd, *_EMULATOR_HEADLESS_FLAGS]
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = _DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)  # noqa: S603 — argv is module-controlled


def ensure_ghost_running(
    *,
    runner: Any = subprocess.run,
    spawner: Any = None,
    environ: dict[str, str] | None = None,
    boot_timeout_seconds: int | None = None,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Lazy, self-healing boot. No-op + forward if already up; else boot the AVD
    (or connect a spare), wait for boot, forward the CDP port. Fail-open — every
    failure maps to a status dict, never an exception.

    Returns {ok, status, serial?, detail}. status is one of: no_serial,
    already_running, connected, booted, no_device, no_emulator, connect_failed,
    boot_failed, boot_timeout.
    """

    env = environ if environ is not None else os.environ
    serial = _ghost_serial(env)
    if not serial:
        return {"ok": False, "status": "no_serial", "detail": "HOMIE_GHOST_ADB_SERIAL not set"}
    port = _ghost_port(env)

    status = ghost_status(runner=runner, environ=env)
    if status["running"] and status["booted"]:
        forwarded = adb_control.ensure_forward(port, serial=serial, runner=runner, environ=env)
        return {
            "ok": bool(forwarded),
            "status": "already_running",
            "serial": serial,
            "detail": "ghost already running; forward " + ("ok" if forwarded else "failed"),
        }

    avd = resolve_ghost_avd(environ=env)

    # Spare physical device (no AVD): connect + forward, never boot.
    if not avd:
        if ":" in serial:
            try:
                adb_control.adb_connect(serial, runner=runner, environ=env)
            except Exception as exc:
                return {"ok": False, "status": "connect_failed", "detail": str(exc)}
            status = ghost_status(runner=runner, environ=env)
            if status["running"]:
                forwarded = adb_control.ensure_forward(
                    port, serial=serial, runner=runner, environ=env
                )
                return {
                    "ok": bool(forwarded),
                    "status": "connected",
                    "serial": serial,
                    "detail": "spare ghost connected; forward " + ("ok" if forwarded else "failed"),
                }
        return {
            "ok": False,
            "status": "no_device",
            "detail": (
                "ghost device not reachable — plug in the spare (and set "
                "HOMIE_GHOST_ADB_SERIAL) or set HOMIE_GHOST_AVD to boot an emulator"
            ),
        }

    # AVD backend: boot headless, then poll physical boot state.
    try:
        emulator = resolve_emulator_bin(environ=env)
    except FileNotFoundError as exc:
        return {"ok": False, "status": "no_emulator", "detail": str(exc)}
    spawn = spawner if spawner is not None else _spawn_emulator
    try:
        spawn(emulator, avd)
    except Exception as exc:
        return {"ok": False, "status": "boot_failed", "detail": f"emulator spawn failed: {exc}"}

    timeout = boot_timeout_seconds if boot_timeout_seconds is not None else DEFAULT_BOOT_TIMEOUT_SECONDS
    polls = max(1, timeout // _BOOT_POLL_INTERVAL_SECONDS)
    for _ in range(polls):
        st = ghost_status(runner=runner, environ=env)
        if st["running"] and st["booted"]:
            forwarded = adb_control.ensure_forward(port, serial=serial, runner=runner, environ=env)
            return {
                "ok": bool(forwarded),
                "status": "booted",
                "serial": serial,
                "detail": "ghost AVD booted; forward " + ("ok" if forwarded else "failed"),
            }
        sleep(_BOOT_POLL_INTERVAL_SECONDS)
    return {
        "ok": False,
        "status": "boot_timeout",
        "detail": f"ghost AVD did not boot within {timeout}s",
    }


def ghost_shutdown(
    *, runner: Any = subprocess.run, environ: dict[str, str] | None = None
) -> dict[str, Any]:
    """Tear the ghost down + reclaim RAM. AVD -> ``adb emu kill``; a spare device
    is left running (not this module's to power off). Fail-open."""

    env = environ if environ is not None else os.environ
    serial = _ghost_serial(env)
    if not serial:
        return {"ok": False, "status": "no_serial", "detail": "HOMIE_GHOST_ADB_SERIAL not set"}
    if not resolve_ghost_avd(environ=env):
        return {
            "ok": True,
            "status": "spare_left_running",
            "detail": "spare ghost device left running (not this module's to power off)",
        }
    try:
        res = adb_control.run_adb(["emu", "kill"], serial=serial, runner=runner, environ=env)
    except Exception as exc:
        return {"ok": False, "status": "kill_failed", "detail": str(exc)}
    return {
        "ok": bool(res.ok),
        "status": "killed" if res.ok else "kill_failed",
        "detail": res.output or "emu kill sent",
    }


def _main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = (args[0] if args else "status").lower()
    if cmd == "status":
        st = ghost_status()
        print(
            f"ghost: running={st['running']} booted={st['booted']} "
            f"serial={st['serial']} avd={st['avd']} — {st['detail']}"
        )
        return 0
    if cmd in ("up", "start", "ensure", "boot"):
        result = ensure_ghost_running()
        print(f"ghost up: {result['status']} — {result['detail']}")
        return 0 if result["ok"] else 1
    if cmd in ("down", "stop", "shutdown", "kill"):
        result = ghost_shutdown()
        print(f"ghost down: {result['status']} — {result['detail']}")
        return 0 if result["ok"] else 1
    print("usage: ghost_control.py [status|up|down]")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
