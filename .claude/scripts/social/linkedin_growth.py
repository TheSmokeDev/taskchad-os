"""Draft-only LinkedIn authority and network-growth packet.

This module never opens a browser and never performs a LinkedIn write. It turns
the operator's private, curated network-target note into a small daily action
packet: one authority pillar, anchor accounts to comment on, peers to warm
before connecting, a profile-proof task, and a visual brief for the existing
Archon image-node factory.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable


PILLARS: tuple[tuple[str, str, str], ...] = (
    (
        "operator_builder",
        "Operator-builder",
        "Show how building and operating the system together changed a real business decision.",
    ),
    (
        "ai_systems",
        "Practical AI systems",
        "Teach one concrete way an AI employee removes a business bottleneck. Lead with the outcome, not tools.",
    ),
    (
        "sales",
        "Sales systems and closing",
        "Explain one lesson about speed-to-lead, follow-up, qualification, objections, or closing from lived operator experience.",
    ),
    (
        "geo",
        "GEO and AI search",
        "Teach one answer-first, citation, entity, or AI-search tactic a business can apply this week.",
    ),
    (
        "ownership",
        "Entrepreneurship and ownership",
        "Make the case for owning the pipeline, data, distribution, or automation instead of renting the critical valve.",
    ),
    (
        "build_receipt",
        "Build-in-public receipt",
        "Use one shipped, fixed, or verified artifact to demonstrate the lesson without calling owner an expert.",
    ),
)

LANE_ORDER: tuple[str, ...] = (
    "AI builders",
    "Speed-to-lead / AI-for-SMB",
    "GEO / AI-search (owner's authority lane)",
    "AI voice (GitHub/X is the real network here)",
    "Insurtech (domain credibility; never name Freeway)",
)

_HEADING_RE = re.compile(r"^###\s+(.+?)\s*$")
_TARGET_RE = re.compile(
    r"^- \[(?P<action>[FC])\]\s+(?P<name>.+?)\s+-\s+"
    r"(?P<url>https://www\.linkedin\.com/in/[^\s)]+)"
)


@dataclass(frozen=True)
class NetworkTarget:
    lane: str
    action: str
    name: str
    url: str


def parse_network_targets(text: str) -> list[NetworkTarget]:
    """Parse the private target note without scraping or browser access."""
    lane = ""
    targets: list[NetworkTarget] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading = _HEADING_RE.match(line)
        if heading:
            lane = heading.group(1)
            continue
        match = _TARGET_RE.match(line)
        if not match or not lane:
            continue
        targets.append(
            NetworkTarget(
                lane=lane,
                action=match.group("action"),
                name=match.group("name").strip(),
                url=match.group("url").rstrip("/.") + "/",
            )
        )
    return targets


def load_network_targets(path: Path) -> list[NetworkTarget]:
    if not path.is_file():
        return []
    return parse_network_targets(path.read_text(encoding="utf-8"))


def _rotate(items: list[NetworkTarget], offset: int, limit: int) -> list[NetworkTarget]:
    if not items:
        return []
    start = offset % len(items)
    rotated = items[start:] + items[:start]
    return rotated[:limit]


def build_growth_plan(
    *,
    run_count: int,
    targets: Iterable[NetworkTarget],
    today: date,
) -> dict:
    pillar_key, pillar_name, pillar_brief = PILLARS[run_count % len(PILLARS)]
    lane = LANE_ORDER[run_count % len(LANE_ORDER)]
    lane_targets = [target for target in targets if target.lane == lane]
    anchors = _rotate(
        [target for target in lane_targets if target.action == "F"], run_count, 4
    )
    peers = _rotate(
        [target for target in lane_targets if target.action == "C"], run_count, 3
    )
    profile_task = (
        "Review the headline, About, Featured, and newest Experience entry for one "
        "fresh public proof artifact. Draft changes only; profile edits remain operator-approved."
        if today.weekday() == 0
        else "No profile rewrite today. Keep the visible proof current through posts and comments."
    )
    return {
        "date": today.isoformat(),
        "pillar_key": pillar_key,
        "pillar_name": pillar_name,
        "pillar_brief": pillar_brief,
        "lane": lane,
        "anchors": anchors,
        "peers": peers,
        "profile_task": profile_task,
    }


def _target_lines(targets: Iterable[NetworkTarget], empty: str) -> str:
    rows = [f"- [{target.name}]({target.url})" for target in targets]
    return "\n".join(rows) if rows else f"- {empty}"


def build_growth_packet(plan: dict) -> str:
    anchors = _target_lines(
        plan["anchors"], "No curated anchor in this lane. Use the lane name to find one manually."
    )
    peers = _target_lines(
        plan["peers"], "No curated peer in this lane. Warm a relevant peer manually before connecting."
    )
    visual_brief = (
        f"LinkedIn 4:5 editorial visual for the {plan['pillar_name']} pillar. "
        "Founder-operator authority, technical depth, human and premium, generous "
        "negative space, no baked text, no logos, no invented claims."
    )
    return f"""---
type: linkedin-growth-packet
date: {plan['date']}
pillar: "{plan['pillar_name']}"
lane: "{plan['lane']}"
tags: [social, linkedin, draft]
---

# LinkedIn growth packet - {plan['date']}

## Authority pillar

**{plan['pillar_name']}**

{plan['pillar_brief']}

Use a real shipped, fixed, sold, or verified experience. Do not claim expertise; demonstrate it.

## Comment-first network lane

**{plan['lane']}**

Read the post before writing anything. Leave a substantive comment that adds one specific point, example, disagreement, or question. No praise-only comments and no automated posting.

{anchors}

## Warm-before-connect peers

Comment first. A connection request remains one approval for one profile; this packet never sends it.

{peers}

## Profile proof task

{plan['profile_task']}

## Visual factory brief

{visual_brief}

Prompt-pack route:

`archon workflow run image-node-factory "{visual_brief} category=Posters & Typography render_mode=overlay aspect=4:5 count=3 render=false design_file=.claude/scripts/social/brand_designs/owner-linkedin.json persona_pack=.claude/image-personas/owner-YourBusiness-rep subject_mode=placeholder"`

The scheduled social cadence uses the same design and persona bindings to generate a reviewable LinkedIn image through `social-content-factory`. No image or post bypasses the approval queue.

## Daily scorecard

- Substantive comments posted: ___
- Replies received: ___
- Warm connection requests approved: ___
- New accepted connections: ___
- Profile views: ___
- Post saves, comments, and qualified DMs: ___

## Safety contract

- Draft and research only. No browser actions run from this packet.
- No invite blasting, engagement pods, copied comments, autonomous DMs, or profile edits.
- Every post, comment, and connection remains individually operator-approved.
"""


def run_growth_packet(
    *,
    dry_run: bool = False,
    target_note: Path | None = None,
    toast: bool = False,
) -> str:
    import config
    from shared import (
        append_to_daily_log,
        file_lock,
        load_state,
        regenerate_lane_index,
        save_state,
    )

    state_path = config.STATE_DIR / "linkedin-growth-state.json"
    note_path = target_note or config.MEMORY_DIR / "docs" / "LINKEDIN-NETWORK-TARGETS.md"

    # Lock only the run_count read (and the increment below) — packet build,
    # file write, and index regen don't touch the state file, and holding the
    # 5s-timeout lock across them makes an overlapping manual+scheduled run
    # fail with TimeoutError instead of proceeding.
    with file_lock(state_path, timeout=5.0):
        state = load_state(state_path)
        run_count = int(state.get("run_count", 0))

    today = config.now_local().date()
    plan = build_growth_plan(
        run_count=run_count,
        targets=load_network_targets(note_path),
        today=today,
    )
    packet = build_growth_packet(plan)

    if dry_run:
        print(packet)
        return packet

    output_dir = config.DRAFTS_DIR / "linkedin-growth"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{today.isoformat()}.md"
    output_path.write_text(packet, encoding="utf-8")

    # Lane index — the inbound edge that keeps daily packets out of the
    # orphan pile. Fail-open: an index failure never blocks the packet run.
    try:
        regenerate_lane_index(
            lane_dir=output_dir,
            index_name="LINKEDIN-GROWTH-INDEX.md",
            title="LinkedIn Growth — Lane Index",
            description=(
                "Auto-generated index of draft-only LinkedIn growth packets; "
                "one row per day."
            ),
            sections=[
                {
                    "heading": "Daily packets",
                    "glob": "[0-9]*.md",
                    "columns": [
                        ("Pillar", "pillar"),
                        ("Lane", "lane"),
                    ],
                }
            ],
        )
    except Exception as exc:
        print(
            f"[{config.now_local().isoformat()}] Lane index regen failed "
            f"for LINKEDIN-GROWTH-INDEX.md: {exc}"
        )

    # Second short lock window: re-read before increment so an overlapping
    # run's bump is never lost (read-modify-write stays atomic per window).
    with file_lock(state_path, timeout=5.0):
        state = load_state(state_path)
        state.update(
            {
                "run_count": int(state.get("run_count", 0)) + 1,
                "last_run": config.now_local().isoformat(),
                "last_pillar": plan["pillar_key"],
                "last_lane": plan["lane"],
            }
        )
        save_state(state, state_path)

    if toast:
        from notifications import send_toast_notification

        send_toast_notification(
            f"LinkedIn growth packet: {plan['pillar_name']}",
            f"Comment targets, connection peers, and visual brief are ready at {output_path.name}.",
            caller="linkedin_growth",
        )
    append_to_daily_log(
        f"LinkedIn growth packet queued ({plan['pillar_name']} / {plan['lane']}) -> {output_path}",
        "LinkedIn Growth",
    )
    return packet


def main() -> None:
    parser = argparse.ArgumentParser(description="Draft-only LinkedIn growth packet")
    parser.add_argument("--dry-run", action="store_true", help="Print only; no files, state, or toast")
    parser.add_argument("--target-note", type=Path, default=None)
    parser.add_argument("--toast", action="store_true", help="Also show a local desktop toast")
    args = parser.parse_args()
    run_growth_packet(dry_run=args.dry_run, target_note=args.target_note, toast=args.toast)


if __name__ == "__main__":
    main()
