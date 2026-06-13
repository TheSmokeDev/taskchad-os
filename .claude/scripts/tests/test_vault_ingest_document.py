"""Phase 3 (doc-upload-truthful-reads): explicit /vault-ingest document ingest.

Covers the default-deny caption trigger, the router handler's reply/persist
contract, the REAL preserve_raw → companion → extract → compile pipeline
(no pipeline faking), partial-state honesty, and preserve_raw's central
dest_name sanitization.

All vault writes land in tmp_path vaults — never the real vault/memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

import entity_extractor
from entity_extractor import CompilationReport, _today, preserve_raw
from models import (
    Attachment,
    Channel,
    IncomingMessage,
    OutgoingMessage,
    Platform,
    User,
)
from router import ChatRouter, _display_filename
from session import SQLiteSessionStore


class _RecordingEngine:
    """Engine stub that records invocations — proves fall-through."""

    def __init__(self, session_store=None) -> None:
        self.session_store = session_store
        self.calls: list[IncomingMessage] = []

    async def handle_message(self, incoming: IncomingMessage, progress: dict[str, Any]):
        self.calls.append(incoming)
        yield OutgoingMessage(
            text="engine reply", channel=incoming.channel, thread=incoming.thread
        )


class _NoopManager:
    command_regex = re.compile(r"^/(\w+)\b\s*(.*)$")

    def get_router_commands(self) -> dict[str, Any]:
        return {}

    def get_all_command_names(self) -> list[str]:
        return ["noop"]

    def detect_intents(self, text: str) -> list[str]:
        return []

    def wants_analysis(self, text: str) -> bool:
        return False


class _CaptureAdapter:
    platform = Platform.CLI

    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []
        self.updates: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        return f"sent-{len(self.sent)}"

    async def update(self, message: OutgoingMessage) -> str:
        self.updates.append(message)
        return message.update_message_id or f"updated-{len(self.updates)}"


def _incoming(
    text: str = "[User uploaded a document: notes.txt]",
    caption: str = "/vault-ingest",
    attachments: list[Attachment] | None = None,
) -> IncomingMessage:
    return IncomingMessage(
        text=text,
        user=User(platform=Platform.CLI, platform_id="user-1"),
        channel=Channel(platform=Platform.CLI, platform_id="test-channel", is_dm=True),
        platform=Platform.CLI,
        attachments=attachments if attachments is not None else [],
        caption=caption,
    )


def _router_with_store(tmp_path: Path) -> tuple[ChatRouter, _RecordingEngine, SQLiteSessionStore]:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    engine = _RecordingEngine(store)
    router = ChatRouter(engine, _NoopManager())  # type: ignore[arg-type]
    return router, engine, store


# Fixture content with eligible entities: H1/H2 headings extract at
# confidence 0.7, above the 0.6 compile threshold (entity_extractor.py).
_FIXTURE_TEXT = (
    "# Quantum Mesh Routing\n"
    "\n"
    "Quantum Mesh Routing is a fictional technique for adaptive packet "
    "steering across unstable links.\n"
    "\n"
    "## Phase Drift Compensation\n"
    "\n"
    "Phase Drift Compensation keeps mesh clocks aligned when nodes disagree "
    "about time.\n"
)


@pytest.fixture
def tmp_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the pipeline's vault to tmp_path and isolate side channels.

    The pipeline resolves config.MEMORY_DIR at CALL time (local import inside
    the staticmethod), so the module-attribute patch is visible. Patching
    recall_service.reindex_file is environment ISOLATION, not pipeline
    faking: compile_entities' best-effort reindex step lazily imports it and
    would otherwise open the REAL memory.db and load the embedding model.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    import config

    monkeypatch.setattr(config, "MEMORY_DIR", vault)
    import recall_service

    monkeypatch.setattr(recall_service, "reindex_file", lambda *a, **k: 0)
    return vault


# ---------------------------------------------------------------------------
# Default-deny trigger behavior
# ---------------------------------------------------------------------------


class TestDefaultDenyTrigger:
    @pytest.mark.asyncio
    async def test_caption_command_invokes_pipeline_and_persists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        router, engine, store = _router_with_store(tmp_path)
        adapter = _CaptureAdapter()

        calls: list[tuple[Any, ...]] = []
        report = CompilationReport(
            pages_created=["c1"], connections_created=["x1"]
        )

        def fake_pipeline(file_path, filename, mimetype):
            calls.append((file_path, filename, mimetype))
            return tmp_path / "raw" / "uploads" / "notes.txt", report

        monkeypatch.setattr(router, "_document_ingest_pipeline", fake_pipeline)

        incoming = _incoming(
            attachments=[
                Attachment(
                    filename="notes.txt",
                    mimetype="text/plain",
                    url=str(tmp_path / "src.txt"),
                )
            ]
        )
        await router._handle_inner(adapter, incoming)

        assert calls == [(str(tmp_path / "src.txt"), "notes.txt", "text/plain")]
        assert engine.calls == []
        assert adapter.sent[0].text == "Ingesting notes.txt..."
        final = adapter.sent[-1]
        assert "Ingested 'notes.txt'" in final.text
        assert "1 concepts, 1 connections, 0 contradictions" in final.text
        assert "Raw: notes.txt." in final.text
        assert final.is_error is False
        messages = store.list_messages("cli:test-channel:test-channel")
        assert [m.role for m in messages] == ["user", "assistant"]
        assert "Ingested 'notes.txt'" in messages[1].content

    @pytest.mark.asyncio
    async def test_prose_caption_falls_through_to_engine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        router, engine, _store = _router_with_store(tmp_path)
        adapter = _CaptureAdapter()

        def forbidden(*args, **kwargs):
            raise AssertionError("pipeline must not run for prose captions")

        monkeypatch.setattr(router, "_document_ingest_pipeline", forbidden)

        incoming = _incoming(
            caption="please vault-ingest this later",
            attachments=[Attachment(filename="notes.txt", mimetype="text/plain", url="x")],
        )
        await router._handle_inner(adapter, incoming)

        assert len(engine.calls) == 1

    @pytest.mark.asyncio
    async def test_captionless_upload_falls_through_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        router, engine, _store = _router_with_store(tmp_path)
        adapter = _CaptureAdapter()

        def forbidden(*args, **kwargs):
            raise AssertionError("pipeline must not run for caption-less uploads")

        monkeypatch.setattr(router, "_document_ingest_pipeline", forbidden)

        incoming = _incoming(
            caption="",
            attachments=[Attachment(filename="notes.txt", mimetype="text/plain", url="x")],
        )
        await router._handle_inner(adapter, incoming)

        assert len(engine.calls) == 1

    @pytest.mark.asyncio
    async def test_bare_command_text_without_attachments_falls_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`/vault-ingest` sent as TEXT after an upload must not ingest —
        no retroactive state tracking."""
        router, _engine, _store = _router_with_store(tmp_path)
        adapter = _CaptureAdapter()

        called = {"hit": False}

        def forbidden(*args, **kwargs):
            called["hit"] = True

        monkeypatch.setattr(router, "_document_ingest_pipeline", forbidden)

        incoming = _incoming(text="/vault-ingest", caption="", attachments=[])
        try:
            await router._handle_inner(adapter, incoming)
        except Exception:
            pass

        assert called["hit"] is False

    @pytest.mark.asyncio
    async def test_caption_is_whitespace_and_case_tolerant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        router, engine, _store = _router_with_store(tmp_path)
        adapter = _CaptureAdapter()

        calls: list[Any] = []

        def fake_pipeline(file_path, filename, mimetype):
            calls.append(filename)
            return tmp_path / "notes.txt", CompilationReport()

        monkeypatch.setattr(router, "_document_ingest_pipeline", fake_pipeline)

        incoming = _incoming(
            caption="  /VAULT-INGEST  ",
            attachments=[Attachment(filename="notes.txt", mimetype="text/plain", url="x")],
        )
        await router._handle_inner(adapter, incoming)

        assert calls == ["notes.txt"]
        assert engine.calls == []

    @pytest.mark.asyncio
    async def test_caption_with_trailing_args_falls_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Caption must be EXACTLY the command — `/vault-ingest now` is prose."""
        router, engine, _store = _router_with_store(tmp_path)
        adapter = _CaptureAdapter()

        def forbidden(*args, **kwargs):
            raise AssertionError("pipeline must not run for caption with args")

        monkeypatch.setattr(router, "_document_ingest_pipeline", forbidden)

        incoming = _incoming(
            caption="/vault-ingest now",
            attachments=[Attachment(filename="notes.txt", mimetype="text/plain", url="x")],
        )
        await router._handle_inner(adapter, incoming)

        assert len(engine.calls) == 1


# ---------------------------------------------------------------------------
# Unsupported attachments — explicit per-file refusal, no silent skips
# ---------------------------------------------------------------------------


class TestUnsupportedAttachments:
    @pytest.mark.asyncio
    async def test_unsupported_file_gets_refusal_and_pipeline_not_called(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        router, engine, store = _router_with_store(tmp_path)
        adapter = _CaptureAdapter()

        def forbidden(*args, **kwargs):
            raise AssertionError("pipeline must not run for unsupported files")

        monkeypatch.setattr(router, "_document_ingest_pipeline", forbidden)

        incoming = _incoming(
            attachments=[Attachment(filename="evil.png", mimetype="image/png", url="x")]
        )
        await router._handle_inner(adapter, incoming)

        assert engine.calls == []
        # No supported files → no "Ingesting..." placeholder; one refusal reply.
        assert len(adapter.sent) == 1
        final = adapter.sent[0]
        assert "Cannot ingest 'evil.png'" in final.text
        assert "unsupported document type" in final.text
        assert final.is_error is True
        messages = store.list_messages("cli:test-channel:test-channel")
        assert "Cannot ingest 'evil.png'" in messages[1].content

    @pytest.mark.asyncio
    async def test_mixed_batch_refuses_unsupported_and_ingests_supported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        router, _engine, _store = _router_with_store(tmp_path)
        adapter = _CaptureAdapter()

        def fake_pipeline(file_path, filename, mimetype):
            return tmp_path / "notes.txt", CompilationReport(pages_created=["c1"])

        monkeypatch.setattr(router, "_document_ingest_pipeline", fake_pipeline)

        incoming = _incoming(
            attachments=[
                Attachment(filename="evil.png", mimetype="image/png", url="x"),
                Attachment(filename="notes.txt", mimetype="text/plain", url="y"),
            ]
        )
        await router._handle_inner(adapter, incoming)

        # Placeholder names ONLY the supported file.
        assert adapter.sent[0].text == "Ingesting notes.txt..."
        final = adapter.sent[-1]
        lines = final.text.splitlines()
        # Reply lines follow attachment order: refusal first, then success.
        assert "Cannot ingest 'evil.png'" in lines[0]
        assert "Ingested 'notes.txt'" in lines[1]
        # Mixed outcome still flags the turn as an error (a refusal occurred).
        assert final.is_error is True


# ---------------------------------------------------------------------------
# REAL pipeline — no pipeline faking (R1 M5), tmp vault, both formats
# ---------------------------------------------------------------------------


class TestRealPipeline:
    def test_real_txt_compiles_companion_never_raw(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        staged = staging / "unique-123_strategy-notes.txt"
        staged.write_text(_FIXTURE_TEXT, encoding="utf-8")
        src_sha = hashlib.sha256(staged.read_bytes()).hexdigest()

        raw_path, report = ChatRouter._document_ingest_pipeline(
            str(staged), "strategy-notes.txt", "text/plain"
        )

        # Raw archived under raw/uploads/ with the ORIGINAL filename.
        assert raw_path == tmp_vault / "raw" / "uploads" / "strategy-notes.txt"
        assert raw_path.exists()
        # Raw byte-identity across the FULL run (immutable-raw contract).
        assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == src_sha
        # Companion sits beside it with gate-passing Homie frontmatter.
        companion = tmp_vault / "raw" / "uploads" / "strategy-notes.ingest.md"
        assert companion.exists()
        comp_text = companion.read_text(encoding="utf-8")
        assert comp_text.startswith("---\n")
        assert "tags: [upload, auto-ingested]" in comp_text
        assert re.search(r"^date: \d{4}-\d{2}-\d{2}$", comp_text, re.MULTILINE)
        assert "related:" in comp_text
        assert "source: strategy-notes.txt" in comp_text
        # Entities actually compiled — not a gas-station pass.
        assert report.entities_processed > 0
        assert report.pages_created
        # The compile surface was the COMPANION: concept pages reference
        # [[strategy-notes.ingest]], never the raw stem.
        concept_texts = [
            Path(p).read_text(encoding="utf-8") for p in report.pages_created
        ]
        assert any("[[strategy-notes.ingest]]" in t for t in concept_texts)
        assert all("[[strategy-notes]]" not in t for t in concept_texts)

    def test_real_md_without_frontmatter_ingests(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        """R1 B2: an uploaded .md WITHOUT frontmatter must ingest — the
        generated companion carries the frontmatter; raw is never compiled."""
        staging = tmp_path / "staging"
        staging.mkdir()
        staged = staging / "unique-9_field-notes.md"
        staged.write_text(_FIXTURE_TEXT, encoding="utf-8")
        src_sha = hashlib.sha256(staged.read_bytes()).hexdigest()

        raw_path, report = ChatRouter._document_ingest_pipeline(
            str(staged), "field-notes.md", "text/markdown"
        )

        assert raw_path == tmp_vault / "raw" / "uploads" / "field-notes.md"
        # Raw .md stays byte-identical — compile never touched it (R1 B2).
        assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == src_sha
        companion = tmp_vault / "raw" / "uploads" / "field-notes.ingest.md"
        assert companion.exists()
        comp_text = companion.read_text(encoding="utf-8")
        assert "tags: [upload, auto-ingested]" in comp_text
        assert re.search(r"^date: \d{4}-\d{2}-\d{2}$", comp_text, re.MULTILINE)
        assert "related:" in comp_text
        assert report.entities_processed > 0
        assert report.pages_created
        concept_texts = [
            Path(p).read_text(encoding="utf-8") for p in report.pages_created
        ]
        assert any("[[field-notes.ingest]]" in t for t in concept_texts)
        assert all("[[field-notes]]" not in t for t in concept_texts)


# ---------------------------------------------------------------------------
# Failure honesty shapes (R1 M4)
# ---------------------------------------------------------------------------


class TestFailureHonesty:
    @pytest.mark.asyncio
    async def test_partial_failure_names_raw_and_states_not_compiled(
        self, tmp_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Real preserve_raw, raising compile → shape 2: name the archived
        raw file AND state concepts were NOT compiled. Never total failure."""
        router, _engine, store = _router_with_store(tmp_path)
        adapter = _CaptureAdapter()

        def explode(*args, **kwargs):
            raise RuntimeError("compile blew up")

        # The pipeline's local `from entity_extractor import compile_entities`
        # binds at CALL time, so the module-attribute patch is visible.
        monkeypatch.setattr(entity_extractor, "compile_entities", explode)

        staged = tmp_path / "unique-77_notes.txt"
        staged.write_text(_FIXTURE_TEXT, encoding="utf-8")
        incoming = _incoming(
            attachments=[
                Attachment(filename="notes.txt", mimetype="text/plain", url=str(staged))
            ]
        )
        await router._handle_inner(adapter, incoming)

        final = adapter.sent[-1]
        assert final.is_error is True
        assert "Raw file archived as 'notes.txt'" in final.text
        assert "concept compilation FAILED (RuntimeError)" in final.text
        assert "No concept pages were created or updated" in final.text
        # Never the nothing-saved shape when the raw copy exists.
        assert "Nothing was saved" not in final.text
        # Raw IS on disk.
        assert (tmp_vault / "raw" / "uploads" / "notes.txt").exists()
        # Persisted as the audit row.
        messages = store.list_messages("cli:test-channel:test-channel")
        assert "Raw file archived as 'notes.txt'" in messages[1].content

    @pytest.mark.asyncio
    async def test_missing_attachment_url_nothing_saved(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        router, _engine, store = _router_with_store(tmp_path)
        adapter = _CaptureAdapter()

        incoming = _incoming(
            attachments=[Attachment(filename="ghost.txt", mimetype="text/plain", url=None)]
        )
        await router._handle_inner(adapter, incoming)

        final = adapter.sent[-1]
        assert final.is_error is True
        assert "Ingest of 'ghost.txt' FAILED" in final.text
        assert "Nothing was saved to the vault" in final.text
        assert "Re-send it with the /vault-ingest caption" in final.text
        # NOTHING landed under the tmp vault raw/ tree.
        raw_tree = tmp_vault / "raw"
        files = [p for p in raw_tree.rglob("*") if p.is_file()] if raw_tree.exists() else []
        assert files == []
        messages = store.list_messages("cli:test-channel:test-channel")
        assert "Nothing was saved to the vault" in messages[1].content

    @pytest.mark.asyncio
    async def test_unreadable_attachment_url_nothing_saved(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        router, _engine, _store = _router_with_store(tmp_path)
        adapter = _CaptureAdapter()

        incoming = _incoming(
            attachments=[
                Attachment(
                    filename="ghost.txt",
                    mimetype="text/plain",
                    url=str(tmp_path / "does-not-exist.txt"),
                )
            ]
        )
        await router._handle_inner(adapter, incoming)

        final = adapter.sent[-1]
        assert final.is_error is True
        assert "Ingest of 'ghost.txt' FAILED (FileNotFoundError)" in final.text
        assert "Nothing was saved to the vault" in final.text
        raw_tree = tmp_vault / "raw"
        files = [p for p in raw_tree.rglob("*") if p.is_file()] if raw_tree.exists() else []
        assert files == []


# ---------------------------------------------------------------------------
# preserve_raw dest_name — central sanitization (R1 M3 + R2 NM3)
# ---------------------------------------------------------------------------


class TestPreserveRawDestName:
    def _vault_and_src(self, tmp_path: Path, body: str = "body") -> tuple[Path, Path]:
        vault = tmp_path / "vault"
        vault.mkdir(exist_ok=True)
        src = tmp_path / "unique-123_staged.txt"
        src.write_text(body, encoding="utf-8")
        return vault, src

    def test_dest_name_overrides_staged_filename(self, tmp_path: Path) -> None:
        vault, src = self._vault_and_src(tmp_path)

        dest = preserve_raw(src, vault, subdir="uploads", dest_name="notes.txt")

        assert dest == vault / "raw" / "uploads" / "notes.txt"
        assert dest.read_text(encoding="utf-8") == "body"

    def test_dest_name_none_keeps_source_name(self, tmp_path: Path) -> None:
        vault, src = self._vault_and_src(tmp_path)

        dest = preserve_raw(src, vault, subdir="uploads")

        assert dest == vault / "raw" / "uploads" / "unique-123_staged.txt"

    def test_dest_name_collision_falls_back_to_date_prefix(self, tmp_path: Path) -> None:
        vault, src = self._vault_and_src(tmp_path, body="second")
        (vault / "raw" / "uploads").mkdir(parents=True)
        (vault / "raw" / "uploads" / "notes.txt").write_text("first", encoding="utf-8")

        dest = preserve_raw(src, vault, subdir="uploads", dest_name="notes.txt")

        assert dest == vault / "raw" / "uploads" / f"{_today()}-notes.txt"
        assert dest.read_text(encoding="utf-8") == "second"
        # Original archive untouched.
        assert (vault / "raw" / "uploads" / "notes.txt").read_text(
            encoding="utf-8"
        ) == "first"

    def test_dest_name_double_collision_raises(self, tmp_path: Path) -> None:
        vault, src = self._vault_and_src(tmp_path, body="third")
        uploads = vault / "raw" / "uploads"
        uploads.mkdir(parents=True)
        (uploads / "notes.txt").write_text("first", encoding="utf-8")
        (uploads / f"{_today()}-notes.txt").write_text("second", encoding="utf-8")

        with pytest.raises(FileExistsError):
            preserve_raw(src, vault, subdir="uploads", dest_name="notes.txt")

    @pytest.mark.parametrize(
        "evil",
        ["../../evil.md", "..\\..\\evil.md", "a/b/../evil.md", "C:\\temp\\evil.md"],
    )
    def test_traversal_dest_name_stays_inside_uploads(
        self, tmp_path: Path, evil: str
    ) -> None:
        vault, src = self._vault_and_src(tmp_path)

        dest = preserve_raw(src, vault, subdir="uploads", dest_name=evil)

        assert dest.parent == vault / "raw" / "uploads"
        assert dest.name == "evil.md"
        assert dest.exists()
        # Nothing escaped the uploads tree.
        assert not (tmp_path / "evil.md").exists()
        assert not (vault / "evil.md").exists()

    def test_control_chars_and_crlf_stripped(self, tmp_path: Path) -> None:
        vault, src = self._vault_and_src(tmp_path)

        dest = preserve_raw(
            src, vault, subdir="uploads", dest_name="ev\ril\n.md"
        )

        assert dest.name == "evil.md"

    @pytest.mark.parametrize("bad", ["", "   ", "...", "\r\n", " . . "])
    def test_empty_after_sanitization_raises(self, tmp_path: Path, bad: str) -> None:
        vault, src = self._vault_and_src(tmp_path)

        with pytest.raises(ValueError):
            preserve_raw(src, vault, subdir="uploads", dest_name=bad)

        # Refused BEFORE any filesystem mutation — raw/ never created.
        assert not (vault / "raw").exists()

    def test_windows_invalid_chars_replaced(self, tmp_path: Path) -> None:
        vault, src = self._vault_and_src(tmp_path)

        dest = preserve_raw(src, vault, subdir="uploads", dest_name="foo:bar?.md")

        assert dest.name == "foo_bar_.md"

    @pytest.mark.parametrize(
        ("reserved", "expected"),
        [("CON.md", "_CON.md"), ("nul.txt", "_nul.txt"), ("com1.csv", "_com1.csv")],
    )
    def test_reserved_device_basename_prefixed(
        self, tmp_path: Path, reserved: str, expected: str
    ) -> None:
        vault, src = self._vault_and_src(tmp_path)

        dest = preserve_raw(src, vault, subdir="uploads", dest_name=reserved)

        assert dest.name == expected


# ---------------------------------------------------------------------------
# Post-build F1 — burst coalescing must never widen or drop the caption gate.
# These drive the REAL production queue path (_queue_incoming → burst flush →
# _merge_incoming_batch → _handle_serialized), which direct _handle_inner
# tests are blind to.
# ---------------------------------------------------------------------------


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    """Poll until predicate() is true or timeout — assertions do the failing."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)


class TestBurstCoalescingGate:
    def _attachment(self, tmp_path: Path, name: str) -> Attachment:
        staged = tmp_path / f"staged-{name}"
        staged.write_text("body", encoding="utf-8")
        return Attachment(filename=name, mimetype="text/plain", url=str(staged))

    def _fake_pipeline(self, tmp_path: Path, calls: list[str]):
        def fake(file_path, filename, mimetype):
            calls.append(filename)
            return (
                tmp_path / "raw" / "uploads" / filename,
                CompilationReport(pages_created=["c1"]),
            )

        return fake

    @pytest.mark.asyncio
    async def test_captioned_ingest_then_captionless_burst_never_widens_consent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A captioned /vault-ingest doc followed by a captionless doc within
        the burst window must ingest EXACTLY the captioned file; the
        captionless upload falls through to the engine as its own turn.
        Pre-fix: both coalesced, the merged turn kept the first caption, and
        the bystander file was ingested without consent."""
        router, engine, _store = _router_with_store(tmp_path)
        router._burst_delay_seconds = 0.5
        adapter = _CaptureAdapter()
        calls: list[str] = []
        monkeypatch.setattr(
            router, "_document_ingest_pipeline", self._fake_pipeline(tmp_path, calls)
        )

        captioned = _incoming(
            text="[User uploaded a document: consented.txt]",
            caption="/vault-ingest",
            attachments=[self._attachment(tmp_path, "consented.txt")],
        )
        captionless = _incoming(
            text="[User uploaded a document: drive-by.txt]",
            caption="",
            attachments=[self._attachment(tmp_path, "drive-by.txt")],
        )

        router._queue_incoming(adapter, captioned)
        router._queue_incoming(adapter, captionless)
        await _wait_until(lambda: bool(engine.calls))

        # Exactly ONE file ingested — the explicitly captioned one.
        assert calls == ["consented.txt"]
        assert any("Ingested 'consented.txt'" in m.text for m in adapter.sent)
        # The captionless upload reached the engine as its own turn, with
        # ONLY its own attachment and no fabricated caption.
        assert len(engine.calls) == 1
        assert [a.filename for a in engine.calls[0].attachments] == ["drive-by.txt"]
        assert (getattr(engine.calls[0], "caption", "") or "") == ""

    @pytest.mark.asyncio
    async def test_captionless_burst_then_captioned_ingest_still_fires(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reverse order: a captionless doc queued first must not swallow a
        captioned /vault-ingest doc arriving within the burst window.
        Pre-fix: both coalesced, the merged turn kept the FIRST (empty)
        caption, and the intended ingest was silently dropped."""
        router, engine, _store = _router_with_store(tmp_path)
        router._burst_delay_seconds = 0.5
        adapter = _CaptureAdapter()
        calls: list[str] = []
        monkeypatch.setattr(
            router, "_document_ingest_pipeline", self._fake_pipeline(tmp_path, calls)
        )

        captionless = _incoming(
            text="[User uploaded a document: drive-by.txt]",
            caption="",
            attachments=[self._attachment(tmp_path, "drive-by.txt")],
        )
        captioned = _incoming(
            text="[User uploaded a document: consented.txt]",
            caption="/vault-ingest",
            attachments=[self._attachment(tmp_path, "consented.txt")],
        )

        router._queue_incoming(adapter, captionless)
        router._queue_incoming(adapter, captioned)
        await _wait_until(lambda: bool(engine.calls) and bool(calls))

        # The ingest still fired — for its own file ONLY.
        assert calls == ["consented.txt"]
        assert any("Ingested 'consented.txt'" in m.text for m in adapter.sent)
        # The captionless upload still reached the engine separately.
        assert len(engine.calls) == 1
        assert [a.filename for a in engine.calls[0].attachments] == ["drive-by.txt"]

    def test_captioned_ingest_upload_is_not_coalescible(self) -> None:
        att = Attachment(filename="notes.txt", mimetype="text/plain", url="x")
        # The caption-borne command gets the slash-command bypass...
        assert (
            ChatRouter._can_coalesce(
                _incoming(caption="/vault-ingest", attachments=[att])
            )
            is False
        )
        # ...but ordinary uploads and prose captions still coalesce.
        assert (
            ChatRouter._can_coalesce(_incoming(caption="", attachments=[att])) is True
        )
        assert (
            ChatRouter._can_coalesce(
                _incoming(caption="please vault-ingest later", attachments=[att])
            )
            is True
        )
        # Command caption WITHOUT attachments is an ordinary text turn.
        assert (
            ChatRouter._can_coalesce(
                _incoming(caption="/vault-ingest", attachments=[])
            )
            is True
        )

    def test_merge_never_fabricates_consent_on_mixed_captions(self) -> None:
        """Defensive invariant: the merged caption survives ONLY when every
        constituent carried the identical non-empty caption."""
        att_a = Attachment(filename="a.txt", mimetype="text/plain", url="x")
        att_b = Attachment(filename="b.txt", mimetype="text/plain", url="y")

        # Captioned first, captionless second → merged caption cleared.
        merged = ChatRouter._merge_incoming_batch(
            [
                _incoming(text="one", caption="/vault-ingest", attachments=[att_a]),
                _incoming(text="two", caption="", attachments=[att_b]),
            ]
        )
        assert merged.caption == ""
        assert len(merged.attachments) == 2

        # Captionless first, captioned second → also cleared.
        merged = ChatRouter._merge_incoming_batch(
            [
                _incoming(text="one", caption="", attachments=[att_a]),
                _incoming(text="two", caption="/vault-ingest", attachments=[att_b]),
            ]
        )
        assert merged.caption == ""

        # Differing non-empty captions → cleared.
        merged = ChatRouter._merge_incoming_batch(
            [
                _incoming(text="one", caption="alpha"),
                _incoming(text="two", caption="beta"),
            ]
        )
        assert merged.caption == ""

    def test_merge_keeps_caption_only_when_identical_on_all(self) -> None:
        merged = ChatRouter._merge_incoming_batch(
            [
                _incoming(text="one", caption="same caption"),
                _incoming(text="two", caption="same caption"),
            ]
        )
        assert merged.caption == "same caption"


# ---------------------------------------------------------------------------
# Post-build F3 — user-controlled filenames echoed into Markdown-parsed replies
# ---------------------------------------------------------------------------


def _assert_no_unescaped_markdown(text: str) -> None:
    for m in re.finditer(r"[`*\[\]_]", text):
        assert m.start() > 0 and text[m.start() - 1] == "\\", (
            f"unescaped Markdown token {text[m.start()]!r} at {m.start()}: {text!r}"
        )


class TestDisplayFilenameSafety:
    def test_display_filename_unit(self) -> None:
        out = _display_filename("*pwn*\n[link](x)`code`.txt")
        assert "\n" not in out and "\r" not in out
        assert "\\*pwn\\*" in out
        assert "\\[link\\]" in out
        assert "\\`code\\`" in out

        # Control chars (incl. DEL) stripped; whitespace collapsed to one line.
        assert _display_filename("a\x01b\x7fc\r\nd e") == "abcd e"
        # Length cap with ellipsis.
        capped = _display_filename("a" * 100)
        assert len(capped) == 80
        assert capped.endswith("...")
        # Empty / None / all-control falls back to the generic label.
        assert _display_filename("") == "attachment"
        assert _display_filename(None) == "attachment"
        assert _display_filename("\x01\x02") == "attachment"

    @pytest.mark.asyncio
    async def test_evil_filename_cannot_spoof_refusal_reply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A filename carrying newlines + Markdown + a fake status line must
        not alter reply structure: one refusal line, every Markdown token
        escaped, no injected 'Ingested' line."""
        router, _engine, _store = _router_with_store(tmp_path)
        adapter = _CaptureAdapter()

        def forbidden(*args, **kwargs):
            raise AssertionError("pipeline must not run for unsupported files")

        monkeypatch.setattr(router, "_document_ingest_pipeline", forbidden)

        evil = "*pwn*\nIngested 'fake.txt'. 9 concepts\n[link](x)`code`.png"
        incoming = _incoming(
            attachments=[Attachment(filename=evil, mimetype="image/png", url="x")]
        )
        await router._handle_inner(adapter, incoming)

        final = adapter.sent[-1]
        assert final.is_error is True
        # Structure: exactly one line, the refusal — injected newlines gone.
        assert "\n" not in final.text
        assert final.text.startswith("Cannot ingest '")
        _assert_no_unescaped_markdown(final.text)
        # The spoofed status line cannot start a line of its own.
        assert not final.text.startswith("Ingested")

    @pytest.mark.asyncio
    async def test_evil_supported_filename_escaped_everywhere_raw_arg_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Display vs storage separation: replies escape the filename at the
        placeholder AND success sites, while the pipeline still receives the
        RAW filename (preserve_raw owns storage sanitization)."""
        router, engine, _store = _router_with_store(tmp_path)
        adapter = _CaptureAdapter()

        calls: list[tuple[Any, ...]] = []

        def fake_pipeline(file_path, filename, mimetype):
            calls.append((file_path, filename, mimetype))
            return (
                tmp_path / "raw" / "uploads" / "ev_il_name_x_.txt",
                CompilationReport(pages_created=["c1"]),
            )

        monkeypatch.setattr(router, "_document_ingest_pipeline", fake_pipeline)

        evil = "ev*il_[name]`x`.txt"
        escaped = "ev\\*il\\_\\[name\\]\\`x\\`.txt"
        incoming = _incoming(
            attachments=[
                Attachment(filename=evil, mimetype="text/plain", url="staged")
            ]
        )
        await router._handle_inner(adapter, incoming)

        # Pipeline got the RAW name — storage sanitization is preserve_raw's.
        assert calls == [("staged", evil, "text/plain")]
        assert engine.calls == []
        # Placeholder and success reply carry only the escaped display form.
        assert adapter.sent[0].text == f"Ingesting {escaped}..."
        final = adapter.sent[-1]
        assert "\n" not in final.text
        assert f"Ingested '{escaped}'" in final.text
        assert evil not in final.text
        _assert_no_unescaped_markdown(final.text)


# ---------------------------------------------------------------------------
# Post-build F2 — preserve_raw immutability under concurrent same-name uploads
# ---------------------------------------------------------------------------


class TestPreserveRawConcurrency:
    def test_racing_same_dest_name_lands_distinct_intact_archives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two threads racing the same sanitized filename with DIFFERENT
        contents must both land at DISTINCT paths with bytes intact.

        A barrier inside copy2 deterministically forces both threads into the
        copy stage simultaneously — pre-fix, both observed dest.exists() ==
        False, both copied to the SAME plain path, and the last writer
        overwrote the 'immutable' archive. Post-fix the exclusive-create
        claim makes the loser take the date-prefix fallback."""
        vault = tmp_path / "vault"
        vault.mkdir()
        src_a = tmp_path / "one.txt"
        src_a.write_text("CONTENT ONE", encoding="utf-8")
        src_b = tmp_path / "two.txt"
        src_b.write_text("CONTENT TWO - different bytes", encoding="utf-8")
        sha_a_src = hashlib.sha256(src_a.read_bytes()).hexdigest()
        sha_b_src = hashlib.sha256(src_b.read_bytes()).hexdigest()

        barrier = threading.Barrier(2, timeout=5)
        real_copy2 = shutil.copy2

        def synced_copy2(src, dst, **kwargs):
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass  # partner errored before copy — proceed, asserts decide
            return real_copy2(src, dst, **kwargs)

        monkeypatch.setattr(shutil, "copy2", synced_copy2)

        results: dict[str, Path] = {}
        errors: dict[str, BaseException] = {}

        def run(tag: str, src: Path) -> None:
            try:
                results[tag] = preserve_raw(
                    src, vault, subdir="uploads", dest_name="notes.txt"
                )
            except BaseException as e:  # noqa: BLE001 — recorded for assert
                errors[tag] = e

        t_a = threading.Thread(target=run, args=("a", src_a))
        t_b = threading.Thread(target=run, args=("b", src_b))
        t_a.start()
        t_b.start()
        t_a.join(timeout=10)
        t_b.join(timeout=10)

        assert not errors, f"unexpected errors: {errors}"
        # Distinct destinations: one plain, one date-prefixed fallback.
        assert results["a"] != results["b"]
        assert {results["a"].name, results["b"].name} == {
            "notes.txt",
            f"{_today()}-notes.txt",
        }
        # Neither overwrote the other — each archive carries ITS thread's
        # exact bytes (sha-verified).
        assert hashlib.sha256(results["a"].read_bytes()).hexdigest() == sha_a_src
        assert hashlib.sha256(results["b"].read_bytes()).hexdigest() == sha_b_src
        # And the uploads tree contains exactly those two archives.
        uploads = vault / "raw" / "uploads"
        assert sorted(p.name for p in uploads.iterdir()) == sorted(
            {results["a"].name, results["b"].name}
        )
