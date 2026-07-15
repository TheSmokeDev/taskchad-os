"""GitHub signal engine — weekly starred-backlog resurfacing + trending digest.

Pipeline: fetch starred inventory → split new-vs-backlog by starred_at
watermark → eligibility filter → contextual picks (one background-tier LLM
call) → trending garnish → deterministic digest + Telegram card → state merge.

Run: cd .claude/scripts && uv run python -m github_signal.engine [--test]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# Boot-shim: must run BEFORE any framework imports
from personas import apply_persona_override

apply_persona_override()

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config import now_local  # noqa: E402

from github_signal import state as state_mod  # noqa: E402
from github_signal.config import get_github_signal_settings  # noqa: E402
from github_signal.fetch import FetchError, fetch_starred  # noqa: E402
from github_signal.output import append_log, notify, write_output  # noqa: E402
from github_signal.picks import pick_backlog  # noqa: E402
from github_signal.trending import fetch_trending, filter_by_keywords  # noqa: E402


def _iso_week(today: date | None = None) -> str:
    d = today or date.today()
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _gather_trending(keywords: list[str], persist: bool = True) -> list[dict]:
    """Fetch → keyword filter → Watermark dedup. Returns [] on any failure."""
    items = filter_by_keywords(fetch_trending(), keywords)
    watchers_dir = _SCRIPTS_DIR / "watchers"
    if str(watchers_dir) not in sys.path:
        sys.path.insert(0, str(watchers_dir))
    from _watermark import Watermark

    wm = Watermark.load("github-trending")
    new_items = wm.filter_new(items, id_key="full_name")
    if persist:
        wm.save()
    return new_items


async def run_github_signal(test_mode: bool = False) -> str:
    """Run the pipeline. Returns 'disabled', 'GITHUB_SIGNAL_SILENT',
    'success', or 'failed'."""
    settings = get_github_signal_settings()
    if not settings.enabled:
        print(f"[{now_local()}] GitHub signal disabled (GITHUB_SIGNAL_ENABLED=false)")
        return "disabled"

    now_iso = datetime.now(timezone.utc).isoformat()
    state = state_mod.load()
    watermark = state.get("starred_watermark") or ""

    # Stage 1: starred inventory (fatal on failure — watermark untouched)
    print(f"[{now_local()}] GitHub signal: fetching starred inventory...")
    try:
        inventory = fetch_starred()
    except FetchError as exc:
        print(f"[{now_local()}] GitHub signal: fetch failed: {exc}")
        if not test_mode:
            state_mod.finalize_run(result="failed", run_time=now_iso)
        return "failed"

    inventory_names = {i["full_name"] for i in inventory}
    starred_ats = [i["starred_at"] for i in inventory if i.get("starred_at")]
    # ISO-8601 Z timestamps of constant shape compare correctly as strings.
    new_watermark = max(starred_ats) if starred_ats else watermark
    new_stars = (
        [i for i in inventory if i.get("starred_at") and i["starred_at"] > watermark]
        if watermark
        else []  # first run: baseline, no replay of all 392 as "new"
    )
    print(
        f"[{now_local()}] GitHub signal: {len(inventory)} starred, "
        f"{len(new_stars)} new since watermark"
    )

    # Stage 2: trending (garnish — never fatal)
    try:
        trending_hits = _gather_trending(
            settings.trending_keywords, persist=not test_mode
        )
    except Exception as exc:
        print(f"[{now_local()}] GitHub signal: trending failed (non-fatal): {exc}")
        trending_hits = []
    trending_hits = trending_hits[:10]

    # Stage 3: eligibility
    eligible = state_mod.eligible_backlog(
        state, inventory, settings.resurface_cooldown_weeks
    )
    print(
        f"[{now_local()}] GitHub signal: {len(eligible)} eligible backlog, "
        f"{len(trending_hits)} trending hits"
    )

    # Silent gate — zero LLM cost, zero ping
    if not new_stars and not eligible and not trending_hits:
        if not test_mode:
            state_mod.finalize_run(
                result="silent",
                watermark=new_watermark or None,
                inventory_names=inventory_names,
                inventory_count=len(inventory),
                new_stars_count=0,
                run_time=now_iso,
            )
        print(f"[{now_local()}] GITHUB_SIGNAL_SILENT")
        return "GITHUB_SIGNAL_SILENT"

    if test_mode:
        print(
            f"[{now_local()}] GitHub signal (test mode): would pick "
            f"{min(settings.pick_count, len(eligible))} of {len(eligible)} eligible, "
            f"write digest {_iso_week()}.md, and notify"
        )
        return "success"

    # Stage 4: contextual picks (one LLM call; deterministic fallback inside)
    picks, used_llm = await pick_backlog(eligible, settings.pick_count)
    by_name = {i["full_name"]: i for i in inventory}
    enriched_picks = [
        {**by_name.get(p["full_name"], {"full_name": p["full_name"]}), **p}
        for p in picks
    ]

    # Stage 5: output (digest is the durable artifact; notify is best-effort)
    digest_data = {
        "week": _iso_week(),
        "date": date.today().isoformat(),
        "new_stars": new_stars,
        "picks": enriched_picks,
        "trending": trending_hits,
        "picks_via_llm": used_llm,
        "inventory_count": len(inventory),
    }
    try:
        digest_path = write_output(digest_data)
    except Exception as exc:
        print(f"[{now_local()}] GitHub signal: digest write failed: {exc}")
        state_mod.finalize_run(result="failed", run_time=now_iso)
        return "failed"
    append_log(digest_data, digest_path)
    tg_sent, dc_sent = notify(digest_data, digest_path)
    print(
        f"[{now_local()}] GitHub signal: digest={digest_path}, "
        f"telegram={'sent' if tg_sent else 'FAILED (non-fatal)'}, "
        f"discord={'sent' if dc_sent else 'off/failed (non-fatal)'}, "
        f"llm={used_llm}"
    )

    # Stage 6: state merge (never downgrades operator used/snoozed)
    state_mod.finalize_run(
        result="success",
        watermark=new_watermark or None,
        inventory_names=inventory_names,
        inventory_count=len(inventory),
        new_stars_count=len(new_stars),
        picked=picks,
        trending=trending_hits,
        run_time=now_iso,
    )

    # Stage 7: scout memory sync (fail-open — persona may not exist)
    try:
        from github_signal.scout_sync import sync_to_scout

        sync_to_scout([digest_path])
    except Exception as exc:
        print(f"[{now_local()}] GitHub signal: scout sync failed (non-fatal): {exc}")
    return "success"


def get_latest_status() -> str:
    """Human-readable status for the /stars command."""
    state = state_mod.load()
    if not state.get("last_run"):
        return (
            "GitHub signal has not run yet.\n"
            "Run now: /stars refresh (or `uv run python -m github_signal.engine`)"
        )
    repos = state.get("repos", {})
    used = sum(1 for e in repos.values() if e.get("status") == "used")
    snoozed = sum(1 for e in repos.values() if e.get("status") == "snoozed")
    lines = [
        "⭐ GitHub Signal Status",
        f"  Last run: {state.get('last_run', 'unknown')}",
        f"  Result: {state.get('last_result', 'unknown')}",
        f"  Starred inventory: {state.get('inventory_count', '?')}",
        f"  New stars last run: {state.get('new_stars_last_run', 0)}",
        f"  Lifecycle: {used} used · {snoozed} snoozed",
    ]
    picks = state.get("last_picks", [])
    if picks:
        lines.append("  Current picks:")
        for pick in picks:
            full = pick.get("full_name", "?")
            entry = repos.get(full, {})
            marker = {"used": " ✅", "snoozed": " 💤"}.get(entry.get("status"), "")
            lines.append(f"    - {full}{marker} — {pick.get('why_now', '')}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GitHub signal — starred backlog resurfacing + trending digest"
    )
    parser.add_argument(
        "--test", action="store_true", help="Dry run (no LLM, no writes, no ping)"
    )
    args = parser.parse_args()
    result = asyncio.run(run_github_signal(test_mode=args.test))
    print(f"\nResult: {result}")


if __name__ == "__main__":
    main()
