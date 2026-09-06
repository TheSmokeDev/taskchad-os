"""Signal engine configuration — paths, toggles, and call-time resolvers."""

from __future__ import annotations

import os
import re
from typing import NamedTuple

import config as _main_config

# ---------------------------------------------------------------------------
# Path constants (derived from the main config's persona-resolved paths)
# ---------------------------------------------------------------------------

SIGNAL_DIR = _main_config.MEMORY_DIR / "signal"
SIGNAL_STATE_FILE = _main_config.STATE_DIR / "signal-state.json"
AUTHORITY_SIGNAL_DIR = _main_config.MEMORY_DIR / "authority-signal"
AUTHORITY_STATE_FILE = _main_config.STATE_DIR / "authority-signal-state.json"
AUTHORITY_FIRECRAWL_LEDGER_FILE = (
    _main_config.STATE_DIR / "authority-firecrawl-usage.json"
)

# Toggle — default ON (same as upstream_watch_ENABLED pattern)
SIGNAL_ENABLED = os.getenv("SIGNAL_ENABLED", "true").lower() == "true"
_AUTHORITY_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


# ---------------------------------------------------------------------------
# Call-time resolver (Rule 1 — None sentinels, env read inside body)
# ---------------------------------------------------------------------------


class SignalSettings(NamedTuple):
    """Effective signal engine knobs (call-time resolved)."""

    enabled: bool
    triage_threshold: float
    max_items_per_run: int
    draft_threshold: float
    rss_feeds: list[str]


class AuthoritySettings(NamedTuple):
    """Effective GEO authority-engine knobs (all resolved at call time)."""

    enabled: bool
    triage_threshold: float
    candidate_threshold: float
    exa_results_per_lane: int
    max_packets_per_run: int
    freshness_days: int
    firecrawl_per_run: int
    firecrawl_per_month: int
    configured_repositories: tuple[str, ...]


def get_signal_settings(
    enabled: bool | None = None,
    triage_threshold: float | None = None,
    max_items_per_run: int | None = None,
    draft_threshold: float | None = None,
    rss_feeds: list[str] | None = None,
) -> SignalSettings:
    """Resolve signal engine knobs at CALL TIME (Rule 1).

    None-sentinel args resolve the env at call time so
    ``monkeypatch.setenv`` / a live ``.env`` edit take effect with no reload.

    Knobs:
        SIGNAL_ENABLED          ("true")
        SIGNAL_TRIAGE_THRESHOLD ("0.3")
        SIGNAL_MAX_ITEMS        ("30")
        SIGNAL_DRAFT_THRESHOLD  ("0.7")
        SIGNAL_RSS_FEEDS        (comma-separated URLs)
    """
    if enabled is None:
        enabled = os.getenv("SIGNAL_ENABLED", "true").lower() == "true"
    if triage_threshold is None:
        raw = os.getenv("SIGNAL_TRIAGE_THRESHOLD", "0.3").strip()
        triage_threshold = float(raw) if raw else 0.3
    if max_items_per_run is None:
        raw = os.getenv("SIGNAL_MAX_ITEMS", "30").strip()
        max_items_per_run = int(raw) if raw else 30
    if draft_threshold is None:
        raw = os.getenv("SIGNAL_DRAFT_THRESHOLD", "0.7").strip()
        draft_threshold = float(raw) if raw else 0.7
    if rss_feeds is None:
        raw = os.getenv("SIGNAL_RSS_FEEDS", "").strip()
        if raw:
            rss_feeds = [u.strip() for u in raw.split(",") if u.strip()]
        else:
            rss_feeds = _DEFAULT_RSS_FEEDS[:]

    return SignalSettings(
        enabled=enabled,
        triage_threshold=triage_threshold,
        max_items_per_run=max_items_per_run,
        draft_threshold=draft_threshold,
        rss_feeds=rss_feeds,
    )


def get_authority_settings(
    *,
    enabled: bool | None = None,
    triage_threshold: float | None = None,
    candidate_threshold: float | None = None,
    exa_results_per_lane: int | None = None,
    max_packets_per_run: int | None = None,
    freshness_days: int | None = None,
    configured_repositories: tuple[str, ...] | None = None,
) -> AuthoritySettings:
    """Resolve authority settings without permitting the locked caps to widen.

    The feature defaults off until containment and credential rotation are
    complete.  Firecrawl remains hard-capped at two reads per run and sixty per
    calendar month; neither value is environment-overridable.
    """

    if enabled is None:
        enabled = os.getenv("AUTHORITY_ENGINE_ENABLED", "false").lower() == "true"
    if triage_threshold is None:
        triage_threshold = float(os.getenv("AUTHORITY_TRIAGE_THRESHOLD", "0.45"))
    if candidate_threshold is None:
        candidate_threshold = float(os.getenv("AUTHORITY_CANDIDATE_THRESHOLD", "0.75"))
    if exa_results_per_lane is None:
        exa_results_per_lane = int(os.getenv("AUTHORITY_EXA_RESULTS_PER_LANE", "5"))
    if max_packets_per_run is None:
        max_packets_per_run = int(os.getenv("AUTHORITY_MAX_PACKETS_PER_RUN", "3"))
    if freshness_days is None:
        freshness_days = int(os.getenv("AUTHORITY_FRESHNESS_DAYS", "30"))
    if configured_repositories is None:
        raw_repos = os.getenv(
            "AUTHORITY_REPOSITORIES",
            "your-github-user/hermes-talk,TheSmokeDev/taskchad-os,your-github-user/geo-skills",
        )
        configured_repositories = tuple(
            dict.fromkeys(repo.strip() for repo in raw_repos.split(",") if repo.strip())
        )
    invalid_repositories = [
        repo for repo in configured_repositories if not _AUTHORITY_REPO_RE.fullmatch(repo)
    ]
    if invalid_repositories:
        raise ValueError("AUTHORITY_REPOSITORIES must contain only owner/name slugs")

    triage = max(0.45, min(float(triage_threshold), 1.0))
    candidate = max(0.75, min(float(candidate_threshold), 1.0))
    if candidate < triage:
        candidate = triage

    return AuthoritySettings(
        enabled=bool(enabled),
        triage_threshold=triage,
        candidate_threshold=candidate,
        exa_results_per_lane=max(1, min(int(exa_results_per_lane), 10)),
        max_packets_per_run=max(1, min(int(max_packets_per_run), 3)),
        freshness_days=max(1, min(int(freshness_days), 90)),
        firecrawl_per_run=2,
        firecrawl_per_month=60,
        configured_repositories=configured_repositories,
    )


# ---------------------------------------------------------------------------
# Default RSS feeds (free, no API key required)
# ---------------------------------------------------------------------------

_DEFAULT_RSS_FEEDS: list[str] = [
    "https://hnrss.org/newest?points=50",
    "https://techcrunch.com/feed/",
    "https://www.insurancejournal.com/rss/news/",
]
