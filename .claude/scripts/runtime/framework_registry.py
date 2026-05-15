"""Runtime-neutral registry for framework skills and MCP server config."""

from __future__ import annotations

import json
import os
import re
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
    """Discover skills and MCP config without loading Claude-specific docs."""

    root = resolve_project_root(project_root)
    config_path = resolve_mcp_config_path(root, explicit=mcp_config_path)
    return FrameworkRegistry(
        project_root=root,
        skills=tuple(discover_skills(root)),
        mcp_servers=tuple(discover_mcp_servers(root, config_path=config_path)),
        mcp_config_path=config_path,
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


def discover_skills(project_root: Path | str) -> list[SkillEntry]:
    """Discover `.claude/skills/**/SKILL.md` entries."""

    root = Path(project_root)
    skills_root = root / ".claude" / "skills"
    if not skills_root.is_dir():
        return []

    entries: list[SkillEntry] = []
    for skill_file in sorted(skills_root.rglob("SKILL.md")):
        try:
            content = skill_file.read_text(encoding="utf-8")
        except OSError:
            continue
        metadata = _parse_skill_frontmatter(content)
        relative = skill_file.relative_to(root).as_posix()
        name = metadata.get("name") or skill_file.parent.name
        description = metadata.get("description") or _first_markdown_sentence(content)
        entries.append(
            SkillEntry(
                name=_compact(name),
                description=_truncate(_compact(description), MAX_DESCRIPTION_CHARS),
                path=relative,
            )
        )
    return entries


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
        entries.append(
            McpServerEntry(
                name=name,
                transport=_transport_for_config(raw_config),
                config=redacted,
                source=source,
            )
        )
    return entries


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
