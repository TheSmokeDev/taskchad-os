---
name: pdf
description: PDF manipulation toolkit for extracting text and tables, creating new PDFs, merging/splitting documents, converting to images, and handling forms. Use when Claude needs to read PDF content, create PDF documents, fill PDF forms, merge/split PDFs, or convert PDFs to images.
---

# PDF Processing

## Quick Reference

| Task | Tool | Example |
|------|------|---------|
| Extract text | pdfplumber | `page.extract_text()` |
| Extract tables | pdfplumber | `page.extract_tables()` |
| Merge PDFs | pypdf | `writer.add_page(page)` |
| Split PDFs | pypdf | One page per file |
| Create PDFs | reportlab | Canvas or Platypus |
| Convert to images | pypdfium2 | `page.render()` |
| Fill forms | pypdf | See forms section |

## Dependencies

```bash
pip install pypdf pdfplumber reportlab pypdfium2
```

## Extract Text

```python
import pdfplumber

with pdfplumber.open("document.pdf") as pdf:
    for page in pdf.pages:
        text = page.extract_text()
        print(text)
```

## Extract Tables

```python
import pdfplumber
import pandas as pd

with pdfplumber.open("document.pdf") as pdf:
    for page in pdf.pages:
        tables = page.extract_tables()
        for table in tables:
            if table:
                df = pd.DataFrame(table[1:], columns=table[0])
                print(df)
```

## Merge PDFs

```python
from pypdf import PdfWriter, PdfReader

writer = PdfWriter()
for pdf_file in ["doc1.pdf", "doc2.pdf"]:
    reader = PdfReader(pdf_file)
    for page in reader.pages:
        writer.add_page(page)

with open("merged.pdf", "wb") as output:
    writer.write(output)
```

## Split PDF

```python
from pypdf import PdfReader, PdfWriter

reader = PdfReader("input.pdf")
for i, page in enumerate(reader.pages):
    writer = PdfWriter()
    writer.add_page(page)
    with open(f"page_{i+1}.pdf", "wb") as output:
        writer.write(output)
```

## Create PDF with reportlab

```python
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

doc = SimpleDocTemplate("output.pdf", pagesize=letter)
styles = getSampleStyleSheet()
story = []

# Add title
story.append(Paragraph("Document Title", styles['Title']))
story.append(Spacer(1, 12))

# Add body text
story.append(Paragraph("This is body text.", styles['Normal']))

# Add table
data = [["Header 1", "Header 2"], ["Cell 1", "Cell 2"]]
table = Table(data)
table.setStyle(TableStyle([
    ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
    ('GRID', (0, 0), (-1, -1), 1, colors.black),
]))
story.append(table)

doc.build(story)
```

## Convert PDF to Images

```python
import pypdfium2 as pdfium

pdf = pdfium.PdfDocument("document.pdf")
for i, page in enumerate(pdf):
    bitmap = page.render(scale=2.0)  # Higher = better quality
    img = bitmap.to_pil()
    img.save(f"page_{i+1}.png", "PNG")
```

## Read PDF Metadata

```python
from pypdf import PdfReader

reader = PdfReader("document.pdf")
meta = reader.metadata
print(f"Title: {meta.title}")
print(f"Author: {meta.author}")
print(f"Pages: {len(reader.pages)}")
```

## Fill Form Fields

For PDFs with fillable form fields:

```python
from pypdf import PdfReader, PdfWriter

reader = PdfReader("form.pdf")
writer = PdfWriter()

# Clone the PDF
writer.append(reader)

# Get form fields
fields = reader.get_fields()
for field_name, field in fields.items():
    print(f"Field: {field_name}, Type: {field.get('/FT')}")

# Fill fields
writer.update_page_form_field_values(
    writer.pages[0],
    {"field_name": "value"}
)

with open("filled_form.pdf", "wb") as output:
    writer.write(output)
```

## Password Protection

```python
from pypdf import PdfReader, PdfWriter

reader = PdfReader("input.pdf")
writer = PdfWriter()

for page in reader.pages:
    writer.add_page(page)

writer.encrypt("userpassword", "ownerpassword")

with open("encrypted.pdf", "wb") as output:
    writer.write(output)
```

## Rotate Pages

```python
from pypdf import PdfReader, PdfWriter

reader = PdfReader("input.pdf")
writer = PdfWriter()

page = reader.pages[0]
page.rotate(90)  # 90, 180, or 270
writer.add_page(page)

with open("rotated.pdf", "wb") as output:
    writer.write(output)
```

## Advanced: Forms Reference

For complex form handling (fillable fields, checkboxes, radio buttons), see `references/forms.md`.
