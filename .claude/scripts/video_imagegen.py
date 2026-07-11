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
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

GENERATION_TIMEOUT_S = 900

# How often to poll for codex's produced png while it is still running (seconds).
# Small enough to grab the file promptly after codex writes it, large enough to
# keep the watch loop cheap. Patchable in tests.
_POLL_INTERVAL_S = 1.5

# How long a png discovered ONLY by thread scoping must sit unchanged before we
# accept it. The codex agent inspects its own render and regenerates when it does
# not like the result (stray text, wrong composition), so the first settled file is
# often the one it threw away. Measured gap between a rejected render and its
# replacement: about 60 seconds. This wait is skipped entirely when the run ends
# normally or codex writes its -o file, both of which name the image the agent
# finally chose. It only costs latency on a render that HANGS after producing
# exactly one image.
_REGEN_GRACE_S = 90.0

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
    " Preserve their natural skin TONE and complexion exactly as shown in the"
    " references; do NOT lighten, whiten, brighten, or wash out their skin color."
    " Render their hair clean, dry, full, well-groomed and controlled, with soft"
    " defined waves and minimal flyaways."
    " Do NOT render hair as damp, oily, greasy, stringy, flat, clumped, frizzy, or wild, even if"
    " the reference photos show it that way."
)


def cli_available() -> bool:
    """True when the codex CLI is on PATH."""

    return shutil.which("codex") is not None


_CLAIMS_DIR = ".claims"


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
    """Every unclaimed png under the roots.

    Claimed images are excluded: once a render owns a file it must vanish from
    every other render's "fresh png" view, or a sibling re-discovers it and two
    prompts return one image.
    """

    found: set[Path] = set()
    for root in roots:
        try:
            if root.is_dir():
                found |= {p for p in root.rglob("*.png") if _CLAIMS_DIR not in p.parts}
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


def _kill_process_tree(proc: "subprocess.Popen | None") -> None:
    """Force-kill a subprocess AND all of its children. The codex CLI can leave
    a surviving grandchild that never exits (it holds the stdout pipe open), so a
    plain proc.kill() reaps only the parent and the tree keeps hanging. taskkill
    /T on Windows / proc.kill() on POSIX tears the whole tree down. Never raises."""
    if proc is None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=15,
            )
        else:
            proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


_GATE = threading.Condition()
_INFLIGHT = 0


def _max_concurrency() -> int:
    """How many codex renders may run at once. Resolved at call time (Rule 1).

    Safe above 1 ONLY because discovery is scoped per process: ``codex exec --json``
    emits ``thread.started`` whose ``thread_id`` IS the ``generated_images/<uuid>/``
    subdir this run writes into, and ``-o`` writes the agent's final path to a file
    we name. Both are private to one render. The unscoped "newest png under the
    roots" diff is NOT, and it silently returned one image for every prompt in a
    batch (2026-07-09).
    """

    try:
        return max(1, int(os.environ.get("IMAGEGEN_MAX_CONCURRENCY", "3")))
    except ValueError:
        return 3


_THREAD_ID_RE = re.compile(r'"thread_id"\s*:\s*"([0-9a-fA-F-]{36})"')


def _thread_id_from_stdout(stdout: str) -> str | None:
    """This run's codex thread id, from the ``thread.started`` JSONL event.

    It doubles as the name of the generated_images subdir codex writes into, so it
    is the per-process scope that makes concurrent renders attributable.
    """

    m = _THREAD_ID_RE.search(stdout or "")
    return m.group(1) if m else None


def _scoped_pngs(roots: list[Path], thread_id: str) -> list[Path]:
    """Every unclaimed png codex wrote for THIS thread. Owned by definition."""

    found: list[Path] = []
    for root in roots:
        d = root / thread_id
        try:
            if d.is_dir():
                found += [p for p in d.rglob("*.png") if _CLAIMS_DIR not in p.parts]
        except OSError:
            continue
    return found


def _png_from_last_message(path: Path | None) -> Path | None:
    """The absolute png path codex wrote to our private ``-o`` file, if any.

    The agent may regenerate (it self-rejects renders with stray text), so this
    names the image it FINALLY chose, not merely the first one it produced.

    A private file does NOT make the path inside it ours. The agent runs shell
    commands to hunt for its own output and can report a SIBLING's image; callers
    must confirm the result lies under this run's own thread dir.
    """

    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return _png_from_stdout(text)


def _within_thread(png: Path, roots: list[Path], thread_id: str | None) -> bool:
    """True when ``png`` lives under this run's own generated_images subdir.

    codex runs with sandbox bypass and greps the whole image tree when it loses
    track of its output, so a path it reports -- even into a file only we can read
    -- may belong to a concurrent render. Attribution has to be checked against
    the thread dir, never inferred from where the path was written.
    """

    if thread_id is None:
        return False
    for root in roots:
        try:
            png.relative_to(root / thread_id)
            return True
        except ValueError:
            continue
    return False


def _solo() -> bool:
    """True when this is the only render in flight in this process."""
    with _GATE:
        return _INFLIGHT <= 1


def _claim(png: Path) -> Path | None:
    """Atomically take ownership of a produced png; the loser of a race gets None.

    codex writes every image into ONE shared directory, so an atomic move is the
    only way two concurrent renders can be stopped from returning the same file.
    The destination is inside ``.claims/`` (same volume, so os.replace is atomic)
    which _snapshot_pngs excludes -- a claimed file must be invisible to every
    other render, not merely renamed in place.
    """
    try:
        claims = png.parent / _CLAIMS_DIR
        claims.mkdir(parents=True, exist_ok=True)
        dst = claims / f"{os.getpid()}-{threading.get_ident()}-{png.name}"
        os.replace(png, dst)
        return dst
    except OSError:
        return None


def _run_codex_watching(
    cmd: list[str],
    instruction: str,
    roots: list[Path],
    before: set[Path],
    timeout: int,
    *,
    last_message_file: Path | None = None,
) -> Path | None:
    """Run the codex image CLI and return the produced png Path, or None.

    Do NOT wait for codex to exit: it writes the png and prints its absolute
    path, THEN can hang on cleanup (a surviving grandchild keeps the stdout pipe
    open, so a plain subprocess.run blocks forever and strands the finished
    image). A reader thread drains stdout so codex never blocks on a full pipe.

    Discovery has three sources, in strict order of trust:

    1. ``last_message_file`` -- the private file codex was told to write its final
       answer into (``-o``). Per-process, and it names the image the agent FINALLY
       chose after any self-rejected regenerations.
    2. the newest settled png under ``generated_images/<thread_id>/``, where
       thread_id comes from the ``thread.started`` JSONL event. That subdir belongs
       to this process alone, so anything in it is ours by construction.
    3. a fresh-png diff over the shared roots. Every codex process writes into the
       same tree, so under concurrency this returns whichever image landed first,
       for every worker. It cannot attribute an image to a prompt, and it is used
       ONLY when this is provably the sole render in flight.

    A concurrent render that reaches neither (1) nor (2) returns None rather than
    silently adopting a neighbour's image. Every accepted png is claimed by an
    atomic move OUT of the watched tree, so no sibling can rediscover it.

    The process tree is force-killed on the way out. Never raises."""
    global _INFLIGHT
    with _GATE:
        while _INFLIGHT >= _max_concurrency():
            _GATE.wait()
        _INFLIGHT += 1
    try:
        # Re-snapshot now that this render owns a slot. The caller took `before`
        # before queueing, so a serialized batch would otherwise inherit a stale
        # view and treat an EARLIER render's leftovers as its own fresh output.
        # (codex writes a descriptive copy beside each ig_*.png; only the
        # reported one gets claimed, so leftovers are the normal case.)
        before = before | _snapshot_pngs(roots)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return None

        chunks: list[bytes] = []

        def _drain() -> None:
            try:
                if proc.stdout is not None:
                    for line in proc.stdout:
                        chunks.append(line)
            except Exception:
                pass

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()

        png: Path | None = None
        seen: dict[Path, tuple[int, float]] = {}
        saw_sibling = not _solo()

        def _ready(paths: list[Path], *, final: bool) -> Path | None:
            """Newest png that is fully written AND not about to be superseded.

            A file is accepted once its size has stopped changing and either the run
            is over (``final``) or it has been stable for _REGEN_GRACE_S. The wait
            exists because the codex agent inspects its own render and regenerates
            when it dislikes the result, so the first stable png is frequently the
            one it discarded.
            """
            now = time.monotonic()
            ready: list[Path] = []
            for p in paths:
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                if size <= 0:
                    continue
                prev = seen.get(p)
                if prev is None or prev[0] != size:
                    seen[p] = (size, now)  # new, or still being written
                    continue
                if final or (now - prev[1]) >= _REGEN_GRACE_S:
                    ready.append(p)
            if not ready:
                return None
            return max(ready, key=lambda p: p.stat().st_mtime)

        def _fallback(*, final: bool = False) -> Path | None:
            """Unscoped fresh-png diff. Solo-only, and solo-ness LATCHES.

            A sibling that has already exited leaves its image on disk, so "nobody
            is running right now" does not mean "every png under the root is mine".
            Once this call has ever seen a sibling it never uses the fallback again.
            """
            nonlocal saw_sibling
            if not _solo():
                saw_sibling = True
            if saw_sibling:
                return None
            fresh = [p for p in (_snapshot_pngs(roots) - before) if p.is_file()]
            return _ready(fresh, final=final)

        def _mine(*, final: bool = False) -> Path | None:
            """Best per-process candidate: our -o file, else our thread's dir.

            Every reported path is checked against this run's thread dir before it
            is trusted. The -o file needs no settle wait once it passes that check:
            codex writes it at turn end, naming the image it finally chose.
            """
            text = b"".join(chunks).decode("utf-8", "replace")
            thread_id = _thread_id_from_stdout(text)

            reported = _png_from_last_message(last_message_file) or _png_from_stdout(text)
            if reported is not None and reported.is_file() and reported.stat().st_size > 0:
                if _within_thread(reported, roots, thread_id):
                    return reported
                if thread_id is None and _solo():
                    # No --json and nothing else is running: it can only be ours.
                    return reported
                # Reported someone else's image. Ignore it and keep looking in our
                # own dir; better to time out than to return a sibling's render.

            if thread_id is None:
                return None
            return _ready(_scoped_pngs(roots, thread_id), final=final)

        try:
            try:
                if proc.stdin is not None:
                    proc.stdin.write(instruction.encode("utf-8"))
                    proc.stdin.close()
            except OSError:
                pass

            deadline = time.monotonic() + max(1, int(timeout))
            while time.monotonic() < deadline:
                # 1+2) sources private to THIS process. Safe under concurrency.
                found = _mine()
                if found is not None:
                    png = _claim(found)
                    if png is not None:
                        break
                # 3) solo-only fallback: a settled fresh png anywhere under roots.
                cand = _fallback()
                if cand is not None:
                    png = _claim(cand)
                    if png is not None:
                        break
                if proc.poll() is not None:  # codex exited cleanly on its own
                    reader.join(timeout=2)
                    exited = _mine(final=True) or _fallback(final=True)
                    png = _claim(exited) if exited is not None else None
                    break
                time.sleep(_POLL_INTERVAL_S)
        except Exception:
            png = None
        finally:
            _kill_process_tree(proc)
        return png
    finally:
        with _GATE:
            _INFLIGHT -= 1
            _GATE.notify()


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
    instruction: str | None = None,
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
            prompt, design, aspect, assets_dir, name=name, refs=refs,
            instruction=instruction,
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
    instruction: str | None = None,
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
            # JSONL events. The first one carries this run's thread_id, which IS
            # the generated_images subdir codex will write into. That is the only
            # per-process handle on "which image is mine" -- see _run_codex_watching.
            "--json",
        ]

        # A private file for codex's final answer. The instruction asks for the
        # absolute png path, and codex may regenerate (it self-rejects renders with
        # stray text), so this names the image it actually settled on. Per-process,
        # therefore safe to read while siblings render.
        last_dir = Path(tempfile.gettempdir()) / "codex-imagegen"
        last_dir.mkdir(parents=True, exist_ok=True)
        last_message_file = last_dir / f"last-{os.getpid()}-{uuid.uuid4().hex}.txt"
        cmd += ["-o", str(last_message_file)]

        for ref in ref_paths:
            cmd += ["-i", str(ref)]

        # Rule 1: the caller's instruction is a None sentinel resolved here, never a
        # bound default. It was previously overwritten unconditionally, so the
        # documented override was dead code. A verbatim override exists because
        # build_instruction() hard-forbids text in the image -- correct for a video
        # frame, wrong for a marketing asset whose copy must be rendered INTO it.
        if instruction is None:
            instruction = build_instruction(prompt, design, aspect)
        if ref_paths:
            instruction += "\n" + _IDENTITY_LOCK_LINE + "\n" + _RETOUCH_LINE

        # codex reads the instruction from stdin as UTF-8 bytes (a text=True
        # subprocess would encode with the platform locale -- cp1252 on Windows --
        # so a non-ASCII char like an em-dash in a brand tagline would become an
        # invalid-UTF-8 byte codex rejects). codex also writes the png and then
        # can HANG on cleanup, so _run_codex_watching grabs the file the instant
        # it lands and force-kills the process tree instead of waiting for a
        # clean exit that never comes.
        try:
            png = _run_codex_watching(
                cmd, instruction, roots, before, _resolve_timeout(),
                last_message_file=last_message_file,
            )
        finally:
            try:
                last_message_file.unlink(missing_ok=True)
            except OSError:
                pass
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
