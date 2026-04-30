#!/usr/bin/env python3
"""
Finance Vault Auto-Linker — Karpathy Knowledge Graph Layer.

Scans every concept page body and injects [[SLUG|display text]] wikilinks
wherever a known concept is mentioned in plain text. Transforms isolated
silos into a navigable knowledge graph with associative trails.

Rules:
  - Longest keyword match wins (avoids partial matches)
  - One link per concept slug per "## From [[source]]" section
  - Skips: frontmatter, ## headers, existing [[wikilinks]], code blocks
  - Never self-links (SELF-EMPLOYMENT page won't link to itself)
  - Idempotent — safe to re-run, won't double-link

Usage:
    PYTHONUTF8=1 uv run python vault_autolink.py --vault-dir "~/finance-vault"
    PYTHONUTF8=1 uv run python vault_autolink.py --vault-dir "..." --dry-run
    PYTHONUTF8=1 uv run python vault_autolink.py concepts/SELF-EMPLOYMENT.md --vault-dir "..."
"""

import re
import sys
import argparse
from pathlib import Path

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override

apply_persona_override()

DEFAULT_VAULT = Path(r"C:\Users\YourUser\finance-vault")

# ---------------------------------------------------------------------------
# Finance domain keyword → concept slug map
# Curated for Example Holdings LLC / Example Insurance context.
# Longer phrases MUST come before shorter ones for longest-match to work.
# ---------------------------------------------------------------------------
FINANCE_LINK_MAP = {
    # ── Business entities ────────────────────────────────────────────────
    "Example Holdings LLC":              "ROAD-SHIELD-LLC",
    "Example Insurance":            "ROAD-SHIELD-LLC",
    "EIN 92-3073139":               "ROAD-SHIELD-LLC",
    "ExampleParent Franchise":             "ROAD-SHIELD-LLC",
    "ExampleParent":                       "ROAD-SHIELD-LLC",
    "ExampleParent":                       "ROAD-SHIELD-LLC",

    # ── Income concepts ──────────────────────────────────────────────────
    "ordinary business income":     "ORDINARY-BUSINESS-INCOME",
    "ExampleParent franchise wire":        "FRANCHISE-REVENUE",
    "franchise wire":               "FRANCHISE-REVENUE",
    "franchise revenue":            "FRANCHISE-REVENUE",
    "franchise commission":         "FRANCHISE-REVENUE",
    "gross receipts":               "GROSS-RECEIPTS-OR-SALES",
    "partner distributions":        "PARTNER-DISTRIBUTIONS",
    "guaranteed payments":          "GUARANTEED-PAYMENTS",
    "distributions":                "PARTNER-DISTRIBUTIONS",

    # ── Tax concepts ─────────────────────────────────────────────────────
    "net SE earnings":              "SELF-EMPLOYMENT",
    "self-employment tax":          "SELF-EMPLOYMENT",
    "SE tax deduction":             "SELF-EMPLOYMENT",
    "SE tax":                       "SELF-EMPLOYMENT",
    "Section 199A deduction":       "SECTION-199A",
    "Section 199A":                 "SECTION-199A",
    "199A deduction":               "SECTION-199A",
    "QBI deduction":                "QBI",
    "QBI":                          "QBI",
    "home office":                  "HOME-OFFICE",
    "Schedule K-1":                 "SCHEDULE-K-1",
    "K-1 Box":                      "SCHEDULE-K-1",
    "Schedule E":                   "SCHEDULE-E",
    "Form 1065":                    "FORM-1065",
    "Form 2553":                    "S-CORPORATION",
    "reasonable salary":            "S-CORPORATION",
    "W-2 salary":                   "S-CORPORATION",
    "S-Corporation":                "S-CORPORATION",
    "S-Corp":                       "S-CORPORATION",
    "S Corp":                       "S-CORPORATION",

    # ── Expense concepts ─────────────────────────────────────────────────
    "123 Example Blvd":        "RETAIL-OFFICE-SPACE",
    "retail office space":          "RETAIL-OFFICE-SPACE",
    "retail office":                "RETAIL-OFFICE-SPACE",
    "Booxkeeping Corp":             "BOOKKEEPING-EXPENSES",
    "bookkeeping":                  "BOOKKEEPING-EXPENSES",
    "Vbs*Vonage":                   "COMMUNICATION-EXPENSES",
    "Vonage":                       "COMMUNICATION-EXPENSES",
    "MetTel":                       "COMMUNICATION-EXPENSES",
    "SDG&E":                        "UTILITY-EXPENSES",
    "SD Gas":                       "UTILITY-EXPENSES",
    "franchise fees":               "FRANCHISE-FEES",
    "Google Workspace":             "SOFTWARE-EXPENSES",
    "Google Gsuite":                "SOFTWARE-EXPENSES",
    "Midjourney":                   "SOFTWARE-EXPENSES",

    # ── Additional expense concepts ──────────────────────────────────────
    "Augusta Rule":                 "AUGUSTA-RULE",
    "§280A":                        "AUGUSTA-RULE",
    "vehicle mileage deduction":    "VEHICLE-MILEAGE",
    "vehicle mileage":              "VEHICLE-MILEAGE",
    "mileage deduction":            "VEHICLE-MILEAGE",
    "advertising expense":          "ADVERTISING-EXPENSES",
    "advertising":                  "ADVERTISING-EXPENSES",
    "repairs and maintenance":      "REPAIRS-MAINTENANCE",
    "repairs & maintenance":        "REPAIRS-MAINTENANCE",
    "business insurance":           "BUSINESS-INSURANCE",
    "E&O insurance":                "BUSINESS-INSURANCE",
    "general liability":            "BUSINESS-INSURANCE",
    "janitorial":                   "JANITORIAL-EXPENSES",
    "business meals":               "MEALS-EXPENSES",
    "professional dues":            "PROFESSIONAL-DUES",
    "amortization":                 "AMORTIZATION-EXPENSES",
    "office supplies":              "SUPPLIES-EXPENSES",
    "supplies":                     "SUPPLIES-EXPENSES",

    # ── Cash / banking ───────────────────────────────────────────────────
    "ending balance":               "CASH-FLOW",
    "beginning balance":            "CASH-FLOW",
    "cash flow":                    "CASH-FLOW",
    "banking fees":                 "BANKING-FEES",
    "service fee":                  "BANKING-FEES",
    "payroll and contractors":      "PAYROLL-EXPENSES",
    "payroll":                      "PAYROLL-EXPENSES",
    "referral fees":                "PAYROLL-EXPENSES",
    "meals expense":                "MEALS-EXPENSES",
    "meals":                        "MEALS-EXPENSES",

    # ── Structure ────────────────────────────────────────────────────────
    "50/50 partnership":            "PARTNERSHIP",
    "50/50 partner":                "PARTNERSHIP",
    "partnership income":           "PARTNERSHIP",
    "partnership":                  "PARTNERSHIP",
}

# Sort by keyword length descending — longest match wins
_SORTED_KEYWORDS = sorted(FINANCE_LINK_MAP.keys(), key=len, reverse=True)


# ---------------------------------------------------------------------------
# Core injection logic
# ---------------------------------------------------------------------------

def find_existing_link_spans(line: str) -> list[tuple[int, int]]:
    """Return (start, end) character ranges of existing [[wikilinks]] in a line."""
    return [(m.start(), m.end()) for m in re.finditer(r'\[\[.*?\]\]', line)]


def in_any_span(pos: int, end: int, spans: list[tuple[int, int]]) -> bool:
    """Check if (pos, end) overlaps with any existing span."""
    return any(s <= pos < e or s < end <= e for s, e in spans)


def inject_links_in_line(
    line: str,
    page_slug: str,
    already_linked: set[str],
) -> tuple[str, list[str]]:
    """
    Inject wikilinks for known concepts into a single body line.

    Args:
        line: The text line to process
        page_slug: Slug of the page being processed (to prevent self-links)
        already_linked: Slugs already linked in this section (modified in-place)

    Returns:
        (modified_line, list_of_new_slugs_linked)
    """
    new_slugs: list[str] = []
    result = line

    for keyword in _SORTED_KEYWORDS:
        slug = FINANCE_LINK_MAP[keyword]

        # Skip self-links and already-linked-in-section slugs
        if slug == page_slug or slug in already_linked:
            continue

        # Case-insensitive search
        pattern = re.compile(re.escape(keyword), re.IGNORECASE)
        match = pattern.search(result)
        if not match:
            continue

        # Check it's not inside an existing [[wikilink]]
        existing_spans = find_existing_link_spans(result)
        if in_any_span(match.start(), match.end(), existing_spans):
            continue

        # Inject link — preserve original display text
        display = match.group(0)
        link = f"[[{slug}|{display}]]"
        result = result[:match.start()] + link + result[match.end():]

        already_linked.add(slug)
        new_slugs.append(slug)

    return result, new_slugs


def process_page(content: str, page_slug: str, dry_run: bool = False) -> tuple[str, int]:
    """
    Process a concept page: inject wikilinks into body text only.

    Returns: (new_content, links_added_count)
    """
    lines = content.split("\n")
    result_lines: list[str] = []
    total_added = 0

    in_frontmatter = False
    frontmatter_done = False
    in_code_block = False
    in_section = False
    section_linked: set[str] = set()

    for i, line in enumerate(lines):
        # ── Frontmatter detection ────────────────────────────────────────
        if i == 0 and line.strip() == "---":
            in_frontmatter = True
            result_lines.append(line)
            continue
        if in_frontmatter:
            result_lines.append(line)
            if line.strip() == "---":
                in_frontmatter = False
                frontmatter_done = True
            continue

        if not frontmatter_done:
            result_lines.append(line)
            continue

        # ── Code block detection ─────────────────────────────────────────
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            result_lines.append(line)
            continue
        if in_code_block:
            result_lines.append(line)
            continue

        # ── Section header: ## From [[source]] ──────────────────────────
        if line.startswith("## "):
            # New section — reset per-section linked set
            in_section = True
            section_linked = set()
            result_lines.append(line)
            continue

        # ── Top-level heading (# Title) — skip ──────────────────────────
        if line.startswith("# "):
            result_lines.append(line)
            continue

        # ── Body text — inject links ─────────────────────────────────────
        if line.strip():
            modified, new_slugs = inject_links_in_line(line, page_slug, section_linked)
            result_lines.append(modified)
            total_added += len(new_slugs)
        else:
            result_lines.append(line)

    return "\n".join(result_lines), total_added


def slug_from_path(path: Path) -> str:
    """Derive concept slug from file path (filename without extension, uppercase)."""
    return path.stem.upper()


# ---------------------------------------------------------------------------
# File processing
# ---------------------------------------------------------------------------

def process_file(path: Path, dry_run: bool = False) -> dict:
    """Process one concept page. Returns result dict."""
    content = path.read_text(encoding="utf-8")
    page_slug = slug_from_path(path)

    new_content, links_added = process_page(content, page_slug, dry_run)

    changed = new_content != content
    if changed and not dry_run:
        path.write_text(new_content, encoding="utf-8")

    return {
        "file": path.name,
        "slug": page_slug,
        "links_added": links_added,
        "changed": changed,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Finance Vault Auto-Linker — inject [[wikilinks]] into concept pages"
    )
    parser.add_argument("files", nargs="*", help="Specific concept page .md files to process")
    parser.add_argument("--vault-dir", default=str(DEFAULT_VAULT),
                        help="Finance vault root directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without writing files")
    args = parser.parse_args()

    vault_dir = Path(args.vault_dir)
    concepts_dir = vault_dir / "concepts"

    if args.files:
        files = [Path(f) for f in args.files]
    else:
        # All concept pages (exclude INDEX, BUILD-LOG)
        files = [
            f for f in sorted(concepts_dir.glob("*.md"))
            if f.stem not in ("INDEX", "BUILD-LOG")
        ]

    if not files:
        print("[!] No concept pages found.")
        sys.exit(1)

    mode = "[DRY RUN] " if args.dry_run else ""
    print(f"{mode}Autolinking {len(files)} concept pages in {concepts_dir}")
    print()

    total_pages_changed = 0
    total_links = 0

    for path in files:
        if not path.exists():
            print(f"  [skip] {path.name} — not found")
            continue

        result = process_file(path, dry_run=args.dry_run)

        if result["links_added"]:
            status = "[preview]" if args.dry_run else "[ok]"
            print(f"  {status} {result['file']} — {result['links_added']} links added")
            total_links += result["links_added"]
            if result["changed"]:
                total_pages_changed += 1
        # Silent for pages with no changes (no noise)

    print()
    action = "would add" if args.dry_run else "added"
    print(f"[ok] Done. {action} {total_links} links across "
          f"{total_pages_changed} pages.")

    # Append to LOG.md
    if not args.dry_run and total_links > 0:
        log_path = vault_dir / "LOG.md"
        from datetime import date
        entry = (f"\n## [{date.today().isoformat()}] autolink | "
                 f"vault_autolink pass | {total_links} links across "
                 f"{total_pages_changed} pages\n")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)


if __name__ == "__main__":
    main()
