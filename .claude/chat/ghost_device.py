"""Ghost Phone P4.1 device-operation slice — the takeover powers.

Where ``ghost_control.py`` owns the ghost's LIFECYCLE (boot / status / shutdown),
this module owns the capability-gated DEVICE OPERATIONS: see its screen, tap /
type / swipe on it, launch + install apps. Every operation:

  1. routes through ``require_ghost_capability(<cap>, target="ghost")`` — the
     structural ghost-only invariant is checked FIRST, so none of these can ever
     touch the operator's personal phone / desktop;
  2. resolves the ghost's OWN serial via ``_resolve_adb_serial_or_raise("ghost")``
     — never single-device autodetect (that could fall onto the phone);
  3. drives raw adb through ``adb_control`` (screencap via the BINARY-safe
     ``adb_exec_out``; input/app via ``run_adb``), NEVER agent-browser (its
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

import math
import random
import re
import shlex
import struct
import time
from pathlib import Path
from typing import Any

import adb_control
from browser_control import _resolve_adb_serial_or_raise
from ghost_capabilities import require_ghost_capability

# screencap of a 1080x2400 device is ~1-3 MB of PNG; give it room but bound it.
SCREENCAP_TIMEOUT_SECONDS = 20
INPUT_TIMEOUT_SECONDS = 10

# Bound operator-typed text; input text is one adb call, not a paste buffer.
MAX_TEXT_LEN = 500
# Android keyevent codes: 0..~310. Bound to a sane range (reject arbitrary ints).
MAX_KEYCODE = 320

# ── Humanization knobs (real touch is jittered, dwelled, curved, irregular) ───
# Default RNG seeded from OS entropy at import; tests inject a seeded Random.
_RNG = random.Random()
_TAP_DWELL_MS = (45, 130)          # human tap press-and-release time
_TYPE_DELAY_S = (0.03, 0.18)       # inter-keystroke pause
_SWIPE_STEPS = (10, 16)            # motionevent MOVE points along the path
_SWIPE_DURATION_JITTER = (0.85, 1.25)  # multiplier on the requested duration
_SWIPE_BOW = 0.08                  # max perpendicular arc, fraction of path length


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
    x1: int, y1: int, x2: int, y2: int, *, width: int, height: int,
    rng: random.Random, steps: int,
) -> list[tuple[int, int]]:
    """Quadratic-bezier path with a perpendicular BOW, eased point spacing
    (variable velocity), and small per-point jitter. Endpoints stay put."""
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy) or 1.0
    perp_x, perp_y = -dy / dist, dx / dist            # unit perpendicular
    bow = rng.uniform(-_SWIPE_BOW, _SWIPE_BOW) * dist  # arc to either side
    ctrl_x = (x1 + x2) / 2 + perp_x * bow
    ctrl_y = (y1 + y2) / 2 + perp_y * bow
    pts: list[tuple[int, int]] = []
    for i in range(steps + 1):
        t = _ease_in_out(i / steps)
        bx = int(round(_bezier(t, x1, ctrl_x, x2)))
        by = int(round(_bezier(t, y1, ctrl_y, y2)))
        radius = 0 if i in (0, steps) else 2          # never move the real endpoints
        pts.append(
            (_jitter_px(bx, width, rng=rng, radius=radius),
             _jitter_px(by, height, rng=rng, radius=radius))
        )
    return pts

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Override reflects the CURRENT display; prefer it over the panel's Physical size.
_WM_OVERRIDE_RE = re.compile(r"Override size:\s*(\d+)x(\d+)")
_WM_PHYSICAL_RE = re.compile(r"Physical size:\s*(\d+)x(\d+)")
# Android package name — letters/digits/underscore segments joined by dots.
_PACKAGE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z][a-zA-Z0-9_]*)+$")
INSTALL_TIMEOUT_SECONDS = 120


def _png_dimensions(png: bytes) -> tuple[int, int]:
    """Parse (width, height) from a PNG's IHDR header without an image library.

    Layout: 8-byte signature, then the IHDR chunk (4-byte length, 4-byte type
    'IHDR', then width + height as big-endian uint32 at bytes 16-24).
    """

    if len(png) < 24 or not png.startswith(_PNG_SIGNATURE) or png[12:16] != b"IHDR":
        raise ValueError("screencap did not return a PNG (no IHDR header)")
    width, height = struct.unpack(">II", png[16:24])
    return int(width), int(height)


def ghost_screencap(
    *,
    environ: dict[str, str] | None = None,
    runner: Any = None,
    caller: str = "ghost.screen.view",
) -> tuple[bytes, int, int]:
    """Capture the ghost's live screen. Returns (png_bytes, width, height).

    Gated by ``ghost.screen.view``; refuses any target != "ghost" and any
    serial-less ghost. Uses raw ``adb exec-out screencap -p`` (bytes straight to
    stdout — no on-device temp file, no ``pull``, no Git-Bash path mangling).
    """

    require_ghost_capability("ghost.screen.view", target="ghost", environ=environ, caller=caller)
    serial = _resolve_adb_serial_or_raise("ghost", environ=environ)
    kwargs: dict[str, Any] = {
        "serial": serial,
        "environ": environ,
        "timeout": SCREENCAP_TIMEOUT_SECONDS,
    }
    if runner is not None:
        kwargs["runner"] = runner
    png = adb_control.adb_exec_out(["exec-out", "screencap", "-p"], **kwargs)
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
    return adb_control.run_adb(args, **kwargs)


def ghost_device_size(*, serial: str | None, environ, runner) -> tuple[int, int]:
    """The ghost's live display dimensions from ``adb shell wm size`` (prefers
    the Override size when present). The coord scaler resolves this FRESH per
    request (Rule 2) — never a cached/assumed 1080x2400."""

    res = _run_adb(["shell", "wm", "size"], serial=serial, environ=environ, runner=runner)
    out = res.output or ""
    match = _WM_OVERRIDE_RE.search(out) or _WM_PHYSICAL_RE.search(out)
    if not match:
        raise RuntimeError(f"could not read ghost display size from: {out or '(no output)'}")
    return int(match.group(1)), int(match.group(2))


def _to_device_pixel(norm: float, size: int) -> int:
    """Normalized [0,1] display coordinate -> a real device pixel, clamped.

    The server owns the scale — the client only ever sends floats relative to
    the image it was shown, never raw device pixels.
    """

    clamped = 0.0 if norm < 0 else (1.0 if norm > 1 else float(norm))
    return max(0, min(size - 1, int(round(clamped * size))))


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

    require_ghost_capability("ghost.input.tap", target="ghost", environ=environ, caller=caller)
    serial = _resolve_adb_serial_or_raise("ghost", environ=environ)
    width, height = ghost_device_size(serial=serial, environ=environ, runner=runner)
    x, y = _to_device_pixel(x_norm, width), _to_device_pixel(y_norm, height)
    if humanize:
        r = _rng_for(rng)
        rad = _tap_jitter_radius(width, height)
        x1, y1 = _jitter_px(x, width, rng=r, radius=rad), _jitter_px(y, height, rng=r, radius=rad)
        x2, y2 = _jitter_px(x, width, rng=r, radius=rad), _jitter_px(y, height, rng=r, radius=rad)
        dwell = r.randint(*_TAP_DWELL_MS)
        # A short swipe with micro-drift + dwell reads as a human press (DOWN,
        # hold, tiny drift, UP), not input tap's instantaneous zero-dwell touch.
        _run_adb(
            ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(dwell)],
            serial=serial, environ=environ, runner=runner,
        )
    else:
        _run_adb(["shell", "input", "tap", str(x), str(y)], serial=serial, environ=environ, runner=runner)
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

    require_ghost_capability("ghost.input.tap", target="ghost", environ=environ, caller=caller)
    serial = _resolve_adb_serial_or_raise("ghost", environ=environ)
    body = (text or "")[:MAX_TEXT_LEN]
    if humanize and body:
        r = _rng_for(rng)
        slp = sleep if sleep is not None else time.sleep
        for i, ch in enumerate(body):
            _run_adb(["shell", _input_text_command(ch)], serial=serial, environ=environ, runner=runner)
            if i < len(body) - 1:
                slp(r.uniform(*_TYPE_DELAY_S))
    else:
        _run_adb(["shell", _input_text_command(body)], serial=serial, environ=environ, runner=runner)
    return {"length": len(body), "humanized": humanize}


def _plain_swipe(x1, y1, x2, y2, dur, *, serial, environ, runner) -> None:
    _run_adb(
        ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(dur)],
        serial=serial, environ=environ, runner=runner,
    )


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

    require_ghost_capability("ghost.input.tap", target="ghost", environ=environ, caller=caller)
    serial = _resolve_adb_serial_or_raise("ghost", environ=environ)
    width, height = ghost_device_size(serial=serial, environ=environ, runner=runner)
    x1, y1 = _to_device_pixel(x1_norm, width), _to_device_pixel(y1_norm, height)
    x2, y2 = _to_device_pixel(x2_norm, width), _to_device_pixel(y2_norm, height)
    dur = max(1, min(10_000, int(duration_ms)))

    if not humanize:
        _plain_swipe(x1, y1, x2, y2, dur, serial=serial, environ=environ, runner=runner)
        return {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": dur, "humanized": False}

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
            _run_adb(["shell", "input", "motionevent", "UP", str(x2), str(y2)],
                     serial=serial, environ=environ, runner=runner)
        except Exception:
            pass
        _plain_swipe(x1, y1, x2, y2, hdur, serial=serial, environ=environ, runner=runner)
        return {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": hdur,
                "humanized": True, "fallback": True, "steps": len(path)}

    try:
        px0, py0 = path[0]
        down = _run_adb(["shell", "input", "motionevent", "DOWN", str(px0), str(py0)],
                        serial=serial, environ=environ, runner=runner)
        # `input motionevent` needs Android 11+; a non-zero DOWN means the device
        # lacks it (run_adb returns ok=False without raising) -> fall back.
        if not down.ok:
            return _fallback()
        for mx, my in path[1:]:
            _run_adb(["shell", "input", "motionevent", "MOVE", str(mx), str(my)],
                     serial=serial, environ=environ, runner=runner)
            slp(per_move_s * r.uniform(0.6, 1.4))  # irregular inter-move timing
        pxN, pyN = path[-1]
        _run_adb(["shell", "input", "motionevent", "UP", str(pxN), str(pyN)],
                 serial=serial, environ=environ, runner=runner)
    except Exception:
        return _fallback()
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": hdur,
            "humanized": True, "steps": len(path)}


def ghost_keyevent(
    code: int,
    *,
    environ: dict[str, str] | None = None,
    runner: Any = None,
    caller: str = "ghost.input.key",
) -> dict[str, int]:
    """Send an Android keyevent (e.g. 4 = BACK, 3 = HOME, 66 = ENTER). The code
    is validated to a bounded int range — never a free-form string."""

    require_ghost_capability("ghost.input.tap", target="ghost", environ=environ, caller=caller)
    try:
        keycode = int(code)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"keyevent code must be an integer, got {code!r}") from exc
    if not 0 <= keycode <= MAX_KEYCODE:
        raise ValueError(f"keyevent code {keycode} out of range [0, {MAX_KEYCODE}]")
    serial = _resolve_adb_serial_or_raise("ghost", environ=environ)
    _run_adb(["shell", "input", "keyevent", str(keycode)], serial=serial, environ=environ, runner=runner)
    return {"keycode": keycode}


# ── App surface (launch / install) ───────────────────────────────────────────


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

    require_ghost_capability("ghost.app.launch", target="ghost", environ=environ, caller=caller)
    pkg = (package or "").strip()
    if not _PACKAGE_RE.match(pkg):
        raise ValueError(f"invalid Android package name: {package!r}")
    serial = _resolve_adb_serial_or_raise("ghost", environ=environ)
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

    require_ghost_capability("ghost.app.install", target="ghost", environ=environ, caller=caller)
    raw = (apk_path or "").strip()
    if not raw:
        raise ValueError("apk_path is required")
    path = Path(raw)
    if path.suffix.lower() != ".apk":
        raise ValueError(f"not an .apk file: {raw!r}")
    if not path.is_file():
        raise ValueError(f"APK not found: {raw!r}")
    serial = _resolve_adb_serial_or_raise("ghost", environ=environ)
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
