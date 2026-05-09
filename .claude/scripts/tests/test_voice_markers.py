"""PRD-8 Phase 4 (WS2) — voice_markers.py port tests.

Asserts the marker parser matches bot.ts:288-317 verbatim two-pattern shape,
SendMarker.kind maps SEND_FILE→'document' / SEND_PHOTO→'photo', and the
path-traversal defense covers the locked 5-case parametrization.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure chat/ on path
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))

from voice_markers import SendMarker, parse_send_markers, strip_send_markers  # noqa: E402


# ─── Basic API ────────────────────────────────────────────────────────────


def test_parse_send_markers_returns_list():
    """parse_send_markers returns a list (empty when no markers found)."""
    result = parse_send_markers("plain text with no markers")
    assert result == []


def test_strip_send_markers_no_markers():
    """strip_send_markers passes through text unchanged when no markers found."""
    text = "plain text"
    assert strip_send_markers(text) == "plain text"


# ─── Marker kind mapping (R1 M5) ──────────────────────────────────────────


def test_marker_kind_document_photo_mapping():
    """SEND_FILE→'document'; SEND_PHOTO→'photo' (matches bot.ts:303-307)."""
    out_file = parse_send_markers("[SEND_FILE:./a.pdf]")
    assert len(out_file) == 1
    assert out_file[0].kind == "document"
    assert out_file[0].path == "./a.pdf"

    out_photo = parse_send_markers("[SEND_PHOTO:./b.png]")
    assert len(out_photo) == 1
    assert out_photo[0].kind == "photo"
    assert out_photo[0].path == "./b.png"


# ─── Two-pattern marker coverage (R1 B3) ──────────────────────────────────


@pytest.mark.parametrize(
    "text,expected_count,first_kind,first_path,first_caption",
    [
        # Two-pattern shape coverage — every case asserts a concrete outcome
        # (post-build NM2: intentional pass on POSIX absolute removed; the
        # path-traversal allow/reject contract is asserted explicitly here too).
        # 1. Canonical bracketed (relative — ALLOWED)
        ("[SEND_FILE:./tmp.pdf]", 1, "document", "./tmp.pdf", None),
        # 2. Canonical with caption (relative — ALLOWED)
        ("[SEND_FILE:./tmp.pdf|my caption]", 1, "document", "./tmp.pdf", "my caption"),
        # 3. Pipe separator + URL (ALLOWED)
        ("[SEND_PHOTO|https://example.com/x.png]", 1, "photo", "https://example.com/x.png", None),
        # 4. Bare URL form (ALLOWED — http(s):// passes path-traversal defense)
        ("text SEND_FILE:https://example.com/doc.pdf more", 1, "document", "https://example.com/doc.pdf", None),
        # 5. Bare URL form with pipe (ALLOWED)
        ("SEND_PHOTO|https://example.com/x.png", 1, "photo", "https://example.com/x.png", None),
        # 6. Bare POSIX absolute path (REJECTED — path-traversal defense drops it)
        ("text SEND_FILE:/tmp/a.pdf more", 0, None, None, None),
        # 7. Bracketed Windows absolute path (REJECTED)
        ("[SEND_FILE:C:/Windows/System32/config/sam]", 0, None, None, None),
        # 8. Bracketed parent-traversal (REJECTED)
        ("[SEND_FILE:../../etc/passwd]", 0, None, None, None),
    ],
)
def test_two_pattern_marker_coverage(
    text, expected_count, first_kind, first_path, first_caption
):
    """Two-pattern coverage — bracketed canonical + bare/URL form, with explicit
    outcome assertions for both ALLOW and REJECT cases (post-build NM2 fix)."""
    out = parse_send_markers(text)
    assert len(out) == expected_count, f"Failed: {text!r} (got {out})"
    if expected_count > 0:
        assert out[0].kind == first_kind
        assert out[0].path == first_path
        assert out[0].caption == first_caption


def test_multiple_markers_in_text():
    """Multiple markers in one text are all parsed."""
    text = "intro [SEND_FILE:./a.pdf|cap one] middle [SEND_PHOTO:./b.png] end"
    out = parse_send_markers(text)
    assert len(out) == 2
    assert out[0].kind == "document"
    assert out[0].path == "./a.pdf"
    assert out[0].caption == "cap one"
    assert out[1].kind == "photo"
    assert out[1].path == "./b.png"


# ─── Path-traversal defense (R3 NM2 / NB5) — locked 5-case parametrization ─


@pytest.mark.parametrize(
    "text,expected_marker_count,description",
    [
        # REJECT cases (3) — silently dropped (no SendMarker output)
        ("[SEND_FILE:/etc/passwd]", 0, "POSIX absolute path"),
        ("[SEND_FILE:C:/Windows/System32/config/sam]", 0, "Windows absolute path"),
        ("[SEND_FILE:../../etc/passwd]", 0, "path traversal `..`"),
        # ALLOW cases (2) — appear in parsed output
        ("[SEND_PHOTO:./valid.png]", 1, "relative path"),
        ("[SEND_FILE:https://example.com/x.pdf]", 1, "http(s) URL"),
    ],
)
def test_path_traversal_rejected(text, expected_marker_count, description):
    """5-case parametrization — 3 reject + 2 allow."""
    out = parse_send_markers(text)
    assert len(out) == expected_marker_count, (
        f"Failed for {description}: text={text!r} → got {len(out)} markers, "
        f"expected {expected_marker_count}"
    )


# ─── strip_send_markers ───────────────────────────────────────────────────


def test_strip_send_markers_removes_canonical():
    """strip_send_markers drops [SEND_FILE:...] from text."""
    text = "before [SEND_FILE:./a.pdf] after"
    result = strip_send_markers(text)
    assert "SEND_FILE" not in result
    assert "before" in result
    assert "after" in result


def test_strip_send_markers_removes_bare():
    """strip_send_markers drops bare SEND_FILE: form too."""
    text = "before SEND_FILE:/abs/path after"
    result = strip_send_markers(text)
    assert "SEND_FILE" not in result


def test_strip_send_markers_removes_rejected_paths():
    """Rejected paths (path-traversal) are still STRIPPED so raw command doesn't leak."""
    text = "before [SEND_FILE:/etc/passwd] after"
    result = strip_send_markers(text)
    assert "/etc/passwd" not in result
    assert "SEND_FILE" not in result


def test_strip_send_markers_collapses_blank_lines():
    """3+ consecutive newlines collapsed to 2 (port bot.ts:314)."""
    text = "before\n\n\n\nafter"
    result = strip_send_markers(text)
    assert "\n\n\n" not in result


# ─── SendMarker dataclass ─────────────────────────────────────────────────


def test_send_marker_is_frozen():
    """SendMarker is frozen dataclass (immutable)."""
    m = SendMarker(kind="document", path="./a.pdf", caption=None)
    with pytest.raises((AttributeError, TypeError)):
        m.kind = "photo"  # type: ignore[misc]


def test_send_marker_caption_optional():
    """SendMarker.caption defaults to None when omitted."""
    m = SendMarker(kind="photo", path="./x.png")
    assert m.caption is None
