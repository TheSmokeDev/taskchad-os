"""Co-founder project model — read side (US-002) + write side (US-003).

Discovers and parses project files from the watched vault folder. Every pass
starts from physical file state (Rule 2): the frontmatter and the three owned
sections on disk are the only truth about a project.

Ownership contract (prd.md Phase 1):
    ## Spec                   STATIC      — only the operator edits
    ## Plan / Working Memory  MUTABLE     — orchestrator may rewrite
    ## Activity Log           APPEND-ONLY — newest at the bottom

Section headings may carry an inline annotation on the heading line
(e.g. ``## Spec (STATIC - orchestrator MUST NOT rewrite)``) — extraction
matches the heading with a word boundary, so ``## Specification`` never
satisfies ``## Spec``.

``status`` stays a raw string at parse level: an LLM-invented status like
``in_progress`` must survive parsing (US-007 owns enum classification and
treats non-enum strings as active builds).

Write side: no public helper can touch the Spec section. ``update_frontmatter``
re-stamps frontmatter only (body byte-preserved); ``append_activity_log`` and
``write_plan`` splice a single section span and reject content that could
smuggle a new H2 heading in (:class:`OwnershipError`). All writes are atomic
(tmp + ``os.replace``) under ``shared.file_lock`` (the living_memory pattern).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DONE_SUBFOLDER = "done"

SECTION_SPEC = "Spec"
SECTION_PLAN = "Plan / Working Memory"
SECTION_ACTIVITY_LOG = "Activity Log"

REQUIRED_SECTIONS = (SECTION_SPEC, SECTION_PLAN, SECTION_ACTIVITY_LOG)

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
_H1_TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)


class ProjectParseError(ValueError):
    """A project file cannot be parsed into a :class:`CofounderProject`."""


class OwnershipError(ValueError):
    """An attempted write violates the section ownership contract."""


@dataclass
class ProjectFrontmatter:
    """Exact PRD frontmatter schema (prd.md Phase 1)."""

    tags: list[str] = field(default_factory=list)
    status: str = "new"
    created: str | None = None
    last_run: str | None = None
    repo: str | None = None
    branch: str | None = None
    current_job_id: str | int | None = None
    iterations: int = 0
    max_iterations: int = 50
    max_wall_clock_hours: float = 72.0
    completion_check: str | None = None
    subjective_gate: bool = False
    archon_workflow: str | None = None
    chat_thread: str | int | None = None


@dataclass
class CofounderProject:
    """One parsed project file: frontmatter + the three owned sections."""

    path: Path
    title: str
    frontmatter: ProjectFrontmatter
    spec: str
    plan: str
    activity_log: str

    @property
    def slug(self) -> str:
        return self.path.stem


def extract_section(content: str, heading: str) -> str | None:
    """Return the body of an H2 section, or None when the section is absent.

    An empty-but-present section returns "" (distinct from None so missing
    sections are detectable as malformed).
    """
    pattern = rf"^## {re.escape(heading)}\b[^\n]*\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, content, re.DOTALL | re.MULTILINE)
    return match.group(1).strip() if match else None


def _opt_str(value: Any) -> str | None:
    """Normalize an optional scalar to str; YAML parses unquoted ISO dates
    into datetime objects, so re-serialize those with isoformat()."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _frontmatter_from_dict(data: dict[str, Any]) -> ProjectFrontmatter:
    try:
        raw_tags = data.get("tags") or []
        if not isinstance(raw_tags, list):
            raise ProjectParseError(f"tags must be a list, got {type(raw_tags).__name__}")
        defaults = ProjectFrontmatter()
        return ProjectFrontmatter(
            tags=[str(t) for t in raw_tags],
            status=str(data.get("status") or defaults.status),
            created=_opt_str(data.get("created")),
            last_run=_opt_str(data.get("last_run")),
            repo=_opt_str(data.get("repo")),
            branch=_opt_str(data.get("branch")),
            current_job_id=data.get("current_job_id"),
            iterations=int(data.get("iterations") or 0),
            max_iterations=int(data.get("max_iterations") or defaults.max_iterations),
            max_wall_clock_hours=float(
                data.get("max_wall_clock_hours") or defaults.max_wall_clock_hours
            ),
            completion_check=_opt_str(data.get("completion_check")),
            subjective_gate=bool(data.get("subjective_gate", False)),
            archon_workflow=_opt_str(data.get("archon_workflow")),
            chat_thread=data.get("chat_thread"),
        )
    except ProjectParseError:
        raise
    except (TypeError, ValueError) as exc:
        raise ProjectParseError(f"bad frontmatter value: {exc}") from exc


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Split into (frontmatter dict, body). Raises ProjectParseError when the
    frontmatter block is missing, unparseable, or not a mapping."""
    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise ProjectParseError("missing frontmatter block")
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ProjectParseError(f"bad YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectParseError(
            f"frontmatter must be a mapping, got {type(data).__name__}"
        )
    return data, content[match.end():]


def parse_project_file(path: Path) -> CofounderProject:
    """Parse one project file. Raises ProjectParseError on any malformation;
    discovery is the fail-open boundary that catches it."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectParseError(f"unreadable: {exc}") from exc

    fm_dict, body = _split_frontmatter(content)
    frontmatter = _frontmatter_from_dict(fm_dict)

    sections: dict[str, str] = {}
    missing: list[str] = []
    for heading in REQUIRED_SECTIONS:
        extracted = extract_section(body, heading)
        if extracted is None:
            missing.append(heading)
        else:
            sections[heading] = extracted
    if missing:
        raise ProjectParseError(f"missing section(s): {', '.join(missing)}")

    title_match = _H1_TITLE_RE.search(body)
    title = title_match.group(1).strip() if title_match else path.stem

    return CofounderProject(
        path=path,
        title=title,
        frontmatter=frontmatter,
        spec=sections[SECTION_SPEC],
        plan=sections[SECTION_PLAN],
        activity_log=sections[SECTION_ACTIVITY_LOG],
    )


def discover_projects(projects_dir: Path) -> list[CofounderProject]:
    """List and parse project files in the watched folder.

    Skips ``_``-prefixed files, ``README*`` (case-insensitive), and the
    ``done/`` subfolder (non-recursive glob). Malformed files are skipped
    with a logged warning. Never raises out of discovery.
    """
    try:
        if not projects_dir.is_dir():
            return []
        candidates = sorted(projects_dir.glob("*.md"))
    except OSError as exc:
        logger.warning("cofounder: cannot list projects dir %s: %s", projects_dir, exc)
        return []

    projects: list[CofounderProject] = []
    for path in candidates:
        name = path.name
        if name.startswith("_") or name.lower().startswith("readme"):
            continue
        if not path.is_file():
            continue
        try:
            projects.append(parse_project_file(path))
        except ProjectParseError as exc:
            logger.warning("cofounder: skipping malformed project file %s: %s", path, exc)
        except Exception as exc:  # never let one file break the pass
            logger.warning("cofounder: skipping project file %s: %s", path, exc)
    return projects


# =============================================================================
# WRITE SIDE (US-003) — machine state is re-stamped in code; Spec is
# physically out of every writer's reach.
# =============================================================================

_H2_RE = re.compile(r"^## ", re.MULTILINE)

_KNOWN_FRONTMATTER_KEYS = frozenset(ProjectFrontmatter.__dataclass_fields__)


def _atomic_write(path: Path, text: str) -> None:
    """Write atomically via tmp + os.replace (living_memory.py pattern)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _split_raw(content: str) -> tuple[str, str]:
    """Split into (frontmatter block incl. delimiters, body), byte-preserving.

    Raises ProjectParseError when the frontmatter block is missing."""
    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise ProjectParseError("missing frontmatter block")
    return content[: match.end()], content[match.end():]


def _section_span(body: str, heading: str) -> tuple[int, int]:
    """Offsets of a section's body within ``body`` (after the heading line,
    up to the next H2 or EOF). Raises ProjectParseError when absent."""
    match = re.search(rf"^## {re.escape(heading)}\b[^\n]*\n", body, re.MULTILINE)
    if match is None:
        raise ProjectParseError(f"missing section: {heading}")
    start = match.end()
    nxt = _H2_RE.search(body, start)
    end = nxt.start() if nxt else len(body)
    return start, end


def _yaml_scalar(value: Any) -> Any:
    """Normalize datetimes back to ISO strings before dump — yaml.safe_load
    parses unquoted ISO dates into datetime, and safe_dump would re-emit them
    space-separated (US-002 learning)."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def update_frontmatter(path: Path, **fields: Any) -> ProjectFrontmatter:
    """Re-stamp frontmatter keys only, preserving the body byte-for-byte.

    Unknown keys are rejected, and the merged mapping is validated against
    the schema BEFORE any bytes touch disk — a bad value can never corrupt
    the file. Returns the validated frontmatter.
    """
    unknown = set(fields) - _KNOWN_FRONTMATTER_KEYS
    if unknown:
        raise ValueError(f"unknown frontmatter key(s): {', '.join(sorted(unknown))}")

    from shared import file_lock  # local import (living_memory precedent)

    with file_lock(path, timeout=5.0):
        content = path.read_text(encoding="utf-8")
        data, body = _split_frontmatter(content)
        data.update(fields)
        validated = _frontmatter_from_dict(data)  # raises before any write
        dumpable = {k: _yaml_scalar(v) for k, v in data.items()}
        fm_text = yaml.safe_dump(dumpable, sort_keys=False, allow_unicode=True, width=4096)
        _atomic_write(path, f"---\n{fm_text}---\n{body}")
    return validated


def append_activity_log(path: Path, line: str, *, timestamp: str | None = None) -> str:
    """Append one timestamped entry at the BOTTOM of '## Activity Log' only.

    The log is append-only: existing entries are never edited or reordered.
    Multi-line input is rejected — an embedded heading could hijack section
    ownership. Returns the exact entry line written.
    """
    text = line.strip()
    if not text:
        raise OwnershipError("activity log entry must be non-empty")
    if "\n" in line or "\r" in line:
        raise OwnershipError("activity log entries are single-line (no embedded newlines)")
    if timestamp is None:
        timestamp = datetime.now().isoformat(timespec="seconds")
    entry = f"- {timestamp} {text}"

    from shared import file_lock

    with file_lock(path, timeout=5.0):
        content = path.read_text(encoding="utf-8")
        head, body = _split_raw(content)
        start, end = _section_span(body, SECTION_ACTIVITY_LOG)
        segment = body[start:end]
        core = segment.rstrip()
        tail = segment[len(core):] or "\n"
        new_segment = (core + "\n" if core else "") + entry + tail
        _atomic_write(path, head + body[:start] + new_segment + body[end:])
    return entry


def write_plan(path: Path, new_plan: str) -> None:
    """Replace only the '## Plan / Working Memory' section body.

    The heading line (including any inline annotation) and every other byte
    of the file are preserved. Plan content may not contain H2 headings —
    that is how a rogue plan would shadow or rewrite the Spec.
    """
    if _H2_RE.search(new_plan):
        raise OwnershipError("plan content may not contain H2 headings (section ownership)")

    from shared import file_lock

    with file_lock(path, timeout=5.0):
        content = path.read_text(encoding="utf-8")
        head, body = _split_raw(content)
        start, end = _section_span(body, SECTION_PLAN)
        core = new_plan.strip("\n")
        followed = end < len(body)
        if core.strip():
            new_segment = core + ("\n\n" if followed else "\n")
        else:
            new_segment = "\n" if followed else ""
        _atomic_write(path, head + body[:start] + new_segment + body[end:])


def archive_to_done(path: Path) -> Path:
    """Move a finished project file into the ``done/`` subfolder, content
    preserved. A name collision gets a numeric suffix — an earlier archive
    is never overwritten. Returns the archived path."""
    from shared import file_lock

    done_dir = path.parent / DONE_SUBFOLDER
    done_dir.mkdir(parents=True, exist_ok=True)
    target = done_dir / path.name
    counter = 1
    while target.exists():
        target = done_dir / f"{path.stem}-{counter}{path.suffix}"
        counter += 1
    with file_lock(path, timeout=5.0):
        os.replace(path, target)
    return target
