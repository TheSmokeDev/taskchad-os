from __future__ import annotations

from datetime import date

from social.linkedin_growth import (
    LANE_ORDER,
    PILLARS,
    build_growth_packet,
    build_growth_plan,
    parse_network_targets,
)


TARGET_NOTE = """
### GEO / AI-search (owner's authority lane)
- [F] Anchor Person - https://www.linkedin.com/in/anchor-person/
- [C] Peer Person - https://www.linkedin.com/in/peer-person/
- [C] Invalid Missing URL - not-a-url

### AI builders
- [F] Builder One - https://www.linkedin.com/in/builder-one/
"""


def test_parse_network_targets_only_accepts_curated_linkedin_profiles():
    targets = parse_network_targets(TARGET_NOTE)
    assert [target.name for target in targets] == [
        "Anchor Person",
        "Peer Person",
        "Builder One",
    ]
    assert all(target.url.startswith("https://www.linkedin.com/in/") for target in targets)


def test_plan_rotates_pillars_and_network_lanes():
    targets = parse_network_targets(TARGET_NOTE)
    first = build_growth_plan(run_count=0, targets=targets, today=date(2026, 7, 13))
    second = build_growth_plan(run_count=1, targets=targets, today=date(2026, 7, 14))
    assert first["pillar_key"] == PILLARS[0][0]
    assert second["pillar_key"] == PILLARS[1][0]
    assert first["lane"] == LANE_ORDER[0]
    assert second["lane"] == LANE_ORDER[1]
    assert [target.name for target in first["anchors"]] == ["Builder One"]
    assert "headline" in first["profile_task"].lower()
    assert "No profile rewrite" in second["profile_task"]


def test_packet_keeps_outward_writes_default_denied():
    plan = build_growth_plan(
        run_count=2,
        targets=parse_network_targets(TARGET_NOTE),
        today=date(2026, 7, 15),
    )
    packet = build_growth_packet(plan)
    assert "image-node-factory" in packet
    assert "social-content-factory" in packet
    assert "Every post, comment, and connection remains individually operator-approved" in packet
    assert "never sends it" in packet
    assert "autonomous DMs" in packet
