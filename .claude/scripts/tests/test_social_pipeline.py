"""Integration tests for the end-to-end social post pipeline (US-009).

Covers: queue lifecycle, channel registry, capabilities, draft generation,
post dispatch (API + browser mock), cadence scheduler, and audit trail.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from integrations.capabilities import IntegrationPolicyError
from social.channels import SocialChannel, get_channel, list_channels
from social.models import SOCIAL_POST_TRANSITIONS
from social.service import SocialPostService


@pytest.fixture()
def svc(tmp_path: Path) -> SocialPostService:
    return SocialPostService(db_path=tmp_path / "test.db")


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
                "execution_method": "manual",
                "cadence_enabled": False,
                "cadence_interval_hours": 12,

                "voice_profile": "",
                "topic_pool": ["hot takes"],
                "browser_workflow_id": "x.post.create",
            },
        }
    }
    p = tmp_path / "channels.yaml"
    with open(p, "w") as f:
        yaml.dump(data, f)
    return p


class TestFullLinkedInLoop:
    """End-to-end: create draft -> approve -> dispatch (mocked browser) -> verify posted + audit."""

    def test_linkedin_loop_happy_path(self, svc: SocialPostService, tmp_path: Path):
        pid = svc.create_draft(
            channel="linkedin",
            title="AI employees are the future",
            body="Every small business deserves a superhuman employee. Here's why.",
            voice_profile="YourProduct",
            topic_source="manual",
        )
        assert svc.get_post(pid).status == "draft"

        post = svc.approve_post(pid)
        assert post.status == "approved"
        assert post.approved_at is not None

        post = svc.mark_posted(pid, post_url="https://linkedin.com/post/123")
        assert post.status == "posted"
        assert post.post_url == "https://linkedin.com/post/123"
        assert post.posted_at is not None

    def test_linkedin_loop_with_audit(self, svc: SocialPostService, tmp_path: Path):
        from social.audit import append_social_audit_record

        audit_path = tmp_path / "audit.jsonl"
        pid = svc.create_draft(
            channel="linkedin", title="Test", body="Content",
        )
        append_social_audit_record(
            channel="linkedin", action="draft", post_id=pid,
            outcome="created", body_preview="Content", audit_path=audit_path,
        )
        svc.approve_post(pid)
        append_social_audit_record(
            channel="linkedin", action="approve", post_id=pid,
            outcome="approved", operator="operator", audit_path=audit_path,
        )
        svc.mark_posted(pid)
        append_social_audit_record(
            channel="linkedin", action="post", post_id=pid,
            outcome="success", audit_path=audit_path,
        )

        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) == 3
        actions = [json.loads(l)["action"] for l in lines]
        assert actions == ["draft", "approve", "post"]


class TestFullFacebookLoop:
    """End-to-end: create draft -> approve -> dispatch (mocked API) -> verify posted."""

    def test_facebook_api_dispatch(self, svc: SocialPostService, tmp_path: Path):
        pid = svc.create_draft(
            channel="facebook", title="FB Post", body="Check out our new service!",
        )
        svc.approve_post(pid)

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.post_url = "https://facebook.com/123"
        mock_result.message = "Posted"

        with patch("social.post_executor.get_channel") as mock_ch:
            mock_ch.return_value = SocialChannel(
                channel_id="facebook",
                display_name="Facebook",
                execution_method="api",
            )
            with patch("integrations.social_media.post_to_platform", return_value=mock_result):
                from social.post_executor import dispatch_post
                ok = dispatch_post(pid, db_path=svc._db._db_path)

        assert ok is True
        post = svc.get_post(pid)
        assert post.status == "posted"
        assert post.post_url == "https://facebook.com/123"

    def test_facebook_api_failure(self, svc: SocialPostService, tmp_path: Path):
        pid = svc.create_draft(
            channel="facebook", title="FB Post", body="Content",
        )
        svc.approve_post(pid)

        mock_result = MagicMock()
        mock_result.success = False
        mock_result.post_url = ""
        mock_result.message = "API keys not configured"

        with patch("social.post_executor.get_channel") as mock_ch:
            mock_ch.return_value = SocialChannel(
                channel_id="facebook",
                display_name="Facebook",
                execution_method="api",
            )
            with patch("integrations.social_media.post_to_platform", return_value=mock_result):
                from social.post_executor import dispatch_post
                ok = dispatch_post(pid, db_path=svc._db._db_path)

        assert ok is False
        post = svc.get_post(pid)
        assert post.status == "failed"
        assert "not configured" in post.error


class TestXDraftOnly:
    """X channel: draft-only, never auto-posts."""

    def test_x_manual_dispatch_fails(self, svc: SocialPostService):
        pid = svc.create_draft(channel="x", title="Hot take", body="AI > everything")
        svc.approve_post(pid)

        with patch("social.post_executor.get_channel") as mock_ch:
            mock_ch.return_value = SocialChannel(
                channel_id="x",
                display_name="X (Twitter)",
                execution_method="manual",
            )
            from social.post_executor import dispatch_post
            ok = dispatch_post(pid, db_path=svc._db._db_path)

        assert ok is False
        post = svc.get_post(pid)
        assert post.status == "failed"
        assert "manual" in post.error.lower()

    def test_x_capability_disabled(self):
        from integrations.capabilities import is_integration_action_allowed
        assert not is_integration_action_allowed("social", "post_x")


class TestRejectionFlow:
    def test_reject_with_reason(self, svc: SocialPostService, tmp_path: Path):
        from social.audit import append_social_audit_record

        audit_path = tmp_path / "audit.jsonl"
        pid = svc.create_draft(channel="linkedin", title="Bad post", body="Off brand content")
        svc.reject_post(pid, reason="Off brand")
        append_social_audit_record(
            channel="linkedin", action="reject", post_id=pid,
            outcome="rejected", audit_path=audit_path,
        )

        post = svc.get_post(pid)
        assert post.status == "rejected"
        assert post.rejection_reason == "Off brand"

        record = json.loads(audit_path.read_text().strip())
        assert record["action"] == "reject"


class TestInvalidTransitions:
    def test_cannot_approve_posted(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        svc.approve_post(pid)
        svc.mark_posted(pid)
        with pytest.raises(ValueError, match="Cannot transition"):
            svc.approve_post(pid)

    def test_cannot_post_draft(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        with pytest.raises(ValueError, match="Cannot transition"):
            svc.mark_posted(pid)

    def test_cannot_post_rejected(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        svc.reject_post(pid)
        with pytest.raises(ValueError, match="Cannot transition"):
            svc.mark_posted(pid)


class TestDefaultDeny:
    def test_linkedin_requires_operator(self):
        from integrations.capabilities import is_integration_action_allowed
        assert is_integration_action_allowed(
            "social", "post_linkedin", surface="operator_confirmed"
        )
        assert not is_integration_action_allowed(
            "social", "post_linkedin", surface="model"
        )

    def test_x_completely_disabled(self):
        from integrations.capabilities import is_integration_action_allowed
        assert not is_integration_action_allowed("social", "post_x")
        assert not is_integration_action_allowed(
            "social", "post_x", surface="operator_confirmed"
        )


class TestChannelRegistry:
    def test_add_new_channel_visible(self, tmp_path: Path):
        data = {
            "channels": {
                "tiktok": {
                    "display_name": "TikTok",
                    "execution_method": "manual",
                    "cadence_enabled": False,
                    "cadence_interval_hours": 24,
    
                    "voice_profile": "",
                    "topic_pool": ["trends"],
                    "browser_workflow_id": None,
                },
            }
        }
        p = tmp_path / "channels.yaml"
        with open(p, "w") as f:
            yaml.dump(data, f)
        channels = list_channels(yaml_path=p)
        assert len(channels) == 1
        assert channels[0].channel_id == "tiktok"


class TestCadenceScheduler:
    def test_cadence_tick_disabled(self, monkeypatch):
        monkeypatch.setenv("SOCIAL_CADENCE_ENABLED", "false")
        from social.cadence import run_cadence_tick
        result = run_cadence_tick()
        assert result["drafts_created"] == 0
        assert "disabled" in str(result.get("skipped", ""))

    def test_cadence_tick_skips_not_due(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SOCIAL_CADENCE_ENABLED", "true")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        state = {"last_draft_at:linkedin": now}
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(state))

        with patch("social.channels.list_active_channels") as mock_list:
            mock_list.return_value = [
                SocialChannel(
                    channel_id="linkedin",
                    cadence_enabled=True,
                    cadence_interval_hours=24,
                    topic_pool=["insights"],
                )
            ]
            from social.cadence import run_cadence_tick
            result = run_cadence_tick(
                state_path=state_path,
                db_path=tmp_path / "test.db",
            )

        assert result["drafts_created"] == 0
        assert "linkedin" in result["channels_skipped"]

    def test_cadence_only_creates_drafts(self, tmp_path: Path, monkeypatch):
        """Cadence generates drafts only — never auto-approves or auto-dispatches."""
        monkeypatch.setenv("SOCIAL_CADENCE_ENABLED", "true")

        with patch("social.channels.list_active_channels") as mock_list:
            mock_list.return_value = [
                SocialChannel(
                    channel_id="x",
                    cadence_enabled=True,
                    cadence_interval_hours=1,
                    topic_pool=["takes"],
                )
            ]
            with patch("social.draft_generator.generate_draft", return_value=1):
                from social.cadence import run_cadence_tick

                result = run_cadence_tick(
                    state_path=tmp_path / "state.json",
                    db_path=tmp_path / "test.db",
                )

                assert result["drafts_created"] == 1
                assert result["posts_dispatched"] == 0


class TestDispatchNonApproved:
    def test_dispatch_draft_raises(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        with patch("social.post_executor.get_channel") as mock_ch:
            mock_ch.return_value = SocialChannel(channel_id="linkedin", execution_method="browser")
            from social.post_executor import dispatch_post
            with pytest.raises(ValueError, match="approved"):
                dispatch_post(pid, db_path=svc._db._db_path)

    def test_dispatch_nonexistent_raises(self, svc: SocialPostService):
        from social.post_executor import dispatch_post
        with pytest.raises(ValueError, match="not found"):
            dispatch_post(9999, db_path=svc._db._db_path)


class TestDispatchGateVerification:
    """Verify that every external write goes through require_integration_action."""

    def test_api_dispatch_calls_gate(self, svc: SocialPostService):
        pid = svc.create_draft(channel="facebook", title="T", body="B")
        svc.approve_post(pid)

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.post_url = "https://facebook.com/123"
        mock_result.message = "Posted"

        with patch("social.post_executor.get_channel") as mock_ch, \
             patch("social.post_executor.require_integration_action") as mock_gate, \
             patch("integrations.social_media.post_to_platform", return_value=mock_result):
            mock_ch.return_value = SocialChannel(
                channel_id="facebook", display_name="Facebook", execution_method="api",
            )
            from social.post_executor import dispatch_post
            dispatch_post(pid, db_path=svc._db._db_path)

        mock_gate.assert_called_once_with(
            "social", "post_facebook", surface="operator_confirmed", caller="dispatch_api",
        )

    def test_browser_dispatch_calls_gate(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        svc.approve_post(pid)

        with patch("social.post_executor.get_channel") as mock_ch, \
             patch("social.post_executor.require_integration_action") as mock_gate:
            mock_ch.return_value = SocialChannel(
                channel_id="linkedin", display_name="LinkedIn", execution_method="browser",
                browser_workflow_id="linkedin.post.create",
            )
            mock_gate.side_effect = IntegrationPolicyError("blocked by test")

            from social.post_executor import dispatch_post
            with pytest.raises(IntegrationPolicyError):
                dispatch_post(pid, db_path=svc._db._db_path)

        mock_gate.assert_called_once_with(
            "social", "post_linkedin", surface="operator_confirmed", caller="dispatch_browser",
        )

    def test_gate_blocked_writes_audit(self, svc: SocialPostService, tmp_path: Path):
        pid = svc.create_draft(channel="facebook", title="T", body="B")
        svc.approve_post(pid)

        with patch("social.post_executor.get_channel") as mock_ch, \
             patch("social.post_executor.require_integration_action") as mock_gate, \
             patch("social.post_executor.append_social_audit_record") as mock_audit:
            mock_ch.return_value = SocialChannel(
                channel_id="facebook", display_name="Facebook", execution_method="api",
            )
            mock_gate.side_effect = IntegrationPolicyError("disabled by policy")

            from social.post_executor import dispatch_post
            with pytest.raises(IntegrationPolicyError):
                dispatch_post(pid, db_path=svc._db._db_path)

        audit_calls = mock_audit.call_args_list
        assert any(c.kwargs.get("outcome") == "blocked" for c in audit_calls)


class TestDispatchDuePosts:
    """Test the dispatch_due_posts batch function."""

    def test_dispatches_scheduled_posts(self, svc: SocialPostService):
        pid1 = svc.create_draft(
            channel="facebook", title="Post 1", body="Body 1",
            scheduled_for="2020-01-01T00:00:00",
        )
        svc.approve_post(pid1)

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.post_url = "https://facebook.com/1"
        mock_result.message = "OK"

        with patch("social.post_executor.get_channel") as mock_ch, \
             patch("social.post_executor.require_integration_action"), \
             patch("integrations.social_media.post_to_platform", return_value=mock_result):
            mock_ch.return_value = SocialChannel(
                channel_id="facebook", display_name="Facebook", execution_method="api",
            )
            from social.post_executor import dispatch_due_posts
            result = dispatch_due_posts(db_path=svc._db._db_path)

        assert result["dispatched"] == 1
        assert result["failed"] == 0

    def test_skips_unscheduled_approved(self, svc: SocialPostService):
        pid = svc.create_draft(channel="facebook", title="T", body="B")
        svc.approve_post(pid)

        from social.post_executor import dispatch_due_posts
        result = dispatch_due_posts(db_path=svc._db._db_path)

        assert result["dispatched"] == 0

    def test_skips_future_scheduled(self, svc: SocialPostService):
        pid = svc.create_draft(
            channel="facebook", title="T", body="B",
            scheduled_for="2099-12-31T23:59:59",
        )
        svc.approve_post(pid)

        from social.post_executor import dispatch_due_posts
        result = dispatch_due_posts(db_path=svc._db._db_path)

        assert result["dispatched"] == 0


class TestPreSendAuditRecord:
    """Verify audit is written BEFORE the external call, not after."""

    def test_api_writes_pending_audit_before_post(self, svc: SocialPostService):
        pid = svc.create_draft(channel="facebook", title="T", body="B")
        svc.approve_post(pid)

        call_order = []
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.post_url = "https://facebook.com/1"
        mock_result.message = "OK"

        def fake_post(*a, **kw):
            call_order.append("post")
            return mock_result

        def fake_audit(**kw):
            call_order.append(f"audit:{kw.get('outcome', '?')}")
            return "id"

        with patch("social.post_executor.get_channel") as mock_ch, \
             patch("social.post_executor.require_integration_action"), \
             patch("social.post_executor.append_social_audit_record", side_effect=fake_audit), \
             patch("integrations.social_media.post_to_platform", side_effect=fake_post):
            mock_ch.return_value = SocialChannel(
                channel_id="facebook", display_name="Facebook", execution_method="api",
            )
            from social.post_executor import dispatch_post
            dispatch_post(pid, db_path=svc._db._db_path)

        assert call_order[0] == "audit:pending"
        assert "post" in call_order
        post_idx = call_order.index("post")
        assert post_idx > 0

    def test_browser_writes_pending_audit_before_dispatch(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        svc.approve_post(pid)

        call_order = []

        mock_receipt = MagicMock()
        mock_receipt.status = "completed"
        mock_receipt.metadata = {"post_url": "https://linkedin.com/post/1"}
        mock_receipt.error = None

        def fake_executor_dispatch(subtask):
            call_order.append("browser_dispatch")
            return mock_receipt

        def fake_audit(**kw):
            call_order.append(f"audit:{kw.get('outcome', '?')}")
            return "id"

        mock_driver_mod = MagicMock()
        mock_be_cls = MagicMock()
        mock_be_instance = MagicMock()
        mock_be_instance.dispatch = fake_executor_dispatch
        mock_be_cls.return_value = mock_be_instance

        import sys
        with patch("social.post_executor.get_channel") as mock_ch, \
             patch("social.post_executor.require_integration_action"), \
             patch("social.post_executor.append_social_audit_record", side_effect=fake_audit), \
             patch.dict(sys.modules, {"chat": MagicMock(), "chat.social_write_driver": mock_driver_mod}), \
             patch("orchestration.browser_executor.BrowserExecutor", mock_be_cls):
            mock_ch.return_value = SocialChannel(
                channel_id="linkedin", display_name="LinkedIn", execution_method="browser",
                browser_workflow_id="linkedin.post.create",
            )
            from social.post_executor import dispatch_post
            dispatch_post(pid, db_path=svc._db._db_path)

        assert call_order[0] == "audit:pending"
        assert "browser_dispatch" in call_order
        dispatch_idx = call_order.index("browser_dispatch")
        assert dispatch_idx > 0


class TestDispatchDueBlockedCount:
    """Verify dispatch_due_posts separately counts gate-blocked posts."""

    def test_blocked_posts_counted_separately(self, svc: SocialPostService):
        pid = svc.create_draft(
            channel="facebook", title="T", body="B",
            scheduled_for="2020-01-01T00:00:00",
        )
        svc.approve_post(pid)

        with patch("social.post_executor.get_channel") as mock_ch, \
             patch("social.post_executor.require_integration_action") as mock_gate:
            mock_ch.return_value = SocialChannel(
                channel_id="facebook", display_name="Facebook", execution_method="api",
            )
            mock_gate.side_effect = IntegrationPolicyError("disabled")

            from social.post_executor import dispatch_due_posts
            result = dispatch_due_posts(db_path=svc._db._db_path)

        assert result["blocked"] == 1
        assert result["dispatched"] == 0
        assert result["failed"] == 0


class TestDirectAPIPath:
    """Prove FB/IG API paths make real REST calls (mocked at requests level, not post_to_platform)."""

    def test_facebook_api_posts_to_graph_api(self, monkeypatch):
        from integrations.social_media import post_to_facebook

        monkeypatch.setenv("FACEBOOK_PAGE_ID", "123456")
        monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "tok_test")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "123456_789"}
        mock_resp.raise_for_status = MagicMock()

        with patch("integrations.social_media.requests.post", return_value=mock_resp) as mock_post:
            result = post_to_facebook("Hello from YourProduct")

        assert result.success is True
        assert "123456_789" in result.post_url
        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        assert "graph.facebook.com" in call_url
        assert "123456" in call_url

    def test_facebook_api_missing_token(self, monkeypatch):
        from integrations.social_media import post_to_facebook

        monkeypatch.delenv("FACEBOOK_PAGE_ID", raising=False)
        monkeypatch.delenv("FACEBOOK_PAGE_ACCESS_TOKEN", raising=False)

        result = post_to_facebook("test")
        assert result.success is False
        assert "not configured" in result.message.lower()

    def test_instagram_api_requires_image(self, monkeypatch):
        from integrations.social_media import post_to_instagram

        monkeypatch.setenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "ig_123")
        monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "tok_test")

        result = post_to_instagram("Caption only, no image")
        assert result.success is False
        assert "image" in result.message.lower()

    def test_instagram_api_two_step_publish(self, monkeypatch):
        from integrations.social_media import post_to_instagram

        monkeypatch.setenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "ig_123")
        monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "tok_test")

        container_resp = MagicMock()
        container_resp.status_code = 200
        container_resp.json.return_value = {"id": "container_456"}
        container_resp.raise_for_status = MagicMock()

        publish_resp = MagicMock()
        publish_resp.status_code = 200
        publish_resp.json.return_value = {"id": "media_789"}
        publish_resp.raise_for_status = MagicMock()

        with patch(
            "integrations.social_media.requests.post",
            side_effect=[container_resp, publish_resp],
        ) as mock_post:
            result = post_to_instagram("Caption", image_url="https://example.com/img.jpg")

        assert result.success is True
        assert mock_post.call_count == 2
        create_url = mock_post.call_args_list[0][0][0]
        publish_url = mock_post.call_args_list[1][0][0]
        assert "media" in create_url
        assert "media_publish" in publish_url

    def test_post_to_platform_routes_to_facebook(self, monkeypatch):
        from integrations.social_media import post_to_platform

        monkeypatch.setenv("FACEBOOK_PAGE_ID", "pg_1")
        monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "tok")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "pg_1_post"}
        mock_resp.raise_for_status = MagicMock()

        with patch("integrations.social_media.requests.post", return_value=mock_resp):
            result = post_to_platform("facebook", "test content")

        assert result.success is True
        assert result.platform == "Facebook"
