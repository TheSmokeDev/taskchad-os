"""GitHub signal configuration — paths, toggles, and call-time resolvers."""

from __future__ import annotations

import os
from typing import NamedTuple

import config as _main_config

# ---------------------------------------------------------------------------
# Path constants (derived from the main config's persona-resolved paths)
# ---------------------------------------------------------------------------

GITHUB_SIGNAL_DIR = _main_config.MEMORY_DIR / "github-signal"
GITHUB_SIGNAL_STATE_FILE = _main_config.STATE_DIR / "github-signal-state.json"
# Shallow clones for /stars eval land here (gitignored; deleted after eval
# unless GITHUB_SIGNAL_EVAL_KEEP_CLONE).
REPO_EVAL_SANDBOX_DIR = _main_config.DATA_DIR / "repo-eval"

_DEFAULT_TRENDING_KEYWORDS = (
    "ai,llm,agent,rag,mcp,claude,gpt,embedding,inference,transformer,voice,eval"
)


class GithubSignalSettings(NamedTuple):
    """Effective GitHub signal knobs (call-time resolved)."""

    enabled: bool
    pick_count: int
    resurface_cooldown_weeks: int
    snooze_weeks: int
    trending_keywords: list[str]
    max_budget_usd: float
    discord_channel_id: str
    scout_profile: str
    eval_max_repo_mb: int
    eval_keep_clone: bool


def get_github_signal_settings(
    enabled: bool | None = None,
    pick_count: int | None = None,
    resurface_cooldown_weeks: int | None = None,
    snooze_weeks: int | None = None,
    trending_keywords: list[str] | None = None,
    max_budget_usd: float | None = None,
    discord_channel_id: str | None = None,
    scout_profile: str | None = None,
    eval_max_repo_mb: int | None = None,
    eval_keep_clone: bool | None = None,
) -> GithubSignalSettings:
    """Resolve GitHub signal knobs at CALL TIME (Rule 1).

    None-sentinel args resolve the env at call time so
    ``monkeypatch.setenv`` / a live ``.env`` edit take effect with no reload.

    Knobs:
        GITHUB_SIGNAL_ENABLED                  ("true")
        GITHUB_SIGNAL_PICK_COUNT               ("4")
        GITHUB_SIGNAL_RESURFACE_COOLDOWN_WEEKS ("8")
        GITHUB_SIGNAL_SNOOZE_WEEKS             ("4")
        GITHUB_SIGNAL_TRENDING_KEYWORDS        (comma list; generic AI terms)
        GITHUB_SIGNAL_MAX_BUDGET_USD           ("0.25")
        GITHUB_SIGNAL_DISCORD_CHANNEL_ID       ("" = Discord lane off)
        GITHUB_SIGNAL_SCOUT_PROFILE            ("repo-scout"; "" = sync off)
        GITHUB_SIGNAL_EVAL_MAX_REPO_MB         ("200" — skip clone above this)
        GITHUB_SIGNAL_EVAL_KEEP_CLONE          ("false" — delete sandbox after eval)
    """
    if enabled is None:
        enabled = os.getenv("GITHUB_SIGNAL_ENABLED", "true").lower() == "true"
    if pick_count is None:
        raw = os.getenv("GITHUB_SIGNAL_PICK_COUNT", "4").strip()
        pick_count = int(raw) if raw else 4
    if resurface_cooldown_weeks is None:
        raw = os.getenv("GITHUB_SIGNAL_RESURFACE_COOLDOWN_WEEKS", "8").strip()
        resurface_cooldown_weeks = int(raw) if raw else 8
    if snooze_weeks is None:
        raw = os.getenv("GITHUB_SIGNAL_SNOOZE_WEEKS", "4").strip()
        snooze_weeks = int(raw) if raw else 4
    if trending_keywords is None:
        raw = os.getenv(
            "GITHUB_SIGNAL_TRENDING_KEYWORDS", _DEFAULT_TRENDING_KEYWORDS
        ).strip()
        trending_keywords = [k.strip().lower() for k in raw.split(",") if k.strip()]
    if max_budget_usd is None:
        raw = os.getenv("GITHUB_SIGNAL_MAX_BUDGET_USD", "0.25").strip()
        max_budget_usd = float(raw) if raw else 0.25
    if discord_channel_id is None:
        discord_channel_id = os.getenv("GITHUB_SIGNAL_DISCORD_CHANNEL_ID", "").strip()
    if scout_profile is None:
        scout_profile = os.getenv("GITHUB_SIGNAL_SCOUT_PROFILE", "repo-scout").strip()
    if eval_max_repo_mb is None:
        raw = os.getenv("GITHUB_SIGNAL_EVAL_MAX_REPO_MB", "200").strip()
        eval_max_repo_mb = int(raw) if raw else 200
    if eval_keep_clone is None:
        eval_keep_clone = (
            os.getenv("GITHUB_SIGNAL_EVAL_KEEP_CLONE", "false").lower() == "true"
        )

    return GithubSignalSettings(
        enabled=enabled,
        pick_count=pick_count,
        resurface_cooldown_weeks=resurface_cooldown_weeks,
        snooze_weeks=snooze_weeks,
        trending_keywords=trending_keywords,
        max_budget_usd=max_budget_usd,
        discord_channel_id=discord_channel_id,
        scout_profile=scout_profile,
        eval_max_repo_mb=eval_max_repo_mb,
        eval_keep_clone=eval_keep_clone,
    )
