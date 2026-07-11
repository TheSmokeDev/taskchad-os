"""Unit tests for the optional hero-art adapter (video_imagegen).

No real CLI, no network, no image generation. The provider CLI is mocked at
every seam: detection (shutil.which), invocation (subprocess.run), and the
generated-images root (a tmp dir). Covers:
  1. detection-absent path: returns None WITHOUT spawning any subprocess
  2. success path: new PNG discovered, copied into assets, relative path back
  3. stdout-path fallback discovery
  4. timeout / quota-wall (no output) paths: None, never a raise
  5. instruction composition: subject + palette + aspect + no-text rule
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

import video_imagegen  # noqa: E402
import video_styles  # noqa: E402


def _design() -> dict:
    return video_styles.resolve_design(style="bold-poster")


class _FakeStdin:
    """Captures the UTF-8 instruction bytes written to codex's stdin."""

    def __init__(self, captured: dict) -> None:
        self._c = captured

    def write(self, b) -> None:
        self._c["input"] = b.decode("utf-8") if isinstance(b, (bytes, bytearray)) else b

    def close(self) -> None:
        pass


def _install_codex(monkeypatch, tmp_path, *, write_png=True, stdout_text="",
                   exits=True):
    """Wire a fake codex render at the Popen seam (the code no longer uses
    subprocess.run for the render). On construction the fake optionally writes a
    png into a session subdir (the fresh-png-diff path) and/or exposes a stdout
    path line (codex's own reported-path path). Returns the capture dict."""
    images_root = tmp_path / "generated"
    monkeypatch.setattr(video_imagegen.shutil, "which", lambda name: "/fake/bin/tool")
    monkeypatch.setattr(video_imagegen, "_generated_images_root", lambda: images_root)
    monkeypatch.setattr(video_imagegen, "_POLL_INTERVAL_S", 0.01)
    captured: dict = {}
    png_path = images_root / "session-1" / "ig_0001.png"

    class _Popen:
        def __init__(self, cmd, stdin=None, stdout=None, stderr=None, **kw):
            captured["cmd"] = cmd
            self.pid = 4242
            self.stdin = _FakeStdin(captured)
            if write_png:
                png_path.parent.mkdir(parents=True, exist_ok=True)
                png_path.write_bytes(b"\x89PNG fake")
            self.stdout = io.BytesIO(stdout_text.encode("utf-8"))

        def poll(self):
            return 0 if exits else None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(video_imagegen.subprocess, "Popen", _Popen)
    # no-op the taskkill teardown so tests never spawn a real process
    monkeypatch.setattr(video_imagegen.subprocess, "run", lambda *a, **k: None)
    return captured


# =============================================================================
# 1. DETECTION-ABSENT PATH
# =============================================================================


def test_absent_cli_returns_none_without_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(video_imagegen.shutil, "which", lambda name: None)

    def _no_spawn(*args, **kwargs):
        raise AssertionError("subprocess must not run when the CLI is absent")

    monkeypatch.setattr(video_imagegen.subprocess, "run", _no_spawn)
    result = video_imagegen.generate_hero(
        "a stadium at night", _design(), "16:9", str(tmp_path / "assets")
    )
    assert result is None
    assert video_imagegen.cli_available() is False


def test_empty_prompt_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(video_imagegen.shutil, "which", lambda name: "/fake/bin/tool")

    def _no_spawn(*args, **kwargs):
        raise AssertionError("subprocess must not run for an empty prompt")

    monkeypatch.setattr(video_imagegen.subprocess, "run", _no_spawn)
    assert video_imagegen.generate_hero("  ", _design(), "16:9", str(tmp_path)) is None


# =============================================================================
# 2. SUCCESS PATH (mocked generation)
# =============================================================================


def test_success_copies_newest_png_into_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assets_dir = tmp_path / "assets"
    captured = _install_codex(monkeypatch, tmp_path)

    result = video_imagegen.generate_hero(
        "a stadium at night", _design(), "9:16", str(assets_dir)
    )
    assert result == "assets/hero.png"
    assert (assets_dir / "hero.png").is_file()
    # The invocation went through exec with the feature flag, prompt on stdin.
    assert "exec" in captured["cmd"]
    assert "--enable" in captured["cmd"]
    assert "image_generation" in captured["cmd"]
    assert "--skip-git-repo-check" in captured["cmd"]
    assert "a stadium at night" in captured["input"]


def test_stdout_path_fallback_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assets_dir = tmp_path / "assets"
    side_png = tmp_path / "elsewhere.png"  # written by "codex" outside the roots
    side_png.write_bytes(b"\x89PNG fake")
    # No png in the roots; codex reports the path on stdout instead.
    _install_codex(monkeypatch, tmp_path, write_png=False, stdout_text=f"done\n{side_png}\n")

    result = video_imagegen.generate_hero(
        "a stadium at night", _design(), "16:9", str(assets_dir)
    )
    assert result == "assets/hero.png"
    assert (assets_dir / "hero.png").is_file()


# =============================================================================
# 3. FAILURE PATHS (None, never a raise)
# =============================================================================


def test_no_output_within_timeout_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # codex never writes a png and never exits -- the watch loop must give up at
    # the deadline, force-kill the tree, and return None (never hang, never raise).
    monkeypatch.setattr(video_imagegen, "_resolve_timeout", lambda: 1)
    _install_codex(monkeypatch, tmp_path, write_png=False, stdout_text="", exits=False)
    assert (
        video_imagegen.generate_hero("a scene", _design(), "16:9", str(tmp_path / "a"))
        is None
    )


def test_quota_wall_no_output_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The CLI runs and exits but produces no new image and no path on stdout
    # (quota walls and refusals look exactly like this).
    _install_codex(monkeypatch, tmp_path, write_png=False, stdout_text="usage limit reached")
    assert (
        video_imagegen.generate_hero("a scene", _design(), "16:9", str(tmp_path / "a"))
        is None
    )


# =============================================================================
# 4. INSTRUCTION COMPOSITION
# =============================================================================


def test_build_instruction_uses_subject_palette_aspect_and_no_text() -> None:
    design = _design()
    instruction = video_imagegen.build_instruction(
        "Mexico winning the world cup", design, "9:16"
    )
    assert "Mexico winning the world cup" in instruction
    assert design["palette"]["bg"] in instruction
    assert design["palette"]["accent"] in instruction
    assert "9:16" in instruction
    assert "no text" in instruction.lower()
    assert "no logos" in instruction.lower()


def test_build_instruction_defaults_unknown_aspect_to_landscape() -> None:
    instruction = video_imagegen.build_instruction("a scene", _design(), "4:3")
    assert "16:9" in instruction


# =============================================================================
# 5. GENERATE_IMAGE V2: NAMED OUTPUTS + REFERENCE IMAGES (-i pairs)
# =============================================================================


def _wire_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Mock the CLI seams for a successful generation; returns the capture dict."""
    return _install_codex(monkeypatch, tmp_path)


def test_generate_image_named_output_with_refs_cmd_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ref_a = tmp_path / "ref0.png"
    ref_a.write_bytes(b"\x89PNG fake")
    ref_b = tmp_path / "ref1.jpg"
    ref_b.write_bytes(b"\xff\xd8\xff fake")
    captured = _wire_success(monkeypatch, tmp_path)

    result = video_imagegen.generate_image(
        "a stadium at night",
        _design(),
        "16:9",
        str(tmp_path / "assets"),
        name="quote",
        refs=[str(ref_a), str(ref_b)],
    )
    assert result == "assets/quote.png"
    assert (tmp_path / "assets" / "quote.png").is_file()

    cmd = captured["cmd"]
    flag_positions = [i for i, token in enumerate(cmd) if token == "-i"]
    assert len(flag_positions) == 2  # one repeatable -i pair per reference
    assert [cmd[i + 1] for i in flag_positions] == [str(ref_a), str(ref_b)]
    # The identity lock rides on the stdin instruction.
    assert "Match the subject identity" in captured["input"]
    assert "new scene" in captured["input"]
    assert "a stadium at night" in captured["input"]


def test_generate_image_without_refs_has_no_identity_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = _wire_success(monkeypatch, tmp_path)
    result = video_imagegen.generate_image(
        "a stadium at night", _design(), "16:9", str(tmp_path / "assets")
    )
    assert result == "assets/hero.png"  # default name stays hero
    assert "-i" not in captured["cmd"]
    assert "Match the subject identity" not in captured["input"]


def test_generate_image_missing_ref_files_are_filtered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = _wire_success(monkeypatch, tmp_path)
    video_imagegen.generate_image(
        "a scene",
        _design(),
        "16:9",
        str(tmp_path / "assets"),
        refs=[str(tmp_path / "nope.png"), ""],
    )
    assert "-i" not in captured["cmd"]
    assert "Match the subject identity" not in captured["input"]


def test_generate_hero_is_thin_wrapper_over_generate_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    def fake_generate_image(prompt, design, aspect, assets_dir, *, name="hero", refs=None):
        seen.update(prompt=prompt, name=name, refs=refs)
        return "assets/hero.png"

    monkeypatch.setattr(video_imagegen, "generate_image", fake_generate_image)
    assert (
        video_imagegen.generate_hero("a scene", _design(), "16:9", "assets")
        == "assets/hero.png"
    )
    assert seen["name"] == "hero"
    assert seen["refs"] is None


# =============================================================================
# 6. GENERATE_ART_PLAN (priority, budget, env, skip-on-fail)
# =============================================================================


def _beat(kind: str, headline: str = "A headline") -> SimpleNamespace:
    return SimpleNamespace(
        kind=kind, headline=headline, subhead="", voice_text="spoken line", eyebrow=""
    )


def _plan_stub(monkeypatch: pytest.MonkeyPatch, fail_names: set[str] | None = None) -> list:
    calls: list[tuple[str, tuple | None]] = []
    failures = fail_names or set()

    def fake_generate_image(prompt, design, aspect, assets_dir, *, name="hero", refs=None):
        calls.append((name, tuple(refs) if refs else None))
        if name in failures:
            return None
        return f"assets/{name}.png"

    monkeypatch.setattr(video_imagegen, "generate_image", fake_generate_image)
    return calls


def test_art_plan_priority_hero_then_payoff_then_quote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("VIDEO_ART_MAX", raising=False)
    calls = _plan_stub(monkeypatch)
    beats = [_beat("hero"), _beat("quote"), _beat("caption"), _beat("payoff")]
    plan = video_imagegen.generate_art_plan(
        beats, _design(), "16:9", str(tmp_path), max_images=2
    )
    assert plan == {0: "assets/hero.png", 3: "assets/art3.png"}
    assert [name for name, _refs in calls] == ["hero", "art3"]  # payoff outranks quote


def test_art_plan_default_budget_is_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("VIDEO_ART_MAX", raising=False)
    calls = _plan_stub(monkeypatch)
    beats = [_beat("hero"), _beat("payoff"), _beat("quote")]
    plan = video_imagegen.generate_art_plan(beats, _design(), "16:9", str(tmp_path))
    assert plan == {0: "assets/hero.png"}
    assert len(calls) == 1


def test_art_plan_env_budget_and_param_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    beats = [_beat("hero"), _beat("payoff"), _beat("quote")]

    monkeypatch.setenv("VIDEO_ART_MAX", "3")
    calls = _plan_stub(monkeypatch)
    plan = video_imagegen.generate_art_plan(beats, _design(), "16:9", str(tmp_path))
    assert set(plan) == {0, 1, 2}  # env read in-body at call time
    assert len(calls) == 3

    calls = _plan_stub(monkeypatch)  # param beats env
    plan = video_imagegen.generate_art_plan(
        beats, _design(), "16:9", str(tmp_path), max_images=1
    )
    assert plan == {0: "assets/hero.png"}
    assert len(calls) == 1

    monkeypatch.setenv("VIDEO_ART_MAX", "not-a-number")
    calls = _plan_stub(monkeypatch)
    plan = video_imagegen.generate_art_plan(beats, _design(), "16:9", str(tmp_path))
    assert len(plan) == 1  # malformed env falls back to the default budget


def test_art_plan_skip_on_fail_does_not_refund_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("VIDEO_ART_MAX", raising=False)
    calls = _plan_stub(monkeypatch, fail_names={"hero"})
    beats = [_beat("hero"), _beat("payoff"), _beat("quote")]
    plan = video_imagegen.generate_art_plan(
        beats, _design(), "16:9", str(tmp_path), max_images=2
    )
    # The failed hero slot is NOT refunded: the quote beat never runs.
    assert plan == {1: "assets/art1.png"}
    assert [name for name, _refs in calls] == ["hero", "art1"]


def test_art_plan_zero_budget_and_no_eligible_beats(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("VIDEO_ART_MAX", raising=False)
    calls = _plan_stub(monkeypatch)
    beats = [_beat("hero"), _beat("payoff")]
    assert (
        video_imagegen.generate_art_plan(
            beats, _design(), "16:9", str(tmp_path), max_images=0
        )
        == {}
    )
    assert calls == []
    assert (
        video_imagegen.generate_art_plan(
            [_beat("caption"), _beat("stat")], _design(), "16:9", str(tmp_path)
        )
        == {}
    )
    assert calls == []


def test_art_plan_passes_refs_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("VIDEO_ART_MAX", raising=False)
    calls = _plan_stub(monkeypatch)
    video_imagegen.generate_art_plan(
        [_beat("hero")], _design(), "16:9", str(tmp_path), refs=["ref0.png", "ref1.jpg"]
    )
    assert calls == [("hero", ("ref0.png", "ref1.jpg"))]


# =============================================================================
# 7. GENERATE_IMAGE ATTEMPTS RETRY
# =============================================================================


def test_generate_image_attempts_retries_until_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = {"n": 0}

    def flaky_once(prompt, design, aspect, assets_dir, *, name="hero", refs=None, **kw):
        calls["n"] += 1
        return "assets/hero.png" if calls["n"] >= 3 else None

    monkeypatch.setattr(video_imagegen, "_generate_image_once", flaky_once)
    result = video_imagegen.generate_image(
        "a scene", _design(), "16:9", str(tmp_path), attempts=3
    )
    assert result == "assets/hero.png"
    assert calls["n"] == 3  # stopped on the first non-None


def test_generate_image_attempts_all_fail_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = {"n": 0}

    def always_none(prompt, design, aspect, assets_dir, *, name="hero", refs=None, **kw):
        calls["n"] += 1
        return None

    monkeypatch.setattr(video_imagegen, "_generate_image_once", always_none)
    result = video_imagegen.generate_image(
        "a scene", _design(), "16:9", str(tmp_path), attempts=3
    )
    assert result is None
    assert calls["n"] == 3  # exhausted every attempt


def test_generate_image_default_attempts_is_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = {"n": 0}

    def always_none(prompt, design, aspect, assets_dir, *, name="hero", refs=None, **kw):
        calls["n"] += 1
        return None

    monkeypatch.setattr(video_imagegen, "_generate_image_once", always_none)
    assert (
        video_imagegen.generate_image("a scene", _design(), "16:9", str(tmp_path))
        is None
    )
    assert calls["n"] == 1  # no retry by default


# =============================================================================
# 8. GENERATE_ART_PLAN PERSONA REFS (per-beat scoping)
# =============================================================================


def _persona_plan_stub(monkeypatch: pytest.MonkeyPatch) -> list:
    """Stub generate_image capturing (name, refs, attempts) per call."""
    calls: list[tuple[str, tuple | None, int]] = []

    def fake_generate_image(
        prompt, design, aspect, assets_dir, *, name="hero", refs=None, attempts=1
    ):
        calls.append((name, tuple(refs) if refs else None, attempts))
        return f"assets/{name}.png"

    monkeypatch.setattr(video_imagegen, "generate_image", fake_generate_image)
    return calls


def test_art_plan_persona_refs_only_on_hero_and_payoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("VIDEO_ART_MAX", raising=False)
    calls = _persona_plan_stub(monkeypatch)
    beats = [_beat("hero"), _beat("quote"), _beat("payoff")]
    video_imagegen.generate_art_plan(
        beats,
        _design(),
        "16:9",
        str(tmp_path),
        refs=["dossier.png"],
        max_images=3,
        persona_refs=["p1.png", "p2.png"],
    )
    by_name = {name: (refs, attempts) for name, refs, attempts in calls}
    # hero + payoff lock onto persona refs with the retry budget...
    assert by_name["hero"] == (("p1.png", "p2.png"), 3)
    assert by_name["art2"] == (("p1.png", "p2.png"), 3)  # payoff beat (index 2)
    # ...the quote beat keeps the dossier refs at the default single attempt.
    assert by_name["art1"] == (("dossier.png",), 1)


def test_art_plan_persona_none_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """persona_refs=None -> every beat uses the dossier refs path, default attempts."""
    monkeypatch.delenv("VIDEO_ART_MAX", raising=False)
    calls = _persona_plan_stub(monkeypatch)
    beats = [_beat("hero"), _beat("payoff"), _beat("quote")]
    plan = video_imagegen.generate_art_plan(
        beats, _design(), "16:9", str(tmp_path), refs=["dossier.png"], max_images=3
    )
    assert plan == {
        0: "assets/hero.png",
        1: "assets/art1.png",
        2: "assets/art2.png",
    }
    # Not one call carries persona refs or a non-default attempts count.
    assert all(refs == ("dossier.png",) and attempts == 1 for _n, refs, attempts in calls)


def test_art_plan_persona_custom_beat_kinds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("VIDEO_ART_MAX", raising=False)
    calls = _persona_plan_stub(monkeypatch)
    beats = [_beat("hero"), _beat("payoff"), _beat("quote")]
    video_imagegen.generate_art_plan(
        beats,
        _design(),
        "16:9",
        str(tmp_path),
        max_images=3,
        persona_refs=["p1.png"],
        persona_beat_kinds=("quote",),
        persona_attempts=5,
    )
    by_name = {name: (refs, attempts) for name, refs, attempts in calls}
    assert by_name["art2"] == (("p1.png",), 5)  # the quote beat (index 2) locks on
    assert by_name["hero"] == (None, 1)  # hero no longer persona-scoped
    assert by_name["art1"] == (None, 1)  # payoff no longer persona-scoped
