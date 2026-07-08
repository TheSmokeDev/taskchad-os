"""Optional generated-art adapter for the video pipeline (public, provider-optional).

Generates scene art for a render through the codex CLI's image_generation
feature, when that CLI happens to be installed on the box. This module is
the ONLY place the video pipeline touches the codex CLI; the rest of the
pipeline stays provider-neutral and treats this adapter as a black box that
either returns served-asset paths or nothing.

Contract:
    generate_image(prompt, design, aspect, assets_dir, *, name="hero",
                   refs=None) -> str | None
    generate_art_plan(beats, design, aspect, assets_dir, *, refs=None,
                      max_images=None) -> dict[int, str]
    generate_hero(prompt, design, aspect, assets_dir) -> str | None

    - generate_image returns the RELATIVE served path (e.g. "assets/hero.png")
      after copying the generated PNG into ``assets_dir``, or None. ``refs``
      are local reference images attached via repeatable ``-i <path>`` args
      (identity lock: the instruction tells the model to keep the subject
      identity shown in the references while composing a new scene).
    - generate_art_plan maps art-eligible beats (kind in ART_KINDS, priority
      hero -> payoff -> quote) onto generated images, sequentially, capped by
      ``max_images`` param > env VIDEO_ART_MAX (read at call time) >
      DEFAULT_ART_MAX. Skip-on-fail: a failed candidate consumes its budget
      slot and is simply absent from the plan. Beat 0 keeps the ``hero.<ext>``
      name (back-compat with the art-drop discovery path); other beats are
      named ``art<index>.<ext>``.
    - generate_hero is the back-compat thin wrapper (UNCHANGED signature).
    - Nothing here ever raises: CLI absence, quota walls, timeouts, parse
      failures, and copy errors all return None / an empty plan so the
      caller falls back to its CSS visuals.

Mechanics:
    - Detection: ``shutil.which("codex")``. Absent -> None immediately.
    - Invocation: ``codex exec --enable image_generation
      --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox``
      (plus ``-i <path>`` per reference) with the instruction PIPED VIA
      STDIN. codex exec reads the prompt from stdin when no positional
      prompt is given; in non-interactive shells stdin MUST be
      piped/redirected or the process hangs waiting for a terminal.
    - Output discovery: newest NEW png under the codex image roots
      (``$CODEX_HOME``/``~/.codex`` ``generated_images``), snapshot
      before/after, falling back to an absolute .png path printed on stdout.
      A reference-heavy run can outlast the timeout yet still have written
      the file, so a timeout still attempts the before/after salvage.
    - The instruction derives from the caller's subject prompt plus the
      design's palette/mood tokens: one bold scene about the topic, with an
      explicit no-text/no-logos rule so the renderer owns all copy.

The pipeline-level off-switch (env VIDEO_ART=off or render_brief(art="off"))
is enforced by the caller; this module only generates when asked.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

GENERATION_TIMEOUT_S = 900

# Beat kinds that are eligible for generated art, and the default budget.
ART_KINDS = ("hero", "quote", "payoff")
DEFAULT_ART_MAX = 1

# Generation order when the budget is tighter than the eligible beats.
_ART_PRIORITY = ("hero", "payoff", "quote")

_ASPECT_HINTS = {
    "16:9": "wide 16:9 landscape, 1920x1080",
    "9:16": "tall 9:16 portrait, 1080x1920",
    "1:1": "square 1:1, 1080x1080",
}

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

_IDENTITY_LOCK_LINE = (
    "Match the subject identity shown in the attached reference image(s);"
    " same character/product/brand subject, new scene."
)

_RETOUCH_LINE = (
    "If the subject is a person, keep their skin and face completely natural and"
    " photo-realistic. Do NOT airbrush, smooth, or beautify the skin. Make only"
    " two corrections: no warts or moles on the face, and no bags or puffiness"
    " under the eyes. Preserve their exact identity, features, and real skin texture."
)


def cli_available() -> bool:
    """True when the codex CLI is on PATH."""

    return shutil.which("codex") is not None


def _generated_images_root() -> Path:
    """Primary dir the codex CLI writes generated images into (session subdirs).

    Honors CODEX_HOME at call time (Rule 1); defaults to ``~/.codex``.
    """

    codex_home = os.environ.get("CODEX_HOME", "").strip()
    base = Path(codex_home) if codex_home else Path.home() / ".codex"
    return base / "generated_images"


def _candidate_roots() -> list[Path]:
    """Every dir to watch for a newly written png, primary first.

    Resolved at call time (Rule 1). Always the active root; plus the default
    ``~/.codex`` location when a CODEX_HOME override points elsewhere, so
    discovery stays correct whether or not CODEX_HOME is set. De-duplicated.
    """

    roots: list[Path] = [_generated_images_root()]
    if os.environ.get("CODEX_HOME", "").strip():
        default_root = Path.home() / ".codex" / "generated_images"
        if default_root not in roots:
            roots.append(default_root)
    return roots


def _snapshot_pngs(roots: list[Path]) -> set[Path]:
    found: set[Path] = set()
    for root in roots:
        try:
            if root.is_dir():
                found |= set(root.rglob("*.png"))
        except OSError:
            continue
    return found


def build_instruction(prompt: str, design: dict, aspect: str) -> str:
    """Compose the image instruction from the subject + design tokens.

    The image is a SCENE about the topic; readable copy stays out of the
    image so the HTML renderer owns every word on screen.
    """

    palette = (design or {}).get("palette", {}) or {}
    tagline = str((design or {}).get("tagline", "") or "")
    hint = _ASPECT_HINTS.get(aspect, _ASPECT_HINTS["16:9"])

    lines = [
        f"Generate an image: {str(prompt).strip()}.",
        f"One bold cinematic scene with a single strong focal point, {hint},"
        " generous negative space, modern and clean.",
    ]
    if tagline:
        lines.append(f"Mood reference: {tagline}")
    bg, accent = palette.get("bg", ""), palette.get("accent", "")
    if bg or accent:
        lines.append(
            f"Color world: background tones near {bg or 'neutral dark'},"
            f" one accent near {accent or 'a single hue'}."
        )
    lines.append(
        "Absolutely no text, no words, no letters, no numbers, no logos,"
        " no watermarks, no UI chrome."
    )
    lines.append(
        "Use your image generation tool. After generating, reply with ONLY"
        " the absolute file path of the PNG you created."
    )
    return "\n".join(lines)


def _newest_new_png(roots: list[Path], before: set[Path]) -> Path | None:
    fresh = [p for p in _snapshot_pngs(roots) - before if p.is_file()]
    if not fresh:
        return None
    try:
        return max(fresh, key=lambda p: p.stat().st_mtime)
    except OSError:
        return None


def _resolve_timeout() -> int:
    """Generation timeout in seconds at call time: env VIDEO_ART_TIMEOUT_S >
    GENERATION_TIMEOUT_S (Rule 1). Reference-heavy runs are slow, so the
    default is generous; the knob lets a slower box widen it further."""

    raw = os.environ.get("VIDEO_ART_TIMEOUT_S", "").strip()
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass  # ambient config never breaks a render
    return GENERATION_TIMEOUT_S


def _log_discovery_miss(roots: list[Path]) -> None:
    """One stderr breadcrumb when no png was found, so a future None is
    diagnosable. Never raises."""

    try:
        checked = ", ".join(str(r) for r in roots) or "(none)"
        print(
            f"[video_imagegen] generate_image: no new png found; checked roots: {checked}",
            file=sys.stderr,
        )
    except Exception:
        pass


def _png_from_stdout(stdout: str) -> Path | None:
    """Fallback discovery: an absolute image path echoed on the last lines."""

    for line in reversed((stdout or "").splitlines()):
        candidate = line.strip().strip('"').strip("'")
        if not candidate:
            continue
        if Path(candidate).suffix.lower() in _IMAGE_SUFFIXES:
            path = Path(candidate)
            if path.is_file():
                return path
    return None


def generate_image(
    prompt: str,
    design: dict,
    aspect: str,
    assets_dir: str,
    *,
    name: str = "hero",
    refs: list[str] | None = None,
    attempts: int = 1,
) -> str | None:
    """Generate one image and copy it into the served assets dir.

    Returns an "assets/<name>.png"-style relative path, or None on ANY
    failure (absence, quota, timeout, no output, copy error). ``refs`` are
    local reference image paths attached via repeatable ``-i <path>`` args;
    when at least one exists, an identity-lock line rides on the
    instruction so the generated scene keeps the referenced subject.
    ``attempts`` retries the (transiently flaky) generation up to that many
    times, returning the first non-None result. Never raises.
    """

    try:
        tries = max(1, int(attempts))
    except (TypeError, ValueError):
        tries = 1
    for _ in range(tries):
        rel = _generate_image_once(
            prompt, design, aspect, assets_dir, name=name, refs=refs
        )
        if rel is not None:
            return rel
    return None


def _generate_image_once(
    prompt: str,
    design: dict,
    aspect: str,
    assets_dir: str,
    *,
    name: str = "hero",
    refs: list[str] | None = None,
) -> str | None:
    """One generation attempt. Returns the relative served path or None on
    ANY failure (absence, quota, timeout, no output, copy error). Never
    raises."""

    try:
        if not str(prompt or "").strip():
            return None
        exe = shutil.which("codex")
        if not exe:
            return None

        ref_paths = [Path(r) for r in (refs or []) if str(r or "").strip()]
        ref_paths = [p for p in ref_paths if p.is_file()]

        roots = _candidate_roots()
        before = _snapshot_pngs(roots)

        cmd = [
            exe,
            "exec",
            "--enable",
            "image_generation",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        for ref in ref_paths:
            cmd += ["-i", str(ref)]

        instruction = build_instruction(prompt, design, aspect)
        if ref_paths:
            instruction += "\n" + _IDENTITY_LOCK_LINE + "\n" + _RETOUCH_LINE

        # Prompt goes through stdin: codex exec reads instructions from stdin
        # when no positional prompt is supplied, and a non-interactive run
        # without piped stdin hangs waiting for a terminal.
        stdout = ""
        try:
            result = subprocess.run(
                cmd,
                input=instruction,
                capture_output=True,
                text=True,
                timeout=_resolve_timeout(),
            )
            stdout = result.stdout or ""
        except subprocess.TimeoutExpired as exc:
            # codex writes the png before it finishes its wrap-up output, so a
            # slow reference-heavy run that trips the timeout may still have
            # produced a file; salvage it from the root diff below.
            partial = getattr(exc, "stdout", None) or getattr(exc, "output", None)
            stdout = partial.decode(errors="replace") if isinstance(partial, bytes) else (partial or "")

        png = _newest_new_png(roots, before)
        if png is None:
            png = _png_from_stdout(stdout)
        if png is None:
            _log_discovery_miss(roots)
            return None  # quota walls / refusals land here: no new image

        dest_dir = Path(assets_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        suffix = png.suffix.lower() if png.suffix.lower() in _IMAGE_SUFFIXES else ".png"
        dst = dest_dir / f"{str(name or 'hero')}{suffix}"
        shutil.copyfile(png, dst)
        return f"{dest_dir.name}/{dst.name}"
    except Exception:
        return None


def generate_hero(prompt: str, design: dict, aspect: str, assets_dir: str) -> str | None:
    """Back-compat wrapper: one opening-beat image named ``hero.<ext>``.

    Returns "assets/hero.png"-style relative path, or None on ANY failure
    (absence, quota, timeout, no output, copy error). Never raises.
    """

    return generate_image(prompt, design, aspect, assets_dir, name="hero")


def _resolve_art_budget(max_images: int | None) -> int:
    """Art budget at call time: param > env VIDEO_ART_MAX > DEFAULT_ART_MAX."""

    if max_images is not None:
        try:
            return max(0, int(max_images))
        except (TypeError, ValueError):
            return DEFAULT_ART_MAX
    raw = os.environ.get("VIDEO_ART_MAX", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass  # ambient config never breaks a render
    return DEFAULT_ART_MAX


def _beat_prompt(beat: object) -> str:
    """The image subject for one beat: headline + subhead, else the narration."""

    headline = str(getattr(beat, "headline", "") or "").strip()
    subhead = str(getattr(beat, "subhead", "") or "").strip()
    combined = " ".join(part for part in (headline, subhead) if part)
    return combined or str(getattr(beat, "voice_text", "") or "").strip()


def generate_art_plan(
    beats: list,
    design: dict,
    aspect: str,
    assets_dir: str,
    *,
    refs: list[str] | None = None,
    max_images: int | None = None,
    persona_refs: list[str] | None = None,
    persona_beat_kinds: tuple[str, ...] = ("hero", "payoff"),
    persona_attempts: int = 3,
) -> dict[int, str]:
    """Generate art for the art-eligible beats. Returns {beat_index: rel_path}.

    Eligible kinds: ``ART_KINDS``. Priority: hero scenes first, then payoff,
    then quote (original beat order within each kind). Budget resolves at
    call time (``max_images`` param > env VIDEO_ART_MAX > DEFAULT_ART_MAX).
    Generation is sequential and skip-on-fail: a failed candidate consumes
    its budget slot (no refund) and is absent from the plan. Beat 0 keeps
    the ``hero.<ext>`` name; other beats are named ``art<index>.<ext>``.

    When ``persona_refs`` is non-empty, art-eligible beats whose kind is in
    ``persona_beat_kinds`` (default hero + payoff) lock onto the persona
    references with ``persona_attempts`` retries; every other beat keeps the
    dossier ``refs`` path. With ``persona_refs`` None the behavior is
    byte-identical to the pre-persona path. Never raises.
    """

    try:
        budget = _resolve_art_budget(max_images)
        if budget <= 0:
            return {}
        beat_list = list(beats or [])
        ordered: list[int] = []
        for kind in _ART_PRIORITY:
            for i, beat in enumerate(beat_list):
                if str(getattr(beat, "kind", "") or "").strip().lower() == kind:
                    ordered.append(i)
        persona_kinds = {
            str(k).strip().lower() for k in (persona_beat_kinds or ())
        }
        art_map: dict[int, str] = {}
        for i in ordered[:budget]:
            beat = beat_list[i]
            name = "hero" if i == 0 else f"art{i}"
            kind = str(getattr(beat, "kind", "") or "").strip().lower()
            if persona_refs and kind in persona_kinds:
                rel = generate_image(
                    _beat_prompt(beat),
                    design,
                    aspect,
                    assets_dir,
                    name=name,
                    refs=persona_refs,
                    attempts=persona_attempts,
                )
            else:
                rel = generate_image(
                    _beat_prompt(beat),
                    design,
                    aspect,
                    assets_dir,
                    name=name,
                    refs=refs,
                )
            if rel:
                art_map[i] = rel
        return art_map
    except Exception:
        return {}
