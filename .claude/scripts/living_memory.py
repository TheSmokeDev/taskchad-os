"""Living Mind Phase 1 — cross-session scratchpad (WORKING.md) primitives.

Reads, writes, and ages `vault/memory/WORKING.md` — the bot's live state
between sessions. Gary Tan's "curated middle tier": small, always-in-context,
explicitly aged, never hard-deleted.

Public API:
    read_working_memory(memory_dir)          -> WorkingMemoryData
    append_open_thread(memory_dir, subject, status)
    append_hypothesis(memory_dir, hypothesis, evidence)
    append_question(memory_dir, question)
    append_heartbeat_observation(memory_dir, group, subject, detail="")
        -> ObservationAppendStatus
    archive_stale_working_items(memory_dir, days=7, observation_days=None)
        -> ArchiveReport
    append_open_threads_from_flush(memory_dir, flush_md) -> int

Design invariants:
    - Zero LLM calls — all extraction/dedup/aging is regex + date math.
    - Archive is insert-only. Active sections lose bullets; Archived (Cold)
      gains them. Dream Phase 4 prune handles eventual archive truncation.
    - File writes are atomic (tmp + os.replace) under cross-platform file lock.
    - Langfuse spans wrap each public write; failure never breaks runtime.
    - Manual edits preserved — writer anchors on section headings, does not
      regenerate file content.
"""

from __future__ import annotations

import enum
import os
import re
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime
from pathlib import Path

WORKING_FILE_NAME = "WORKING.md"

# Fixed section order — writers never add new sections (forward-compat for
# Phase 2-4). If a section is missing, it's reconstructed from the template.
ACTIVE_SECTIONS = ("Open Threads", "Active Hypotheses", "Unresolved Questions")
ALL_SECTIONS = (
    "Open Threads",
    "Active Hypotheses",
    "Unresolved Questions",
    "Heartbeat Observations",
    "Archived (Cold)",
)

# Caps (see plan: Open Threads 10, others 5, archive unbounded)
OPEN_THREADS_CAP = int(os.getenv("WORKING_MEMORY_OPEN_THREADS_CAP", "10"))
OTHER_ACTIVE_CAP = int(os.getenv("WORKING_MEMORY_OTHER_CAP", "5"))
# WORKING_MEMORY_MAX_FLUSH_THREADS (default 3) and WORKING_MEMORY_DEDUP_DAYS
# (default 3) are resolved at CALL TIME inside _extract_thread_candidates and
# _dedup_match respectively (Rule 1 — no import-time binding for tunable knobs).

# Regex — single-line bullet with leading date: `- [YYYY-MM-DD] content`
_BULLET_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2})\] (.+)$")
_ARCHIVED_BULLET_RE = re.compile(
    r"^- \[archived \d{4}-\d{2}-\d{2}\] \(was: (\d{4}-\d{2}-\d{2})\) (.+)$"
)
_FRONTMATTER_DATE_RE = re.compile(r"^date: .+$", re.MULTILINE)


@dataclass(frozen=True)
class WorkingMemoryData:
    """Parsed view of WORKING.md — named distinct from cognition.working_memory.WorkingMemory.

    Contains the raw content plus per-section bullet lists. Empty sections
    are represented as empty lists, not missing keys.
    """

    open_threads: list[str] = field(default_factory=list)
    active_hypotheses: list[str] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)
    heartbeat_observations: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    raw_content: str = ""
    exists: bool = False


@dataclass
class ArchiveReport:
    archived_count: int = 0
    days: int = 7
    sections_touched: list[str] = field(default_factory=list)


class ObservationAppendStatus(enum.Enum):
    """Outcome of append_heartbeat_observation — each path is distinct.

    A bare ``0`` return conflated dedup-skips with sanitize-drops and made the
    heartbeat report lie (Act 2 R1 minor 3). ``EMPTY_AFTER_SANITIZE`` is
    decided before any file I/O; only ``WRITTEN`` consumes the heartbeat's
    per-run write budget.
    """

    WRITTEN = "written"
    DEDUP = "dedup"
    EMPTY_AFTER_SANITIZE = "empty_after_sanitize"


# =============================================================================
# Langfuse helper (lazy — no hard dep on runtime being importable)
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
# Template & parsing
# =============================================================================


_DEFAULT_TEMPLATE = """---
tags: [system, memory, working]
status: current
date: {today}
summary: "Live cross-session scratchpad \u2014 open threads, hypotheses, questions. Aged weekly by dream cycle."
priority: P1
---

# WORKING.md \u2014 Cross-Session Scratchpad

_Updated automatically by session-end hook and dream cycle. Manual edits allowed (preserved through aging)._

## Open Threads

<!-- Things the bot was actively working on. Each item: `- [YYYY-MM-DD] <subject> \u2014 <status>`. Max 10 active. Older items age to Archived (Cold). -->


## Active Hypotheses

<!-- Working beliefs the bot formed but hasn't confirmed. Each item: `- [YYYY-MM-DD] <hypothesis> \u2014 evidence: <pointer>`. -->


## Unresolved Questions

<!-- Things the bot asked the user or itself that remain open. Each item: `- [YYYY-MM-DD] <question>`. -->


## Heartbeat Observations

<!-- Ambient observations written by the heartbeat (Living Mind Act 2). Each item: `- [YYYY-MM-DD] [group] <subject> — <detail>`. Counts/dates/operator-owned labels only — never external free text. Capped, deduped, aged automatically. -->


## Archived (Cold)

<!-- Items aged out of active sections by dream cycle. NEVER hard-deleted. Chronological, newest first. Format: `- [archived YYYY-MM-DD] (was: <original_date>) <content>`. -->
"""


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _bootstrap_file(path: Path) -> str:
    """Write the default template to `path` and return its content."""
    content = _DEFAULT_TEMPLATE.format(today=_today_str())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return content


def _split_sections(content: str) -> dict[str, list[str]]:
    """Parse WORKING.md content into {section_name: [bullet_lines]}.

    Only returns known sections. Comments (`<!-- ... -->`) are stripped.
    Bullet lines keep their full form (e.g. `- [2026-04-17] subject`).
    """
    sections: dict[str, list[str]] = {name: [] for name in ALL_SECTIONS}
    current: str | None = None
    buffer: list[str] = []
    in_frontmatter = False
    past_frontmatter = False

    for line in content.splitlines():
        stripped = line.rstrip()
        if not past_frontmatter:
            if stripped == "---":
                if not in_frontmatter:
                    in_frontmatter = True
                else:
                    in_frontmatter = False
                    past_frontmatter = True
                continue
            if in_frontmatter:
                continue
            # Before frontmatter closed: skip everything
            continue

        if stripped.startswith("## "):
            if current is not None:
                sections[current] = _clean_bullets(buffer)
            header = stripped[3:].strip()
            current = header if header in sections else None
            buffer = []
            continue

        if current is not None:
            buffer.append(line)

    if current is not None:
        sections[current] = _clean_bullets(buffer)
    return sections


def _clean_bullets(lines: list[str]) -> list[str]:
    """Keep only lines that look like bullet entries, strip blanks + comments."""
    out: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("<!--"):
            continue
        if line.startswith("- "):
            out.append(line)
    return out


def read_working_memory(memory_dir: Path) -> WorkingMemoryData:
    """Parse WORKING.md into a structured view. Missing file returns empty data.

    Emits `living_memory_read` Langfuse span with counts + byte size.
    """
    path = memory_dir / WORKING_FILE_NAME
    with _langfuse_span("living_memory_read") as span:
        if not path.exists():
            _safe_update(
                span,
                file_exists=False,
                bytes_read=0,
                threads_count=0,
                hypotheses_count=0,
                questions_count=0,
                observations_count=0,
            )
            return WorkingMemoryData(exists=False)

        content = path.read_text(encoding="utf-8")
        sections = _split_sections(content)
        data = WorkingMemoryData(
            open_threads=sections["Open Threads"],
            active_hypotheses=sections["Active Hypotheses"],
            unresolved_questions=sections["Unresolved Questions"],
            heartbeat_observations=sections["Heartbeat Observations"],
            archived=sections["Archived (Cold)"],
            raw_content=content,
            exists=True,
        )
        _safe_update(
            span,
            file_exists=True,
            bytes_read=len(content),
            threads_count=len(data.open_threads),
            hypotheses_count=len(data.active_hypotheses),
            questions_count=len(data.unresolved_questions),
            observations_count=len(data.heartbeat_observations),
        )
        return data


# =============================================================================
# Rendering (sections -> markdown)
# =============================================================================


_SECTION_COMMENTS: dict[str, str] = {
    "Open Threads": (
        "<!-- Things the bot was actively working on. "
        "Each item: `- [YYYY-MM-DD] <subject> \u2014 <status>`. "
        "Max 10 active. Older items age to Archived (Cold). -->"
    ),
    "Active Hypotheses": (
        "<!-- Working beliefs the bot formed but hasn't confirmed. "
        "Each item: `- [YYYY-MM-DD] <hypothesis> \u2014 evidence: <pointer>`. -->"
    ),
    "Unresolved Questions": (
        "<!-- Things the bot asked the user or itself that remain open. "
        "Each item: `- [YYYY-MM-DD] <question>`. -->"
    ),
    "Heartbeat Observations": (
        "<!-- Ambient observations written by the heartbeat (Living Mind Act 2). "
        "Each item: `- [YYYY-MM-DD] [group] <subject> — <detail>`. "
        "Counts/dates/operator-owned labels only — never external free text. "
        "Capped, deduped, aged automatically. -->"
    ),
    "Archived (Cold)": (
        "<!-- Items aged out of active sections by dream cycle. "
        "NEVER hard-deleted. Chronological, newest first. "
        "Format: `- [archived YYYY-MM-DD] (was: <original_date>) <content>`. -->"
    ),
}


def _render_document(sections: dict[str, list[str]], existing_content: str | None) -> str:
    """Render sections + frontmatter back into full document text.

    Preserves existing frontmatter (except `date:` which is refreshed). If no
    frontmatter in `existing_content`, renders a fresh one from template.
    """
    header = _extract_header(existing_content)

    body_parts: list[str] = [header.rstrip(), ""]
    for section_name in ALL_SECTIONS:
        body_parts.append(f"## {section_name}")
        body_parts.append("")
        comment = _SECTION_COMMENTS.get(section_name)
        if comment:
            body_parts.append(comment)
            body_parts.append("")
        for bullet in sections.get(section_name, []):
            body_parts.append(bullet)
        body_parts.append("")  # trailing blank between sections

    return "\n".join(body_parts).rstrip() + "\n"


def _extract_header(existing_content: str | None) -> str:
    """Return frontmatter + title + intro paragraph, with `date:` refreshed.

    Falls back to the template's frontmatter + title block when no prior content.
    """
    template = _DEFAULT_TEMPLATE.format(today=_today_str())
    if not existing_content or not existing_content.startswith("---"):
        return _header_slice(template)

    # Refresh date: in existing frontmatter
    refreshed = _FRONTMATTER_DATE_RE.sub(f"date: {_today_str()}", existing_content, count=1)
    return _header_slice(refreshed)


def _header_slice(content: str) -> str:
    """Return everything from start through the first `##` (exclusive) — i.e.
    frontmatter, title, and intro paragraph."""
    idx = content.find("\n## ")
    if idx < 0:
        return content
    return content[:idx]


# =============================================================================
# Atomic write
# =============================================================================


def _atomic_write(path: Path, content: str) -> int:
    """Write `content` to `path` atomically via shared.atomic_write_text.

    Thin lazy wrapper — the tmp + os.replace logic was consolidated into
    shared.py (2026-07-07 framework refactor). Local import keeps test
    collection side-effect free, matching the file_lock imports below.
    """
    from shared import atomic_write_text

    return atomic_write_text(path, content)


# =============================================================================
# Write path — append operations
# =============================================================================


def _dedup_match(section: list[str], subject: str, window_days: int | None = None) -> bool:
    """Return True if an existing bullet in `section` matches `subject` within window.

    The stored bullet body is `<subject> — <status>` or `<hypothesis> — evidence: <...>`
    (for hypotheses) so a prefix match on the subject alone catches re-appends with
    different status/evidence text.

    ``window_days=None`` follows the ``_extract_thread_candidates`` sentinel
    pattern (Rule 1): resolved from WORKING_MEMORY_DEDUP_DAYS at call time
    (default 3).
    """
    if window_days is None:
        window_days = int(os.getenv("WORKING_MEMORY_DEDUP_DAYS", "3"))
    today = _date.today()
    needle = subject.strip().lower()[:40]
    if not needle:
        return False
    for bullet in section:
        m = _BULLET_RE.match(bullet)
        if not m:
            continue
        try:
            bullet_date = _date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if (today - bullet_date).days > window_days:
            continue
        body = m.group(2).strip().lower()
        if body.startswith(needle):
            return True
    return False


def _enforce_cap(
    sections: dict[str, list[str]], section_name: str, cap: int
) -> list[str]:
    """Trim `sections[section_name]` to `cap`, moving oldest overflow to Archived.

    Returns the list of bullets moved to archive (for logging / span metadata).
    """
    bullets = sections.get(section_name, [])
    if len(bullets) <= cap:
        return []

    parsed = [(_parse_date(b), b) for b in bullets]
    # Sort oldest first; stable
    parsed.sort(key=lambda p: p[0] or _date.min)
    overflow = parsed[: len(bullets) - cap]
    keep = parsed[len(bullets) - cap :]
    sections[section_name] = [p[1] for p in keep]

    today_str = _today_str()
    moved: list[str] = []
    for orig_date, bullet in overflow:
        moved.append(_format_archived(bullet, orig_date, today_str))
    if moved:
        sections["Archived (Cold)"] = moved + sections.get("Archived (Cold)", [])
    return moved


def _parse_date(bullet: str) -> _date | None:
    m = _BULLET_RE.match(bullet)
    if not m:
        return None
    try:
        return _date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _format_archived(bullet: str, orig_date: _date | None, archived_on: str) -> str:
    """Produce `- [archived YYYY-MM-DD] (was: YYYY-MM-DD) <content>` from a bullet.

    Falls back to `was: unknown` if we can't parse the original date.
    """
    m = _BULLET_RE.match(bullet)
    if m:
        was = m.group(1)
        content = m.group(2)
    else:
        was = orig_date.isoformat() if orig_date else "unknown"
        content = bullet[2:] if bullet.startswith("- ") else bullet
    return f"- [archived {archived_on}] (was: {was}) {content}"


def _append_to_section(
    memory_dir: Path,
    section_name: str,
    bullet_text: str,
    subject_for_dedup: str,
    cap: int,
    span_name: str,
    dedup_days: int | None = None,
    age_days: int | None = None,
) -> int:
    """Common append path — locked, atomic, dedup-aware, cap-aware.

    Returns 1 if appended, 0 if skipped (dedup).

    ``dedup_days=None`` keeps the existing ``_dedup_match`` default window —
    the three existing append helpers are byte-identical in behavior.
    ``age_days=None`` means no in-write aging (existing behavior). When set,
    bullets in the TARGET section older than ``age_days`` move to
    "Archived (Cold)" BEFORE dedup/append — same lock, same render, same
    atomic write. In-write aging touches only the target section.
    """
    from shared import file_lock  # local import keeps test collection side-effect free

    path = memory_dir / WORKING_FILE_NAME
    with _langfuse_span(span_name) as span, file_lock(path, timeout=5.0):
        content = path.read_text(encoding="utf-8") if path.exists() else _bootstrap_file(path)
        sections = _split_sections(content)

        aged: list[str] = []
        if age_days is not None:
            today = _date.today()
            today_str = _today_str()
            keep: list[str] = []
            for bullet in sections.get(section_name, []):
                dt = _parse_date(bullet)
                if dt is not None and (today - dt).days > age_days:
                    aged.append(_format_archived(bullet, dt, today_str))
                else:
                    keep.append(bullet)
            if aged:
                sections[section_name] = keep
                sections["Archived (Cold)"] = aged + sections.get("Archived (Cold)", [])

        if dedup_days is None:
            is_duplicate = _dedup_match(sections.get(section_name, []), subject_for_dedup)
        else:
            is_duplicate = _dedup_match(
                sections.get(section_name, []), subject_for_dedup, window_days=dedup_days
            )
        if is_duplicate:
            # Persist in-write aging even on a dedup skip — the lifecycle is
            # structural: a permanently-deduped subject must not block other
            # stale bullets from aging out of the section.
            dedup_bytes = 0
            if aged:
                rendered = _render_document(sections, content)
                dedup_bytes = _atomic_write(path, rendered)
            _safe_update(
                span,
                section=section_name,
                threads_appended=0,
                threads_skipped_dedup=1,
                bytes_written=dedup_bytes,
            )
            return 0

        sections.setdefault(section_name, []).append(bullet_text)
        _enforce_cap(sections, section_name, cap)
        rendered = _render_document(sections, content)
        bytes_written = _atomic_write(path, rendered)

        _safe_update(
            span,
            section=section_name,
            threads_appended=1,
            threads_skipped_dedup=0,
            bytes_written=bytes_written,
        )
        return 1


def append_open_thread(memory_dir: Path, subject: str, status: str) -> int:
    """Append an Open Threads bullet. Returns 1 if written, 0 if deduped."""
    subject = subject.strip()
    status = status.strip()
    today = _today_str()
    bullet = f"- [{today}] {subject} \u2014 {status}" if status else f"- [{today}] {subject}"
    return _append_to_section(
        memory_dir,
        "Open Threads",
        bullet,
        subject_for_dedup=subject,
        cap=OPEN_THREADS_CAP,
        span_name="living_memory_write",
    )


def append_hypothesis(memory_dir: Path, hypothesis: str, evidence: str) -> int:
    hypothesis = hypothesis.strip()
    evidence = evidence.strip()
    today = _today_str()
    if evidence:
        bullet = f"- [{today}] {hypothesis} \u2014 evidence: {evidence}"
    else:
        bullet = f"- [{today}] {hypothesis}"
    return _append_to_section(
        memory_dir,
        "Active Hypotheses",
        bullet,
        subject_for_dedup=hypothesis,
        cap=OTHER_ACTIVE_CAP,
        span_name="living_memory_write",
    )


def append_question(memory_dir: Path, question: str) -> int:
    question = question.strip()
    today = _today_str()
    bullet = f"- [{today}] {question}"
    return _append_to_section(
        memory_dir,
        "Unresolved Questions",
        bullet,
        subject_for_dedup=question,
        cap=OTHER_ACTIVE_CAP,
        span_name="living_memory_write",
    )


# =============================================================================
# Heartbeat observations (Living Mind Act 2)
# =============================================================================


def _sanitize_observation_text(text: str, max_chars: int) -> str:
    """Deterministic observation sanitizer (defense in depth).

    Strips control chars/newlines, collapses whitespace, strips backticks and
    HTML-comment markers (``<!--``/``-->``), and trims to ``max_chars`` at a
    word boundary. Observation content is template-generated (counts, dates,
    operator-owned labels) — this is the belt-and-braces layer, not the
    primary defense.
    """
    s = str(text or "")
    s = s.replace("<!--", " ").replace("-->", " ")
    s = s.replace("`", "")
    s = "".join(" " if (ord(ch) < 32 or ord(ch) == 127) else ch for ch in s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_chars:
        cut = s[:max_chars]
        boundary = cut.rfind(" ")
        if boundary > 0:
            cut = cut[:boundary]
        s = cut.strip()
    return s


def append_heartbeat_observation(
    memory_dir: Path,
    group: str,
    subject: str,
    detail: str = "",
    *,
    cap: int | None = None,
    dedup_days: int | None = None,
    age_days: int | None = None,
) -> ObservationAppendStatus:
    """Append a Heartbeat Observations bullet — locked, atomic, deduped,
    capped, in-write aged.

    Bullet shape: ``- [YYYY-MM-DD] [group] <subject>`` plus
    `` — <detail>`` when detail is non-empty. The dedup subject is
    ``[group] <subject>`` (stable, template-fixed); volatile numbers live in
    the detail, outside the dedup prefix window.

    None-sentinel knobs body-resolve at call time (Rule 1 — the
    ``_extract_thread_candidates`` precedent, NOT import-time constants):
    ``HEARTBEAT_OBSERVATION_CAP`` (10), ``HEARTBEAT_OBSERVATION_DEDUP_DAYS``
    (3), ``HEARTBEAT_OBSERVATION_AGE_DAYS`` (7).
    """
    if cap is None:
        cap = int(os.getenv("HEARTBEAT_OBSERVATION_CAP", "10"))
    if dedup_days is None:
        dedup_days = int(os.getenv("HEARTBEAT_OBSERVATION_DEDUP_DAYS", "3"))
    if age_days is None:
        age_days = int(os.getenv("HEARTBEAT_OBSERVATION_AGE_DAYS", "7"))

    group_s = _sanitize_observation_text(group, 20)
    subject_s = _sanitize_observation_text(subject, 80)
    detail_s = _sanitize_observation_text(detail, 120)
    if not subject_s:
        # Decided before any file I/O — a sanitize-drop never touches the file.
        return ObservationAppendStatus.EMPTY_AFTER_SANITIZE

    today = _today_str()
    dedup_subject = f"[{group_s}] {subject_s}"
    bullet = f"- [{today}] {dedup_subject}"
    if detail_s:
        bullet += f" — {detail_s}"

    written = _append_to_section(
        memory_dir,
        "Heartbeat Observations",
        bullet,
        subject_for_dedup=dedup_subject,
        cap=cap,
        span_name="living_memory_write",
        dedup_days=dedup_days,
        age_days=age_days,
    )
    return (
        ObservationAppendStatus.WRITTEN
        if written == 1
        else ObservationAppendStatus.DEDUP
    )


# =============================================================================
# Session flush → threads extraction
# =============================================================================


_FLUSH_SIGNALS = (
    re.compile(r"^\s*TODO:\s*(.+)$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*-\s*\[\s*\]\s*(.+)$", re.MULTILINE),
    re.compile(r"still need to (.+?)(?:[.!?\n]|$)", re.IGNORECASE),
    re.compile(r"waiting (?:for|on) (.+?)(?:[.!?\n]|$)", re.IGNORECASE),
    re.compile(r"next up[:,]?\s*(.+?)(?:[.!?\n]|$)", re.IGNORECASE),
    re.compile(r"need to verify (.+?)(?:[.!?\n]|$)", re.IGNORECASE),
)

# Bare pronouns that disqualify a subject when they lead it — a thread that
# starts with "it"/"this"/... is a context-free fragment, not a subject.
_SUBJECT_PRONOUNS = frozenset({"it", "this", "that", "they", "them"})

_SUBJECT_MAX_CHARS = 140
_SUBJECT_MIN_WORDS = 4

# Leading line structure to strip from a candidate: heading hashes, list
# markers (with optional checkbox), numbered-list markers.
_LEADING_STRUCTURE_RE = re.compile(
    r"^(?:#{1,6}\s+|[-*+]\s+(?:\[[ xX]\]\s*)?|\d+[.)]\s+)+"
)


def _line_containing(text: str, pos: int) -> str:
    """Return the full line of `text` containing character offset `pos`."""
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end == -1:
        end = len(text)
    return text[start:end]


def _normalize_subject(line: str) -> str | None:
    """Normalize a candidate subject line; return None when rejected.

    Full-line subject quality contract (Living Mind Act 1, behavior 6):
      - strip leading list markers / checkboxes / heading hashes and a
        leading ``TODO:`` marker (signal structure, not content)
      - strip markdown decoration (``**``, backticks), collapse whitespace
      - reject empty or punctuation-only fragments, subjects under
        ``_SUBJECT_MIN_WORDS`` words, and subjects led by a bare pronoun
      - trim to ``_SUBJECT_MAX_CHARS`` at a word boundary
    """
    text = line.strip()
    text = _LEADING_STRUCTURE_RE.sub("", text).strip()
    text = re.sub(r"^TODO:\s*", "", text, flags=re.IGNORECASE).strip()
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.rstrip(".,;:").strip()
    if not text or not re.search(r"[A-Za-z0-9]", text):
        return None
    words = text.split()
    if len(words) < _SUBJECT_MIN_WORDS:
        return None
    first_word = words[0].strip("\"'()*_[]").lower()
    if first_word in _SUBJECT_PRONOUNS:
        return None
    if len(text) > _SUBJECT_MAX_CHARS:
        cut = text[:_SUBJECT_MAX_CHARS]
        boundary = cut.rfind(" ")
        if boundary > 0:
            cut = cut[:boundary]
        text = cut.rstrip(".,;:").strip()
    return text


def _extract_thread_candidates(
    flush_md: str, max_threads: int | None = None
) -> list[str]:
    """Return up to `max_threads` normalized full-line subjects from a flush.

    The candidate is the SIGNAL LINE'S content, never the regex group tail —
    tail capture produced fragments like "the verdict" and "** line (so it
    surfaces even in a fresh session)". `max_threads` uses the None-sentinel
    pattern (Rule 1): resolved from WORKING_MEMORY_MAX_FLUSH_THREADS at call
    time (default 3).
    """
    if max_threads is None:
        max_threads = int(os.getenv("WORKING_MEMORY_MAX_FLUSH_THREADS", "3"))
    seen: set[str] = set()
    candidates: list[str] = []
    for pattern in _FLUSH_SIGNALS:
        for m in pattern.finditer(flush_md):
            subject = _normalize_subject(_line_containing(flush_md, m.start()))
            if subject is None:
                continue
            key = subject.lower()[:60]
            if key in seen:
                continue
            seen.add(key)
            candidates.append(subject)
            if len(candidates) >= max_threads:
                return candidates
    return candidates


def append_open_threads_from_flush(memory_dir: Path, flush_md: str) -> int:
    """Extract TODO-ish lines from a session flush markdown and append as open threads.

    Returns the count appended (deduped items don't count). Caps at
    WORKING_MEMORY_MAX_FLUSH_THREADS (default 3) per session.
    """
    candidates = _extract_thread_candidates(flush_md or "")
    if not candidates:
        return 0
    appended = 0
    for subject in candidates:
        appended += append_open_thread(memory_dir, subject=subject, status="open")
    return appended


def resolve_open_thread(memory_dir: Path, index: int) -> tuple[bool, str]:
    """Move Open Threads item N (1-based) to Archived (Cold) with `[resolved ...]` prefix.

    Returns (success, message). Message describes the resolved bullet or error.
    """
    from shared import file_lock

    path = memory_dir / WORKING_FILE_NAME
    today = _today_str()

    with _langfuse_span("living_memory_write") as span, file_lock(path, timeout=5.0):
        if not path.exists():
            _safe_update(span, resolved=0, reason="no_file")
            return False, "WORKING.md does not exist yet."

        content = path.read_text(encoding="utf-8")
        sections = _split_sections(content)
        threads = sections.get("Open Threads", [])

        if index < 1 or index > len(threads):
            _safe_update(span, resolved=0, reason="out_of_range", requested=index, available=len(threads))
            return False, f"Item {index} not found. Open Threads has {len(threads)} item(s)."

        bullet = threads.pop(index - 1)
        m = _BULLET_RE.match(bullet)
        was = m.group(1) if m else "unknown"
        content_body = m.group(2) if m else (bullet[2:] if bullet.startswith("- ") else bullet)
        archived_line = f"- [resolved {today}] (was: {was}) {content_body}"

        archive = sections.setdefault("Archived (Cold)", [])
        archive.insert(0, archived_line)

        rendered = _render_document(sections, content)
        bytes_written = _atomic_write(path, rendered)

        _safe_update(
            span,
            resolved=1,
            bytes_written=bytes_written,
            section="Open Threads",
        )
        return True, content_body.strip()


# =============================================================================
# Age path — archive stale items
# =============================================================================


def archive_stale_working_items(
    memory_dir: Path, days: int = 7, observation_days: int | None = None
) -> ArchiveReport:
    """Move items older than `days` from active sections to Archived (Cold).

    Insert-only — active sections lose bullets, archive gains them. Never
    hard-deletes. Emits `living_memory_archive` span.

    "Heartbeat Observations" ages with its OWN window: ``observation_days``,
    or ``HEARTBEAT_OBSERVATION_AGE_DAYS`` (default 7) when None — the same
    env knob the in-write ager uses. The ``ACTIVE_SECTIONS`` tuple itself is
    unchanged; existing callers (``memory_dream.py``) need zero changes.
    """
    from shared import file_lock

    if observation_days is None:
        observation_days = int(os.getenv("HEARTBEAT_OBSERVATION_AGE_DAYS", "7"))

    report = ArchiveReport(archived_count=0, days=days, sections_touched=[])
    path = memory_dir / WORKING_FILE_NAME

    with _langfuse_span("living_memory_archive") as span, file_lock(path, timeout=5.0):
        if not path.exists():
            _safe_update(
                span,
                archived_count=0,
                sections_touched=0,
                days_threshold=days,
                file_exists=False,
            )
            return report

        content = path.read_text(encoding="utf-8")
        sections = _split_sections(content)
        today = _date.today()
        today_str = today.isoformat()

        archived: list[str] = []
        per_section_counts: dict[str, int] = {}
        section_thresholds: dict[str, int] = {s: days for s in ACTIVE_SECTIONS}
        section_thresholds["Heartbeat Observations"] = observation_days
        for section_name in (*ACTIVE_SECTIONS, "Heartbeat Observations"):
            threshold = section_thresholds[section_name]
            bullets = sections.get(section_name, [])
            keep: list[str] = []
            moved_from_section: list[str] = []
            for bullet in bullets:
                dt = _parse_date(bullet)
                if dt is not None and (today - dt).days > threshold:
                    moved_from_section.append(_format_archived(bullet, dt, today_str))
                else:
                    keep.append(bullet)
            if moved_from_section:
                sections[section_name] = keep
                archived.extend(moved_from_section)
                report.sections_touched.append(section_name)
                per_section_counts[section_name] = len(moved_from_section)

        if archived:
            existing_archive = sections.get("Archived (Cold)", [])
            sections["Archived (Cold)"] = archived + existing_archive
            rendered = _render_document(sections, content)
            _atomic_write(path, rendered)
            report.archived_count = len(archived)

            # Vault-level LOG.md event (Karpathy LLM Wiki pattern).
            try:
                from entity_extractor import append_vault_log

                bullets_log = [
                    f"{s}: {per_section_counts[s]} items > "
                    f"{section_thresholds[s]}d old moved to cold"
                    for s in report.sections_touched
                ]
                append_vault_log(
                    memory_dir,
                    event_type="working_memory_archive",
                    title=f"archived {report.archived_count} items",
                    bullets=bullets_log,
                )
            except Exception:
                pass  # log failure is never fatal

        _safe_update(
            span,
            archived_count=report.archived_count,
            sections_touched=len(report.sections_touched),
            days_threshold=days,
            file_exists=True,
        )
    return report


# =============================================================================
# Briefing helper (imported by bootstrap.py)
# =============================================================================


def build_briefing_section(memory_dir: Path, max_items_per_section: int = 3) -> str:
    """Build a compact `## Working Memory` briefing block (~400 chars).

    Returns empty string if WORKING.md is missing or all active sections empty.
    Called by bootstrap.build_session_briefing().
    """
    data = read_working_memory(memory_dir)
    if not data.exists:
        return ""

    lines: list[str] = []

    def _fmt(bullets: list[str], label: str) -> None:
        if not bullets:
            return
        entries: list[str] = []
        for b in bullets[:max_items_per_section]:
            m = _BULLET_RE.match(b)
            if m:
                entries.append(f"{m.group(2)} ({m.group(1)})")
            else:
                entries.append(b.lstrip("- ").strip())
        lines.append(f"{label}: " + ", ".join(entries))

    _fmt(data.open_threads, "Open threads")
    _fmt(data.active_hypotheses, "Active hypotheses")
    _fmt(data.unresolved_questions, "Unresolved")
    _fmt(data.heartbeat_observations, "Heartbeat observations")

    if not lines:
        return ""
    return "## Working Memory\n" + "\n".join(lines)
