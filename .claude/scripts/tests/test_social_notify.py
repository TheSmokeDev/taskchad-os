"""Tests for social draft Telegram delivery (social/notify.py).

Covers the card builder, the inline-button callback contract, the token-leak
redaction (Step-1 HIGH fix), and the fail-open delivery guarantees.
"""

from __future__ import annotations

import urllib.parse

import pytest

from social import notify
from social.models import SocialPost


def _post(**kw) -> SocialPost:
    base = dict(id=5, channel="linkedin", topic_source="cadence", body="Hello world.")
    base.update(kw)
    return SocialPost(**base)


class TestCardText:
    def test_contains_header_body_footer(self):
        card = notify._build_card_text(_post(body="My draft body."))
        assert "#5" in card
        assert "LINKEDIN" in card
        assert "My draft body." in card
        assert "Approve & Post" in card

    def test_truncates_long_body_under_limit(self):
        card = notify._build_card_text(_post(body="x" * 9000))
        assert len(card) <= notify._TG_TEXT_LIMIT
        assert card.endswith("Tap Approve & Post to publish, Edit to tweak, or Reject.")

    def test_hard_caps_even_with_huge_header_fields(self):
        # An unbounded topic_source must never push the card past the limit.
        card = notify._build_card_text(_post(topic_source="z" * 9000, body="short"))
        assert len(card) <= notify._TG_TEXT_LIMIT


class TestReplyMarkup:
    def test_callback_data_contract(self):
        mk = notify._build_reply_markup(42)
        rows = mk["inline_keyboard"]
        assert rows[0][0]["callback_data"] == "social:approve:42"
        assert rows[1][0]["callback_data"] == "social:edit:42"
        assert rows[1][1]["callback_data"] == "social:reject:42"

    def test_callback_data_within_64_bytes(self):
        mk = notify._build_reply_markup(9999999999)
        for row in mk["inline_keyboard"]:
            for btn in row:
                assert len(btn["callback_data"].encode("utf-8")) <= 64


class TestRedact:
    def test_strips_token(self):
        out = notify._redact("error at https://api.telegram.org/botSECRET/sendMessage", "SECRET")
        assert "SECRET" not in out
        assert "***" in out

    def test_noop_when_token_absent(self):
        assert notify._redact("plain message", "SECRET") == "plain message"


class TestDelivery:
    def test_returns_false_without_creds(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "")
        assert notify.deliver_draft_to_telegram(_post()) is False

    def test_returns_false_on_invalid_post_id(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")
        assert notify.deliver_draft_to_telegram(_post(id=0)) is False
        # non-int id must not raise (e.g. a malformed row)
        assert notify.deliver_draft_to_telegram(_post(id="oops")) is False  # type: ignore[arg-type]

    def test_success_sends_correct_request(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "55555, 66666")
        captured: dict = {}

        def fake_urlopen(req, timeout=10):
            captured["url"] = req.full_url
            captured["data"] = req.data
            return None

        monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
        ok = notify.deliver_draft_to_telegram(_post(id=7, body="Body."))
        assert ok is True
        assert "bottok123/sendMessage" in captured["url"]
        params = dict(urllib.parse.parse_qsl(captured["data"].decode()))
        assert params["chat_id"] == "55555"  # first allowed id
        assert "Body." in params["text"]
        assert "social:approve:7" in params["reply_markup"]

    def test_never_raises_and_redacts_token_on_network_error(self, monkeypatch, capsys):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SUPERSECRET")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")

        def boom(req, timeout=10):
            # Simulate urllib echoing the token-bearing URL in the error.
            raise RuntimeError("HTTP error for https://api.telegram.org/botSUPERSECRET/sendMessage")

        monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
        ok = notify.deliver_draft_to_telegram(_post())
        assert ok is False
        out = capsys.readouterr().out
        assert "SUPERSECRET" not in out  # token must be redacted from logs


class TestPhotoCaption:
    def test_caption_capped_at_1024(self):
        cap = notify._build_photo_caption(_post(body="y" * 5000))
        assert len(cap) <= notify._TG_CAPTION_LIMIT
        assert "Approve & Post" in cap

    def test_caption_utf16_capped_with_emoji(self):
        # Supplementary-plane emoji count as 2 UTF-16 units each; a body of them
        # can stay <=1024 code points while exceeding Telegram's 1024 UTF-16 cap.
        cap = notify._build_photo_caption(_post(body="🔥" * 2000))
        assert notify._utf16_len(cap) <= notify._TG_CAPTION_LIMIT

    def test_utf16_truncate_never_splits_surrogate_pair(self):
        out = notify._utf16_truncate("a" + "🔥" * 10, 5)
        assert notify._utf16_len(out) <= 5
        out.encode("utf-16")  # a split surrogate pair would raise here


class TestPhotoDelivery:
    def _img(self, tmp_path):
        p = tmp_path / "card.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)  # non-empty fake PNG
        return str(p)

    def test_sends_photo_when_image_attached(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "55555")
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["url"] = req.full_url
            captured["ctype"] = req.get_header("Content-type")
            captured["data"] = req.data
            return None

        monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
        post = _post(id=9, body="Body.", media_path=self._img(tmp_path), media_type="image")
        ok = notify.deliver_draft_to_telegram(post)
        assert ok is True
        assert "bottok123/sendPhoto" in captured["url"]
        assert "multipart/form-data" in (captured["ctype"] or "")
        # the approve/edit/reject buttons still ride with the photo
        assert b"social:approve:9" in captured["data"]

    def test_text_card_when_no_media(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "55555")
        captured: dict = {}
        monkeypatch.setattr(
            notify.urllib.request, "urlopen",
            lambda req, timeout=10: captured.update(url=req.full_url),
        )
        ok = notify.deliver_draft_to_telegram(_post(id=3))
        assert ok is True
        assert "sendMessage" in captured["url"]

    def test_missing_file_falls_back_to_text(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "55555")
        captured: dict = {}
        monkeypatch.setattr(
            notify.urllib.request, "urlopen",
            lambda req, timeout=10: captured.update(url=req.full_url),
        )
        post = _post(id=6, media_path="/no/such/file.png", media_type="image")
        ok = notify.deliver_draft_to_telegram(post)
        assert ok is True
        assert "sendMessage" in captured["url"]

    def test_photo_send_failure_falls_back_to_text(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "55555")
        calls: list = []

        def fake_urlopen(req, timeout=10):
            calls.append(req.full_url)
            if "sendPhoto" in req.full_url:
                raise RuntimeError("boom")
            return None

        monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
        post = _post(id=4, media_path=self._img(tmp_path), media_type="image")
        ok = notify.deliver_draft_to_telegram(post)
        assert ok is True  # text card still delivered
        assert any("sendPhoto" in u for u in calls)
        assert any("sendMessage" in u for u in calls)
