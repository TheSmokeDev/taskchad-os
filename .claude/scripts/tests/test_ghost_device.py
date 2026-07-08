"""Ghost Phone P4.1 device-operation slice — screen capture (B2).

Proves the takeover's screen power: structurally ghost-only (the capability
seam refuses target != 'ghost' BEFORE the gate), serial-scoped to the ghost's
OWN device, binary-safe (PNG bytes survive), and refused when its env
kill-switch is off.
"""

from __future__ import annotations

import random
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import ghost_device as gd  # type: ignore[import-not-found]  # noqa: E402
import ghost_capabilities as gc  # type: ignore[import-not-found]  # noqa: E402

# adb binary resolved from env so the tests never touch the real SDK path.
_GHOST_ENV = {"HOMIE_GHOST_ADB_SERIAL": "emulator-5554", "HOMIE_ADB_BIN": "adb"}


def _fake_png(width: int, height: int, *, tail: bytes = b"rest-of-image") -> bytes:
    """A byte string with a valid PNG signature + IHDR carrying (width, height)."""
    return (
        b"\x89PNG\r\n\x1a\n"          # signature
        + b"\x00\x00\x00\x0d"          # IHDR length (13)
        + b"IHDR"                       # chunk type
        + struct.pack(">II", width, height)
        + tail
    )


def _runner_returning(png: bytes):
    """A subprocess.run stand-in: records argv, returns PNG bytes on stdout."""
    calls: list[list[str]] = []

    def runner(argv, *, capture_output=False, timeout=None, **_k):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=png, stderr=b"")

    return runner, calls


def test_png_dimensions_parses_ihdr() -> None:
    assert gd._png_dimensions(_fake_png(1080, 2400)) == (1080, 2400)


def test_png_dimensions_rejects_non_png() -> None:
    with pytest.raises(ValueError, match="PNG"):
        gd._png_dimensions(b"not a png at all, definitely not")


def test_ghost_screencap_returns_bytes_and_dims_and_scopes_serial() -> None:
    png = _fake_png(1080, 2400)
    runner, calls = _runner_returning(png)

    out, width, height = gd.ghost_screencap(environ=_GHOST_ENV, runner=runner)

    assert out == png                      # bytes survive untouched (binary-safe)
    assert (width, height) == (1080, 2400)
    # scoped to the ghost's OWN serial, never single-device autodetect
    argv = calls[0]
    assert "-s" in argv and "emulator-5554" in argv
    assert argv[argv.index("-s") + 1] == "emulator-5554"
    assert "screencap" in argv


def test_ghost_screencap_refused_when_capability_off() -> None:
    env = dict(_GHOST_ENV, HOMIE_GHOST_CAP_SCREEN_VIEW="false")
    runner, calls = _runner_returning(_fake_png(1080, 2400))

    with pytest.raises(gc.GhostCapabilityDenied, match="disabled"):
        gd.ghost_screencap(environ=env, runner=runner)
    assert calls == []  # refused BEFORE any adb call


def test_ghost_screencap_raises_without_ghost_serial() -> None:
    runner, calls = _runner_returning(_fake_png(1080, 2400))
    with pytest.raises(RuntimeError, match="HOMIE_GHOST_ADB_SERIAL"):
        gd.ghost_screencap(environ={"HOMIE_ADB_BIN": "adb"}, runner=runner)
    assert calls == []


# ── B3 — input surface (tap / type / swipe / key) ────────────────────────────


def _text_runner(*, size: str = "Physical size: 1080x2400"):
    """A runner that answers `wm size` and records every adb argv (text mode)."""
    calls: list[list[str]] = []

    def runner(argv, *, capture_output=False, timeout=None, **_k):
        calls.append(argv)
        out = size if ("wm" in argv and "size" in argv) else ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    return runner, calls


def test_to_device_pixel_scales_and_clamps() -> None:
    assert gd._to_device_pixel(0.0, 1080) == 0
    assert gd._to_device_pixel(0.5, 1080) == 540
    assert gd._to_device_pixel(1.0, 1080) == 1079   # clamped to size-1
    assert gd._to_device_pixel(-0.5, 1080) == 0     # below range clamps low
    assert gd._to_device_pixel(2.0, 2400) == 2399   # above range clamps high


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
    gd.ghost_text(";$`", rng=random.Random(1), sleep=lambda _s: None,
                  environ=_GHOST_ENV, runner=runner)
    per_char = [c[-1] for c in calls if any("input text" in str(a) for a in c)]
    assert per_char == ["input text ';'", "input text '$'", "input text '`'"]


def test_ghost_text_humanized_types_char_by_char_with_delays() -> None:
    delays: list[float] = []
    runner, calls = _text_runner()
    out = gd.ghost_text(
        "hi ok", rng=random.Random(0), sleep=lambda s: delays.append(s),
        environ=_GHOST_ENV, runner=runner,
    )
    assert out == {"length": 5, "humanized": True}
    text_calls = [c for c in calls if any("input text" in str(a) for a in c)]
    assert len(text_calls) == 5                       # one adb call per character
    assert text_calls[2][-1] == "input text %s"       # the space -> %s
    assert len(delays) == 4                            # a pause between each pair
    assert all(0.03 <= d <= 0.18 for d in delays)     # human inter-key cadence


def test_ghost_screencap_refuses_serial_collision_with_phone() -> None:
    """Adversarial-review LOW (2026-07-07): if a config typo makes the ghost
    serial equal the personal phone's, a ghost power would drive the real phone.
    Refuse rather than let the misconfig bypass the structural invariant."""
    env = {
        "HOMIE_GHOST_ADB_SERIAL": "R5CX12ABCDE",
        "HOMIE_PHONE_ADB_SERIAL": "R5CX12ABCDE",  # same device — misconfig
        "HOMIE_ADB_BIN": "adb",
    }
    runner, calls = _runner_returning(_fake_png(1080, 2400))
    with pytest.raises(RuntimeError, match="equals HOMIE_PHONE_ADB_SERIAL"):
        gd.ghost_screencap(environ=env, runner=runner)
    assert calls == []  # refused before any adb call


def test_ghost_swipe_scales_both_endpoints() -> None:
    runner, calls = _text_runner()
    # humanize=False -> exact input swipe, proves endpoint scaling deterministically.
    out = gd.ghost_swipe(0.0, 0.0, 1.0, 1.0, duration_ms=250, humanize=False,
                         environ=_GHOST_ENV, runner=runner)
    assert out == {"x1": 0, "y1": 0, "x2": 1079, "y2": 2399, "duration_ms": 250,
                   "humanized": False}
    swipe = [c for c in calls if "swipe" in c][0]
    assert swipe[-6:] == ["swipe", "0", "0", "1079", "2399", "250"]


# ── Humanized input shape (2026-07-07) ───────────────────────────────────────


def test_ghost_tap_humanized_jitters_and_dwells() -> None:
    """Default tap is a short DWELL touch (input swipe) landing NEAR the target,
    not input tap's pixel-perfect zero-dwell touch."""
    runner, calls = _text_runner()
    out = gd.ghost_tap(0.5, 0.25, rng=random.Random(0), environ=_GHOST_ENV, runner=runner)
    assert out["humanized"] is True
    assert out["x"] == 540 and out["y"] == 600           # reported NOMINAL target
    # No plain `input tap`; a short swipe (DOWN..dwell..UP with micro-drift).
    assert not any("tap" in c for c in calls)
    swipe = [c for c in calls if "swipe" in c][0]
    x1, y1, x2, y2, dwell = (int(v) for v in swipe[-5:])
    assert abs(x1 - 540) <= 9 and abs(y1 - 600) <= 9     # within the jitter radius
    assert (x1, y1) != (540, 600) or (x2, y2) != (540, 600)  # actually jittered
    assert 45 <= dwell <= 130                            # human press dwell
    assert 0 <= x1 < 1080 and 0 <= y1 < 2400             # on-screen


def test_ghost_swipe_humanized_is_curved_variable_velocity() -> None:
    """Default swipe is a motionevent DOWN/MOVE…/UP sequence tracing a curved,
    jittered path — never a single straight constant-velocity input swipe."""
    runner, calls = _text_runner()
    out = gd.ghost_swipe(0.5, 0.8, 0.5, 0.2, duration_ms=300, rng=random.Random(0),
                         sleep=lambda _s: None, environ=_GHOST_ENV, runner=runner)
    assert out["humanized"] is True and out.get("fallback") is None
    motion = [c for c in calls if "motionevent" in c]
    kinds = [c[c.index("motionevent") + 1] for c in motion]
    assert kinds[0] == "DOWN" and kinds[-1] == "UP"
    assert kinds.count("MOVE") >= 9                       # many intermediate points
    assert not any("swipe" in c for c in calls)           # not the plain path
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
        out = "Physical size: 1080x2400" if ("wm" in argv and "size" in argv) else ""
        if "motionevent" in argv and "DOWN" in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr="Unknown command: motionevent")
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    out = gd.ghost_swipe(0.0, 0.0, 1.0, 1.0, rng=random.Random(0), sleep=lambda _s: None,
                         environ=_GHOST_ENV, runner=runner)
    assert out["humanized"] is True and out["fallback"] is True
    assert any("swipe" in c for c in calls)               # completed via plain swipe
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
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    return runner, calls


def test_ghost_app_launch_validates_package_and_shells_monkey() -> None:
    runner, calls = _ok_runner("Events injected: 1")
    out = gd.ghost_app_launch("com.android.chrome", environ=_GHOST_ENV, runner=runner)
    assert out == {"package": "com.android.chrome"}
    argv = calls[0]
    assert argv[-6:] == ["monkey", "-p", "com.android.chrome", "-c",
                         "android.intent.category.LAUNCHER", "1"]
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
    argv = calls[0]
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
