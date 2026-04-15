"""
Integration registry for The Homie.

Lightweight registry that tracks which integrations are available and which
are enabled based on environment configuration. No plugin system — just a
dict of metadata with lazy module loading.

Usage:
    from integrations.registry import get_all, get_enabled, is_enabled

    all_integrations = get_all()       # All registered integrations
    enabled = get_enabled()            # Only integrations with required config set
    if is_enabled("gmail"):            # Check a specific integration
        ...
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class IntegrationInfo:
    """Metadata for a registered integration."""

    name: str  # e.g. "gmail"
    display_name: str  # e.g. "Gmail"
    auth_type: str  # "google_oauth" | "token"
    required_config: list[str] = field(default_factory=list)  # Env vars needed
    module_path: str = ""  # e.g. "integrations.gmail"


# ---------------------------------------------------------------------------
# Registry — populated at import time with metadata only.
# Actual integration modules are NOT imported until needed (lazy).
# ---------------------------------------------------------------------------
_REGISTRY: dict[str, IntegrationInfo] = {
    "gmail": IntegrationInfo(
        name="gmail",
        display_name="Gmail",
        auth_type="google_oauth",
        required_config=[],  # File-based (google_token.json)
        module_path="integrations.gmail",
    ),
    "calendar": IntegrationInfo(
        name="calendar",
        display_name="Google Calendar",
        auth_type="google_oauth",
        required_config=["GOOGLE_CALENDAR_ID"],
        module_path="integrations.calendar_api",
    ),
    "asana": IntegrationInfo(
        name="asana",
        display_name="Asana",
        auth_type="token",
        required_config=["ASANA_ACCESS_TOKEN"],
        module_path="integrations.asana_api",
    ),
    "slack": IntegrationInfo(
        name="slack",
        display_name="Slack",
        auth_type="token",
        required_config=["SLACK_BOT_TOKEN"],
        module_path="integrations.slack_api",
    ),
    "sheets": IntegrationInfo(
        name="sheets",
        display_name="Google Sheets",
        auth_type="google_oauth",
        required_config=[],
        module_path="integrations.sheets_api",
    ),
    "docs": IntegrationInfo(
        name="docs",
        display_name="Google Docs",
        auth_type="google_oauth",
        required_config=[],
        module_path="integrations.docs_api",
    ),
    "drive": IntegrationInfo(
        name="drive",
        display_name="Google Drive",
        auth_type="google_oauth",
        required_config=[],
        module_path="integrations.drive_api",
    ),
    "circle": IntegrationInfo(
        name="circle",
        display_name="Circle",
        auth_type="token",
        required_config=["CIRCLE_ADMIN_TOKEN"],
        module_path="integrations.circle_api",
    ),
    "search_console": IntegrationInfo(
        name="search_console",
        display_name="Google Search Console",
        auth_type="google_oauth",
        required_config=["GSC_SITE_URL"],
        module_path="integrations.search_console_api",
    ),
    "analytics": IntegrationInfo(
        name="analytics",
        display_name="Google Analytics (GA4)",
        auth_type="google_oauth",
        required_config=["GA4_PROPERTY_ID"],
        module_path="integrations.analytics_api",
    ),
    "personal_gmail": IntegrationInfo(
        name="personal_gmail",
        display_name="Personal Gmail (read-only)",
        auth_type="personal_gmail_token",
        required_config=[],  # File-based (google_token_pedro.json)
        module_path="integrations.personal_gmail",
    ),
}


def _has_google_token() -> bool:
    """Check if Google OAuth token file exists."""
    token_path = Path(__file__).parent / "google_token.json"
    return token_path.exists()


def _has_personal_gmail_token() -> bool:
    """Check if personal Gmail OAuth token file exists."""
    import os
    token_path = os.getenv("PERSONAL_GMAIL_TOKEN", str(Path(__file__).parent / "google_token_pedro.json"))
    return Path(token_path).exists()


def get_all() -> dict[str, IntegrationInfo]:
    """Return all registered integrations."""
    return dict(_REGISTRY)


def get_enabled() -> dict[str, IntegrationInfo]:
    """Return only integrations whose required config is set."""
    enabled: dict[str, IntegrationInfo] = {}

    for name, info in _REGISTRY.items():
        if info.auth_type == "google_oauth":
            # Google integrations need the token file to exist
            if not _has_google_token():
                continue
            # Plus any extra required config (e.g. GOOGLE_CALENDAR_ID)
            if info.required_config and not all(
                os.getenv(var, "") for var in info.required_config
            ):
                continue
        elif info.auth_type == "personal_gmail_token":
            if not _has_personal_gmail_token():
                continue
        elif info.required_config:
            # Token-based integrations need their env vars set
            if not all(os.getenv(var, "") for var in info.required_config):
                continue

        enabled[name] = info

    return enabled


def is_enabled(name: str) -> bool:
    """Check if a specific integration is enabled."""
    return name in get_enabled()
