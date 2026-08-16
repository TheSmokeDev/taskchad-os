"""Focused safety tests for scheduled SEO/GEO Discord receipts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import config  # noqa: E402
import seo_geo_discord_notify as notify  # noqa: E402
import seo_geo_scheduled_job as scheduled  # noqa: E402


def test_target_resolution_requires_one_bound_channel_and_one_operator(tmp_path, monkeypatch):
    bindings = tmp_path / "bindings.json"
    bindings.write_text(json.dumps({
        "guild_id": "guild-1",
        "channels": {
            "channel-1": {"name": "seo_geo", "persona": "seo_geo", "kind": "persona"},
        },
    }), encoding="utf-8")
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "token", raising=False)
    monkeypatch.setattr(config, "DISCORD_ALLOWED_GUILDS", ["guild-1"], raising=False)
    monkeypatch.setattr(config, "DISCORD_ALLOWED_USERS", ["operator-1"], raising=False)

    assert notify.resolve_target(bindings_path=bindings) == notify.DiscordTarget(
        "token", "guild-1", "channel-1", "operator-1"
    )

    monkeypatch.setattr(config, "DISCORD_ALLOWED_USERS", ["operator-1", "operator-2"], raising=False)
    assert notify.resolve_target(bindings_path=bindings) is None


def test_notification_posts_only_the_configured_operator_mention(tmp_path, monkeypatch):
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps({
        "generated_at": "2026-08-12T09:15:00+00:00",
        "sources": {
            "gsc": {"status": "ok"},
            "ga4": {"status": "ok"},
            "measurement_registry": {"status": "ok", "summary": {
                "expected_public_brands": 27,
                "gsc_verified_access_or_fresh_data_receipts": 27,
                "ga4_deployed_tag_proofs": 0,
                "terminal_lead_receipts": 0,
            }},
        },
    }), encoding="utf-8")
    target = notify.DiscordTarget("token", "guild", "channel", "operator")
    captured: dict[str, object] = {}

    monkeypatch.setattr(notify, "resolve_target", lambda: target)
    monkeypatch.setattr(notify, "_post", lambda current, *, content: captured.update({"target": current, "content": content}) or "message-1")

    result = notify.notify(
        job="daily",
        status="completed",
        receipt_path=receipt_path,
        exit_code=0,
        out_dir=tmp_path / "out",
    )

    assert result["delivery"] == {
        "status": "delivered",
        "channel_id": "channel",
        "message_id": "message-1",
        "operator_mention": True,
        "content_sha256": result["delivery"]["content_sha256"],
    }
    assert captured["target"] == target
    assert "GA4 events are not terminal leads" in str(captured["content"])
    assert "terminal lead receipts=0" in str(captured["content"])
    assert (tmp_path / "out" / "daily-latest.json").is_file()


def test_daily_message_reports_gsc_movement_and_concrete_update_candidates(tmp_path):
    previous_path = tmp_path / "2026-08-11.json"
    current_path = tmp_path / "2026-08-12.json"
    previous_path.write_text(json.dumps({
        "ranges": {"primary": {"start": "2026-07-12", "end": "2026-08-08", "days": 28}},
        "brands": [{
            "brand_id": "YourBusiness", "display_name": "YourBusiness",
            "analytics": {"totals": {"impressions": 100, "clicks": 5}},
        }],
    }), encoding="utf-8")
    current_path.write_text(json.dumps({
        "ranges": {"primary": {"start": "2026-07-13", "end": "2026-08-09", "days": 28}},
        "fleet_window_comparisons": {
            "28d": {
                "current": {"impressions": 140, "clicks": 8, "ctr": 8 / 140, "position": 8.6},
                "previous": {"impressions": 100, "clicks": 5, "ctr": 0.05, "position": 9.2},
                "delta": {"impressions": 40, "clicks": 3},
                "current_range": {"start": "2026-07-13", "end": "2026-08-09"},
                "previous_range": {"start": "2026-06-15", "end": "2026-07-12"},
            }
        },
        "brands": [{
            "brand_id": "YourBusiness", "display_name": "YourBusiness",
            "analytics": {
                "totals": {"impressions": 140, "clicks": 8},
                "window_comparisons": {"7d": {"delta": {"impressions": 40}}},
                "top_queries": [{"keys": ["dmv sr22 form"], "categories": ["sr22_dui_highrisk"]}],
                "top_query_pages": [{"keys": ["dmv sr22 form", "https://www.your-business.example.com/guide"], "impressions": 22, "clicks": 2, "position": 8.6}],
            },
        }],
    }), encoding="utf-8")
    receipt = {
        "sources": {"gsc": {"stdout": f"SNAPSHOT_JSON={current_path}\n"}},
        "generated_at": "2026-08-12T09:15:00+00:00",
    }

    message = notify.render_message(
        job="daily",
        status="completed",
        receipt=receipt,
        exit_code=0,
        daily_context=notify._daily_context(receipt),
    )

    assert "GSC 28d: 140 imp (+40, +40.0%)" in message
    assert "8 clicks (+3, +60.0%)" in message
    assert "dmv sr22 form" in message
    assert "approval required; no page changed" in message


def test_daily_sitemap_alert_names_error_owner_and_counts_warning_brands():
    snapshot = {
        "brands": [
            {
                "display_name": "SR22 Filing California",
                "sitemaps": [{"errors": 2, "warnings": 0}],
            },
            {"display_name": "Carnal Seguro", "sitemaps": [{"errors": 0, "warnings": 100}]},
            {"display_name": "YourBusiness", "sitemaps": [{"errors": 0, "warnings": 3}]},
        ]
    }

    assert notify._sitemap_alert_line(snapshot) == (
        "Sitemap alert: SR22 Filing California 2 errors · warnings on 2 brands (103 total)"
    )


def test_scheduled_wrapper_does_not_report_an_old_receipt_as_new(tmp_path, monkeypatch):
    old_receipt = tmp_path / "old.json"
    old_receipt.write_text('{"generated_at":"old"}', encoding="utf-8")
    monkeypatch.setitem(scheduled.JOB_COMMANDS, "daily", (["fake.py"], old_receipt))
    monkeypatch.setattr(scheduled.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        scheduled,
        "notify",
        lambda **kwargs: observed.update(kwargs) or {"delivery": {"status": "dry_run", "message_id": None}},
    )

    code, _ = scheduled.run_job(job="daily")

    assert code == 1
    assert observed["status"] == "failed"
    assert observed["receipt_path"] is None
    assert observed["failure_reason"] == "no fresh local receipt was written"


def test_scheduled_wrapper_fails_when_a_fresh_job_receipt_cannot_reach_discord(tmp_path, monkeypatch):
    receipt_path = tmp_path / "fresh.json"
    monkeypatch.setitem(scheduled.JOB_COMMANDS, "daily", (["fake.py"], receipt_path))

    def _fresh_process(*_args, **_kwargs):
        receipt_path.write_text('{"generated_at":"new"}', encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(scheduled.subprocess, "run", _fresh_process)
    monkeypatch.setattr(
        scheduled,
        "notify",
        lambda **_kwargs: {"delivery": {"status": "delivery_failed", "message_id": None}},
    )

    code, _ = scheduled.run_job(job="daily")

    assert code == 3
