from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from crypto_round import service
from crypto_round.config import DiscordSource, MarketRoundSettings
from crypto_round.conversation_tape import canonical_source
from crypto_round.db import CryptoRoundDB
from crypto_round.mintscan import _fetch_script, _json_result
from crypto_round.models import CryptoRoundOutput
from runtime import tool_impl_crypto_round


def _settings() -> MarketRoundSettings:
    return MarketRoundSettings(
        enabled=True,
        domain="crypto",
        debauchery_alias="Debauchery",
        approved_guild_id="1000",
        discord_channels=(
            DiscordSource("1001", "war-room", "primary"),
            DiscordSource("2001", "lounge", "primary", "2000", "hoodstreet", "HoodStreet"),
            DiscordSource("2002", "alpha", "primary", "2000", "hoodstreet", "HoodStreet"),
        ),
        x_feeds=("for_you", "following"),
        every_hours=2,
        discord_minute=2,
        x_minute=32,
        research_prefetch_times=("07:45", "19:45"),
        rollup_times=("08:00", "20:00"),
        timezone="America/Los_Angeles",
        discord_messages_per_channel=250,
        x_items_per_feed=100,
        last30days_days=3,
        last30days_runs_per_day=2,
        max_evidence_chars=48_000,
        model_tier="quality",
        judge_tier="quality",
        max_turns=6,
    )


def test_channel_identity_keeps_communities_and_guilds_separate() -> None:
    settings = _settings()
    assert settings.discord_source_name("1001") == "discord:debauchery"
    assert settings.discord_guild("1001") == "1000"
    assert settings.discord_source_name("2002") == "discord:hoodstreet"
    assert settings.discord_guild("2002") == "2000"
    assert settings.discord_community_labels() == {
        "discord:debauchery": "Debauchery",
        "discord:hoodstreet": "HoodStreet",
    }
    assert canonical_source("HoodStreet", settings) == "discord:hoodstreet"


def test_discord_staging_preserves_per_community_tapes(tmp_path) -> None:
    class SourceDB:
        marked: list[str] = []

        def undigested_messages(self, *, limit):
            assert limit == 750
            return [
                SimpleNamespace(
                    message_id="5001",
                    channel_id="1001",
                    channel_name="war-room",
                    author="Hawk",
                    posted_at="2026-08-14T15:00:00+00:00",
                    content="watch BTC",
                    embed_text="",
                ),
                SimpleNamespace(
                    message_id="5002",
                    channel_id="2002",
                    channel_name="alpha",
                    author="Alice",
                    posted_at="2026-08-14T15:01:00+00:00",
                    content="new mint lead",
                    embed_text="",
                ),
            ]

        def mark_digested(self, identifiers):
            self.marked.extend(identifiers)

    source_db = SourceDB()
    ledger = CryptoRoundDB(tmp_path / "rounds.db")
    result = service.stage_discord_evidence(
        settings=_settings(),
        ledger=ledger,
        source_db=source_db,
        receipts=[
            {"channel_id": "1001", "source": "discord:debauchery"},
            {"channel_id": "2002", "source": "discord:hoodstreet"},
        ],
        now=datetime(2026, 8, 14, 15, 5, tzinfo=UTC),
    )
    rows = ledger.round_evidence(result["round_id"])
    assert {item["source"] for item in rows} == {
        "discord:debauchery",
        "discord:hoodstreet",
    }
    assert source_db.marked == ["5001", "5002"]


def test_mintscan_script_is_bounded_and_has_no_transaction_verbs() -> None:
    script = _fetch_script("600", "hood", 99)
    assert "slice(0,25)" in script
    assert "credentials:'omit'" in script
    assert "privateKey" not in script
    assert "eth_sendTransaction" not in script
    assert "mint(" not in script


def test_structured_output_accepts_configured_discord_source_slug() -> None:
    payload = {
        "round_id": "r1",
        "decision": "no_call",
        "generated_at": "unknown",
        "source_health": [],
        "regime": "unknown",
        "levels": [],
        "catalysts": [],
        "opportunities": [],
        "signals": [],
        "paper_calls": [],
        "conversation_tape": [
            {
                "source": "discord:hoodstreet",
                "window_start": "unknown",
                "window_end": "unknown",
                "topics": [],
                "speakers": [],
                "agreements": [],
                "disagreements": [],
                "unanswered_questions": [],
                "signal_summary": "No material signal.",
                "noise_summary": "No material signal.",
                "coverage": {
                    "evidence_count": 0,
                    "unique_speakers": 0,
                    "observed_from": "unknown",
                    "observed_to": "unknown",
                    "attempts": 0,
                    "scrolls": 0,
                    "stop_reason": "empty",
                    "partial": False,
                    "gaps": [],
                },
            }
        ],
        "recap": "No entry.",
        "warnings": [],
    }
    output = CryptoRoundOutput.parse(json.dumps(payload), expected_round_id="r1")
    assert output.conversation_tape[0].source == "discord:hoodstreet"


def test_mintscan_json_parser_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="invalid JSON"):
        _json_result("not-json")
    with pytest.raises(RuntimeError, match="blocked"):
        _json_result(json.dumps({"success": False, "error": "blocked"}))


def test_mintscan_tool_keeps_discovery_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "crypto_round.mintscan.read_mintscan",
        lambda **kwargs: {
            "available": True,
            "candidates": [{"name": "Example", "verified": False}],
            "verification": "discovery only",
            "authority": "read-only; no wallet, mint, signing, or transaction path",
        },
    )
    result = json.loads(tool_impl_crypto_round._crypto_mintscan(window="10m", chain="all"))
    assert result["candidates"][0]["verified"] is False
    assert "no wallet" in result["authority"]
