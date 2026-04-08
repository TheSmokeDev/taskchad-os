# PDF Forms Reference

## Check if PDF has Fillable Fields

```python
from pypdf import PdfReader

reader = PdfReader("form.pdf")
fields = reader.get_fields()

if fields:
    print(f"Found {len(fields)} fillable fields:")
    for name, field in fields.items():
        field_type = field.get('/FT', 'Unknown')
        print(f"  {name}: {field_type}")
else:
    print("No fillable fields - form may need manual annotation")
```

## Field Types

| Type | Code | Description |
|------|------|-------------|
| Text | `/Tx` | Text input field |
| Checkbox | `/Btn` | Checkbox (check with `/Yes` or `/On`) |
| Radio | `/Btn` | Radio button group |
| Choice | `/Ch` | Dropdown or list |
| Signature | `/Sig` | Signature field |

## Fill Text Fields

```python
from pypdf import PdfReader, PdfWriter

reader = PdfReader("form.pdf")
writer = PdfWriter()
writer.append(reader)

# Fill multiple fields
field_values = {
    "first_name": "John",
    "last_name": "Doe",
    "email": "john@example.com",
}

writer.update_page_form_field_values(writer.pages[0], field_values)

with open("filled.pdf", "wb") as f:
    writer.write(f)
```

## Fill Checkboxes

Checkboxes use `/Yes` or `/On` to check, `/Off` to uncheck:

```python
writer.update_page_form_field_values(
    writer.pages[0],
    {"agree_terms": "/Yes"}  # or "/On" depending on the PDF
)
```

To find the correct check value:
```python
fields = reader.get_fields()
checkbox_field = fields.get("checkbox_name")
if checkbox_field:
    # Look for /AP (appearance) or /AS (appearance state)
    print(checkbox_field)
```

## Fill Radio Buttons

Radio buttons are filled by setting the group to one option's value:

```python
# First, find the radio group options
fields = reader.get_fields()
radio_field = fields.get("payment_method")
print(radio_field)  # Look for /Opt or kids

# Then set to one option
writer.update_page_form_field_values(
    writer.pages[0],
    {"payment_method": "/credit_card"}
)
```

## Fill Dropdown/Choice Fields

```python
writer.update_page_form_field_values(
    writer.pages[0],
    {"country": "United States"}  # Use the display value
)
```

## Non-Fillable Forms: Add Text Annotations

If a PDF has no fillable fields, add text directly:

```python
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from io import BytesIO

# Create overlay with text
packet = BytesIO()
c = canvas.Canvas(packet, pagesize=letter)
c.setFont("Helvetica", 12)
c.drawString(100, 700, "John Doe")  # x, y coordinates
c.drawString(100, 680, "123 Main St")
c.save()
packet.seek(0)

# Merge overlay with original
from pypdf import PdfReader as PR
overlay = PR(packet)
original = PdfReader("form.pdf")
writer = PdfWriter()

page = original.pages[0]
page.merge_page(overlay.pages[0])
writer.add_page(page)

with open("filled.pdf", "wb") as f:
    writer.write(f)
```

## Convert PDF to Images for Visual Analysis

When you need to visually identify where form fields are:

```python
import pypdfium2 as pdfium

pdf = pdfium.PdfDocument("form.pdf")
for i, page in enumerate(pdf):
    # Render at high resolution for clarity
    bitmap = page.render(scale=3.0)
    img = bitmap.to_pil()
    img.save(f"form_page_{i+1}.png")
```

Then analyze the images to determine bounding boxes for text placement.

## Flatten Form Fields

Make filled fields permanent (non-editable):

```python
from pypdf import PdfReader, PdfWriter

reader = PdfReader("filled_form.pdf")
writer = PdfWriter()

for page in reader.pages:
    writer.add_page(page)

# Remove form field interactivity
if "/AcroForm" in writer._root_object:
    del writer._root_object["/AcroForm"]

with open("flattened.pdf", "wb") as f:
    writer.write(f)
```
