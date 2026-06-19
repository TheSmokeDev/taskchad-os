"""Helper to import the business_signal package for tests.

Thin re-export layer so test files have a single canonical import site.
The package is ``business_signal`` (NOT ``signal`` — renamed to avoid
stdlib collision).
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from business_signal.models import SignalDigest, SignalItem  # noqa: E402, F401
from business_signal.config import (  # noqa: E402, F401
    SIGNAL_DIR,
    SIGNAL_STATE_FILE,
    SignalSettings,
    get_signal_settings,
)
from business_signal.focus import ChannelFocus, default_focus  # noqa: E402, F401
from business_signal.triage import triage_items  # noqa: E402, F401
from business_signal.fetchers import BaseFetcher, FetcherRegistry  # noqa: E402, F401
from business_signal.fetchers.rss_fetcher import (  # noqa: E402, F401
    RSSFetcher,
    _feed_name,
    _load_seen_urls,
    _parse_feed,
)
from business_signal.output import _create_drafts, _slugify, _write_digest  # noqa: E402, F401
from business_signal.signal_engine import (  # noqa: E402, F401
    _save_run_state,
    get_latest_status,
)
