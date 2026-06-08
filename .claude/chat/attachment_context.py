"""Bounded model-readable context for uploaded chat documents."""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree

from models import Attachment

MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_CHARS_PER_ATTACHMENT = 6000
MAX_TOTAL_CHARS = 18000

_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".log"}
_CSV_EXTENSIONS = {".csv", ".tsv"}
_PDF_EXTENSIONS = {".pdf"}
_DOCX_EXTENSIONS = {".docx"}

_TEXT_MIMES = {
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "application/markdown",
}
_CSV_MIMES = {
    "text/csv",
    "text/tab-separated-values",
    "application/csv",
    "application/vnd.ms-excel",
}
_PDF_MIMES = {"application/pdf"}
_DOCX_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@dataclass(frozen=True)
class AttachmentContext:
    filename: str
    mimetype: str
    size_bytes: int | None
    status: str
    content: str = ""
    warning: str = ""


def is_supported_document_attachment(filename: str, mimetype: str | None = None) -> bool:
    ext = Path(filename).suffix.lower()
    mime = (mimetype or "").split(";")[0].strip().lower()
    return (
        ext in _TEXT_EXTENSIONS
        or ext in _CSV_EXTENSIONS
        or ext in _PDF_EXTENSIONS
        or ext in _DOCX_EXTENSIONS
        or mime in _TEXT_MIMES
        or mime in _CSV_MIMES
        or mime in _PDF_MIMES
        or mime in _DOCX_MIMES
    )


def build_attachment_context(attachments: Iterable[Attachment]) -> str:
    """Render prompt-safe context for supported local document attachments."""

    contexts = [extract_attachment_context(att) for att in attachments]
    contexts = [ctx for ctx in contexts if ctx.status != "unsupported"]
    if not contexts:
        return ""

    parts: list[str] = []
    total_chars = 0
    for index, ctx in enumerate(contexts, start=1):
        header = [
            f"## Attachment {index}: {ctx.filename}",
            f"mime: {ctx.mimetype or 'unknown'}",
            f"size_bytes: {ctx.size_bytes if ctx.size_bytes is not None else 'unknown'}",
            f"status: {ctx.status}",
        ]
        if ctx.warning:
            header.append(f"warning: {ctx.warning}")
        if ctx.content:
            header.append("content:")
            header.append(ctx.content)
        block = "\n".join(header)
        remaining = MAX_TOTAL_CHARS - total_chars
        if remaining <= 0:
            parts.append("[TRUNCATED: attachment context total budget reached]")
            break
        if len(block) > remaining:
            block = block[: max(0, remaining - 60)].rstrip() + (
                "\n[TRUNCATED: attachment context total budget reached]"
            )
        parts.append(block)
        total_chars += len(block)

    return "\n\n".join(parts)


def extract_attachment_context(attachment: Attachment) -> AttachmentContext:
    filename = _clean_filename(attachment.filename)
    mimetype = (attachment.mimetype or "").split(";")[0].strip().lower()

    if not is_supported_document_attachment(filename, mimetype):
        return AttachmentContext(filename, mimetype, attachment.size_bytes, "unsupported")

    if attachment.size_bytes is not None and attachment.size_bytes > MAX_ATTACHMENT_BYTES:
        return AttachmentContext(
            filename,
            mimetype,
            attachment.size_bytes,
            "skipped",
            warning=f"file exceeds {MAX_ATTACHMENT_BYTES} byte parser limit",
        )

    if not attachment.url:
        return AttachmentContext(
            filename,
            mimetype,
            attachment.size_bytes,
            "skipped",
            warning="attachment has no local file reference",
        )

    path = Path(attachment.url)
    try:
        stat = path.stat()
    except OSError as exc:
        return AttachmentContext(
            filename,
            mimetype,
            attachment.size_bytes,
            "error",
            warning=f"local attachment could not be opened: {type(exc).__name__}",
        )

    if stat.st_size > MAX_ATTACHMENT_BYTES:
        return AttachmentContext(
            filename,
            mimetype,
            stat.st_size,
            "skipped",
            warning=f"file exceeds {MAX_ATTACHMENT_BYTES} byte parser limit",
        )

    try:
        content = _extract_path_text(path, filename, mimetype)
    except Exception as exc:
        return AttachmentContext(
            filename,
            mimetype,
            stat.st_size,
            "error",
            warning=f"parser failed: {type(exc).__name__}",
        )

    content = _truncate(content.strip(), MAX_CHARS_PER_ATTACHMENT)
    if not content:
        return AttachmentContext(
            filename,
            mimetype,
            stat.st_size,
            "empty",
            warning="no extractable text found",
        )

    return AttachmentContext(filename, mimetype, stat.st_size, "parsed", content=content)


def _extract_path_text(path: Path, filename: str, mimetype: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in _PDF_EXTENSIONS or mimetype in _PDF_MIMES:
        return _extract_pdf(path)
    if ext in _DOCX_EXTENSIONS or mimetype in _DOCX_MIMES:
        return _extract_docx(path)
    if ext in _CSV_EXTENSIONS or mimetype in _CSV_MIMES:
        return _extract_csv(path, delimiter="\t" if ext == ".tsv" else ",")
    return _read_text(path)


def _extract_pdf(path: Path) -> str:
    import fitz  # PyMuPDF

    pages: list[str] = []
    with fitz.open(str(path)) as doc:
        if doc.is_encrypted:
            raise ValueError("encrypted_pdf")
        for page_num, page in enumerate(doc, start=1):
            text = page.get_text("text", sort=True).strip()
            if text:
                pages.append(f"[page {page_num}]\n{text}")
    return "\n\n".join(pages)


def _extract_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        raw = archive.read("word/document.xml")
    root = ElementTree.fromstring(raw)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    for paragraph in root.iter(namespace + "p"):
        texts = [node.text or "" for node in paragraph.iter(namespace + "t")]
        line = "".join(texts).strip()
        if line:
            paragraphs.append(line)
    return "\n".join(paragraphs)


def _extract_csv(path: Path, *, delimiter: str) -> str:
    text = _read_text(path)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[str] = []
    for row_index, row in enumerate(reader):
        if row_index >= 60:
            rows.append("[TRUNCATED: CSV row limit reached]")
            break
        cells = [_truncate(cell.strip(), 120) for cell in row[:12]]
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 50].rstrip() + "\n[TRUNCATED: attachment content budget reached]"


def _clean_filename(filename: str) -> str:
    name = Path(filename or "attachment").name
    return name.replace("\r", " ").replace("\n", " ").strip() or "attachment"


__all__ = [
    "AttachmentContext",
    "build_attachment_context",
    "extract_attachment_context",
    "is_supported_document_attachment",
]
