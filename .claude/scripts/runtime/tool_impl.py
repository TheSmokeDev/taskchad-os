"""Register the framework's real capabilities as callable tools.

The gap this closes: epic #236 built a registry, toolsets, two execution lanes,
per-persona scoping, a kill switch, and an audit trail — and registered ZERO
tools. Every piece of plumbing worked and nothing flowed through it. A persona
granted `crypto` resolved to an empty array and behaved exactly as it did
before the epic, which is a very expensive way to change nothing.

Design rules for anything added here:

* **Wrap what already exists.** Every tool below delegates to a framework
  capability that already ships (`memory_search`, `recall_service`, the vault
  on disk). A tool that needs new business logic belongs in the slice that owns
  that logic, registered from there — not reimplemented in this file.
* **Return TEXT the model can read.** Handlers return prose or compact JSON,
  never Python objects. The result is going into a conversation.
* **Safe-core tools are read-only.** Nothing registered here mutates. Broad
  file reads and write/exec verbs live in ``tool_impl_exec`` and belong to the
  explicit ``operator_exec`` capability class; legacy ``core`` still composes
  both classes for backward compatibility.
* **Registration is idempotent and import-time.** `register_tools()` is called
  once per process; re-registering the same tool under the same toolset is a
  legal reload.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_MAX_RESULT_CHARS = 4000
_MAX_FILE_CHARS = 20_000


def _truncate(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    """Bound every tool result.

    An unbounded tool result is a context-window denial of service: one search
    over a large vault can evict the conversation that asked for it.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[TRUNCATED — {len(text) - limit} more chars]"


# ---------------------------------------------------------------------------
# Memory — the framework's actual differentiator
# ---------------------------------------------------------------------------


def _persona_memory_dir(persona_id: str | None) -> Path | None:
    if persona_id is None:
        return None

    from personas import get_persona_paths

    return get_persona_paths(persona_id)["memory"]


def _memory_search(
    query: str = "",
    mode: str = "hybrid",
    limit: int = 5,
    *,
    _persona_id: str | None = None,
    **_: Any,
) -> str:
    """Hybrid/keyword/semantic search across the operator's vault."""
    if not query.strip():
        return "error: query is required"
    import memory_search as _ms

    results = _ms.search(
        query,
        mode=mode,
        limit=max(1, min(20, int(limit or 5))),
        memory_dir=_persona_memory_dir(_persona_id),
    )
    if not results:
        return f"No vault results for {query!r}."
    lines = [f"{len(results)} result(s) for {query!r}:"]
    for r in results:
        header = f"\n— {r.path}:{r.start_line}"
        if r.section_title:
            header += f" ({r.section_title})"
        lines.append(f"{header}\n{r.text.strip()}")
    return _truncate("\n".join(lines))


def _read_file(path: str = "", **_: Any) -> str:
    """Read a UTF-8 text file.

    Confined to the repo and the operator vault. Without confinement a persona
    granted ``operator_exec`` (or legacy ``core``) could read ``.env`` and hand
    credentials to a model — the registry decides WHICH tools a persona gets,
    never which paths a tool may touch, so the boundary has to live in the tool.
    """
    if not path.strip():
        return "error: path is required"

    target = Path(path).expanduser()
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return f"error: cannot resolve {path!r}: {exc}"

    roots = [Path(__file__).resolve().parents[3], (Path.home() / ".homie").resolve()]
    if not any(_is_within(resolved, root) for root in roots):
        return f"error: {path!r} is outside the readable roots (repo, ~/.homie)"
    if resolved.name.startswith(".env") or resolved.suffix in {".pem", ".key"}:
        return f"error: {resolved.name!r} is a credential file and is never readable by a tool"

    try:
        return _truncate(resolved.read_text(encoding="utf-8", errors="replace"), _MAX_FILE_CHARS)
    except OSError as exc:
        return f"error: {exc}"


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _search_files(
    pattern: str = "",
    path_prefix: str = "",
    limit: int = 10,
    *,
    _persona_id: str | None = None,
    **_: Any,
) -> str:
    """Find vault content by pattern — a thin alias over keyword search.

    Deliberately NOT a filesystem grep: the vault index already answers this
    faster and with section context, and a raw grep would reach files the
    read-confinement above exists to exclude.
    """
    if not pattern.strip():
        return "error: pattern is required"
    import memory_search as _ms

    results = _ms.search_keyword(
        pattern,
        limit=max(1, min(25, int(limit or 10))),
        path_prefix=path_prefix,
        memory_dir=_persona_memory_dir(_persona_id),
    )
    if not results:
        return f"No matches for {pattern!r}."
    return _truncate(
        "\n".join(f"{r.path}:{r.start_line}  {r.text.strip()[:160]}" for r in results)
    )


# ---------------------------------------------------------------------------
# Skills — closes the build_skill_index gap (#243, partial)
# ---------------------------------------------------------------------------


def _skills_list(**_: Any) -> str:
    """Names + one-liners for available skills."""
    root = Path(__file__).resolve().parents[2] / "skills"
    if not root.is_dir():
        return "No skills directory found."
    rows = []
    for skill_md in sorted(root.glob("*/SKILL.md")):
        name = skill_md.parent.name
        summary = ""
        for line in skill_md.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("description:"):
                summary = line.split(":", 1)[1].strip()
                break
        rows.append(f"{name} — {summary[:160]}" if summary else name)
    return _truncate("\n".join(rows) or "No skills found.")


def _skill_view(name: str = "", **_: Any) -> str:
    """Read a skill's body.

    THE gap this epic identified: `build_skill_index` emits names and
    one-liners only, so a persona could see a skill existed and never read it.
    """
    if not name.strip():
        return "error: name is required"
    skill_md = Path(__file__).resolve().parents[2] / "skills" / name.strip() / "SKILL.md"
    if not skill_md.is_file():
        return f"error: no skill named {name!r} (use skills_list)"
    return _truncate(skill_md.read_text(encoding="utf-8", errors="replace"), _MAX_FILE_CHARS)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_SPECS: tuple[tuple[str, str, str, dict[str, Any], Any], ...] = (
    (
        "memory_search",
        "safe_core",
        "Search the operator's vault (notes, decisions, episodes, concepts). Use this "
        "before answering anything that depends on prior context — it is the memory.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for."},
                "mode": {"type": "string", "enum": ["hybrid", "keyword", "semantic"]},
                "limit": {"type": "integer", "description": "Max results (1-20)."},
            },
            "required": ["query"],
        },
        _memory_search,
    ),
    (
        "read_file",
        "operator_exec",
        "Read a UTF-8 text file from the repo or the operator profile directory.",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute or relative path."}},
            "required": ["path"],
        },
        _read_file,
    ),
    (
        "search_files",
        "safe_core",
        "Find vault content matching a keyword pattern, with file/line locations.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path_prefix": {"type": "string", "description": "Scope to a subdirectory."},
                "limit": {"type": "integer"},
            },
            "required": ["pattern"],
        },
        _search_files,
    ),
    (
        "skills_list",
        "safe_core",
        "List available skills by name with a one-line description each.",
        {"type": "object", "properties": {}},
        _skills_list,
    ),
    (
        "skill_view",
        "safe_core",
        "Read the full body of one skill by name. Use after skills_list.",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        _skill_view,
    ),
)


def register_tools() -> int:
    """Register every implemented tool. Returns the count. Never raises.

    Fail-open per tool: one bad registration must not deny a persona the rest of
    its toolset. A tool that fails to register simply is not there, which the
    registry already treats as "optional plugin not loaded".
    """
    from runtime import tool_registry

    registered = 0
    try:
        # The authorization bridge is a safe-core meta-tool.  It can create a
        # pending request but cannot grant or execute anything itself.
        from runtime import persona_elevation

        registered += persona_elevation.register_tools()
    except Exception:  # noqa: BLE001
        _logger.warning("persona-elevation registration failed", exc_info=True)

    try:
        # The eyes live in their own module (X + browser reads reach outside the
        # process and carry ban-safety obligations the vault tools do not).
        from runtime import tool_impl_eyes

        registered += tool_impl_eyes.register_tools()
    except Exception:  # noqa: BLE001 — one dead group must not deny the rest
        _logger.warning("eye-tool registration failed", exc_info=True)

    try:
        # SEO/GEO owns its direct, read-only GSC/GA4/Firecrawl/OpenSEO wrappers.
        # Keeping them out of the generic read set avoids granting research
        # credentials to unrelated personas.
        from runtime import tool_impl_seo_geo

        registered += tool_impl_seo_geo.register_tools()
    except Exception:  # noqa: BLE001
        _logger.warning("SEO/GEO tool registration failed", exc_info=True)

    try:
        from runtime import tool_impl_crypto

        registered += tool_impl_crypto.register_tools()
    except Exception:  # noqa: BLE001
        _logger.warning("crypto-tool registration failed", exc_info=True)

    try:
        # The rest of the desk: indicators, levels, funding, sizing,
        # liquidation, the play ledger, the safety veto. Separate module
        # because these wrap the ANALYSIS slice, which has its own two-shape
        # availability contract to pass through.
        from runtime import tool_impl_crypto_desk

        registered += tool_impl_crypto_desk.register_tools()
    except Exception:  # noqa: BLE001
        _logger.warning("crypto-desk-tool registration failed", exc_info=True)

    # Optional private extension: the sanitizer intentionally omits this module.
    if importlib.util.find_spec("runtime.tool_impl_crypto_round") is not None:
        try:
            # Profile-private research artifacts and public prediction books only.
            # The live order module remains a separate, operator-gated boundary.
            from runtime import tool_impl_crypto_round

            registered += tool_impl_crypto_round.register_tools()
        except Exception:  # noqa: BLE001
            _logger.warning("crypto-round-tool registration failed", exc_info=True)

    try:
        # The order path — mandate, preflight, bracket submission. Its own
        # module because it is the only crypto surface that can move money, and
        # a reader looking for "what can this persona actually execute" should
        # find one file rather than a branch inside the read tools.
        from runtime import tool_impl_crypto_trade

        registered += tool_impl_crypto_trade.register_tools()
    except Exception:  # noqa: BLE001
        _logger.warning("crypto-trade-tool registration failed", exc_info=True)

    try:
        # The hands. Operator-granted 2026-07-27 ("give him full shell a hundred
        # percent") — the exec/write verbs `_HOMIE_CORE_TOOLS` has always
        # declared. Own module because they carry confinement, a denylist, and a
        # tree-kill that the read tools have no business inheriting.
        from runtime import tool_impl_exec

        registered += tool_impl_exec.register_tools()
    except Exception:  # noqa: BLE001
        _logger.warning("exec-tool registration failed", exc_info=True)

    for name, toolset, description, parameters, handler in _SPECS:
        try:
            tool_registry.register_tool(
                name,
                description,
                toolset=toolset,
                parameters=parameters,
                handler=handler,
                effect="read",
                persona_scoped=name in {"memory_search", "search_files"},
                elevatable=True,
            )
            registered += 1
        except Exception:  # noqa: BLE001
            _logger.warning("failed to register tool %r", name, exc_info=True)
    return registered


def describe_registered() -> str:
    """Diagnostics: what is actually callable right now."""
    from runtime import tool_registry

    return json.dumps(
        {
            e.name: {"toolset": e.toolset, "effect": e.effect}
            for e in tool_registry.list_registered()
        },
        indent=2,
        sort_keys=True,
    )


__all__ = ["describe_registered", "register_tools"]
