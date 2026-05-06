"""Identity payload shim — single source for reading identity files into a dict.

This module is the canonical entry point used by the chat engine and the cron
memory pipelines (reflect / weekly / dream) for assembling the identity-file
payload (SOUL, SELF, USER, MEMORY, GOALS, WORKING). Each consumer keeps its own
prompt assembly + ordering + headers; the shim only hands back raw file
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
)


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
        ``WORKING``). Pass an explicit tuple to scope the read to a subset.

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
        if content:
            payload[name] = content
    return payload


__all__ = ("build_identity_payload",)
