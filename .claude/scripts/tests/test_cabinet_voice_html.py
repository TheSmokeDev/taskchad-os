"""PRD-8 Phase 6 / WS1 — voice_html port tests.

Covers contract criterion:
  * voice_html_byte_equals_warroom_html_modulo_translation_map (relaxed
    to: "the rendered HTML applies every Translation Boundary Audit
    substitution and contains the load-bearing structural elements")
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from cabinet.voice.voice_html import get_voice_meeting_html  # noqa: E402


def _render_default(**overrides):
    kw = {
        "token": "tok_abc",
        "meeting_id": 42,
        "chat_id": "tg-12345",
        "ws_port": 7860,
    }
    kw.update(overrides)
    return get_voice_meeting_html(**kw)


def test_returns_complete_html_document():
    """get_voice_meeting_html returns a fully-formed HTML5 document."""
    html = _render_default()
    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html
    assert '<meta charset="UTF-8">' in html


def test_template_substitutions_applied():
    """Token + meeting_id + chat_id + ws_port all appear in the rendered HTML."""
    html = _render_default(token="custom_tok", meeting_id=999, chat_id="chat_xyz", ws_port=8765)
    assert "custom_tok" in html
    assert "999" in html
    assert "chat_xyz" in html
    assert "8765" in html


def test_translation_boundary_warroom_client_renamed():
    """B6: /warroom-client.js -> /api/cabinet/voice/client.bundle.js."""
    html = _render_default()
    assert "/api/cabinet/voice/client.bundle.js" in html
    assert "/warroom-client.js" not in html


def test_translation_boundary_avatar_renamed():
    """B6: /api/agents/{id}/avatar (upstream) -> /api/cabinet/voice/avatars/{id}.png (Homie deviation)."""
    html = _render_default()
    assert "/api/cabinet/voice/avatars/" in html
    assert "/warroom-avatar/" not in html


def test_translation_boundary_no_warroom_music():
    """Music feature dropped per Phase 6 MVP scope."""
    html = _render_default()
    assert "/warroom-music" not in html


def test_pipecat_websocket_transport_constructed():
    """The page wires `new WebSocketTransport({ wsUrl })` per upstream pattern."""
    html = _render_default()
    assert "WebSocketTransport" in html
    assert "PipecatClient" in html
    assert "window.PipecatWarRoom" in html


def test_transport_ready_activates_meeting_ui():
    """WebSocket transport readiness is enough to move the page out of
    Connecting... for the cabinet voice legacy WebSocket path.

    The Pipecat WebSocket server can receive ``client-ready`` and audio
    frames before the old ``onConnected`` UI path resolves, so the page must
    not wait forever on that callback.
    """
    html = _render_default()
    assert "onTransportStateChanged" in html
    assert "state === 'ready' || state === 'connected'" in html
    assert "activateMeeting('transport')" in html


def test_connect_timeout_resets_start_button():
    """A browser audio startup hang should become an operator-visible timeout
    instead of leaving Start Meeting disabled forever.
    """
    html = _render_default()
    assert "scheduleConnectTimeout" in html
    assert "connect timeout" in html
    assert "Connection timed out while starting browser audio" in html
    assert "Start Meeting" in html


def test_start_meeting_connects_with_microphone_off():
    """Start Meeting should not block on browser mic capture.

    The explicit mic button is the browser-permission step. That keeps the
    control plane usable when the room transport is ready but media capture is
    delayed or denied.
    """
    html = _render_default()
    assert "enableMic: false" in html
    assert "Meeting connected. Press the mic button to talk." in html
    assert "starting mic..." in html
    assert "microphone activation timed out" in html
    assert "Microphone permission denied. Allow microphone for this page" in html


def test_voice_page_uses_page_local_media_manager():
    """The Cabinet page must not let Daily device initialization block the
    WebSocket handshake.

    The page-local media manager keeps room connect separate from explicit
    microphone capture while preserving Pipecat's WebSocket protocol.
    """
    html = _render_default()
    assert "class CabinetSimpleMediaManager" in html
    assert "mediaManager: new CabinetSimpleMediaManager()" in html
    assert "navigator.mediaDevices.getUserMedia" in html
    assert "createScriptProcessor" in html


def test_handle_server_message_renders_agent_error():
    """R1 v2 B2 — kill-switch refusals from SSE error events surface as
    transcript entries via the agent_error event handler."""
    html = _render_default()
    assert "handleServerMessage" in html
    assert "agent_error" in html
    assert "hand_down" in html
    assert "agent_selected" in html


def test_default_5_agent_tiles_rendered():
    """Default ClaudeClaw personas render as agent cards in the sidebar.

    PRD-8 Phase 6 v2 fix-pass 2026-05-10 (B2 fix) — the lead persona uses
    canonical wire string ``"default"`` (not upstream's ``"main"``) so the
    avatar URL resolves through ``personas.load_persona_config("default")``
    without a translation hop. The Q4 lock is enforced at the HTML
    emission boundary (voice_html.py:_build_default_agent_tiles_html).
    """
    html = _render_default()
    for agent_id in ("default", "research", "comms", "content", "ops"):
        assert f'id="agent-{agent_id}"' in html, (
            f"Expected agent tile id={agent_id!r} in rendered HTML"
        )
    # Q4 lock: upstream `id="agent-main"` MUST NOT appear (B2 regression).
    assert 'id="agent-main"' not in html, (
        "voice_html must NOT emit upstream `agent-main` wire string — "
        "Q4 translation locks the canonical default at the HTML boundary."
    )


def test_html_escaping_prevents_xss():
    """Token/chat_id are escaped to prevent injection via malformed query strings.

    The token/chat_id appear in the raw HTML in three forms:
      1. As URL query values inside HTML attribute (URL-encoded via
         urllib.parse.quote — applies to token only, R2 fix-pass).
      2. As HTML attribute text values (HTML-escaped via
         html.escape(..., quote=True) — applies to chat_id, meeting_id).
      3. As JS string literals (escaped via json.dumps()).
    None of those forms should leak an unescaped <script> tag.
    """
    payload = '"><script>alert(1)</script>'
    rendered = _render_default(token=payload, chat_id=payload)

    # token enters URL-encoded form in bundle/avatar URLs (R2 fix-pass).
    # urllib.parse.quote percent-encodes `"`, `<`, `>` so the ``"><script>``
    # XSS vector becomes ``%22%3E%3Cscript%3E`` in the rendered URLs.
    assert '%22%3E%3Cscript%3E' in rendered, (
        "token URL-encoding must percent-encode quote/angle-bracket chars; "
        "without it the bundle URL would close the attribute and inject."
    )
    # token + chat_id enter inline-script context via _script_safe_json
    # (R3 fix-pass), which post-escapes ``<``, ``>``, ``&``, ``/`` to
    # ``\\uXXXX`` form. The dangerous HTML-attribute-context form is
    # blocked, AND the ``</script>`` script-context-break is blocked.
    assert '\\u003c\\u002fscript\\u003e' in rendered or '\\u003e\\u003cscript\\u003e' in rendered, (
        "_script_safe_json must escape < / > / / so payloads containing "
        "</script>...<script> can't break out of the inline script context."
    )
    # Sanity: an unescaped HTML-context script-injection NEVER lands in an
    # attribute value boundary.
    import re as _re
    bad_attr_pattern = _re.compile(r'src="[^"]*"><script>alert\(1\)')
    assert not bad_attr_pattern.search(rendered)


def test_inline_script_context_blocks_close_script_xss():
    """B1-R3 — token/chat_id embedded in inline ``<script>`` block must
    NOT allow a ``</script>`` payload to close the script tag and run as
    a fresh script.

    Class-of-bug: Python's ``json.dumps()`` escapes JS string delimiters
    (``"``, ``\\``) but does NOT escape ``</script>`` or ``<!--``. A
    payload of ``</script><script>alert(1)</script>`` becomes
    ``"</script><script>alert(1)</script>"`` in the inline script — the
    HTML parser sees the literal ``</script>`` tag and closes the script
    block, then ``<script>alert(1)</script>`` runs as a fresh script
    (script-context XSS).

    Fix locks: ``_script_safe_json()`` post-escapes ``<``, ``>``, ``&``,
    ``/`` to ``\\uXXXX`` form so the payload renders as
    ``"\\u003c\\u002fscript\\u003e\\u003cscript\\u003ealert(1)\\u003c\\u002fscript\\u003e"``
    and stays safely inside the JS string literal.
    """
    payload = "</script><script>alert(1)</script>"
    rendered = _render_default(token=payload, chat_id=payload, meeting_id=42)

    # NEGATIVE: literal ``</script>`` must not appear in the rendered
    # output as part of the inline script context (the closing tag at
    # the END of the inline block is fine — that's the legitimate
    # ``</script>`` closer).
    inline_script_open = 'const TOKEN = '
    open_idx = rendered.index(inline_script_open)
    # Find the next legitimate </script> after our inline block (the
    # one closing the inline script tag).
    legitimate_close_idx = rendered.find('</script>', open_idx + len(inline_script_open))
    # Within the JS string literals, no raw </script> should appear.
    js_block = rendered[open_idx:legitimate_close_idx]
    assert '</script>' not in js_block, (
        "Inline script block contains a raw </script> from the payload — "
        "the JS string literal failed to escape the script-context break, "
        "and the payload would close the <script> tag and run as fresh "
        "script (XSS).\nBlock excerpt:\n" + js_block[:500]
    )

    # POSITIVE: the script-safe-escaped form is present.
    assert '\\u003c\\u002fscript\\u003e' in rendered, (
        "Expected script-safe escape of </script> not found. "
        "_script_safe_json must escape `<`, `>`, `/` to \\uXXXX form."
    )


def test_meeting_id_zero_renders():
    """Edge case — meeting_id=0 renders without crash (numeric coercion safe)."""
    html = _render_default(meeting_id=0)
    assert "Meeting #0" in html


def test_empty_chat_id_renders():
    """Edge case — empty chat_id renders (no chat-scope binding in URL)."""
    html = _render_default(chat_id="")
    # No exception, document is generated.
    assert "<!DOCTYPE html>" in html


def test_no_warroom_legacy_routes_in_output():
    """Translation Boundary Audit completeness — no legacy /warroom-* routes survive."""
    html = _render_default()
    legacy_paths = ["/warroom-client.js", "/warroom-music", "/warroom-avatar/", "/api/warroom/"]
    for path in legacy_paths:
        assert path not in html, f"Found legacy route {path!r} in output"


def test_token_query_param_threaded_to_bundle():
    """The bundle URL carries the token (matches upstream's token-passing pattern)."""
    html = _render_default(token="secret_token_xyz")
    # Bundle URL should include token query param.
    pattern = re.compile(r"/api/cabinet/voice/client\.bundle\.js\?token=secret_token_xyz")
    assert pattern.search(html) is not None


# ─── Dynamic-roster tile rendering (PRD-8 Phase 6 follow-up 2026-05-10) ──


def test_dynamic_roster_renders_only_provided_agents():
    """When ``roster`` is provided, ONLY those tiles render (in order).

    Closes the UI-vs-routing gap surfaced by Phase 6 live-test verification:
    the hardcoded 5-tile stub showed Research/Comms/Content/Ops even when
    only Main was actually registered, leading to 'Research is typing'
    indicators on personas that never received turns.
    """
    roster = [
        {"id": "default", "name": "Main", "description": "General ops and triage"},
        {"id": "seo_lead", "name": "SEO Lead", "description": "Tactical SEO + content angles"},
    ]
    html = _render_default(roster=roster)
    # Both roster tiles present.
    assert 'id="agent-default"' in html
    assert 'id="agent-seo_lead"' in html
    assert ">Main<" in html
    assert ">SEO Lead<" in html
    # Hardcoded stubs that are NOT in the roster must NOT render.
    assert 'id="agent-research"' not in html
    assert 'id="agent-comms"' not in html
    assert 'id="agent-content"' not in html
    assert 'id="agent-ops"' not in html


def test_roster_none_falls_through_to_hardcoded_default():
    """Backwards-compat: when ``roster`` is None (pre-Phase-6 meetings with
    NULL broadcast_order, OR any malformed-snapshot fall-through), the
    5-stub hardcoded default renders verbatim. Preserves the
    pre-2026-05-10 behavior for meetings created before the dynamic-roster
    wiring landed."""
    html = _render_default(roster=None)
    for agent_id in ("default", "research", "comms", "content", "ops"):
        assert f'id="agent-{agent_id}"' in html
    # Display names from the hardcoded default.
    for name in ("Main", "Research", "Comms", "Content", "Ops"):
        assert f">{name}<" in html


def test_roster_empty_list_falls_through_to_hardcoded_default():
    """Defensive: a malformed empty roster list also falls through to the
    hardcoded default (rather than rendering zero tiles)."""
    html = _render_default(roster=[])
    # 5-stub default present.
    assert 'id="agent-default"' in html
    assert 'id="agent-ops"' in html


def test_roster_with_malformed_entries_renders_only_well_formed():
    """Malformed entries (non-dict, missing id, empty id) are skipped; if
    every entry is bad, fall through to hardcoded default."""
    roster = [
        {"id": "good_one", "name": "Good", "description": "Real entry"},
        "not_a_dict",  # skipped
        {"name": "no id"},  # skipped (no id)
        {"id": "", "name": "empty id"},  # skipped (empty id)
        {"id": "another_good", "name": "Another", "description": ""},
    ]
    html = _render_default(roster=roster)
    assert 'id="agent-good_one"' in html
    assert 'id="agent-another_good"' in html
    # The bad ones must not appear.
    assert 'id="agent-no_id"' not in html


def test_roster_falls_back_to_id_when_name_missing():
    """When a roster entry has no ``name`` (e.g. persona was deleted
    post-meeting-create and the dashboard handler stubbed it), the tile
    renders with the id as the display name rather than crashing."""
    roster = [{"id": "deleted_persona"}]
    html = _render_default(roster=roster)
    assert 'id="agent-deleted_persona"' in html
    assert ">deleted_persona<" in html  # id-as-name fallback


def test_roster_xss_payload_escaped_in_attributes_and_body():
    """Per ``feedback_security_test_attack_payload.md`` — when ``roster``
    entries come from user-configured profile YAML, every dynamic value
    embedded into HTML must be escaped at the source.

    Hostile payload covers all three contexts:
      * agent_id in attribute (``id="agent-{...}"``)
      * agent_id in URL path (``/avatars/{...}.png``)
      * name + description in HTML body (``<div>{...}</div>``)
    """
    roster = [{
        "id": "x\"><script>alert(1)</script>",
        "name": "<script>alert('name')</script>",
        "description": "<img src=x onerror=alert('desc')>",
    }]
    html = _render_default(roster=roster)
    # NEGATIVE: the raw attack payloads must NOT survive into the rendered
    # output anywhere (script-tag injection, attribute boundary break, etc).
    assert "<script>alert(1)</script>" not in html
    assert "<script>alert('name')</script>" not in html
    assert "<img src=x onerror=alert('desc')>" not in html
    # POSITIVE: escaped forms confirm we ran the values through the
    # escape pipeline (not just dropped them).
    assert "&lt;script&gt;" in html or "%3Cscript%3E" in html, (
        "Hostile name/description must be HTML-escaped to &lt;script&gt; "
        "(html.escape) or URL-encoded to %3Cscript%3E (urllib.parse.quote)."
    )
