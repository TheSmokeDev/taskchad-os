"""Persona capability matrix for env delegation and skill allowlists.

The master secret source stays at ``.claude/scripts/.env``. This module reads a
local matrix of env-key groups and persona skill allowlists, then derives the
profile-owned env files used by named Homies.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from .core import get_persona_paths

_CLAUDE_DIR = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = _CLAUDE_DIR.parent
DEFAULT_MATRIX_PATH = _CLAUDE_DIR / "data" / "persona-capability-matrix.yaml"
MASTER_ENV_PATH = _CLAUDE_DIR / "scripts" / ".env"


class CapabilityMatrixError(ValueError):
    """Raised when the persona capability matrix is malformed."""


@dataclass(frozen=True)
class EnvSyncPlan:
    """Safe-to-render plan for deriving one profile's env file."""

    profile_name: str
    profile_env_path: Path
    allowed_keys: list[str]
    present_keys: list[str]
    missing_keys: list[str]
    values: dict[str, str]


_BASE_RUNTIME_ENV_KEYS: frozenset[str] = frozenset({
    "CLAUDE_CONFIG_DIR",
    "COMSPEC",
    "HOME",
    "HOMIE_PERSONA_CAPABILITY_MATRIX",
    "LOGNAME",
    "PATH",
    "PATHEXT",
    "PYTHONHOME",
    "PYTHONPATH",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USER",
    "USERNAME",
    "USERPROFILE",
    "VIRTUAL_ENV",
    "WINDIR",
})


_DEFAULT_MATRIX: dict[str, Any] = {
    "version": 1,
    "env_groups": {
        "runtime_core": [
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "GEMINI_API_KEY",
            "LANGFUSE_BASE_URL",
            "LANGFUSE_ENABLED",
            "LANGFUSE_PUBLIC_KEY",
            "LANGFUSE_SECRET_KEY",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "OWNER_NAME",
            "SECOND_BRAIN_CLAUDE_MODEL",
            "SECOND_BRAIN_GENERIC_PROVIDER",
            "SECOND_BRAIN_RUNTIME_LANE",
            "SECOND_BRAIN_RUNTIME_PROVIDER",
            "SENTRY_DSN",
            "SENTRY_ENVIRONMENT",
        ],
        "vault_memory": [
            "HOMIE_CODING_VAULT_DIR",
            "HOMIE_UNIFIED_VAULT_DIR",
            "HOMIE_VAULT_DIR",
        ],
        "discord_ingress": [
            "DISCORD_ALLOWED_GUILDS",
            "DISCORD_ALLOWED_USERS",
            "DISCORD_BOT_TOKEN",
            "DISCORD_CHANNEL_BINDINGS_FILE",
            "DISCORD_WATCH_ALL_GUILD_CHANNELS",
            "DISCORD_WATCH_EXCLUDE_CHANNELS",
            "DISCORD_WATCHED_CHANNELS",
        ],
        "browser_ops": [
            "AGENT_BROWSER_HOME",
            "HOMIE_BROWSER_CDP_PORT",
        ],
        "search_analytics": [
            "GA4_PROPERTY_ID",
            "GOOGLE_CLOUD_PROJECT",
            "GSC_SITE_URL",
        ],
        "business_profile": [
            "BBB_URL",
            "BUSINESS_ADDRESS",
            "BUSINESS_EMAIL",
            "BUSINESS_PHONE",
            "BUSINESS_PHONE_800",
            "FACEBOOK_URL",
            "INSTAGRAM_URL",
            "LINKEDIN_URL",
            "OWNER_NAME",
            "WALLETHUB_URL",
            "X_URL",
            "YELP_URL",
        ],
        "socials_write": [
            "CIRCLE_ADMIN_TOKEN",
            "CIRCLE_COMMUNITY_MEMBER_ID",
            "CIRCLE_HEADLESS_TOKEN",
            "CIRCLE_MEMBER_EMAIL",
            "FACEBOOK_EMAIL",
            "FACEBOOK_PAGE_ACCESS_TOKEN",
            "FACEBOOK_PAGE_ID",
            "FACEBOOK_PASSWORD",
            "INSTAGRAM_BUSINESS_ACCOUNT_ID",
            "INSTAGRAM_EMAIL",
            "INSTAGRAM_PASSWORD",
            "LINKEDIN_ACCESS_TOKEN",
            "LINKEDIN_EMAIL",
            "LINKEDIN_PASSWORD",
            "X_ACCESS_TOKEN",
            "X_ACCESS_TOKEN_SECRET",
            "X_API_KEY",
            "X_API_SECRET",
            "X_PASSWORD",
            "X_USERNAME",
        ],
        "sales_ops": [
            "ASANA_ACCESS_TOKEN",
            "ASANA_PROJECT_ID",
            "ASANA_USERS",
            "ASANA_WORKSPACE_ID",
            "GRAPH_CLIENT_ID",
            "GRAPH_CLIENT_SECRET",
            "GRAPH_TENANT_ID",
            "GRAPH_USER_EMAIL",
            "PAPERCLIP_API_KEY",
            "PAPERCLIP_API_URL",
            "PAPERCLIP_COMPANY_ID",
            "PERSONAL_GMAIL_ACCOUNT",
            "YourBusiness_API_URL",
            "YourBusiness_INTERN_TOKEN",
            "SLACK_APP_TOKEN",
            "SLACK_BOT_TOKEN",
            "SLACK_MONITORED_CHANNELS",
            "SLACK_NOTIFICATION_CHANNEL",
            "SLACK_OWNER_USER_ID",
        ],
        "customer_ops": [
            "ASANA_ACCESS_TOKEN",
            "ASANA_PROJECT_ID",
            "ASANA_USERS",
            "ASANA_WORKSPACE_ID",
            "GRAPH_CLIENT_ID",
            "GRAPH_CLIENT_SECRET",
            "GRAPH_TENANT_ID",
            "GRAPH_USER_EMAIL",
            "GOOGLE_CALENDAR_ID",
            "PERSONAL_GMAIL_ACCOUNT",
            "YourBusiness_API_URL",
            "YourBusiness_INTERN_TOKEN",
            "SLACK_APP_TOKEN",
            "SLACK_BOT_TOKEN",
            "SLACK_MONITORED_CHANNELS",
            "SLACK_NOTIFICATION_CHANNEL",
        ],
        "finance": [
            "PLAID_CLIENT_ID",
            "PLAID_ENV",
            "PLAID_SECRET",
            "TELLER_ACCESS_TOKEN",
            "TELLER_CERT_PATH",
            "TELLER_KEY_PATH",
        ],
        "YourBusiness_ops": [
            "CLOUDFLARE_ACCOUNT_ID",
            "CLOUDFLARE_API_TOKEN",
            "NEXT_PUBLIC_SUPABASE_URL",
            "PAPERCLIP_API_KEY",
            "PAPERCLIP_API_URL",
            "PAPERCLIP_COMPANY_ID",
            "YourBusiness_API_URL",
            "YourBusiness_INTERN_TOKEN",
            "YourBusiness_SSH_HOST",
            "YourBusiness_SSH_USER",
            "SUPABASE_SERVICE_ROLE_KEY",
        ],
        "mission_control": [
            "MC_AGENT_API_KEY",
            "MC_GATEWAY_ORIGINS",
            "MC_GATEWAY_TOKEN",
            "MC_HEARTBEAT_URL",
            "RELAY_AUTH_TOKEN",
            "RELAY_WS_URL",
        ],
        "voice": [
            "VOICE_TTS_ENGINE",
            "VOICE_TTS_VOICE_EDGE",
        ],
        "heartbeat": [
            "HEARTBEAT_ACTIVE_HOURS_END",
            "HEARTBEAT_ACTIVE_HOURS_START",
            "HEARTBEAT_INTERVAL_MINUTES",
            "HEARTBEAT_TIMEZONE",
            "INTENT_AUTODISPATCH_ENABLED",
            "REFLECTION_HOUR",
            "SOCIAL_CADENCE_ENABLED",
        ],
    },
    "skill_groups": {
        "website_design": [
            "agent-browser",
            "css-animations",
            "gsap",
            "hyperframes",
            "hyperframes-cli",
            "hyperframes-media",
            "hyperframes-registry",
            "image-persona",
            "imagegen",
            "tailwind",
            "three",
            "waapi",
            "website-design-homie",
            "website-to-hyperframes",
        ],
        "socials": [
            "founder-video-digester",
            "image-persona",
            "imagegen",
            "instagram-post",
            "linkedin",
            "linkedin-post",
            "reddit-post",
            "vault-ops",
            "video-director",
            "video-processor",
            "x-post",
            "yt-livestream",
            "yt-script",
            "yt-shorts",
        ],
        "browser_ops": [
            "agent-browser",
            "desktop-agent",
            "mcp-client",
            "telegram-bot-test",
            "url-hunter",
        ],
        "sales": [
            "direct-integrations",
            "founder-video-digester",
            "linkedin",
            "linkedin-post",
            "pdf",
            "pptx-generator",
            "sop-creator",
            "vault-ops",
        ],
        "seo": [
            "brand-fleet-seo",
            "direct-integrations",
            "url-hunter",
            "vault-ops",
            "website-design-homie",
            "website-to-hyperframes",
        ],
        "content": [
            "founder-video-digester",
            "image-persona",
            "imagegen",
            "instagram-post",
            "linkedin-post",
            "reddit-post",
            "vault-ops",
            "video-director",
            "video-processor",
            "x-post",
            "yt-livestream",
            "yt-script",
            "yt-shorts",
        ],
        "finance": [
            "check-balance",
            "daily-spend-query",
            "teller-api-audit",
        ],
        "support": [
            "direct-integrations",
            "file-vault-note",
            "pdf",
            "vault-ops",
        ],
        "operations": [
            "audit-and-harden",
            "clutch",
            "codebase-orient",
            "diagnose-process",
            "direct-integrations",
            "live-chat",
            "mcp-client",
            "phase0-recon",
            "repo-prime",
            "schedule-task",
            "session-resume",
            "vault-ops",
        ],
        "internal_factory": [
            "capabilities-audit",
            "export-framework",
            "homie-self-map",
            "inspect-model-agnosticism",
            "prd",
            "prp-from-context",
            "skill-creator",
            "sop-creator",
            "verify-hooks",
        ],
    },
    "profile_defaults": {
        "env_groups": ["runtime_core", "vault_memory"],
        "skill_groups": [],
        "skills": [],
    },
    "profiles": {
        "default": {"env_groups": ["*"], "skill_groups": ["*"]},
        "browser_ops": {
            "env_groups": ["runtime_core", "vault_memory", "browser_ops", "business_profile"],
            "skill_groups": ["browser_ops"],
        },
        "content": {
            "env_groups": ["runtime_core", "vault_memory", "business_profile"],
            "skill_groups": ["content"],
        },
        "customer_success": {
            "env_groups": ["runtime_core", "vault_memory", "customer_ops", "business_profile"],
            "skill_groups": ["support"],
        },
        "finance": {
            "env_groups": ["runtime_core", "vault_memory", "finance", "business_profile"],
            "skill_groups": ["finance"],
        },
        "finance_admin": {
            "env_groups": ["runtime_core", "vault_memory", "finance", "YourBusiness_ops"],
            "skill_groups": ["finance", "operations"],
        },
        "front_desk": {
            "env_groups": ["runtime_core", "vault_memory", "customer_ops", "business_profile"],
            "skill_groups": ["support"],
        },
        "internal_factory": {
            "env_groups": [
                "runtime_core",
                "vault_memory",
                "mission_control",
                "YourBusiness_ops",
                "heartbeat",
            ],
            "skill_groups": ["internal_factory", "operations"],
        },
        "marketing": {
            "env_groups": ["runtime_core", "vault_memory", "business_profile", "search_analytics"],
            "skill_groups": ["content", "seo"],
        },
        "operations": {
            "env_groups": [
                "runtime_core",
                "vault_memory",
                "mission_control",
                "YourBusiness_ops",
                "heartbeat",
                "voice",
            ],
            "skill_groups": ["operations", "internal_factory"],
        },
        "sales": {
            "env_groups": ["runtime_core", "vault_memory", "sales_ops", "business_profile"],
            "skill_groups": ["sales"],
        },
        "seo_content": {
            "env_groups": ["runtime_core", "vault_memory", "search_analytics", "business_profile"],
            "skill_groups": ["seo", "content"],
        },
        "seo_geo": {
            "env_groups": ["runtime_core", "vault_memory", "search_analytics", "business_profile"],
            "skill_groups": ["seo"],
        },
        "socials": {
            "env_groups": [
                "runtime_core",
                "vault_memory",
                "socials_write",
                "business_profile",
                "browser_ops",
            ],
            "skill_groups": ["socials", "browser_ops"],
        },
        "support": {
            "env_groups": ["runtime_core", "vault_memory", "customer_ops", "business_profile"],
            "skill_groups": ["support"],
        },
        "website_design": {
            "env_groups": [
                "runtime_core",
                "vault_memory",
                "browser_ops",
                "search_analytics",
                "business_profile",
            ],
            "skill_groups": ["website_design", "seo"],
        },
    },
}


def get_matrix_path(path: str | Path | None = None) -> Path:
    """Return the active capability-matrix path."""

    if path is not None:
        return Path(path).expanduser().resolve(strict=False)
    env_path = os.environ.get("HOMIE_PERSONA_CAPABILITY_MATRIX", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve(strict=False)
    return DEFAULT_MATRIX_PATH


def get_master_env_path(path: str | Path | None = None) -> Path:
    """Return the master env path. This is intentionally not profile-scoped."""

    if path is not None:
        return Path(path).expanduser().resolve(strict=False)
    return MASTER_ENV_PATH


def load_capability_matrix(path: str | Path | None = None) -> dict[str, Any]:
    """Load the local matrix, falling back to the built-in default matrix."""

    matrix_path = get_matrix_path(path)
    if not matrix_path.is_file():
        matrix = copy.deepcopy(_DEFAULT_MATRIX)
    else:
        try:
            raw = yaml.safe_load(matrix_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise CapabilityMatrixError(f"yaml: {matrix_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise CapabilityMatrixError(
                f"shape: {matrix_path}: top-level must be a mapping"
            )
        matrix = raw
    validate_capability_matrix(matrix)
    return matrix


def validate_capability_matrix(matrix: dict[str, Any]) -> None:
    """Validate matrix shape and group references."""

    env_groups = matrix.get("env_groups", {})
    skill_groups = matrix.get("skill_groups", {})
    profiles = matrix.get("profiles", {})
    profile_defaults = matrix.get("profile_defaults", {})
    for field_name, section in (
        ("env_groups", env_groups),
        ("skill_groups", skill_groups),
        ("profiles", profiles),
    ):
        if not isinstance(section, dict):
            raise CapabilityMatrixError(f"{field_name} must be a mapping")
    if profile_defaults and not isinstance(profile_defaults, dict):
        raise CapabilityMatrixError("profile_defaults must be a mapping")

    for group_name, keys in env_groups.items():
        _require_string_list(keys, f"env_groups.{group_name}")
    for group_name, names in skill_groups.items():
        _require_string_list(names, f"skill_groups.{group_name}")

    for profile_name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise CapabilityMatrixError(f"profiles.{profile_name} must be a mapping")
        for group_name in _coerce_string_list(profile.get("env_groups", [])):
            if group_name != "*" and group_name not in env_groups:
                raise CapabilityMatrixError(
                    f"profiles.{profile_name}.env_groups references unknown group "
                    f"{group_name!r}"
                )
        for group_name in _coerce_string_list(profile.get("skill_groups", [])):
            if group_name != "*" and group_name not in skill_groups:
                raise CapabilityMatrixError(
                    f"profiles.{profile_name}.skill_groups references unknown group "
                    f"{group_name!r}"
                )
        _require_string_list(profile.get("skills", []), f"profiles.{profile_name}.skills")


def resolve_env_keys(
    profile_name: str,
    *,
    matrix: dict[str, Any] | None = None,
    matrix_path: str | Path | None = None,
    master_keys: Iterable[str] | None = None,
) -> list[str]:
    """Return env key names delegated to *profile_name*."""

    capability_matrix = matrix or load_capability_matrix(matrix_path)
    env_groups = capability_matrix.get("env_groups", {})
    profile = _profile_entry(capability_matrix, profile_name)
    group_names = _coerce_string_list(profile.get("env_groups", []))
    if "*" in group_names:
        if master_keys is not None:
            return sorted({key for key in master_keys if key})
        return sorted({key for keys in env_groups.values() for key in keys})
    keys: set[str] = set()
    for group_name in group_names:
        try:
            keys.update(env_groups[group_name])
        except KeyError as exc:
            raise CapabilityMatrixError(
                f"profile {profile_name!r} references unknown env group "
                f"{group_name!r}"
            ) from exc
    return sorted(keys)


def resolve_skill_allowlist(
    profile_name: str,
    *,
    matrix: dict[str, Any] | None = None,
    matrix_path: str | Path | None = None,
) -> frozenset[str] | None:
    """Return allowed central skill names, or None when the profile gets all."""

    capability_matrix = matrix or load_capability_matrix(matrix_path)
    skill_groups = capability_matrix.get("skill_groups", {})
    profile = _profile_entry(capability_matrix, profile_name)
    group_names = _coerce_string_list(profile.get("skill_groups", []))
    direct_skills = _coerce_string_list(profile.get("skills", []))
    if "*" in group_names or "*" in direct_skills:
        return None
    skills: set[str] = set(direct_skills)
    for group_name in group_names:
        try:
            skills.update(skill_groups[group_name])
        except KeyError as exc:
            raise CapabilityMatrixError(
                f"profile {profile_name!r} references unknown skill group "
                f"{group_name!r}"
            ) from exc
    return frozenset(sorted(skills))


def read_env_values(path: str | Path) -> dict[str, str]:
    """Read a dotenv file without leaking values to stdout."""

    env_path = Path(path)
    if not env_path.is_file():
        return {}
    from dotenv import dotenv_values

    return {str(k): (v or "") for k, v in dotenv_values(str(env_path)).items() if k}


def build_env_sync_plan(
    profile_name: str,
    *,
    matrix_path: str | Path | None = None,
    master_env_path: str | Path | None = None,
) -> EnvSyncPlan:
    """Build a no-side-effect plan to derive a profile env file."""

    master_path = get_master_env_path(master_env_path)
    master_env = read_env_values(master_path)
    allowed_keys = resolve_env_keys(
        profile_name,
        matrix_path=matrix_path,
        master_keys=master_env.keys(),
    )
    values = {
        key: master_env[key]
        for key in allowed_keys
        if key in master_env and master_env[key] != ""
    }
    present_keys = sorted(values)
    missing_keys = sorted(key for key in allowed_keys if key not in values)
    return EnvSyncPlan(
        profile_name=profile_name,
        profile_env_path=get_persona_paths(profile_name)["env_file"],
        allowed_keys=allowed_keys,
        present_keys=present_keys,
        missing_keys=missing_keys,
        values=values,
    )


def write_profile_env(plan: EnvSyncPlan) -> Path:
    """Write one derived profile env file from an EnvSyncPlan."""

    if plan.profile_name == "default":
        raise CapabilityMatrixError("default profile uses the master env directly")
    plan.profile_env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Generated by `thehomie profile env-sync --write`.",
        "# Source: .claude/scripts/.env",
        "# Do not hand-edit secrets here; update the master env and resync.",
        "",
    ]
    for key in plan.present_keys:
        lines.append(f"{key}={_format_env_value(plan.values[key])}")
    plan.profile_env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return plan.profile_env_path


def build_capability_scoped_env(
    profile_name: str,
    *,
    profile_root: Path,
    parent_env: dict[str, str] | None = None,
    matrix_path: str | Path | None = None,
    master_env_path: str | Path | None = None,
) -> dict[str, str]:
    """Return a subprocess env containing only base OS keys plus delegated keys."""

    from runtime.subprocess_env import get_scrubbed_sdk_env

    scrubbed = get_scrubbed_sdk_env(parent_env=parent_env, profile_root=profile_root)
    plan = build_env_sync_plan(
        profile_name,
        matrix_path=matrix_path,
        master_env_path=master_env_path,
    )
    out = {
        key: value
        for key, value in scrubbed.items()
        if key.upper() in _BASE_RUNTIME_ENV_KEYS
    }
    out.update(plan.values)
    out["HOMIE_HOME"] = str(profile_root)
    return out


def safe_env_sync_summary(plan: EnvSyncPlan) -> dict[str, Any]:
    """Return a secret-free summary for dry-runs and JSON output."""

    return {
        "profile": plan.profile_name,
        "env_file": str(plan.profile_env_path),
        "allowed_keys": list(plan.allowed_keys),
        "present_keys": list(plan.present_keys),
        "missing_keys": list(plan.missing_keys),
        "present_count": len(plan.present_keys),
        "missing_count": len(plan.missing_keys),
    }


def capability_matrix_template_text() -> str:
    """Return the built-in matrix as YAML for bootstrapping local config."""

    return yaml.safe_dump(
        _DEFAULT_MATRIX,
        sort_keys=False,
        allow_unicode=False,
        width=100,
    )


def _profile_entry(matrix: dict[str, Any], profile_name: str) -> dict[str, Any]:
    profiles = matrix.get("profiles", {})
    if profile_name in profiles:
        profile = profiles[profile_name]
    else:
        profile = matrix.get("profile_defaults", {})
    if not isinstance(profile, dict):
        raise CapabilityMatrixError(f"profile {profile_name!r} must resolve to mapping")
    return profile


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def _require_string_list(value: Any, field_path: str) -> None:
    if value is None:
        return
    if isinstance(value, str):
        return
    if not isinstance(value, list):
        raise CapabilityMatrixError(f"{field_path} must be a list of strings")
    for item in value:
        if not isinstance(item, str):
            raise CapabilityMatrixError(f"{field_path} must contain only strings")


_SIMPLE_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:@%+=,-]*$")


def _format_env_value(value: str) -> str:
    if _SIMPLE_ENV_VALUE_RE.match(value):
        return value
    return "'" + value.replace("'", "\\'").replace("\n", "\\n") + "'"


__all__ = [
    "CapabilityMatrixError",
    "DEFAULT_MATRIX_PATH",
    "EnvSyncPlan",
    "MASTER_ENV_PATH",
    "build_env_sync_plan",
    "build_capability_scoped_env",
    "capability_matrix_template_text",
    "get_master_env_path",
    "get_matrix_path",
    "load_capability_matrix",
    "read_env_values",
    "resolve_env_keys",
    "resolve_skill_allowlist",
    "safe_env_sync_summary",
    "validate_capability_matrix",
    "write_profile_env",
]
