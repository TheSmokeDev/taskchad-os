"""Living Mind Act 3 — episode primitives (the self's autobiography).

Every session that produces a meaningful flush leaves a structured narrative
episode in ``{memory_dir}/episodes/``. The writer consumes ONLY the flush
LLM's distillation (``response_text``) plus the context FILENAME — its
signature physically cannot receive transcript text (privacy by
construction; raw chat transcripts never enter episodes).

Public API:
    derive_flush_meta(context_filename, *, now=None) -> FlushMeta
    parse_flush_sections(response_text) -> dict[str, str]
    write_episode_from_flush(memory_dir, *, context_filename, response_text,
        now=None, settings=None, persona_id=None) -> (EpisodeWriteStatus, Path | None)
    list_open_episodes(memory_dir, *, days, now=None) -> list[Path]
    render_episodes_digest(paths, *, settings=None) -> str
    mark_episodes_consolidated(paths, *, now=None) -> int
        (raises EpisodeFlipError after the loop when real I/O failures occur)
    read_episode_frontmatter(path) -> dict[str, str]

Design invariants (sibling of living_memory.py):
    - Zero LLM calls — parsing is deterministic and heading-tolerant.
    - Episode key is LIFECYCLE-unique: the hook-run timestamp embedded in the
      context filename. Chat session keys are stable composites that get
      reused after /clear, so they CANNOT key episodes (R1 B2). A same-key
      re-flush is a same-lifecycle retry by construction and appends an
      ``## Update`` block instead of a new file.
    - Episodes are insert-only history. The dream pass flips frontmatter
      ``status: open -> consolidated`` (physical state, Rule 2 — never a
      sidecar registry in a state file).
    - File writes are atomic (tmp + os.replace) under shared.file_lock.
    - Langfuse spans only via the runtime-owned accessor (Rule 3); failure
      never breaks the flush or the dream.
"""

from __future__ import annotations

import enum
import hashlib
import re
from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime
from pathlib import Path

from shared import atomic_write_text as _atomic_write
from shared import file_lock

EPISODES_DIR_NAME = "episodes"

# Fixed heading vocabulary the flush prompt emits. Structural constant, not a
# tunable knob (tunables live in config.get_episode_settings — Rule 1).
SECTION_HEADINGS = ("Summary", "Key Decisions", "Open Threads", "Texture")

_KNOWN_H2_RE = re.compile(
    r"^##\s*(Summary|Key Decisions|Open Threads|Texture)\b[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
_H2_DEMOTE_RE = re.compile(r"^## ", re.MULTILINE)
_STATUS_LINE_RE = re.compile(r"^status:\s*(\S+)\s*$", re.MULTILINE)
_CONSOLIDATED_AT_LINE_RE = re.compile(r"^consolidated_at:[^\n]*\n?", re.MULTILINE)
_FRONTMATTER_DATE_RE = re.compile(r"^date: .+$", re.MULTILINE)
_FRONTMATTER_BLOCK_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_TIMESTAMP_DATE_RE = re.compile(r"^\d{8}$")
_TIMESTAMP_TIME_RE = re.compile(r"^\d{6}$")

_PLATFORM_SURFACE_RE = re.compile(r"^(telegram|discord|slack|whatsapp|web|cli)-")


# =============================================================================
# Langfuse helper (lazy — copied shape from living_memory.py, Rule 3)
# =============================================================================


def _langfuse_span(name: str):
    """Return a context manager for a Langfuse span, or a no-op if disabled.

    Never breaks runtime — any import / auth failure falls back to no-op.
    Langfuse is reached only through the runtime-owned accessor
    ``langfuse_setup.get_observation_client()`` via module-attribute lookup
    (Rule 3) so a monkeypatch on the accessor propagates to this call site.
    """
    try:
        from runtime import langfuse_setup

        client = langfuse_setup.get_observation_client()
        if client is None:
            return _NoOpSpan()
        return client.start_as_current_observation(name=name)
    except Exception:
        return _NoOpSpan()


class _NoOpSpan:
    """Context manager that does nothing. Returned when Langfuse is off."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def update(self, **_kwargs):
        pass


def _safe_update(span, **metadata) -> None:
    """Apply metadata to a span without ever raising."""
    try:
        span.update(metadata=metadata)
    except Exception:
        pass


# =============================================================================
# Sanitizer (local copy of the _sanitize_observation_text shape — do NOT
# import private helpers across modules)
# =============================================================================


def _sanitize_line(text: str, max_chars: int) -> str:
    """Deterministic single-line sanitizer for frontmatter summary fields.

    Strips control chars/newlines, backticks and quotes, collapses
    whitespace, trims to ``max_chars`` at a word boundary.
    """
    s = str(text or "")
    s = s.replace("`", "").replace('"', "").replace("'", "")
    s = "".join(" " if (ord(ch) < 32 or ord(ch) == 127) else ch for ch in s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_chars:
        cut = s[:max_chars]
        boundary = cut.rfind(" ")
        if boundary > 0:
            cut = cut[:boundary]
        s = cut.strip()
    return s


# =============================================================================
# Filename metadata
# =============================================================================


@dataclass(frozen=True)
class FlushMeta:
    """Lifecycle metadata derived purely from a flush context FILENAME."""

    session_id: str
    surface: str
    sid8: str
    lifecycle_ts: str
    episode_date: str
    time_token: str


class EpisodeWriteStatus(enum.Enum):
    """Outcome of write_episode_from_flush — each path is distinct."""

    WRITTEN = "written"
    UPDATED = "updated"
    SKIPPED_MIN_CHARS = "skipped_min_chars"
    SKIPPED_DAY_CAP = "skipped_day_cap"


def derive_flush_meta(
    context_filename: str, *, now: datetime | None = None
) -> FlushMeta:
    """Parse session/surface/lifecycle metadata from a context filename.

    Pure filename parse — never reads the file, never raises. ``now`` is
    resolved in the body (Rule 1) and used ONLY on the malformed-stem
    fallback.

    The lifecycle identifier is the hook-run timestamp BOTH hooks always
    embed: ``session-flush-{safe_id}-{YYYYMMDD}-{HHMMSS}.md`` /
    ``flush-context-{safe_id}-{YYYYMMDD}-{HHMMSS}.md``. Chat session keys
    are channel-stable and reused after /clear, so the timestamp — generated
    once per hook invocation — is what makes the episode key
    lifecycle-unique (R1 B2).
    """
    if now is None:
        now = datetime.now()
    stem = Path(str(context_filename)).stem
    parts = stem.split("-")

    # Session id parse — mirrors memory_flush._extract_session_id.
    if len(parts) >= 5:
        session_id = "-".join(parts[2:-2]) or "unknown"
    else:
        session_id = "unknown"

    # Lifecycle parse: trailing YYYYMMDD-HHMMSS (the shape both hooks emit).
    if (
        len(parts) >= 5
        and _TIMESTAMP_DATE_RE.fullmatch(parts[-2])
        and _TIMESTAMP_TIME_RE.fullmatch(parts[-1])
    ):
        date_part, time_part = parts[-2], parts[-1]
        lifecycle_ts = f"{date_part}-{time_part}"
        episode_date = f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]}"
        time_token = time_part
    else:
        # Malformed-stem fallback (pure defense; deterministic, never raises):
        # same stem -> same key within a day.
        time_token = "u" + hashlib.sha1(stem.encode("utf-8")).hexdigest()[:7]
        episode_date = now.strftime("%Y-%m-%d")
        lifecycle_ts = "unknown-" + time_token

    # Surface table (deterministic, exact — uuid hex cannot start with a
    # platform token + dash: 'l', 'i', 'w', 's', 't', 'g', 'm' are non-hex).
    if stem.startswith("flush-context"):
        surface = "compact"
    else:
        platform_match = _PLATFORM_SURFACE_RE.match(session_id)
        surface = platform_match.group(1) if platform_match else "code"

    sid8 = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:8]

    return FlushMeta(
        session_id=session_id,
        surface=surface,
        sid8=sid8,
        lifecycle_ts=lifecycle_ts,
        episode_date=episode_date,
        time_token=time_token,
    )


# =============================================================================
# Section parsing (provider-tolerant)
# =============================================================================


def parse_flush_sections(response_text: str) -> dict[str, str]:
    """Tolerant H2 split of the flush LLM response.

    Keys are a subset of SECTION_HEADINGS. Preamble text before the first
    recognized heading joins Summary; NO recognized headings -> the entire
    text lands under Summary (provider-variance fallback). Unrecognized H2
    sections stay verbatim inside whichever known section they follow
    (never dropped). Never raises on arbitrary text.
    """
    text = str(response_text or "")
    matches = list(_KNOWN_H2_RE.finditer(text))
    if not matches:
        return {"Summary": text.strip()}

    sections: dict[str, str] = {}

    def _add(key: str, content: str) -> None:
        content = content.strip()
        if not content:
            sections.setdefault(key, sections.get(key, ""))
            return
        if sections.get(key):
            sections[key] = sections[key] + "\n\n" + content
        else:
            sections[key] = content

    preamble = text[: matches[0].start()].strip()
    if preamble:
        _add("Summary", preamble)

    for i, m in enumerate(matches):
        raw_name = m.group(1)
        key = next(s for s in SECTION_HEADINGS if s.lower() == raw_name.lower())
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        _add(key, text[start:end])

    # Drop empty placeholder keys created by _add's setdefault path.
    return {k: v for k, v in sections.items() if v}


def _render_section_blocks(sections: dict[str, str]) -> str:
    """Render parsed sections as ordered ``## <Section>`` blocks.

    Only sections with content are rendered; Summary is always present when
    any content exists (the parser routes fallback text there).
    """
    blocks = []
    for name in SECTION_HEADINGS:
        content = sections.get(name, "").strip()
        if content:
            blocks.append(f"## {name}\n\n{content}")
    return "\n\n".join(blocks)


def _split_frontmatter(content: str) -> tuple[str, str]:
    """Split a markdown file into (frontmatter_block, rest).

    ``frontmatter_block`` includes the surrounding ``---`` fences. Returns
    ("", content) when no frontmatter exists — manipulations stay scoped to
    the frontmatter so body text like 'status: open' is never touched.
    """
    m = _FRONTMATTER_BLOCK_RE.match(content)
    if not m:
        return "", content
    end = m.end()
    return content[:end], content[end:]


# _atomic_write is the canonical shared.atomic_write_text (imported above) —
# the local clone was consolidated in the 2026-07-07 framework refactor.


# =============================================================================
# Writer
# =============================================================================


def write_episode_from_flush(
    memory_dir: Path,
    *,
    context_filename: str,
    response_text: str,
    now: datetime | None = None,
    settings=None,
    persona_id: str | None = None,
) -> tuple[EpisodeWriteStatus, Path | None]:
    """Write (or same-lifecycle-update) an episode from a flush response.

    The only permitted inputs to episode content are ``response_text`` (the
    flush LLM's distillation) and code-generated metadata from the context
    FILENAME — the transcript physically cannot reach this function.

    ``now`` feeds the ``## Update`` clock, the frontmatter ``date:`` refresh,
    and the malformed-stem fallback — NEVER the filename of a well-formed
    stem (filename date = lifecycle START date, so one lifecycle crossing
    midnight stays in ONE file).
    """
    if settings is None:
        from config import get_episode_settings

        settings = get_episode_settings()
    if now is None:
        now = datetime.now()

    meta = derive_flush_meta(context_filename, now=now)
    sections = parse_flush_sections(response_text)
    body = _render_section_blocks(sections)

    with _langfuse_span("episode_write") as span:
        if len(body.strip()) < settings.min_chars:
            _safe_update(
                span,
                status=EpisodeWriteStatus.SKIPPED_MIN_CHARS.value,
                surface=meta.surface,
                bytes_written=0,
            )
            return (EpisodeWriteStatus.SKIPPED_MIN_CHARS, None)

        episodes_dir = memory_dir / EPISODES_DIR_NAME
        episodes_dir.mkdir(parents=True, exist_ok=True)
        path = episodes_dir / (
            f"{meta.episode_date}-{meta.surface}-{meta.sid8}-{meta.time_token}.md"
        )
        today = now.strftime("%Y-%m-%d")

        with file_lock(path, timeout=5.0):
            if path.exists():
                # Same key = SAME LIFECYCLE by construction (retry/double-spawn):
                # append an Update block, refresh date, re-open status.
                content = path.read_text(encoding="utf-8")
                frontmatter, rest = _split_frontmatter(content)
                if frontmatter:
                    frontmatter = _CONSOLIDATED_AT_LINE_RE.sub("", frontmatter)
                    frontmatter = _STATUS_LINE_RE.sub(
                        "status: open", frontmatter, count=1
                    )
                    frontmatter = _FRONTMATTER_DATE_RE.sub(
                        f"date: {today}", frontmatter, count=1
                    )
                demoted = _H2_DEMOTE_RE.sub("### ", body)
                update_block = (
                    f"\n\n## Update ({now.strftime('%H:%M')})\n\n{demoted}\n"
                )
                bytes_written = _atomic_write(
                    path, frontmatter + rest.rstrip("\n") + update_block
                )
                _safe_update(
                    span,
                    status=EpisodeWriteStatus.UPDATED.value,
                    surface=meta.surface,
                    bytes_written=bytes_written,
                )
                return (EpisodeWriteStatus.UPDATED, path)

            # NEW file: per-day cap counted against the lifecycle-date prefix
            # (physical files, Rule 2). Same-key updates above are exempt.
            existing_for_day = len(
                list(episodes_dir.glob(f"{meta.episode_date}-*.md"))
            )
            if existing_for_day >= settings.max_per_day:
                _safe_update(
                    span,
                    status=EpisodeWriteStatus.SKIPPED_DAY_CAP.value,
                    surface=meta.surface,
                    bytes_written=0,
                )
                return (EpisodeWriteStatus.SKIPPED_DAY_CAP, None)

            summary_line = _sanitize_line(sections.get("Summary", ""), 100)
            persona_line = f"persona_id: {persona_id}\n" if persona_id else ""
            content = (
                "---\n"
                "tags: [system, memory, living-mind]\n"
                "status: open\n"
                f"date: {today}\n"
                f'session_id: "{meta.session_id}"\n'
                f"surface: {meta.surface}\n"
                f'lifecycle: "{meta.lifecycle_ts}"\n'
                f'summary: "{summary_line}"\n'
                f"{persona_line}"
                "---\n"
                "\n"
                f"# Episode: {meta.episode_date} — {meta.surface}\n"
                "\n"
                f"{body}\n"
            )
            bytes_written = _atomic_write(path, content)
            _safe_update(
                span,
                status=EpisodeWriteStatus.WRITTEN.value,
                surface=meta.surface,
                bytes_written=bytes_written,
            )
            return (EpisodeWriteStatus.WRITTEN, path)


# =============================================================================
# Readers / dream consumers
# =============================================================================


def read_episode_frontmatter(path: Path) -> dict[str, str]:
    """Minimal frontmatter parser for episode files (vault_lint shape)."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    m = _FRONTMATTER_BLOCK_RE.match(content)
    if not m:
        return {}
    fm_text = m.group(1)
    out: dict[str, str] = {}
    for key in (
        "status",
        "date",
        "surface",
        "session_id",
        "lifecycle",
        "consolidated_at",
        # Living Mind Act 4: the session-opening brief renders the episode's
        # frontmatter summary — additive key, existing consumers unchanged.
        "summary",
        # Persona learning loop: persona-attributed episodes carry the
        # persona_id that produced them — additive, omitted for main.
        "persona_id",
    ):
        km = re.search(rf"^{key}:\s*(.+?)\s*$", fm_text, re.MULTILINE)
        if km:
            out[key] = km.group(1).strip().strip('"')
    return out


def list_open_episodes(
    memory_dir: Path, *, days: int, now: datetime | None = None
) -> list[Path]:
    """List ``status: open`` episodes whose ``date:`` falls in the window.

    Window math mirrors heartbeat._blocker_day_in_window: a day counts while
    ``0 <= (today - day).days <= days`` (future-dated never counts). Missing
    ``episodes/`` dir -> ``[]`` (fail-open; existing vaults need zero setup).
    Sorted newest-first by date then name.
    """
    if now is None:
        now = datetime.now()
    episodes_dir = memory_dir / EPISODES_DIR_NAME
    if not episodes_dir.exists():
        return []
    today = now.date()
    keyed: list[tuple[str, str, Path]] = []
    for path in episodes_dir.glob("*.md"):
        try:
            fm = read_episode_frontmatter(path)
            if fm.get("status") != "open":
                continue
            try:
                day = _date.fromisoformat(fm.get("date", ""))
            except (TypeError, ValueError):
                continue
            delta = (today - day).days
            if not (0 <= delta <= days):
                continue
            keyed.append((fm.get("date", ""), path.name, path))
        except Exception:
            continue
    keyed.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [path for _, _, path in keyed]


def list_episodes_since(memory_dir: Path, *, since: datetime) -> list[Path]:
    """List episodes written strictly after ``since`` — status-AGNOSTIC.

    The session-opening brief query (Living Mind Act 4). Unlike
    ``list_open_episodes`` this does NOT filter ``status``: the overnight
    dream may consolidate exactly the episodes the operator most needs to
    hear about. Recency test: frontmatter ``lifecycle`` ("YYYYMMDD-HHMMSS")
    parsed to a naive instant, kept when **strictly >** ``since``. Malformed
    lifecycle (the Act 3 ``unknown-…`` fallback) falls back to frontmatter
    ``date`` (kept when ``date >= since.date()``); both unparseable -> skip.
    Missing/non-dir ``episodes/`` -> ``[]`` (fail-open, the
    ``list_open_episodes`` contract). Sorted newest-first by (parsed instant
    or date, name) descending. No ``now`` parameter — pure comparison
    against ``since``.
    """
    episodes_dir = Path(memory_dir) / EPISODES_DIR_NAME
    try:
        if not episodes_dir.is_dir():
            return []
    except OSError:
        return []
    keyed: list[tuple[datetime, str, Path]] = []
    for path in episodes_dir.glob("*.md"):
        try:
            fm = read_episode_frontmatter(path)
            if not fm:
                continue
            instant: datetime | None = None
            try:
                instant = datetime.strptime(
                    fm.get("lifecycle", ""), "%Y%m%d-%H%M%S"
                )
            except (TypeError, ValueError):
                instant = None
            if instant is not None:
                if instant <= since:
                    continue
            else:
                try:
                    day = _date.fromisoformat(fm.get("date", ""))
                except (TypeError, ValueError):
                    continue
                if day < since.date():
                    continue
                instant = datetime.combine(day, datetime.min.time())
            keyed.append((instant, path.name, path))
        except Exception:
            continue
    keyed.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [path for _, _, path in keyed]


def render_episodes_digest(paths: list[Path], *, settings=None) -> str:
    """Render a capped digest of episode bodies for the dream prompt.

    Pure: newest-first as given, at most ``dream_max_files`` files, per-file
    excerpt capped at ``dream_max_chars_per`` chars of the body (frontmatter
    stripped), total hard-capped at ``dream_max_total_chars``. Empty paths
    -> "".
    """
    if settings is None:
        from config import get_episode_settings

        settings = get_episode_settings()
    if not paths:
        return ""
    parts: list[str] = []
    total = 0
    for path in paths[: settings.dream_max_files]:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        _, body = _split_frontmatter(content)
        excerpt = body.strip()[: settings.dream_max_chars_per]
        block = f"### {path.stem}\n\n{excerpt}"
        remaining = settings.dream_max_total_chars - total
        if remaining <= 0:
            break
        if len(block) > remaining:
            parts.append(block[:remaining])
            break
        parts.append(block)
        total += len(block) + 2  # account for the join separator
    return "\n\n".join(parts)


class EpisodeFlipError(RuntimeError):
    """Real I/O failure(s) during the consolidation flip (collect-then-raise).

    The flip loop still flips every path it can, then raises ONE summarizing
    error so a single bad file never blocks the rest. ``flipped`` carries the
    partial success count (physical truth, Rule 2 — those files now say
    ``status: consolidated``); every path in ``failed_paths`` stays
    ``status: open`` and is re-fed by the next dream run. memory_dream's
    non-fatal warning wrapper reads ``flipped`` via getattr — keep the
    attribute name stable.
    """

    def __init__(self, flipped: int, failures: list[tuple[Path, Exception]]):
        self.flipped = flipped
        self.failed_paths = [path for path, _exc in failures]
        shown = [
            f"{path.name} ({exc.__class__.__name__}: {exc})"
            for path, exc in failures[:5]
        ]
        if len(failures) > 5:
            shown.append(f"and {len(failures) - 5} more")
        super().__init__(
            f"{len(failures)} episode flip(s) failed "
            f"({flipped} flipped): " + "; ".join(shown)
        )


def mark_episodes_consolidated(
    paths: list[Path], *, now: datetime | None = None
) -> int:
    """Flip frontmatter ``status: open -> consolidated`` on the given files.

    Adds/replaces ``consolidated_at: YYYY-MM-DD`` after the status line.
    Idempotent: already-consolidated files, missing files, and files without
    frontmatter are benign skips — silent, counted as not-flipped. Physical
    state in the episode file is the record (Rule 2) — no state file
    registry exists. Returns the flipped count.

    Real I/O failures (lock timeout, read/write/replace errors) are NOT
    swallowed into the skip count: the loop flips everything it can, then
    raises EpisodeFlipError summarizing the failed paths so the caller's
    warning path fires (memory_dream wraps this call non-fatally per R1 M1).
    Failed files stay ``status: open`` — the next dream run re-feeds them.
    """
    if now is None:
        now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    flipped = 0
    failures: list[tuple[Path, Exception]] = []
    with _langfuse_span("episode_consolidate_flip") as span:
        for path in paths:
            try:
                if not path.exists():
                    continue
                with file_lock(path, timeout=5.0):
                    content = path.read_text(encoding="utf-8")
                    frontmatter, rest = _split_frontmatter(content)
                    if not frontmatter:
                        continue
                    status_match = _STATUS_LINE_RE.search(frontmatter)
                    if not status_match or status_match.group(1) != "open":
                        continue
                    frontmatter = _CONSOLIDATED_AT_LINE_RE.sub("", frontmatter)
                    frontmatter = _STATUS_LINE_RE.sub(
                        f"status: consolidated\nconsolidated_at: {today}",
                        frontmatter,
                        count=1,
                    )
                    _atomic_write(path, frontmatter + rest)
                    flipped += 1
            except Exception as exc:
                failures.append((path, exc))
        _safe_update(
            span, flipped=flipped, requested=len(paths), failed=len(failures)
        )
        if failures:
            raise EpisodeFlipError(flipped, failures)
    return flipped
