#!/usr/bin/env python3
"""Independent never-ship gate for the public export.

WHY THIS EXISTS
---------------
``sanitize.py`` has two layers that look independent but are not: the
REPLACEMENTS rules that scrub content, and the LEAK_PATTERNS that validate the
result. LEAK_PATTERNS are written as twins of REPLACEMENTS, so any spelling the
scrub rule cannot physically match, the validator cannot flag either. The check
and the thing being checked share a brain.

On 2026-08-16 that cost six leaks in a single session -- a lowercase org slug, an
X handle inside a longer token, a separator-stripped home path, two tax modules
carrying a real LLC + business address, and client names embedded between
underscores. Every one of them passed "PASSED zero leaks detected". Not one was
caught by automation; all six were found by a human grepping the staged tree.

This gate is deliberately DUMB and deliberately SEPARATE:

  * literal substrings, case-insensitive -- no regex, so there is no clever
    boundary (``\\b``) or casing assumption that can silently fail to match
  * it imports NOTHING from sanitize.py, so it cannot inherit a blind spot
  * its token list is curated by hand from the vault, so it is an INDEPENDENT
    oracle rather than a restatement of the scrub rules

A token list is allowed to be blunt. False positives cost one line in an
allowlist; a false negative costs a public leak.

USAGE
-----
    python scripts/never_ship_check.py                     # default export dir
    python scripts/never_ship_check.py --export-dir PATH
    python scripts/never_ship_check.py --list PATH         # alternate token file

Exit codes: 0 = clean, 1 = tokens found (or list missing/empty), 2 = bad usage.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXPORT = REPO_ROOT.parent / "thehomie-framework"
DEFAULT_LIST = REPO_ROOT / "scripts" / "never-ship.txt"
EXAMPLE_LIST = REPO_ROOT / "scripts" / "never-ship.example.txt"

# Binary/vendored trees are not worth scanning and produce noise.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".mp3", ".mp4", ".wav",
    ".woff", ".woff2", ".ttf", ".eot", ".zip", ".gz", ".pdf", ".onnx", ".bin",
}


def load_tokens(list_path: Path) -> tuple[list[str], list[str]]:
    """Return (tokens, allow_substrings).

    Format: one token per line. ``#`` starts a comment. A line beginning with
    ``!`` is an ALLOW entry -- a substring that legitimately contains a token
    (e.g. the canonical public repo URL contains the old org handle). Allow
    entries are matched on the surrounding line, not the token.
    """
    tokens: list[str] = []
    allows: list[str] = []
    for raw in list_path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("!"):
            allows.append(line[1:].strip().lower())
        else:
            tokens.append(line.lower())
    return tokens, allows


def iter_files(root: Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIXES:
            continue
        yield p


def scan(export_dir: Path, tokens: list[str], allows: list[str]) -> list[tuple[str, int, str, str]]:
    """Return [(relpath, lineno, token, line)] for every unallowed hit."""
    hits: list[tuple[str, int, str, str]] = []
    for path in iter_files(export_dir):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        low = text.lower()
        # Cheap pre-filter: skip the per-line walk unless the file matches.
        present = [t for t in tokens if t in low]
        if not present:
            continue
        rel = path.relative_to(export_dir).as_posix()
        for lineno, line in enumerate(text.splitlines(), 1):
            ll = line.lower()
            if any(a in ll for a in allows):
                continue
            for tok in present:
                if tok in ll:
                    hits.append((rel, lineno, tok, line.strip()[:160]))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="Independent never-ship gate for the public export")
    ap.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT)
    ap.add_argument("--list", dest="list_path", type=Path, default=DEFAULT_LIST)
    args = ap.parse_args()

    if not args.export_dir.is_dir():
        print(f"never-ship: export dir not found: {args.export_dir}", file=sys.stderr)
        return 2

    list_path = args.list_path
    if not list_path.is_file():
        print(
            f"never-ship: token list not found at {list_path}\n"
            f"  This gate FAILS CLOSED: a missing list means no protection, which is\n"
            f"  indistinguishable from a clean run. Copy {EXAMPLE_LIST.name} to\n"
            f"  {list_path.name} and fill it in from your vault.",
            file=sys.stderr,
        )
        return 1

    tokens, allows = load_tokens(list_path)
    if not tokens:
        print(f"never-ship: token list {list_path} is empty — failing closed.", file=sys.stderr)
        return 1

    hits = scan(args.export_dir, tokens, allows)
    if not hits:
        print(f"never-ship: CLEAN — {len(tokens)} tokens checked against {args.export_dir}")
        return 0

    print(f"never-ship: BLOCKED — {len(hits)} hit(s) across {len({h[0] for h in hits})} file(s)\n",
          file=sys.stderr)
    for rel, lineno, tok, line in hits[:60]:
        print(f"  {rel}:{lineno}  [{tok}]  {line}", file=sys.stderr)
    if len(hits) > 60:
        print(f"  ... and {len(hits) - 60} more", file=sys.stderr)
    print(
        "\nFix in scripts/sanitize.py (scrub rule + leak-validation twin + a test),\n"
        "never by hand-editing the public repo. If a hit is legitimate, add an\n"
        "allow line to the token list beginning with '!'.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
