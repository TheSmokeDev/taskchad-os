"""Tests for social channel registry (US-002) and capabilities + audit (US-003)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from social.channels import (
    SocialChannel,
    get_channel,
    list_active_channels,
    list_channels,
)
from social.audit import append_social_audit_record


@pytest.fixture()
def yaml_path(tmp_path: Path) -> Path:
    data = {
        "channels": {
            "linkedin": {
                "display_name": "LinkedIn",
                "execution_method": "browser",
                "cadence_enabled": True,
                "cadence_interval_hours": 24,

                "voice_profile": "",
                "topic_pool": ["insights", "updates"],
                "browser_workflow_id": "linkedin.post.create",
            },
            "facebook": {
                "display_name": "Facebook",
                "execution_method": "api",
                "cadence_enabled": False,
                "cadence_interval_hours": 24,

                "voice_profile": "",
                "topic_pool": ["news"],
                "browser_workflow_id": None,
            },
            "x": {
                "display_name": "X (Twitter)",
                "execution_method": "browser",
                "cadence_enabled": False,
                "cadence_interval_hours": 12,

                "voice_profile": "",
                "topic_pool": ["hot takes"],
                "browser_workflow_id": "x.post.create",
                "image_aspect": "16:9",
            },
        }
    }
    p = tmp_path / "channels.yaml"
    with open(p, "w") as f:
        yaml.dump(data, f)
    return p


class TestChannelRegistry:
    def test_list_all_channels(self, yaml_path: Path):
        channels = list_channels(yaml_path=yaml_path)
        assert len(channels) == 3
        ids = {c.channel_id for c in channels}
        assert ids == {"linkedin", "facebook", "x"}

    def test_get_channel_by_id(self, yaml_path: Path):
        ch = get_channel("linkedin", yaml_path=yaml_path)
        assert ch is not None
        assert ch.display_name == "LinkedIn"
        assert ch.execution_method == "browser"
        assert ch.cadence_enabled is True
        assert ch.browser_workflow_id == "linkedin.post.create"

    def test_get_channel_missing(self, yaml_path: Path):
        assert get_channel("tiktok", yaml_path=yaml_path) is None

    def test_list_active_channels(self, yaml_path: Path):
        active = list_active_channels(yaml_path=yaml_path)
        assert len(active) == 1
        assert active[0].channel_id == "linkedin"

    def test_x_is_browser_driven(self, yaml_path: Path):
        ch = get_channel("x", yaml_path=yaml_path)
        assert ch is not None
        assert ch.execution_method == "browser"
        assert ch.image_aspect == "16:9"

    def test_facebook_is_api(self, yaml_path: Path):
        ch = get_channel("facebook", yaml_path=yaml_path)
        assert ch is not None
        assert ch.execution_method == "api"

    def test_topic_pool_loaded(self, yaml_path: Path):
        ch = get_channel("linkedin", yaml_path=yaml_path)
        assert ch is not None
        assert "insights" in ch.topic_pool
        assert "updates" in ch.topic_pool

    def test_persona_pack_loads_from_yaml(self, tmp_path: Path):
        data = {
            "channels": {
                "instagram": {
                    "display_name": "Instagram",
                    "execution_method": "api",
                    "design_file": "brand_designs/YourBrand.json",
                    "persona_pack": "owner-YourBusiness-rep",
                },
                "reddit": {
                    "display_name": "Reddit",
                    "execution_method": "browser",
                },
            }
        }
        p = tmp_path / "channels.yaml"
        with open(p, "w") as f:
            yaml.dump(data, f)
        ig = get_channel("instagram", yaml_path=p)
        assert ig is not None
        assert ig.persona_pack == "owner-YourBusiness-rep"
        # Absent key defaults to empty string (no persona).
        reddit = get_channel("reddit", yaml_path=p)
        assert reddit is not None
        assert reddit.persona_pack == ""
        assert reddit.image_aspect == "1:1"

    def test_missing_yaml_returns_empty(self, tmp_path: Path):
        missing = tmp_path / "nonexistent.yaml"
        channels = list_channels(yaml_path=missing)
        assert channels == []

    def test_empty_yaml_returns_empty(self, tmp_path: Path):
        empty = tmp_path / "empty.yaml"
        empty.write_text("")
        channels = list_channels(yaml_path=empty)
        assert channels == []


class TestSocialAudit:
    def test_writes_jsonl(self, tmp_path: Path):
        path = tmp_path / "audit.jsonl"
        audit_id = append_social_audit_record(
            channel="linkedin",
            action="draft",
            post_id=1,
            outcome="created",
            body_preview="Hello world this is a test post",
            audit_path=path,
        )
        assert path.exists()
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["channel"] == "linkedin"
        assert record["action"] == "draft"
        assert record["post_id"] == 1
        assert record["outcome"] == "created"
        assert "Hello world" in record["body_preview"]
        assert "linkedin" in audit_id

    def test_appends_multiple(self, tmp_path: Path):
        path = tmp_path / "audit.jsonl"
        append_social_audit_record(
            channel="linkedin", action="draft", post_id=1, audit_path=path
        )
        append_social_audit_record(
            channel="linkedin", action="approve", post_id=1, audit_path=path
        )
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_truncates_body_preview(self, tmp_path: Path):
        path = tmp_path / "audit.jsonl"
        long_body = "A" * 200
        append_social_audit_record(
            channel="facebook",
            action="draft",
            post_id=2,
            body_preview=long_body,
            audit_path=path,
        )
        record = json.loads(path.read_text().strip())
        assert len(record["body_preview"]) == 80

    def test_strips_newlines_from_preview(self, tmp_path: Path):
        path = tmp_path / "audit.jsonl"
        append_social_audit_record(
            channel="x",
            action="draft",
            post_id=3,
            body_preview="line1\nline2\nline3",
            audit_path=path,
        )
        record = json.loads(path.read_text().strip())
        assert "\n" not in record["body_preview"]


class TestSocialCapabilities:
    def test_linkedin_post_declared(self):
        from integrations.capabilities import get_integration_action

        action = get_integration_action("social", "post_linkedin")
        assert action is not None
        assert action.effect == "external_post"
        assert "operator_confirmed" in action.exposures

    def test_facebook_post_declared(self):
        from integrations.capabilities import get_integration_action

        action = get_integration_action("social", "post_facebook")
        assert action is not None
        assert action.effect == "external_post"

    def test_x_post_is_operator_confirmed(self):
        from integrations.capabilities import is_integration_action_allowed

        assert is_integration_action_allowed(
            "social", "post_x", surface="operator_confirmed"
        )
        assert not is_integration_action_allowed("social", "post_x", surface="model")

    def test_x_post_requires_operator_surface(self):
        from integrations.capabilities import IntegrationPolicyError, require_integration_action

        action = require_integration_action(
            "social", "post_x", surface="operator_confirmed", caller="test"
        )
        assert action.effect == "external_post"
        with pytest.raises(IntegrationPolicyError, match="not exposed"):
            require_integration_action(
                "social", "post_x", surface="model", caller="test"
            )

    def test_draft_content_allowed_internally(self):
        from integrations.capabilities import is_integration_action_allowed

        assert is_integration_action_allowed(
            "social", "draft_content", surface="internal"
        )

    def test_draft_content_allowed_by_operator(self):
        from integrations.capabilities import is_integration_action_allowed

        assert is_integration_action_allowed(
            "social", "draft_content", surface="operator_confirmed"
        )

    def test_reddit_post_declared(self):
        from integrations.capabilities import get_integration_action

        action = get_integration_action("social", "post_reddit")
        assert action is not None
        assert action.effect == "external_post"

    def test_instagram_post_declared(self):
        from integrations.capabilities import get_integration_action

        action = get_integration_action("social", "post_instagram")
        assert action is not None
