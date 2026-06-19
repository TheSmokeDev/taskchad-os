"""Business signal engine — daily intelligence digest.

Fetch → triage → research → analyze → synthesize → output (6 stages).
Runs on the heartbeat cadence (daily reflection post-step) or manually from CLI / Telegram.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Boot-shim: must run BEFORE any framework imports
from personas import apply_persona_override

apply_persona_override()

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config import now_local  # noqa: E402
from shared import (  # noqa: E402
    append_to_daily_log,
    file_lock,
    load_state,
    save_state,
)

from business_signal.config import SIGNAL_STATE_FILE, get_signal_settings  # noqa: E402
from business_signal.focus import default_focus  # noqa: E402
from business_signal.models import SignalDigest  # noqa: E402

logger = logging.getLogger(__name__)


async def run_signal_engine(
    test_mode: bool = False,
    days: int = 7,
) -> str:
    """Run the full signal pipeline. Returns 'SIGNAL_SILENT', 'success', or 'failed'."""
    settings = get_signal_settings()
    if not settings.enabled:
        logger.info("Signal engine disabled (SIGNAL_ENABLED=false)")
        return "disabled"

    with file_lock(SIGNAL_STATE_FILE, timeout=10.0):
        return await _run_pipeline(test_mode=test_mode, days=days)


async def _run_pipeline(test_mode: bool, days: int) -> str:
    """Core pipeline — called under file lock."""
    from business_signal.fetchers import default_registry
    from business_signal.research import research_items
    from business_signal.triage import triage_items

    settings = get_signal_settings()
    focus = default_focus()
    now_iso = datetime.now(timezone.utc).isoformat()

    # Stage 1: Fetch
    print(f"[{now_local()}] Signal: fetching from all sources...")
    registry = default_registry()
    all_items, sources_checked, sources_failed = await registry.fetch_all()
    total_fetched = len(all_items)
    print(f"[{now_local()}] Signal: {total_fetched} items from {sources_checked} sources ({sources_failed} failed)")

    # Stage 2: Triage
    triaged = triage_items(all_items, focus, threshold=settings.triage_threshold)
    triaged = triaged[: settings.max_items_per_run]
    print(f"[{now_local()}] Signal: {len(triaged)} items passed triage (threshold={settings.triage_threshold})")

    # SIGNAL_SILENT — zero LLM cost
    if not triaged:
        _save_run_state(now_iso, "silent", 0, 0)
        if not test_mode:
            append_to_daily_log("No relevant signal found this cycle.", "Signal Digest")
        print(f"[{now_local()}] SIGNAL_SILENT")
        return "SIGNAL_SILENT"

    # Stage 3: Research (content enrichment)
    if not test_mode:
        try:
            triaged = await research_items(triaged)
            print(f"[{now_local()}] Signal: research enrichment complete")
        except Exception as exc:
            print(f"[{now_local()}] Signal: research failed (non-fatal): {exc}")

    # Stage 4: Analysis (LLM)
    if not test_mode:
        try:
            from business_signal.analyze import analyze_items
            triaged = await analyze_items(triaged, focus)
            print(f"[{now_local()}] Signal: analysis complete")
        except Exception as exc:
            print(f"[{now_local()}] Signal: analysis failed (non-fatal): {exc}")

    # Stage 5: Synthesis (LLM)
    digest = SignalDigest(
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        items=triaged,
        sources_checked=sources_checked,
        sources_failed=sources_failed,
        total_fetched=total_fetched,
        total_triaged=len(triaged),
    )

    if not test_mode:
        try:
            from business_signal.synthesize import synthesize_digest
            digest = await synthesize_digest(
                triaged,
                digest_date=digest.date,
                sources_checked=sources_checked,
                sources_failed=sources_failed,
                total_fetched=total_fetched,
            )
            print(f"[{now_local()}] Signal: synthesis complete")
        except Exception as exc:
            print(f"[{now_local()}] Signal: synthesis failed (non-fatal): {exc}")

    # Stage 6: Output
    drafts_count = 0
    if not test_mode:
        try:
            from business_signal.output import write_signal_output
            result = await write_signal_output(digest)
            drafts_count = len(result.get("drafts_created", []))
            print(f"[{now_local()}] Signal: output written — digest={result.get('digest_path')}, drafts={drafts_count}")
        except Exception as exc:
            print(f"[{now_local()}] Signal: output failed: {exc}")
            _save_run_state(now_iso, "failed", len(triaged), 0)
            return "failed"
    else:
        print(f"[{now_local()}] Signal (test mode): would write {len(triaged)} items")

    _save_run_state(now_iso, "success", len(triaged), drafts_count)
    return "success"


def _save_run_state(
    run_time: str, result: str, items_count: int, drafts_count: int
) -> None:
    """Persist run state for /signal status display."""
    state = load_state(SIGNAL_STATE_FILE)
    state["last_run"] = run_time
    state["last_result"] = result
    state["items_count"] = items_count
    state["drafts_count"] = drafts_count
    save_state(state, SIGNAL_STATE_FILE)


def get_latest_status() -> str:
    """Return a human-readable status summary for the /signal command."""
    state = load_state(SIGNAL_STATE_FILE)
    if not state.get("last_run"):
        return "Signal engine has not run yet."

    last_run = state.get("last_run", "unknown")
    last_result = state.get("last_result", "unknown")
    items = state.get("items_count", 0)
    drafts = state.get("drafts_count", 0)

    return (
        f"*Signal Engine Status*\n"
        f"  Last run: {last_run}\n"
        f"  Result: {last_result}\n"
        f"  Items triaged: {items}\n"
        f"  Drafts created: {drafts}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Business signal engine — daily intelligence digest")
    parser.add_argument("--test", action="store_true", help="Dry run (no file writes, no LLM calls)")
    parser.add_argument("--days", type=int, default=7, help="Lookback days for dedup window")
    args = parser.parse_args()

    result = asyncio.run(run_signal_engine(test_mode=args.test, days=args.days))
    print(f"\nResult: {result}")


if __name__ == "__main__":
    main()
