"""Private repository memory helpers.

This module only reads the private ``vault/memory`` repo index and
per-repo pages. The sanitizer denies that tree, so these helpers must not be
used to build public framework output.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_INDEX_FILE = "REPOSITORIES.md"
REPOSITORY_PAGES_DIR = "repositories"

REQUIRED_PAGE_SECTIONS = (
    "Identity",
    "Archon Configuration",
    "Workflow Preferences",
    "Dispatch History",
    "Recent Activity",
    "Related",
)

REQUIRED_FRONTMATTER_KEYS = (
    "slug",
    "github_repo",
    "visibility",
    "default_branch",
    "local_path",
    "archon_enabled",
)

DEFAULT_REPOSITORY_BRIEFING_CHARS = 900


def read_text_safe(path: Path) -> str:
    try:
        if path.exists():
            return path.read_text(encoding="utf-8")
    except Exception:
        return ""
    return ""


def extract_h2_section(content: str, heading: str) -> str:
    pattern = rf"^## {re.escape(heading)}\s*\n(.*?)(?=\n## |\Z)"
    match = re.search(pattern, content, re.DOTALL | re.MULTILINE)
    return match.group(1).strip() if match else ""


def build_repository_briefing_section(
    memory_dir: Path,
    *,
    max_chars: int = DEFAULT_REPOSITORY_BRIEFING_CHARS,
) -> str:
    """Return a compact ``### Repositories`` briefing block.

    Missing or malformed files fail open by returning an empty string. The
    block intentionally favors the active repo table and dispatch defaults over
    long per-repo history.
    """

    content = read_text_safe(memory_dir / REPOSITORY_INDEX_FILE).strip()
    if not content:
        return ""

    active = extract_h2_section(content, "Active Repositories")
    defaults = extract_h2_section(content, "Dispatch Defaults")

    chunks: list[str] = []
    if active:
        chunks.append(active)
    if defaults:
        chunks.append("Dispatch defaults:\n" + defaults)
    if not chunks:
        chunks.append(content)

    body = "\n\n".join(chunks).strip()
    if len(body) > max_chars:
        body = body[:max_chars]
        last_newline = body.rfind("\n")
        if last_newline > 0:
            body = body[:last_newline]
        body = body.rstrip() + "\n- ... truncated; read REPOSITORIES.md for full repo context."

    return "### Repositories\n" + body


def parse_frontmatter(content: str) -> dict[str, str]:
    if not content.startswith("---\n"):
        return {}
    end = content.find("\n---", 4)
    if end == -1:
        return {}
    raw = content[4:end].strip()
    parsed: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed[key.strip()] = value.strip().strip('"')
    return parsed


def _index_page_links(index_content: str) -> set[str]:
    links = set()
    for match in re.finditer(r"\]\((repositories/[^)]+\.md)\)", index_content):
        links.add(match.group(1))
    return links


def validate_repository_memory(memory_dir: Path) -> list[str]:
    """Validate private repo index/page shape.

    Returns human-readable errors. Does not raise, so callers can use it in
    tests, CLI probes, or future diagnostics without risking startup failure.
    """

    errors: list[str] = []
    index_path = memory_dir / REPOSITORY_INDEX_FILE
    index_content = read_text_safe(index_path)
    if not index_content:
        return [f"missing {REPOSITORY_INDEX_FILE}"]

    for rel_link in sorted(_index_page_links(index_content)):
        page_path = memory_dir / rel_link
        if not page_path.exists():
            errors.append(f"index link missing page: {rel_link}")

    pages_dir = memory_dir / REPOSITORY_PAGES_DIR
    if not pages_dir.exists():
        errors.append(f"missing {REPOSITORY_PAGES_DIR}/")
        return errors

    for page_path in sorted(pages_dir.glob("*.md")):
        rel = page_path.relative_to(memory_dir).as_posix()
        content = read_text_safe(page_path)
        frontmatter = parse_frontmatter(content)

        for key in REQUIRED_FRONTMATTER_KEYS:
            if key not in frontmatter or not frontmatter[key]:
                errors.append(f"{rel}: missing frontmatter key {key}")

        for section in REQUIRED_PAGE_SECTIONS:
            if f"## {section}" not in content:
                errors.append(f"{rel}: missing section ## {section}")

        local_path = frontmatter.get("local_path", "")
        archon_enabled = frontmatter.get("archon_enabled", "").lower() == "true"
        if archon_enabled and local_path:
            repo_root = Path(local_path)
            if repo_root.exists() and not (repo_root / ".archon").exists():
                errors.append(f"{rel}: archon_enabled true but .archon/ missing")

    return errors
