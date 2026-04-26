"""Conversational compounding loop — concept drafter.

When the bot produces a long, analytical response, this module silently
writes a structured draft into ``vault/memory/concepts/_drafts/`` and
returns a footer + interactive components that prompt the user to
``/file accept`` or ``/file diff`` the draft. Footer never enters
``response_text`` — it lives on the structured ``OutgoingMessage.footer``
field so persistence sees only the assistant answer.

Public surface:

* ``should_draft(user_text, response_text)`` — predicate lifted from
  ``engine.py:939-948`` (note: empirically unvalidated; gap-6.1 will
  F1-test against a hand-labeled corpus).
* ``derive_slug(user_text, response_text)`` — H1/H2 in response → first
  4 non-stopword tokens of the user message → ``UNTITLED``.
* ``create_draft(...)`` — atomic write to the drafts directory with
  cross-volume guard. Returns ``DraftResult``.
* ``find_draft_by_id(auto_id, vault_dir)`` — full UUID OR 8+ char prefix
  lookup. Raises ``DraftAmbiguityError`` on ambiguous prefix.
* ``accept_draft(...)`` / ``diff_draft(...)`` — synchronous, fail-soft.
* ``sweep_expired(vault_dir)`` — deletes drafts older than 24h, skips
  files with mtime within last 60s.
* ``maybe_draft_and_footer(...)`` — single entrypoint the engine calls.
  Always returns ``(footer_str, components_list)``; on any error returns
  ``("", [])`` so drafting never blocks message delivery.

Per the iteration-3 plan §I3, ``create_draft`` calls
``_maybe_inline_sweep`` which runs ``sweep_expired`` at most once per
hour. The OS-scheduled standalone (``sweep_concept_drafts.py``) handles
daily cadence.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Add scripts dir for entity_extractor / chat dir for models
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))


# ---------------------------------------------------------------------------
# Constants — lifted verbatim from engine.py:939-948
# ---------------------------------------------------------------------------

DRAFT_THRESHOLD_CHARS = 800

# Lifted verbatim from engine.py:940-942 (empirically unvalidated; gap-6.1
# will F1-test). DO NOT edit without the corpus benchmark.
ANALYSIS_MARKERS: tuple[str, ...] = (
    "compared to",
    "difference between",
    "trade-off",
    "the reason",
    "this means",
    "in summary",
    "the key insight",
    "versus",
    " vs ",
    "analysis",
)

DRAFT_TTL_SECONDS = 24 * 3600
SWEEP_INFLIGHT_GUARD_SECONDS = 60
_INLINE_SWEEP_INTERVAL_SECONDS = 3600  # 1 hour throttle on inline sweep

DRAFTS_RELATIVE = Path("concepts") / "_drafts"

# Module-global throttle for inline sweep. Updated when sweep_expired
# completes successfully; remains stale on failure so retry happens.
_LAST_INLINE_SWEEP: float = 0.0


# Stopwords used by the slug fallback path. Small, English-biased — matches
# the conversational-English corpus the bot is built on.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "between",
        "but", "by", "can", "could", "did", "do", "does", "for", "from",
        "had", "has", "have", "how", "i", "if", "in", "into", "is", "it",
        "its", "let", "lets", "may", "me", "might", "my", "no", "not",
        "of", "on", "or", "our", "out", "should", "so", "than", "that",
        "the", "their", "them", "then", "there", "these", "this", "those",
        "to", "too", "us", "was", "we", "were", "what", "when", "where",
        "which", "who", "why", "will", "with", "would", "you", "your",
        "yours", "about", "any", "just", "much", "more", "some", "tell",
        "give", "show", "explain",
    }
)

_HEADING_RE = re.compile(r"^\s*#{1,2}\s+(.+?)\s*$", re.MULTILINE)
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class DraftResult:
    """Outcome of a ``create_draft`` call."""

    created: bool
    path: Path | None = None
    slug: str = ""
    auto_draft_id: str = ""
    skip_reason: str = ""


class DraftAmbiguityError(Exception):
    """Multiple drafts matched a UUID prefix — caller renders disambiguation."""

    def __init__(self, candidates: list[Path]):
        self.candidates = candidates
        super().__init__(f"{len(candidates)} drafts matched prefix")


# ---------------------------------------------------------------------------
# Predicate + slug derivation
# ---------------------------------------------------------------------------


def should_draft(user_text: str, response_text: str) -> bool:
    """Replicates ``engine.py:939-948`` predicate verbatim.

    Empirically unvalidated — gap-6.1 adds an F1 corpus benchmark.
    """
    if not response_text or len(response_text) <= DRAFT_THRESHOLD_CHARS:
        return False
    if user_text and user_text.strip().startswith("/"):
        return False
    haystack = response_text.lower()
    return any(sig in haystack for sig in ANALYSIS_MARKERS)


def _slugify(name: str) -> str:
    """UPPER-KEBAB-CASE slug, mirroring ``ExtractedEntity.slug`` style."""
    s = re.sub(r"^[\d]+[\.\-\s]+", "", name.strip())
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s.upper() or "UNTITLED"


def derive_slug(user_text: str, response_text: str) -> str:
    """Three-tier slug derivation.

    1. First H1/H2 heading in the response wins.
    2. Otherwise, first four non-stopword tokens of the user message.
    3. ``UNTITLED`` as a last resort.
    """
    if response_text:
        m = _HEADING_RE.search(response_text)
        if m:
            heading = m.group(1).strip().rstrip("#").strip()
            if heading:
                slug = _slugify(heading)
                if slug and slug != "UNTITLED":
                    return slug

    tokens: list[str] = []
    for tok in _TOKEN_RE.findall(user_text or ""):
        low = tok.lower()
        if low in _STOPWORDS:
            continue
        tokens.append(tok)
        if len(tokens) >= 4:
            break

    if tokens:
        slug = _slugify(" ".join(tokens))
        if slug and slug != "UNTITLED":
            return slug

    return "UNTITLED"


def _disambiguate_slug(base_slug: str, drafted_slugs: set[str]) -> str:
    """Suffix ``-2``, ``-3``, ... when a slug already exists in the session set.

    Preserves multi-turn refinement instead of silently dedup-dropping the
    second draft (R2 Bug E fix, plan §I9).
    """
    if base_slug not in drafted_slugs:
        return base_slug
    for n in range(2, 100):
        candidate = f"{base_slug}-{n}"
        if candidate not in drafted_slugs:
            return candidate
    # Extreme fallback — collision after 98 retries
    return f"{base_slug}-{int(time.time())}"


# ---------------------------------------------------------------------------
# Atomic file ops with cross-volume guard
# ---------------------------------------------------------------------------


def _atomic_move_or_replace(src: Path, dst: Path) -> None:
    """``os.replace`` where possible, ``shutil.move`` for cross-volume.

    Windows raises ``OSError(WinError 17)`` on a cross-device replace.
    We catch it (and the rare WinError 5 "access denied") and fall back to
    ``shutil.move`` so cross-volume vault layouts (junctions/symlinks) work.
    """
    try:
        os.replace(str(src), str(dst))
    except OSError as e:
        winerr = getattr(e, "winerror", None)
        if e.errno not in (17, 5) and winerr not in (17, 5):
            raise
        shutil.move(str(src), str(dst))


# ---------------------------------------------------------------------------
# Throttled inline sweep
# ---------------------------------------------------------------------------


def _maybe_inline_sweep(vault_dir: Path) -> None:
    """Run ``sweep_expired`` at most once per hour from inline call sites.

    OS-scheduler runs the daily cadence; this is a fallback for processes
    that stay up for days. Updates ``_LAST_INLINE_SWEEP`` only on success
    so failures retry on the next call.
    """
    global _LAST_INLINE_SWEEP
    now = time.time()
    if now - _LAST_INLINE_SWEEP < _INLINE_SWEEP_INTERVAL_SECONDS:
        return
    try:
        sweep_expired(vault_dir)
        _LAST_INLINE_SWEEP = now
    except Exception as e:  # noqa: BLE001
        print(f"[Drafter] inline sweep failed (non-blocking): {e}", flush=True)


# ---------------------------------------------------------------------------
# Draft creation
# ---------------------------------------------------------------------------


def _drafts_dir(vault_dir: Path) -> Path:
    return Path(vault_dir) / DRAFTS_RELATIVE


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _build_frontmatter(
    *,
    slug: str,
    auto_draft_id: str,
    session_id: str,
    turn_id: str,
    user_text: str,
    response_text: str,
) -> str:
    """Concept-page frontmatter + draft markers per §3.

    Matches ``entity_extractor.py:565-575`` schema with three additions:
    ``auto_draft_id``, ``draft`` tag, and ``status: draft``.
    """
    title = slug.replace("-", " ").title()
    summary = ""
    # Derive a brief summary from the first prose line (skip headings).
    for line in (response_text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        summary = stripped[:200].replace('"', "'")
        break
    if not summary:
        summary = title

    compiled_from = f"[[chat:{session_id}:{turn_id}]]"
    return (
        "---\n"
        f'aliases: ["{title}"]\n'
        "tags: [concept, auto-drafted, draft]\n"
        "status: draft\n"
        f"date: {_today()}\n"
        f'auto_draft_id: "{auto_draft_id}"\n'
        f"session_id: \"{session_id}\"\n"
        f"turn_id: \"{turn_id}\"\n"
        "compiled_from:\n"
        f'  - "{compiled_from}"\n'
        f'summary: "{summary}"\n'
        "---\n"
    )


def _build_body(slug: str, user_text: str, response_text: str) -> str:
    title = slug.replace("-", " ").title()
    return (
        f"# {title}\n\n"
        f"## Source turn\n\n"
        f"**User:** {user_text.strip()}\n\n"
        f"**Assistant:**\n\n"
        f"{response_text.strip()}\n"
    )


def create_draft(
    user_text: str,
    response_text: str,
    vault_dir: Path,
    *,
    session_id: str = "",
    turn_id: str = "",
    drafted_slugs: set[str] | None = None,
) -> DraftResult:
    """Atomically write a new draft to ``concepts/_drafts/`` and return result.

    Skips if ``should_draft`` is False. Throttled inline sweep runs first.
    Same-volume writes use ``os.replace``; cross-volume falls back to
    ``shutil.move`` (R2 Bug B).
    """
    if not should_draft(user_text, response_text):
        return DraftResult(created=False, skip_reason="predicate")

    vault_dir = Path(vault_dir)
    drafts_dir = _drafts_dir(vault_dir)
    drafts_dir.mkdir(parents=True, exist_ok=True)

    # Throttled inline sweep — at most once per hour
    _maybe_inline_sweep(vault_dir)

    base_slug = derive_slug(user_text, response_text)
    slug_set = drafted_slugs if drafted_slugs is not None else set()
    final_slug = _disambiguate_slug(base_slug, slug_set)
    slug_set.add(final_slug)

    auto_draft_id = uuid.uuid4().hex
    filename = f"{_today()}-{final_slug}.md"
    target = drafts_dir / filename

    frontmatter = _build_frontmatter(
        slug=final_slug,
        auto_draft_id=auto_draft_id,
        session_id=session_id,
        turn_id=turn_id,
        user_text=user_text,
        response_text=response_text,
    )
    body = _build_body(final_slug, user_text, response_text)
    contents = frontmatter + "\n" + body

    # Tempfile + atomic move — write to a sibling path so the move is
    # same-volume on the happy path. Cross-volume falls back to shutil.move.
    fd, tmp_path = tempfile.mkstemp(
        prefix=".draft-", suffix=".md.tmp", dir=str(drafts_dir),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(contents)
        _atomic_move_or_replace(Path(tmp_path), target)
    except Exception:
        # Clean up tempfile if anything went wrong before the move.
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return DraftResult(
        created=True,
        path=target,
        slug=final_slug,
        auto_draft_id=auto_draft_id,
    )


# ---------------------------------------------------------------------------
# Lookup — full UUID + 8-char prefix
# ---------------------------------------------------------------------------


_AUTO_ID_RE = re.compile(
    r'^auto_draft_id:\s*"?([0-9a-f]+)"?\s*$', re.MULTILINE,
)


def find_draft_by_id(auto_draft_id: str, vault_dir: Path) -> Path | None:
    """Return draft path by full UUID or 8+ char prefix.

    Returns ``None`` on no match. Raises ``DraftAmbiguityError`` on >1
    match so the router renders a disambiguation reply (R2 UUID UX, §I10).
    """
    if not auto_draft_id:
        return None

    drafts_dir = _drafts_dir(Path(vault_dir))
    if not drafts_dir.exists():
        return None

    auto_id_lower = auto_draft_id.lower()
    matches: list[Path] = []
    for p in drafts_dir.iterdir():
        if p.suffix != ".md" or not p.is_file():
            continue
        try:
            head = p.read_text(encoding="utf-8")[:1024]
        except OSError:
            continue
        m = _AUTO_ID_RE.search(head)
        if not m:
            continue
        full_id = m.group(1).lower()
        if full_id == auto_id_lower or full_id.startswith(auto_id_lower):
            matches.append(p)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise DraftAmbiguityError(matches)
    return None


# ---------------------------------------------------------------------------
# Accept / diff
# ---------------------------------------------------------------------------


def _strip_draft_markers(content: str) -> str:
    """Replace draft frontmatter fields with the live concept ones."""
    content = re.sub(
        r'^auto_draft_id:.*$\n?', "", content, count=1, flags=re.MULTILINE,
    )
    content = re.sub(
        r'^session_id:.*$\n?', "", content, count=1, flags=re.MULTILINE,
    )
    content = re.sub(
        r'^turn_id:.*$\n?', "", content, count=1, flags=re.MULTILINE,
    )
    content = content.replace("status: draft", "status: current", 1)
    content = content.replace(
        "tags: [concept, auto-drafted, draft]",
        "tags: [concept, auto-compiled, auto-drafted]",
        1,
    )
    return content


def accept_draft(
    auto_draft_id: str,
    vault_dir: Path,
    memory_dir: Path | None = None,
) -> dict[str, Any]:
    """Move the draft into ``concepts/`` and run real entity compilation.

    Synchronous (compile is ~200-500ms) — user typed and is waiting.
    Returns a dict with ``status``, ``path``, ``connections``,
    ``contradictions``. ``status`` is ``filed`` on success,
    ``not_found`` if the draft is gone, ``error`` on compile failure.
    """
    vault_dir = Path(vault_dir)
    try:
        draft_path = find_draft_by_id(auto_draft_id, vault_dir)
    except DraftAmbiguityError:
        raise

    if draft_path is None:
        return {
            "status": "not_found",
            "auto_draft_id": auto_draft_id,
            "path": "",
            "connections": [],
            "contradictions": [],
        }

    concepts_dir = vault_dir / "concepts"
    concepts_dir.mkdir(parents=True, exist_ok=True)

    # Strip the draft markers and rewrite in place before moving so the
    # destination file is a clean concept page.
    try:
        original = draft_path.read_text(encoding="utf-8")
        draft_path.write_text(_strip_draft_markers(original), encoding="utf-8")
    except OSError:
        return {
            "status": "error",
            "auto_draft_id": auto_draft_id,
            "path": str(draft_path),
            "connections": [],
            "contradictions": [],
            "error": "read/write failed",
        }

    # Filename strategy: drop the date prefix so it's a normal concept page.
    target_name = draft_path.name
    if re.match(r"^\d{4}-\d{2}-\d{2}-", target_name):
        target_name = target_name.split("-", 3)[-1]
    target = concepts_dir / target_name

    try:
        _atomic_move_or_replace(draft_path, target)
    except OSError as e:
        return {
            "status": "error",
            "auto_draft_id": auto_draft_id,
            "path": str(draft_path),
            "connections": [],
            "contradictions": [],
            "error": f"move failed: {e}",
        }

    # Real compile — best-effort. Report keys present even on failure.
    connections: list[str] = []
    contradictions: list[Any] = []
    pages_created: list[str] = []
    pages_updated: list[str] = []
    try:
        from entity_extractor import compile_entities, extract_entities_heuristic

        content = target.read_text(encoding="utf-8")
        entities = extract_entities_heuristic(content, str(target))
        report = compile_entities(
            entities, str(target), vault_dir, memory_dir, event_type="file",
        )
        connections = list(report.connections_created)
        contradictions = list(report.contradictions_found)
        pages_created = list(report.pages_created)
        pages_updated = list(report.pages_updated)
    except Exception as e:  # noqa: BLE001
        print(f"[Drafter] compile_entities failed: {e}", flush=True)

    return {
        "status": "filed",
        "auto_draft_id": auto_draft_id,
        "path": str(target),
        "connections": connections,
        "contradictions": contradictions,
        "pages_created": pages_created,
        "pages_updated": pages_updated,
    }


def diff_draft(auto_draft_id: str, vault_dir: Path) -> dict[str, Any]:
    """Return a read-only summary of the draft. No mutation."""
    vault_dir = Path(vault_dir)
    draft_path = find_draft_by_id(auto_draft_id, vault_dir)
    if draft_path is None:
        return {
            "status": "not_found",
            "auto_draft_id": auto_draft_id,
            "path": "",
            "preview": "",
        }
    try:
        content = draft_path.read_text(encoding="utf-8")
    except OSError as e:
        return {
            "status": "error",
            "auto_draft_id": auto_draft_id,
            "path": str(draft_path),
            "preview": "",
            "error": str(e),
        }

    # Split frontmatter from body for a cleaner preview.
    body = content
    fm_match = re.match(r"^---\n.*?\n---\n?", content, re.DOTALL)
    if fm_match:
        body = content[fm_match.end():]

    preview = body[:1500].rstrip()
    if len(body) > 1500:
        preview += "\n\n…(truncated)"
    return {
        "status": "ok",
        "auto_draft_id": auto_draft_id,
        "path": str(draft_path),
        "preview": preview,
    }


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


def sweep_expired(
    vault_dir: Path, ttl_seconds: int = DRAFT_TTL_SECONDS,
) -> list[Path]:
    """Delete drafts older than ``ttl_seconds``. Skip files modified in the
    last ``SWEEP_INFLIGHT_GUARD_SECONDS`` to avoid racing with create_draft.
    """
    drafts_dir = _drafts_dir(Path(vault_dir))
    if not drafts_dir.exists():
        return []
    removed: list[Path] = []
    now = time.time()
    for p in list(drafts_dir.iterdir()):
        if p.suffix != ".md" or not p.is_file():
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        age = now - mtime
        if age < SWEEP_INFLIGHT_GUARD_SECONDS:
            continue
        if age > ttl_seconds:
            try:
                p.unlink()
                removed.append(p)
            except OSError as e:
                print(f"[Drafter] sweep unlink failed: {e}", flush=True)
    return removed


# ---------------------------------------------------------------------------
# Footer construction
# ---------------------------------------------------------------------------


def _build_footer(slug: str, auto_draft_id: str) -> tuple[str, list[Any]]:
    """Return a (footer_str, components_list) tuple for the engine.

    Footer text is medium-agnostic; adapters render it (§I8). The
    components list carries Accept / Diff / Ignore buttons. The full
    UUID rides on the custom_id; users can also tap a button. CLI / web
    typers can use ``/file accept <8-char-prefix>``.
    """
    short = auto_draft_id[:8]
    footer = (
        f"Drafted as `{slug}` (id `{short}`). "
        f"Reply `/file accept {short}` to file, `/file diff {short}` to preview."
    )
    try:
        from models import MessageComponent

        components: list[Any] = [
            MessageComponent(
                label="Accept",
                custom_id=f"concept_accept:{auto_draft_id}",
                style="success",
            ),
            MessageComponent(
                label="Diff",
                custom_id=f"concept_diff:{auto_draft_id}",
                style="primary",
            ),
            MessageComponent(
                label="Ignore",
                custom_id=f"concept_ignore:{auto_draft_id}",
                style="secondary",
            ),
        ]
    except Exception:  # noqa: BLE001
        components = []
    return footer, components


# ---------------------------------------------------------------------------
# Engine entrypoint
# ---------------------------------------------------------------------------


def maybe_draft_and_footer(
    user_text: str,
    response_text: str,
    vault_dir: Path,
    *,
    session_id: str,
    turn_id: str,
    drafted_slugs: set[str],
) -> tuple[str, list[Any]]:
    """Engine entrypoint — returns ``(footer_str, components_list)``.

    Always returns a tuple; on any internal failure, returns ``("", [])``
    so the engine yield site never raises. Mutates ``drafted_slugs`` in
    place via ``create_draft`` so the per-session set is preserved.
    """
    try:
        result = create_draft(
            user_text,
            response_text,
            vault_dir,
            session_id=session_id,
            turn_id=turn_id,
            drafted_slugs=drafted_slugs,
        )
        if not result.created:
            return ("", [])
        return _build_footer(result.slug, result.auto_draft_id)
    except Exception as e:  # noqa: BLE001
        print(f"[Drafter] maybe_draft_and_footer failed: {e}", flush=True)
        return ("", [])
