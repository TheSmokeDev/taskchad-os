"""Signal engine configuration — paths, toggles, and call-time resolvers."""

from __future__ import annotations

import os
from typing import NamedTuple

import config as _main_config

# ---------------------------------------------------------------------------
# Path constants (derived from the main config's persona-resolved paths)
# ---------------------------------------------------------------------------

SIGNAL_DIR = _main_config.MEMORY_DIR / "signal"
SIGNAL_STATE_FILE = _main_config.STATE_DIR / "signal-state.json"

# Toggle — default ON (same as example_scout_ENABLED pattern)
SIGNAL_ENABLED = os.getenv("SIGNAL_ENABLED", "true").lower() == "true"


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


# ---------------------------------------------------------------------------
# Default RSS feeds (free, no API key required)
# ---------------------------------------------------------------------------

_DEFAULT_RSS_FEEDS: list[str] = [
    "https://hnrss.org/newest?points=50",
    "https://techcrunch.com/feed/",
    "https://www.insurancejournal.com/rss/news/",
]
