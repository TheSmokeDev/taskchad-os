"""Output stage — dated weekly vault digest, daily-log append, Telegram card.

Deterministic Python renders the entire digest; the LLM only supplied the
picks JSON upstream. Dated files (github-signal/YYYY-WNN.md) because picks
have a lifecycle — an overwrite-style digest would orphan the audit trail.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from shared import append_to_daily_log, regenerate_lane_index  # noqa: E402

from github_signal.config import (  # noqa: E402
    GITHUB_SIGNAL_DIR,
    get_github_signal_settings,
)


def digest_path_for_week(week: str) -> Path:
    return GITHUB_SIGNAL_DIR / f"{week}.md"


def regenerate_github_signal_index(lane_dir: Path | None = None) -> Path | None:
    """Regenerate the lane index (digests + evals). Single config owner.

    Called after every digest AND eval write so both note families keep an
    inbound edge. Callers wrap in try/except — an index failure never blocks
    the pipeline. ``lane_dir`` is a None sentinel resolved at call time so
    test-monkeypatched module dirs thread through.
    """
    if lane_dir is None:
        lane_dir = GITHUB_SIGNAL_DIR
    return regenerate_lane_index(
        lane_dir=lane_dir,
        index_name="GITHUB-SIGNAL-INDEX.md",
        title="GitHub Signal — Lane Index",
        description="Auto-generated index of github-signal weekly digests and repo evals.",
        sections=[
            {
                "heading": "Weekly digests",
                "glob": "[0-9]*.md",
                "columns": [
                    ("New stars", "new_stars"),
                    ("Picks", "picks"),
                    ("Trending", "trending_hits"),
                ],
            },
            {
                "heading": "Repo evals",
                "glob": "*.md",
                "subdir": "evals",
                "columns": [
                    ("Repo", "repo"),
                    ("Recommendation", "recommendation"),
                    ("Disposition", "disposition"),
                ],
            },
        ],
    )


def write_output(data: dict[str, Any]) -> Path:
    """Render and write the weekly digest markdown. Returns the path."""
    week = data["week"]
    new_stars = data.get("new_stars", [])
    picks = data.get("picks", [])
    trending = data.get("trending", [])

    frontmatter = (
        f"---\n"
        f"tags: [signal, github, auto-generated]\n"
        f"week: {week}\n"
        f"date: {data['date']}\n"
        f"new_stars: {len(new_stars)}\n"
        f"picks: {len(picks)}\n"
        f"trending_hits: {len(trending)}\n"
        f"picks_via_llm: {str(bool(data.get('picks_via_llm'))).lower()}\n"
        f"inventory_count: {data.get('inventory_count', 0)}\n"
        f"---\n\n"
    )

    body = f"# GitHub Signal — {week}\n\n"

    body += "## New stars this week\n\n"
    if new_stars:
        for item in new_stars:
            desc = (item.get("description") or "").strip()
            lang = f" ({item['language']})" if item.get("language") else ""
            body += f"- [{item['full_name']}]({item.get('html_url', '')}) — {desc}{lang}\n"
    else:
        body += "_None since last run._\n"
    body += "\n"

    body += "## Backlog picks — why now\n\n"
    if picks:
        for i, pick in enumerate(picks, 1):
            starred = str(pick.get("starred_at") or "")[:10] or "unknown"
            lang = pick.get("language") or "-"
            desc = (pick.get("description") or "").strip()
            body += (
                f"### {i}. {pick['full_name']}  ·  starred {starred}  ·  {lang}\n\n"
            )
            if desc:
                body += f"{desc}\n\n"
            body += f"**Why now:** {pick.get('why_now', '')}\n\n"
            body += f"{pick.get('html_url', 'https://github.com/' + pick['full_name'])}\n\n"
            body += (
                f"`/stars used {pick['full_name']}` · "
                f"`/stars snooze {pick['full_name']}`\n\n"
            )
    else:
        body += "_No eligible backlog picks this week._\n\n"

    body += "## Trending this week (filtered)\n\n"
    if trending:
        for item in trending:
            stars = item.get("stars", "?")
            desc = (item.get("description") or "").strip()
            lang = f" ({item['language']})" if item.get("language") else ""
            url = f"https://github.com/{item['full_name']}"
            body += f"- [{item['full_name']}]({url}) ★{stars} — {desc}{lang}\n"
    else:
        body += "_No trending hits matched the keyword filter._\n"
    body += "\n"

    path = digest_path_for_week(week)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter + body, encoding="utf-8")
    try:
        regenerate_github_signal_index()
    except Exception:
        pass
    return path


def render_telegram_card(data: dict[str, Any], digest_path: Path) -> str:
    """Compact operator card — plain text, no parse_mode."""
    lines = [
        f"⭐ GitHub Signal — {data['week']}",
        (
            f"{len(data.get('new_stars', []))} new stars · "
            f"{len(data.get('picks', []))} backlog picks · "
            f"{len(data.get('trending', []))} trending"
        ),
        "",
    ]
    picks = data.get("picks", [])
    if picks:
        lines.append("Backlog — why now:")
        for i, pick in enumerate(picks, 1):
            lines.append(f"{i}. {pick['full_name']} — {pick.get('why_now', '')}")
        lines.append("")
    trending = data.get("trending", [])
    if trending:
        tops = ", ".join(
            f"{t['full_name']} ★{t.get('stars', '?')}" for t in trending[:3]
        )
        lines.append(f"Trending: {tops}")
        lines.append("")
    lines.append("Close the loop: /stars used <repo> · /stars snooze <repo>")
    lines.append(f"Full digest: {digest_path}")
    return "\n".join(lines)


def append_log(data: dict[str, Any], digest_path: Path) -> None:
    """One-line receipt in today's daily log. Never raises."""
    try:
        summary = (
            f"GitHub signal {data['week']}: {len(data.get('new_stars', []))} new stars, "
            f"{len(data.get('picks', []))} backlog picks, "
            f"{len(data.get('trending', []))} trending hits — {digest_path.name}"
        )
        append_to_daily_log(summary, "GitHub Signal")
    except Exception:
        pass


def notify(data: dict[str, Any], digest_path: Path) -> tuple[bool, bool]:
    """Send the Telegram card and (when configured) the Discord card.

    Returns ``(telegram_sent, discord_sent)``. Each lane is independently
    fail-open — one failing never blocks the other, and neither ever raises.
    """
    card = render_telegram_card(data, digest_path)
    tg_sent = False
    dc_sent = False
    try:
        from social import notify as social_notify

        tg_sent = social_notify.send_text_to_telegram(card)
    except Exception:
        tg_sent = False
    try:
        channel_id = get_github_signal_settings().discord_channel_id
        if channel_id:
            from social import notify as social_notify

            dc_sent = social_notify.send_text_to_discord(card, channel_id)
    except Exception:
        dc_sent = False
    return tg_sent, dc_sent
