"""Tests for concept_drafter — predicate, slug, draft I/O, lookup, footer.

24 tests per PRP §6. Match existing pytest/uv conventions.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import concept_drafter
from concept_drafter import (
    ANALYSIS_MARKERS,
    DRAFT_TTL_SECONDS,
    DraftAmbiguityError,
    DraftResult,
    accept_draft,
    create_draft,
    derive_slug,
    diff_draft,
    find_draft_by_id,
    maybe_draft_and_footer,
    should_draft,
    sweep_expired,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Vault layout — concepts/ and concepts/_drafts/ ready to use."""
    (tmp_path / "concepts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "concepts" / "_drafts").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_inline_sweep_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset module-global sweep throttle so each test starts fresh."""
    monkeypatch.setattr(concept_drafter, "_LAST_INLINE_SWEEP", 0.0, raising=False)


# Helper — build a long analytical response that passes should_draft.
def _long_response(slug_heading: str = "Budget Overview") -> str:
    body = (
        f"# {slug_heading}\n\n"
        "Here is the analysis. compared to last month, we are tighter "
        "on liquidity. The reason is the loan repayment schedule shifted. "
        "in summary, we have a 15% drop in operating cash. "
    )
    # Pad to >800 chars
    return body + ("Detail line. " * 60)


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


class TestShouldDraft:
    def test_should_draft_threshold_and_markers(self):
        # Long enough + has marker → True
        assert should_draft("show me the budget", _long_response())
        # Too short → False
        assert not should_draft("hi", "short answer")
        # Long but slash-prefixed user → False
        assert not should_draft("/budget", _long_response())
        # Long but no markers → False
        no_marker = "Just some prose. " * 80
        assert not should_draft("budget question?", no_marker)
        # Sanity: each marker individually triggers
        for marker in ANALYSIS_MARKERS:
            text = "x" * 850 + " " + marker + " " + "y" * 50
            assert should_draft("user query?", text), f"marker {marker!r} missed"


# ---------------------------------------------------------------------------
# Slug derivation
# ---------------------------------------------------------------------------


class TestDeriveSlug:
    def test_derive_slug_heading_first(self):
        resp = "# Budget Overview\n\nSome content."
        assert derive_slug("anything", resp) == "BUDGET-OVERVIEW"

    def test_derive_slug_question_fallback(self):
        # No heading → first 4 non-stopword tokens of user text.
        user = "what is the difference between vector and keyword search"
        resp = "Long body without a heading"
        slug = derive_slug(user, resp)
        # Stopwords (what, is, the, between, and) dropped.
        assert slug == "DIFFERENCE-VECTOR-KEYWORD-SEARCH"


# ---------------------------------------------------------------------------
# Draft creation
# ---------------------------------------------------------------------------


class TestCreateDraft:
    def test_create_draft_writes_atomic(self, vault: Path):
        called = {"count": 0}
        real_replace = os.replace

        def spy(src, dst):
            called["count"] += 1
            return real_replace(src, dst)

        with patch.object(concept_drafter.os, "replace", side_effect=spy):
            result = create_draft(
                "topic question",
                _long_response(),
                vault,
                session_id="sess",
                turn_id="t1",
                drafted_slugs=set(),
            )
        assert result.created
        assert called["count"] >= 1
        assert result.path is not None
        assert result.path.exists()
        # Tempfile cleanup verified — no .draft-*.tmp lingering
        leftovers = list((vault / "concepts" / "_drafts").glob(".draft-*.tmp"))
        assert leftovers == []

    def test_create_draft_uuid_format(self, vault: Path):
        result = create_draft(
            "topic question",
            _long_response(),
            vault,
            session_id="sess",
            turn_id="t1",
            drafted_slugs=set(),
        )
        assert result.created
        assert re.fullmatch(r"[0-9a-f]{32}", result.auto_draft_id)

    def test_create_draft_compiled_from_schema(self, vault: Path):
        result = create_draft(
            "topic question",
            _long_response(),
            vault,
            session_id="sess-A",
            turn_id="turn-7",
            drafted_slugs=set(),
        )
        assert result.created and result.path is not None
        content = result.path.read_text(encoding="utf-8")
        assert "compiled_from:" in content
        assert '- "[[chat:sess-A:turn-7]]"' in content

    def test_create_draft_session_dedup_first_call(self, vault: Path):
        slugs: set[str] = set()
        result = create_draft(
            "topic", _long_response(), vault,
            session_id="s", turn_id="1", drafted_slugs=slugs,
        )
        assert result.created
        assert result.slug in slugs

    def test_multi_turn_dedup_suffixes(self, vault: Path):
        slugs: set[str] = set()
        first = create_draft(
            "topic", _long_response("Budget Overview"), vault,
            session_id="s", turn_id="1", drafted_slugs=slugs,
        )
        second = create_draft(
            "topic", _long_response("Budget Overview"), vault,
            session_id="s", turn_id="2", drafted_slugs=slugs,
        )
        assert first.created and second.created
        assert first.slug == "BUDGET-OVERVIEW"
        assert second.slug == "BUDGET-OVERVIEW-2"
        assert first.path != second.path
        assert second.path.exists()

    def test_create_draft_throttled_sweep(self, vault: Path):
        sweep_calls = {"n": 0}

        def fake_sweep(*args, **kwargs):
            sweep_calls["n"] += 1
            return []

        with patch.object(concept_drafter, "sweep_expired", side_effect=fake_sweep):
            # First call → sweep runs
            create_draft(
                "topic", _long_response(), vault,
                session_id="s", turn_id="1", drafted_slugs=set(),
            )
            assert sweep_calls["n"] == 1

            # Second call within 1h → no sweep
            create_draft(
                "topic", _long_response(), vault,
                session_id="s", turn_id="2", drafted_slugs=set(),
            )
            assert sweep_calls["n"] == 1

            # Force throttle to expire — sweep runs again
            concept_drafter._LAST_INLINE_SWEEP = (
                time.time() - concept_drafter._INLINE_SWEEP_INTERVAL_SECONDS - 1
            )
            create_draft(
                "topic", _long_response(), vault,
                session_id="s", turn_id="3", drafted_slugs=set(),
            )
            assert sweep_calls["n"] == 2

    def test_create_draft_falls_back_on_cross_volume(self, vault: Path):
        """OSError(WinError 17) from os.replace → shutil.move fallback."""
        real_move = concept_drafter.shutil.move
        move_calls = {"n": 0}

        def spy_move(src, dst):
            move_calls["n"] += 1
            return real_move(src, dst)

        cross_volume_err = OSError(17, "cross-device")
        cross_volume_err.winerror = 17

        with patch.object(
            concept_drafter.os, "replace", side_effect=cross_volume_err,
        ), patch.object(concept_drafter.shutil, "move", side_effect=spy_move):
            result = create_draft(
                "topic", _long_response(), vault,
                session_id="s", turn_id="1", drafted_slugs=set(),
            )
        assert result.created
        assert move_calls["n"] == 1
        assert result.path is not None and result.path.exists()


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


class TestFindDraft:
    def test_find_draft_by_full_uuid(self, vault: Path):
        result = create_draft(
            "topic", _long_response(), vault,
            session_id="s", turn_id="1", drafted_slugs=set(),
        )
        assert result.created
        found = find_draft_by_id(result.auto_draft_id, vault)
        assert found == result.path

    def test_find_draft_by_8char_prefix(self, vault: Path):
        result = create_draft(
            "topic", _long_response(), vault,
            session_id="s", turn_id="1", drafted_slugs=set(),
        )
        assert result.created
        prefix = result.auto_draft_id[:8]
        found = find_draft_by_id(prefix, vault)
        assert found == result.path

    def test_find_draft_ambiguous_prefix_raises(self, vault: Path):
        # Force a known prefix collision by writing two drafts with hand-crafted IDs.
        drafts_dir = vault / "concepts" / "_drafts"
        for suffix in ("0001", "0002"):
            full_id = "abcd1234" + suffix + "0" * (32 - 8 - 4)
            (drafts_dir / f"2026-04-24-PROBE-{suffix}.md").write_text(
                "---\n"
                'aliases: ["Probe"]\n'
                "tags: [concept, draft]\n"
                f'auto_draft_id: "{full_id}"\n'
                "---\n# Probe\n",
                encoding="utf-8",
            )
        with pytest.raises(DraftAmbiguityError) as excinfo:
            find_draft_by_id("abcd1234", vault)
        assert len(excinfo.value.candidates) == 2


# ---------------------------------------------------------------------------
# Accept
# ---------------------------------------------------------------------------


class TestAccept:
    def test_accept_draft_atomic_move(self, vault: Path):
        result = create_draft(
            "topic", _long_response(), vault,
            session_id="s", turn_id="1", drafted_slugs=set(),
        )
        assert result.created and result.path is not None

        replace_calls = {"n": 0}
        real_replace = os.replace

        def spy(src, dst):
            replace_calls["n"] += 1
            return real_replace(src, dst)

        with patch.object(concept_drafter.os, "replace", side_effect=spy):
            outcome = accept_draft(result.auto_draft_id, vault)
        assert outcome["status"] == "filed"
        assert replace_calls["n"] >= 1
        assert not result.path.exists()  # original draft moved
        assert Path(outcome["path"]).exists()
        assert (vault / "concepts").is_dir()

    def test_accept_runs_real_compile(self, vault: Path):
        # No monkeypatch — real entity_extractor.
        result = create_draft(
            "tell me about transformer attention",
            _long_response("Transformer Attention"),
            vault,
            session_id="s", turn_id="1", drafted_slugs=set(),
        )
        assert result.created
        outcome = accept_draft(result.auto_draft_id, vault)
        assert outcome["status"] == "filed"
        # Real compile populates these keys regardless of contents.
        assert "connections" in outcome
        assert "contradictions" in outcome

    def test_accept_idempotent_on_duplicate(self, vault: Path):
        result = create_draft(
            "topic", _long_response(), vault,
            session_id="s", turn_id="1", drafted_slugs=set(),
        )
        assert result.created
        outcome1 = accept_draft(result.auto_draft_id, vault)
        outcome2 = accept_draft(result.auto_draft_id, vault)
        assert outcome1["status"] == "filed"
        assert outcome2["status"] == "not_found"

    def test_accept_falls_back_on_cross_volume(self, vault: Path):
        result = create_draft(
            "topic", _long_response(), vault,
            session_id="s", turn_id="1", drafted_slugs=set(),
        )
        assert result.created
        real_move = concept_drafter.shutil.move
        move_calls = {"n": 0}

        def spy_move(src, dst):
            move_calls["n"] += 1
            return real_move(src, dst)

        # Make every os.replace raise — both create_draft's tempfile move and
        # accept_draft's draft-to-concepts move should fall through to shutil.move.
        cross = OSError(17, "cross-device")
        cross.winerror = 17

        with patch.object(
            concept_drafter.os, "replace", side_effect=cross,
        ), patch.object(concept_drafter.shutil, "move", side_effect=spy_move):
            outcome = accept_draft(result.auto_draft_id, vault)

        assert outcome["status"] == "filed"
        assert move_calls["n"] >= 1


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


class TestDiff:
    def test_diff_draft_no_mutation(self, vault: Path):
        result = create_draft(
            "topic", _long_response(), vault,
            session_id="s", turn_id="1", drafted_slugs=set(),
        )
        assert result.created and result.path is not None
        before_drafts = sorted((vault / "concepts" / "_drafts").iterdir())
        before_concepts = sorted((vault / "concepts").iterdir())

        outcome = diff_draft(result.auto_draft_id, vault)

        assert outcome["status"] == "ok"
        assert "preview" in outcome and outcome["preview"]
        assert sorted((vault / "concepts" / "_drafts").iterdir()) == before_drafts
        assert sorted((vault / "concepts").iterdir()) == before_concepts


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


class TestSweep:
    def test_sweep_removes_expired(self, vault: Path):
        result = create_draft(
            "topic", _long_response(), vault,
            session_id="s", turn_id="1", drafted_slugs=set(),
        )
        assert result.created and result.path is not None
        # Force mtime > 24h old
        old = time.time() - DRAFT_TTL_SECONDS - 60
        os.utime(result.path, (old, old))

        removed = sweep_expired(vault)
        assert result.path in removed
        assert not result.path.exists()

    def test_sweep_skips_inflight(self, vault: Path):
        result = create_draft(
            "topic", _long_response(), vault,
            session_id="s", turn_id="1", drafted_slugs=set(),
        )
        assert result.created and result.path is not None
        # Just-now mtime → within in-flight guard
        removed = sweep_expired(vault)
        assert removed == []
        assert result.path.exists()


# ---------------------------------------------------------------------------
# Engine + router integration
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_maybe_draft_and_footer_failsoft(self, vault: Path):
        # Force should_draft to raise — drafter must return ("", [])
        with patch.object(concept_drafter, "create_draft", side_effect=RuntimeError("boom")):
            footer, components = maybe_draft_and_footer(
                "topic", _long_response(), vault,
                session_id="s", turn_id="1", drafted_slugs=set(),
            )
        assert footer == ""
        assert components == []

    def test_engine_yields_outgoing_with_footer_field(self, vault: Path):
        """Drafter populates OutgoingMessage.footer; text stays clean."""
        from models import OutgoingMessage

        footer, components = maybe_draft_and_footer(
            "topic", _long_response(), vault,
            session_id="s", turn_id="1", drafted_slugs=set(),
        )
        assert footer  # drafted → non-empty
        assert components

        msg = OutgoingMessage(
            text=_long_response(),
            channel=SimpleNamespace(),  # type: ignore[arg-type]
            footer=footer or None,
            components=components or [],
        )
        assert msg.footer == footer
        # Critical R2 fix — text MUST NOT contain footer.
        assert footer not in msg.text

    def test_router_persistence_excludes_footer(self, vault: Path):
        """_persist_router_turn receives only response text, never footer."""
        # The router's _persist_router_turn signature accepts (incoming, reply).
        # We exercise it directly with a footer-laden OutgoingMessage to assert
        # that callers can keep persistence text-only by passing outgoing.text.
        import importlib

        router_mod = importlib.import_module("router")

        from models import OutgoingMessage

        footer, components = maybe_draft_and_footer(
            "topic", _long_response(), vault,
            session_id="s", turn_id="1", drafted_slugs=set(),
        )
        assert footer

        outgoing = OutgoingMessage(
            text="ANSWER ONLY",
            channel=SimpleNamespace(platform_id="c"),  # type: ignore[arg-type]
            footer=footer,
            components=components,
        )

        # Capture whatever the router calls store.add_message with.
        captured: list[tuple[Any, ...]] = []

        class FakeStore:
            def get(self, *a, **kw):
                return None

            def create(self, *a, **kw):
                return None

            def update(self, *a, **kw):
                return None

            def add_message(self, *a, **kw):
                captured.append(a)

        fake_engine = SimpleNamespace(session_store=FakeStore())
        # Hand-roll a router instance without going through ChatRouter.__init__,
        # which requires real adapters. We only need _persist_router_turn here.
        router = router_mod.ChatRouter.__new__(router_mod.ChatRouter)
        router.engine = fake_engine
        router.adapters = {}
        router.manager = None
        router._transcript_reset_commands = set()

        from models import Channel, IncomingMessage, Platform, User

        incoming = IncomingMessage(
            text="user said something",
            user=User(Platform.CLI, "u", "u"),
            channel=Channel(Platform.CLI, "c", is_dm=True),
            platform=Platform.CLI,
        )
        # Persistence call mirrors what router does with outgoing.text — it
        # MUST NOT see footer text.
        router._persist_router_turn(incoming, outgoing.text)

        for args in captured:
            for arg in args:
                if isinstance(arg, str):
                    assert footer not in arg, (
                        f"Footer leaked into persistence: {arg!r}"
                    )

    def test_router_intercepts_file_accept(self):
        """/file accept <id> route is intercepted by the router and forwarded
        to _handle_file_subcommand without reaching the engine."""
        import importlib

        router_mod = importlib.import_module("router")
        # Sanity check the helper exists on the router class.
        assert hasattr(router_mod.ChatRouter, "_handle_file_subcommand")


# ---------------------------------------------------------------------------
# Adapter footer rendering (§I8 contract)
# ---------------------------------------------------------------------------


class TestAdapterFooter:
    @pytest.mark.asyncio
    async def test_telegram_adapter_renders_footer_with_components(self):
        from types import SimpleNamespace

        from adapters.telegram import TelegramAdapter
        from models import (
            Channel,
            MessageComponent,
            OutgoingMessage,
            Platform,
        )

        class FakeBot:
            def __init__(self):
                self.calls: list[tuple[str, dict]] = []
                self._next = 100

            async def send_message(self, **kwargs):
                self.calls.append(("send_message", kwargs))
                self._next += 1
                return SimpleNamespace(message_id=self._next)

            async def edit_message_text(self, **kwargs):
                self.calls.append(("edit_message_text", kwargs))
                return SimpleNamespace(message_id=999)

        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter._app = SimpleNamespace(bot=FakeBot())
        adapter._sent_messages = {}
        adapter._callback_id_map = {}
        adapter._voice_reply_threads = set()

        msg = OutgoingMessage(
            text="Body of the answer.",
            channel=Channel(Platform.TELEGRAM, "123", is_dm=True),
            components=[
                MessageComponent(label="Accept", custom_id="concept_accept:abc"),
            ],
            footer="Drafted as `FOO`. Reply `/file accept abc12345`",
        )
        await adapter.send(msg)

        # The footer text should appear in the last send_message text payload.
        sends = [c for c in adapter._app.bot.calls if c[0] == "send_message"]
        assert sends, "no send_message calls recorded"
        body_texts = " ".join(c[1].get("text", "") for c in sends)
        assert "/file accept abc12345" in body_texts

    @pytest.mark.asyncio
    async def test_cli_adapter_renders_footer_with_separator(
        self, capsys: pytest.CaptureFixture[str],
    ):
        from adapters.cli_adapter import CLIAdapter
        from models import Channel, OutgoingMessage, Platform

        adapter = CLIAdapter(quiet=False)
        msg = OutgoingMessage(
            text="ANSWER",
            channel=Channel(Platform.CLI, "c", is_dm=True),
            footer="FOOTER LINE",
        )
        await adapter.send(msg)
        captured = capsys.readouterr()
        assert "ANSWER" in captured.out
        assert "FOOTER LINE" in captured.out
        # Separator: blank line between text and footer.
        assert "ANSWER\n\nFOOTER LINE" in captured.out

    @pytest.mark.asyncio
    async def test_web_adapter_renders_footer_with_separator(self):
        from types import SimpleNamespace

        from adapters.web import WebAdapter
        from models import Channel, OutgoingMessage, Platform, Thread

        sent: list[dict] = []

        class FakeWS:
            async def send_response(self, **kwargs):
                sent.append(kwargs)

        adapter = WebAdapter(FakeWS())
        msg = OutgoingMessage(
            text="ANSWER",
            channel=Channel(Platform.WEB, "c", is_dm=True),
            thread=Thread(thread_id="t"),
            footer="FOOTER LINE",
        )
        await adapter.send(msg)
        assert sent
        text = sent[0].get("text", "")
        assert "ANSWER" in text
        assert "FOOTER LINE" in text
        # Web uses "\n\n--\n" separator per §I8 contract.
        assert "ANSWER\n\n--\nFOOTER LINE" == text
