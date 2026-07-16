"""Canonical direct-integration action policy.

This module is the code-level source of truth for integration actions,
their effects, and which runtime surfaces may invoke them. OAuth scopes and
credential files still live in the existing auth/config layers; this policy is
the software guard that keeps wrappers and internal callers aligned.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

EffectLevel = Literal["read", "write", "send", "archive", "delete", "external_post"]
Exposure = Literal["model", "internal", "operator_confirmed"]


class IntegrationPolicyError(PermissionError):
    """Raised when an integration action is not allowed by policy."""


@dataclass(frozen=True)
class IntegrationAction:
    """Declared policy metadata for one integration action."""

    integration: str
    action: str
    effect: EffectLevel
    exposures: tuple[Exposure, ...] = ("model",)
    default_enabled: bool = True
    required_scopes: tuple[str, ...] = field(default_factory=tuple)
    config_hints: tuple[str, ...] = field(default_factory=tuple)
    description: str = ""

    @property
    def id(self) -> str:
        return f"{self.integration}.{self.action}"

    @property
    def is_mutating(self) -> bool:
        return self.effect != "read"


def _norm(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _action(
    integration: str,
    action: str,
    effect: EffectLevel,
    *,
    exposures: tuple[Exposure, ...] = ("model",),
    default_enabled: bool = True,
    required_scopes: tuple[str, ...] = (),
    config_hints: tuple[str, ...] = (),
    description: str = "",
) -> IntegrationAction:
    return IntegrationAction(
        integration=_norm(integration),
        action=_norm(action),
        effect=effect,
        exposures=exposures,
        default_enabled=default_enabled,
        required_scopes=required_scopes,
        config_hints=config_hints,
        description=description,
    )


_ACTIONS: tuple[IntegrationAction, ...] = (
    # Gmail: reads are model-facing; archive requires an explicit operator or
    # internal automation path and still relies on the shared Google token.
    _action("gmail", "list", "read", required_scopes=("gmail.readonly",)),
    _action("gmail", "urgent", "read", required_scopes=("gmail.readonly",)),
    _action("gmail", "unread", "read", required_scopes=("gmail.readonly",)),
    _action("gmail", "read", "read", required_scopes=("gmail.readonly",)),
    _action(
        "gmail",
        "archive",
        "archive",
        exposures=("operator_confirmed", "internal"),
        required_scopes=("gmail.modify",),
        description="Remove inbox label from Gmail messages.",
    ),
    # Personal Gmail remains read-only.
    _action("personal_gmail", "list", "read", required_scopes=("gmail.readonly",)),
    _action("personal_gmail", "unread", "read", required_scopes=("gmail.readonly",)),
    _action("personal_gmail", "read", "read", required_scopes=("gmail.readonly",)),
    # Calendar.
    _action("calendar", "today", "read", required_scopes=("calendar.readonly",)),
    _action("calendar", "upcoming", "read", required_scopes=("calendar.readonly",)),
    _action("calendar", "soon", "read", required_scopes=("calendar.readonly",)),
    # Asana wrapper actions. Module-level Asana mutator enforcement is a
    # follow-on; wrapper calls are still declared and policy-checked here.
    _action("asana", "my_tasks", "read", config_hints=("ASANA_ACCESS_TOKEN",)),
    _action("asana", "project", "read", config_hints=("ASANA_ACCESS_TOKEN",)),
    _action("asana", "overdue", "read", config_hints=("ASANA_ACCESS_TOKEN",)),
    _action("asana", "due_soon", "read", config_hints=("ASANA_ACCESS_TOKEN",)),
    _action(
        "asana",
        "complete",
        "write",
        exposures=("operator_confirmed",),
        config_hints=("ASANA_ACCESS_TOKEN",),
    ),
    _action(
        "asana",
        "create",
        "write",
        exposures=("operator_confirmed",),
        config_hints=("ASANA_ACCESS_TOKEN",),
    ),
    _action(
        "asana",
        "comment",
        "write",
        exposures=("operator_confirmed",),
        config_hints=("ASANA_ACCESS_TOKEN",),
    ),
    _action(
        "asana",
        "move",
        "write",
        exposures=("operator_confirmed",),
        config_hints=("ASANA_ACCESS_TOKEN",),
    ),
    # Slack.
    _action("slack", "channels", "read", config_hints=("SLACK_BOT_TOKEN",)),
    _action("slack", "messages", "read", config_hints=("SLACK_BOT_TOKEN",)),
    _action("slack", "check", "read", config_hints=("SLACK_BOT_TOKEN",)),
    _action(
        "slack",
        "send",
        "external_post",
        exposures=("operator_confirmed", "internal"),
        config_hints=("SLACK_BOT_TOKEN",),
        description="Post a Slack message or notification.",
    ),
    # Google Sheets.
    _action("sheets", "read", "read", required_scopes=("spreadsheets.readonly",)),
    _action("sheets", "info", "read", required_scopes=("spreadsheets.readonly",)),
    _action(
        "sheets",
        "write",
        "write",
        exposures=("operator_confirmed",),
        required_scopes=("spreadsheets",),
    ),
    _action(
        "sheets",
        "append",
        "write",
        exposures=("operator_confirmed",),
        required_scopes=("spreadsheets",),
    ),
    # Docs and Drive are read-only through the wrapper.
    _action("docs", "read", "read", required_scopes=("documents.readonly",)),
    _action("docs", "info", "read", required_scopes=("documents.readonly",)),
    _action("drive", "find", "read", required_scopes=("drive.readonly",)),
    _action("drive", "list", "read", required_scopes=("drive.readonly",)),
    _action("drive", "get", "read", required_scopes=("drive.readonly",)),
    # Circle wrapper is read-only in this skill.
    _action("circle", "spaces", "read", config_hints=("CIRCLE_ADMIN_TOKEN",)),
    _action("circle", "posts", "read", config_hints=("CIRCLE_ADMIN_TOKEN",)),
    _action("circle", "post", "read", config_hints=("CIRCLE_ADMIN_TOKEN",)),
    _action("circle", "search", "read", config_hints=("CIRCLE_ADMIN_TOKEN",)),
    _action("circle", "dms", "read", config_hints=("CIRCLE_ADMIN_TOKEN",)),
    _action("circle", "dm", "read", config_hints=("CIRCLE_ADMIN_TOKEN",)),
    _action("circle", "notifications", "read", config_hints=("CIRCLE_ADMIN_TOKEN",)),
    _action("circle", "feed", "read", config_hints=("CIRCLE_ADMIN_TOKEN",)),
    # Search Console and Analytics.
    _action(
        "search_console",
        "top_queries",
        "read",
        required_scopes=("webmasters.readonly",),
        config_hints=("GSC_SITE_URL",),
    ),
    _action(
        "search_console",
        "top_pages",
        "read",
        required_scopes=("webmasters.readonly",),
        config_hints=("GSC_SITE_URL",),
    ),
    _action(
        "search_console",
        "overview",
        "read",
        required_scopes=("webmasters.readonly",),
        config_hints=("GSC_SITE_URL",),
    ),
    _action(
        "analytics",
        "overview",
        "read",
        required_scopes=("analytics.readonly",),
        config_hints=("GA4_PROPERTY_ID",),
    ),
    _action(
        "analytics",
        "top_pages",
        "read",
        required_scopes=("analytics.readonly",),
        config_hints=("GA4_PROPERTY_ID",),
    ),
    _action(
        "analytics",
        "traffic_sources",
        "read",
        required_scopes=("analytics.readonly",),
        config_hints=("GA4_PROPERTY_ID",),
    ),
    _action(
        "analytics",
        "realtime",
        "read",
        required_scopes=("analytics.readonly",),
        config_hints=("GA4_PROPERTY_ID",),
    ),
    # Outlook is used by chat handlers and cleanup, even though it is not
    # currently exposed through the direct-integrations query wrapper.
    _action(
        "outlook",
        "list",
        "read",
        config_hints=("GRAPH_CLIENT_ID", "GRAPH_TENANT_ID", "GRAPH_USER_EMAIL"),
    ),
    _action(
        "outlook",
        "read",
        "read",
        config_hints=("GRAPH_CLIENT_ID", "GRAPH_TENANT_ID", "GRAPH_USER_EMAIL"),
    ),
    _action(
        "outlook",
        "unread",
        "read",
        config_hints=("GRAPH_CLIENT_ID", "GRAPH_TENANT_ID", "GRAPH_USER_EMAIL"),
    ),
    _action(
        "outlook",
        "archive",
        "archive",
        exposures=("operator_confirmed", "internal"),
        config_hints=("GRAPH_CLIENT_ID", "GRAPH_TENANT_ID", "GRAPH_USER_EMAIL"),
    ),
    _action(
        "outlook",
        "send_email",
        "send",
        exposures=("operator_confirmed",),
        config_hints=("GRAPH_CLIENT_ID", "GRAPH_TENANT_ID", "GRAPH_USER_EMAIL"),
    ),
    # Social media posting — default-deny, operator-confirmed.
    _action(
        "social",
        "draft_content",
        "write",
        exposures=("operator_confirmed", "internal"),
        description="Generate a social media draft (cadence scheduler + manual).",
    ),
    _action(
        "social",
        "post_linkedin",
        "external_post",
        exposures=("operator_confirmed",),
        config_hints=("LINKEDIN_EMAIL", "LINKEDIN_PASSWORD"),
        description="Post to LinkedIn via browser executor.",
    ),
    _action(
        "social",
        "post_facebook",
        "external_post",
        exposures=("operator_confirmed",),
        config_hints=("FACEBOOK_PAGE_ACCESS_TOKEN", "FACEBOOK_PAGE_ID"),
        description="Post to Facebook Page via Graph API.",
    ),
    _action(
        "social",
        "post_x",
        "external_post",
        exposures=("operator_confirmed",),
        config_hints=("HOMIE_BROWSER_CDP_PORT",),
        description="Post as Primo on X via the operator-approved visible browser executor.",
    ),
    _action(
        "social",
        "post_reddit",
        "external_post",
        exposures=("operator_confirmed",),
        description="Post or comment on Reddit via browser executor.",
    ),
    _action(
        "social",
        "post_instagram",
        "external_post",
        exposures=("operator_confirmed",),
        config_hints=("INSTAGRAM_BUSINESS_ACCOUNT_ID", "FACEBOOK_PAGE_ACCESS_TOKEN"),
        description="Post to Instagram via Meta Graph API (requires image URL).",
    ),
    # Postiz-transport channels (execution_method: postiz in channels.yaml).
    # The gate stays per-channel; Postiz is only the publishing transport.
    _action(
        "social",
        "post_mastodon",
        "external_post",
        exposures=("operator_confirmed",),
        config_hints=("POSTIZ_API_URL", "POSTIZ_API_KEY"),
        description="Post to Mastodon via the Postiz publishing lane.",
    ),
    _action(
        "social",
        "post_bluesky",
        "external_post",
        exposures=("operator_confirmed",),
        config_hints=("POSTIZ_API_URL", "POSTIZ_API_KEY"),
        description="Post to Bluesky via the Postiz publishing lane.",
    ),
    _action(
        "social",
        "post_threads",
        "external_post",
        exposures=("operator_confirmed",),
        config_hints=("POSTIZ_API_URL", "POSTIZ_API_KEY"),
        description="Post to Threads via the Postiz publishing lane.",
    ),
    _action(
        "social",
        "post_youtube",
        "external_post",
        exposures=("operator_confirmed",),
        config_hints=("POSTIZ_API_URL", "POSTIZ_API_KEY"),
        description=(
            "Publish to YouTube via the Postiz publishing lane. "
            "Video platforms are refused by the v1 executor (text/image only)."
        ),
    ),
    # Autonomous co-founder orchestrator. The send runs from the heartbeat
    # or cron process (never model-invoked) and the message itself IS the
    # operator notification, so the exposure is internal.
    _action(
        "cofounder",
        "notify",
        "send",
        exposures=("internal",),
        config_hints=("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_IDS"),
        description="Co-founder terminal-flip notification to the operator's Telegram.",
    ),
)

try:
    from local_extension_loader import apply_local_extension_hook

    _local_actions = list(_ACTIONS)
    apply_local_extension_hook(
        "register_integration_actions",
        _local_actions,
        action_factory=_action,
    )
    _ACTIONS = tuple(_local_actions)
except ImportError:
    pass

_ACTION_INDEX: dict[tuple[str, str], IntegrationAction] = {
    (action.integration, action.action): action for action in _ACTIONS
}


def normalize_integration_id(name: str) -> str:
    """Normalize service ids used by CLI slugs and registry keys."""
    return _norm(name)


def normalize_action_name(name: str) -> str:
    """Normalize CLI action slugs into canonical action names."""
    return _norm(name)


def get_integration_actions(integration: str | None = None) -> tuple[IntegrationAction, ...]:
    """Return declared actions, optionally filtered by integration id."""
    if integration is None:
        return _ACTIONS

    normalized = normalize_integration_id(integration)
    return tuple(action for action in _ACTIONS if action.integration == normalized)


def get_integration_action(
    integration: str,
    action: str,
) -> IntegrationAction | None:
    """Return a declared integration action if present."""
    return _ACTION_INDEX.get(
        (normalize_integration_id(integration), normalize_action_name(action))
    )


def is_integration_action_allowed(
    integration: str,
    action: str,
    *,
    surface: Exposure | None = None,
    policy_overrides: Mapping[str, bool] | None = None,
) -> bool:
    """Return whether a declared action is enabled and exposed to a surface.

    ``policy_overrides`` is an explicit test/embedding hook keyed by
    ``"<integration>.<action>"``. It keeps policy evaluation deterministic
    without adding a deployment UI or per-tenant override system.
    """
    declared = get_integration_action(integration, action)
    if declared is None:
        return False

    enabled = declared.default_enabled
    if policy_overrides is not None:
        enabled = policy_overrides.get(declared.id, enabled)
    if not enabled:
        return False

    return surface is None or surface in declared.exposures


def require_integration_action(
    integration: str,
    action: str,
    *,
    surface: Exposure | None = None,
    caller: str = "unspecified",
    policy_overrides: Mapping[str, bool] | None = None,
) -> IntegrationAction:
    """Return action metadata or raise a deterministic policy error."""
    declared = get_integration_action(integration, action)
    normalized_id = f"{normalize_integration_id(integration)}.{normalize_action_name(action)}"
    if declared is None:
        raise IntegrationPolicyError(
            f"Unknown integration action '{normalized_id}' requested by {caller}."
        )

    enabled = declared.default_enabled
    if policy_overrides is not None:
        enabled = policy_overrides.get(declared.id, enabled)
    if not enabled:
        raise IntegrationPolicyError(
            f"Integration action '{declared.id}' is disabled by policy for {caller}."
        )

    if surface is not None and surface not in declared.exposures:
        allowed = ", ".join(declared.exposures)
        raise IntegrationPolicyError(
            f"Integration action '{declared.id}' is not exposed to '{surface}' "
            f"for {caller}; allowed surfaces: {allowed}."
        )

    return declared
