"""Model-agnostic brief-to-MP4 video pipeline (HyperFrames HTML renderer).

Turns a one-paragraph brief into a finished, voiced, styled MP4:

    brief -> runtime-lane copywriting (beats) -> claim-safety gate
          -> per-beat voiceover (edge-tts) -> ffprobe-measured durations
          -> allocate_scene_frames() (voiceover drives timing)
          -> deterministic HTML composition (GSAP timeline, design-token driven)
          -> npx hyperframes render -> MP4
          -> ffprobe verify (H.264 + AAC, full duration) -> scorecard

Model-agnostic by construction: the copywriting pass and the optional judge
pass both go through the framework runtime lanes
(``runtime.lane_router.run_with_runtime_lanes`` with ``TEXT_REASONING``,
``allowed_tools=[]``, ``max_turns=1``), so whichever provider lane the
operator has configured writes the copy. When no lane is available or its
output cannot be parsed, a deterministic 2-beat fallback built from the raw
brief keeps the render alive (the pipeline never fails on copy generation).

Visual identity comes ENTIRELY from a design dict resolved by
``video_styles.resolve_design()``: palette, fonts, motion hints, and flourish
flags. The HTML writer takes no other visual input, so any registered style
(or an operator-supplied design file) renders the same brief faithfully.

Operating rules carried over from the framework media stack:
    1. Unique output dir per run (default under ``.claude/data/video-renders``).
    2. Claim-safety gate: invented metrics/superlatives that are not present
       in the brief or ``claims_source`` get the copy swapped for the
       deterministic fallback before anything renders.
    3. Voiceover drives timing: every spoken beat finishes before the visual
       changes (min floor + pad, optionally scaled to the target duration).
    4. Pre-hide rule: every later-revealing element is set to autoAlpha 0 at
       t=0 in the GSAP timeline, so no frame ever flashes unstyled content.
    5. Served assets only: audio and images are referenced relatively from
       the project ``assets/`` dir (file:// URIs do not load in the headless
       render).
    6. Verify after render: ffprobe gate for H.264 video + AAC audio spanning
       the full duration.
    7. ``render_brief()`` is synchronous and never raises for operational
       failures: it returns ``ok=False`` plus an ``error`` string. ``ok``
       means rendered AND verified; the scorecard is reported in ``score``
       and callers that want the adversarial gate can enforce
       ``score["passed"]``.

Usage:
    uv run python video_pipeline.py "What shipped and why it matters" \
        --style blockframe --duration-target 20 --claims-source "facts..."
    uv run python video_pipeline.py --list-styles
    uv run python video_pipeline.py --check-deps
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Boot-shim: must run BEFORE any framework imports (runtime, etc.) so a
# persona profile selected via CLI/env applies to the whole process.
from personas import apply_persona_override

apply_persona_override()

from runtime.base import RuntimeRequest  # noqa: E402
from runtime.capabilities import TEXT_REASONING  # noqa: E402
from runtime.lane_router import run_with_runtime_lanes  # noqa: E402

import video_styles  # noqa: E402

# =============================================================================
# CONSTANTS (honest constants only; tunables are read from env at call time)
# =============================================================================

# Pin the HyperFrames CLI so renders are reproducible across runs/machines.
HYPERFRAMES_VERSION = "0.6.88"

FPS = 30

# Voiceover-drives-timing constants.
MIN_SCENE_FRAMES = 54  # ~1.8s at 30fps, floor so no beat flashes by
SCENE_PAD_FRAMES = 8  # breathing room after each spoken beat

# Neutral default voice. Override per call (voice=...) or via env VIDEO_VOICE;
# both are resolved inside the function body at call time.
DEFAULT_VOICE = "en-US-GuyNeural"
DEFAULT_VOICE_RATE = "+8%"

SCORE_GATE = 80

ASPECT_CANVAS: dict[str, tuple[int, int]] = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}

_GSAP_CDN = "https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"


def _repo_root() -> Path:
    """The repository root, resolved from this file's location."""

    return Path(__file__).resolve().parents[2]


def _default_output_root() -> Path:
    """Default render root: <repo>/.claude/data/video-renders (gitignored)."""

    return Path(__file__).resolve().parent.parent / "data" / "video-renders"


def _resolve_exe(name: str) -> str:
    """Resolve an executable to its full path (Windows .CMD/.EXE aware).

    subprocess.run without shell=True does not honor PATHEXT, so a bare
    "npx" raises WinError 2 on Windows even though the shim exists. Resolve
    through shutil.which first, fall back to the bare name on POSIX.
    """

    return shutil.which(name) or name


def _edge_tts_importable() -> bool:
    try:
        import edge_tts  # noqa: F401

        return True
    except ImportError:
        return False


def check_dependencies() -> list[str]:
    """Names of missing tools. Empty list means the pipeline is ready.

    Checks the executables the render path shells out to (node, npx, ffmpeg,
    ffprobe) and that the ``edge_tts`` python module is importable.
    """

    missing = [
        tool
        for tool in ("node", "npx", "ffmpeg", "ffprobe")
        if shutil.which(tool) is None
    ]
    if not _edge_tts_importable():
        missing.append("edge_tts")
    return missing


# =============================================================================
# CLAIM-SAFETY GATE
# =============================================================================
# Reject render/voice text that asserts metrics, benchmarks, or superlatives
# the caller did NOT supply. The allowlist is the set of tokens present in the
# brief + claims_source; anything claim-shaped outside that set is a rejection.

_BANNED_SUPERLATIVES = (
    "best",
    "fastest",
    "cheapest",
    "lowest",
    "#1",
    "number one",
    "guaranteed",
    "world-class",
    "revolutionary",
    "game-changing",
    "unbeatable",
    "save up to",
)

# Claim-shaped patterns (numbers + a unit/comparator). Patterns capture the
# FULL number (including decimals) so "5.9KB" is matched whole.
_NUM = r"\d[\d,]*(?:\.\d+)?"
_CLAIM_PATTERNS = (
    re.compile(rf"{_NUM}\s*x\b", re.IGNORECASE),  # 10x, 2.5x
    re.compile(rf"{_NUM}\s*%"),  # 58%
    re.compile(
        rf"{_NUM}\s*(?:stars?|downloads?|users?|tests?|kb|mb|gb)\b", re.IGNORECASE
    ),
    re.compile(rf"\$\s?{_NUM}"),  # prices: $29, $1,200
)


@dataclass(frozen=True)
class ClaimCheck:
    """Result of scanning copy against the supplied-fact allowlist."""

    ok: bool
    rejections: tuple[str, ...] = ()

    @property
    def detail(self) -> str:
        if self.ok:
            return "no invented claims"
        return "; ".join(self.rejections)


def _allowed_tokens(*sources: str) -> set[str]:
    """Lowercased word/number tokens the caller explicitly supplied."""

    tokens: set[str] = set()
    for src in sources:
        if not src:
            continue
        for raw in re.findall(r"[A-Za-z0-9.$%]+", src.lower()):
            stripped = raw.strip(".")
            if stripped:
                tokens.add(stripped)
    return tokens


def check_claims(render_text: str, *supplied_sources: str) -> ClaimCheck:
    """Reject invented metrics/superlatives not present in supplied sources.

    A claim-shaped token (number+unit, percent, multiplier, price) is allowed
    only when the same token appears in one of the caller-supplied sources.
    Marketing superlatives are always rejected.
    """

    rejections: list[str] = []
    lowered = render_text.lower()

    for banned in _BANNED_SUPERLATIVES:
        if banned in lowered:
            rejections.append(f"banned superlative: '{banned}'")

    allowed = _allowed_tokens(*supplied_sources)
    for pattern in _CLAIM_PATTERNS:
        for match in pattern.finditer(render_text):
            phrase = match.group(0).strip()
            claim_tokens = {
                t.strip(".")
                for t in re.findall(r"[A-Za-z0-9.$%]+", phrase.lower())
                if t.strip(".")
            }
            if claim_tokens and not claim_tokens.issubset(allowed):
                rejections.append(f"unsupplied metric: '{phrase}'")

    seen: set[str] = set()
    unique = [r for r in rejections if not (r in seen or seen.add(r))]
    return ClaimCheck(ok=not unique, rejections=tuple(unique))


# =============================================================================
# BEATS (the deterministic copy contract between writer and renderer)
# =============================================================================


@dataclass
class Beat:
    """One render beat: on-screen copy + the spoken line for that scene."""

    eyebrow: str
    headline: str
    subhead: str
    voice_text: str
    cta: str = ""
    scene_frames: int = MIN_SCENE_FRAMES
    voice_duration: float = 0.0  # ffprobe-measured, seconds
    voice_path: str = ""  # absolute path to this beat's audio (or "")

    def render_text(self) -> str:
        """All operator-visible copy in this beat (for claim-checking)."""

        parts = (self.eyebrow, self.headline, self.subhead, self.cta)
        return " ".join(p for p in parts if p)


# The em-dash character, kept as an escape so the source itself stays clean.
_EM_DASH = "\u2014"


def _clean_field(value: Any, limit: int) -> str:
    """Normalize one copy field: single line, no em-dash, truncated."""

    text = str(value or "").strip()
    text = re.sub(rf"\s*{_EM_DASH}\s*", ", ", text)  # em-dash never ships
    text = re.sub(r"\s+", " ", text)
    return _shorten(text, limit)


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]).rstrip(",;: ")


def parse_beats(text: str) -> list[Beat] | None:
    """Parse model output into beats. Returns None when nothing is usable.

    Accepts (most robust first):
      1. a fenced ```json code block containing a list of beat objects
      2. a bare JSON array anywhere in the text
      3. numbered lines: ``N. eyebrow | headline | subhead | voice``
    """

    if not (text or "").strip():
        return None

    candidates: list[str] = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text)
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for raw in candidates:
        snippet = raw.strip()
        s, e = snippet.find("["), snippet.rfind("]")
        if s == -1 or e <= s:
            continue
        try:
            data = json.loads(snippet[s : e + 1])
        except ValueError:
            continue
        beats = _beats_from_objects(data)
        if beats:
            return beats

    return _beats_from_numbered_lines(text)


def _beats_from_objects(data: Any) -> list[Beat] | None:
    if not isinstance(data, list):
        return None
    beats: list[Beat] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        headline = _clean_field(item.get("headline"), 60)
        if not headline:
            continue
        voice = _clean_field(item.get("voice") or item.get("voice_text"), 220)
        beats.append(
            Beat(
                eyebrow=_clean_field(item.get("eyebrow"), 28),
                headline=headline,
                subhead=_clean_field(item.get("subhead"), 110),
                voice_text=voice or headline,
                cta=_clean_field(item.get("cta"), 60),
            )
        )
    return beats[:6] or None


def _beats_from_numbered_lines(text: str) -> list[Beat] | None:
    beats: list[Beat] = []
    for match in re.finditer(r"(?m)^\s*\d+[.)]\s+(.+)$", text):
        parts = [p.strip() for p in match.group(1).split("|")]
        if not parts or not parts[0]:
            continue
        if len(parts) >= 4:
            eyebrow, headline, subhead, voice = parts[0], parts[1], parts[2], parts[3]
        elif len(parts) == 3:
            eyebrow, headline, subhead, voice = "", parts[0], parts[1], parts[2]
        elif len(parts) == 2:
            eyebrow, headline, subhead = "", parts[0], parts[1]
            voice = f"{parts[0]}. {parts[1]}"
        else:
            eyebrow, headline, subhead, voice = "", parts[0], "", parts[0]
        beats.append(
            Beat(
                eyebrow=_clean_field(eyebrow, 28),
                headline=_clean_field(headline, 60),
                subhead=_clean_field(subhead, 110),
                voice_text=_clean_field(voice, 220) or _clean_field(headline, 60),
            )
        )
    return beats[:6] or None


def fallback_beats(brief: str) -> list[Beat]:
    """Deterministic 2-beat plan built ONLY from the raw brief.

    Used when the lane output is malformed or fails the claim gate. By
    construction the copy contains no tokens beyond the brief itself plus a
    handful of neutral connective words, so it always passes the claim gate.
    """

    text = re.sub(r"\s+", " ", (brief or "").strip()) or "A quick update."
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s]
    first = sentences[0] if sentences else text
    rest = " ".join(sentences[1:]).strip()

    return [
        Beat(
            eyebrow="OVERVIEW",
            headline=_clean_field(first, 60),
            subhead=_clean_field(rest, 110),
            voice_text=_clean_field(first, 220),
        ),
        Beat(
            eyebrow="IN SHORT",
            headline="In short",
            subhead=_clean_field(first, 110),
            voice_text=_clean_field(rest or first, 220),
        ),
    ]


def coerce_beats(text: str, brief: str) -> tuple[list[Beat], bool]:
    """Parse lane output, or fall back deterministically. Never fails.

    Returns (beats, used_fallback).
    """

    beats = parse_beats(text)
    if beats:
        return beats, False
    return fallback_beats(brief), True


# =============================================================================
# COPY GENERATION (runtime lanes; provider-agnostic by contract)
# =============================================================================


def _beats_prompt(
    brief: str,
    claims_source: str,
    design: dict,
    duration_target_s: int,
) -> str:
    beat_hint = max(2, min(5, round(duration_target_s / 7)))
    claims_block = claims_source.strip() or (
        "(none provided: do not use any numbers, metrics, percentages, "
        "multipliers, or prices at all)"
    )
    return f"""You are writing the on-screen copy and voiceover for a short product video.

BRIEF (the only story you may tell):
{brief.strip()}

VERIFIED CLAIMS SOURCE (the only numbers/metrics you may use):
{claims_block}

VISUAL STYLE: {design.get('name', 'neutral')} ({design.get('tagline', '')})
TARGET LENGTH: about {duration_target_s} seconds total. Write {beat_hint} beats.

HARD RULES (breaking any one is a failure):
- Only state facts present in the brief or the claims source above. Never
  invent numbers, percentages, multipliers, prices, or benchmarks.
- No marketing superlatives (best, fastest, cheapest, number one, guaranteed,
  revolutionary, game-changing, world-class, unbeatable).
- No em-dash characters anywhere. Use periods or commas.
- eyebrow: 1 to 3 word uppercase kicker. headline: 60 characters max.
  subhead: 110 characters max. voice: ONE spoken sentence, 16 words max.
- cta: empty string unless the brief contains an explicit call to action.

Return EXACTLY one fenced JSON code block and nothing else:

```json
[
  {{"eyebrow": "...", "headline": "...", "subhead": "...", "voice": "...", "cta": ""}}
]
```"""


def _run_lane(prompt: str, task_name: str) -> tuple[str, str]:
    """One no-tools, single-turn lane call. Returns (text, provider_label)."""

    result = asyncio.run(
        run_with_runtime_lanes(
            RuntimeRequest(
                prompt=prompt,
                cwd=_repo_root(),
                task_name=task_name,
                capability=TEXT_REASONING,
                max_turns=1,
                allowed_tools=[],
            )
        )
    )
    label = result.provider or "unknown"
    if result.model:
        label = f"{label}:{result.model}"
    return (result.text or "").strip(), label


def generate_beats(
    brief: str,
    claims_source: str,
    design: dict,
    duration_target_s: int,
) -> tuple[list[Beat], str, list[str]]:
    """Brief -> beats via the runtime lanes, claim-gated, never failing.

    Returns (beats, provider, notes). provider is the lane label that wrote
    the final copy, or "fallback" when the deterministic plan was used.
    """

    notes: list[str] = []
    text, lane_label = "", ""
    try:
        text, lane_label = _run_lane(
            _beats_prompt(brief, claims_source, design, duration_target_s),
            task_name="video_brief_beats",
        )
    except Exception as exc:
        notes.append(f"lane unavailable: {type(exc).__name__}: {exc}")

    beats, used_fallback = coerce_beats(text, brief)
    if used_fallback:
        if lane_label and text:
            notes.append("lane output unparseable; deterministic fallback used")
        return beats, "fallback", notes

    # Claim gate on the lane copy (visible text AND the spoken lines).
    spoken = " ".join(b.voice_text for b in beats)
    visible = " ".join(b.render_text() for b in beats)
    check = check_claims(f"{visible} {spoken}", brief, claims_source)
    if not check.ok:
        notes.append(f"lane copy rejected by claim gate: {check.detail}")
        return fallback_beats(brief), "fallback", notes

    return beats, lane_label or "fallback", notes


# =============================================================================
# VOICEOVER (edge-tts per beat) + TIMING ALLOCATION
# =============================================================================


def ffprobe_duration(media_path: str | Path) -> float:
    """Return media duration in seconds via ffprobe, or 0.0 on failure."""

    try:
        result = subprocess.run(
            [
                _resolve_exe("ffprobe"),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        raw = (result.stdout or "").strip()
        return float(raw) if raw else 0.0
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0.0


def allocate_scene_frames(
    voice_durations: list[float],
    *,
    fps: int = FPS,
    min_frames: int = MIN_SCENE_FRAMES,
    pad_frames: int = SCENE_PAD_FRAMES,
    total_frames: int | None = None,
) -> list[int]:
    """Convert per-beat voiceover durations into per-scene frame counts.

    Each scene gets ``ceil(voice_duration * fps) + pad`` frames, floored at
    ``min_frames``. When ``total_frames`` is given, the per-scene counts are
    scaled proportionally to sum to EXACTLY that total while never dropping
    below the floor (rounding drift is reconciled onto the longest scene).

    A beat with no measured voice (duration 0) still gets ``min_frames`` so
    it never flashes by.
    """

    if not voice_durations:
        return []

    raw = [
        max(min_frames, math.ceil(max(0.0, d) * fps) + pad_frames)
        for d in voice_durations
    ]

    if total_frames is None:
        return raw

    natural_total = sum(raw)
    if natural_total <= 0 or natural_total == total_frames:
        return raw

    scale = total_frames / natural_total
    scaled = [max(min_frames, int(round(f * scale))) for f in raw]
    drift = total_frames - sum(scaled)
    if drift != 0 and scaled:
        idx = max(range(len(scaled)), key=lambda i: scaled[i])
        scaled[idx] = max(min_frames, scaled[idx] + drift)
    return scaled


async def _synthesize_edge(text: str, out_path: Path, voice: str, rate: str) -> bool:
    try:
        import edge_tts

        communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
        await communicate.save(str(out_path))
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception as exc:  # pragma: no cover - network/runtime dependent
        print(f"[video_pipeline] edge-tts failed: {exc}")
        return False


def build_voiceover(beats: list[Beat], assets_dir: Path, *, voice: str | None = None) -> str:
    """Synthesize each beat and measure it. Returns "edge-tts" or "".

    The voice resolves at call time: explicit param > env VIDEO_VOICE > the
    neutral default. Mutates beats in place (voice_path, voice_duration).
    """

    resolved_voice = (voice or os.environ.get("VIDEO_VOICE", "")).strip() or DEFAULT_VOICE
    resolved_rate = os.environ.get("VIDEO_VOICE_RATE", "").strip() or DEFAULT_VOICE_RATE

    assets_dir.mkdir(parents=True, exist_ok=True)
    produced = False
    for i, beat in enumerate(beats):
        out_path = assets_dir / f"beat{i}.mp3"
        ok = asyncio.run(
            _synthesize_edge(beat.voice_text, out_path, resolved_voice, resolved_rate)
        )
        if ok:
            beat.voice_path = str(out_path)
            beat.voice_duration = ffprobe_duration(out_path)
            produced = produced or beat.voice_duration > 0
        else:
            beat.voice_path = ""
            beat.voice_duration = 0.0
    return "edge-tts" if produced else ""


def concat_voiceover_adelay(
    beats: list[Beat],
    out_path: Path,
    *,
    fps: int = FPS,
    total_s: float | None = None,
) -> bool:
    """Mix the per-beat audio into ONE track, each beat delayed to its scene.

    Uses ffmpeg adelay (per input, delay = scene start in ms) + amix, so the
    spoken line for beat N starts exactly when scene N appears.
    """

    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    cursor_frames = 0
    idx = 0
    for beat in beats:
        if beat.voice_path and Path(beat.voice_path).exists():
            start_ms = max(1, int(round(cursor_frames / fps * 1000)))
            inputs += ["-i", beat.voice_path]
            filters.append(f"[{idx}:a]adelay={start_ms}:all=1[a{idx}]")
            labels.append(f"[a{idx}]")
            idx += 1
        cursor_frames += beat.scene_frames

    if not labels:
        return False

    filter_complex = (
        ";".join(filters)
        + f";{''.join(labels)}amix=inputs={idx}:duration=longest:normalize=0[out]"
    )
    cmd = [_resolve_exe("ffmpeg"), "-y", *inputs, "-filter_complex", filter_complex]
    cmd += ["-map", "[out]"]
    if total_s:
        cmd += ["-t", f"{total_s:.3f}"]
    cmd.append(str(out_path))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return result.returncode == 0 and out_path.exists()
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"[video_pipeline] voiceover mix failed: {exc}")
        return False


# =============================================================================
# HTML COMPOSITION (every visual decision comes from the design dict)
# =============================================================================


def _esc(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def compose_html(
    beats: list[Beat],
    design: dict,
    *,
    width: int,
    height: int,
    fps: int = FPS,
    total_frames: int | None = None,
    audio_rel: str = "",
    hero_rel: str = "",
) -> str:
    """Assemble the deterministic index.html for the run.

    Pure function: visual decisions come ONLY from the design dict (palette,
    fonts, motion, flags). Scenes are sequential clips on track 1, the mixed
    voiceover plays on track 0, and the styled hero background is CSS-built
    from the palette (or an operator-supplied image, fit-contained).

    Timeline rules:
      - PRE-HIDE: every later-revealing element gets a ``tl.set`` to
        autoAlpha 0 at t=0 before any reveal tween.
      - SERVED ASSETS: audio/images are referenced relatively (``assets/...``).
      - The timeline is registered on ``window.__timelines``.
    """

    palette = design["palette"]
    fonts = design["fonts"]
    motion = design.get("motion", {})
    flags = design.get("flags", {}) or {}
    extras = design.get("extras", {}) or {}

    bg, fg = palette["bg"], palette["fg"]
    accent, accent_dim = palette["accent"], palette["accent_dim"]
    muted = video_styles.blend_hex(fg, bg, 0.35)
    dark_canvas = video_styles.relative_luminance(bg) < 0.5

    ease = motion.get("entrance_ease", "power3.out")
    transition = motion.get("transition", "crossfade")
    display_weight = int(fonts.get("display_weight", 800))

    total = total_frames or sum(b.scene_frames for b in beats) or MIN_SCENE_FRAMES
    total_s = round(total / fps, 4)
    m = min(width, height)

    pad_x = int(width * 0.099)
    pad_bottom = int(height * 0.12)
    sizes = {
        "eyebrow": int(m * 0.026),
        "headline": int(m * 0.088),
        "subhead": int(m * 0.036),
        "cta": int(m * 0.030),
        "counter": int(m * 0.020),
        "numeral": int(m * 0.42),
    }

    # ---- flourish CSS driven by flags -------------------------------------
    headline_extra = ""
    if flags.get("uppercase_display"):
        headline_extra += " text-transform: uppercase;"
    if flags.get("lowercase_display"):
        headline_extra += " text-transform: lowercase;"
    if flags.get("tilted_display"):
        headline_extra += " transform: rotate(-3deg); transform-origin: left bottom;"
    if flags.get("stacked_text_shadow"):
        headline_extra += f" text-shadow: 0.045em 0.045em 0 {accent_dim};"

    panel_css = "background: transparent;"
    if flags.get("hard_borders"):
        border_w = max(4, m // 240)
        shadow_w = border_w * 2 if flags.get("offset_shadow") else 0
        panel_bg = extras.get("white", bg)
        panel_css = (
            f"background: {panel_bg}; border: {border_w}px solid {fg};"
            f" padding: {int(m * 0.045)}px {int(m * 0.05)}px;"
        )
        if shadow_w:
            panel_css += f" box-shadow: {shadow_w}px {shadow_w}px 0 {fg};"
    elif flags.get("card_chrome"):
        panel_css = (
            f"background: {video_styles.blend_hex(bg, accent, 0.05)};"
            f" border: 2px solid {accent_dim}; border-radius: {int(m * 0.014)}px;"
            f" padding: {int(m * 0.04)}px {int(m * 0.045)}px;"
        )

    eyebrow_pill = ""
    if flags.get("pill_shapes") or flags.get("pill_tags"):
        eyebrow_pill = (
            f" border: 2px solid {fg}; border-radius: 999px;"
            f" padding: 0.35em 1em; width: max-content;"
        )

    # ---- background + chrome layers ----------------------------------------
    if hero_rel:
        hero_layer = (
            f'      <div id="hero" style="position:absolute; inset:0; z-index:0; '
            f"background-image:url('{hero_rel}'); background-size:contain; "
            f"background-position:center; background-repeat:no-repeat; "
            f'background-color:{bg};"></div>'
        )
    elif dark_canvas:
        hero_layer = (
            f'      <div id="hero" style="position:absolute; inset:0; z-index:0; '
            f"background:radial-gradient(900px 900px at 18% 12%, {accent_dim}, transparent 62%), "
            f"radial-gradient(760px 760px at 86% 88%, {accent_dim}, transparent 64%), "
            f'{bg};"></div>'
        )
    else:
        glow = video_styles.blend_hex(bg, accent, 0.16)
        hero_layer = (
            f'      <div id="hero" style="position:absolute; inset:0; z-index:0; '
            f"background:radial-gradient(1000px 1000px at 84% 10%, {glow}, transparent 60%), "
            f'{bg};"></div>'
        )

    chrome: list[str] = []
    if flags.get("graph_grid"):
        cell = max(24, int(m * 0.035))
        chrome.append(
            f'      <div id="grid" style="position:absolute; inset:0; z-index:1; '
            f"background-image:linear-gradient(to right, {accent_dim} 1px, transparent 1px), "
            f"linear-gradient(to bottom, {accent_dim} 1px, transparent 1px); "
            f'background-size:{cell}px {cell}px;"></div>'
        )
    if flags.get("color_region_split"):
        chrome.append(
            f'      <div id="region" style="position:absolute; left:0; top:0; bottom:0; '
            f'width:34%; background:{accent}; z-index:1;"></div>'
        )
    if flags.get("decorative_pills"):
        pill_colors = list(extras.values())[:2] or [accent_dim, accent_dim]
        chrome.append(
            f'      <div id="pill-a" style="position:absolute; top:{int(height*0.08)}px; '
            f"right:{int(width*0.07)}px; width:{int(m*0.30)}px; height:{int(m*0.11)}px; "
            f'border-radius:999px; background:{pill_colors[0]}; opacity:0.55; z-index:1;"></div>'
        )
        chrome.append(
            f'      <div id="pill-b" style="position:absolute; top:{int(height*0.30)}px; '
            f"right:{int(width*0.16)}px; width:{int(m*0.18)}px; height:{int(m*0.08)}px; "
            f"border-radius:999px; background:{pill_colors[1 % len(pill_colors)]}; "
            f'opacity:0.45; z-index:1;"></div>'
        )
    if flags.get("hairline_rules"):
        inset_y = int(height * 0.055)
        chrome.append(
            f'      <div class="rule" style="position:absolute; top:{inset_y}px; '
            f"left:{int(pad_x*0.55)}px; right:{int(pad_x*0.55)}px; height:2px; "
            f'background:{accent}; z-index:3;"></div>'
        )
        chrome.append(
            f'      <div class="rule" style="position:absolute; bottom:{inset_y}px; '
            f"left:{int(pad_x*0.55)}px; right:{int(pad_x*0.55)}px; height:2px; "
            f'background:{accent}; z-index:3;"></div>'
        )
    if flags.get("topbar_rule"):
        chrome.append(
            f'      <div id="topbar" style="position:absolute; top:{int(height*0.06)}px; '
            f"left:{int(pad_x*0.55)}px; right:{int(pad_x*0.55)}px; height:2px; "
            f'background:{fg}; z-index:3;"></div>'
        )
    if flags.get("footline"):
        chrome.append(
            f'      <div id="footline" style="position:absolute; bottom:{int(height*0.055)}px; '
            f"left:{int(pad_x*0.55)}px; right:{int(pad_x*0.55)}px; height:1px; "
            f'background:{video_styles.blend_hex(fg, bg, 0.5)}; z-index:3;"></div>'
        )
    if flags.get("progress_bar"):
        chrome.append(
            f'      <div id="progress" style="position:absolute; left:0; bottom:0; '
            f"width:100%; height:{max(6, int(m*0.008))}px; background:{accent}; "
            f'transform:scaleX(0); transform-origin:left center; z-index:3;"></div>'
        )

    # ---- scenes -------------------------------------------------------------
    scene_html: list[str] = []
    prehide_js: list[str] = []
    entrance_js: list[str] = []
    transition_js: list[str] = []

    show_counter = bool(flags.get("topbar_rule"))
    show_numeral = bool(flags.get("wallpaper_numeral"))

    cursor = 0
    starts: list[float] = []
    for i, beat in enumerate(beats):
        start_s = round(cursor / fps, 4)
        dur_s = round(beat.scene_frames / fps, 4)
        starts.append(start_s)
        sid = f"s{i}"

        inner: list[str] = []
        if show_numeral:
            inner.append(
                f'      <div id="{sid}-numeral" class="numeral">{i + 1:02d}</div>'
            )
        if show_counter:
            inner.append(
                f'      <div id="{sid}-counter" class="counter">{i + 1:02d} / {len(beats):02d}</div>'
            )
        panel_parts: list[str] = []
        if beat.eyebrow:
            panel_parts.append(
                f'        <div id="{sid}-eyebrow" class="eyebrow">{_esc(beat.eyebrow)}</div>'
            )
        panel_parts.append(
            f'        <div id="{sid}-headline" class="headline">{_esc(beat.headline)}</div>'
        )
        if beat.subhead:
            panel_parts.append(
                f'        <div id="{sid}-subhead" class="subhead">{_esc(beat.subhead)}</div>'
            )
        if beat.cta:
            panel_parts.append(
                f'        <div id="{sid}-cta" class="cta">{_esc(beat.cta)}</div>'
            )
        inner.append('      <div class="panel">\n' + "\n".join(panel_parts) + "\n      </div>")

        scene_html.append(
            f'    <div id="{sid}" class="scene clip" data-start="{start_s}" '
            f'data-duration="{dur_s}" data-track-index="1">\n'
            + "\n".join(inner)
            + "\n    </div>"
        )

        # PRE-HIDE rule: every later-revealing element starts at autoAlpha 0.
        reveal_offsets = [
            ("eyebrow", 0.10, bool(beat.eyebrow)),
            ("headline", 0.26, True),
            ("subhead", 0.50, bool(beat.subhead)),
            ("cta", 0.70, bool(beat.cta)),
            ("counter", 0.10, show_counter),
        ]
        for suffix, offset, present in reveal_offsets:
            if not present:
                continue
            prehide_js.append(
                f'  tl.set("#{sid}-{suffix}", {{ autoAlpha: 0, y: {int(m * 0.026)} }}, 0);'
            )
            entrance_js.append(
                f'  tl.to("#{sid}-{suffix}", {{ autoAlpha: 1, y: 0, duration: 0.55, '
                f'ease: "{ease}" }}, {round(start_s + offset, 3)});'
            )
        if i > 0:
            slide_in = ", x: 60" if transition == "slide" else ""
            prehide_js.append(f'  tl.set("#{sid}", {{ autoAlpha: 0{slide_in} }}, 0);')

        cursor += beat.scene_frames

    # Scene-boundary transitions, per the design's motion default.
    for i in range(1, len(beats)):
        b = starts[i]
        prev, cur = f"s{i - 1}", f"s{i}"
        if transition == "cut":
            transition_js.append(f'  tl.set("#{prev}", {{ autoAlpha: 0 }}, {b});')
            transition_js.append(f'  tl.set("#{cur}", {{ autoAlpha: 1 }}, {b});')
        elif transition == "slide":
            t0 = max(0.0, round(b - 0.45, 3))
            transition_js.append(
                f'  tl.to("#{prev}", {{ autoAlpha: 0, x: -60, duration: 0.45, '
                f'ease: "power1.inOut" }}, {t0});'
            )
            transition_js.append(
                f'  tl.to("#{cur}", {{ autoAlpha: 1, x: 0, duration: 0.45, '
                f'ease: "power1.out" }}, {max(0.0, round(b - 0.40, 3))});'
            )
        else:  # crossfade
            t0 = max(0.0, round(b - 0.45, 3))
            transition_js.append(
                f'  tl.to("#{prev}", {{ autoAlpha: 0, duration: 0.45, '
                f'ease: "power1.inOut" }}, {t0});'
            )
            transition_js.append(
                f'  tl.to("#{cur}", {{ autoAlpha: 1, duration: 0.45, '
                f'ease: "power1.inOut" }}, {max(0.0, round(b - 0.40, 3))});'
            )

    ambient_js: list[str] = [
        f'  tl.to("#hero", {{ scale: 1.06, duration: {total_s}, ease: "sine.inOut" }}, 0);'
    ]
    if flags.get("progress_bar"):
        ambient_js.append(
            f'  tl.to("#progress", {{ scaleX: 1, duration: {total_s}, ease: "none" }}, 0);'
        )

    audio_block = ""
    if audio_rel:
        audio_block = (
            f'      <audio id="vo" data-start="0" data-duration="{total_s}" '
            f'data-track-index="0" src="{audio_rel}" data-volume="1"></audio>'
        )

    css = f"""      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{ margin: 0; width: {width}px; height: {height}px; overflow: hidden; background: {bg}; }}
      body {{ font-family: "{fonts['body']}", sans-serif; color: {fg}; }}
      .scene {{ position: absolute; inset: 0; z-index: 2; display: flex; flex-direction: column; justify-content: flex-end; padding: {int(height * 0.10)}px {pad_x}px {pad_bottom}px; }}
      .panel {{ display: flex; flex-direction: column; gap: {int(m * 0.020)}px; max-width: {int(width * 0.80)}px; {panel_css} }}
      .eyebrow {{ font-family: "{fonts['mono']}", sans-serif; font-size: {sizes['eyebrow']}px; font-weight: 600; letter-spacing: 0.18em; text-transform: uppercase; color: {accent};{eyebrow_pill} }}
      .headline {{ font-family: "{fonts['display']}", serif; font-size: {sizes['headline']}px; font-weight: {display_weight}; line-height: 1.04; color: {fg};{headline_extra} }}
      .subhead {{ font-family: "{fonts['body']}", sans-serif; font-size: {sizes['subhead']}px; font-weight: 400; line-height: 1.35; color: {muted}; }}
      .cta {{ font-family: "{fonts['mono']}", sans-serif; font-size: {sizes['cta']}px; font-weight: 600; color: {accent}; margin-top: {int(m * 0.012)}px; }}
      .counter {{ position: absolute; top: {int(height * 0.035)}px; right: {int(pad_x * 0.55)}px; font-family: "{fonts['mono']}", monospace; font-size: {sizes['counter']}px; letter-spacing: 0.14em; color: {fg}; }}
      .numeral {{ position: absolute; top: -2%; right: 3%; font-family: "{fonts['display']}", serif; font-size: {sizes['numeral']}px; font-weight: {display_weight}; line-height: 1; color: {accent}; opacity: 0.16; z-index: 0; }}
      #accent-rule {{ position: absolute; left: {pad_x}px; bottom: {int(pad_bottom * 0.72)}px; width: {int(m * 0.16)}px; height: 4px; background: {accent}; z-index: 3; }}"""

    nl = "\n"
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={width}, height={height}" />
    <link rel="stylesheet" href="{fonts['google_fonts_url']}" />
    <script src="{_GSAP_CDN}"></script>
    <style>
{css}
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="{total_s}" data-width="{width}" data-height="{height}">
{hero_layer}
{nl.join(chrome)}
{audio_block}
      <div id="accent-rule"></div>
{nl.join(scene_html)}
    </div>

    <script>
      window.__timelines = window.__timelines || {{}};
      const tl = gsap.timeline({{ paused: true }});
{nl.join(prehide_js)}
{nl.join(ambient_js)}
  tl.from("#accent-rule", {{ scaleX: 0, duration: 0.6, ease: "{ease}", transformOrigin: "left center" }}, 0.2);
{nl.join(entrance_js)}
{nl.join(transition_js)}
      window.__timelines["main"] = tl;
    </script>
  </body>
</html>
"""


# =============================================================================
# RENDER + VERIFY
# =============================================================================


def _write_project(out_dir: Path, html: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "assets").mkdir(exist_ok=True)
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    manifest = {
        "paths": {
            "blocks": "compositions",
            "components": "compositions/components",
            "assets": "assets",
        }
    }
    (out_dir / "hyperframes.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def run_hyperframes_render(out_dir: Path, mp4_path: Path, *, fps: int = FPS) -> dict[str, Any]:
    """Invoke the pinned HyperFrames CLI on the project dir."""

    quality = os.environ.get("VIDEO_RENDER_QUALITY", "").strip() or "standard"
    cmd = [
        _resolve_exe("npx"),
        "--yes",
        f"hyperframes@{HYPERFRAMES_VERSION}",
        "render",
        "--quality",
        quality,
        "--fps",
        str(fps),
        "--output",
        str(mp4_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(out_dir),
            capture_output=True,
            text=True,
            timeout=900,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "error": str(exc), "command": " ".join(cmd)}

    ok = result.returncode == 0 and mp4_path.exists()
    return {
        "ok": ok,
        "error": "" if ok else (result.stderr or result.stdout or "")[-600:],
        "command": " ".join(cmd),
    }


def verify_rendered_mp4(mp4_path: str | Path, expected_duration: float) -> dict[str, Any]:
    """ffprobe gate: H.264 video + AAC audio spanning ~the full duration."""

    path = Path(mp4_path)
    if not path.exists():
        return {"ok": False, "reason": f"file missing: {path.name}", "duration": 0.0}

    try:
        result = subprocess.run(
            [
                _resolve_exe("ffprobe"),
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(result.stdout or "{}")
    except (subprocess.SubprocessError, ValueError, OSError) as exc:
        return {"ok": False, "reason": f"ffprobe failed: {exc}", "duration": 0.0}

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    reasons: list[str] = []
    if not video:
        reasons.append("no video stream")
    elif video.get("codec_name") != "h264":
        reasons.append(f"video codec is {video.get('codec_name')}, expected h264")
    if not audio:
        reasons.append("no audio stream")
    elif audio.get("codec_name") != "aac":
        reasons.append(f"audio codec is {audio.get('codec_name')}, expected aac")

    container_dur = ffprobe_duration(path)
    if expected_duration > 0 and abs(container_dur - expected_duration) > 0.6:
        reasons.append(
            f"duration {container_dur:.2f}s != expected {expected_duration:.2f}s"
        )

    return {
        "ok": not reasons,
        "reason": "; ".join(reasons) if reasons else "h264+aac, full duration",
        "video_codec": video.get("codec_name") if video else None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "duration": round(container_dur, 3),
    }


# =============================================================================
# SCORECARD (auto heuristic + optional lane judge; take the MIN; never block)
# =============================================================================

SCORE_CATEGORIES: tuple[tuple[str, int], ...] = (
    ("technical_validity", 18),
    ("claim_safety", 16),
    ("text_readability", 12),
    ("pacing", 12),
    ("audio_fit", 10),
    ("visual_polish", 10),
    ("copy_hygiene", 8),
    ("hook_strength", 8),
    ("structure", 6),
)


def score_auto(
    beats: list[Beat],
    verify: dict[str, Any],
    claim_check: ClaimCheck,
    voice_provider: str,
    *,
    fps: int = FPS,
    hero_present: bool = False,
) -> dict[str, Any]:
    """Deterministic heuristic scorecard. Returns {score, categories, notes}."""

    cat: dict[str, int] = {}
    all_text = " ".join(b.render_text() for b in beats)

    cat["technical_validity"] = 18 if verify.get("ok") else 0
    cat["claim_safety"] = 16 if claim_check.ok else 0

    readable = all(
        len(b.headline) <= 60 and len(b.subhead) <= 120 for b in beats
    )
    cat["text_readability"] = 12 if readable else 6

    max_scene_frames = int(12 * fps)
    paced = all(MIN_SCENE_FRAMES <= b.scene_frames <= max_scene_frames for b in beats)
    cat["pacing"] = 12 if paced else 6

    voiced = bool(voice_provider) and all(b.voice_duration > 0 for b in beats)
    cat["audio_fit"] = 10 if voiced else (5 if voice_provider else 0)

    # A styled CSS hero is the v1 contract; supplied art scores full marks.
    cat["visual_polish"] = 10 if hero_present else 8

    cat["copy_hygiene"] = 8 if (_EM_DASH not in all_text and claim_check.ok) else 4

    hook = beats[0].headline if beats else ""
    cat["hook_strength"] = 8 if len(hook.split()) >= 3 else 4

    cat["structure"] = 6 if len(beats) >= 3 else 3

    score = sum(cat.values())
    notes: list[str] = []
    if not verify.get("ok"):
        notes.append(f"technical: {verify.get('reason')}")
    if not claim_check.ok:
        notes.append(f"claims: {claim_check.detail}")
    return {"score": score, "categories": cat, "notes": notes}


def judge_with_lanes(beats: list[Beat], design: dict) -> dict[str, Any]:
    """Optional adversarial judge via the runtime lanes. Never blocks.

    Returns {"score": int | None, "raw": ...}. Score None means the judge was
    unavailable/disabled/unparseable and the caller should fall back to the
    auto score alone. Disable entirely with env VIDEO_JUDGE=off.
    """

    mode = os.environ.get("VIDEO_JUDGE", "on").strip().lower()
    if mode in {"off", "0", "false", "no"}:
        return {"score": None, "raw": "judge disabled via VIDEO_JUDGE"}

    copy_lines = "\n".join(
        f"- [{i}] {b.eyebrow} | {b.headline} | {b.subhead} | voice: {b.voice_text}"
        for i, b in enumerate(beats)
    )
    prompt = (
        "You are an adversarial reviewer of a short product video. Score the "
        "copy below 0-100 across: claim_safety, text_readability, pacing, "
        "copy_hygiene, hook_strength, structure. Reject invented metrics or "
        "marketing superlatives hard. The visual style is "
        f"{design.get('name', 'neutral')} ({design.get('tagline', '')}).\n\n"
        'Reply with ONLY a JSON object: {"score": <int 0-100>, '
        '"verdict": "PASS|NEEDS RERENDER|BLOCKED", "notes": "..."}.\n\n'
        f"On-screen copy:\n{copy_lines}\n"
    )
    try:
        text, _label = _run_lane(prompt, task_name="video_brief_judge")
    except Exception as exc:
        return {"score": None, "raw": f"judge unavailable: {type(exc).__name__}: {exc}"}
    return _parse_judge_score(text)


def _parse_judge_score(text: str) -> dict[str, Any]:
    """Extract the last JSON object with a numeric 'score' from judge output."""

    for match in reversed(list(re.finditer(r"\{[^{}]*\"score\"[^{}]*\}", text or ""))):
        try:
            obj = json.loads(match.group(0))
            score = obj.get("score")
            if isinstance(score, (int, float)):
                return {"score": int(score), "raw": obj}
        except (ValueError, TypeError):
            continue
    return {"score": None, "raw": (text or "")[-300:]}


def final_score(auto: dict[str, Any], judge: dict[str, Any]) -> dict[str, Any]:
    """Take the MINIMUM of auto + judge. Judge None means auto only."""

    auto_score = int(auto.get("score", 0))
    judge_score = judge.get("score")
    if isinstance(judge_score, (int, float)):
        final = min(auto_score, int(judge_score))
        source = "min(auto, judge)"
    else:
        final = auto_score
        source = "auto-only (judge unavailable)"
    return {
        "final": final,
        "source": source,
        "auto": auto_score,
        "judge": judge_score if isinstance(judge_score, (int, float)) else None,
        "passed": final >= SCORE_GATE,
        "gate": SCORE_GATE,
        "categories": auto.get("categories", {}),
        "notes": list(auto.get("notes", [])),
    }


# =============================================================================
# ORCHESTRATION
# =============================================================================


def _resolve_art(art_dir: str | None, assets_dir: Path) -> str:
    """Copy the newest image from art_dir into served assets. Returns rel ref."""

    if not art_dir:
        return ""
    drop = Path(art_dir)
    if not drop.is_dir():
        return ""
    images = sorted(
        (p for p in drop.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not images:
        return ""
    src = images[0]
    dst = assets_dir / f"hero{src.suffix.lower()}"
    try:
        assets_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    except OSError:
        return ""
    return f"assets/{dst.name}"


def render_brief(
    brief: str,
    *,
    style: str | None = None,
    design_file: str | None = None,
    aspect: str = "16:9",
    duration_target_s: int = 30,
    claims_source: str = "",
    output_root: str | None = None,
    art_dir: str | None = None,
) -> dict:
    """Brief in, MP4 out. Synchronous; never raises for operational failures.

    Returns: {"ok", "mp4_path", "output_dir", "duration_s", "score",
    "provider", "style", "error"}. ``ok`` means rendered AND ffprobe-verified
    (H.264 + AAC, full duration); the scorecard rides in ``score`` and callers
    wanting the adversarial gate can enforce ``score["passed"]``.

    ``duration_target_s`` is a ceiling: when the measured voiceover runs
    longer, scenes are scaled down to fit; a shorter voiceover is never
    stretched out with dead air.
    """

    result = {
        "ok": False,
        "mp4_path": "",
        "output_dir": "",
        "duration_s": 0.0,
        "score": {},
        "provider": "",
        "style": "",
        "error": "",
    }

    try:
        design = video_styles.resolve_design(style=style, design_file=design_file)
    except ValueError as exc:
        result["error"] = str(exc)
        return result
    result["style"] = design.get("name", "neutral")

    if not (brief or "").strip():
        result["error"] = "empty brief"
        return result

    missing = check_dependencies()
    if missing:
        result["error"] = "missing dependencies: " + ", ".join(missing)
        return result

    width, height = ASPECT_CANVAS.get(aspect, ASPECT_CANVAS["16:9"])
    root = Path(output_root) if output_root else _default_output_root()
    run_id = (
        f"{result['style']}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    )
    out_dir = root / run_id
    assets_dir = out_dir / "assets"
    result["output_dir"] = str(out_dir)

    try:
        out_dir.mkdir(parents=True, exist_ok=True)

        # 1. Copy (runtime lanes, claim-gated, deterministic fallback).
        beats, provider, notes = generate_beats(
            brief, claims_source, design, duration_target_s
        )
        result["provider"] = provider

        # 2. Voiceover first: it drives the timing.
        voice_provider = build_voiceover(beats, assets_dir)

        durations = [b.voice_duration for b in beats]
        natural = allocate_scene_frames(durations)
        target_frames = max(MIN_SCENE_FRAMES, int(round(duration_target_s * FPS)))
        if sum(natural) > target_frames:
            frames = allocate_scene_frames(durations, total_frames=target_frames)
        else:
            frames = natural
        for beat, count in zip(beats, frames):
            beat.scene_frames = count
        total_frames = sum(frames)
        total_s = round(total_frames / FPS, 4)

        # 3. One mixed audio track, each beat delayed to its scene start.
        audio_rel = ""
        if voice_provider:
            audio_path = assets_dir / "voice.mp3"
            if concat_voiceover_adelay(beats, audio_path, fps=FPS, total_s=total_s):
                audio_rel = f"assets/{audio_path.name}"

        # 4. Optional operator-supplied hero art (newest image in art_dir).
        hero_rel = _resolve_art(art_dir, assets_dir)

        # 5. Deterministic composition + audit record.
        html = compose_html(
            beats,
            design,
            width=width,
            height=height,
            fps=FPS,
            total_frames=total_frames,
            audio_rel=audio_rel,
            hero_rel=hero_rel,
        )
        _write_project(out_dir, html)
        (out_dir / "beats.json").write_text(
            json.dumps(
                {
                    "brief": brief,
                    "style": result["style"],
                    "provider": provider,
                    "voice_provider": voice_provider,
                    "aspect": aspect,
                    "total_frames": total_frames,
                    "beats": [
                        {
                            "eyebrow": b.eyebrow,
                            "headline": b.headline,
                            "subhead": b.subhead,
                            "cta": b.cta,
                            "voice_text": b.voice_text,
                            "scene_frames": b.scene_frames,
                            "voice_duration": round(b.voice_duration, 3),
                        }
                        for b in beats
                    ],
                    "notes": notes,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        # 6. Render + verify.
        mp4_path = out_dir / f"{run_id}.mp4"
        render = run_hyperframes_render(out_dir, mp4_path, fps=FPS)
        if not render["ok"]:
            result["error"] = f"render failed: {render['error']}"
            return result
        result["mp4_path"] = str(mp4_path)

        verify = verify_rendered_mp4(mp4_path, total_s)
        result["duration_s"] = float(verify.get("duration") or total_s)

        # 7. Scorecard (auto + optional lane judge; judge never blocks).
        spoken = " ".join(b.voice_text for b in beats)
        visible = " ".join(b.render_text() for b in beats)
        claim_check = check_claims(f"{visible} {spoken}", brief, claims_source)
        auto = score_auto(
            beats, verify, claim_check, voice_provider, hero_present=bool(hero_rel)
        )
        judge = judge_with_lanes(beats, design)
        score = final_score(auto, judge)
        score["notes"].extend(notes)
        result["score"] = score

        result["ok"] = bool(verify.get("ok"))
        if not verify.get("ok"):
            result["error"] = f"verify failed: {verify.get('reason')}"
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result


# =============================================================================
# CLI
# =============================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Brief to MP4 video pipeline (HyperFrames, style registry)"
    )
    parser.add_argument("brief", nargs="?", default="", help="What the video should say")
    parser.add_argument("--style", default=None, help="Registered style name")
    parser.add_argument("--design-file", default=None, help="design.md/frame.md or JSON token file")
    parser.add_argument("--aspect", default="16:9", choices=sorted(ASPECT_CANVAS))
    parser.add_argument("--duration-target", type=int, default=30, dest="duration_target")
    parser.add_argument("--claims-source", default="", help="Verified facts the copy may cite")
    parser.add_argument("--output-root", default=None)
    parser.add_argument("--art-dir", default=None, help="Optional hero image drop dir (newest used)")
    parser.add_argument("--list-styles", action="store_true")
    parser.add_argument("--check-deps", action="store_true")
    args = parser.parse_args()

    if args.list_styles:
        for entry in video_styles.list_styles():
            print(f"{entry['name']:18s} {entry['tagline']}")
        return
    if args.check_deps:
        missing = check_dependencies()
        print(json.dumps({"ready": not missing, "missing": missing}))
        return
    if not args.brief:
        parser.error("a brief is required (or use --list-styles / --check-deps)")

    outcome = render_brief(
        args.brief,
        style=args.style,
        design_file=args.design_file,
        aspect=args.aspect,
        duration_target_s=args.duration_target,
        claims_source=args.claims_source,
        output_root=args.output_root,
        art_dir=args.art_dir,
    )
    print(json.dumps(outcome, indent=2))
    if not outcome.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
