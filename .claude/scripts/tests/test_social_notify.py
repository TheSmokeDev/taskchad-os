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
