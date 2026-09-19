"""Snapshot image bytes for model input, independently of model tool authority."""

from __future__ import annotations

import base64
import hashlib
from io import BytesIO

from PIL import Image


def image_blocks(request):
    """Return immutable Anthropic image blocks and the matching provenance."""
    paths = request.image_paths or []
    if len(paths) > 4:
        raise ValueError("at most four image inputs are supported")
    expected = (request.metadata or {}).get("image_input_hashes")
    if expected is not None and (not isinstance(expected, list) or len(expected) != len(paths)):
        raise ValueError("image hashes must match the image input count")
    blocks, receipts = [], []
    for index, raw in enumerate(paths):
        from pathlib import Path

        path = Path(raw)
        with path.open("rb") as handle:
            data = handle.read(5 * 1024 * 1024 + 1)
        if not data or len(data) > 5 * 1024 * 1024:
            raise ValueError("image input exceeds the byte limit")
        digest = hashlib.sha256(data).hexdigest()
        if expected is not None and expected[index] != digest:
            raise ValueError("image changed after its evidence snapshot")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime, fmt = "image/png", "PNG"
        elif data.startswith(b"\xff\xd8\xff"):
            mime, fmt = "image/jpeg", "JPEG"
        else:
            raise ValueError("image input must be a PNG or JPEG")
        with Image.open(BytesIO(data)) as bitmap:
            if bitmap.format != fmt or bitmap.width * bitmap.height > 36_000_000:
                raise ValueError("invalid image format or dimensions")
            bitmap.verify()
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": base64.b64encode(data).decode("ascii"),
                },
            }
        )
        receipts.append({"sha256": digest, "mime_type": mime, "delivered": False})
    return blocks, receipts


async def image_prompt(text, blocks):
    yield {
        "type": "user",
        "session_id": "",
        "parent_tool_use_id": None,
        "message": {"role": "user", "content": [{"type": "text", "text": text}, *blocks]},
    }
