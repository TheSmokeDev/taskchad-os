"""Vault health linting — 8 checks, zero LLM cost, pure Python.

Checks: orphan_pages, broken_wikilinks, frontmatter_validation,
tag_audit, stale_content, page_size, index_completeness, contradiction_scan.

Usage:
    uv run python vault_lint.py --vault-dir vault/memory
    uv run python vault_lint.py --vault-dir path --check orphan_pages --check tag_audit
    uv run python vault_lint.py --vault-dir path --format json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

# Add scripts dir for entity_extractor imports
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


@dataclass
class LintIssue:
    """A single lint finding."""

    check: str       # check name
    severity: str    # error | warning | info
    file: str        # relative path
    message: str


# Wikilink regex
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")

# Fenced code blocks (```...```) and inline code (`...`)
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`]+`")


def _strip_code_blocks(content: str) -> str:
    """Remove fenced and inline code blocks so template wikilinks aren't scanned."""
    content = _FENCED_CODE_RE.sub("", content)
    content = _INLINE_CODE_RE.sub("", content)
    return content

# Directories that are auto-generated or infrastructure (not user content)
_SKIP_LINT_DIRS = {".obsidian", "_templates", "_canvas", "_state", ".conversations",
                   ".conversations-archived", ".nexus", ".workspaces", ".workspaces-archived"}


def _all_md_files(vault_dir: Path) -> list[Path]:
    """Get all .md files in the vault, excluding infrastructure dirs."""
    files = []
    for md in vault_dir.rglob("*.md"):
        rel_parts = md.relative_to(vault_dir).parts
        if any(p in _SKIP_LINT_DIRS for p in rel_parts):
            continue
        files.append(md)
    return sorted(files)


def _concept_files(vault_dir: Path) -> list[Path]:
    """Get all concept .md files (excluding BUILD-LOG and INDEX)."""
    concepts_dir = vault_dir / "concepts"
    if not concepts_dir.exists():
        return []
    return sorted(
        f for f in concepts_dir.glob("*.md")
        if not f.stem.startswith("BUILD-LOG") and f.stem != "INDEX"
    )


def _parse_frontmatter(content: str) -> dict:
    """Extract basic frontmatter fields as a dict."""
    fm_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not fm_match:
        return {}
    fm_text = fm_match.group(1)
    result = {}

    tags_m = re.search(r"tags:\s*\[([^\]]*)\]", fm_text)
    if tags_m:
        result["tags"] = [t.strip() for t in tags_m.group(1).split(",") if t.strip()]

    status_m = re.search(r"status:\s*(\S+)", fm_text)
    if status_m:
        result["status"] = status_m.group(1)

    date_m = re.search(r"date:\s*(\d{4}-\d{2}-\d{2})", fm_text)
    if date_m:
        result["date"] = date_m.group(1)

    summary_m = re.search(r'summary:\s*"([^"]*)"', fm_text)
    if summary_m:
        result["summary"] = summary_m.group(1)

    return result


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_orphan_pages(vault_dir: Path) -> list[LintIssue]:
    """Find concept pages with no inbound wikilinks from non-concept, non-connection files."""
    issues = []
    concept_slugs = {f.stem for f in _concept_files(vault_dir)}
    if not concept_slugs:
        return issues

    # Scan all non-concept files for wikilink references
    referenced: set[str] = set()
    for md in _all_md_files(vault_dir):
        rel = md.relative_to(vault_dir)
        # Skip concept and connection files themselves
        if rel.parts and rel.parts[0] in ("concepts", "connections"):
            continue
        try:
            content = _strip_code_blocks(md.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        for m in _WIKILINK_RE.finditer(content):
            referenced.add(m.group(1).strip())

    for slug in sorted(concept_slugs):
        if slug not in referenced:
            issues.append(LintIssue(
                check="orphan_pages", severity="warning",
                file=f"concepts/{slug}.md",
                message=f"No inbound wikilinks from non-concept files",
            ))

    return issues


def check_broken_wikilinks(vault_dir: Path) -> list[LintIssue]:
    """Find [[wikilinks]] that don't resolve to any existing .md file."""
    issues = []

    # Build set of all valid link targets (stem names)
    all_stems = set()
    for md in _all_md_files(vault_dir):
        all_stems.add(md.stem)

    for md in _all_md_files(vault_dir):
        try:
            content = _strip_code_blocks(md.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue

        rel = str(md.relative_to(vault_dir))
        for m in _WIKILINK_RE.finditer(content):
            target = m.group(1).strip()
            if target not in all_stems:
                issues.append(LintIssue(
                    check="broken_wikilinks", severity="error",
                    file=rel,
                    message=f"Broken wikilink: [[{target}]]",
                ))

    return issues


def check_frontmatter_validation(vault_dir: Path) -> list[LintIssue]:
    """Check that all files have required frontmatter fields."""
    issues = []
    required = {"tags", "date"}  # Minimum required for all note types

    for md in _all_md_files(vault_dir):
        # Skip auto-generated infrastructure files
        rel = md.relative_to(vault_dir)
        if rel.parts and rel.parts[0] in ("_dashboards", "daily", "teams"):
            continue
        if rel.name == "BUILD-LOG.md":
            continue

        try:
            content = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        fm = _parse_frontmatter(content)
        if not fm:
            issues.append(LintIssue(
                check="frontmatter_validation", severity="error",
                file=str(rel), message="Missing frontmatter block",
            ))
            continue

        for field_name in required:
            if field_name not in fm:
                issues.append(LintIssue(
                    check="frontmatter_validation", severity="error",
                    file=str(rel), message=f"Missing required field: {field_name}",
                ))

    return issues


def check_tag_audit(vault_dir: Path, schema: dict | None = None) -> list[LintIssue]:
    """Find tags not in the SCHEMA.md taxonomy."""
    issues = []
    if not schema or not schema.get("tag_taxonomy"):
        return issues

    valid_tags = schema["tag_taxonomy"]

    for md in _all_md_files(vault_dir):
        try:
            content = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        fm = _parse_frontmatter(content)
        for tag in fm.get("tags", []):
            if tag and tag not in valid_tags:
                rel = str(md.relative_to(vault_dir))
                issues.append(LintIssue(
                    check="tag_audit", severity="warning",
                    file=rel, message=f"Tag not in schema taxonomy: '{tag}'",
                ))

    return issues


def check_stale_content(vault_dir: Path, days: int = 90) -> list[LintIssue]:
    """Find concept pages with date field older than N days."""
    issues = []
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    for md in _concept_files(vault_dir):
        try:
            content = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        fm = _parse_frontmatter(content)
        page_date = fm.get("date", "")
        if page_date and page_date < cutoff:
            issues.append(LintIssue(
                check="stale_content", severity="info",
                file=str(md.relative_to(vault_dir)),
                message=f"Last updated {page_date} (>{days} days old)",
            ))

    return issues


def check_page_size(vault_dir: Path, max_lines: int = 200) -> list[LintIssue]:
    """Flag files exceeding max_lines."""
    issues = []
    for md in _concept_files(vault_dir):
        try:
            line_count = len(md.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            continue

        if line_count > max_lines:
            issues.append(LintIssue(
                check="page_size", severity="warning",
                file=str(md.relative_to(vault_dir)),
                message=f"{line_count} lines (max {max_lines}) — consider splitting",
            ))

    return issues


def check_index_completeness(vault_dir: Path) -> list[LintIssue]:
    """Check that every concept page appears in INDEX.md."""
    issues = []
    index_path = vault_dir / "concepts" / "INDEX.md"
    if not index_path.exists():
        return [LintIssue(
            check="index_completeness", severity="warning",
            file="concepts/INDEX.md", message="INDEX.md does not exist",
        )]

    try:
        index_content = index_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return issues

    for md in _concept_files(vault_dir):
        slug = md.stem
        if f"[[{slug}]]" not in index_content:
            issues.append(LintIssue(
                check="index_completeness", severity="warning",
                file=f"concepts/{slug}.md",
                message="Not listed in INDEX.md",
            ))

    return issues


def check_contradiction_scan(vault_dir: Path) -> list[LintIssue]:
    """Run contradiction detection on all multi-source concept pages."""
    issues = []

    try:
        from entity_extractor import check_contradictions
    except ImportError:
        return issues

    for md in _concept_files(vault_dir):
        try:
            contras = check_contradictions(md)
        except Exception:
            continue

        for c in contras:
            issues.append(LintIssue(
                check="contradiction_scan", severity="info",
                file=str(md.relative_to(vault_dir)),
                message=f"Contradiction ({c.severity}): [{c.source_a}] vs [{c.source_b}]",
            ))

    return issues


# ---------------------------------------------------------------------------
# Main lint runner
# ---------------------------------------------------------------------------

_ALL_CHECKS = {
    "orphan_pages": check_orphan_pages,
    "broken_wikilinks": check_broken_wikilinks,
    "frontmatter_validation": check_frontmatter_validation,
    "tag_audit": check_tag_audit,
    "stale_content": check_stale_content,
    "page_size": check_page_size,
    "index_completeness": check_index_completeness,
    "contradiction_scan": check_contradiction_scan,
}


def run_lint(
    vault_dir: Path,
    schema: dict | None = None,
    checks: list[str] | None = None,
) -> list[LintIssue]:
    """Run all or specified lint checks. Never raises — returns partial results on failure."""
    selected = checks or list(_ALL_CHECKS.keys())
    all_issues: list[LintIssue] = []

    for check_name in selected:
        fn = _ALL_CHECKS.get(check_name)
        if not fn:
            all_issues.append(LintIssue(
                check=check_name, severity="error",
                file="", message=f"Unknown check: {check_name}",
            ))
            continue

        try:
            if check_name == "tag_audit":
                issues = fn(vault_dir, schema=schema)
            else:
                issues = fn(vault_dir)
        except Exception as exc:
            all_issues.append(LintIssue(
                check=check_name, severity="error",
                file="", message=f"Check failed: {exc}",
            ))
            continue

        all_issues.extend(issues)

    return all_issues


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Vault health linting — 8 checks, zero LLM cost")
    parser.add_argument("--vault-dir", required=True, help="Path to vault root")
    parser.add_argument("--check", action="append", dest="checks", help="Run specific check(s)")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    args = parser.parse_args()

    vault_dir = Path(args.vault_dir)

    # Load schema for tag_audit
    schema = None
    try:
        from entity_extractor import load_schema
        schema = load_schema(vault_dir)
    except ImportError:
        pass

    issues = run_lint(vault_dir, schema=schema, checks=args.checks)

    if args.format == "json":
        print(json.dumps([asdict(i) for i in issues], indent=2))
    else:
        # Group by severity
        errors = [i for i in issues if i.severity == "error"]
        warnings = [i for i in issues if i.severity == "warning"]
        infos = [i for i in issues if i.severity == "info"]

        print(f"\n=== Vault Lint Report ===")
        print(f"Errors: {len(errors)} | Warnings: {len(warnings)} | Info: {len(infos)}\n")

        for severity, group in [("ERROR", errors), ("WARNING", warnings), ("INFO", infos)]:
            if not group:
                continue
            print(f"--- {severity} ---")
            for issue in group:
                print(f"  [{issue.check}] {issue.file}: {issue.message}")
            print()


if __name__ == "__main__":
    main()
