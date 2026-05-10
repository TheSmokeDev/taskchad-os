"""Server-rendered cabinet voice meeting HTML page.

Mechanical Python port of ClaudeClaw ``src/warroom-html.ts:1-2057`` —
``getWarRoomHtml(token, chatId, warroomPort)`` returns the full HTML+CSS+JS
for the war room. The Homie variant adds ``meeting_id`` (Phase 5a meeting
binding) and applies the Translation Boundary Audit substitutions.

Translation Boundary Audit (per PRP §"Translation Boundary Audit"):

  * ``/warroom-client.js?token=...&v=...``    -> ``/api/cabinet/voice/client.bundle.js?token=...&v=...``
  * ``/warroom-music?token=...``              -> dropped (Phase 6 MVP — no entrance music)
  * ``/warroom-music-upload``                 -> dropped (no upload UI)
  * ``/api/agents/{id}/avatar?token=...``     -> ``/api/cabinet/voice/avatars/{id}.png`` (server reads token)
  * ``/api/warroom/start``                    -> caller-built ws_url passed via template, no fetch
  * ``/api/warroom/meeting/start``            -> Phase 5a ``cabinet_api.create_meeting()`` (caller-side)
  * ``/api/warroom/meeting/transcript``       -> dropped (transcripts persisted via Phase 5a SSE pipeline)
  * ``WARROOM_PORT`` (env)                    -> ``ws_port`` (template var)
  * ``CHAT_ID = ${jsChatId}`` (single shared) -> per-Telegram-chat scoped
  * ``WARROOM_CHAT_ID="warroom"``             -> ``meetingId`` template var (Phase 5a binding)

Phase 6 scope (MVP):

  * Cinematic intro overlay with stage + 5 default avatar tiles (matches
    upstream visual identity but trimmed to ~300 LOC of CSS).
  * Voice meeting controls (start/end + mic toggle + transcript area).
  * Pipecat WebSocket client wired via the vendored bundle at
    ``/api/cabinet/voice/client.bundle.js``.
  * RTVI ``onServerMessage`` handler renders ``agent_selected``,
    ``hand_down``, and ``agent_error`` events from the bridge.

Out of Phase 6 (Phase 6+ enhancement): full cinematic intro music, mic
waveform canvas, mode selector (direct/auto), per-agent click-to-pin,
cost display polling. The page shipped here is the functional MVP — full
upstream visual polish can be backported in a follow-up.
"""

from __future__ import annotations

import html
import json
import urllib.parse
from typing import Any


# PRD-8 Phase 6 v2 R3 fix-pass 2026-05-10 (B1-R3) — script-safe JSON.
#
# ``json.dumps()`` does NOT escape ``</script>``, ``<!--``, or other HTML
# parser sentinels. When a JSON value is embedded inside an inline
# ``<script>`` block (rather than e.g. a ``<script type="application/json">``
# island that the browser does NOT execute), an attacker-supplied string
# containing ``</script><script>alert(1)</script>`` would close the script
# tag and run as a fresh script — XSS in the script-context.
#
# ``_script_safe_json()`` post-processes ``json.dumps`` output to escape
# ``<``, ``>``, ``&``, ``/`` to their ``\\uXXXX`` form. This is the same
# pattern used by Flask's ``htmlsafe_dumps`` and Django's ``escapejs`` — it
# keeps the output valid JSON AND valid JS, but prevents the HTML parser
# from leaving the script context.
_HTML_SCRIPT_ESCAPES = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    "/": "\\u002f",
}


def _script_safe_json(value: Any) -> str:
    """Serialize ``value`` to JSON safe for embedding inside an inline
    ``<script>`` block. Escapes ``<``, ``>``, ``&``, ``/`` so an attacker
    cannot break out of the script context via ``</script>`` or close
    HTML comments via ``<!--``.
    """
    raw = json.dumps(value)
    for ch, esc in _HTML_SCRIPT_ESCAPES.items():
        raw = raw.replace(ch, esc)
    return raw


def get_voice_meeting_html(
    token: str,
    meeting_id: int,
    chat_id: str,
    ws_port: int,
) -> str:
    """Return the HTML page for a single voice cabinet meeting.

    Args:
        token: orchestration API bearer token (passed back to the server
            on RTVI ``server-message`` events). Empty string in loopback
            no-token mode.
        meeting_id: Phase 5a cabinet meeting id.
        chat_id: Telegram chat id (for chat-scope binding on send/end).
        ws_port: WebSocket transport port (separate process; matches
            ``CABINET_VOICE_PORT`` env, default 7860).

    Returns:
        Complete HTML document as a string.

    Anti-pattern compliance:
      * Rule 1 — all params required; no def-time bind to module/config
        constants (template substitutions happen inside the function body).
      * Rule 3 N/A (no optional-provider SDK touched here; pure string
        template).
    """
    # PRD-8 Phase 6 v2 R2 fix-pass 2026-05-10 (B1-R2): URL-encoded token for
    # use in server-rendered URL query strings. ``html.escape`` alone is NOT
    # sufficient — it does NOT URL-encode ``&`` or ``=``, so a token like
    # ``a&b=c`` would split the browser-parsed query and fail the middleware
    # token check on bundle/avatar requests. ``urllib.parse.quote`` with
    # ``safe=''`` percent-encodes ALL reserved chars; the trailing
    # ``html.escape`` is defensive for HTML attribute context.
    safe_token_qs = html.escape(urllib.parse.quote(token, safe=""), quote=True)
    safe_chat_id = html.escape(chat_id, quote=True)
    safe_meeting_id = html.escape(str(meeting_id), quote=True)
    # PRD-8 Phase 6 v2 R3 fix-pass 2026-05-10 (B1-R3): script-safe JSON
    # serialization. Plain ``json.dumps()`` escapes JS string delimiters
    # (``"``, ``\\``) but does NOT escape ``</script>`` — a token like
    # ``</script><script>alert(1)</script>`` would close the inline script
    # tag and inject. ``_script_safe_json()`` post-processes ``json.dumps``
    # output to escape ``<``, ``>``, ``&``, ``/`` to their ``\\uXXXX``
    # equivalents, keeping the value valid JSON+JS while preventing the
    # parser from leaving the script context.
    js_token = _script_safe_json(token)
    js_chat_id = _script_safe_json(chat_id)
    js_meeting_id = _script_safe_json(meeting_id)
    js_ws_port = _script_safe_json(ws_port)

    # Pre-format the avatars and the inline script to keep the f-string
    # clean. Five default ClaudeClaw personas; Phase 6 follow-up can wire
    # this from /api/cabinet/voice/agents.
    avatars_html = _build_default_agent_tiles_html(safe_token_qs)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cabinet Voice Meeting #{safe_meeting_id}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #050505;
    color: #e0e0e0;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    height: 100vh;
    overflow: hidden;
  }}

  /* Cinematic intro overlay (port of warroom-html.ts:33-81). */
  .intro-overlay {{
    position: fixed;
    inset: 0;
    background: #000;
    z-index: 100;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    transition: opacity 1.5s ease;
    cursor: pointer;
  }}
  .intro-overlay.fade-out {{ opacity: 0; pointer-events: none; }}
  .intro-title {{
    font-size: 48px;
    font-weight: 800;
    letter-spacing: 12px;
    text-transform: uppercase;
    color: #fff;
    opacity: 0;
    animation: titleReveal 2s ease forwards 0.5s;
  }}
  .intro-subtitle {{
    font-size: 14px;
    letter-spacing: 4px;
    text-transform: uppercase;
    color: #3b82f6;
    opacity: 0;
    margin-top: 12px;
    animation: titleReveal 1.5s ease forwards 1.5s;
  }}
  .intro-line {{
    width: 0;
    height: 1px;
    background: linear-gradient(90deg, transparent, #3b82f6, transparent);
    margin-top: 20px;
    animation: lineExpand 2s ease forwards 1s;
  }}
  @keyframes titleReveal {{
    from {{ opacity: 0; transform: translateY(8px); }}
    to {{ opacity: 1; transform: translateY(0); }}
  }}
  @keyframes lineExpand {{
    from {{ width: 0; }}
    to {{ width: 300px; }}
  }}

  /* Main layout (port of warroom-html.ts:212-249). */
  .app {{
    height: 100vh;
    display: flex;
    flex-direction: column;
    opacity: 0;
    transition: opacity 1s ease;
  }}
  .app.visible {{ opacity: 1; }}

  .header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 24px;
    background: rgba(10,10,10,0.95);
    border-bottom: 1px solid rgba(255,255,255,0.06);
    z-index: 10;
  }}
  .header h1 {{
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 3px;
    text-transform: uppercase;
    color: #3b82f6;
  }}
  .header .meta {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: #555;
  }}

  .main {{ flex: 1; display: flex; overflow: hidden; }}

  /* Agent panel (port of warroom-html.ts:253-322). */
  .agents-panel {{
    width: 300px;
    background: rgba(8,8,8,0.95);
    border-right: 1px solid rgba(255,255,255,0.04);
    padding: 20px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    overflow-y: auto;
  }}
  .panel-label {{
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: #333;
    margin-bottom: 4px;
  }}

  .agent-card {{
    background: rgba(255,255,255,0.02);
    border: 1px solid rgba(255,255,255,0.04);
    border-radius: 12px;
    padding: 14px 16px;
    display: flex;
    align-items: center;
    gap: 12px;
    transition: all 0.4s cubic-bezier(0.16, 1, 0.3, 1);
    position: relative;
  }}
  .agent-card.speaking {{
    border-color: rgba(34, 197, 94, 0.4);
    background: rgba(34, 197, 94, 0.05);
    box-shadow: 0 0 20px rgba(34, 197, 94, 0.08);
  }}
  .agent-card.hand-up {{
    border-color: rgba(251, 191, 36, 0.7);
    background: rgba(251, 191, 36, 0.08);
    box-shadow: 0 0 26px rgba(251, 191, 36, 0.2);
    transform: translateY(-3px) scale(1.02);
  }}
  .agent-card.hand-up::before {{
    content: '✋';
    position: absolute;
    top: -10px;
    right: -6px;
    font-size: 18px;
    animation: hand-wave 0.9s ease-out;
  }}
  @keyframes hand-wave {{
    0%   {{ transform: rotate(-20deg) scale(0.4); opacity: 0; }}
    30%  {{ transform: rotate(8deg) scale(1.2); opacity: 1; }}
    60%  {{ transform: rotate(-4deg) scale(1); opacity: 1; }}
    100% {{ transform: rotate(0deg) scale(1); opacity: 1; }}
  }}

  .agent-avatar {{
    width: 42px;
    height: 42px;
    border-radius: 50%;
    background-size: cover;
    background-position: center;
    flex-shrink: 0;
    border: 2px solid rgba(255,255,255,0.1);
  }}
  .agent-info {{ flex: 1; min-width: 0; }}
  .agent-name {{ font-size: 13px; font-weight: 700; color: #e0e0e0; }}
  .agent-role {{ font-size: 11px; color: #555; margin-top: 2px; }}
  .agent-indicator {{
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #1a1a1a;
    flex-shrink: 0;
    transition: all 0.3s;
  }}
  .agent-indicator.online {{ background: #22c55e; box-shadow: 0 0 8px rgba(34,197,94,0.4); }}

  /* Transcript (port of warroom-html.ts:425-476). */
  .transcript-panel {{
    flex: 1;
    display: flex;
    flex-direction: column;
    background: rgba(5,5,5,0.95);
  }}
  .transcript-area {{
    flex: 1;
    overflow-y: auto;
    padding: 24px;
    scroll-behavior: smooth;
  }}
  .transcript-area::-webkit-scrollbar {{ width: 4px; }}
  .transcript-area::-webkit-scrollbar-thumb {{ background: #222; border-radius: 4px; }}
  .transcript-placeholder {{
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    gap: 8px;
  }}
  .transcript-placeholder .icon {{ font-size: 32px; opacity: 0.15; }}
  .transcript-placeholder .text {{ font-size: 13px; color: #333; }}

  .transcript-entry {{
    margin-bottom: 20px;
    animation: entrySlide 0.3s ease;
  }}
  @keyframes entrySlide {{
    from {{ opacity: 0; transform: translateY(6px); }}
    to {{ opacity: 1; transform: translateY(0); }}
  }}
  .transcript-speaker {{
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1px;
    text-transform: uppercase;
    margin-bottom: 4px;
  }}
  .transcript-speaker.user {{ color: #60a5fa; }}
  .transcript-speaker.agent {{ color: #22c55e; }}
  .transcript-speaker.system {{ color: #333; }}
  .transcript-text {{
    font-size: 14px;
    line-height: 1.6;
    color: #aaa;
  }}
  .transcript-text.system-text {{ color: #555; font-size: 12px; }}
  .transcript-text.error-text {{ color: #ef4444; font-size: 13px; }}

  /* Controls (port of warroom-html.ts:478-537). */
  .controls {{
    padding: 16px 24px;
    background: rgba(10,10,10,0.95);
    border-top: 1px solid rgba(255,255,255,0.04);
    display: flex;
    align-items: center;
    gap: 14px;
  }}
  .btn {{
    padding: 10px 28px;
    border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.08);
    background: rgba(255,255,255,0.03);
    color: #ccc;
    font-family: 'Inter', sans-serif;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
    letter-spacing: 0.3px;
  }}
  .btn:hover {{ background: rgba(255,255,255,0.06); }}
  .btn.start {{ background: rgba(34,197,94,0.1); border-color: rgba(34,197,94,0.2); color: #22c55e; }}
  .btn.start:hover {{ background: rgba(34,197,94,0.15); }}
  .btn.end {{ background: rgba(239,68,68,0.1); border-color: rgba(239,68,68,0.2); color: #ef4444; }}
  .btn.end:hover {{ background: rgba(239,68,68,0.15); }}

  .mic-btn {{
    width: 52px;
    height: 52px;
    border-radius: 50%;
    border: 2px solid rgba(255,255,255,0.08);
    background: rgba(255,255,255,0.02);
    color: #555;
    font-size: 20px;
    cursor: pointer;
    transition: all 0.3s;
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .mic-btn:hover {{ border-color: rgba(255,255,255,0.15); color: #888; }}
  .mic-btn.recording {{
    background: rgba(239,68,68,0.15);
    border-color: rgba(239,68,68,0.4);
    color: #ef4444;
    box-shadow: 0 0 20px rgba(239,68,68,0.15);
    animation: pulse 1.5s ease-in-out infinite;
  }}
  .mic-btn:disabled {{ opacity: 0.4; cursor: not-allowed; }}

  @keyframes pulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.4; }}
  }}

  .status-text {{
    font-size: 12px;
    color: #555;
    font-family: 'JetBrains Mono', monospace;
    white-space: nowrap;
    flex: 1;
    text-align: right;
  }}
</style>
</head>
<body>

<!-- Cinematic intro overlay (port of warroom-html.ts:691-697 + click-to-enter). -->
<div class="intro-overlay" id="introOverlay" onclick="enterCabinet()">
  <div class="intro-title">Cabinet</div>
  <div class="intro-line"></div>
  <div class="intro-subtitle">Voice Meeting</div>
  <div style="margin-top:36px;font-size:11px;color:#666;letter-spacing:2px">Click anywhere to begin</div>
</div>

<!-- Pipecat client SDK (vendored bundle from ClaudeClaw warroom/client.bundle.js). -->
<script src="/api/cabinet/voice/client.bundle.js?token={safe_token_qs}"></script>

<!-- Main app (hidden during intro). -->
<div class="app" id="app">
  <div class="header">
    <h1>Cabinet Voice</h1>
    <div class="meta">Meeting #{safe_meeting_id}</div>
  </div>

  <div class="main">
    <div class="agents-panel">
      <div class="panel-label">Your Team</div>
      {avatars_html}
    </div>

    <div class="transcript-panel">
      <div class="transcript-area" id="transcript">
        <div class="transcript-placeholder" id="placeholder">
          <div class="icon">\U0001F3A4</div>
          <div class="text">Start a meeting to begin</div>
        </div>
      </div>

      <div class="controls">
        <button class="btn start" id="meetingBtn" onclick="toggleMeeting()">Start Meeting</button>
        <button class="mic-btn" id="micBtn" onclick="toggleMic()" disabled>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
            <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
            <line x1="12" y1="19" x2="12" y2="23"/>
            <line x1="8" y1="23" x2="16" y2="23"/>
          </svg>
        </button>
        <div class="status-text" id="statusText">ready</div>
      </div>
    </div>
  </div>
</div>

<script>
const TOKEN = {js_token};
const CHAT_ID = {js_chat_id};
const MEETING_ID = {js_meeting_id};
const WS_PORT = {js_ws_port};
const API_BASE = window.location.origin;

let pipecatClient = null;
let currentTransport = null;
let meetingActive = false;
let micActive = false;

// Verbatim port of warroom-html.ts:810-813 — token+chatId encoded into ws url.
function buildWsUrl() {{
  const proto = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
  // The cabinet voice subprocess listens on its own WS port (default 7860),
  // separate from the dashboard origin. Use window.location.hostname so LAN
  // exposure (CABINET_VOICE_BIND=0.0.0.0) works without manual config.
  return proto + window.location.hostname + ':' + WS_PORT
       + '?token=' + encodeURIComponent(TOKEN)
       + '&meetingId=' + encodeURIComponent(MEETING_ID)
       + '&chatId=' + encodeURIComponent(CHAT_ID);
}}

function enterCabinet() {{
  const overlay = document.getElementById('introOverlay');
  const app = document.getElementById('app');
  if (overlay) {{
    overlay.classList.add('fade-out');
    setTimeout(function() {{
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    }}, 1500);
  }}
  if (app) app.classList.add('visible');
}}

// Verbatim port of warroom-html.ts:1122-1176 handleServerMessage.
// Renders agent_selected / hand_down / agent_error events from the bridge.
let handUpTimer = null;
function handleServerMessage(msg) {{
  try {{
    if (!msg) return;
    const data = msg.data || msg;
    if (!data || typeof data !== 'object') return;
    const ev = data.event;
    const agent = data.agent;

    if (ev === 'hand_down') {{
      if (handUpTimer) {{ clearTimeout(handUpTimer); handUpTimer = null; }}
      if (agent) {{
        const c = document.getElementById('agent-' + agent);
        if (c) c.classList.remove('hand-up');
      }} else {{
        document.querySelectorAll('.agent-card').forEach(function(c) {{ c.classList.remove('hand-up'); }});
      }}
      return;
    }}

    // Server tells us a sub-agent call failed. Surface as a visible system
    // entry so the operator sees kill-switch refusals + bridge errors.
    if (ev === 'agent_error') {{
      const label = (agent || 'Agent');
      const errMsg = (typeof data.error === 'string' && data.error) ? data.error : 'unknown error';
      addTranscriptEntry('system', label + ' failed: ' + errMsg, null, true);
      return;
    }}

    if (ev !== 'agent_selected') return;
    if (!agent) return;
    const card = document.getElementById('agent-' + agent);
    if (!card) return;

    document.querySelectorAll('.agent-card').forEach(function(c) {{ c.classList.remove('hand-up'); }});
    card.classList.add('hand-up');
    addTranscriptEntry('system', (agent.charAt(0).toUpperCase() + agent.slice(1)) + ' is taking this.');

    if (handUpTimer) clearTimeout(handUpTimer);
    handUpTimer = setTimeout(function() {{
      card.classList.remove('hand-up');
      handUpTimer = null;
    }}, 6000);
  }} catch (e) {{
    console.warn('[Cabinet] handleServerMessage failed', e);
  }}
}}

// Port of warroom-html.ts:1178-1212 addTranscriptEntry (trimmed — no
// transcript persistence call; Phase 5a's SSE pipeline handles that).
function addTranscriptEntry(speaker, text, agentId, isError) {{
  const area = document.getElementById('transcript');
  const ph = document.getElementById('placeholder');
  if (ph && ph.parentNode) ph.parentNode.removeChild(ph);

  const entry = document.createElement('div');
  entry.className = 'transcript-entry';

  const speakerEl = document.createElement('div');
  const speakerClass = speaker === 'You' ? 'user' : (speaker === 'system' ? 'system' : 'agent');
  speakerEl.className = 'transcript-speaker ' + speakerClass;
  speakerEl.textContent = speaker === 'system' ? '' : speaker;

  const textEl = document.createElement('div');
  let textClass = 'transcript-text';
  if (speaker === 'system') textClass += ' system-text';
  if (isError) textClass += ' error-text';
  textEl.className = textClass;
  textEl.textContent = text;

  if (speaker !== 'system') entry.appendChild(speakerEl);
  entry.appendChild(textEl);
  area.appendChild(entry);
  area.scrollTop = area.scrollHeight;

  if (agentId) setAgentSpeaking(agentId);
}}

function setAgentSpeaking(agentId) {{
  document.querySelectorAll('.agent-card').forEach(function(c) {{
    c.classList.remove('speaking');
  }});
  const card = document.getElementById('agent-' + agentId);
  if (card) card.classList.add('speaking');
}}

// Port of warroom-html.ts:1712-2053 toggleMeeting (trimmed — no fetch
// to upstream's start route since we have a direct ws_url from template).
async function toggleMeeting() {{
  const btn = document.getElementById('meetingBtn');
  if (!meetingActive) {{
    btn.textContent = 'Connecting...';
    btn.disabled = true;
    btn.className = 'btn';
    document.getElementById('statusText').textContent = 'connecting...';

    if (!window.PipecatWarRoom || !window.PipecatWarRoom.PipecatClient) {{
      document.getElementById('statusText').textContent = 'pipecat client failed to load';
      addTranscriptEntry('system', 'Pipecat client bundle did not load. Check /api/cabinet/voice/client.bundle.js.', null, true);
      btn.textContent = 'Start Meeting';
      btn.className = 'btn start';
      btn.disabled = false;
      return;
    }}

    try {{
      const wsUrl = buildWsUrl();
      const WebSocketTransport = window.PipecatWarRoom.WebSocketTransport;
      const PipecatClient = window.PipecatWarRoom.PipecatClient;
      currentTransport = new WebSocketTransport({{ wsUrl: wsUrl }});
      pipecatClient = new PipecatClient({{
        transport: currentTransport,
        enableMic: true,
        enableCam: false,
        callbacks: {{
          onConnected: function() {{
            console.log('[Cabinet] Connected');
            meetingActive = true;
            btn.textContent = 'End Meeting';
            btn.className = 'btn end';
            btn.disabled = false;
            document.getElementById('micBtn').disabled = false;
            micActive = true;
            document.getElementById('micBtn').classList.add('recording');
            document.getElementById('statusText').textContent = 'meeting active';
            document.querySelectorAll('.agent-indicator').forEach(function(s) {{ s.classList.add('online'); }});
            addTranscriptEntry('system', 'Meeting started. Speak now.');
          }},
          onDisconnected: function() {{
            console.log('[Cabinet] Disconnected');
            meetingActive = false;
            btn.textContent = 'Start Meeting';
            btn.className = 'btn start';
            btn.disabled = false;
            document.getElementById('micBtn').disabled = true;
            document.getElementById('micBtn').classList.remove('recording');
            document.getElementById('statusText').textContent = 'disconnected';
          }},
          onUserTranscript: function(data) {{
            if (data && data.final) addTranscriptEntry('You', data.text);
          }},
          onBotTranscript: function(data) {{
            if (data) addTranscriptEntry('Agent', data.text || '', 'main');
          }},
          onServerMessage: function(msg) {{ handleServerMessage(msg); }},
          onError: function(err) {{
            console.error('[Cabinet] error', err);
            const m = (err && err.message) ? err.message : 'connection error';
            addTranscriptEntry('system', 'Error: ' + m, null, true);
          }},
        }},
      }});
      await pipecatClient.connect({{ wsUrl: wsUrl }});
    }} catch (e) {{
      console.error('[Cabinet] connect failed', e);
      addTranscriptEntry('system', 'Connect failed: ' + ((e && e.message) || 'unknown'), null, true);
      btn.textContent = 'Start Meeting';
      btn.className = 'btn start';
      btn.disabled = false;
    }}
  }} else {{
    // End meeting.
    btn.textContent = 'Ending...';
    btn.disabled = true;
    try {{
      if (pipecatClient) {{ await pipecatClient.disconnect(); pipecatClient = null; }}
    }} catch (e) {{ /* ignore */ }}
    currentTransport = null;
    meetingActive = false;
    btn.textContent = 'Start Meeting';
    btn.className = 'btn start';
    btn.disabled = false;
    document.getElementById('micBtn').disabled = true;
    document.getElementById('micBtn').classList.remove('recording');
    document.querySelectorAll('.agent-indicator').forEach(function(s) {{ s.classList.remove('online'); }});
    document.getElementById('statusText').textContent = 'ended';
    addTranscriptEntry('system', 'Meeting ended.');
  }}
}}

function toggleMic() {{
  if (!pipecatClient) return;
  micActive = !micActive;
  const btn = document.getElementById('micBtn');
  try {{
    pipecatClient.enableMic(micActive);
  }} catch (e) {{ /* ignore */ }}
  if (micActive) {{
    btn.classList.add('recording');
    document.getElementById('statusText').textContent = 'listening...';
  }} else {{
    btn.classList.remove('recording');
    document.getElementById('statusText').textContent = 'muted';
  }}
}}

// Cleanup on page unload (port of warroom-html.ts:1703-1709).
function __cabinetVoiceCleanup() {{
  try {{ if (pipecatClient) {{ pipecatClient.disconnect(); pipecatClient = null; }} }} catch(e) {{ }}
}}
window.addEventListener('pagehide', __cabinetVoiceCleanup);
window.addEventListener('beforeunload', __cabinetVoiceCleanup);
</script>
</body>
</html>"""


def _build_default_agent_tiles_html(safe_token_qs: str) -> str:
    """Build the default agent-card tiles for the sidebar.

    Port of warroom-html.ts:706-725 stage avatars. Five default ClaudeClaw
    personas — Phase 6 follow-up can render dynamically from the cabinet
    roster API.

    PRD-8 Phase 6 v2 R2 fix-pass 2026-05-10 (B1-R2): the ``safe_token_qs``
    argument is the URL-encoded-then-HTML-escaped token. Avatar URLs use it
    in ``?token=…`` query strings so tokens containing ``&`` / ``=`` /
    URL-reserved chars don't split browser-parsed query params and fail
    the middleware token check.
    """
    # PRD-8 Phase 6 v2 fix-pass 2026-05-10 (B2 fix) — Q4 lock: emit the
    # canonical internal id ``"default"`` as the wire string at the HTML
    # boundary. Upstream ClaudeClaw uses ``"main"`` end-to-end (Python +
    # client-side). The Homie's persona registry stores the persona under
    # id ``"default"`` (per Phase 5a roster snapshot), so the avatar URL
    # must match: ``/api/cabinet/voice/avatars/default.png`` resolves
    # through ``personas.load_persona_config("default")`` without a
    # boundary-translation hop. The display name stays "Main" — only the
    # wire id is canonicalized.
    agents = [
        ("default", "Main", "General ops & triage"),
        ("research", "Research", "Web research & competitive intel"),
        ("comms", "Comms", "Email, Slack, customer comms"),
        ("content", "Content", "Writing, scripts, creative direction"),
        ("ops", "Ops", "Calendar, automations, scheduled tasks"),
    ]
    parts: list[str] = []
    for agent_id, name, role in agents:
        # Note: the avatar URL points to the Phase 6 cabinet voice avatar
        # endpoint. Per Translation Boundary Audit (R1 v2 B6 fix), this is
        # the renamed Homie deviation — upstream's /warroom-avatar/:id was
        # already removed, so the canonical replacement is
        # /api/cabinet/voice/avatars/{id}.png. The endpoint enforces
        # tokenized access via the dashboard auth middleware.
        parts.append(
            f'<div class="agent-card" id="agent-{agent_id}">'
            f'<div class="agent-avatar" style="background-image:url(&apos;/api/cabinet/voice/avatars/{agent_id}.png?token={safe_token_qs}&apos;)"></div>'
            f'<div class="agent-info">'
            f'<div class="agent-name">{name}</div>'
            f'<div class="agent-role">{role}</div>'
            f"</div>"
            f'<div class="agent-indicator" id="status-{agent_id}"></div>'
            f"</div>"
        )
    return "\n".join(parts)


__all__ = [
    "get_voice_meeting_html",
]
