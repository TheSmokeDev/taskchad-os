"""Ghost Phone P4.1 device-operation slice — screen capture (B2).

Proves the takeover's screen power: structurally ghost-only (the capability
seam refuses target != 'ghost' BEFORE the gate), serial-scoped to the ghost's
OWN device, binary-safe (PNG bytes survive), and refused when its env
kill-switch is off.
"""

from __future__ import annotations

import io
import json
import random
import struct
import subprocess
import sys
import threading
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import ghost_capabilities as gc  # type: ignore[import-not-found]  # noqa: E402
import ghost_device as gd  # type: ignore[import-not-found]  # noqa: E402

# adb binary resolved from env so the tests never touch the real SDK path.
_GHOST_ENV = {
    "HOMIE_GHOST_ADB_SERIAL": "emulator-5554",
    "HOMIE_GHOST_AVD": "homie_pixel",
    "HOMIE_ADB_BIN": "adb",
}
_SPARE_ENV = {
    "HOMIE_GHOST_ADB_SERIAL": "192.168.0.222:5555",
    "HOMIE_GHOST_SPARE_HARDWARE_ID": "spare-hardware-123",
    "HOMIE_ADB_BIN": "adb",
}


def _fake_png(width: int, height: int, *, tail: bytes = b"") -> bytes:
    """Generate a structurally genuine RGB PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    signature = bytes((137, 80, 78, 71, 13, 10, 26, 10))
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", tail) + chunk(b"IEND", b"")


def _runner_returning(png: bytes, *, env: dict[str, str] | None = None):
    """Verified AVD identity runner + an injected binary Popen seam."""
    runner, calls = _identity_runner(env or _GHOST_ENV, qemu="1", avd_name="homie_pixel")
    process = _FakePopen(stdout=io.BytesIO(png))
    return runner, calls, process


def _identity_runner(
    env: dict[str, str],
    *,
    qemu: str,
    avd_name: str = "",
    hardware_id: str = "spare-hardware-123",
    mutation_output: str = "",
):
    """Fake the configured transport + the device identity properties."""
    calls: list[list[str]] = []
    serial = env["HOMIE_GHOST_ADB_SERIAL"]

    def runner(argv, **_kwargs):
        calls.append(list(argv))
        if "devices" in argv:
            out = f"List of devices attached\n{serial} device product:test model:test\n"
        elif "sys.boot_completed" in argv:
            out = "1"
        elif "ro.kernel.qemu" in argv:
            out = qemu
        elif "ro.boot.qemu.avd_name" in argv:
            out = avd_name
        elif "ro.serialno" in argv:
            out = hardware_id
        elif "wm" in argv and "size" in argv:
            out = "Physical size: 1080x2400"
        else:
            out = mutation_output
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    return runner, calls


class _GeneratedStream:
    """Generate a logical byte stream without allocating it all at once."""

    def __init__(self, total: int, byte: bytes = b"x") -> None:
        self.remaining = total
        self.byte = byte
        self.delivered = 0

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        amount = self.remaining if size < 0 else min(size, self.remaining)
        self.remaining -= amount
        self.delivered += amount
        return self.byte * amount

    def close(self) -> None:
        self.remaining = 0


class _BlockingStream:
    def __init__(self) -> None:
        self.released = threading.Event()

    def read(self, _size: int = -1) -> bytes:
        self.released.wait(2)
        return b""

    def close(self) -> None:
        self.released.set()


class _FakePopen:
    def __init__(
        self,
        *,
        stdout,
        stderr=None,
        returncode: int = 0,
        running: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr if stderr is not None else io.BytesIO()
        self.returncode = None if running else returncode
        self.final_returncode = returncode
        self.running = running
        self.terminated = False
        self.killed = False
        self.wait_calls = 0
        self.argv: list[str] = []
        self.kwargs: dict = {}

    def factory(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs
        return self

    def poll(self):
        return None if self.running else self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.running = False
        self.returncode = self.final_returncode
        for stream in (self.stdout, self.stderr):
            release = getattr(stream, "released", None)
            if release is not None:
                release.set()

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.running:
            raise subprocess.TimeoutExpired(self.argv or ["adb"], timeout)
        return self.returncode


def test_png_dimensions_parses_ihdr() -> None:
    assert gd._png_dimensions(_fake_png(1080, 2400)) == (1080, 2400)


def test_png_dimensions_rejects_non_png() -> None:
    with pytest.raises(ValueError, match="PNG"):
        gd._png_dimensions(b"not a png at all, definitely not")


def test_ghost_screencap_returns_bytes_and_dims_and_scopes_serial() -> None:
    png = _fake_png(1080, 2400)
    runner, _calls, process = _runner_returning(png)

    out, width, height = gd.ghost_screencap(
        environ=_GHOST_ENV, runner=runner, popen_factory=process.factory
    )

    assert out == png  # bytes survive untouched (binary-safe)
    assert (width, height) == (1080, 2400)
    # scoped to the ghost's OWN serial, never single-device autodetect
    argv = process.argv
    assert "-s" in argv and "emulator-5554" in argv
    assert argv[argv.index("-s") + 1] == "emulator-5554"
    assert "screencap" in argv


def test_ghost_screencap_refused_when_capability_off() -> None:
    env = dict(_GHOST_ENV, HOMIE_GHOST_CAP_SCREEN_VIEW="false")
    runner, calls, process = _runner_returning(_fake_png(1080, 2400))

    with pytest.raises(gc.GhostCapabilityDenied, match="disabled"):
        gd.ghost_screencap(environ=env, runner=runner, popen_factory=process.factory)
    assert calls == []  # refused BEFORE any adb call
    assert process.argv == []


def test_ghost_screencap_raises_without_ghost_serial() -> None:
    runner, calls, process = _runner_returning(_fake_png(1080, 2400))
    with pytest.raises(RuntimeError, match="HOMIE_GHOST_ADB_SERIAL"):
        gd.ghost_screencap(
            environ={"HOMIE_ADB_BIN": "adb"},
            runner=runner,
            popen_factory=process.factory,
        )
    assert calls == []
    assert process.argv == []


def test_ghost_screencap_avd_mode_refuses_physical_serial() -> None:
    env = {
        "HOMIE_GHOST_ADB_SERIAL": "R5CX12ABCDE",
        "HOMIE_GHOST_AVD": "homie_pixel",
        "HOMIE_ADB_BIN": "adb",
    }
    runner, calls, process = _runner_returning(_fake_png(1080, 2400), env=env)
    with pytest.raises(RuntimeError, match="AVD mode requires.*emulator serial"):
        gd.ghost_screencap(environ=env, runner=runner, popen_factory=process.factory)
    assert calls == []
    assert process.argv == []


# ── B3


def _text_runner(*, size: str = "Physical size: 1080x2400"):
    """A runner that answers `wm size` and records every adb argv (text mode)."""
    calls: list[list[str]] = []

    def runner(argv, *, capture_output=False, timeout=None, **_k):
        calls.append(argv)
        if "devices" in argv:
            out = "List of devices attached\nemulator-5554 device product:test model:test\n"
        elif "sys.boot_completed" in argv:
            out = "1"
        elif "ro.kernel.qemu" in argv:
            out = "1"
        elif "ro.boot.qemu.avd_name" in argv:
            out = "homie_pixel"
        else:
            out = size if ("wm" in argv and "size" in argv) else ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    return runner, calls


def test_to_device_pixel_scales_valid_coordinates() -> None:
    assert gd._to_device_pixel(0.0, 1080) == 0
    assert gd._to_device_pixel(0.5, 1080) == 540
    assert gd._to_device_pixel(1.0, 1080) == 1079


def test_ghost_device_size_prefers_override() -> None:
    runner, _ = _text_runner(size="Physical size: 1080x2400\nOverride size: 1080x2000")
    assert gd.ghost_device_size(serial="emulator-5554", environ=_GHOST_ENV, runner=runner) == (
        1080,
        2000,
    )


def test_ghost_tap_scales_normalized_to_device_pixels() -> None:
    runner, calls = _text_runner()
    # humanize=False -> exact input tap, proves the scaling math deterministically.
    out = gd.ghost_tap(0.5, 0.25, humanize=False, environ=_GHOST_ENV, runner=runner)
    assert out == {"x": 540, "y": 600, "width": 1080, "height": 2400, "humanized": False}
    tap = [c for c in calls if "tap" in c][0]
    assert tap[-3:] == ["tap", "540", "600"]
    assert "-s" in tap and "emulator-5554" in tap  # ghost serial, never the phone


def test_ghost_tap_refused_for_non_ghost_is_structural() -> None:
    # The seam is called with target="ghost" internally, so a tap can never be
    # aimed at the phone from here — but prove the capability guard fires when
    # the input gate is explicitly killed.
    env = dict(_GHOST_ENV, HOMIE_GHOST_CAP_INPUT_TAP="false")
    runner, calls = _text_runner()
    with pytest.raises(gc.GhostCapabilityDenied, match="disabled"):
        gd.ghost_tap(0.5, 0.5, environ=env, runner=runner)
    assert calls == []  # refused before wm size or input


def test_ghost_text_escapes_spaces_and_caps_length() -> None:
    runner, calls = _text_runner()
    # humanize=False -> whole string in one command; spaces -> %s in a quoted arg.
    out = gd.ghost_text("hello world", humanize=False, environ=_GHOST_ENV, runner=runner)
    assert out == {"length": len("hello world"), "humanized": False}
    text_call = [c for c in calls if any("input text" in str(a) for a in c)][0]
    assert text_call[-1] == "input text hello%sworld"

    long = "a" * 999
    out2 = gd.ghost_text(long, humanize=False, environ=_GHOST_ENV, runner=runner)
    assert out2["length"] == gd.MAX_TEXT_LEN


def test_ghost_text_neutralizes_shell_injection() -> None:
    """Adversarial-review HIGH (2026-07-07): `adb shell input text x;reboot`
    would run `reboot` on the ghost. The text must reach the device shell as a
    single LITERAL argument — no metacharacter can break out to a command. Holds
    on BOTH the whole-string and the per-keystroke (humanized) paths."""
    runner, calls = _text_runner()
    gd.ghost_text("x;reboot", humanize=False, environ=_GHOST_ENV, runner=runner)
    cmd = [c for c in calls if any("input text" in str(a) for a in c)][0][-1]
    assert cmd == "input text 'x;reboot'"

    # ${IFS}/backtick/pipe/newline payloads are all quoted, never bare.
    for payload in ("a`id`", "a|nc 1.2.3.4", "a${IFS}b", "a\nrm -rf /", "a&&pm clear"):
        calls.clear()
        gd.ghost_text(payload, humanize=False, environ=_GHOST_ENV, runner=runner)
        sent = [c for c in calls if any("input text" in str(a) for a in c)][0][-1]
        assert sent.startswith("input text '") and sent.endswith("'")

    # Humanized (per-keystroke) path quotes each dangerous char too.
    calls.clear()
    gd.ghost_text(
        ";$`", rng=random.Random(1), sleep=lambda _s: None, environ=_GHOST_ENV, runner=runner
    )
    per_char = [c[-1] for c in calls if any("input text" in str(a) for a in c)]
    assert per_char == ["input text ';'", "input text '$'", "input text '`'"]


def test_ghost_text_humanized_types_char_by_char_with_delays() -> None:
    delays: list[float] = []
    runner, calls = _text_runner()
    out = gd.ghost_text(
        "hi ok",
        rng=random.Random(0),
        sleep=lambda s: delays.append(s),
        environ=_GHOST_ENV,
        runner=runner,
    )
    assert out == {"length": 5, "humanized": True}
    text_calls = [c for c in calls if any("input text" in str(a) for a in c)]
    assert len(text_calls) == 5  # one adb call per character
    assert text_calls[2][-1] == "input text %s"  # the space -> %s
    assert len(delays) == 4  # a pause between each pair
    assert all(0.03 <= d <= 0.18 for d in delays)  # human inter-key cadence


def test_ghost_screencap_refuses_serial_collision_with_phone() -> None:
    """Adversarial-review LOW (2026-07-07): if a config typo makes the ghost
    serial equal the personal phone's, a ghost power would drive the real phone.
    Refuse rather than let the misconfig bypass the structural invariant."""
    env = {
        "HOMIE_GHOST_ADB_SERIAL": "R5CX12ABCDE",
        "HOMIE_PHONE_ADB_SERIAL": "R5CX12ABCDE",  # same device — misconfig
        "HOMIE_ADB_BIN": "adb",
    }
    runner, calls, process = _runner_returning(_fake_png(1080, 2400))
    with pytest.raises(RuntimeError, match="equals HOMIE_PHONE_ADB_SERIAL"):
        gd.ghost_screencap(environ=env, runner=runner, popen_factory=process.factory)
    assert calls == []  # refused before any adb call
    assert process.argv == []


def test_ghost_swipe_scales_both_endpoints() -> None:
    runner, calls = _text_runner()
    # humanize=False -> exact input swipe, proves endpoint scaling deterministically.
    out = gd.ghost_swipe(
        0.0, 0.0, 1.0, 1.0, duration_ms=250, humanize=False, environ=_GHOST_ENV, runner=runner
    )
    assert out == {"x1": 0, "y1": 0, "x2": 1079, "y2": 2399, "duration_ms": 250, "humanized": False}
    swipe = [c for c in calls if "swipe" in c][0]
    assert swipe[-6:] == ["swipe", "0", "0", "1079", "2399", "250"]


# ── Humanized input shape (2026-07-07) ───────────────────────────────────────


def test_ghost_tap_humanized_jitters_and_dwells() -> None:
    """Default tap is a short DWELL touch (input swipe) landing NEAR the target,
    not input tap's pixel-perfect zero-dwell touch."""
    runner, calls = _text_runner()
    out = gd.ghost_tap(0.5, 0.25, rng=random.Random(0), environ=_GHOST_ENV, runner=runner)
    assert out["humanized"] is True
    assert out["x"] == 540 and out["y"] == 600  # reported NOMINAL target
    # No plain `input tap`; a short swipe (DOWN..dwell..UP with micro-drift).
    assert not any("tap" in c for c in calls)
    swipe = [c for c in calls if "swipe" in c][0]
    x1, y1, x2, y2, dwell = (int(v) for v in swipe[-5:])
    assert abs(x1 - 540) <= 9 and abs(y1 - 600) <= 9  # within the jitter radius
    assert (x1, y1) != (540, 600) or (x2, y2) != (540, 600)  # actually jittered
    assert 45 <= dwell <= 130  # human press dwell
    assert 0 <= x1 < 1080 and 0 <= y1 < 2400  # on-screen


def test_ghost_swipe_humanized_is_curved_variable_velocity() -> None:
    """Default swipe is a motionevent DOWN/MOVE…/UP sequence tracing a curved,
    jittered path — never a single straight constant-velocity input swipe."""
    runner, calls = _text_runner()
    out = gd.ghost_swipe(
        0.5,
        0.8,
        0.5,
        0.2,
        duration_ms=300,
        rng=random.Random(0),
        sleep=lambda _s: None,
        environ=_GHOST_ENV,
        runner=runner,
    )
    assert out["humanized"] is True and out.get("fallback") is None
    motion = [c for c in calls if "motionevent" in c]
    kinds = [c[c.index("motionevent") + 1] for c in motion]
    assert kinds[0] == "DOWN" and kinds[-1] == "UP"
    assert kinds.count("MOVE") >= 9  # many intermediate points
    assert not any("swipe" in c for c in calls)  # not the plain path
    # A dead-straight input swipe would hold x=540 the whole way; the bezier bow
    # + jitter make the x-track deviate (curved, not a constant-velocity line).
    xs = [int(c[-2]) for c in motion]
    assert len(set(xs)) > 1


def test_ghost_swipe_falls_back_when_motionevent_unsupported() -> None:
    """Older devices lack `input motionevent`: on failure the gesture releases
    (best-effort UP) and completes via a plain swipe — never a stuck finger."""
    calls: list[list[str]] = []

    def runner(argv, *, capture_output=False, timeout=None, **_k):
        calls.append(argv)
        if "devices" in argv:
            out = "List of devices attached\nemulator-5554 device product:test model:test\n"
        elif "sys.boot_completed" in argv:
            out = "1"
        elif "ro.kernel.qemu" in argv:
            out = "1"
        elif "ro.boot.qemu.avd_name" in argv:
            out = "homie_pixel"
        else:
            out = "Physical size: 1080x2400" if ("wm" in argv and "size" in argv) else ""
        if "motionevent" in argv and "DOWN" in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr="Unknown command: motionevent")
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    out = gd.ghost_swipe(
        0.0,
        0.0,
        1.0,
        1.0,
        rng=random.Random(0),
        sleep=lambda _s: None,
        environ=_GHOST_ENV,
        runner=runner,
    )
    assert out["humanized"] is True and out["fallback"] is True
    assert any("swipe" in c for c in calls)  # completed via plain swipe
    assert any("motionevent" in c and "UP" in c for c in calls)  # released first


def test_ghost_keyevent_validates_range() -> None:
    runner, calls = _text_runner()
    assert gd.ghost_keyevent(4, environ=_GHOST_ENV, runner=runner) == {"keycode": 4}
    key = [c for c in calls if "keyevent" in c][0]
    assert key[-2:] == ["keyevent", "4"]

    with pytest.raises(ValueError, match="out of range"):
        gd.ghost_keyevent(9999, environ=_GHOST_ENV, runner=runner)


# ── B4 — app launch / install ────────────────────────────────────────────────


def _ok_runner(output: str = ""):
    calls: list[list[str]] = []

    def runner(argv, *, capture_output=False, timeout=None, **_k):
        calls.append(argv)
        if "devices" in argv:
            out = "List of devices attached\nemulator-5554 device product:test model:test\n"
        elif "sys.boot_completed" in argv:
            out = "1"
        elif "ro.kernel.qemu" in argv:
            out = "1"
        elif "ro.boot.qemu.avd_name" in argv:
            out = "homie_pixel"
        else:
            out = output
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    return runner, calls


def test_ghost_app_launch_validates_package_and_shells_monkey() -> None:
    runner, calls = _ok_runner("Events injected: 1")
    out = gd.ghost_app_launch("com.android.chrome", environ=_GHOST_ENV, runner=runner)
    assert out == {"package": "com.android.chrome"}
    argv = next(call for call in calls if "monkey" in call)
    assert argv[-6:] == [
        "monkey",
        "-p",
        "com.android.chrome",
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
    ]
    assert "-s" in argv and "emulator-5554" in argv


@pytest.mark.parametrize(
    "bad",
    ["", "no spaces here", "com.evil; rm -rf /", "-p", "com..bad", "0startsdigit"],
)
def test_ghost_app_launch_rejects_bad_package(bad: str) -> None:
    runner, calls = _ok_runner()
    with pytest.raises(ValueError, match="invalid Android package"):
        gd.ghost_app_launch(bad, environ=_GHOST_ENV, runner=runner)
    assert calls == []  # never shells on a bad package


def test_ghost_app_launch_raises_when_not_installed() -> None:
    runner, _ = _ok_runner("** No activities found to run, monkey aborted.")
    with pytest.raises(RuntimeError, match="no launchable activity"):
        gd.ghost_app_launch("com.absent.app", environ=_GHOST_ENV, runner=runner)


def test_ghost_app_launch_refused_when_capability_off() -> None:
    env = dict(_GHOST_ENV, HOMIE_GHOST_CAP_APP_LAUNCH="false")
    runner, calls = _ok_runner("Events injected: 1")
    with pytest.raises(gc.GhostCapabilityDenied, match="disabled"):
        gd.ghost_app_launch("com.android.chrome", environ=env, runner=runner)
    assert calls == []


def test_ghost_app_install_validates_apk_and_reports_success(tmp_path) -> None:
    apk = tmp_path / "test.apk"
    apk.write_bytes(b"PK\x03\x04fake-apk")
    runner, calls = _ok_runner("Success")
    out = gd.ghost_app_install(str(apk), environ=_GHOST_ENV, runner=runner)
    assert out == {"apk": "test.apk"}
    argv = next(call for call in calls if "install" in call)
    assert argv[-2] == "install"
    assert argv[-1] == str(apk)
    assert "-s" in argv and "emulator-5554" in argv


def test_ghost_app_install_rejects_non_apk(tmp_path) -> None:
    txt = tmp_path / "notes.txt"
    txt.write_text("hi")
    runner, calls = _ok_runner("Success")
    with pytest.raises(ValueError, match="not an .apk"):
        gd.ghost_app_install(str(txt), environ=_GHOST_ENV, runner=runner)
    assert calls == []


def test_ghost_app_install_rejects_missing_file(tmp_path) -> None:
    runner, calls = _ok_runner("Success")
    with pytest.raises(ValueError, match="APK not found"):
        gd.ghost_app_install(str(tmp_path / "gone.apk"), environ=_GHOST_ENV, runner=runner)
    assert calls == []


def test_ghost_app_install_raises_on_adb_failure(tmp_path) -> None:
    apk = tmp_path / "test.apk"
    apk.write_bytes(b"PK\x03\x04fake-apk")
    runner, _ = _ok_runner("Failure [INSTALL_FAILED_INVALID_APK]")
    with pytest.raises(RuntimeError, match="install failed"):
        gd.ghost_app_install(str(apk), environ=_GHOST_ENV, runner=runner)


# Phase B1 fail-closed hardening.
def test_ghost_screencap_rejects_oversized_png() -> None:
    runner, _, process = _runner_returning(_fake_png(1080, 2400, tail=b"x" * (10 * 1024 * 1024)))
    with pytest.raises(RuntimeError, match="10 MiB"):
        gd.ghost_screencap(environ=_GHOST_ENV, runner=runner, popen_factory=process.factory)


def test_png_dimensions_rejects_malformed_ihdr() -> None:
    malformed = _fake_png(1080, 2400)
    malformed = malformed[:8] + b"\x00\x00\x00\x0c" + malformed[12:]
    with pytest.raises(ValueError, match="CRC|IHDR"):
        gd._png_dimensions(malformed)


@pytest.mark.parametrize("value", [-0.01, 1.01, float("nan"), float("inf"), -float("inf")])
def test_ghost_tap_rejects_out_of_range_or_nonfinite_coordinates(value: float) -> None:
    runner, calls = _text_runner()
    with pytest.raises(ValueError, match=r"finite.*\[0, 1\]"):
        gd.ghost_tap(value, 0.5, humanize=False, environ=_GHOST_ENV, runner=runner)
    assert calls == []


def test_ghost_tap_surfaces_adb_error() -> None:
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        if "devices" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout="List of devices attached\nemulator-5554 device product:test model:test\n",
                stderr="",
            )
        if "sys.boot_completed" in argv:
            return SimpleNamespace(returncode=0, stdout="1", stderr="")
        if "ro.kernel.qemu" in argv:
            return SimpleNamespace(returncode=0, stdout="1", stderr="")
        if "ro.boot.qemu.avd_name" in argv:
            return SimpleNamespace(returncode=0, stdout="homie_pixel", stderr="")
        if "wm" in argv:
            return SimpleNamespace(returncode=0, stdout="Physical size: 1080x2400", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="device offline")

    with pytest.raises(RuntimeError, match="Ghost device command failed"):
        gd.ghost_tap(0.5, 0.5, humanize=False, environ=_GHOST_ENV, runner=runner)


def test_avd_is_accepted_only_after_transport_and_property_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gc, "_default_audit", lambda **_row: None)
    runner, calls = _identity_runner(
        _GHOST_ENV, qemu="1", avd_name="homie_pixel", mutation_output="Events injected: 1"
    )

    gd.ghost_app_launch("com.android.chrome", environ=_GHOST_ENV, runner=runner)

    devices_at = next(i for i, call in enumerate(calls) if "devices" in call)
    booted_at = next(i for i, call in enumerate(calls) if "sys.boot_completed" in call)
    qemu_at = next(i for i, call in enumerate(calls) if "ro.kernel.qemu" in call)
    avd_at = next(i for i, call in enumerate(calls) if "ro.boot.qemu.avd_name" in call)
    mutation_at = next(i for i, call in enumerate(calls) if "monkey" in call)
    assert devices_at < booted_at < qemu_at < avd_at < mutation_at
    assert all("emulator-5554" in call for call in calls if "-s" in call)


@pytest.mark.parametrize(
    ("qemu", "avd_name"),
    [("0", "homie_pixel"), ("1", "operator_phone")],
)
def test_avd_spoof_or_property_mismatch_is_denied_before_mutation(
    qemu: str,
    avd_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gc, "_default_audit", lambda **_row: None)
    runner, calls = _identity_runner(
        _GHOST_ENV, qemu=qemu, avd_name=avd_name, mutation_output="Events injected: 1"
    )

    with pytest.raises(RuntimeError, match="identity"):
        gd.ghost_app_launch("com.android.chrome", environ=_GHOST_ENV, runner=runner)

    assert not any("monkey" in call for call in calls)


def test_configured_spare_is_accepted_after_physical_identity_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gc, "_default_audit", lambda **_row: None)
    runner, calls = _identity_runner(_SPARE_ENV, qemu="0", mutation_output="Events injected: 1")

    out = gd.ghost_app_launch("com.android.chrome", environ=_SPARE_ENV, runner=runner)

    assert out == {"package": "com.android.chrome"}
    assert any("devices" in call for call in calls)
    assert any("sys.boot_completed" in call for call in calls)
    assert any("ro.kernel.qemu" in call for call in calls)
    assert any("ro.serialno" in call for call in calls)
    mutation = next(call for call in calls if "monkey" in call)
    assert mutation[mutation.index("-s") + 1] == _SPARE_ENV["HOMIE_GHOST_ADB_SERIAL"]


@pytest.mark.parametrize(
    ("env_update", "hardware_id"),
    [
        (None, "spare-hardware-123"),
        ({"HOMIE_GHOST_SPARE_HARDWARE_ID": ""}, "spare-hardware-123"),
        ({"HOMIE_GHOST_SPARE_HARDWARE_ID": "expected-hardware"}, "other-hardware"),
        ({"HOMIE_GHOST_SPARE_HARDWARE_ID": "spare-hardware-123"}, ""),
    ],
)
def test_spare_missing_mismatched_or_blank_hardware_binding_is_denied_before_mutation(
    env_update: dict[str, str] | None,
    hardware_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gc, "_default_audit", lambda **_row: None)
    env = dict(_SPARE_ENV)
    if env_update is None:
        env.pop("HOMIE_GHOST_SPARE_HARDWARE_ID")
    else:
        env.update(env_update)
    runner, calls = _identity_runner(
        env, qemu="0", hardware_id=hardware_id, mutation_output="Events injected: 1"
    )

    with pytest.raises(RuntimeError, match="identity") as exc_info:
        gd.ghost_app_launch("com.android.chrome", environ=env, runner=runner)

    assert not any("monkey" in call for call in calls)
    configured_id = env.get("HOMIE_GHOST_SPARE_HARDWARE_ID", "")
    if configured_id:
        assert configured_id not in str(exc_info.value)
    if hardware_id:
        assert hardware_id not in str(exc_info.value)


def test_personal_device_cannot_pass_spare_identity_using_transport_serial_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gc, "_default_audit", lambda **_row: None)
    transport = "PERSONAL-PHONE-SERIAL"
    env = {
        "HOMIE_GHOST_ADB_SERIAL": transport,
        "HOMIE_GHOST_SPARE_HARDWARE_ID": transport,
        "HOMIE_ADB_BIN": "adb",
    }
    runner, calls = _identity_runner(
        env,
        qemu="0",
        hardware_id="actual-personal-hardware-id",
        mutation_output="Events injected: 1",
    )

    with pytest.raises(RuntimeError, match="identity"):
        gd.ghost_app_launch("com.android.chrome", environ=env, runner=runner)

    assert not any("monkey" in call for call in calls)


@pytest.mark.parametrize(
    "timeout",
    [True, False, 0, -1, float("nan"), float("inf"), -float("inf"), "1"],
)
def test_screencap_rejects_non_native_nonfinite_or_nonpositive_timeout_before_popen(
    timeout: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gc, "_default_audit", lambda **_row: None)
    runner, _calls, process = _runner_returning(_fake_png(1080, 2400))

    with pytest.raises(ValueError, match="finite positive native"):
        gd.ghost_screencap(
            environ=_GHOST_ENV,
            runner=runner,
            popen_factory=process.factory,
            timeout_seconds=timeout,  # type: ignore[arg-type]
        )

    assert process.argv == []


def test_screencap_stream_overflow_terminates_kills_and_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gc, "_default_audit", lambda **_row: None)
    runner, _ = _identity_runner(_GHOST_ENV, qemu="1", avd_name="homie_pixel")
    stdout = _GeneratedStream(gd.MAX_SCREENCAP_BYTES + 1024)
    process = _FakePopen(stdout=stdout, running=True)

    with pytest.raises(RuntimeError, match="10 MiB"):
        gd.ghost_screencap(
            environ=_GHOST_ENV,
            runner=runner,
            popen_factory=process.factory,
            timeout_seconds=1,
        )

    assert stdout.delivered == gd.MAX_SCREENCAP_BYTES + 1
    assert process.terminated is True
    assert process.killed is True
    assert process.wait_calls >= 2


def test_screencap_timeout_terminates_kills_and_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gc, "_default_audit", lambda **_row: None)
    runner, _ = _identity_runner(_GHOST_ENV, qemu="1", avd_name="homie_pixel")
    process = _FakePopen(stdout=_BlockingStream(), running=True)

    with pytest.raises(TimeoutError, match="timed out"):
        gd.ghost_screencap(
            environ=_GHOST_ENV,
            runner=runner,
            popen_factory=process.factory,
            timeout_seconds=0.02,
        )

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_calls >= 2


def test_screencap_nonzero_exit_has_bounded_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gc, "_default_audit", lambda **_row: None)
    runner, _ = _identity_runner(_GHOST_ENV, qemu="1", avd_name="homie_pixel")
    process = _FakePopen(
        stdout=io.BytesIO(),
        stderr=_GeneratedStream(2 * 1024 * 1024, b"e"),
        returncode=7,
    )

    with pytest.raises(RuntimeError) as exc_info:
        gd.ghost_screencap(
            environ=_GHOST_ENV,
            runner=runner,
            popen_factory=process.factory,
            timeout_seconds=1,
        )

    message = str(exc_info.value)
    assert "exit 7" in message
    assert "stderr truncated" in message
    assert len(message.encode()) <= gd.MAX_SCREENCAP_STDERR_BYTES + 128
    assert process.stderr.delivered == 2 * 1024 * 1024


def test_direct_capability_success_writes_one_terminal_success_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict] = []
    monkeypatch.setattr(gc, "_default_audit", lambda **row: audits.append(row))
    runner, _ = _identity_runner(
        _GHOST_ENV, qemu="1", avd_name="homie_pixel", mutation_output="Events injected: 1"
    )

    gd.ghost_app_launch("com.android.chrome", environ=_GHOST_ENV, runner=runner)

    assert [row["outcome"] for row in audits] == ["succeeded"]
    assert audits[0]["capability"] == "ghost.app.launch"


def test_direct_validation_failure_writes_one_redacted_terminal_failure_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import browser_audit  # type: ignore[import-not-found]

    audit_path = tmp_path / "ghost-audit.jsonl"
    monkeypatch.setattr(browser_audit, "BROWSER_AUDIT_LOG", audit_path)
    sensitive = "https://example.com/path?token=secret#frag"

    with pytest.raises(ValueError, match="invalid Android package"):
        gd.ghost_app_launch(sensitive, environ=_GHOST_ENV, runner=lambda *_a, **_k: None)

    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert [row["outcome"] for row in rows] == ["failed"]
    assert rows[0]["action"] == "ghost.app.launch"
    assert "token=secret" not in rows[0]["reason"]
    assert "#frag" not in rows[0]["reason"]
