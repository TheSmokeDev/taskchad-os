"""Co-founder repo slug resolution (US-004).

Resolves a project's ``repo:`` frontmatter slug to a local path + default
branch through the Repositories System: the ``## Active Repositories``
markdown table in ``vault/memory/REPOSITORIES.md``, read via the existing
``repository_memory`` readers. Vault project files stay portable because they
carry only the slug; the machine-local path lives in the private repo index.

``greenfield`` is a recognized sentinel for projects that have no tracked
repo yet — it resolves without touching the index at all.

Unknown slugs (and a missing/short index) raise :class:`RepoResolutionError`,
a :class:`~cofounder.project_model.ProjectParseError` subclass — so the
pass's existing fail-open boundary (catch, warn, skip the project) covers
repo resolution with no new catch logic. Resolution never crashes a pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import repository_memory
from cofounder.project_model import ProjectParseError

GREENFIELD_SLUG = "greenfield"

_ACTIVE_REPOS_HEADING = "Active Repositories"

# Column order of the Active Repositories table:
# | Slug | GitHub | Visibility | Default branch | Local path | Archon | Page |
_COL_SLUG = 0
_COL_DEFAULT_BRANCH = 3
_COL_LOCAL_PATH = 4
_MIN_COLUMNS = 5


class RepoResolutionError(ProjectParseError):
    """A repo slug cannot be resolved through REPOSITORIES.md."""


@dataclass(frozen=True)
class RepoResolution:
    """One resolved repo target for dispatch."""

    slug: str
    local_path: Path | None
    default_branch: str | None
    greenfield: bool = False


def _table_rows(section: str) -> list[list[str]]:
    """Markdown table rows as stripped cell lists, header/separator dropped."""
    rows: list[list[str]] = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells:
            continue
        if all(set(cell) <= {"-", ":", " "} for cell in cells):
            continue  # |---|---| separator
        if cells[_COL_SLUG].lower() == "slug":
            continue  # header row
        rows.append(cells)
    return rows


def list_tracked_repos(*, memory_dir: Path | str | None = None) -> list[str]:
    """All Active Repositories slugs, in table order. Fail-open to ``[]``.

    The v2 agenda scan's repo universe: a missing/empty index or a malformed
    table yields an empty list (the scan narrows; nothing raises). Rows are
    included whenever the slug cell is non-empty — dispatchability (path and
    branch cells) stays :func:`resolve_repo`'s concern.
    """
    try:
        if memory_dir is None:
            import config

            memory_dir = config.MEMORY_DIR
        index_path = Path(memory_dir) / repository_memory.REPOSITORY_INDEX_FILE
        content = repository_memory.read_text_safe(index_path)
        if not content.strip():
            return []
        section = repository_memory.extract_h2_section(content, _ACTIVE_REPOS_HEADING)
        if not section:
            return []
        return [
            cells[_COL_SLUG]
            for cells in _table_rows(section)
            # Same row-shape bar as resolve_repo: a malformed short row must
            # not surface a "tracked" slug that resolution would later reject.
            if len(cells) >= _MIN_COLUMNS and cells[_COL_SLUG].strip()
        ]
    except Exception:
        return []


def resolve_repo(slug: str, *, memory_dir: Path | str | None = None) -> RepoResolution:
    """Resolve a REPOSITORIES.md slug to local path + default branch.

    ``greenfield`` returns the sentinel resolution without reading the index.
    Any other failure — empty slug, missing index, missing table, unknown
    slug, or a matching row with blank path/branch cells — raises
    :class:`RepoResolutionError` for the caller's skip-and-warn boundary.
    ``memory_dir`` defaults to ``config.MEMORY_DIR`` at call time (Rule 1).
    """
    normalized = (slug or "").strip()
    if not normalized:
        raise RepoResolutionError("empty repo slug")

    if normalized.lower() == GREENFIELD_SLUG:
        return RepoResolution(
            slug=GREENFIELD_SLUG, local_path=None, default_branch=None, greenfield=True
        )

    if memory_dir is None:
        import config

        memory_dir = config.MEMORY_DIR
    memory_dir = Path(memory_dir)

    index_path = memory_dir / repository_memory.REPOSITORY_INDEX_FILE
    content = repository_memory.read_text_safe(index_path)
    if not content.strip():
        raise RepoResolutionError(f"missing or empty repo index: {index_path}")

    section = repository_memory.extract_h2_section(content, _ACTIVE_REPOS_HEADING)
    if not section:
        raise RepoResolutionError(
            f"no '## {_ACTIVE_REPOS_HEADING}' section in {index_path}"
        )

    for cells in _table_rows(section):
        if len(cells) < _MIN_COLUMNS or cells[_COL_SLUG] != normalized:
            continue
        default_branch = cells[_COL_DEFAULT_BRANCH]
        local_path = cells[_COL_LOCAL_PATH]
        if not default_branch or not local_path:
            raise RepoResolutionError(
                f"repo '{normalized}' row is missing default branch or local path"
            )
        return RepoResolution(
            slug=normalized,
            local_path=Path(local_path),
            default_branch=default_branch,
        )

    raise RepoResolutionError(f"unknown repo slug: {normalized}")
