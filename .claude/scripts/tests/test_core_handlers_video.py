"""Tests for the /video V3 guided wizard + vision approval gate (core_handlers).

Patterns (modeled on test_core_handlers_cabinet.py):

* SimpleNamespace incoming with real Channel-shaped attrs; FakeAdapter
  capturing send/send_typing.
* The video pipeline + research modules are stubbed via the monkeypatched
  lazy importers (_import_video_pipeline / _import_video_research), so no
  lane calls, no network, no render.
* Covers the full wizard matrix: kind/style/voice/vision buttons, typed
  fallback consumption rules (match-only pickers vs full-consume input),
  research degradation, derived-first ranking, vision approve binding,
  approve-during-render guard, redo/feedback, cancel, TTL expiry, bare
  /video restart, collect_only usage, the flagged text wizard, and the
  power-path bypass.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure both .claude/scripts and .claude/chat are importable.
_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import core_handlers  # type: ignore[import-not-found]  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers - incoming + adapter doubles
# ---------------------------------------------------------------------------


def _incoming(text: str = "", platform: str = "telegram", chan: str = "100") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        channel=SimpleNamespace(platform=platform, platform_id=chan),
        platform=platform,
        thread=None,
        chat_id=42,
    )


class FakeAdapter:
    def __init__(self) -> None:
        self.sent: list = []
        self.typing = 0

    async def send(self, message) -> None:  # noqa: ANN001
        self.sent.append(message)

    async def send_typing(self, channel) -> None:  # noqa: ANN001
        self.typing += 1

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent]

    def last_components(self) -> list[str]:
        return [c.custom_id for c in (self.sent[-1].components or [])]


def _fresh_vision() -> dict:
    return {
        "ok": True,
        "angle": "A tight look at the launch.",
        "beats": [
            {"kind": "hero", "summary": "Open on the name."},
            {"kind": "stat", "summary": "The one number that matters."},
            {"kind": "payoff", "summary": "Close on the call to action."},
        ],
        "imagery": {"treatment": "stylized", "note": "brand art carries it"},
        "duration_s": 30,
        "aspect": "16:9",
        "style": "bold-poster",
        "voice": "andrew",
        "provider": "lane:test",
        "notes": [],
    }


def _derived_dossier(**overrides) -> dict:
    base = {
        "ok": True,
        "mode": "url",
        "query": "https://site.example/x",
        "url": "https://site.example/x",
        "title": "Site Example",
        "summary_text": "Background facts about the site.",
        "facts": ["A fact about the site."],
        "claims_text": "",
        "derived_design": {"name": "brandsite"},
        "images": [],
        "search": [],
        "audit": [],
        "notes": [],
        "html_text": "<html><body>cached</body></html>",
    }
    base.update(overrides)
    return base


class _Calls:
    def __init__(self) -> None:
        self.render: list[tuple[str, dict]] = []
        self.vision: list[tuple[str, dict]] = []
        self.research: list[str] = []


def _stub_pipeline(calls: _Calls, *, missing: list[str] | None = None) -> SimpleNamespace:
    def render_brief(brief, **kwargs):  # noqa: ANN001
        calls.render.append((brief, kwargs))
        return {
            "ok": True,
            "mp4_path": "out/video.mp4",
            "output_dir": "out",
            "duration_s": 30.0,
            "score": {"final": 90},
            "provider": "lane:test",
            "style": "bold-poster",
            "error": "",
        }

    def generate_vision(brief, **kwargs):  # noqa: ANN001
        calls.vision.append((brief, kwargs))
        return _fresh_vision()

    return SimpleNamespace(
        check_dependencies=lambda: list(missing or []),
        render_brief=render_brief,
        generate_vision=generate_vision,
    )


@pytest.fixture
def wizard(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    """Stubbed pipeline + research, clean module state before and after."""
    calls = _Calls()
    pipeline = _stub_pipeline(calls)
    monkeypatch.setattr(core_handlers, "_import_video_pipeline", lambda: pipeline)

    def build_dossier(query):  # noqa: ANN001
        calls.research.append(query)
        return _derived_dossier(url=query, query=query)

    research = SimpleNamespace(build_dossier=build_dossier)
    monkeypatch.setattr(core_handlers, "_import_video_research", lambda: research)

    core_handlers._VIDEO_PENDING.clear()
    core_handlers._VIDEO_RENDER_STATE.update({"running": False, "started": "", "brief": ""})
    yield calls
    core_handlers._VIDEO_PENDING.clear()
    core_handlers._VIDEO_RENDER_STATE.update({"running": False, "started": "", "brief": ""})


def _key(incoming: SimpleNamespace) -> str:
    return core_handlers._video_channel_key(incoming)


async def _drive_to_vision(adapter: FakeAdapter, incoming: SimpleNamespace) -> dict:
    """Bare /video -> kind -> input (no URL) -> style -> voice -> vision card."""
    await core_handlers.handle_video(adapter, incoming, "")
    await core_handlers.handle_video_button(adapter, incoming, "video_kind:promo")
    typed = _incoming(text="make some noise about the launch")
    assert await core_handlers.try_consume_video_message(adapter, typed)
    await core_handlers.handle_video_button(adapter, incoming, "video_style:bold-poster")
    await core_handlers.handle_video_button(adapter, incoming, "video_voice:ryan")
    pending = core_handlers._VIDEO_PENDING[_key(incoming)]
    assert pending["stage"] == "await_vision"
    return pending


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_video_in_core_handlers() -> None:
    assert core_handlers.CORE_HANDLERS["video"] is core_handlers.handle_video


# ---------------------------------------------------------------------------
# STEP 1: bare /video -> kind keyboard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bare_video_starts_kind_step(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    out = await core_handlers.handle_video(adapter, inc, "")
    assert out is None  # the step sent its own message
    assert "What kind?" in adapter.texts[-1]
    assert "1 event recap" in adapter.texts[-1]
    ids = adapter.last_components()
    assert ids == [f"video_kind:{k}" for k, _, _ in core_handlers._VIDEO_KINDS]
    assert all(len(cid.encode()) <= 64 for cid in ids)
    assert core_handlers._VIDEO_PENDING[_key(inc)]["stage"] == "await_kind"


@pytest.mark.asyncio
async def test_bare_video_mid_wizard_restarts(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    await core_handlers.handle_video_button(adapter, inc, "video_kind:hype")
    assert core_handlers._VIDEO_PENDING[_key(inc)]["kind"] == "hype"
    await core_handlers.handle_video(adapter, inc, "")
    assert adapter.texts[-1].startswith("Restarting the video setup.")
    pending = core_handlers._VIDEO_PENDING[_key(inc)]
    assert pending["stage"] == "await_kind"
    assert pending["kind"] is None


@pytest.mark.asyncio
async def test_collect_only_bare_video_returns_usage(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    out = await core_handlers.handle_video(adapter, inc, "", collect_only=True)
    assert out is not None and "Usage:" in out
    assert adapter.sent == []
    assert _key(inc) not in core_handlers._VIDEO_PENDING


# ---------------------------------------------------------------------------
# Kind step: buttons + typed fallback (match-only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kind_button_advances_to_input(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    await core_handlers.handle_video_button(adapter, inc, "video_kind:promo")
    pending = core_handlers._VIDEO_PENDING[_key(inc)]
    assert pending["stage"] == "await_input"
    assert pending["kind"] == "promo"
    assert "raw material" in adapter.texts[-1]


@pytest.mark.asyncio
async def test_kind_button_without_pending_replies_expired(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video_button(adapter, inc, "video_kind:promo")
    assert "expired" in adapter.texts[-1]
    assert _key(inc) not in core_handlers._VIDEO_PENDING


@pytest.mark.asyncio
async def test_typed_number_consumed_at_kind_picker(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    typed = _incoming(text="2")
    assert await core_handlers.try_consume_video_message(adapter, typed)
    assert core_handlers._VIDEO_PENDING[_key(inc)]["kind"] == "promo"


@pytest.mark.asyncio
async def test_typed_nonmatch_falls_through_at_picker(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    typed = _incoming(text="2pm works for me")
    assert not await core_handlers.try_consume_video_message(adapter, typed)
    # The wizard stays pending - the message went to normal chat.
    assert core_handlers._VIDEO_PENDING[_key(inc)]["stage"] == "await_kind"


@pytest.mark.asyncio
async def test_slash_and_button_text_pass_through(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    assert not await core_handlers.try_consume_video_message(adapter, _incoming(text="/status"))
    assert not await core_handlers.try_consume_video_message(adapter, _incoming(text="__button:x"))


@pytest.mark.asyncio
async def test_typed_cancel_pops_at_any_picker(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    assert await core_handlers.try_consume_video_message(adapter, _incoming(text="cancel"))
    assert _key(inc) not in core_handlers._VIDEO_PENDING
    assert "Scrapped" in adapter.texts[-1]


# ---------------------------------------------------------------------------
# Input step: full consume + flags + URL extraction + research
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_await_input_consumes_brief_flags_and_url(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    await core_handlers.handle_video_button(adapter, inc, "video_kind:promo")
    typed = _incoming(text="tell our story https://site.example/about --aspect 9:16 --duration 20")
    assert await core_handlers.try_consume_video_message(adapter, typed)
    pending = core_handlers._VIDEO_PENDING[_key(inc)]
    assert pending["url"] == "https://site.example/about"
    assert pending["input"] == "tell our story"  # URL stripped from the brief
    assert pending["aspect"] == "9:16"
    assert pending["duration"] == 20
    assert wizard.research == ["https://site.example/about"]
    # Research succeeded -> dossier stored -> style step with derived first.
    assert pending["stage"] == "await_style"
    assert pending["dossier"]["ok"] is True
    assert "Reading the site" in " ".join(adapter.texts)
    assert adapter.last_components()[0] == "video_style:derived"


@pytest.mark.asyncio
async def test_research_failure_goes_theme_only(
    wizard: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bad_dossier(query):  # noqa: ANN001
        return {"ok": False, "notes": ["could not fetch the page"]}

    monkeypatch.setattr(
        core_handlers,
        "_import_video_research",
        lambda: SimpleNamespace(build_dossier=bad_dossier),
    )
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    await core_handlers.handle_video_button(adapter, inc, "video_kind:promo")
    typed = _incoming(text="our story https://site.example/x")
    assert await core_handlers.try_consume_video_message(adapter, typed)
    pending = core_handlers._VIDEO_PENDING[_key(inc)]
    assert pending["dossier"] is None
    assert pending["stage"] == "await_style"
    joined = " ".join(adapter.texts)
    assert "Couldn't read site.example" in joined
    assert "your words carry it" in joined
    # No derived option without a dossier.
    assert "video_style:derived" not in adapter.last_components()


@pytest.mark.asyncio
async def test_research_not_wired_degrades_gracefully(
    wizard: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise ImportError("video_research not deployed")

    monkeypatch.setattr(core_handlers, "_import_video_research", boom)
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    await core_handlers.handle_video_button(adapter, inc, "video_kind:promo")
    typed = _incoming(text="our story https://site.example/x")
    assert await core_handlers.try_consume_video_message(adapter, typed)
    assert "site research isn't wired yet - going theme-only" in " ".join(adapter.texts)
    assert core_handlers._VIDEO_PENDING[_key(inc)]["stage"] == "await_style"


@pytest.mark.asyncio
async def test_research_off_flag_skips_research(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    await core_handlers.handle_video_button(adapter, inc, "video_kind:promo")
    typed = _incoming(text="our story https://site.example/x --research off")
    assert await core_handlers.try_consume_video_message(adapter, typed)
    assert wizard.research == []
    assert core_handlers._VIDEO_PENDING[_key(inc)]["stage"] == "await_style"


@pytest.mark.asyncio
async def test_researching_stage_holds_optionish_replies(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    core_handlers._video_wizard_set(_key(inc), stage="researching")
    assert await core_handlers.try_consume_video_message(adapter, _incoming(text="2"))
    assert "Still reading the site" in adapter.texts[-1]
    long_msg = _incoming(text="can you also check my calendar for tomorrow")
    assert not await core_handlers.try_consume_video_message(adapter, long_msg)


# ---------------------------------------------------------------------------
# Style step: ranking, derived-first, typed fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_style_ranking_recommended_tag_without_dossier(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    await core_handlers.handle_video_button(adapter, inc, "video_kind:hype")
    typed = _incoming(text="a launch hype reel")
    assert await core_handlers.try_consume_video_message(adapter, typed)
    msg = adapter.sent[-1]
    assert msg.components[0].custom_id != "video_style:derived"
    assert "(recommended)" in msg.components[0].label
    assert msg.components[-1].custom_id == "video_style:auto"
    assert "1." in msg.text and "(reply with a number or a name)" in msg.text


@pytest.mark.asyncio
async def test_style_pick_advances_to_voice(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    await core_handlers.handle_video_button(adapter, inc, "video_kind:promo")
    assert await core_handlers.try_consume_video_message(adapter, _incoming(text="the launch story"))
    await core_handlers.handle_video_button(adapter, inc, "video_style:bold-poster")
    pending = core_handlers._VIDEO_PENDING[_key(inc)]
    assert pending["stage"] == "await_voice"
    assert pending["style"] == "bold-poster"
    ids = adapter.last_components()
    assert ids == [f"video_voice:{k}" for _, k, _ in core_handlers._VIDEO_VOICES]
    assert "Style locked: bold-poster" in adapter.texts[-1]


@pytest.mark.asyncio
async def test_typed_style_number_one_picks_derived_when_present(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    await core_handlers.handle_video_button(adapter, inc, "video_kind:promo")
    typed = _incoming(text="our story https://site.example/x")
    assert await core_handlers.try_consume_video_message(adapter, typed)
    assert await core_handlers.try_consume_video_message(adapter, _incoming(text="1"))
    assert core_handlers._VIDEO_PENDING[_key(inc)]["style"] == "derived"


@pytest.mark.asyncio
async def test_legacy_style_tap_without_pending_starts_at_voice(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video_button(adapter, inc, "video_style:coral")
    pending = core_handlers._VIDEO_PENDING[_key(inc)]
    assert pending["stage"] == "await_voice"
    assert pending["style"] == "coral"
    assert pending["kind"] is None


# ---------------------------------------------------------------------------
# Voice step -> vision card
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voice_pick_drafts_vision_card(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await _drive_to_vision(adapter, inc)
    brief, kwargs = wizard.vision[-1]
    assert brief == "make some noise about the launch"
    assert kwargs["kind"] == "promo"
    assert kwargs["style"] == "bold-poster"
    assert kwargs["voice_label"] == "ryan"
    assert kwargs["dossier"] is None
    card = adapter.texts[-1]
    assert "THE VISION" in card
    assert "1. [hero] Open on the name." in card
    assert "imagery: stylized identity-locked art" in card
    assert "look: bold-poster" in card and "voice: ryan" in card
    assert "~30s" in card and "16:9" in card
    assert "Reply with notes to redo it your way." in card
    assert adapter.last_components() == [
        "video_vision:approve",
        "video_vision:style",
        "video_vision:redo",
        "video_vision:cancel",
    ]
    assert "Drafting the vision..." in adapter.texts


@pytest.mark.asyncio
async def test_typed_voice_name_consumed(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    await core_handlers.handle_video_button(adapter, inc, "video_kind:promo")
    assert await core_handlers.try_consume_video_message(adapter, _incoming(text="the launch story"))
    await core_handlers.handle_video_button(adapter, inc, "video_style:bold-poster")
    assert await core_handlers.try_consume_video_message(adapter, _incoming(text="ryan"))
    pending = core_handlers._VIDEO_PENDING[_key(inc)]
    assert pending["stage"] == "await_vision"
    assert pending["voice_key"] == "ryan"
    assert pending["voice"] == core_handlers._video_voice_for_key("ryan")


@pytest.mark.asyncio
async def test_legacy_three_part_voice_tolerated(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    await core_handlers.handle_video_button(adapter, inc, "video_kind:promo")
    assert await core_handlers.try_consume_video_message(adapter, _incoming(text="the launch story"))
    await core_handlers.handle_video_button(adapter, inc, "video_style:bold-poster")
    # A stale v1-era 3-part voice id still lands the voice + drafts the vision.
    await core_handlers.handle_video_button(adapter, inc, "video_voice:bold-poster:ryan")
    pending = core_handlers._VIDEO_PENDING[_key(inc)]
    assert pending["stage"] == "await_vision"
    assert pending["voice_key"] == "ryan"


@pytest.mark.asyncio
async def test_legacy_three_part_voice_without_pending_restores_input_step(
    wizard: _Calls,
) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video_button(adapter, inc, "video_voice:coral:ryan")
    pending = core_handlers._VIDEO_PENDING[_key(inc)]
    assert pending["stage"] == "await_input"
    assert pending["style"] == "coral"
    assert pending["voice_key"] == "ryan"
    assert "raw material" in adapter.texts[-1]


@pytest.mark.asyncio
async def test_two_part_voice_without_pending_replies_expired(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video_button(adapter, inc, "video_voice:ryan")
    assert "expired" in adapter.texts[-1]


# ---------------------------------------------------------------------------
# Vision gate: approve / change style / redo / cancel / guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vision_approve_pops_and_binds_opts(
    wizard: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await _drive_to_vision(adapter, inc)
    captured: dict = {}

    async def fake_kickoff(adapter_, incoming_, pipeline_, brief, opts, *, collect_only=False):  # noqa: ANN001
        captured["brief"] = brief
        captured["opts"] = opts
        return "Rendering your video now."

    monkeypatch.setattr(core_handlers, "_kickoff_video_render", fake_kickoff)
    await core_handlers.handle_video_button(adapter, inc, "video_vision:approve")
    assert _key(inc) not in core_handlers._VIDEO_PENDING  # popped after kickoff
    assert captured["brief"] == "make some noise about the launch"
    opts = captured["opts"]
    assert opts["style"] == "bold-poster"
    assert opts["aspect"] == "16:9"
    assert opts["duration"] == 30
    assert opts["voice"] == core_handlers._video_voice_for_key("ryan")
    assert opts["research_dossier"] is None
    assert opts["vision"]["angle"] == "A tight look at the launch."
    assert opts["imagery"] == "stylized"
    assert "Rendering your video now." in adapter.texts[-1]


@pytest.mark.asyncio
async def test_vision_approve_derived_style_binds_none(
    wizard: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    await core_handlers.handle_video_button(adapter, inc, "video_kind:promo")
    assert await core_handlers.try_consume_video_message(
        adapter, _incoming(text="our story https://site.example/x")
    )
    await core_handlers.handle_video_button(adapter, inc, "video_style:derived")
    await core_handlers.handle_video_button(adapter, inc, "video_voice:andrew")
    captured: dict = {}

    async def fake_kickoff(adapter_, incoming_, pipeline_, brief, opts, *, collect_only=False):  # noqa: ANN001
        captured["opts"] = opts
        return "Rendering your video now."

    monkeypatch.setattr(core_handlers, "_kickoff_video_render", fake_kickoff)
    await core_handlers.handle_video_button(adapter, inc, "video_vision:approve")
    assert captured["opts"]["style"] is None  # derived -> render resolves from dossier
    assert captured["opts"]["research_dossier"]["ok"] is True
    assert captured["opts"]["research_dossier"]["derived_design"] == {"name": "brandsite"}


@pytest.mark.asyncio
async def test_approve_while_render_running_keeps_pending(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await _drive_to_vision(adapter, inc)
    core_handlers._VIDEO_RENDER_STATE.update({"running": True, "started": "12:00:00", "brief": "x"})
    await core_handlers.handle_video_button(adapter, inc, "video_vision:approve")
    assert "already running" in adapter.texts[-1]
    pending = core_handlers._VIDEO_PENDING[_key(inc)]
    assert pending["stage"] == "await_vision"  # KEPT for a later /video approve


@pytest.mark.asyncio
async def test_approve_with_missing_deps_keeps_pending(
    wizard: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await _drive_to_vision(adapter, inc)
    broken = _stub_pipeline(_Calls(), missing=["ffmpeg"])
    monkeypatch.setattr(core_handlers, "_import_video_pipeline", lambda: broken)
    await core_handlers.handle_video_button(adapter, inc, "video_vision:approve")
    assert "needs these tools" in adapter.texts[-1]
    assert core_handlers._VIDEO_PENDING[_key(inc)]["stage"] == "await_vision"


@pytest.mark.asyncio
async def test_redo_button_regenerates_with_prior(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    pending = await _drive_to_vision(adapter, inc)
    first_vision = pending["vision"]
    await core_handlers.handle_video_button(adapter, inc, "video_vision:redo")
    assert len(wizard.vision) == 2
    _, kwargs = wizard.vision[-1]
    assert kwargs["feedback"] == ""
    assert kwargs["prior_vision"] is first_vision
    assert core_handlers._VIDEO_PENDING[_key(inc)]["stage"] == "await_vision"


@pytest.mark.asyncio
async def test_typed_feedback_at_vision_regenerates(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    pending = await _drive_to_vision(adapter, inc)
    first_vision = pending["vision"]
    typed = _incoming(text="make it darker and lead with the number")
    assert await core_handlers.try_consume_video_message(adapter, typed)
    _, kwargs = wizard.vision[-1]
    assert kwargs["feedback"] == "make it darker and lead with the number"
    assert kwargs["prior_vision"] is first_vision


@pytest.mark.asyncio
async def test_change_style_loops_back_and_skips_voice(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await _drive_to_vision(adapter, inc)
    await core_handlers.handle_video_button(adapter, inc, "video_vision:style")
    assert core_handlers._VIDEO_PENDING[_key(inc)]["stage"] == "await_style"
    await core_handlers.handle_video_button(adapter, inc, "video_style:coral")
    # Voice already chosen -> straight back to a fresh vision, no voice step.
    assert len(wizard.vision) == 2
    _, kwargs = wizard.vision[-1]
    assert kwargs["style"] == "coral"
    assert core_handlers._VIDEO_PENDING[_key(inc)]["stage"] == "await_vision"


@pytest.mark.asyncio
async def test_cancel_button_pops(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await _drive_to_vision(adapter, inc)
    await core_handlers.handle_video_button(adapter, inc, "video_vision:cancel")
    assert _key(inc) not in core_handlers._VIDEO_PENDING
    assert "Scrapped" in adapter.texts[-1]


@pytest.mark.asyncio
async def test_vision_button_without_pending_replies_expired(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video_button(adapter, inc, "video_vision:approve")
    assert "expired" in adapter.texts[-1]


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wizard_expiry_pops_state(
    wizard: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    frozen = time.time()
    monkeypatch.setattr(core_handlers.time, "time", lambda: frozen + 601)
    # Typed input silently falls through after expiry...
    assert not await core_handlers.try_consume_video_message(adapter, _incoming(text="2"))
    assert _key(inc) not in core_handlers._VIDEO_PENDING
    # ...and a button tap gets the expiry copy.
    await core_handlers.handle_video_button(adapter, inc, "video_kind:promo")
    assert "expired" in adapter.texts[-1]


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_includes_wizard_stage(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    await core_handlers.handle_video_button(adapter, inc, "video_kind:promo")
    out = await core_handlers.handle_video(adapter, inc, "status")
    assert "Wizard: stage await_input, kind promo" in out


@pytest.mark.asyncio
async def test_cancel_subcommand(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")
    out = await core_handlers.handle_video(adapter, inc, "cancel")
    assert "Scrapped" in out
    assert _key(inc) not in core_handlers._VIDEO_PENDING
    out2 = await core_handlers.handle_video(adapter, inc, "cancel")
    assert "Nothing to cancel" in out2


@pytest.mark.asyncio
async def test_approve_subcommand_without_vision(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    out = await core_handlers.handle_video(adapter, inc, "approve")
    assert "Nothing awaiting approval" in out


# ---------------------------------------------------------------------------
# Flagged wizard (text vision - the CLI path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flagged_wizard_returns_text_vision(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    out = await core_handlers.handle_video(adapter, inc, "--kind promo --url https://site.example/x")
    assert out is not None
    assert "THE VISION" in out
    assert "-> /video approve | /video redo [notes] | /video cancel" in out
    assert adapter.sent == []  # pure text return: works on buttonless adapters
    pending = core_handlers._VIDEO_PENDING[_key(inc)]
    assert pending["stage"] == "await_vision"
    assert pending["text_mode"] is True
    assert wizard.research == ["https://site.example/x"]
    _, kwargs = wizard.vision[-1]
    assert kwargs["kind"] == "promo"
    assert kwargs["voice_label"] == "andrew"  # default voice
    assert kwargs["dossier"]["ok"] is True


@pytest.mark.asyncio
async def test_flagged_wizard_approve_subcommand_kicks_off(
    wizard: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "--kind promo --url https://site.example/x")
    captured: dict = {}

    async def fake_kickoff(adapter_, incoming_, pipeline_, brief, opts, *, collect_only=False):  # noqa: ANN001
        captured["opts"] = opts
        captured["collect_only"] = collect_only
        return "Rendering your video now."

    monkeypatch.setattr(core_handlers, "_kickoff_video_render", fake_kickoff)
    out = await core_handlers.handle_video(adapter, inc, "approve")
    assert out == "Rendering your video now."
    assert captured["opts"]["vision"]["angle"] == "A tight look at the launch."
    assert _key(inc) not in core_handlers._VIDEO_PENDING


@pytest.mark.asyncio
async def test_flagged_wizard_redo_subcommand_returns_text_card(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "--kind promo --url https://site.example/x")
    out = await core_handlers.handle_video(adapter, inc, "redo lead with the price")
    assert out is not None and "THE VISION" in out
    assert "-> /video approve" in out
    _, kwargs = wizard.vision[-1]
    assert kwargs["feedback"] == "lead with the price"
    assert isinstance(kwargs["prior_vision"], dict)


@pytest.mark.asyncio
async def test_flagged_wizard_voice_and_imagery_flags(wizard: _Calls) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    out = await core_handlers.handle_video(
        adapter, inc, "--kind promo --url https://site.example/x --voice ryan --imagery css"
    )
    assert out is not None and "THE VISION" in out
    pending = core_handlers._VIDEO_PENDING[_key(inc)]
    assert pending["voice_key"] == "ryan"
    # --imagery css overrides the vision's proposed treatment.
    assert pending["vision"]["imagery"]["treatment"] == "css"
    assert "imagery: pure CSS scenes" in out


# ---------------------------------------------------------------------------
# Power path (explicit brief) bypasses the wizard entirely
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_power_path_bypasses_wizard_and_pops_pending(
    wizard: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, inc = FakeAdapter(), _incoming()
    await core_handlers.handle_video(adapter, inc, "")  # wizard pending

    async def fake_kickoff(adapter_, incoming_, pipeline_, brief, opts, *, collect_only=False):  # noqa: ANN001
        return "Rendering your video now."

    monkeypatch.setattr(core_handlers, "_kickoff_video_render", fake_kickoff)
    out = await core_handlers.handle_video(adapter, inc, "a launch video --style coral")
    assert out == "Rendering your video now."
    assert _key(inc) not in core_handlers._VIDEO_PENDING  # no wizard, no gate
    assert wizard.vision == []  # the vision gate never ran


@pytest.mark.asyncio
async def test_power_path_inline_cli_render_with_url_research(wizard: _Calls) -> None:
    adapter = FakeAdapter()
    inc = _incoming(platform="cli", chan="repl")
    out = await core_handlers.handle_video(
        adapter, inc, "a launch video --style coral --url https://site.example/x"
    )
    assert out is not None and "Video ready." in out
    brief, kwargs = wizard.render[-1]
    assert brief == "a launch video"
    assert kwargs["research"] == "https://site.example/x"  # --url rides as research
    assert kwargs["style"] == "coral"


@pytest.mark.asyncio
async def test_parse_video_flags_v3_additions(wizard: _Calls) -> None:
    brief, opts = core_handlers._parse_video_flags(
        "the story --kind promo --url https://x.example --research off "
        "--voice ryan --imagery photos --aspect 9:16".split()
    )
    assert brief == "the story"
    assert opts["kind"] == "promo"
    assert opts["url"] == "https://x.example"
    assert opts["research"] is False
    assert opts["voice"] == core_handlers._video_voice_for_key("ryan")
    assert opts["voice_key"] == "ryan"
    assert opts["imagery"] == "photos"
    assert opts["aspect"] == "9:16"
    # Unknown kind/imagery values are dropped (flag still consumed).
    brief2, opts2 = core_handlers._parse_video_flags("x --kind sitcom --imagery oils".split())
    assert brief2 == "x"
    assert opts2["kind"] is None
    assert opts2["imagery"] is None
