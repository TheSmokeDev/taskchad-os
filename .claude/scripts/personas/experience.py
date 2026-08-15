"""Deterministic persona work-experience note writer (issue #420).

Every executed unit of persona work appends one section to ONE daily note
inside that persona's OWN memory tree
(``~/.homie/profiles/<id>/memory/experience/YYYY-MM-DD.md``), where
``memory_index.py``'s existing ``rglob("*.md")`` and the persona's recall
already reach - zero indexer changes.

Pattern-copy of ``crypto_round/market_notes.py`` (issue #395): the crypto
writer records ONE domain's market read, this one records "what this persona
did and how it turned out" for ANY persona and any kind of work. The
MECHANICS are copied verbatim - whole-body fail-open receipt (``written`` |
``duplicate`` | ``skipped_cap`` | ``error``), ``file_lock`` + atomic write,
in-file dedup key, daily-file cap, ``_compact`` truncation, and a reindex
guarded on the profile's PHYSICAL ``data/`` sibling (Rule 2). The SHAPE is
the slim generic contract from the architecture doc (Q1), not crypto's
domain sections.

Host-owned, never a tool the persona must remember to call: facts come from
code, prose comes from the execution's EXISTING output. Zero new LLM calls,
and a note failure never fails the work that produced it.

Path-parameterized by ``persona_id``: the DEFAULT-profile worktick process
writes into another profile's tree by resolving paths as an ARGUMENT
(``personas.core.get_persona_paths``), never by mutating ``HOMIE_HOME``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from personas import core as _core
from shared import safe_exc_text

# Architecture Q2 - the note corpus is an ENUMERATED registry, never a glob
# of the memory tree (episodes/, daily/, curricula/ each have their own
# consumers). Writer, tick gate, and distiller corpus share this one
# constant.
PERSONA_NOTE_DIRS: tuple[str, ...] = ("experience", "market")

NOTES_SUBDIR = "experience"

# Section-render caps (per field, applied before the whole-section cap).
MAX_TASK_CHARS = 400
MAX_SUMMARY_CHARS = 800
MAX_FIELD_CHARS = 300
MAX_EXCERPT_CHARS = 1_500
MAX_INGEST_CHARS = 6_000
MAX_SECTION_CHARS = 8_000
# Defensive daily-file cap (dozens of assignments/day at <8K each stays far
# under this).
MAX_NOTE_FILE_CHARS = 200_000
# Ingest never reads more than this off disk before capping down to text.
MAX_INGEST_READ_BYTES = 2_000_000

# A persona id reaches this module from a mailbox payload, a directory
# listing, or an operator CLI argument. Anything that is not ONE safe path
# segment is refused before any path math (traversal defense).
_SAFE_PERSONA_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Dedup-key tokens are built from operator/LLM-adjacent text; strip every
# character that could break out of the HTML comment that carries them.
_KEY_UNSAFE = re.compile(r"[^A-Za-z0-9._:#/@-]+")

KEY_MARKER_PREFIX = "<!-- experience-key: "
KEY_MARKER_SUFFIX = " -->"


# =============================================================================
# Paths.
# =============================================================================


def _profile_dirs(persona_id: str, root: Path | None) -> tuple[Path, Path]:
    """Return ``(memory_dir, data_dir)`` for *persona_id*.

    ``root`` (a profile root) wins when given - the test/explicit seam the
    crypto writer exposes. Otherwise the persona's own path map resolves it,
    which is what makes a cross-profile write from the default process work.
    """
    if root is not None:
        return root / "memory", root / "data"
    if not _SAFE_PERSONA_ID.match(str(persona_id or "")):
        raise ValueError(f"unsafe persona id {persona_id!r}")
    paths = _core.get_persona_paths(persona_id)
    return paths["memory"], paths["data"]


def notes_dir(persona_id: str, root: Path | None = None) -> Path:
    """The persona's experience-note directory."""
    memory_dir, _ = _profile_dirs(persona_id, root)
    return memory_dir / NOTES_SUBDIR


# =============================================================================
# Corpus discovery (issue #425) - the ONE freshness owner.
# =============================================================================


def note_dirs(memory_dir: Path, names: Sequence[str] | None = None) -> list[Path]:
    """The persona note directories that PHYSICALLY exist under *memory_dir*.

    Architecture Q2: an ENUMERATED registry, never a tree glob - globbing the
    memory tree would swallow ``episodes/``, ``curricula/`` and ``daily/``,
    which have their own consumers. Rule 2: existence is checked on disk, not
    inferred from a roster or a config claim.

    ``names`` is a ``None`` sentinel resolved to ``PERSONA_NOTE_DIRS``
    inside the body (Rule 1).
    """
    resolved = PERSONA_NOTE_DIRS if names is None else tuple(names)
    found: list[Path] = []
    for name in resolved:
        try:
            candidate = Path(memory_dir) / str(name)
            if candidate.is_dir():
                found.append(candidate)
        except OSError:
            continue  # fail-open: an unreadable dir is simply not a source
    return found


def list_fresh_notes(
    memory_dir: Path,
    since: datetime | None = None,
    *,
    max_files: int | None = None,
    note_dirs_names: Sequence[str] | None = None,
) -> list[Path]:
    """Note files under ``PERSONA_NOTE_DIRS`` modified strictly after *since*.

    The shared freshness primitive behind BOTH the learning tick's gate and
    the distiller's corpus, so the two can never disagree about what "fresh"
    means. Newest-first by mtime.

    ``since`` is a NAIVE LOCAL datetime (what
    ``cognition.proactive_brief.normalize_physical_timestamp`` hands back)
    and is compared against ``datetime.fromtimestamp(st_mtime)``, which is
    also naive local - never a string compare. ``None`` means "no floor"
    (every note counts).

    ``max_files`` is a ``None`` sentinel meaning UNCAPPED; each caller
    resolves its own cap at call time (Rule 1) - the tick counts uncapped,
    the distiller passes its corpus budget.

    Fail-open by contract: an unreadable directory or a file whose ``stat()``
    raises is skipped, and any unexpected failure yields ``[]`` rather than
    breaking the tick or the reflection that called it.
    """
    try:
        fresh: list[tuple[float, Path]] = []
        for directory in note_dirs(memory_dir, note_dirs_names):
            try:
                candidates = sorted(directory.glob("*.md"))
            except OSError:
                continue
            for path in candidates:
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue  # fail-open: an unstattable file counts as 0
                if since is not None and datetime.fromtimestamp(mtime) <= since:
                    continue
                fresh.append((mtime, path))
        fresh.sort(key=lambda item: item[0], reverse=True)
        paths = [path for _mtime, path in fresh]
        if max_files is not None:
            # A negative cap is not a valid "uncapped" signal — ``paths[:-1]``
            # would silently drop the newest file instead, and ``paths[-0:]``
            # (max_files == 0) is the whole list, not zero files. Clamp to a
            # non-negative slice bound so 0 means "no files" and any negative
            # value degrades to the same, never to uncapped.
            paths = paths[: max(0, max_files)]
        return paths
    except Exception:  # noqa: BLE001 - fail-open contract
        return []


def count_fresh_notes(memory_dir: Path, since: datetime | None = None) -> int:
    """How many note files are fresher than *since* (0 on any error)."""
    return len(list_fresh_notes(memory_dir, since))


# =============================================================================
# Rendering.
# =============================================================================


def _compact(text: Any, cap: int) -> str:
    """Collapse to one line and cap.

    Verbatim from ``market_notes._compact``. The whitespace collapse is
    load-bearing beyond tidiness: every value rendered here is
    operator-adjacent or LLM-authored, and collapsing newlines means no
    supplied text can forge a ``##`` heading or a dedup marker at column 0.
    """
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= cap:
        return collapsed
    return collapsed[: max(0, cap - 12)] + " [TRUNCATED]"


def _key_token(value: Any, cap: int = 120) -> str:
    token = _KEY_UNSAFE.sub("_", " ".join(str(value or "").split()))
    return token[:cap] or "none"


def _key_marker(key: str) -> str:
    return f"{KEY_MARKER_PREFIX}{key}{KEY_MARKER_SUFFIX}"


def _marker_line_present(content: str, marker: str) -> bool:
    """True only if *marker* appears as its OWN line in *content*.

    A genuine dedup marker is always emitted as a standalone list element
    (see ``_render_section``), so it lands on its own line. Every rendered
    field, by contrast, is prefixed (``- Label: ...``, ``> ...`` for an
    excerpt, ``## HH:MM - ...`` for the heading) and passed through
    ``_compact()``, which collapses embedded newlines to spaces — so hostile
    prose that contains the literal marker text can only ever land MID-line,
    never as a standalone line. A plain substring check (``marker in
    content``) does not make that distinction and can be pre-poisoned by an
    earlier field value that happens to contain a later assignment's exact
    marker text, causing a legitimate note to be silently dropped as a
    false ``duplicate``.
    """
    return any(line == marker for line in content.splitlines())


def _fact_lines(facts: Sequence[tuple[str, Any, int]]) -> list[str]:
    lines: list[str] = []
    for label, value, cap in facts:
        if value is None:
            continue
        text = _compact(value, cap)
        if text:
            lines.append(f"- {label}: {text}")
    return lines


def _render_section(
    *,
    heading_ref: str,
    mode: str,
    status: str,
    dedup_key: str,
    facts: Sequence[tuple[str, Any, int]],
    excerpt: Any = "",
    excerpt_heading: str = "Output excerpt",
    excerpt_cap: int | None = None,
    local_time: datetime | None = None,
) -> str:
    """The slim generic section contract (architecture Q1)."""
    # Rule 1 - the cap is a None sentinel resolved at call time, never a
    # module constant bound into the signature.
    cap = MAX_EXCERPT_CHARS if excerpt_cap is None else excerpt_cap
    when = local_time or datetime.now().astimezone()
    lines = [
        f"## {when.strftime('%H:%M')} - {_compact(heading_ref, 160)} "
        f"({_compact(mode, 40)} -> {_compact(status, 40)})",
        "",
        _key_marker(dedup_key),
        "",
    ]
    lines.extend(_fact_lines(facts))
    body = _compact(excerpt, cap)
    if body:
        lines.extend(["", f"### {excerpt_heading}", "", f"> {body}"])

    section = "\n".join(lines)
    if len(section) > MAX_SECTION_CHARS:
        section = section[:MAX_SECTION_CHARS].rsplit("\n", 1)[0] + "\n[TRUNCATED]"
    return section


def assignment_key(agenda_ref: Any, message_id: Any) -> str:
    """In-file dedup key for one executed assignment (architecture Q1)."""
    return f"{_key_token(agenda_ref)}|{_key_token(message_id, 64)}"


def ingest_key(label: Any, content: Any) -> str:
    """In-file dedup key for an ad-hoc ingest - label plus a content digest.

    Content-derived so re-ingesting the same article on the same day is a
    ``duplicate``, while a genuinely different source under the same label
    still lands.
    """
    digest = hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()[:16]
    return f"ingest|{_key_token(label, 64)}|{digest}"


def render_assignment_section(
    *,
    agenda_ref: Any,
    message_id: Any,
    mode: Any,
    status: Any,
    task: Any = "",
    repo: Any = None,
    summary: Any = "",
    deliverable_path: Any = None,
    run_id: Any = None,
    branch: Any = None,
    output_excerpt: Any = "",
    local_time: datetime | None = None,
) -> str:
    """One worktick assignment rendered as an experience section."""
    return _render_section(
        heading_ref=agenda_ref or "unassigned",
        mode=mode or "unknown",
        status=status or "unknown",
        dedup_key=assignment_key(agenda_ref, message_id),
        facts=[
            ("Task", task, MAX_TASK_CHARS),
            ("Repo", repo, MAX_FIELD_CHARS),
            ("Outcome", summary, MAX_SUMMARY_CHARS),
            ("Deliverable", deliverable_path, MAX_FIELD_CHARS),
            ("Archon run", run_id, MAX_FIELD_CHARS),
            ("Branch", branch, MAX_FIELD_CHARS),
        ],
        excerpt=output_excerpt,
        excerpt_heading="Output excerpt",
        excerpt_cap=MAX_EXCERPT_CHARS,
        local_time=local_time,
    )


def render_ingest_section(
    *,
    label: Any,
    content: Any,
    source: Any = None,
    note: Any = None,
    local_time: datetime | None = None,
) -> str:
    """One ad-hoc operator-dropped source rendered as an experience section."""
    text = str(content or "")
    return _render_section(
        heading_ref=f"ingest: {label or 'untitled'}",
        mode="ingest",
        status="captured",
        dedup_key=ingest_key(label, text),
        facts=[
            ("Source", source, MAX_FIELD_CHARS),
            ("Captured", f"{len(text)} chars", MAX_FIELD_CHARS),
            ("Operator note", note, MAX_SUMMARY_CHARS),
        ],
        excerpt=text,
        excerpt_heading="Source excerpt",
        excerpt_cap=MAX_INGEST_CHARS,
        local_time=local_time,
    )


# =============================================================================
# The append core (market_notes mechanics, verbatim).
# =============================================================================


def _reindex_note(path: Path, memory_dir: Path) -> None:
    import sys

    chat_dir = Path(__file__).resolve().parents[2] / "chat"
    if str(chat_dir) not in sys.path:
        sys.path.insert(0, str(chat_dir))
    from recall_service import reindex_file

    reindex_file(path, memory_dir)


def _note_header(day: str, persona_id: str) -> str:
    return (
        "---\n"
        "tags: [system, persona, experience]\n"
        f"date: {day}\n"
        f"persona: {_key_token(persona_id, 64)}\n"
        'summary: "Persona work-experience notes - one section appended per '
        'executed unit of work."\n'
        "---\n"
        f"# Experience Notes - {day}\n"
    )


def append_experience_section(
    *,
    persona_id: str,
    section: str,
    dedup_key: str,
    local_time: datetime | None = None,
    root: Path | None = None,
    reindex: bool = True,
) -> dict[str, Any]:
    """Append one rendered section to the persona's daily experience note.

    Whole-body fail-open by contract: always returns a receipt dict, never
    raises. Receipt statuses: written | duplicate | skipped_cap | error.
    """
    try:
        memory_dir, data_dir = _profile_dirs(persona_id, root)
        target_dir = memory_dir / NOTES_SUBDIR
        when = local_time or datetime.now().astimezone()
        day = when.strftime("%Y-%m-%d")
        path = target_dir / f"{day}.md"
        marker = _key_marker(dedup_key)

        from shared import atomic_write_text, file_lock

        target_dir.mkdir(parents=True, exist_ok=True)
        with file_lock(path, timeout=5.0):
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            if _marker_line_present(existing, marker):
                return {"status": "duplicate", "path": str(path)}
            content = existing if existing else _note_header(day, persona_id)
            content = content.rstrip("\n") + "\n\n" + section + "\n"
            if len(content) > MAX_NOTE_FILE_CHARS:
                return {
                    "status": "skipped_cap",
                    "path": str(path),
                    "detail": f"daily note exceeds {MAX_NOTE_FILE_CHARS} chars",
                }
            atomic_write_text(path, content)

        receipt: dict[str, Any] = {"status": "written", "path": str(path)}
        # Reindex only when the profile physically has the memory/data
        # sibling layout resolve_db_path keys on (Rule 2) - otherwise
        # reindex_file would derive a mis-keyed DB under the MAIN vault's
        # data dir and index a persona's note into the operator's index.
        if reindex and data_dir.is_dir():
            try:
                _reindex_note(path, memory_dir)
                receipt["reindexed"] = True
            except Exception as exc:  # noqa: BLE001 - note is already written
                receipt["reindexed"] = False
                receipt["reindex_error"] = safe_exc_text(exc)
        return receipt
    except Exception as exc:  # noqa: BLE001 - fail-open contract
        return {"status": "error", "detail": safe_exc_text(exc)}


# =============================================================================
# Caller 1 - the co-founder worktick.
# =============================================================================


def write_assignment_note(
    *,
    persona_id: str,
    agenda_ref: Any,
    message_id: Any,
    mode: Any,
    status: Any,
    task: Any = "",
    repo: Any = None,
    summary: Any = "",
    deliverable_path: Any = None,
    run_id: Any = None,
    branch: Any = None,
    output_excerpt: Any = "",
    local_time: datetime | None = None,
    root: Path | None = None,
    reindex: bool = True,
) -> dict[str, Any]:
    """Record ONE executed worktick assignment in the persona's own tree.

    Every outcome is recorded - ``done``, ``dispatched``, ``failed`` and
    ``refused`` alike. A persona learns as much from a refused grant or a
    dead provider as from a shipped deliverable, and the distiller reads
    outcomes, not just successes.

    Whole-body fail-open: always a receipt, never a raise.
    """
    try:
        section = render_assignment_section(
            agenda_ref=agenda_ref,
            message_id=message_id,
            mode=mode,
            status=status,
            task=task,
            repo=repo,
            summary=summary,
            deliverable_path=deliverable_path,
            run_id=run_id,
            branch=branch,
            output_excerpt=output_excerpt,
            local_time=local_time,
        )
        key = assignment_key(agenda_ref, message_id)
    except Exception as exc:  # noqa: BLE001 - fail-open contract
        return {"status": "error", "detail": safe_exc_text(exc)}
    return append_experience_section(
        persona_id=persona_id,
        section=section,
        dedup_key=key,
        local_time=local_time,
        root=root,
        reindex=reindex,
    )


# =============================================================================
# Caller 2 - the ad-hoc operator ingest surface.
# =============================================================================


def write_ingest_note(
    *,
    persona_id: str,
    label: Any,
    content: Any,
    source: Any = None,
    note: Any = None,
    local_time: datetime | None = None,
    root: Path | None = None,
    reindex: bool = True,
) -> dict[str, Any]:
    """Record one operator-dropped source in the persona's own tree."""
    try:
        text = str(content or "")
        section = render_ingest_section(
            label=label,
            content=text,
            source=source,
            note=note,
            local_time=local_time,
        )
        key = ingest_key(label, text)
    except Exception as exc:  # noqa: BLE001 - fail-open contract
        return {"status": "error", "detail": safe_exc_text(exc)}
    return append_experience_section(
        persona_id=persona_id,
        section=section,
        dedup_key=key,
        local_time=local_time,
        root=root,
        reindex=reindex,
    )


def _read_source_text(path: Path) -> str:
    """Read at most ``MAX_INGEST_READ_BYTES`` and decode leniently.

    A bounded binary read, not ``read_text()``: the operator can point this
    at anything on disk, and a multi-gigabyte file must not be pulled into
    memory to then be capped down to a few thousand characters.
    """
    with open(path, "rb") as handle:
        raw = handle.read(MAX_INGEST_READ_BYTES)
    return raw.decode("utf-8", errors="replace")


def ingest_source(
    persona_id: str,
    source: str,
    *,
    label: str | None = None,
    note: str | None = None,
    force_text: bool = False,
    local_time: datetime | None = None,
    root: Path | None = None,
    reindex: bool = True,
) -> dict[str, Any]:
    """``thehomie persona ingest`` business logic - file OR literal text.

    Resolution: ``force_text`` wins; otherwise an existing file path is read
    from disk; otherwise *source* is taken as the literal text. The receipt
    always names which branch ran (``source_kind``) so an operator typo in a
    path is visible instead of silently becoming a one-line "note".
    """
    receipt_context: dict[str, Any] = {"persona_id": persona_id}
    try:
        _core.validate_persona_name(persona_id)
        if root is None:
            memory_dir, _ = _profile_dirs(persona_id, None)
            if not memory_dir.parent.is_dir():
                # Rule 2 - physical state, not a roster/meta claim.
                return {
                    **receipt_context,
                    "status": "error",
                    "detail": f"no profile tree at {memory_dir.parent}",
                }

        path: Path | None = None
        if not force_text:
            candidate = Path(source).expanduser()
            if candidate.is_file():
                path = candidate

        if path is not None:
            text = _read_source_text(path)
            resolved_label = label or path.stem
            source_label: str = str(path)
            source_kind = "file"
        else:
            text = str(source or "")
            resolved_label = label or "text"
            source_label = "text (inline)"
            source_kind = "text"

        receipt_context.update(
            {
                "source_kind": source_kind,
                "label": resolved_label,
                "chars": len(text),
            }
        )
        if not text.strip():
            return {
                **receipt_context,
                "status": "error",
                "detail": "source has no text to ingest",
            }
    except Exception as exc:  # noqa: BLE001 - fail-open contract
        return {
            **receipt_context,
            "status": "error",
            "detail": safe_exc_text(exc),
        }

    receipt = write_ingest_note(
        persona_id=persona_id,
        label=resolved_label,
        content=text,
        source=source_label,
        note=note,
        local_time=local_time,
        root=root,
        reindex=reindex,
    )
    return {**receipt_context, **receipt}


__all__ = [
    "MAX_EXCERPT_CHARS",
    "MAX_INGEST_CHARS",
    "MAX_NOTE_FILE_CHARS",
    "MAX_SECTION_CHARS",
    "NOTES_SUBDIR",
    "PERSONA_NOTE_DIRS",
    "append_experience_section",
    "assignment_key",
    "count_fresh_notes",
    "ingest_key",
    "ingest_source",
    "list_fresh_notes",
    "note_dirs",
    "notes_dir",
    "render_assignment_section",
    "render_ingest_section",
    "write_assignment_note",
    "write_ingest_note",
]
