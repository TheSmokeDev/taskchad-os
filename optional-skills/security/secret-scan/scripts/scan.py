#!/usr/bin/env python3
"""Lightweight secret scanner for the secret-scan optional skill.

Walks a path and flags lines that look like leaked credentials. Read-only:
reports findings and exits non-zero if any are found. Never edits files.

Usage:
    scan.py <path> [--quiet]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# (label, compiled pattern). High-signal, low-noise.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("Slack token", re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}")),
    ("GitHub token", re.compile(r"gh[pousr]_[0-9A-Za-z]{36,}")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b")),
    ("OpenAI key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("Anthropic key", re.compile(r"sk-ant-[A-Za-z0-9\-]{20,}")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("Generic secret assignment",
     re.compile(r"(?i)(?:secret|password|passwd|api[_-]?key|token)\s*[:=]\s*['\"][^'\"]{8,}['\"]")),
]

PLACEHOLDERS = re.compile(r"(?i)(your[_-]?key|example|placeholder|xxxx+|changeme|<[^>]+>)")

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
SKIP_NAMES = {".env.example"}
TEXT_SUFFIXES = {".md", ".py", ".js", ".ts", ".json", ".yml", ".yaml", ".sh",
                 ".txt", ".env", ".toml", ".cfg", ".ini", ".html", ".tsx", ".jsx"}


def is_textual(path: Path) -> bool:
    if path.suffix in TEXT_SUFFIXES or path.name.startswith(".env"):
        return path.name not in SKIP_NAMES
    return False


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    findings = []
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return findings
    for lineno, line in enumerate(text.splitlines(), 1):
        for label, pat in PATTERNS:
            if pat.search(line) and not PLACEHOLDERS.search(line):
                findings.append((lineno, label, line.strip()[:120]))
    return findings


def main() -> None:
    p = argparse.ArgumentParser(description="Scan for leaked secrets.")
    p.add_argument("path", help="File or directory to scan.")
    p.add_argument("--quiet", action="store_true", help="Only print on findings.")
    args = p.parse_args()

    root = Path(args.path)
    targets: list[Path]
    if root.is_file():
        targets = [root]
    else:
        targets = [
            f for f in root.rglob("*")
            if f.is_file()
            and is_textual(f)
            and not any(part in SKIP_DIRS for part in f.parts)
        ]

    total = 0
    for f in targets:
        for lineno, label, snippet in scan_file(f):
            total += 1
            print(f"{f}:{lineno}: [{label}] {snippet}")

    if total:
        print(f"\n✗ {total} potential secret(s) found.", file=sys.stderr)
        sys.exit(1)
    if not args.quiet:
        print(f"✓ No secrets found in {len(targets)} file(s).")


if __name__ == "__main__":
    main()
