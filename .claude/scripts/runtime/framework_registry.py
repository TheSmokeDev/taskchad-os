"""Runtime-neutral registry for framework skills and MCP server config."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|authorization|auth|bearer|credential|client[_-]?secret)",
    re.IGNORECASE,
)
ENV_PLACEHOLDER_RE = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?$")
MAX_DESCRIPTION_CHARS = 140


@dataclass(frozen=True)
class SkillEntry:
    """One discovered framework skill."""

    name: str
    description: str
    path: str


@dataclass(frozen=True)
class McpServerEntry:
    """One redacted MCP server entry."""

    name: str
    transport: str
    config: dict[str, Any]
    source: str
    configured: bool | None = None
    callable: bool | None = None


@dataclass(frozen=True)
class FrameworkRegistry:
    """Discovered framework tools available to generic runtimes."""

    project_root: Path
    skills: tuple[SkillEntry, ...]
    mcp_servers: tuple[McpServerEntry, ...]
    mcp_config_path: Path | None = None


def discover_framework_registry(
    project_root: Path | str | None = None,
    *,
    mcp_config_path: Path | str | None = None,
) -> FrameworkRegistry:
    """Discover skills and MCP config without loading Claude-specific docs.

    Filesystem/config discovery is the BASELINE and always wins. Capability
    plugins may add process-local rows on top of it (issue #531); a plugin row
    whose name collides with a baseline row is dropped here rather than merged,
    so a plugin can never redefine a skill or MCP server that physically exists
    on disk. Registration refuses that collision up front — this merge-side
    check is the second line, for a plugin row that was legal at load time and
    then had a file appear underneath it.
    """

    root = resolve_project_root(project_root)
    config_path = resolve_mcp_config_path(root, explicit=mcp_config_path)
    skills = list(discover_skills(root))
    mcp_servers = list(discover_mcp_servers(root, config_path=config_path))

    baseline_skill_names = {entry.name for entry in skills}
    skills.extend(
        entry
        for entry, _owner in _plugin_skill_rows()
        if entry.name not in baseline_skill_names
    )
    baseline_mcp_names = {entry.name for entry in mcp_servers}
    mcp_servers.extend(
        entry
        for entry, _owner in _plugin_mcp_rows()
        if entry.name not in baseline_mcp_names
    )

    return FrameworkRegistry(
        project_root=root,
        skills=tuple(skills),
        mcp_servers=tuple(mcp_servers),
        mcp_config_path=config_path,
    )


# ---------------------------------------------------------------------------
# Capability-plugin discovery overlay (issue #531)
# ---------------------------------------------------------------------------


class FrameworkOverlayError(ValueError):
    """Raised on a plugin skill/MCP row that would shadow or corrupt discovery."""


_OVERLAY_LOCK = threading.RLock()
# name -> (entry, plugin_id, plugin_version)
_PLUGIN_SKILLS: dict[str, tuple[SkillEntry, str, str]] = {}
_PLUGIN_MCP_SERVERS: dict[str, tuple[McpServerEntry, str, str]] = {}
_OVERLAY_GENERATION: int = 0


def get_overlay_generation() -> int:
    """Current plugin-overlay generation. Bumps on every overlay mutation."""
    return _OVERLAY_GENERATION


def _plugin_skill_rows() -> tuple[tuple[SkillEntry, tuple[str, str]], ...]:
    with _OVERLAY_LOCK:
        rows = sorted(_PLUGIN_SKILLS.items())
    return tuple((entry, (owner, version)) for _name, (entry, owner, version) in rows)


def _plugin_mcp_rows() -> tuple[tuple[McpServerEntry, tuple[str, str]], ...]:
    with _OVERLAY_LOCK:
        rows = sorted(_PLUGIN_MCP_SERVERS.items())
    return tuple((entry, (owner, version)) for _name, (entry, owner, version) in rows)


def register_plugin_skill(
    entry: SkillEntry,
    *,
    plugin_id: str,
    plugin_version: str,
    project_root: Path | str | None = None,
) -> None:
    """Install one plugin-owned skill row into generic-lane discovery.

    Raises:
        FrameworkOverlayError: on a blank owner, on a name another plugin
            already owns, or on a name that physically exists on disk.
    """
    global _OVERLAY_GENERATION

    _require_overlay_owner(plugin_id, plugin_version)
    root = resolve_project_root(project_root)
    with _OVERLAY_LOCK:
        existing = _PLUGIN_SKILLS.get(entry.name)
        if existing is not None:
            raise FrameworkOverlayError(
                f"skill {entry.name!r} is already contributed by "
                f"{existing[1]}@{existing[2]}; refusing to shadow it for "
                f"{plugin_id!r}"
            )
        # Physical read, not a cached name set: the baseline is the filesystem.
        if any(found.name == entry.name for found in discover_skills(root)):
            raise FrameworkOverlayError(
                f"skill {entry.name!r} already exists on disk; a plugin may add "
                "a skill but may never redefine one"
            )
        _PLUGIN_SKILLS[entry.name] = (entry, plugin_id.strip(), plugin_version.strip())
        _OVERLAY_GENERATION += 1


def unregister_plugin_skill(
    name: str,
    *,
    plugin_id: str,
    plugin_version: str,
) -> bool:
    """Compare-and-remove one plugin-owned skill row. True if it was removed."""
    global _OVERLAY_GENERATION

    with _OVERLAY_LOCK:
        existing = _PLUGIN_SKILLS.get(name)
        if existing is None:
            return False
        if existing[1] != plugin_id or existing[2] != plugin_version:
            raise FrameworkOverlayError(
                f"skill {name!r} is owned by {existing[1]}@{existing[2]}; "
                f"refusing to unregister it for {plugin_id}@{plugin_version}"
            )
        del _PLUGIN_SKILLS[name]
        _OVERLAY_GENERATION += 1
        return True


def register_plugin_mcp_server(
    entry: McpServerEntry,
    *,
    plugin_id: str,
    plugin_version: str,
    project_root: Path | str | None = None,
    mcp_config_path: Path | str | None = None,
) -> None:
    """Install one plugin-owned MCP server row.

    The stored config is re-redacted here rather than trusted from the caller:
    the row is rendered into prompts and catalogs, and a plugin-supplied config
    is hostile input.
    """
    global _OVERLAY_GENERATION

    _require_overlay_owner(plugin_id, plugin_version)
    root = resolve_project_root(project_root)
    with _OVERLAY_LOCK:
        existing = _PLUGIN_MCP_SERVERS.get(entry.name)
        if existing is not None:
            raise FrameworkOverlayError(
                f"MCP server {entry.name!r} is already contributed by "
                f"{existing[1]}@{existing[2]}; refusing to shadow it for "
                f"{plugin_id!r}"
            )
        configured = discover_mcp_servers(root, config_path=mcp_config_path)
        if any(found.name == entry.name for found in configured):
            raise FrameworkOverlayError(
                f"MCP server {entry.name!r} is already configured on disk; a "
                "plugin may add a server but may never redefine one"
            )
        configured = entry.configured
        callable_state = entry.callable
        if configured is None or callable_state is None:
            derived_configured, derived_callable = _mcp_physical_state(
                dict(entry.config),
                entry.transport,
            )
            configured = derived_configured if configured is None else configured
            callable_state = derived_callable if callable_state is None else callable_state
        redacted = McpServerEntry(
            name=entry.name,
            transport=entry.transport,
            config=redact_mcp_config(dict(entry.config)),
            source=entry.source,
            configured=bool(configured),
            callable=bool(callable_state),
        )
        _PLUGIN_MCP_SERVERS[entry.name] = (
            redacted,
            plugin_id.strip(),
            plugin_version.strip(),
        )
        _OVERLAY_GENERATION += 1


def unregister_plugin_mcp_server(
    name: str,
    *,
    plugin_id: str,
    plugin_version: str,
) -> bool:
    """Compare-and-remove one plugin-owned MCP server row."""
    global _OVERLAY_GENERATION

    with _OVERLAY_LOCK:
        existing = _PLUGIN_MCP_SERVERS.get(name)
        if existing is None:
            return False
        if existing[1] != plugin_id or existing[2] != plugin_version:
            raise FrameworkOverlayError(
                f"MCP server {name!r} is owned by {existing[1]}@{existing[2]}; "
                f"refusing to unregister it for {plugin_id}@{plugin_version}"
            )
        del _PLUGIN_MCP_SERVERS[name]
        _OVERLAY_GENERATION += 1
        return True


def list_plugin_overlay_rows() -> tuple[tuple[str, str, str, str], ...]:
    """Every overlay row as ``(kind, name, plugin_id, plugin_version)``."""
    rows = [
        ("skill", entry.name, owner[0], owner[1])
        for entry, owner in _plugin_skill_rows()
    ]
    rows.extend(
        ("mcp_server", entry.name, owner[0], owner[1])
        for entry, owner in _plugin_mcp_rows()
    )
    return tuple(sorted(rows))


def _require_overlay_owner(plugin_id: str, plugin_version: str) -> None:
    if not plugin_id or not plugin_id.strip():
        raise FrameworkOverlayError("plugin overlay registration requires a plugin id")
    if not plugin_version or not plugin_version.strip():
        raise FrameworkOverlayError(
            "plugin overlay registration requires a plugin version"
        )


def resolve_project_root(start: Path | str | None = None) -> Path:
    """Resolve repo root from a cwd or file path."""

    explicit = start is not None
    candidate = Path(start or os.getcwd()).expanduser().resolve(strict=False)
    if candidate.is_file():
        candidate = candidate.parent

    if explicit:
        if candidate.name == "scripts" and candidate.parent.name == ".claude":
            return candidate.parent.parent
        if candidate.name == ".claude":
            return candidate.parent
        if candidate.parent.name == ".claude":
            return candidate.parent.parent
        if (candidate / ".claude").exists() or (candidate / ".git").exists():
            return candidate
        return candidate

    for path in (candidate, *candidate.parents):
        if (path / ".claude").is_dir():
            return path
        if path.name == "scripts" and path.parent.name == ".claude":
            return path.parent.parent

    return candidate


def discover_skills(project_root: Path | str, *, fenced: bool = True) -> list[SkillEntry]:
    """Discover `.claude/skills/**/SKILL.md` entries.

    ``fenced=True`` (every runtime caller): persona-scoped promoted skills are
    excluded — this discovery feeds generic-lane tool maps, and a skill scoped
    to one persona must never enter another's runtime context (#429 codex R5).
    ``fenced=False`` is for the operator's OWN management surfaces (the
    dashboard browse list), which must see the whole inventory to manage it.
    """

    root = Path(project_root)
    skills_root = root / ".claude" / "skills"
    if not skills_root.is_dir():
        return []

    # The framework registry feeds GENERIC runtime lanes — the whole-pool,
    # default-profile context. A persona-scoped promoted skill must never
    # enter it (#429 codex R5 BLOCKER): build_skill_index fences by reader,
    # but this discovery path used to return every promoted SKILL.md
    # regardless, and the tool map then handed any runtime a readable path to
    # a skill scoped to somebody else — invisible only while the rendering cap
    # happened to hide it. One bulk sidecar read; fail closed.
    scope_map, scope_readable = _framework_scope_map() if fenced else ({}, True)

    entries: list[SkillEntry] = []
    for skill_file in sorted(skills_root.rglob("SKILL.md")):
        # Default-deny: exclude auto-drafted skills under generated/ — unvetted
        # (no scan, no operator gate) skills must not enter the generic-lane tool map.
        relative_parts: tuple[str, ...] = ()
        try:
            relative_parts = skill_file.relative_to(skills_root).parts
            if "generated" in relative_parts:
                continue
        except ValueError:
            pass
        try:
            content = skill_file.read_text(encoding="utf-8")
        except OSError:
            continue
        metadata = _parse_skill_frontmatter(content)
        relative = skill_file.relative_to(root).as_posix()
        name = metadata.get("name") or skill_file.parent.name
        if "promoted" in relative_parts and not _framework_scope_allows(
            name, scope_map, scope_readable
        ):
            continue
        description = metadata.get("description") or _first_markdown_sentence(content)
        entries.append(
            SkillEntry(
                name=_compact(name),
                description=_truncate(_compact(description), MAX_DESCRIPTION_CHARS),
                path=relative,
            )
        )
    return entries


def _framework_scope_map() -> tuple[dict[str, frozenset[str]], bool]:
    """Bulk-read the persona-scope sidecar for the generic-lane fence.

    ``({}, False)`` when the scope machinery is unavailable — the caller then
    hides EVERY promoted skill from generic lanes (fail closed; hand-authored
    skills are unaffected, they never went through the promotion gate).
    """
    try:
        from cognition.skills import _load_persona_scope_map
    except Exception:
        return {}, False
    try:
        return _load_persona_scope_map()
    except Exception:
        return {}, False


def _framework_scope_allows(
    name: str, scope_map: dict[str, frozenset[str]], scope_readable: bool
) -> bool:
    """May a promoted skill enter the GENERIC (default-profile) tool map?"""
    if not scope_readable:
        return False
    assigned = scope_map.get(name)
    if assigned is None:
        return False  # promoted with no scope row = un-vouched-for
    if not assigned:
        return True  # legacy/global migration row
    try:
        from cognition import skill_usage

        sentinel = str(getattr(skill_usage, "SCOPE_UNRESTRICTED", "*") or "*")
    except Exception:
        sentinel = "*"
    try:
        from cognition import skills as _skills_mod

        unrestricted = str(getattr(_skills_mod, "_UNRESTRICTED_PROFILE", "default"))
    except Exception:
        unrestricted = "default"
    return bool(assigned & {unrestricted, sentinel})


def resolve_mcp_config_path(
    project_root: Path | str,
    *,
    explicit: Path | str | None = None,
) -> Path | None:
    """Return the first approved MCP config path that exists."""

    root = Path(project_root)
    env_path = os.getenv("MCP_CONFIG_PATH", "").strip()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(
        [
            root / ".claude" / "skills" / "mcp-client" / "references" / "mcp-config.json",
            root / ".mcp.json",
            root / ".claude" / "mcp-global-backup.json",
        ]
    )

    for candidate in candidates:
        path = candidate.expanduser()
        if not path.is_absolute():
            path = root / path
        path = path.resolve(strict=False)
        if path.is_file():
            return path
    return None


def discover_mcp_servers(
    project_root: Path | str,
    *,
    config_path: Path | str | None = None,
) -> list[McpServerEntry]:
    """Discover MCP servers from the approved project config."""

    root = Path(project_root)
    path = Path(config_path) if config_path else resolve_mcp_config_path(root)
    if path is None or not path.is_file():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return []

    entries: list[McpServerEntry] = []
    source = _relative_or_absolute(path, root)
    for name, raw_config in sorted(servers.items()):
        if not isinstance(name, str) or not isinstance(raw_config, dict):
            continue
        if _is_zapier_server(name, raw_config):
            continue
        redacted = redact_mcp_config(raw_config)
        transport = _transport_for_config(raw_config)
        configured, callable_state = _mcp_physical_state(raw_config, transport)
        entries.append(
            McpServerEntry(
                name=name,
                transport=transport,
                config=redacted,
                source=source,
                configured=configured,
                callable=callable_state,
            )
        )
    return entries


def _mcp_physical_state(
    config: dict[str, Any],
    transport: str,
) -> tuple[bool, bool]:
    normalized_transport = str(transport or "").strip().casefold().replace("-", "_")
    env = config.get("env")
    env_ready = True
    if isinstance(env, dict):
        for raw_value in env.values():
            value = str(raw_value or "").strip()
            placeholder = ENV_PLACEHOLDER_RE.fullmatch(value)
            if not value or (placeholder and not os.environ.get(placeholder.group(1))):
                env_ready = False
                break
    if normalized_transport == "stdio":
        command = str(config.get("command") or "").strip()
        configured = bool(command and env_ready)
        command_path = Path(command).expanduser() if command else None
        callable_state = bool(
            configured
            and (
                shutil.which(command) is not None
                or (command_path is not None and command_path.is_file())
            )
        )
        return configured, callable_state
    if normalized_transport in {"http", "sse", "streamable_http"}:
        url = str(config.get("url") or "").strip()
        configured = bool(_valid_http_url(url) and env_ready)
        return configured, configured
    return False, False


def _valid_http_url(value: str) -> bool:
    """Require a concrete HTTP(S) authority without resolving the network."""
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme.casefold() in {"http", "https"}
        and parsed.netloc
        and parsed.hostname
        and not any(character.isspace() for character in value)
    )


def redact_mcp_config(config: dict[str, Any]) -> dict[str, Any]:
    """Redact secrets and env values before prompt injection."""

    redacted: dict[str, Any] = {}
    for key, value in config.items():
        if _is_secret_key(key):
            redacted[key] = _redact_scalar(value)
            continue
        if key == "url" and isinstance(value, str):
            redacted[key] = _redact_url(value)
            continue
        if key == "env" and isinstance(value, dict):
            redacted[key] = {
                str(env_key): _env_placeholder(str(env_key))
                for env_key in sorted(value)
            }
            continue
        redacted[key] = _redact_value(value)
    return redacted


def render_framework_tool_map(
    project_root: Path | str | None = None,
    *,
    max_skills: int = 24,
    max_mcp_servers: int = 12,
) -> str:
    """Render a compact prompt-safe framework map for generic tool runtimes."""

    registry = discover_framework_registry(project_root)
    lines: list[str] = [
        "Framework tool map (v2 runtime-native):",
        "Prefer direct integrations and repo-local scripts first. Use MCP only as an optional fallback through the mcp-client skill.",
    ]

    if registry.skills:
        lines.append(f"Skills ({len(registry.skills)} discovered; showing {min(max_skills, len(registry.skills))}):")
        for skill in registry.skills[:max_skills]:
            detail = f" - {skill.name}: {skill.description}"
            if skill.path:
                detail += f" [{skill.path}]"
            lines.append(detail)
        if len(registry.skills) > max_skills:
            lines.append(f" - ... {len(registry.skills) - max_skills} more skills")

    if registry.mcp_servers:
        lines.append(
            f"MCP servers ({len(registry.mcp_servers)} discovered; values redacted):"
        )
        for server in registry.mcp_servers[:max_mcp_servers]:
            summary = _summarize_mcp_config(server.config)
            lines.append(f" - {server.name}: {server.transport}; {summary}")
        if len(registry.mcp_servers) > max_mcp_servers:
            lines.append(f" - ... {len(registry.mcp_servers) - max_mcp_servers} more MCP servers")
        lines.append(
            "MCP client entrypoint: python .claude/skills/mcp-client/scripts/mcp_client.py servers|tools <server>|call <server> <tool> '<json>'"
        )

    if len(lines) == 2:
        return ""
    return "\n".join(lines)


def _parse_skill_frontmatter(content: str) -> dict[str, str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    metadata: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip().lower()] = value.strip().strip("'\"")
    return metadata


def _first_markdown_sentence(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("---") or stripped.startswith("#"):
            continue
        return stripped
    return ""


def _transport_for_config(config: dict[str, Any]) -> str:
    if "command" in config:
        return "stdio"
    url = str(config.get("url", ""))
    if url.endswith("/sse"):
        return "sse"
    if url.endswith("/mcp"):
        return "streamable-http"
    if url:
        return "http"
    return "unknown"


def _summarize_mcp_config(config: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("command", "args", "url", "env"):
        if key not in config:
            continue
        value = config[key]
        if isinstance(value, list):
            parts.append(f"{key}=[{', '.join(str(item) for item in value[:4])}]")
        elif isinstance(value, dict):
            parts.append(f"{key}=[{', '.join(value.keys())}]")
        else:
            parts.append(f"{key}={value}")
    return "; ".join(parts) if parts else "configured"


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return redact_mcp_config(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_scalar(value)
    return value


def _redact_scalar(value: Any) -> str:
    raw = str(value)
    match = ENV_PLACEHOLDER_RE.match(raw.strip())
    if match:
        return _env_placeholder(match.group(1))
    if raw and _looks_secretish(raw):
        return "<redacted>"
    return raw


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        query.append((key, "<redacted>" if _is_secret_key(key) else value))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query, safe="<>"), parts.fragment)
    )


def _env_placeholder(name: str) -> str:
    return f"<env:{name}>"


def _is_secret_key(key: str) -> bool:
    return bool(SECRET_KEY_RE.search(key))


def _looks_secretish(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    if stripped.startswith(("<env:", "$")):
        return False
    return len(stripped) >= 32 and not stripped.startswith(("http://", "https://"))


def _is_zapier_server(name: str, config: dict[str, Any]) -> bool:
    haystack = json.dumps({"name": name, "config": config}, default=str).lower()
    return "zapier" in haystack


def _relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path.resolve(strict=False))


def _compact(value: str) -> str:
    return " ".join(str(value).split())


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."
