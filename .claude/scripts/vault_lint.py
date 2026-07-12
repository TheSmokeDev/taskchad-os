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
import hashlib
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

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override  # noqa: E402

apply_persona_override()


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
                   ".conversations-archived", ".nexus", ".workspaces", ".workspaces-archived",
                   # Native /design artifacts + bundled DESIGN.md systems: HTML
                   # artifacts and brand-system docs do not follow the vault-note
                   # frontmatter schema (see .claude/scripts/design/).
                   "design",
                   # Ops ledger (append-only history, discovery/review reports):
                   # operational telemetry migrated from unified-vault 2026-07-11.
                   # Its records cite cross-vault notes by design — not lintable
                   # knowledge content.
                   "_ops"}


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
# Per-file cores — shared by the full-scan check_* functions AND the delta path
# so incremental output is byte-identical to a full scan.
# ---------------------------------------------------------------------------


def _extract_links(content: str) -> list[str]:
    """Ordered wikilink targets (duplicates preserved) after stripping code.

    ``.strip()`` per occurrence mirrors both original scanners; the trailing
    ``.rstrip("\\")`` used by broken_wikilinks is applied at evaluation time so
    orphan_pages (which does NOT rstrip) and broken_wikilinks can share one
    stored link list.
    """
    stripped = _strip_code_blocks(content)
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(stripped)]


def _is_concept_rel(rel: Path) -> bool:
    """True for ``concepts/<slug>.md`` direct children (excl. BUILD-LOG*/INDEX).

    Mirrors ``_concept_files`` (a NON-recursive ``concepts/*.md`` glob), so a
    nested ``concepts/sub/x.md`` is correctly excluded.
    """
    parts = rel.parts
    return (
        len(parts) == 2
        and parts[0] == "concepts"
        and not rel.stem.startswith("BUILD-LOG")
        and rel.stem != "INDEX"
    )


def _frontmatter_skip(rel: Path) -> bool:
    """Files excluded from frontmatter_validation (machine-stamped / auto-gen)."""
    if rel.parts and rel.parts[0] in ("_dashboards", "cofounder", "daily", "teams"):
        return True
    if rel.name == "BUILD-LOG.md":
        return True
    return False


def _frontmatter_issues_for(rel: Path, content: str) -> list[LintIssue]:
    """Per-file frontmatter_validation core."""
    if _frontmatter_skip(rel):
        return []
    required = {"tags", "date"}  # Minimum required for all note types
    issues: list[LintIssue] = []
    fm = _parse_frontmatter(content)
    if not fm:
        issues.append(LintIssue(
            check="frontmatter_validation", severity="error",
            file=str(rel), message="Missing frontmatter block",
        ))
        return issues
    for field_name in required:
        if field_name not in fm:
            issues.append(LintIssue(
                check="frontmatter_validation", severity="error",
                file=str(rel), message=f"Missing required field: {field_name}",
            ))
    return issues


def _tag_issues_for(rel: Path, content: str, valid_tags) -> list[LintIssue]:
    """Per-file tag_audit core (caller guarantees ``valid_tags`` is truthy)."""
    issues: list[LintIssue] = []
    fm = _parse_frontmatter(content)
    for tag in fm.get("tags", []):
        if tag and tag not in valid_tags:
            issues.append(LintIssue(
                check="tag_audit", severity="warning",
                file=str(rel), message=f"Tag not in schema taxonomy: '{tag}'",
            ))
    return issues


def _page_size_issue_for(rel: Path, content: str, max_lines: int = 200) -> list[LintIssue]:
    """Per-file page_size core (caller restricts to concept files)."""
    line_count = len(content.splitlines())
    if line_count > max_lines:
        return [LintIssue(
            check="page_size", severity="warning",
            file=str(rel),
            message=f"{line_count} lines (max {max_lines}) — consider splitting",
        )]
    return []


def _contradiction_issues_for(md: Path, rel: Path) -> list[LintIssue]:
    """Per-file contradiction_scan core (caller restricts to concept files)."""
    issues: list[LintIssue] = []
    try:
        from entity_extractor import check_contradictions
    except ImportError:
        return issues
    try:
        contras = check_contradictions(md)
    except Exception:
        return issues
    for c in contras:
        issues.append(LintIssue(
            check="contradiction_scan", severity="info",
            file=str(rel),
            message=f"Contradiction ({c.severity}): [{c.source_a}] vs [{c.source_b}]",
        ))
    return issues


def _stale_issue_from_date(rel: Path, page_date: str, days: int, cutoff: str) -> list[LintIssue]:
    """Per-file stale_content core — time-dependent, recomputed every run."""
    if page_date and page_date < cutoff:
        return [LintIssue(
            check="stale_content", severity="info",
            file=str(rel),
            message=f"Last updated {page_date} (>{days} days old)",
        )]
    return []


def _content_pure_issues_for(md: Path, rel: Path, content: str, schema: dict | None) -> list[LintIssue]:
    """All content-pure per-file issues, cacheable in the delta state.

    Order within a file: frontmatter_validation, tag_audit, page_size,
    contradiction_scan. The delta assembler regroups these BY CHECK across
    files, so cross-check ordering does not depend on this order.
    """
    issues: list[LintIssue] = []
    issues.extend(_frontmatter_issues_for(rel, content))
    valid_tags = schema.get("tag_taxonomy") if schema else None
    if valid_tags:
        issues.extend(_tag_issues_for(rel, content, valid_tags))
    if _is_concept_rel(rel):
        issues.extend(_page_size_issue_for(rel, content))
        issues.extend(_contradiction_issues_for(md, rel))
    return issues


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
            content = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        referenced.update(_extract_links(content))

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
            content = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        rel = str(md.relative_to(vault_dir))
        for link in _extract_links(content):
            target = link.rstrip("\\")  # strip trailing \ from table-cell aliases
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
    for md in _all_md_files(vault_dir):
        # Skip auto-generated infrastructure files (cofounder project
        # frontmatter is machine-stamped: created/last_run, no date field)
        rel = md.relative_to(vault_dir)
        if _frontmatter_skip(rel):
            continue

        try:
            content = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        issues.extend(_frontmatter_issues_for(rel, content))

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

        issues.extend(_tag_issues_for(md.relative_to(vault_dir), content, valid_tags))

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
        issues.extend(
            _stale_issue_from_date(md.relative_to(vault_dir), fm.get("date", ""), days, cutoff)
        )

    return issues


def check_page_size(vault_dir: Path, max_lines: int = 200) -> list[LintIssue]:
    """Flag files exceeding max_lines."""
    issues = []
    for md in _concept_files(vault_dir):
        try:
            content = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        issues.extend(_page_size_issue_for(md.relative_to(vault_dir), content, max_lines))

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
        from entity_extractor import check_contradictions  # noqa: F401 — import probe
    except ImportError:
        return issues

    for md in _concept_files(vault_dir):
        issues.extend(_contradiction_issues_for(md, md.relative_to(vault_dir)))

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


def _run_lint_full(
    vault_dir: Path,
    schema: dict | None,
    selected: list[str],
) -> list[LintIssue]:
    """Full scan: run each selected check top-to-bottom. Never raises."""
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


def run_lint(
    vault_dir: Path,
    schema: dict | None = None,
    checks: list[str] | None = None,
    delta: bool | None = None,
) -> list[LintIssue]:
    """Run all or specified lint checks. Never raises — returns partial results on failure.

    ``delta`` (None → resolve ``LINT_DELTA_ENABLED``) selects the incremental
    path: only changed/new files are re-checked, unchanged files replay their
    cached content-pure issues, and the whole thing is wrapped so any failure
    falls back to a full scan. The output is byte-identical to a full scan by
    construction. A ``--check`` subset ignores delta entirely (full-scan subset,
    state untouched).
    """
    if delta is None:
        delta = _resolve_delta_default()

    selected = checks or list(_ALL_CHECKS.keys())
    is_full_set = checks is None or set(checks) == set(_ALL_CHECKS.keys())

    if not delta or not is_full_set:
        return _run_lint_full(vault_dir, schema, selected)

    try:
        return _run_lint_delta(vault_dir, schema)
    except Exception:
        return _run_lint_full(vault_dir, schema, selected)


# ---------------------------------------------------------------------------
# Delta lint — incremental state (content-hash mirror of memory_index)
# ---------------------------------------------------------------------------

_LINT_STATE_VERSION = 1


def _resolve_delta_default() -> bool:
    """Resolve ``LINT_DELTA_ENABLED`` lazily (config, then os.getenv fallback)."""
    try:
        import config

        return config.get_lint_delta_enabled()
    except ImportError:
        import os

        return os.getenv("LINT_DELTA_ENABLED", "false").lower() == "true"


def _lint_state_path(vault_dir: Path) -> Path:
    return vault_dir / "_state" / "lint-state.json"


def _load_lint_state(vault_dir: Path) -> dict | None:
    """Return the persisted lint state, or None on missing/corrupt/version-mismatch.

    Intentionally NOT ``shared.load_state`` — that returns ``{}`` for both empty
    and corrupt, and delta needs to distinguish "rebuild from scratch" (None)
    from a valid state.
    """
    path = _lint_state_path(vault_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != _LINT_STATE_VERSION or data.get("hash_algo") != "sha256":
        return None
    if not isinstance(data.get("files"), dict):
        return None
    return data


def _save_lint_state(vault_dir: Path, files_data: dict) -> None:
    """Best-effort atomic save of the lint state (all errors swallowed)."""
    try:
        from datetime import datetime, timezone

        from shared import save_state

        payload = {
            "version": _LINT_STATE_VERSION,
            "hash_algo": "sha256",
            "generated": datetime.now(timezone.utc).isoformat(),
            "files": files_data,
        }
        save_state(payload, _lint_state_path(vault_dir))
    except Exception:
        pass


def _compute_file_record(md: Path, rel: Path, schema: dict | None, content_hash: str | None) -> dict:
    """Fresh per-file record: hash + links + fm_date + content-pure issues.

    An unreadable file yields an empty record — mirroring the full scan, where
    every check independently ``continue``s past a read error (no issues, no
    links).
    """
    try:
        content = md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"hash": content_hash, "links": [], "fm_date": None, "issues": []}
    fm = _parse_frontmatter(content)
    return {
        "hash": content_hash,
        "links": _extract_links(content),
        "fm_date": fm.get("date"),
        "issues": [asdict(i) for i in _content_pure_issues_for(md, rel, content, schema)],
    }


def _assemble_from_data(
    vault_dir: Path,
    md_files: list[Path],
    concept_files: list[Path],
    files_data: dict,
    days: int = 90,
) -> list[LintIssue]:
    """Assemble the full issue list from per-file records, in ``_ALL_CHECKS`` order.

    Global checks (orphan/broken/index) and stale_content are recomputed from
    the in-memory link map / stored ``fm_date``; the content-pure checks are
    replayed from cached records and REGROUPED by check so ordering matches a
    full scan exactly.
    """
    result: list[LintIssue] = []
    concept_slugs = {f.stem for f in concept_files}
    all_stems = {md.stem for md in md_files}
    links_by_rel = {rel: rec.get("links", []) for rel, rec in files_data.items()}
    issues_by_rel = {
        rel: [LintIssue(**d) for d in rec.get("issues", [])]
        for rel, rec in files_data.items()
    }

    # 1. orphan_pages — concept slugs with no inbound link from non-concept,
    #    non-connection files (mirrors check_orphan_pages; .strip(), no rstrip).
    if concept_slugs:
        referenced: set[str] = set()
        for rel, links in links_by_rel.items():
            parts = Path(rel).parts
            if parts and parts[0] in ("concepts", "connections"):
                continue
            referenced.update(links)
        for slug in sorted(concept_slugs):
            if slug not in referenced:
                result.append(LintIssue(
                    check="orphan_pages", severity="warning",
                    file=f"concepts/{slug}.md",
                    message="No inbound wikilinks from non-concept files",
                ))

    # 2. broken_wikilinks — per-occurrence in _all_md_files order (rstrip at eval).
    for md in md_files:
        rel = md.relative_to(vault_dir)
        for link in links_by_rel.get(rel.as_posix(), []):
            target = link.rstrip("\\")
            if target not in all_stems:
                result.append(LintIssue(
                    check="broken_wikilinks", severity="error",
                    file=str(rel), message=f"Broken wikilink: [[{target}]]",
                ))

    # 3-4. frontmatter_validation, tag_audit — all files, regrouped by check.
    for check_name in ("frontmatter_validation", "tag_audit"):
        for md in md_files:
            for iss in issues_by_rel.get(md.relative_to(vault_dir).as_posix(), []):
                if iss.check == check_name:
                    result.append(iss)

    # 5. stale_content — concept files, recomputed from stored fm_date every run.
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    for f in concept_files:
        rec = files_data.get(f.relative_to(vault_dir).as_posix())
        page_date = (rec.get("fm_date") if rec else None) or ""
        result.extend(_stale_issue_from_date(f.relative_to(vault_dir), page_date, days, cutoff))

    # 6. page_size — concept files, replayed.
    for f in concept_files:
        for iss in issues_by_rel.get(f.relative_to(vault_dir).as_posix(), []):
            if iss.check == "page_size":
                result.append(iss)

    # 7. index_completeness — read INDEX.md + concept list live (cheap, exact).
    result.extend(check_index_completeness(vault_dir))

    # 8. contradiction_scan — concept files, replayed.
    for f in concept_files:
        for iss in issues_by_rel.get(f.relative_to(vault_dir).as_posix(), []):
            if iss.check == "contradiction_scan":
                result.append(iss)

    return result


def _run_lint_delta(vault_dir: Path, schema: dict | None) -> list[LintIssue]:
    """Incremental lint: hash every file, recompute only changed records."""
    state = _load_lint_state(vault_dir)
    md_files = _all_md_files(vault_dir)
    concept_files = _concept_files(vault_dir)
    old_files = state.get("files", {}) if state else {}

    # Hash every file once (bytes only — cheap vs re-running every check).
    hashes: dict[str, str | None] = {}
    for md in md_files:
        rel_posix = md.relative_to(vault_dir).as_posix()
        try:
            hashes[rel_posix] = hashlib.sha256(md.read_bytes()).hexdigest()
        except OSError:
            hashes[rel_posix] = None

    # SCHEMA.md governs tag_audit taxonomy — if it changed, the cached tag issues
    # for untouched files are stale, so force a full recompute this run.
    force_full = state is None
    if state is not None:
        if old_files.get("SCHEMA.md", {}).get("hash") != hashes.get("SCHEMA.md"):
            force_full = True

    files_data: dict = {}
    for md in md_files:
        rel = md.relative_to(vault_dir)
        rel_posix = rel.as_posix()
        h = hashes[rel_posix]
        cached = old_files.get(rel_posix)
        if not force_full and cached and h is not None and cached.get("hash") == h:
            files_data[rel_posix] = cached
        else:
            files_data[rel_posix] = _compute_file_record(md, rel, schema, h)

    result = _assemble_from_data(vault_dir, md_files, concept_files, files_data)
    _save_lint_state(vault_dir, files_data)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Vault health linting — 8 checks, zero LLM cost")
    parser.add_argument("--vault-dir", required=True, help="Path to vault root")
    parser.add_argument("--check", action="append", dest="checks", help="Run specific check(s)")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    parser.add_argument(
        "--delta", action="store_true",
        help="Incremental lint — only re-check changed files (state in {vault}/_state/lint-state.json)",
    )
    args = parser.parse_args()

    vault_dir = Path(args.vault_dir)

    # Load schema for tag_audit
    schema = None
    try:
        from entity_extractor import load_schema
        schema = load_schema(vault_dir)
    except ImportError:
        pass

    issues = run_lint(
        vault_dir, schema=schema, checks=args.checks,
        delta=True if args.delta else None,
    )

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
