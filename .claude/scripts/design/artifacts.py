"""Design path resolution — two distinct roots, by intent.

There are two kinds of files, and they live in DIFFERENT trees on purpose:

1. **Bundled brand-system library** — a TRACKED framework asset that ships with
   the repo (private AND public), so a fresh clone can run ``/design system
   <slug>`` with no setup. Lives BESIDE this module:

       .claude/scripts/design/_systems/<slug>/   # DESIGN.md + tokens.css + ...

2. **Generated artifacts** — the operator's outputs. Personal + regenerable, so
   they stay in the gitignored, sanitizer-denied vault (never shipped):

       vault/memory/design/<slug>/<kind>-YYYYMMDD/finalized.html

The vault ``design`` dir is in ``vault_lint._SKIP_LINT_DIRS`` (HTML artifacts are
not vault-note frontmatter). The bundled library ships; the artifacts do not.
"""

from __future__ import annotations

import re
from pathlib import Path

# The design module's own directory (.claude/scripts/design/). The bundled
# system library lives here so it is version-controlled and ships with the repo.
_PACKAGE_DIR = Path(__file__).resolve().parent

# Rule 1: never bind config paths as defaults — resolve at call time so tests
# (and a relocated vault) can override config.MEMORY_DIR.


def _memory_dir() -> Path:
    """Resolve the canonical vault dir at call time (Rule 1 — no cached default)."""
    from config import MEMORY_DIR

    return Path(MEMORY_DIR)


def design_root(memory_dir: Path | None = None) -> Path:
    """Return ``<vault>/design`` — the root for GENERATED artifacts (gitignored)."""
    base = memory_dir if memory_dir is not None else _memory_dir()
    return Path(base) / "design"


def systems_root(systems_dir: Path | None = None) -> Path:
    """Return the bundled brand-system library root — a TRACKED framework asset
    at ``.claude/scripts/design/_systems`` (NOT under the gitignored vault), so
    it ships with the repo. ``systems_dir`` overrides for tests."""
    if systems_dir is not None:
        return Path(systems_dir)
    return _PACKAGE_DIR / "_systems"


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, max_len: int = 48, fallback: str = "design") -> str:
    """kebab-case slug from free text; ASCII-only, bounded length."""
    lowered = (text or "").strip().lower()
    slug = _SLUG_STRIP.sub("-", lowered).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or fallback


def artifact_dir(
    slug: str,
    kind: str,
    *,
    date_str: str,
    memory_dir: Path | None = None,
) -> Path:
    """Resolve ``<vault>/design/<slug>/<kind>-<date>`` (not created here).

    ``date_str`` is passed in (YYYYMMDD) rather than computed — keeps this
    deterministic/testable and avoids the workflow ``Date.now()`` trap.
    """
    safe_kind = slugify(kind, fallback="artifact")
    return design_root(memory_dir) / slug / f"{safe_kind}-{date_str}"
