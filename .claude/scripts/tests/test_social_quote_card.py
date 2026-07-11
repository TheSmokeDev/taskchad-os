"""Tests for the Instagram quote-card pipeline (render -> host -> dispatch).

Covers: card rendering (short + very long body), and the IG dispatch wiring
(card generated + uploaded + image_url passed; failure -> mark_failed with no
post_to_platform call; non-IG channels unchanged).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from social.channels import SocialChannel
from social.quote_card import render_quote_card
from social.service import SocialPostService


@pytest.fixture()
def svc(tmp_path: Path) -> SocialPostService:
    return SocialPostService(db_path=tmp_path / "test.db")


class TestRenderQuoteCard:
    def test_renders_short_body(self, tmp_path: Path):
        out = render_quote_card(
            "Every small business deserves a superhuman employee.",
            title="AI employees",
            out_dir=tmp_path,
        )
        assert out.is_file()
        with Image.open(out) as im:
            assert im.format == "PNG"
            assert im.size == (1080, 1080)

    def test_renders_very_long_body(self, tmp_path: Path):
        body = (
            "Insurance shopping is broken. You call around, you wait on hold, "
            "you repeat the same details five times, and you still are not sure "
            "you got the best rate. YourBusiness compares carriers in one place so "
            "you can stop guessing and start saving. " * 6
        )
        out = render_quote_card(body, out_dir=tmp_path)
        assert out.is_file()
        with Image.open(out) as im:
            assert im.format == "PNG"
            assert im.size == (1080, 1080)

    def test_no_title_renders(self, tmp_path: Path):
        out = render_quote_card("No kicker here.", out_dir=tmp_path)
        assert out.is_file()
        with Image.open(out) as im:
            assert im.size == (1080, 1080)

    def test_filename_is_content_stable(self, tmp_path: Path):
        a = render_quote_card("same body", out_dir=tmp_path)
        b = render_quote_card("same body", out_dir=tmp_path)
        assert a.name == b.name


class TestInstagramDispatchWiring:
    """The IG dispatch path generates a card, uploads it, and passes image_url."""

    def test_ig_path_generates_uploads_and_passes_image_url(
        self, svc: SocialPostService, tmp_path: Path
    ):
        pid = svc.create_draft(
            channel="instagram", title="Hook line", body="Caption body for IG"
        )
        svc.approve_post(pid)

        fake_card = tmp_path / "card-abc.png"
        fake_card.write_bytes(b"\x89PNG\r\n")

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.post_url = "https://instagram.com/p/123"
        mock_result.message = "Posted to feed"

        with patch("social.post_executor.get_channel") as mock_ch, \
             patch("social.post_executor.require_integration_action"), \
             patch("social.quote_card.render_quote_card", return_value=fake_card) as mock_render, \
             patch("social.image_host.upload_public", return_value="https://cdn.example.com/card-abc.png") as mock_upload, \
             patch("integrations.social_media.post_to_platform", return_value=mock_result) as mock_post:
            mock_ch.return_value = SocialChannel(
                channel_id="instagram", display_name="Instagram", execution_method="api",
            )
            from social.post_executor import dispatch_post
            ok = dispatch_post(pid, db_path=svc._db._db_path)

        assert ok is True
        mock_render.assert_called_once()
        # title threaded through to the renderer
        assert mock_render.call_args.kwargs.get("title") == "Hook line"
        mock_upload.assert_called_once_with(fake_card)
        # image_url must be passed to post_to_platform
        mock_post.assert_called_once_with(
            "instagram", "Caption body for IG",
            image_url="https://cdn.example.com/card-abc.png",
            video_url="",
        )
        assert svc.get_post(pid).status == "posted"

    def test_card_render_failure_marks_failed_no_post(
        self, svc: SocialPostService
    ):
        pid = svc.create_draft(channel="instagram", title="T", body="B")
        svc.approve_post(pid)

        with patch("social.post_executor.get_channel") as mock_ch, \
             patch("social.post_executor.require_integration_action"), \
             patch("social.quote_card.render_quote_card", side_effect=RuntimeError("render boom")), \
             patch("integrations.social_media.post_to_platform") as mock_post:
            mock_ch.return_value = SocialChannel(
                channel_id="instagram", display_name="Instagram", execution_method="api",
            )
            from social.post_executor import dispatch_post
            ok = dispatch_post(pid, db_path=svc._db._db_path)

        assert ok is False
        mock_post.assert_not_called()
        post = svc.get_post(pid)
        assert post.status == "failed"
        assert "render boom" in post.error

    def test_upload_failure_marks_failed_no_post(self, svc: SocialPostService, tmp_path: Path):
        pid = svc.create_draft(channel="ig", title="T", body="B")
        svc.approve_post(pid)

        fake_card = tmp_path / "card-x.png"
        fake_card.write_bytes(b"\x89PNG\r\n")

        with patch("social.post_executor.get_channel") as mock_ch, \
             patch("social.post_executor.require_integration_action"), \
             patch("social.quote_card.render_quote_card", return_value=fake_card), \
             patch("social.image_host.upload_public", side_effect=RuntimeError("upload boom")), \
             patch("integrations.social_media.post_to_platform") as mock_post:
            mock_ch.return_value = SocialChannel(
                channel_id="ig", display_name="Instagram", execution_method="api",
            )
            from social.post_executor import dispatch_post
            ok = dispatch_post(pid, db_path=svc._db._db_path)

        assert ok is False
        mock_post.assert_not_called()
        post = svc.get_post(pid)
        assert post.status == "failed"
        assert "upload boom" in post.error

    def test_facebook_path_unchanged_no_card_generated(
        self, svc: SocialPostService
    ):
        """Non-IG channels: no card generated, image_url is empty."""
        pid = svc.create_draft(channel="facebook", title="T", body="FB body")
        svc.approve_post(pid)

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.post_url = "https://facebook.com/123"
        mock_result.message = "Posted"

        with patch("social.post_executor.get_channel") as mock_ch, \
             patch("social.post_executor.require_integration_action"), \
             patch("social.quote_card.render_quote_card") as mock_render, \
             patch("social.image_host.upload_public") as mock_upload, \
             patch("integrations.social_media.post_to_platform", return_value=mock_result) as mock_post:
            mock_ch.return_value = SocialChannel(
                channel_id="facebook", display_name="Facebook", execution_method="api",
            )
            from social.post_executor import dispatch_post
            ok = dispatch_post(pid, db_path=svc._db._db_path)

        assert ok is True
        mock_render.assert_not_called()
        mock_upload.assert_not_called()
        mock_post.assert_called_once_with("facebook", "FB body", image_url="", video_url="")
