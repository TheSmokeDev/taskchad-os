"""Auth-profile helpers for provider-backed runtime adapters."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AuthProfileStatus:
    """Availability status for a resolved auth profile."""

    available: bool
    detail: str = ""


@dataclass(slots=True)
class CodexAuthProfile:
    """Subscription-backed auth profile for the local Codex CLI."""

    key: str
    command: str


@dataclass(slots=True)
class GeminiAuthProfile:
    """Subscription-backed auth profile for the local Gemini CLI."""

    key: str
    command: str
    auth_type: str


def resolve_command_path(command: str) -> str:
    """Resolve a command name into a concrete executable path when possible."""

    if Path(command).exists():
        return str(Path(command))

    resolved = shutil.which(command)
    return resolved or command


def resolve_codex_auth_profile() -> CodexAuthProfile:
    """Resolve the local Codex auth profile from environment."""

    return CodexAuthProfile(
        key=os.getenv("SECOND_BRAIN_CODEX_AUTH_PROFILE", "default").strip() or "default",
        command=resolve_command_path(
            os.getenv("SECOND_BRAIN_CODEX_COMMAND", "codex").strip() or "codex"
        ),
    )


def codex_cli_exists(command: str) -> bool:
    """Return True when the configured Codex CLI command is resolvable."""

    if Path(command).exists():
        return True
    return shutil.which(command) is not None


def _needs_shell(command: str) -> bool:
    """Return True when the command is a Windows .CMD/.BAT shim (npm installs)."""
    resolved = shutil.which(command) or command
    return resolved.lower().endswith((".cmd", ".bat"))


def codex_auth_status(profile: CodexAuthProfile | None = None) -> AuthProfileStatus:
    """Check whether local Codex subscription auth is ready to use."""

    profile = profile or resolve_codex_auth_profile()
    if not codex_cli_exists(profile.command):
        return AuthProfileStatus(False, f"Codex CLI not found: {profile.command}")

    try:
        # Windows npm shims (.CMD) need shell=True to execute
        use_shell = _needs_shell(profile.command)
        result = subprocess.run(
            [profile.command, "login", "status"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            shell=use_shell,
        )
    except Exception as exc:
        return AuthProfileStatus(False, str(exc))

    output = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
    )
    if result.returncode == 0 and "logged in" in output.lower():
        return AuthProfileStatus(True, output or "Logged in")
    if not output:
        output = f"codex login status exited with code {result.returncode}"
    return AuthProfileStatus(False, output)


def codex_auth_available(profile: CodexAuthProfile | None = None) -> bool:
    """Convenience predicate for subscription-backed Codex availability."""

    return codex_auth_status(profile).available


def resolve_gemini_auth_profile() -> GeminiAuthProfile:
    """Resolve the local Gemini auth profile from environment and settings."""

    settings = _read_json(Path.home() / ".gemini" / "settings.json")
    auth_type = (
        os.getenv("SECOND_BRAIN_GEMINI_AUTH_TYPE", "").strip()
        or str(settings.get("security", {}).get("auth", {}).get("selectedType", "")).strip()
        or "oauth-personal"
    )

    return GeminiAuthProfile(
        key=auth_type,
        command=resolve_command_path(
            os.getenv("SECOND_BRAIN_GEMINI_COMMAND", "gemini").strip() or "gemini"
        ),
        auth_type=auth_type,
    )


def gemini_cli_exists(command: str) -> bool:
    """Return True when the configured Gemini CLI command is resolvable."""

    if Path(command).exists():
        return True
    return shutil.which(command) is not None


def gemini_auth_status(profile: GeminiAuthProfile | None = None) -> AuthProfileStatus:
    """Check whether local Gemini auth is ready to use."""

    profile = profile or resolve_gemini_auth_profile()
    if not gemini_cli_exists(profile.command):
        return AuthProfileStatus(False, f"Gemini CLI not found: {profile.command}")

    gemini_home = Path.home() / ".gemini"
    settings_path = gemini_home / "settings.json"
    if not settings_path.exists():
        return AuthProfileStatus(False, f"Gemini settings not found: {settings_path}")

    if profile.auth_type.startswith("oauth"):
        oauth_creds = gemini_home / "oauth_creds.json"
        account_index = gemini_home / "google_accounts.json"
        if oauth_creds.exists() and account_index.exists():
            return AuthProfileStatus(True, f'Authenticated via "{profile.auth_type}"')
        return AuthProfileStatus(
            False,
            (
                f'Gemini auth type "{profile.auth_type}" is selected but OAuth credential files '
                "are missing"
            ),
        )

    if profile.auth_type in {"gemini-api-key", "api-key"}:
        if os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip():
            return AuthProfileStatus(True, f'Authenticated via "{profile.auth_type}"')
        return AuthProfileStatus(
            False,
            (
                f'Gemini auth type "{profile.auth_type}" is selected but GEMINI_API_KEY or '
                "GOOGLE_API_KEY is not configured"
            ),
        )

    if profile.auth_type == "vertex-ai":
        if os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() == "true":
            return AuthProfileStatus(True, 'Authenticated via "vertex-ai"')
        return AuthProfileStatus(
            False,
            (
                'Gemini auth type "vertex-ai" is selected but GOOGLE_GENAI_USE_VERTEXAI is '
                "not enabled"
            ),
        )

    return AuthProfileStatus(True, f'Gemini auth type "{profile.auth_type}" is configured')


def gemini_auth_available(profile: GeminiAuthProfile | None = None) -> bool:
    """Convenience predicate for Gemini CLI availability."""

    return gemini_auth_status(profile).available


def _read_json(path: Path) -> dict:
    """Read a small JSON config file, returning an empty dict on failure."""

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
