"""Framework-native team memory model.

Private memory lives at: vault/memory/agents/{agent_id}/
Team memory lives at:    vault/memory/teams/{team_name}/

Both use the canonical vault as storage — no vendor sync, no new daemon.
Secret guardrails: team writes are scanned before disk; credential patterns
are refused. Private (agent) writes bypass the secret scan since they are
not shared across the team.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from orchestration.observability import orchestration_span, update_observation

# Canonical vault root — overridable via env for tests and alt installs
_DEFAULT_VAULT_ROOT = Path(
    os.getenv("VAULT_ROOT")
    or str(Path(__file__).resolve().parents[3] / "TheHomie" / "Memory")
)


def _vault_root() -> Path:
    """Resolve the vault root at call time so tests can override VAULT_ROOT."""
    env = os.getenv("VAULT_ROOT")
    if env:
        return Path(env)
    return _DEFAULT_VAULT_ROOT


# ── Secret patterns ───────────────────────────────────────────────────────
# Applied before any team memory write. Each pattern matches a credential
# shape we never want to land in shared notes.

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(api[_-]?key|secret[_-]?key|password|passwd|token|bearer)\s*[:=]\s*\S{8,}"),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),       # OpenAI-style keys
    re.compile(r"pk-lf-[a-zA-Z0-9_-]{10,}"),  # Langfuse public key
    re.compile(r"sk-lf-[a-zA-Z0-9_-]{10,}"),  # Langfuse secret key
    re.compile(r"pcp_[a-zA-Z0-9]{20,}"),      # Paperclip API key
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),      # GitHub personal access token
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),  # JWT
]


def scan_for_secrets(content: str) -> list[str]:
    """Return descriptions of any credential patterns found in content.

    If the returned list is non-empty, the caller MUST refuse the write.
    """
    found: list[str] = []
    for pat in _SECRET_PATTERNS:
        if pat.search(content):
            found.append(pat.pattern)
    return found


# ── Path resolution ───────────────────────────────────────────────────────


def _sanitize_component(name: str) -> str:
    """Map an arbitrary name to a safe single directory component."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", name).strip("_")
    if not cleaned:
        raise ValueError(f"Name is empty after sanitization: {name!r}")
    return cleaned


def get_team_memory_path(team_id: int) -> Path:
    """Return the team memory directory for a given team session.

    Keyed by numeric team_id (e.g. teams/team-42/) so two team sessions
    that happen to share the same name cannot read each other's memory.
    """
    return _vault_root() / "teams" / f"team-{team_id}"


def get_agent_memory_path(agent_id: str) -> Path:
    """Return the private memory directory for a specific agent."""
    return _vault_root() / "agents" / _sanitize_component(agent_id)


def _validate_filename(filename: str) -> None:
    """Reject path-traversal and nested paths in memory filenames."""
    if not filename:
        raise ValueError("filename is required")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError(f"Invalid memory filename: {filename!r}")
    if filename.startswith("."):
        raise ValueError(f"Hidden filenames are not allowed: {filename!r}")


# ── Team memory (shared) ──────────────────────────────────────────────────


def write_team_memory(
    team_id: int,
    filename: str,
    content: str,
    *,
    overwrite: bool = False,
) -> Path:
    """Write a file to team shared memory.

    Keyed by team_id so distinct sessions with the same name stay isolated.
    Raises ValueError if content matches any secret pattern or filename is
    unsafe. Raises FileExistsError if the file exists and overwrite=False.
    """
    with orchestration_span(
        "team_memory_write",
        metadata={
            "team_id": team_id,
            "memory_scope": "team",
            "memory_filename": filename,
            "overwrite": overwrite,
        },
        trace_metadata={"feature_phase": 7, "team_id": team_id},
        expected_exceptions=(ValueError, FileExistsError),
    ):
        _validate_filename(filename)
        secrets = scan_for_secrets(content)
        if secrets:
            update_observation(
                metadata={
                    "team_id": team_id,
                    "memory_scope": "team",
                    "memory_filename": filename,
                    "memory_write_allowed": False,
                    "error_type": "secret_guardrail",
                    "secret_pattern_count": len(secrets),
                },
                level="WARNING",
                status_message="team memory write refused by secret guardrail",
            )
            raise ValueError(
                "Refused to write team memory: potential secrets detected "
                f"(patterns: {secrets})"
            )

        path = get_team_memory_path(team_id)
        path.mkdir(parents=True, exist_ok=True)

        target = path / filename
        if target.exists() and not overwrite:
            update_observation(
                metadata={
                    "team_id": team_id,
                    "memory_scope": "team",
                    "memory_filename": filename,
                    "memory_write_allowed": False,
                    "error_type": "file_exists",
                },
                level="WARNING",
                status_message="team memory write refused because file exists",
            )
            raise FileExistsError(f"Team memory file already exists: {target}")

        target.write_text(content, encoding="utf-8")
        update_observation(
            metadata={
                "team_id": team_id,
                "memory_scope": "team",
                "memory_filename": filename,
                "memory_write_allowed": True,
            },
            output={"path": str(target)},
        )
        return target


def read_team_memory(team_id: int, filename: str) -> str:
    """Read a file from team shared memory."""
    with orchestration_span(
        "team_memory_read",
        metadata={"team_id": team_id, "memory_scope": "team", "memory_filename": filename},
        trace_metadata={"feature_phase": 7, "team_id": team_id},
        expected_exceptions=(ValueError, FileNotFoundError),
    ):
        _validate_filename(filename)
        path = get_team_memory_path(team_id) / filename
        if not path.exists():
            update_observation(
                metadata={"team_id": team_id, "memory_filename": filename, "error_type": "not_found"},
                level="WARNING",
                status_message="team memory file not found",
            )
            raise FileNotFoundError(f"Team memory file not found: {path}")
        content = path.read_text(encoding="utf-8")
        update_observation(metadata={"team_id": team_id, "memory_filename": filename})
        return content


def list_team_memory(team_id: int) -> list[str]:
    """List all files in a team's shared memory directory."""
    with orchestration_span(
        "team_memory_list",
        metadata={"team_id": team_id, "memory_scope": "team"},
        trace_metadata={"feature_phase": 7, "team_id": team_id},
    ):
        path = get_team_memory_path(team_id)
        if not path.exists():
            update_observation(metadata={"team_id": team_id, "memory_file_count": 0})
            return []
        files = sorted(f.name for f in path.iterdir() if f.is_file())
        update_observation(metadata={"team_id": team_id, "memory_file_count": len(files)})
        return files


def delete_team_memory(team_id: int, filename: str) -> bool:
    """Delete a file from team shared memory. Returns True if removed."""
    with orchestration_span(
        "team_memory_delete",
        metadata={"team_id": team_id, "memory_scope": "team", "memory_filename": filename},
        trace_metadata={"feature_phase": 7, "team_id": team_id},
        expected_exceptions=(ValueError,),
    ):
        _validate_filename(filename)
        path = get_team_memory_path(team_id) / filename
        if not path.exists():
            update_observation(metadata={"team_id": team_id, "memory_filename": filename, "deleted": False})
            return False
        path.unlink()
        update_observation(metadata={"team_id": team_id, "memory_filename": filename, "deleted": True})
        return True


# ── Agent memory (private) ────────────────────────────────────────────────


def write_agent_memory(
    agent_id: str,
    filename: str,
    content: str,
    *,
    overwrite: bool = False,
) -> Path:
    """Write a file to an agent's private memory.

    Private memory is not scanned for secrets (not shared), but filenames
    are still validated against path traversal.
    """
    _validate_filename(filename)
    path = get_agent_memory_path(agent_id)
    path.mkdir(parents=True, exist_ok=True)

    target = path / filename
    if target.exists() and not overwrite:
        raise FileExistsError(f"Agent memory file already exists: {target}")

    target.write_text(content, encoding="utf-8")
    return target


def read_agent_memory(agent_id: str, filename: str) -> str:
    """Read a file from an agent's private memory."""
    _validate_filename(filename)
    path = get_agent_memory_path(agent_id) / filename
    if not path.exists():
        raise FileNotFoundError(f"Agent memory file not found: {path}")
    return path.read_text(encoding="utf-8")


def list_agent_memory(agent_id: str) -> list[str]:
    """List all files in an agent's private memory directory."""
    path = get_agent_memory_path(agent_id)
    if not path.exists():
        return []
    return sorted(f.name for f in path.iterdir() if f.is_file())
