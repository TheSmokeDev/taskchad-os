"""Identity payload shim — single source for reading identity files into a dict.

This module is the canonical entry point used by the chat engine and the cron
memory pipelines (reflect / weekly / dream) for assembling the identity-file
payload (SOUL, SELF, USER, MEMORY, GOALS, WORKING, SAFETY). Each consumer keeps
its own prompt assembly + ordering + headers; the shim only hands back raw file
content keyed by uppercase name.

Design rules enforced here:
- **Rule 1**: ``include`` defaults to ``None`` (sentinel). Resolved to
  ``DEFAULT_INCLUDE`` inside the function body so runtime overrides of either
  the include set or the underlying read helper propagate. There is NO
  ``budget`` parameter — consumers apply their existing truncation.
- **Rule 2**: file reads happen inside the function body on every call. No
  module-level caching of file content. The only module-level work is
  computing ``_SCRIPTS_DIR`` (a pure ``pathlib.Path``) and inserting it into
  ``sys.path`` so ``runtime.bootstrap`` resolves when this module is imported
  from ``.claude/chat`` (where ``runtime`` is not a sibling package).

Fail-open contract (matches ``runtime.bootstrap.read_file_safe``):
- Missing files → key is ABSENT from the returned dict (NOT empty string,
  NOT ``None``).
- Empty ``memory_dir`` → ``{}``.
- ``OSError`` and other read failures during the read are swallowed by
  ``read_file_safe`` and surface as an absent key — exceptions never escape.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Self-bootstrap: when imported from .claude/chat (e.g. by engine.py),
# ``runtime`` is not a sibling package. Inject .claude/scripts/ onto sys.path
# so the lazy ``runtime.bootstrap`` import resolves. This block is the only
# module-level work — no file reads, no I/O.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


DEFAULT_INCLUDE: tuple[str, ...] = (
    "SOUL",
    "SELF",
    "USER",
    "MEMORY",
    "GOALS",
    "WORKING",
    # Issue #484: SAFETY.md carries a persona's hard boundaries (spend
    # ceilings, default-deny surfaces). Wired here so it reaches the prompt
    # deterministically instead of depending on a recall match. Stub-gated in
    # build_identity_payload — an unedited lifecycle seed yields NO key.
    "SAFETY",
)

# --- Authored-vs-stub predicate (#484, tightened in r2) --------------------
# A seeded identity file is a KNOWN scaffold: a frontmatter block carrying
# exactly the seeder's two keys (``profile`` / ``identity_file``), one H1
# whose text is the file's own title, and an HTML seed comment. The
# predicate recognizes ONLY that scaffold and treats everything else as
# authored — it never strips general shapes, because an operator may
# legitimately write policy AS frontmatter keys or AS multiple H1 headings,
# and a shape-stripping predicate would silently discard those rules (the
# exact recall-dependent disappearance #484 exists to prevent). It is not a
# string match on the seed comment text either, so a template reword cannot
# break it. Every malformed input errs toward SHOWING safety content:
# unclosed comments, unclosed frontmatter, and unrecognized lines all
# classify as authored.
_FRONTMATTER_BLOCK_RE = re.compile(r"\A---[ \t]*\n(?P<body>.*?)\n---[ \t]*\n?", re.DOTALL)
_FRONTMATTER_SCAFFOLD_KEY_RE = re.compile(r"^(?:profile|identity_file)[ \t]*:")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_H1_HEADING_RE = re.compile(r"^#(?!#)[ \t]+(?P<title>.*?)[ \t]*$", re.MULTILINE)


def has_authored_content(text: str, *, scaffold_title: str) -> bool:
    """True when *text* carries content beyond the seeder's known scaffold.

    Scaffold recognition (anything outside it counts as authored):

    - Frontmatter: only the seeder's own keys (``profile``,
      ``identity_file``) are scaffold. ANY other frontmatter line — an extra
      key, a list item, a comment — is authored content (policy may live in
      frontmatter). An unclosed frontmatter block is left in place and thus
      counts as authored.
    - Headings: only ONE H1, and only when its text equals
      ``scaffold_title`` (the seeded ``# <FILE-STEM>`` line), is scaffold.
      A second H1, or a first H1 with any other text, is authored content —
      constraints written as H1 headings must never be discarded.
    - HTML comments: closed comments are scaffold. An unclosed comment is
      left in place and counts as authored.

    Malformed input always errs toward SHOWING safety content, never toward
    hiding it.
    """
    remainder = text
    frontmatter = _FRONTMATTER_BLOCK_RE.match(text)
    if frontmatter:
        for line in frontmatter.group("body").splitlines():
            stripped_line = line.strip()
            if not stripped_line:
                continue
            if not _FRONTMATTER_SCAFFOLD_KEY_RE.match(stripped_line):
                return True
        remainder = text[frontmatter.end():]

    remainder = _HTML_COMMENT_RE.sub("", remainder)

    first_h1 = _H1_HEADING_RE.search(remainder)
    if first_h1 is not None and first_h1.group("title") == scaffold_title:
        remainder = remainder[: first_h1.start()] + remainder[first_h1.end():]

    return bool(remainder.strip())


def build_identity_payload(
    memory_dir: Path,
    *,
    include: tuple[str, ...] | None = None,
) -> dict[str, str]:
    """Read identity files from ``memory_dir`` and return them as a dict.

    Parameters
    ----------
    memory_dir:
        Directory containing the identity markdown files (one per name in
        ``include``). Typically ``vault/memory/``.
    include:
        Optional tuple of uppercase identity names to read. Defaults to
        ``DEFAULT_INCLUDE`` (``SOUL``/``SELF``/``USER``/``MEMORY``/``GOALS``/
        ``WORKING``/``SAFETY``). Pass an explicit tuple to scope the read to
        a subset. ``SAFETY`` is additionally stub-gated: an unedited seeded
        stub (frontmatter + H1 + seed comment only) yields no key.

    Returns
    -------
    dict[str, str]
        Mapping from uppercase name (no ``.md`` suffix) to raw file content.
        Missing files are ABSENT from the dict (no exception, no empty
        string). Empty ``memory_dir`` returns ``{}``.

    Notes
    -----
    The shim does NOT assemble headers, does NOT concatenate, does NOT
    truncate. Each downstream consumer (engine, reflect, weekly, dream)
    builds its own assembled prompt in its own existing order with its
    existing headers. Errors NEVER escape (fail-open like
    ``runtime.bootstrap.read_file_safe``).
    """
    # Lazy import keeps module load cheap and keeps the runtime layer
    # decoupled from cognition import order. The sys.path injection at module
    # top guarantees the import resolves regardless of caller cwd.
    from runtime.bootstrap import read_file_safe

    # Rule 1 resolution: include is resolved here, not bound at def time.
    names = include if include is not None else DEFAULT_INCLUDE

    payload: dict[str, str] = {}
    for name in names:
        content = read_file_safe(memory_dir / f"{name}.md")
        if not content:
            continue
        # #484: an unedited SAFETY.md seed stub yields NO key (not an empty
        # string) so downstream prompts stay byte-identical for unconfigured
        # personas. Scoped to SAFETY only — other identity files keep their
        # existing include-if-non-empty behavior verbatim. The scaffold H1
        # title is the file's own stem (the seeder writes ``# SAFETY``).
        if name == "SAFETY" and not has_authored_content(content, scaffold_title=name):
            continue
        payload[name] = content
    return payload


__all__ = ("build_identity_payload",)
