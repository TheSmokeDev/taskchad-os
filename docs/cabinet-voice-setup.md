# Cabinet Voice Setup

The Homie's cabinet voice feature gives operators a single-user voice meeting with their cabinet personas through a browser. The architecture ports verbatim from ClaudeClaw's war room: a server-rendered HTML page drives a Pipecat WebSocket client that connects to a small Python voice subprocess. The voice subprocess routes turns through the same brain (`text_orchestrator.handle_text_turn`) that powers Telegram cabinet meetings, so persona behavior stays identical across surfaces.

## Architecture at a glance

| Component | Location | Role |
|---|---|---|
| Browser HTML page | `GET /api/cabinet/voice/ui?token=&meetingId=&chatId=` | Server-rendered page, served by the orchestration API on port 4322. |
| Pipecat client bundle | `GET /api/cabinet/voice/client.bundle.js` | Vendored from ClaudeClaw's `warroom/client.bundle.js` (BSD-2 attributed). |
| Voice subprocess | `python -m cabinet.voice.voice_server --meeting-id N` | Pipecat `WebsocketServerTransport`, default port 7860. |
| Voice pipeline | `transport.input → HomieSTT → AgentRouter → HomieAgentBridge → HomieTTS → transport.output` | Verbatim port of ClaudeClaw `warroom/server.py:751-758` legacy mode. |
| Persona reasoning | Phase 5a `text_orchestrator.handle_text_turn` over HTTP | Voice never invokes an LLM directly; it consumes the Phase 5a SSE stream. |

## Prerequisites

1. **Phase 4 voice cascade** configured. At least one TTS provider must be available (`ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID`, or `EDGE_TTS_VOICE`, or any other Phase 4 cascade member). The `voice` kill-switch is honored at the cascade entry point.
2. **Phase 5a + 5b cabinet** running. The orchestration API process must be up on `127.0.0.1:4322` (`uv run python -m orchestration.run_api` from `.claude/scripts/`).
3. **Pipecat installed.** Phase 6 keeps Pipecat as an optional dependency; install it on the voice host:

   ```bash
   cd .claude/scripts
   uv add 'pipecat-ai[websocket,silero]==0.0.108'
   ```

4. **Browser with mic permission.** Chrome and Edge are the proven targets (the upstream client.bundle.js is built against their WebRTC mic capture path). The voice meeting page asks for mic permission on the first click; subsequent visits reuse the granted permission.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CABINET_VOICE_PORT` | `7860` | WebSocket transport port for the voice subprocess. |
| `CABINET_VOICE_BIND` | `127.0.0.1` | Bind interface. Set to `0.0.0.0` to expose on LAN (operator opt-in; matches Phase 7a default-bind-loopback rule). |
| `CABINET_VOICE_BRIDGE_TIMEOUT_S` | `60` | Per-turn timeout before the bridge surfaces a friendly fallback. |
| `CABINET_VOICE_ROSTER_PATH` | `<tempdir>/cabinet-roster.json` | Optional override for the file-IPC roster path used by `AgentRouter` for name-prefix routing. Default resolves via `tempfile.gettempdir()` (cross-platform: `/tmp` on POSIX, `%TEMP%` on Windows). |
| `CABINET_VOICE_PIN_PATH` | `<tempdir>/cabinet-voice-pin.json` | Optional override for the click-to-pin file. Same `tempfile.gettempdir()` resolution as above. |
| `ORCHESTRATION_API_BASE_URL` | `http://127.0.0.1:4322` | Base URL the voice subprocess uses to reach the cabinet HTTP API. |
| `ORCHESTRATION_API_TOKEN` | (empty) | Bearer token if the orchestration API process has authentication enabled. |

## Per-persona voice configuration

Each cabinet-eligible persona gets its own voice through the persona's `<profile>/config.yaml`. The cabinet section accepts four optional fields:

```yaml
cabinet:
  # TTS voice id (provider-specific format).
  voice_id: "YOUR_ELEVENLABS_VOICE_ID"
  # Provider key. One of: elevenlabs, edge, openai, gemini, mistral,
  # gradium, kokoro, kittentts, macos_say.
  voice_provider: "elevenlabs"
  # Voice persona system prompt (replaces the upstream AGENT_PERSONAS dict).
  voice_persona_prompt: |
    You are the SEO Lead. Tactical, concise, opinionated.
  # Optional avatar image (relative to profile root or absolute).
  avatar_path: "static/seo-lead.png"
  # Cabinet-eligibility tools (Phase 5a).
  tools:
    - get_time
    - list_agents
```

When `voice_id` and `voice_provider` are unset, the cabinet voice falls through to the Phase 4 cascade default (`ELEVENLABS_VOICE_ID` env, then Gradium, then Mistral, etc.). The fallback never blocks meeting creation.

When `voice_persona_prompt` is unset, the persona gets a synthesized prompt built from its display name + role description, plus the verbatim `SHARED_RULES` block from upstream.

## Starting a voice meeting

Three entry points:

### From Telegram

```
/cabinet voice
```

Without an explicit meeting id, this creates a fresh cabinet meeting and returns a browser URL. With an id (`/cabinet voice 42`), it verifies the meeting exists in the current chat scope and returns the URL for the existing meeting.

### From the CLI

```bash
# Create + open a new voice meeting
thehomie cabinet voice
```

(Mirrors the Telegram surface; see `.claude/chat/cli.py` for the wrapper.)

### Direct browser URL

```
http://127.0.0.1:4322/api/cabinet/voice/ui?token=&meetingId=42&chatId=YOUR_CHAT_ID
```

The page renders the cinematic intro overlay — click anywhere to enter, then click "Start Meeting" to connect to the WebSocket transport.

## Routing

The voice subprocess's `AgentRouter` (verbatim port of `warroom/router.py`) supports four routing modes per upstream:

1. **Broadcast triggers** — saying "everyone, status update" routes to `agent_id="all"` and the bridge broadcasts to each persona in the meeting's roster snapshot.
2. **Name prefix** — "research, summarize this" routes to the research persona.
3. **Pinned agent** — write `{"agent": "comms"}` to the pin path (`<tempdir>/cabinet-voice-pin.json` by default, `CABINET_VOICE_PIN_PATH` to override) or use the dashboard click-to-pin; unprefixed utterances then route to comms.
4. **Default** — everything else falls through to the main persona.

The bridge passes the routed agent id to Phase 5a's orchestrator as `targetAgentId`, so the Haiku router does NOT re-evaluate the routing — voice respects the spoken/pinned target.

## Kill switches

Two kill switches gate cabinet voice:

* `HOMIE_KILLSWITCH_VOICE=disabled` — refuses TTS at the Phase 4 cascade entry. Refusal counter increments exactly once per refusal (no double-count).
* `HOMIE_KILLSWITCH_CABINET=disabled` — refusal arrives via SSE `{type:"error"}` event from Phase 5a (cabinet POST is fire-and-forget). The voice page renders the friendly error message in the transcript area.

Synchronous endpoints (`/api/cabinet/new`, `/api/cabinet/end`) raise HTTP 503 on cabinet-disabled; the chat handlers catch and surface the friendly message.

## Logs and diagnostics

Voice subprocess logs go to stderr (visible when run in the foreground). Every log call sites that touch dynamic args wraps them in the `redact()` helper so secrets in URLs / exception messages get scrubbed before they land. `HOMIE_REDACT_SECRETS=false` disables redaction for triage scenarios.

The voice subprocess emits a JSON handshake on stdout when it's ready:

```json
{"ws_url": "ws://localhost:7860", "status": "ready", "transport": "websocket", "mode": "legacy"}
```

Operators or supervisors can parse this to confirm the subprocess is listening before opening the browser page.

## Troubleshooting

* **"Pipecat client bundle did not load"** — verify `GET /api/cabinet/voice/client.bundle.js` returns 200. The bundle is in `.claude/scripts/cabinet/voice/static/client.bundle.js`. If missing, re-vendor from upstream.
* **Browser can't reach WebSocket** — check `CABINET_VOICE_PORT` is open and `CABINET_VOICE_BIND` matches the host the browser hits. Default is loopback only.
* **Mic permission blocked** — Chrome only grants mic access on `https://` or `http://localhost`. If accessing from a remote machine, use SSH port-forwarding or set up TLS.
* **"The X agent took too long to respond"** — the bridge timeout fired before Phase 5a returned an `agent_done` event. Either Phase 5a is slow (check its logs), or the SSE stream got disconnected. Increase `CABINET_VOICE_BRIDGE_TIMEOUT_S` or restart the orchestration API.
* **"Cabinet declined this turn"** — kill switch refusal. Check `HOMIE_KILLSWITCH_CABINET` env state and the audit log.
* **No persona reply audio** — verify the Phase 4 TTS cascade has at least one configured provider (`ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID`, etc.).

## Out of scope for Phase 6

* Multi-party voice rooms (Phase 6c — self-hosted LiveKit SFU).
* External participant adapter (Phase 6d).
* Hermes push-to-talk + VAD desktop mode.
* Cinematic intro music asset.
* Live React management UI (Phase 6 follow-up).

These are tracked as separate sub-PRDs.

## Architecture references

* Upstream port sources: `https://github.com/seandearnaley/claudeclaw-os` — `warroom/{server,router,personas,agent_bridge,config}.py` + `src/warroom-html.ts` (BSD-2 attributed in `cabinet/voice/static/client.bundle.js` header).
* In-repo Phase 4 cascade: `.claude/chat/voice.py`.
* In-repo Phase 5a brain: `.claude/scripts/cabinet/text_orchestrator.py`.
* In-repo Phase 5b HTTP client: `.claude/scripts/integrations/cabinet_api.py`.
* Phase 6 voice surface: `.claude/scripts/cabinet/voice/`.
