"""Business signal engine — daily intelligence digest on the heartbeat cadence.

Fetches external data (RSS, HARO, web), triages against a business-focus
profile, runs LLM analysis/synthesis on the fast tier, and writes a daily
digest + content drafts to the vault.
"""

from business_signal.config import (
    SIGNAL_DIR,
    SIGNAL_ENABLED,
    SIGNAL_STATE_FILE,
    get_signal_settings,
)
from business_signal.fetchers import BaseFetcher, FetcherRegistry, default_registry
from business_signal.focus import ChannelFocus, default_focus
from business_signal.models import SignalDigest, SignalItem

__all__ = [
    # Config
    "SIGNAL_DIR",
    "SIGNAL_ENABLED",
    "SIGNAL_STATE_FILE",
    "get_signal_settings",
    # Fetchers
    "BaseFetcher",
    "FetcherRegistry",
    "default_registry",
    # Focus
    "ChannelFocus",
    "default_focus",
    # Models
    "SignalDigest",
    "SignalItem",
]
