"""
Google Docs Direct Integration for The Homie.

Read-only access to Google Docs. Shares OAuth token with Gmail, Calendar, Drive, and Sheets.

Usage:
    uv run python -m integrations.docs_api read <document_id>
    uv run python -m integrations.docs_api info <document_id>
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Add parent dir for config imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared import with_retry  # noqa: E402


@dataclass
class DocSection:
    """A section of a Google Doc (heading + content under it)."""

    heading: str
    level: int  # 0 = body text, 1-6 = heading levels
    content: str


@dataclass
class DocumentData:
    """Represents a Google Doc."""

    id: str
    title: str
    url: str
    body_text: str = ""
    sections: list[DocSection] = field(default_factory=list)


def get_docs_service() -> Any:
    """Build authenticated Docs API service."""
    from googleapiclient.discovery import build  # type: ignore[import-untyped]

    from integrations.auth import get_google_credentials

    creds = get_google_credentials()
    service: Any = build("docs", "v1", credentials=creds)
    return service


def _extract_text_from_element(element: dict[str, Any]) -> str:
    """Extract plain text from a document structural element."""
    text = ""

    paragraph = element.get("paragraph")
    if paragraph:
        for elem in paragraph.get("elements", []):
            text_run = elem.get("textRun")
            if text_run:
                text += text_run.get("content", "")

    table = element.get("table")
    if table:
        for row in table.get("tableRows", []):
            row_texts: list[str] = []
            for cell in row.get("tableCells", []):
                cell_text = ""
                for cell_elem in cell.get("content", []):
                    cell_text += _extract_text_from_element(cell_elem)
                row_texts.append(cell_text.strip())
            text += " | ".join(row_texts) + "\n"

    return text


def _get_heading_level(paragraph: dict[str, Any]) -> int:
    """Get heading level from paragraph style (0 = normal text)."""
    style = paragraph.get("paragraphStyle", {})
    named_style = style.get("namedStyleType", "NORMAL_TEXT")

    if named_style.startswith("HEADING_"):
        try:
            return int(named_style.split("_")[1])
        except (IndexError, ValueError):
            return 0
    return 0


def read_document(document_id: str) -> DocumentData:
    """
    Read a Google Doc and extract its text content.

    Args:
        document_id: The document ID from the URL
    """
    service = get_docs_service()

    doc: dict[str, Any] = with_retry(
        lambda: service.documents().get(documentId=document_id).execute()
    )

    title = doc.get("title", "(untitled)")
    doc_id = doc.get("documentId", document_id)
    url = f"https://docs.google.com/document/d/{doc_id}/edit"

    # Extract body content
    body = doc.get("body", {})
    content_elements = body.get("content", [])

    full_text_parts: list[str] = []
    sections: list[DocSection] = []
    current_heading = ""
    current_level = 0
    current_content_parts: list[str] = []

    for element in content_elements:
        paragraph = element.get("paragraph")
        if paragraph:
            heading_level = _get_heading_level(paragraph)
            text = _extract_text_from_element(element)

            if heading_level > 0:
                # Save previous section
                if current_heading or current_content_parts:
                    sections.append(
                        DocSection(
                            heading=current_heading,
                            level=current_level,
                            content="".join(current_content_parts).strip(),
                        )
                    )
                current_heading = text.strip()
                current_level = heading_level
                current_content_parts = []
            else:
                current_content_parts.append(text)

            full_text_parts.append(text)
        else:
            # Tables and other elements
            text = _extract_text_from_element(element)
            full_text_parts.append(text)
            current_content_parts.append(text)

    # Don't forget last section
    if current_heading or current_content_parts:
        sections.append(
            DocSection(
                heading=current_heading,
                level=current_level,
                content="".join(current_content_parts).strip(),
            )
        )

    return DocumentData(
        id=doc_id,
        title=title,
        url=url,
        body_text="".join(full_text_parts).strip(),
        sections=sections,
    )


def get_document_info(document_id: str) -> DocumentData:
    """
    Get document metadata without full content (faster).

    Args:
        document_id: The document ID from the URL
    """
    service = get_docs_service()

    doc: dict[str, Any] = with_retry(
        lambda: service.documents().get(documentId=document_id).execute()
    )

    title = doc.get("title", "(untitled)")
    doc_id = doc.get("documentId", document_id)

    # Just count approximate content length
    body = doc.get("body", {})
    content_elements = body.get("content", [])
    text_parts: list[str] = []
    for element in content_elements:
        text_parts.append(_extract_text_from_element(element))

    body_text = "".join(text_parts).strip()

    return DocumentData(
        id=doc_id,
        title=title,
        url=f"https://docs.google.com/document/d/{doc_id}/edit",
        body_text=body_text,
    )


def format_document_for_context(data: DocumentData, max_chars: int = 4000) -> str:
    """Format document for inclusion in Claude's context prompt."""
    output: list[str] = []

    output.append(f"**{data.title}**")
    output.append(f"ID: `{data.id}`")
    output.append(f"URL: {data.url}")

    if data.sections:
        output.append(f"\nSections: {len(data.sections)}")
        output.append("")

        chars = sum(len(line) for line in output)

        for section in data.sections:
            if section.heading:
                prefix = "#" * section.level if section.level > 0 else ""
                heading_line = f"{prefix} {section.heading}".strip()
                output.append(heading_line)
                chars += len(heading_line)

            if section.content:
                if chars + len(section.content) > max_chars:
                    remaining_chars = max_chars - chars - 50
                    if remaining_chars > 100:
                        output.append(section.content[:remaining_chars] + "...")
                    output.append(f"\n... content truncated (total ~{len(data.body_text)} chars)")
                    break
                output.append(section.content)
                chars += len(section.content)

            output.append("")
    elif data.body_text:
        if len(data.body_text) > max_chars:
            output.append(f"\n{data.body_text[:max_chars]}...")
            output.append(f"\n... truncated (total ~{len(data.body_text)} chars)")
        else:
            output.append(f"\n{data.body_text}")

    return "\n".join(output)


# CLI for testing
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Google Docs integration")
    parser.add_argument("command", choices=["read", "info"])
    parser.add_argument("document_id")
    parser.add_argument("--max-chars", type=int, default=4000)

    args = parser.parse_args()

    if args.command == "read":
        doc_data = read_document(args.document_id)
        print(format_document_for_context(doc_data, max_chars=args.max_chars))

    elif args.command == "info":
        doc_data = get_document_info(args.document_id)
        char_count = len(doc_data.body_text)
        print(f"Title: {doc_data.title}")
        print(f"ID: {doc_data.id}")
        print(f"URL: {doc_data.url}")
        print(f"Content length: ~{char_count} chars")
