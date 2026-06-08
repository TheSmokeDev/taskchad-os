from __future__ import annotations

import zipfile
from pathlib import Path

from attachment_context import build_attachment_context, is_supported_document_attachment
from models import Attachment


def test_text_attachment_context_is_bounded_and_hides_path(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("alpha\n" * 2000, encoding="utf-8")

    context = build_attachment_context([
        Attachment(filename="notes.txt", mimetype="text/plain", url=str(path))
    ])

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
