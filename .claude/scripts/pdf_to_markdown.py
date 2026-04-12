#!/usr/bin/env python3
"""
PDF → Markdown preprocessor using PyMuPDF.
Handles IRS forms, bank statements, tax returns.

Usage:
    uv run python pdf_to_markdown.py path/to/file.pdf
    uv run python pdf_to_markdown.py path/to/directory/   # batch mode
"""

import sys
from pathlib import Path

import fitz  # PyMuPDF


def pdf_to_markdown(pdf_path: Path, output_path: Path | None = None) -> Path:
    """Convert a PDF to markdown, preserving page structure."""
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        raise RuntimeError(f"Cannot open PDF: {e}")

    if doc.is_encrypted:
        raise RuntimeError(f"PDF is password-protected: {pdf_path.name}")

    pages = []
    for page_num, page in enumerate(doc, 1):
        text = page.get_text("text", sort=True).strip()
        if text:
            pages.append(f"<!-- page {page_num} -->\n\n{text}")
        else:
            pages.append(f"<!-- page {page_num} -->\n\n[Scanned image — no extractable text. Run OCR before ingesting.]")

    doc.close()

    markdown = f"# {pdf_path.stem}\n\n" + "\n\n---\n\n".join(pages)
    out = output_path or pdf_path.with_suffix(".md")
    out.write_text(markdown, encoding="utf-8")
    return out


def batch_convert(dir_path: Path) -> list[Path]:
    """Convert all PDFs in a directory."""
    pdfs = list(dir_path.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {dir_path}")
        return []
    results = []
    for pdf in pdfs:
        try:
            out = pdf_to_markdown(pdf)
            print(f"  ✓ {pdf.name} → {out.name}")
            results.append(out)
        except Exception as e:
            print(f"  ✗ {pdf.name}: {e}")
    return results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: pdf_to_markdown.py <file.pdf|directory/>")
        sys.exit(1)

    target = Path(sys.argv[1])

    if target.is_dir():
        results = batch_convert(target)
        print(f"\nConverted {len(results)} files.")
    elif target.suffix.lower() == ".pdf":
        out = pdf_to_markdown(target)
        print(f"✓ {target.name} → {out.name}")
    else:
        print(f"Error: {target} is not a PDF or directory.")
        sys.exit(1)
