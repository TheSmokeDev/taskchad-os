"""Business signal engine — daily intelligence digest on the heartbeat cadence.

Fetches external data (RSS, HARO, web), triages against a business-focus
profile, runs LLM analysis/synthesis on the fast tier, and writes a daily
digest + content drafts to the vault.
"""

from business_signal.config import (
    AUTHORITY_FIRECRAWL_LEDGER_FILE,
    AUTHORITY_SIGNAL_DIR,
    AUTHORITY_STATE_FILE,
    SIGNAL_DIR,
    SIGNAL_ENABLED,
    SIGNAL_STATE_FILE,
    get_authority_settings,
    get_signal_settings,
)
from business_signal.fetchers import BaseFetcher, FetcherRegistry, default_registry
from business_signal.focus import AuthorityFocus, ChannelFocus, authority_focus, default_focus
from business_signal.models import AuthoritySignalPacket, SignalDigest, SignalItem

__all__ = [
    # Config
    "SIGNAL_DIR",
    "SIGNAL_ENABLED",
    "SIGNAL_STATE_FILE",
    "get_signal_settings",
    "AUTHORITY_SIGNAL_DIR",
    "AUTHORITY_STATE_FILE",
    "AUTHORITY_FIRECRAWL_LEDGER_FILE",
    "get_authority_settings",
    # Fetchers
    "BaseFetcher",
    "FetcherRegistry",
    "default_registry",
    # Focus
    "ChannelFocus",
    "default_focus",
    "AuthorityFocus",
    "authority_focus",
    # Models
    "SignalDigest",
    "SignalItem",
    "AuthoritySignalPacket",
]
