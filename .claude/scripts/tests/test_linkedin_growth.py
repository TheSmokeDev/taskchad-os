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


class TestLaneIndex:
    """The lane index gives every daily packet an inbound edge (orphan cure)."""

    LINKEDIN_SECTIONS = [
        {
            "heading": "Daily packets",
            "glob": "[0-9]*.md",
            "columns": [
                ("Pillar", "pillar"),
                ("Lane", "lane"),
            ],
        }
    ]

    def _regenerate(self, lane_dir):
        from shared import regenerate_lane_index

        return regenerate_lane_index(
            lane_dir=lane_dir,
            index_name="LINKEDIN-GROWTH-INDEX.md",
            title="LinkedIn Growth — Lane Index",
            description="test",
            sections=self.LINKEDIN_SECTIONS,
        )

    def _write_packet(self, lane_dir, day, pillar, lane):
        (lane_dir / f"{day}.md").write_text(
            f'---\ntype: linkedin-growth-packet\ndate: {day}\npillar: "{pillar}"\n'
            f'lane: "{lane}"\ntags: [social, linkedin, draft]\n---\n\n'
            f"# LinkedIn growth packet - {day}\n",
            encoding="utf-8",
        )

    def test_row_per_packet_newest_first(self, tmp_path):
        self._write_packet(tmp_path, "2026-07-01", "Operator-builder", "AI builders")
        self._write_packet(
            tmp_path, "2026-07-08", "Practical AI systems", "Speed-to-lead / AI-for-SMB"
        )
        index = self._regenerate(tmp_path)
        text = index.read_text(encoding="utf-8")
        assert "[[2026-07-08]]" in text
        assert "[[2026-07-01]]" in text
        assert text.index("2026-07-08") < text.index("2026-07-01")
        assert "Practical AI systems" in text and "AI builders" in text
        assert "[[MOC-thehomie]]" in text

    def test_tolerates_packets_missing_frontmatter(self, tmp_path):
        # A legacy packet written before the frontmatter wiring — still indexed.
        (tmp_path / "2026-06-01.md").write_text(
            "# LinkedIn growth packet - 2026-06-01\n", encoding="utf-8"
        )
        index = self._regenerate(tmp_path)
        assert "[[2026-06-01]]" in index.read_text(encoding="utf-8")

    def test_index_excludes_itself_and_is_idempotent(self, tmp_path):
        self._write_packet(tmp_path, "2026-07-01", "Sales systems and closing", "GEO / AI-search")
        first = self._regenerate(tmp_path).read_text(encoding="utf-8")
        second = self._regenerate(tmp_path).read_text(encoding="utf-8")
        assert first == second
        assert "[[LINKEDIN-GROWTH-INDEX]]" not in first
        assert "Daily packets (1)" in first

    def test_missing_lane_dir_returns_none(self, tmp_path):
        assert self._regenerate(tmp_path / "nope") is None

    def test_packet_carries_indexable_frontmatter(self):
        # The wiring: build_growth_packet emits pillar/lane frontmatter so the
        # lane index renders real columns instead of blanks (the lane-index
        # contract: notes carry their own per-run stats).
        plan = build_growth_plan(run_count=0, targets=[], today=date(2026, 7, 15))
        packet = build_growth_packet(plan)
        assert packet.startswith("---\n")
        assert "type: linkedin-growth-packet" in packet
        assert f'pillar: "{plan["pillar_name"]}"' in packet
        assert f'lane: "{plan["lane"]}"' in packet

    def _redirect_config(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setattr(config, "DRAFTS_DIR", tmp_path / "drafts")
        monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "vault")
        # DAILY_DIR is a fixed module constant (MEMORY_DIR / "daily") computed
        # at config.py import time, not re-derived from MEMORY_DIR — it must be
        # redirected separately or run_growth_packet's append_to_daily_log()
        # call writes into the real operator vault during the test.
        monkeypatch.setattr(config, "DAILY_DIR", tmp_path / "vault" / "daily")
        return config

    def test_run_growth_packet_regenerates_lane_index(self, tmp_path, monkeypatch):
        # Closes the gap Review Focus Area #3 flagged: run_growth_packet()
        # itself (not just regenerate_lane_index) must produce a real index.
        self._redirect_config(tmp_path, monkeypatch)
        from social.linkedin_growth import run_growth_packet

        packet = run_growth_packet()

        index_path = tmp_path / "drafts" / "linkedin-growth" / "LINKEDIN-GROWTH-INDEX.md"
        assert index_path.exists()
        index_text = index_path.read_text(encoding="utf-8")
        assert "Daily packets (1)" in index_text
        pillar_line = next(line for line in packet.splitlines() if line.startswith("pillar:"))
        assert pillar_line.split(":", 1)[1].strip().strip('"') in index_text

    def test_index_failure_does_not_block_packet_write(self, tmp_path, monkeypatch):
        import shared

        self._redirect_config(tmp_path, monkeypatch)
        monkeypatch.setattr(
            shared,
            "regenerate_lane_index",
            lambda **_kwargs: (_ for _ in ()).throw(TimeoutError("simulated lock contention")),
        )
        from social.linkedin_growth import run_growth_packet

        packet = run_growth_packet()

        output_dir = tmp_path / "drafts" / "linkedin-growth"
        packet_files = [p for p in output_dir.glob("*.md") if p.name != "LINKEDIN-GROWTH-INDEX.md"]
        assert len(packet_files) == 1
        assert packet_files[0].read_text(encoding="utf-8") == packet
