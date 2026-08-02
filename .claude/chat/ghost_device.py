"""Ghost Phone P4.1 device-operation slice — the takeover powers.

Where ``ghost_control.py`` owns the ghost's LIFECYCLE (boot / status / shutdown),
this module owns the capability-gated DEVICE OPERATIONS: see its screen, tap /
type / swipe on it, launch + install apps. Every operation:

  1. routes through ``require_ghost_capability(<cap>, target="ghost")`` — the
     structural ghost-only invariant is checked FIRST, so none of these can ever
     touch the operator's personal phone / desktop;
  2. resolves the ghost's OWN serial via ``_resolve_adb_serial_or_raise("ghost")``
     — never single-device autodetect (that could fall onto the phone);
  3. drives raw adb through ``adb_control`` (screencap via a bounded, binary
     ``Popen`` stream; input/app via ``run_adb``), NEVER agent-browser (its
     daemon wedges on the emulator — confirmed live 2026-07-06).

HUMANIZED INPUT (2026-07-07): naive ``input tap/swipe/text`` produces the exact
kinematic tells anti-bot systems sample for — pixel-perfect coordinates, zero
press dwell, dead-straight constant-velocity swipes, and instant whole-string
typing. By default the input verbs now shape input like a human hand: position
JITTER, a press DWELL, CURVED variable-velocity swipe paths (quadratic bezier +
eased timing via ``input motionevent``), and per-keystroke typing CADENCE. This
is deterministic-testable via an injectable ``rng`` + ``sleep`` and can be turned
off per call (``humanize=False``) for a precise/scripted action. HONEST LIMIT:
``adb shell input`` cannot set touch PRESSURE or TOOL_TYPE (those read as 0 /
UNKNOWN and are a separate, lower-level tell); faking them needs ``sendevent``,
which is device-specific and out of scope here.

The capability seam audits every attempt; the dashboard/API layer adds its own
audit row on top.
"""

from __future__ import annotations

import functools
import hmac
import math
import os
import random
import re
import shlex
import struct
import subprocess
import threading
import time
import zlib
from pathlib import Path
from typing import Any

import adb_control
import ghost_capabilities
import ghost_control
from browser_control import _resolve_adb_serial_or_raise

# screencap of a 1080x2400 device is ~1-3 MB of PNG; give it room but bound it.
SCREENCAP_TIMEOUT_SECONDS = 20
MAX_SCREENCAP_BYTES = 10 * 1024 * 1024
MAX_SCREENCAP_STDERR_BYTES = 8 * 1024
_SCREENCAP_READ_CHUNK_BYTES = 64 * 1024
_SCREENCAP_CLEANUP_TIMEOUT_SECONDS = 0.25
INPUT_TIMEOUT_SECONDS = 10
MAX_AUDIT_REASON_CHARS = 2048

# Bound operator-typed text; input text is one adb call, not a paste buffer.
MAX_TEXT_LEN = 500
# Android keyevent codes: 0..~310. Bound to a sane range (reject arbitrary ints).
MAX_KEYCODE = 320

# ── Humanization knobs (real touch is jittered, dwelled, curved, irregular) ───
# Default RNG seeded from OS entropy at import; tests inject a seeded Random.
_RNG = random.Random()
_TAP_DWELL_MS = (45, 130)  # human tap press-and-release time
_TYPE_DELAY_S = (0.03, 0.18)  # inter-keystroke pause
_SWIPE_STEPS = (10, 16)  # motionevent MOVE points along the path
_SWIPE_DURATION_JITTER = (0.85, 1.25)  # multiplier on the requested duration
_SWIPE_BOW = 0.08  # max perpendicular arc, fraction of path length


def _rng_for(rng: random.Random | None) -> random.Random:
    return rng if rng is not None else _RNG


def _clamp_px(value: int, size: int) -> int:
    return max(0, min(size - 1, value))


def _jitter_px(value: int, size: int, *, rng: random.Random, radius: int) -> int:
    if radius <= 0:
        return _clamp_px(value, size)
    return _clamp_px(value + rng.randint(-radius, radius), size)


def _tap_jitter_radius(width: int, height: int) -> int:
    # ~0.8% of the smaller edge (≈9px on 1080), min 2 — well under any tap target.
    return max(2, min(width, height) // 120)


def _ease_in_out(t: float) -> float:
    """easeInOutQuad — slow at the ends, fast in the middle (human velocity)."""
    return 2 * t * t if t < 0.5 else 1 - ((-2 * t + 2) ** 2) / 2


def _bezier(t: float, p0: float, p1: float, p2: float) -> float:
    mt = 1 - t
    return mt * mt * p0 + 2 * mt * t * p1 + t * t * p2


def _human_swipe_path(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    width: int,
    height: int,
    rng: random.Random,
    steps: int,
) -> list[tuple[int, int]]:
    """Quadratic-bezier path with a perpendicular BOW, eased point spacing
    (variable velocity), and small per-point jitter. Endpoints stay put."""
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy) or 1.0
    perp_x, perp_y = -dy / dist, dx / dist  # unit perpendicular
    bow = rng.uniform(-_SWIPE_BOW, _SWIPE_BOW) * dist  # arc to either side
    ctrl_x = (x1 + x2) / 2 + perp_x * bow
    ctrl_y = (y1 + y2) / 2 + perp_y * bow
    pts: list[tuple[int, int]] = []
    for i in range(steps + 1):
        t = _ease_in_out(i / steps)
        bx = int(round(_bezier(t, x1, ctrl_x, x2)))
        by = int(round(_bezier(t, y1, ctrl_y, y2)))
        radius = 0 if i in (0, steps) else 2  # never move the real endpoints
        pts.append(
            (
                _jitter_px(bx, width, rng=rng, radius=radius),
                _jitter_px(by, height, rng=rng, radius=radius),
            )
        )
    return pts


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Override reflects the CURRENT display; prefer it over the panel's Physical size.
_WM_OVERRIDE_RE = re.compile(r"Override size:\s*(\d+)x(\d+)")
_WM_PHYSICAL_RE = re.compile(r"Physical size:\s*(\d+)x(\d+)")
# Android package name — letters/digits/underscore segments joined by dots.
_PACKAGE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z][a-zA-Z0-9_]*)+$")
INSTALL_TIMEOUT_SECONDS = 120
MAX_DEVICE_DIMENSION = 16_384


def _terminal_capability(capability: str, *, default_caller: str):
    """Replace the gate's provisional ``allowed`` row with one terminal row.

    Gate refusals are forwarded immediately as their single ``blocked`` row.
    Once the gate opens, validation and execution are wrapped so the capability
    seam records exactly one truthful ``succeeded`` or ``failed`` outcome.
    """

    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            pending: dict[str, Any] = {}

            def gate_audit(**row: Any) -> None:
                if row.get("outcome") == "allowed":
                    pending.update(row)
                else:
                    ghost_capabilities._default_audit(**row)

            ghost_capabilities.require_ghost_capability(
                capability,
                target="ghost",
                environ=kwargs.get("environ"),
                caller=kwargs.get("caller", default_caller),
                audit=gate_audit,
            )
            try:
                result = function(*args, **kwargs)
            except Exception as exc:
                row = dict(pending)
                row.update(
                    outcome="failed",
                    reason=(str(exc) or type(exc).__name__)[:MAX_AUDIT_REASON_CHARS],
                )
                ghost_capabilities._default_audit(**row)
                raise
            row = dict(pending)
            row.update(outcome="succeeded", reason="completed")
            ghost_capabilities._default_audit(**row)
            return result

        return wrapped

    return decorate


def _identity_property(
    name: str, *, serial: str, environ: dict[str, str] | None, runner: Any
) -> str:
    kwargs: dict[str, Any] = {
        "serial": serial,
        "environ": environ,
        "timeout": INPUT_TIMEOUT_SECONDS,
    }
    if runner is not None:
        kwargs["runner"] = runner
    result = adb_control.run_adb(["shell", "getprop", name], **kwargs)
    if not result.ok:
        raise RuntimeError("Ghost backend identity check failed: device property unavailable")
    return (result.stdout or "").strip()


def _resolve_verified_ghost_serial(*, environ: dict[str, str] | None, runner: Any) -> str:
    """Resolve the configured backend and verify its live transport identity.

    ``HOMIE_GHOST_AVD`` selects the existing AVD backend; without it, the
    existing dedicated-spare backend is selected. Both are pinned to the exact
    configured ghost serial and must be in adb ``device`` state. AVD mode also
    proves qemu + configured AVD-name properties. Spare mode proves it is not an
    emulator. The personal-phone collision guard runs first in the shared
    serial resolver.
    """

    env = environ if environ is not None else os.environ
    serial = _resolve_adb_serial_or_raise("ghost", environ=env)
    if serial is None:  # defensive: the shared resolver should already raise
        raise RuntimeError("Ghost backend identity check failed: configured serial is missing")
    avd = ghost_control.resolve_ghost_avd(environ=env)
    if avd and not re.fullmatch(r"emulator-\d+", serial):
        raise RuntimeError(
            "Ghost backend identity check failed: AVD mode requires the configured emulator serial"
        )

    state_runner = runner if runner is not None else subprocess.run
    status = ghost_control.ghost_status(runner=state_runner, environ=env)
    if not (
        status.get("running")
        and status.get("booted")
        and status.get("serial") == serial
        and status.get("avd") == avd
    ):
        raise RuntimeError(
            "Ghost backend identity check failed: configured device is not live and booted"
        )

    qemu = _identity_property("ro.kernel.qemu", serial=serial, environ=env, runner=runner)
    if avd:
        if qemu != "1":
            raise RuntimeError(
                "Ghost backend identity check failed: configured AVD transport is not an emulator"
            )
        live_avd = _identity_property(
            "ro.boot.qemu.avd_name", serial=serial, environ=env, runner=runner
        )
        if live_avd != avd:
            raise RuntimeError(
                "Ghost backend identity check failed: live emulator is not the configured AVD"
            )
    else:
        if qemu == "1" or re.fullmatch(r"emulator-\d+", serial):
            raise RuntimeError(
                "Ghost backend identity check failed: spare-device mode resolved an emulator"
            )
        expected_hardware_id = env.get("HOMIE_GHOST_SPARE_HARDWARE_ID", "")
        if not expected_hardware_id.strip():
            raise RuntimeError(
                "Ghost backend identity check failed: spare hardware binding is not configured"
            )
        # ro.serialno is device-owned and independent of the adb transport serial,
        # unlike a USB serial or host:port endpoint. Pin and verify it exactly.
        live_hardware_id = _identity_property(
            "ro.serialno", serial=serial, environ=env, runner=runner
        )
        if not live_hardware_id or not hmac.compare_digest(
            live_hardware_id.encode("utf-8"), expected_hardware_id.encode("utf-8")
        ):
            raise RuntimeError(
                "Ghost backend identity check failed: spare hardware binding mismatch"
            )
    return serial


def _png_dimensions(png: bytes) -> tuple[int, int]:
    """Strictly validate a bounded PNG and return its dimensions."""
    if len(png) > MAX_SCREENCAP_BYTES:
        raise ValueError("screencap PNG is too large")
    if not png.startswith(_PNG_SIGNATURE):
        raise ValueError("screencap did not return a valid PNG")
    offset, index, seen_idat = 8, 0, False
    width = height = 0
    while offset < len(png):
        if len(png) - offset < 12:
            raise ValueError("screencap PNG is truncated")
        length = struct.unpack(">I", png[offset : offset + 4])[0]
        end = offset + 12 + length
        if length > MAX_SCREENCAP_BYTES or end > len(png):
            raise ValueError("screencap PNG has invalid chunk length")
        kind = png[offset + 4 : offset + 8]
        data = png[offset + 8 : offset + 8 + length]
        crc = struct.unpack(">I", png[offset + 8 + length : end])[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != crc:
            raise ValueError("screencap PNG has invalid CRC")
        if index == 0:
            if kind != b"IHDR" or length != 13:
                raise ValueError("screencap PNG has invalid IHDR")
            width, height, depth, colour, comp, filt, interlace = struct.unpack(">IIBBBBB", data)
            depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if not (
                1 <= width <= MAX_DEVICE_DIMENSION
                and 1 <= height <= MAX_DEVICE_DIMENSION
                and depth in depths.get(colour, set())
                and comp == filt == 0
                and interlace in (0, 1)
            ):
                raise ValueError("screencap PNG has invalid IHDR")
        elif kind == b"IHDR":
            raise ValueError("screencap PNG has duplicate IHDR")
        seen_idat |= kind == b"IDAT"
        if kind == b"IEND":
            if length or not seen_idat or end != len(png):
                raise ValueError("screencap PNG has invalid IEND")
            return int(width), int(height)
        offset, index = end, index + 1
    raise ValueError("screencap PNG has no terminal IEND")


def _close_process_pipes(process: Any) -> None:
    for stream in (getattr(process, "stdout", None), getattr(process, "stderr", None)):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


def _terminate_kill_reap(process: Any) -> None:
    """Best-effort bounded shutdown: terminate, kill if stubborn, always wait."""

    try:
        process.terminate()
    except Exception:
        pass
    try:
        process.wait(timeout=_SCREENCAP_CLEANUP_TIMEOUT_SECONDS)
        return
    except Exception:
        pass
    try:
        process.kill()
    except Exception:
        pass
    try:
        process.wait(timeout=_SCREENCAP_CLEANUP_TIMEOUT_SECONDS)
    except Exception:
        pass


def _bounded_stderr_text(parts: list[bytes], *, truncated: bool) -> str:
    marker = " [stderr truncated]" if truncated else ""
    text = b"".join(parts).decode("utf-8", "replace").strip()
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_SCREENCAP_STDERR_BYTES - len(marker.encode("utf-8")):
        marker = " [stderr truncated]"
    budget = max(0, MAX_SCREENCAP_STDERR_BYTES - len(marker.encode("utf-8")))
    if len(encoded) > budget:
        text = encoded[:budget].decode("utf-8", "ignore")
    return (text + marker).strip()


def _read_screencap_popen(
    argv: list[str],
    *,
    popen_factory: Any,
    timeout_seconds: float,
) -> bytes:
    """Read raw PNG stdout incrementally with hard memory/time bounds."""

    process = popen_factory(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    if process.stdout is None or process.stderr is None:
        _terminate_kill_reap(process)
        _close_process_pipes(process)
        raise RuntimeError("adb screencap did not expose stdout/stderr pipes")

    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    state: dict[str, Any] = {
        "stdout_bytes": 0,
        "overflow": False,
        "stderr_bytes": 0,
        "stderr_truncated": False,
        "stdout_error": None,
        "stderr_error": None,
    }
    stdout_done = threading.Event()
    stderr_done = threading.Event()
    progress = threading.Event()

    def read_stdout() -> None:
        try:
            while True:
                remaining = MAX_SCREENCAP_BYTES + 1 - state["stdout_bytes"]
                if remaining <= 0:
                    state["overflow"] = True
                    return
                chunk = process.stdout.read(min(_SCREENCAP_READ_CHUNK_BYTES, remaining))
                if not chunk:
                    return
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                stdout_parts.append(chunk)
                state["stdout_bytes"] += len(chunk)
                if state["stdout_bytes"] > MAX_SCREENCAP_BYTES:
                    state["overflow"] = True
                    return
                progress.set()
        except Exception as exc:
            state["stdout_error"] = exc
        finally:
            stdout_done.set()
            progress.set()

    def read_stderr() -> None:
        try:
            while True:
                chunk = process.stderr.read(_SCREENCAP_READ_CHUNK_BYTES)
                if not chunk:
                    return
                room = MAX_SCREENCAP_STDERR_BYTES - state["stderr_bytes"]
                if room > 0:
                    kept = chunk[:room]
                    stderr_parts.append(kept)
                    state["stderr_bytes"] += len(kept)
                if len(chunk) > max(room, 0) or room <= 0:
                    state["stderr_truncated"] = True
                progress.set()
        except Exception as exc:
            state["stderr_error"] = exc
        finally:
            stderr_done.set()
            progress.set()

    threads = (
        threading.Thread(target=read_stdout, name="ghost-screencap-stdout", daemon=True),
        threading.Thread(target=read_stderr, name="ghost-screencap-stderr", daemon=True),
    )
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout_seconds

    def cleanup() -> None:
        _terminate_kill_reap(process)
        _close_process_pipes(process)
        for thread in threads:
            thread.join(timeout=_SCREENCAP_CLEANUP_TIMEOUT_SECONDS)

    while not (stdout_done.is_set() and stderr_done.is_set()):
        if state["overflow"]:
            cleanup()
            raise RuntimeError("Ghost screencap exceeded the hard 10 MiB limit")
        if state["stdout_error"] is not None or state["stderr_error"] is not None:
            cleanup()
            raise RuntimeError("Ghost screencap pipe read failed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            cleanup()
            raise TimeoutError(f"Ghost screencap timed out after {timeout_seconds:g}s")
        progress.wait(timeout=min(0.01, remaining))
        progress.clear()

    if state["overflow"]:
        cleanup()
        raise RuntimeError("Ghost screencap exceeded the hard 10 MiB limit")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        cleanup()
        raise TimeoutError(f"Ghost screencap timed out after {timeout_seconds:g}s")
    try:
        returncode = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        cleanup()
        raise TimeoutError(f"Ghost screencap timed out after {timeout_seconds:g}s") from exc
    finally:
        _close_process_pipes(process)
    if returncode != 0:
        detail = _bounded_stderr_text(stderr_parts, truncated=bool(state["stderr_truncated"]))
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"adb screencap failed (exit {returncode}){suffix}")
    return b"".join(stdout_parts)


@_terminal_capability("ghost.screen.view", default_caller="ghost.screen.view")
def ghost_screencap(
    *,
    environ: dict[str, str] | None = None,
    runner: Any = None,
    popen_factory: Any = None,
    timeout_seconds: float | None = None,
    caller: str = "ghost.screen.view",
) -> tuple[bytes, int, int]:
    """Capture the ghost's live screen. Returns (png_bytes, width, height).

    Gated by ``ghost.screen.view``; refuses any target != "ghost" and any
    serial-less ghost. Uses raw ``adb exec-out screencap -p`` (bytes straight to
    stdout — no on-device temp file, no ``pull``, no Git-Bash path mangling).
    """

    timeout = SCREENCAP_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("screencap timeout must be a finite positive native number")
    serial = _resolve_verified_ghost_serial(environ=environ, runner=runner)
    adb = adb_control.resolve_adb(environ=environ)
    factory = subprocess.Popen if popen_factory is None else popen_factory
    png = _read_screencap_popen(
        [adb, "-s", serial, "exec-out", "screencap", "-p"],
        popen_factory=factory,
        timeout_seconds=float(timeout),
    )
    width, height = _png_dimensions(png)
    return png, width, height


# ── Input surface (tap / type / swipe / key) — the RDP feature ────────────────


def _run_adb(args: list[str], *, serial: str | None, environ, runner) -> adb_control.AdbResult:
    kwargs: dict[str, Any] = {
        "serial": serial,
        "environ": environ,
        "timeout": INPUT_TIMEOUT_SECONDS,
    }
    if runner is not None:
        kwargs["runner"] = runner
    result = adb_control.run_adb(args, **kwargs)
    if not result.ok:
        raise RuntimeError("Ghost device command failed")
    return result


def ghost_device_size(*, serial: str | None, environ, runner) -> tuple[int, int]:
    """The ghost's live display dimensions from ``adb shell wm size`` (prefers
    the Override size when present). The coord scaler resolves this FRESH per
    request (Rule 2) — never a cached/assumed 1080x2400."""

    res = _run_adb(["shell", "wm", "size"], serial=serial, environ=environ, runner=runner)
    out = res.output or ""
    match = _WM_OVERRIDE_RE.search(out) or _WM_PHYSICAL_RE.search(out)
    if not match:
        raise RuntimeError("could not read Ghost display size")
    width, height = int(match.group(1)), int(match.group(2))
    if not (1 <= width <= MAX_DEVICE_DIMENSION and 1 <= height <= MAX_DEVICE_DIMENSION):
        raise RuntimeError("Ghost display size is outside the supported range")
    return width, height


def _to_device_pixel(norm: float, size: int) -> int:
    """Strict normalized [0,1] display coordinate -> a real device pixel.

    The server owns the scale — the client only ever sends floats relative to
    the image it was shown, never raw device pixels.
    """

    if isinstance(norm, bool) or type(norm) not in (int, float):
        raise ValueError("coordinate must be a native finite number in [0, 1]")
    value = norm
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("coordinate must be finite and in [0, 1]")
    return max(0, min(size - 1, int(round(value * size))))


@_terminal_capability("ghost.input.tap", default_caller="ghost.input.tap")
def ghost_tap(
    x_norm: float,
    y_norm: float,
    *,
    humanize: bool = True,
    rng: random.Random | None = None,
    environ: dict[str, str] | None = None,
    runner: Any = None,
    caller: str = "ghost.input.tap",
) -> dict[str, Any]:
    """Tap the ghost at a normalized (x, y). Server scales to device pixels.

    Humanized by default: the tap lands within a few pixels of the target (real
    fingers don't hit the exact same pixel) and is sent as a short DWELL touch
    with micro-drift (via ``input swipe``) instead of ``input tap``'s
    instantaneous zero-dwell touch. ``humanize=False`` sends the exact
    ``input tap x y`` for a precise/scripted action. The reported (x, y) is the
    NOMINAL scaled target, not the jittered pixel."""

    # Validate client coordinates before any adb call; invalid input fails closed.
    _to_device_pixel(x_norm, 1)
    _to_device_pixel(y_norm, 1)
    serial = _resolve_verified_ghost_serial(environ=environ, runner=runner)
    width, height = ghost_device_size(serial=serial, environ=environ, runner=runner)
    x, y = _to_device_pixel(x_norm, width), _to_device_pixel(y_norm, height)
    if humanize:
        r = _rng_for(rng)
        rad = _tap_jitter_radius(width, height)
        x1, y1 = (
            _jitter_px(x, width, rng=r, radius=rad),
            _jitter_px(y, height, rng=r, radius=rad),
        )
        x2, y2 = (
            _jitter_px(x, width, rng=r, radius=rad),
            _jitter_px(y, height, rng=r, radius=rad),
        )
        dwell = r.randint(*_TAP_DWELL_MS)
        # A short swipe with micro-drift + dwell reads as a human press (DOWN,
        # hold, tiny drift, UP), not input tap's instantaneous zero-dwell touch.
        _run_adb(
            ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(dwell)],
            serial=serial,
            environ=environ,
            runner=runner,
        )
    else:
        _run_adb(
            ["shell", "input", "tap", str(x), str(y)],
            serial=serial,
            environ=environ,
            runner=runner,
        )
    return {"x": x, "y": y, "width": width, "height": height, "humanized": humanize}


def _input_text_command(chunk: str) -> str:
    """One injection-safe `input text …` shell command for a chunk of text.

    SECURITY: `adb shell <args>` re-joins its args and re-parses them through the
    device shell (`sh -c`), so a raw `input text x;reboot` would run `reboot` on
    the ghost. Building the whole `input text …` as ONE shlex.quoted command means
    the device shell treats the text LITERALLY — shell metacharacters
    (`; & | $ \\` newline …`) can never break out. %s is `input`'s own space
    encoder, applied before quoting."""
    return "input text " + shlex.quote(chunk.replace(" ", "%s"))


@_terminal_capability("ghost.input.tap", default_caller="ghost.input.text")
def ghost_text(
    text: str,
    *,
    humanize: bool = True,
    rng: random.Random | None = None,
    sleep: Any = None,
    environ: dict[str, str] | None = None,
    runner: Any = None,
    caller: str = "ghost.input.text",
) -> dict[str, Any]:
    """Type text on the ghost (adb input text). Length-capped.

    Humanized by default: typed CHARACTER BY CHARACTER with randomized
    inter-keystroke pauses, so the input stream carries a human typing cadence
    instead of one instantaneous whole-string injection (typing rhythm is a known
    behavioral biometric). ``humanize=False`` sends the whole string in one
    injection-safe `input text` call. Either way each chunk is shlex-quoted, so
    shell metacharacters can never break out to a device command."""

    body = (text or "")[:MAX_TEXT_LEN]
    serial = _resolve_verified_ghost_serial(environ=environ, runner=runner)
    if humanize and body:
        r = _rng_for(rng)
        slp = sleep if sleep is not None else time.sleep
        for i, ch in enumerate(body):
            _run_adb(
                ["shell", _input_text_command(ch)],
                serial=serial,
                environ=environ,
                runner=runner,
            )
            if i < len(body) - 1:
                slp(r.uniform(*_TYPE_DELAY_S))
    else:
        _run_adb(
            ["shell", _input_text_command(body)],
            serial=serial,
            environ=environ,
            runner=runner,
        )
    return {"length": len(body), "humanized": humanize}


def _plain_swipe(x1, y1, x2, y2, dur, *, serial, environ, runner) -> None:
    _run_adb(
        ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(dur)],
        serial=serial,
        environ=environ,
        runner=runner,
    )


@_terminal_capability("ghost.input.tap", default_caller="ghost.input.swipe")
def ghost_swipe(
    x1_norm: float,
    y1_norm: float,
    x2_norm: float,
    y2_norm: float,
    *,
    duration_ms: int = 300,
    humanize: bool = True,
    rng: random.Random | None = None,
    sleep: Any = None,
    environ: dict[str, str] | None = None,
    runner: Any = None,
    caller: str = "ghost.input.swipe",
) -> dict[str, Any]:
    """Swipe from a normalized start to a normalized end over duration_ms.

    Humanized by default: instead of ``input swipe``'s dead-straight,
    constant-velocity line (the classic automation tell), the gesture traces a
    slightly CURVED path (quadratic bezier with a random perpendicular bow) at
    VARIABLE velocity (eased point spacing — slow at the ends, fast in the
    middle) with small per-point jitter, emitted as an ``input motionevent``
    DOWN / MOVE… / UP sequence. Duration is jittered around the request.
    ``humanize=False`` sends the exact ``input swipe`` line. On any motionevent
    failure it releases (best-effort UP) and falls back to a plain swipe so the
    gesture still completes. Reported endpoints are the NOMINAL scaled targets."""

    # Validate every coordinate and duration before the first adb request.
    for coordinate in (x1_norm, y1_norm, x2_norm, y2_norm):
        _to_device_pixel(coordinate, 1)
    if isinstance(duration_ms, bool) or type(duration_ms) is not int:
        raise ValueError("swipe duration must be a native integer")
    serial = _resolve_verified_ghost_serial(environ=environ, runner=runner)
    width, height = ghost_device_size(serial=serial, environ=environ, runner=runner)
    x1, y1 = _to_device_pixel(x1_norm, width), _to_device_pixel(y1_norm, height)
    x2, y2 = _to_device_pixel(x2_norm, width), _to_device_pixel(y2_norm, height)
    dur = max(1, min(10_000, int(duration_ms)))

    if not humanize:
        _plain_swipe(x1, y1, x2, y2, dur, serial=serial, environ=environ, runner=runner)
        return {
            "x1": x1,
            "y1": y1,
            "x2": x2,
            "y2": y2,
            "duration_ms": dur,
            "humanized": False,
        }

    r = _rng_for(rng)
    slp = sleep if sleep is not None else time.sleep
    steps = r.randint(*_SWIPE_STEPS)
    hdur = max(1, int(dur * r.uniform(*_SWIPE_DURATION_JITTER)))
    path = _human_swipe_path(x1, y1, x2, y2, width=width, height=height, rng=r, steps=steps)
    per_move_s = (hdur / 1000.0) / max(1, len(path) - 1)

    def _fallback() -> dict[str, Any]:
        # Never leave a stuck finger: best-effort release, then a plain swipe so
        # the operator's gesture still lands (older devices lacking motionevent).
        try:
            _run_adb(
                ["shell", "input", "motionevent", "UP", str(x2), str(y2)],
                serial=serial,
                environ=environ,
                runner=runner,
            )
        except Exception:
            pass
        _plain_swipe(x1, y1, x2, y2, hdur, serial=serial, environ=environ, runner=runner)
        return {
            "x1": x1,
            "y1": y1,
            "x2": x2,
            "y2": y2,
            "duration_ms": hdur,
            "humanized": True,
            "fallback": True,
            "steps": len(path),
        }

    try:
        px0, py0 = path[0]
        down = _run_adb(
            ["shell", "input", "motionevent", "DOWN", str(px0), str(py0)],
            serial=serial,
            environ=environ,
            runner=runner,
        )
        # `input motionevent` needs Android 11+; a non-zero DOWN means the device
        # lacks it (run_adb returns ok=False without raising) -> fall back.
        if not down.ok:
            return _fallback()
        for mx, my in path[1:]:
            _run_adb(
                ["shell", "input", "motionevent", "MOVE", str(mx), str(my)],
                serial=serial,
                environ=environ,
                runner=runner,
            )
            slp(per_move_s * r.uniform(0.6, 1.4))  # irregular inter-move timing
        pxn, pyn = path[-1]
        _run_adb(
            ["shell", "input", "motionevent", "UP", str(pxn), str(pyn)],
            serial=serial,
            environ=environ,
            runner=runner,
        )
    except Exception:
        return _fallback()
    return {
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "duration_ms": hdur,
        "humanized": True,
        "steps": len(path),
    }


@_terminal_capability("ghost.input.tap", default_caller="ghost.input.key")
def ghost_keyevent(
    code: int,
    *,
    environ: dict[str, str] | None = None,
    runner: Any = None,
    caller: str = "ghost.input.key",
) -> dict[str, int]:
    """Send an Android keyevent (e.g. 4 = BACK, 3 = HOME, 66 = ENTER). The code
    is validated to a bounded int range — never a free-form string."""

    if isinstance(code, bool) or type(code) is not int:
        raise ValueError("keyevent code must be a native integer")
    keycode = code
    if not 0 <= keycode <= MAX_KEYCODE:
        raise ValueError(f"keyevent code {keycode} out of range [0, {MAX_KEYCODE}]")
    serial = _resolve_verified_ghost_serial(environ=environ, runner=runner)
    _run_adb(
        ["shell", "input", "keyevent", str(keycode)],
        serial=serial,
        environ=environ,
        runner=runner,
    )
    return {"keycode": keycode}


# ── App surface (launch / install) ───────────────────────────────────────────


@_terminal_capability("ghost.app.launch", default_caller="ghost.app.launch")
def ghost_app_launch(
    package: str,
    *,
    environ: dict[str, str] | None = None,
    runner: Any = None,
    caller: str = "ghost.app.launch",
) -> dict[str, str]:
    """Launch an app on the ghost by package name (adb monkey LAUNCHER).

    The package is validated against the Android package grammar — never a
    free-form string that could smuggle extra ``monkey`` arguments.
    """

    pkg = (package or "").strip()
    if not _PACKAGE_RE.match(pkg):
        raise ValueError(f"invalid Android package name: {package!r}")
    serial = _resolve_verified_ghost_serial(environ=environ, runner=runner)
    res = _run_adb(
        ["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"],
        serial=serial,
        environ=environ,
        runner=runner,
    )
    # monkey prints "No activities found ... aborting" (still exit 0) when the
    # package is absent — surface that as a failure instead of a false success.
    if "No activities found" in (res.output or "") or "aborting" in (res.output or "").lower():
        raise RuntimeError(f"no launchable activity for {pkg} (is it installed?)")
    return {"package": pkg}


@_terminal_capability("ghost.app.install", default_caller="ghost.app.install")
def ghost_app_install(
    apk_path: str,
    *,
    environ: dict[str, str] | None = None,
    runner: Any = None,
    caller: str = "ghost.app.install",
) -> dict[str, str]:
    """Install a LOCAL APK on the ghost (adb install). The path is validated as
    an existing ``.apk`` file on the host — no on-device path, no Git-Bash
    mangling concern."""

    raw = (apk_path or "").strip()
    if not raw:
        raise ValueError("apk_path is required")
    path = Path(raw)
    if path.suffix.lower() != ".apk":
        raise ValueError(f"not an .apk file: {raw!r}")
    if not path.is_file():
        raise ValueError(f"APK not found: {raw!r}")
    serial = _resolve_verified_ghost_serial(environ=environ, runner=runner)
    kwargs: dict[str, Any] = {
        "serial": serial,
        "environ": environ,
        "timeout": INSTALL_TIMEOUT_SECONDS,
    }
    if runner is not None:
        kwargs["runner"] = runner
    res = adb_control.run_adb(["install", str(path)], **kwargs)
    output = res.output or ""
    # adb install can exit 0 while printing "Failure [INSTALL_FAILED_*]".
    if not res.ok or "Failure" in output:
        raise RuntimeError(f"install failed: {output or 'adb install returned no output'}")
    return {"apk": path.name}
