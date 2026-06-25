---
name: meme-generator
description: Generate captioned meme images for social replies and chat. Use when the user asks for a meme, wants to caption an image with top/bottom text, or needs a quick visual for a post. Produces a PNG from a template plus caption text.
version: 1.0.0
author: TaskChad OS
license: MIT
platforms: [linux, macos, windows]
metadata:
  taskchad:
    category: creative
    tags: [meme, image, caption, social, creative]
    related_skills: []
    mutates: false
---

# Meme Generator

Caption an image into a shareable meme PNG.

## Approaches (pick by what's installed)

**Local with Pillow (offline, deterministic):**

```python
from PIL import Image, ImageDraw, ImageFont
# Load base image, draw uppercase Impact-style text with a black stroke
# at top and bottom, wrap long lines, export PNG.
```

Use a bundled or system font; render text in white with a 2–3px black outline so
it reads on any background. Wrap captions to the image width; scale font down if
a line overflows.

**Keyless API (no local deps):** the `memegen.link` service renders from a URL:

```
https://api.memegen.link/images/<template>/<top_text>/<bottom_text>.png
```

Escape spaces as `_`, `?` as `~q`, `/` as `~s`. Good for standard templates
(`drake`, `doge`, `fry`, etc.).

## Workflow

1. Clarify the joke / top + bottom text and the template or base image.
2. Render the PNG to a temp path.
3. Hand the file back to the user (or to the social-posting skill).

## Notes

- Generation is local/read-only. **Posting** a meme to any platform is a
  separate, gated mutation — never auto-post; surface the file and let the user
  or an explicit posting skill send it.
- Keep captions punchy. If the user gives a paragraph, compress it to a punchline.
