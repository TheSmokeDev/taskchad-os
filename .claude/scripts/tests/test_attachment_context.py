from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from attachment_context import (
    build_attachment_context,
    extract_attachment_context,
    is_supported_document_attachment,
)
from models import Attachment


def test_text_attachment_context_is_bounded_and_hides_path(tmp_path: Path) -> None:
    # Phase 2: the default per-attachment cap is 100K chars, so a 12K fixture
    # no longer truncates by default — pass an explicit small cap to keep
    # exercising the bounded path.
    path = tmp_path / "notes.txt"
    path.write_text("alpha\n" * 2000, encoding="utf-8")

    context = build_attachment_context(
        [Attachment(filename="notes.txt", mimetype="text/plain", url=str(path))],
        max_chars=2000,
    )

    assert "## Attachment 1: notes.txt" in context
    assert "alpha" in context
    assert "[TRUNCATED: attachment content budget reached]" in context
    assert str(path) not in context


def test_csv_attachment_context_renders_rows(tmp_path: Path) -> None:
    path = tmp_path / "leads.csv"
    path.write_text("name,email\nAlice,a@example.com\nBob,b@example.com\n", encoding="utf-8")

    context = build_attachment_context([
        Attachment(filename="leads.csv", mimetype="text/csv", url=str(path))
    ])

    assert "name | email" in context
    assert "Alice | a@example.com" in context
    assert str(path) not in context


def test_docx_attachment_context_extracts_paragraphs(tmp_path: Path) -> None:
    path = tmp_path / "brief.docx"
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        "<w:p><w:r><w:t>First paragraph.</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Second paragraph.</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document_xml)

    context = build_attachment_context([
        Attachment(
            filename="brief.docx",
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            url=str(path),
        )
    ])

    assert "First paragraph." in context
    assert "Second paragraph." in context
    assert str(path) not in context


def test_pdf_attachment_context_extracts_text(tmp_path: Path) -> None:
    import fitz

    path = tmp_path / "report.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Quarterly report summary")
    doc.save(path)
    doc.close()

    context = build_attachment_context([
        Attachment(filename="report.pdf", mimetype="application/pdf", url=str(path))
    ])

    assert "[page 1]" in context
    assert "Quarterly report summary" in context
    assert str(path) not in context


def test_supported_document_detection_by_extension_and_mime() -> None:
    assert is_supported_document_attachment("report.pdf", "")
    assert is_supported_document_attachment("brief.bin", "application/pdf")
    assert is_supported_document_attachment("sheet.csv", "")
    assert is_supported_document_attachment("notes.txt", "")
    assert is_supported_document_attachment("brief.docx", "")
    assert not is_supported_document_attachment("photo.png", "image/png")


def test_caps_resolve_from_config_at_call_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 1 proof: mutating config.CHAT_ATTACHMENT_MAX_CHARS AFTER import
    moves the truncation point — no def-time binding anywhere in the chain.

    Patches the config MODULE ATTRIBUTE, never os.environ (config.py runs
    load_dotenv(override=True) at import, making env mutation a no-op).
    """
    import config

    path = tmp_path / "notes.txt"
    path.write_text("word " * 500, encoding="utf-8")  # 2,500 chars

    # Under the default 100K cap the file inlines fully.
    context_default = build_attachment_context([
        Attachment(filename="notes.txt", mimetype="text/plain", url=str(path))
    ])
    assert "[TRUNCATED: attachment content budget reached]" not in context_default
    assert "PARTIAL CONTENT" not in context_default

    # Shrink the cap on the live config module — the very next call must clip.
    monkeypatch.setattr(config, "CHAT_ATTACHMENT_MAX_CHARS", 256)

    context_small = build_attachment_context([
        Attachment(filename="notes.txt", mimetype="text/plain", url=str(path))
    ])
    assert "[TRUNCATED: attachment content budget reached]" in context_small
    assert "PARTIAL CONTENT: only the first" in context_small


def test_explicit_small_caps_thread_through_build(tmp_path: Path) -> None:
    """R1 M2 proof: build_attachment_context passes its resolved max_chars
    into extract_attachment_context — an explicit small cap clips the
    per-attachment content, not just the total budget."""
    path = tmp_path / "doc.txt"
    path.write_text("z" * 500, encoding="utf-8")

    context = build_attachment_context(
        [Attachment(filename="doc.txt", mimetype="text/plain", url=str(path))],
        max_chars=64,
    )

    assert "[TRUNCATED: attachment content budget reached]" in context
    assert "PARTIAL CONTENT: only the first" in context
    # 500 chars never reach the output under the 64-char cap.
    assert "z" * 100 not in context


def test_large_text_attachment_inlines_fully_under_default_cap(tmp_path: Path) -> None:
    """An 85,000-char document fits under the 100K default cap — fully
    inlined, no truncation marker, tail content present."""
    path = tmp_path / "big.txt"
    body = ("lorem ipsum " * 7_082).strip() + "\nTAIL-SENTINEL-END"  # ~85K chars
    assert 80_000 < len(body) < 100_000
    path.write_text(body, encoding="utf-8")

    context = build_attachment_context([
        Attachment(filename="big.txt", mimetype="text/plain", url=str(path))
    ])

    assert "[TRUNCATED" not in context
    assert "PARTIAL CONTENT" not in context
    assert "TAIL-SENTINEL-END" in context


@pytest.mark.parametrize("tiny_cap", [32, 50])
def test_tiny_cap_truncation_does_not_underflow_and_leak(
    tmp_path: Path, tiny_cap: int
) -> None:
    """F1 regression: max_chars below the 50-char marker headroom made the
    slice stop negative — text[:negative] keeps the document HEAD and only
    drops the last few chars, leaking almost the whole document past the cap
    and corrupting the PARTIAL included-count. Post-fix the slice clamps to 0:
    marker only, included=0. 50 is the exact boundary (slice length 0 even
    pre-clamp) — locked here so the clamp never regresses it."""
    body = "alpha " * 80 + "NEAR-TAIL-SENTINEL" + "z" * 40
    path = tmp_path / "doc.txt"
    path.write_text(body, encoding="utf-8")

    ctx = extract_attachment_context(
        Attachment(filename="doc.txt", mimetype="text/plain", url=str(path)),
        max_chars=tiny_cap,
    )

    marker = "[TRUNCATED: attachment content budget reached]"
    assert ctx.status == "parsed"
    assert marker in ctx.content
    # At most max_chars of document content — here exactly 0 (marker only).
    document_chars = ctx.content.replace(marker, "").strip()
    assert len(document_chars) <= tiny_cap
    # The underflow leak symptom: pre-fix text[:-18] kept the head AND the
    # near-tail sentinel; neither may survive the cap now.
    assert "alpha" not in ctx.content
    assert "NEAR-TAIL-SENTINEL" not in ctx.content
    # Included-count math reflects what was actually included: zero.
    assert (
        f"PARTIAL CONTENT: only the first 0 of {len(body):,} characters are included"
        == ctx.warning
    )


def test_build_with_tiny_cap_discloses_partial_without_leak(tmp_path: Path) -> None:
    """F1 at the build level: a sub-50 cap threads through to extraction,
    leaks nothing, and still prepends the top-level PARTIAL NOTE."""
    path = tmp_path / "doc.txt"
    path.write_text("alpha " * 80, encoding="utf-8")

    context = build_attachment_context(
        [Attachment(filename="doc.txt", mimetype="text/plain", url=str(path))],
        max_chars=32,
    )

    assert context.startswith("NOTE: some content below is PARTIAL.")
    assert "[TRUNCATED: attachment content budget reached]" in context
    assert "PARTIAL CONTENT: only the first 0 of" in context
    assert "alpha" not in context


def test_truncated_attachment_discloses_partial_read(tmp_path: Path) -> None:
    """Over-cap file: PARTIAL CONTENT warning carries both char counts and
    the top-level NOTE instruction line is prepended."""
    path = tmp_path / "huge.txt"
    path.write_text("a" * 150_000, encoding="utf-8")

    context = build_attachment_context([
        Attachment(filename="huge.txt", mimetype="text/plain", url=str(path))
    ])

    assert (
        "PARTIAL CONTENT: only the first 99,950 of 150,000 characters are included"
        in context
    )
    assert context.startswith("NOTE: some content below is PARTIAL.")
    assert "tell the user explicitly that you only read part of the document" in context
