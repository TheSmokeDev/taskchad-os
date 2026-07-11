"""Tests for the social cadence media routing (social/cadence.py).

The daily cadence renders an on-brand image for channels carrying brand assets
(design_file OR persona_pack) and stays caption-only otherwise, gated by
SOCIAL_CADENCE_MEDIA. It routes through content_factory.produce (the same engine
the Archon social-content-factory workflow uses), preserving per-channel dedup
and the fail-open contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from social import cadence


@dataclass
class _Ch:
    channel_id: str
    cadence_interval_hours: int = 24
    topic_pool: tuple = ("topic-a",)
    design_file: str = ""
    persona_pack: str = ""


def _install(monkeypatch, channels, *, media_env="true", produce_impl=None):
    """Wire fakes for every collaborator the tick pulls in at call time, and
    return the list of produce() calls (channel + media kind + topic_source +
    autopilot). ``produce_impl(channel_id)`` lets a test inject a return value
    or raise per channel."""
    monkeypatch.setenv("SOCIAL_CADENCE_ENABLED", "true")
    monkeypatch.setenv("SOCIAL_CADENCE_MEDIA", media_env)
    calls: list = []

    def fake_produce(channel_id, *, count=1, media="auto", topic=None,
                     topic_source="factory", autopilot=None, db_path=None):
        calls.append({"channel": channel_id, "media": media,
                      "topic_source": topic_source, "autopilot": autopilot})
        if produce_impl is not None:
            return produce_impl(channel_id)
        return {"queued": [100 + len(calls)], "posted": [], "failed": []}

    # cadence does local `from ... import` inside run_cadence_tick, so patch the
    # SOURCE modules (resolved when the tick actually runs).
    monkeypatch.setattr("social.content_factory.produce", fake_produce)
    monkeypatch.setattr("social.channels.list_active_channels", lambda: channels)
    monkeypatch.setattr("social.post_executor.dispatch_due_posts", lambda db_path=None: {"dispatched": 0})

    class _Svc:
        def __init__(self, db_path=None):
            pass

        def get_post(self, pid):
            return None  # skip delivery — we only assert the produce routing

    monkeypatch.setattr("social.service.SocialPostService", _Svc)
    import config
    monkeypatch.setattr(config, "get_postiz_settings",
                        lambda: type("P", (), {"configured": False})())
    return calls


def test_asset_channel_gets_image_assetless_stays_none(monkeypatch, tmp_path):
    channels = [
        _Ch("facebook", persona_pack="owner-YourBusiness-rep", design_file="brand_designs/x.json"),
        _Ch("linkedin"),  # no design_file, no persona_pack
    ]
    calls = _install(monkeypatch, channels)
    cadence.run_cadence_tick(state_path=tmp_path / "state.json", db_path=None)
    kinds = {c["channel"]: c["media"] for c in calls}
    assert kinds["facebook"] == "image"
    assert kinds["linkedin"] == "none"
    # cadence-origin drafts keep their provenance (not "factory")
    assert all(c["topic_source"] == "cadence" for c in calls)


def test_design_file_alone_enables_image(monkeypatch, tmp_path):
    channels = [_Ch("youtube", design_file="brand_designs/x.json")]  # design, no pack
    calls = _install(monkeypatch, channels)
    cadence.run_cadence_tick(state_path=tmp_path / "state.json", db_path=None)
    assert calls[0]["media"] == "image"


def test_persona_pack_alone_enables_image(monkeypatch, tmp_path):
    channels = [_Ch("instagram", persona_pack="owner-YourBusiness-rep")]  # pack, no design
    calls = _install(monkeypatch, channels)
    cadence.run_cadence_tick(state_path=tmp_path / "state.json", db_path=None)
    assert calls[0]["media"] == "image"


def test_media_toggle_off_forces_caption_only(monkeypatch, tmp_path):
    channels = [_Ch("facebook", persona_pack="owner-YourBusiness-rep")]
    calls = _install(monkeypatch, channels, media_env="false")
    cadence.run_cadence_tick(state_path=tmp_path / "state.json", db_path=None)
    assert calls[0]["media"] == "none"


def test_dedup_skips_recently_drafted_channel(monkeypatch, tmp_path):
    channels = [_Ch("facebook", persona_pack="p", cadence_interval_hours=24)]
    calls = _install(monkeypatch, channels)
    state = tmp_path / "state.json"
    cadence.run_cadence_tick(state_path=state, db_path=None)   # drafts once
    after_first = len(calls)
    assert after_first == 1
    cadence.run_cadence_tick(state_path=state, db_path=None)   # within 24h -> skip
    assert len(calls) == after_first  # produce not called a second time


def test_cadence_never_autoposts(monkeypatch, tmp_path):
    # The approval-card lane must force autopilot=False regardless of the global
    # HOMIE_SOCIAL_UNATTENDED flag.
    channels = [_Ch("facebook", persona_pack="p")]
    calls = _install(monkeypatch, channels)
    cadence.run_cadence_tick(state_path=tmp_path / "state.json", db_path=None)
    assert calls[0]["autopilot"] is False


def test_produce_error_skips_channel_and_keeps_going(monkeypatch, tmp_path):
    # A hard error in one channel's produce() (e.g. a transient DB lock) must not
    # abort the remaining channels or the dispatch step.
    channels = [_Ch("facebook", persona_pack="p"), _Ch("linkedin")]

    def impl(channel_id):
        if channel_id == "facebook":
            raise RuntimeError("database is locked")
        return {"queued": [200]}

    calls = _install(monkeypatch, channels, produce_impl=impl)
    dispatched: list = []
    monkeypatch.setattr(
        "social.post_executor.dispatch_due_posts",
        lambda db_path=None: (dispatched.append(True), {"dispatched": 0})[1],
    )
    out = cadence.run_cadence_tick(state_path=tmp_path / "state.json", db_path=None)
    attempted = [c["channel"] for c in calls]
    assert "facebook" in attempted and "linkedin" in attempted  # both attempted
    assert "facebook" in out["channels_skipped"]                # failed one skipped
    assert out["drafts_created"] == 1                            # linkedin still drafted
    assert dispatched == [True]                                  # dispatch still ran


def test_disabled_cadence_no_ops(monkeypatch, tmp_path):
    channels = [_Ch("facebook", persona_pack="p")]
    calls = _install(monkeypatch, channels)
    monkeypatch.setenv("SOCIAL_CADENCE_ENABLED", "false")
    out = cadence.run_cadence_tick(state_path=tmp_path / "state.json", db_path=None)
    assert out.get("skipped") == "cadence disabled"
    assert calls == []
