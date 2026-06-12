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

# Fallback caps — used only when config import fails. The live caps resolve
# at CALL TIME from config.CHAT_ATTACHMENT_MAX_BYTES / CHAT_ATTACHMENT_MAX_CHARS /
# CHAT_ATTACHMENT_TOTAL_MAX_CHARS via _resolve_cap (Rule 1 None-sentinels).
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_CHARS_PER_ATTACHMENT = 6000
MAX_TOTAL_CHARS = 18000

_TRUNCATION_MARKER = "\n[TRUNCATED: attachment content budget reached]"
_PARTIAL_NOTE = (
    "NOTE: some content below is PARTIAL. When answering, tell the user "
    "explicitly that you only read part of the document."
)

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


def _resolve_cap(value: int | None, config_attr: str, fallback: int) -> int:
    """Resolve a cap at call time: explicit value > config module attr > fallback.

    Module-attribute access at call time (mirrors router._engine_timeout_seconds)
    so /reload and test monkeypatches of config.<ATTR> take effect — Rule 1.
    """
    if value is not None:
        return value
    try:
        import config

        return getattr(config, config_attr)
    except Exception:
        return fallback


def build_attachment_context(
    attachments: Iterable[Attachment],
    *,
    max_chars: int | None = None,
    total_max_chars: int | None = None,
    max_bytes: int | None = None,
) -> str:
    """Render prompt-safe context for supported local document attachments."""

    # Resolve caps ONCE, then thread the resolved values into every
    # extract_attachment_context call — a caller-supplied cap must reach
    # extraction, not just the total-budget loop (R1 M2).
    resolved_max_chars = _resolve_cap(
        max_chars, "CHAT_ATTACHMENT_MAX_CHARS", MAX_CHARS_PER_ATTACHMENT
    )
    resolved_total_max_chars = _resolve_cap(
        total_max_chars, "CHAT_ATTACHMENT_TOTAL_MAX_CHARS", MAX_TOTAL_CHARS
    )
    resolved_max_bytes = _resolve_cap(
        max_bytes, "CHAT_ATTACHMENT_MAX_BYTES", MAX_ATTACHMENT_BYTES
    )

    contexts = [
        extract_attachment_context(
            att, max_chars=resolved_max_chars, max_bytes=resolved_max_bytes
        )
        for att in attachments
    ]
    contexts = [ctx for ctx in contexts if ctx.status != "unsupported"]
    if not contexts:
        return ""

    any_partial = any(ctx.warning.startswith("PARTIAL CONTENT") for ctx in contexts)
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
        remaining = resolved_total_max_chars - total_chars
        if remaining <= 0:
            parts.append("[TRUNCATED: attachment context total budget reached]")
            any_partial = True
            break
        if len(block) > remaining:
            block = block[: max(0, remaining - 60)].rstrip() + (
                "\n[TRUNCATED: attachment context total budget reached]"
            )
            any_partial = True
        parts.append(block)
        total_chars += len(block)

    body = "\n\n".join(parts)
    if any_partial:
        # Disclosure rides the model instruction (+ Phase 1 grounding rule) —
        # the model must tell the user it only read part of the document.
        return _PARTIAL_NOTE + "\n\n" + body
    return body


def extract_attachment_context(
    attachment: Attachment,
    *,
    max_chars: int | None = None,
    max_bytes: int | None = None,
) -> AttachmentContext:
    # Rule 1 — caps resolve inside the body at call time, never at def time.
    max_chars = _resolve_cap(max_chars, "CHAT_ATTACHMENT_MAX_CHARS", MAX_CHARS_PER_ATTACHMENT)
    max_bytes = _resolve_cap(max_bytes, "CHAT_ATTACHMENT_MAX_BYTES", MAX_ATTACHMENT_BYTES)

    filename = _clean_filename(attachment.filename)
    mimetype = (attachment.mimetype or "").split(";")[0].strip().lower()

    if not is_supported_document_attachment(filename, mimetype):
        return AttachmentContext(filename, mimetype, attachment.size_bytes, "unsupported")

    if attachment.size_bytes is not None and attachment.size_bytes > max_bytes:
        return AttachmentContext(
            filename,
            mimetype,
            attachment.size_bytes,
            "skipped",
            warning=f"file exceeds {max_bytes} byte parser limit",
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

    if stat.st_size > max_bytes:
        return AttachmentContext(
            filename,
            mimetype,
            stat.st_size,
            "skipped",
            warning=f"file exceeds {max_bytes} byte parser limit",
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

    content = content.strip()
    total = len(content)
    content = _truncate(content, max_chars)
    if not content:
        return AttachmentContext(
            filename,
            mimetype,
            stat.st_size,
            "empty",
            warning="no extractable text found",
        )

    if total > max_chars:
        # Truncation disclosure — the warning header line renders in the
        # context block so the model knows the read was partial.
        included = max(0, len(content) - len(_TRUNCATION_MARKER))
        return AttachmentContext(
            filename,
            mimetype,
            stat.st_size,
            "parsed",
            content=content,
            warning=(
                f"PARTIAL CONTENT: only the first {included:,} of "
                f"{total:,} characters are included"
            ),
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
    # CSV deliberately keeps the 60-row tabular-preview semantics — it is a
    # preview format, not prose, so the full-read char caps do not apply here.
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
    # Clamp the marker headroom: caps are runtime-tunable, and max_chars < 50
    # would make the slice stop NEGATIVE — text[:negative] drops chars from
    # the END, leaking almost the whole document past the cap (post-build F1).
    slice_len = max(0, max_chars - 50)
    return text[:slice_len].rstrip() + _TRUNCATION_MARKER


def _clean_filename(filename: str) -> str:
    name = Path(filename or "attachment").name
    return name.replace("\r", " ").replace("\n", " ").strip() or "attachment"


__all__ = [
    "AttachmentContext",
    "build_attachment_context",
    "extract_attachment_context",
    "is_supported_document_attachment",
]
