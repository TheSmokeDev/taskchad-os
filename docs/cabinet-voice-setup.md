# Cabinet Voice Setup

The Homie's cabinet voice feature gives operators a single-user voice meeting with their cabinet personas through a browser. The stable path ports from ClaudeClaw's war room: a server-rendered HTML page drives a Pipecat WebSocket client that connects to a small Python voice subprocess. The voice subprocess routes turns through the same brain (`text_orchestrator.handle_text_turn`) that powers Telegram cabinet meetings, so persona behavior stays identical across surfaces.

A parallel LiveKit local transport spike now exists beside Pipecat. It does not replace the Pipecat path yet. The current LiveKit slice mints room-scoped browser tokens, lets the dashboard join a local LiveKit room and publish the microphone, runs a Python LiveKit Agents transcript receiver, and posts final STT user turns into Cabinet's normal text router. Spoken LiveKit TTS is a later slice.

For the broader Cabinet dashboard manual and room-state vertical slice, see `docs/cabinet-room-manual.md`.

## Architecture at a glance

| Component | Location | Role |
|---|---|---|
| Browser HTML page | `GET /api/cabinet/voice/ui?token=&meetingId=&chatId=` | Server-rendered page, served by the orchestration API on port 4322. |
| Pipecat client bundle | `GET /api/cabinet/voice/client.bundle.js` | Vendored from ClaudeClaw's `warroom/client.bundle.js` (BSD-2 attributed). |
| Voice subprocess | `python -m cabinet.voice.voice_server --meeting-id N` | Pipecat `WebsocketServerTransport`, default port 7860. |
| Lifecycle supervisor | `.claude/scripts/cabinet/voice/lifecycle.py` | Python-owned single-session status/start/stop/restart process control. |
| Voice pipeline | `transport.input → HomieSTT → AgentRouter → HomieAgentBridge → HomieTTS → transport.output` | Verbatim port of ClaudeClaw `warroom/server.py:751-758` legacy mode. |
| LiveKit session/token spike | `GET /api/cabinet/voice/livekit/session` + `.claude/scripts/cabinet/voice/livekit_session.py` | Issues local room metadata and browser participant tokens without exposing LiveKit secrets. |
| LiveKit transcript runner | `python -m cabinet.voice.livekit_agent --meeting-id N` | Joins the LiveKit room through AgentServer, receives final STT user-turn events, and posts transcripts to Cabinet with `is_voice=True`, `audience="auto"`, and no forced target. |
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
| `CABINET_VOICE_START_TIMEOUT_S` | `10` | Seconds the supervisor waits for the subprocess ready handshake before marking start failed. |
| `CABINET_VOICE_BRIDGE_TIMEOUT_S` | `60` | Per-turn timeout before the bridge surfaces a friendly fallback. |
| `CABINET_VOICE_ROSTER_PATH` | `<tempdir>/cabinet-roster.json` | Optional override for the file-IPC roster path used by `AgentRouter` for name-prefix routing. Default resolves via `tempfile.gettempdir()` (cross-platform: `/tmp` on POSIX, `%TEMP%` on Windows). |
| `CABINET_VOICE_PIN_PATH` | `<tempdir>/cabinet-voice-pin.json` | Optional override for the click-to-pin file. Same `tempfile.gettempdir()` resolution as above. |
| `ORCHESTRATION_API_BASE_URL` | `http://127.0.0.1:4322` | Base URL the voice subprocess uses to reach the cabinet HTTP API. |
| `ORCHESTRATION_API_TOKEN` | (empty) | Bearer token if the orchestration API process has authentication enabled. |
| `CABINET_LIVEKIT_URL` | `ws://127.0.0.1:7880` | LiveKit signal URL for the local OSS spike. |
| `LIVEKIT_API_KEY` | (empty) | Server-side LiveKit API key used to mint browser tokens and run the Python LiveKit agent; never returned by the API. |
| `LIVEKIT_API_SECRET` | (empty) | Server-side LiveKit API secret for token/agent work; never returned by the API. |
| `CABINET_LIVEKIT_TOKEN_TTL_S` | `1800` | Browser participant token TTL, clamped between 60 seconds and 24 hours. |
| `CABINET_LIVEKIT_ROOM_PREFIX` | `cabinet` | Prefix for deterministic LiveKit room names such as `cabinet-42`. |
| `CABINET_LIVEKIT_AGENT_NAME` | `cabinet-livekit-agent` | Expected Python LiveKit agent name/identity prefix. |
| `CABINET_LIVEKIT_STT_PROVIDER` | `openai` | STT provider for the LiveKit runner. Use `openai` for local dev with `OPENAI_API_KEY`; use `inference` for LiveKit Cloud inference credentials. |
| `CABINET_LIVEKIT_STT_MODEL` | `gpt-4o-mini-transcribe` | STT model passed to the selected provider. |
| `CABINET_LIVEKIT_STT_LANGUAGE` | `en` | STT language hint passed to LiveKit Agents. |
| `CABINET_LIVEKIT_TURN_DETECTION` | `stt` | Turn detection mode passed through `turn_handling`. |

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

### From the dashboard

Open `/voices` to create or reuse the current Cabinet room. The page polls
`/api/cabinet/voice/status`, exposes Start, Stop, and Restart controls, and
enables Open Voice after the Python supervisor reports the subprocess ready for
that meeting. The page also exposes Join LiveKit for the local OSS spike. The
mic button on `/cabinet` opens Pipecat voice for the selected room.

Lifecycle endpoints are mounted on the Python orchestration API and proxied by
Hono:

```text
GET  /api/cabinet/voice/status?meetingId=42&chatId=YOUR_CHAT_ID
POST /api/cabinet/voice/start
POST /api/cabinet/voice/stop
POST /api/cabinet/voice/restart
GET  /api/cabinet/voice/livekit/session?meetingId=42&chatId=YOUR_CHAT_ID
```

The POST body is:

```json
{"meetingId": 42, "chatId": "YOUR_CHAT_ID"}
```

`start` and `restart` validate that the Cabinet meeting exists and is open.
`stop` validates scope when a meeting id is supplied, but can stop an ended
meeting's tracked voice process. The lifecycle supervisor is deliberately
single-session: a different active meeting returns a conflict instead of
allocating another port.

The LiveKit session endpoint validates the Cabinet meeting and chat scope
before minting a token. The response includes `roomName`, `serverUrl`,
`participantIdentity`, `participantToken`, `agentName`, and `agentIdentity`;
it never includes `LIVEKIT_API_KEY` or `LIVEKIT_API_SECRET`.

### LiveKit local spike

Start a local LiveKit server:

```powershell
livekit-server --dev
```

Install the optional Python dependencies when running the LiveKit agent path:

```powershell
cd .claude/scripts
uv sync --extra livekit
```

For the local dev server, set the local process environment to the dev
credentials before starting the orchestration API:

```powershell
$env:CABINET_LIVEKIT_URL = "ws://127.0.0.1:7880"
$env:LIVEKIT_API_KEY = "devkey"
$env:LIVEKIT_API_SECRET = "secret"
```

Then open `/voices` and use Join LiveKit. The first shipped Python contract is
the final-transcript handoff helper used by a LiveKit Agents session.

Start the transcript receiver in a second terminal:

```powershell
cd .claude/scripts
uv run --extra livekit python -m cabinet.voice.livekit_agent --meeting-id 16 --chat-id cabinet-browser
```

With no trailing LiveKit CLI args, the runner defaults to:

```text
connect --room cabinet-16
```

You can pass explicit LiveKit CLI args after the Cabinet flags when needed:

```powershell
uv run --extra livekit python -m cabinet.voice.livekit_agent --meeting-id 16 --chat-id cabinet-browser connect --room cabinet-16
```

The local LiveKit server is the media transport. The default STT path uses
the LiveKit OpenAI plugin with `OPENAI_API_KEY` loaded from `.env` or the
process environment, plus LiveKit Silero VAD because OpenAI STT is not a
native streaming STT. To use LiveKit Cloud inference instead, set
`CABINET_LIVEKIT_STT_PROVIDER=inference`, choose an inference model such as
`deepgram/nova-3`, and provide real LiveKit Cloud credentials. Local dev
`devkey/secret` are valid for the local room/token server, but not for
LiveKit Cloud inference. LiveKit audio response/TTS is intentionally out of
scope until the transcript path is proven locally.

For phone testing, open the dashboard over the machine's Tailscale URL and
include the Cabinet chat scope, for example:

```text
http://<TAILSCALE_IP>:5173/voices?chatId=cabinet-browser
```

### Direct browser URL

```
http://127.0.0.1:4322/api/cabinet/voice/ui?token=&meetingId=42&chatId=YOUR_CHAT_ID
```

The page renders the cinematic intro overlay — click anywhere to enter, then click "Start Meeting" to connect to the WebSocket transport.

## Routing

The Pipecat voice subprocess's `AgentRouter` supports these routing modes:

1. **Broadcast triggers** — saying "everyone, status update" routes to `agent_id="all"` and the bridge broadcasts to each persona in the meeting's roster snapshot.
2. **Name prefix** — "research, summarize this" routes to the research persona.
3. **Pinned agent** — write `{"agent": "comms"}` to the pin path (`<tempdir>/cabinet-voice-pin.json` by default, `CABINET_VOICE_PIN_PATH` to override) or use the dashboard click-to-pin; unprefixed utterances then route to comms.
4. **Default auto-route** — everything else posts into Cabinet with
   `audience="auto"` and no `targetAgentId`, so the normal text router chooses
   the responder from mentions, pin, sticky context, router decision, and social
   fallback.

The bridge passes `targetAgentId` only when speech or pinning selected a
specific target. Broadcast and explicit targets keep their deterministic
behavior; unaddressed speech no longer forces the main persona.

## Kill switches

Two kill switches gate cabinet voice:

* `HOMIE_KILLSWITCH_VOICE=disabled` — refuses TTS at the Phase 4 cascade entry. Refusal counter increments exactly once per refusal (no double-count).
* `HOMIE_KILLSWITCH_CABINET=disabled` — refusal arrives via SSE `{type:"error"}` event from Phase 5a (cabinet POST is fire-and-forget). The voice page renders the friendly error message in the transcript area.

Synchronous endpoints (`/api/cabinet/new`, `/api/cabinet/end`) raise HTTP 503 on cabinet-disabled; the chat handlers catch and surface the friendly message.

## Logs and diagnostics

Voice subprocess logs go to stderr when run in the foreground. When started by
the lifecycle supervisor, logs are written under the active profile log
directory at `cabinet-voice/cabinet-voice-<meetingId>.log` and surfaced in the
status payload as `logPath`. Every log call site that touches dynamic args
wraps them in the `redact()` helper so secrets in URLs / exception messages get
scrubbed before they land. `HOMIE_REDACT_SECRETS=false` disables redaction for
triage scenarios.

The voice subprocess emits a JSON handshake on stdout when it's ready:

```json
{"ws_url": "ws://localhost:7860", "status": "ready", "transport": "websocket", "mode": "legacy"}
```

Operators or supervisors can parse this to confirm the subprocess is listening before opening the browser page.

The lifecycle status payload also reports `status`, `active`, `matchesMeeting`,
`pid`, `port`, `bind`, `wsUrl`, `startedAt`, `readyAt`, `stoppedAt`,
`uptimeS`, `lastError`, `logPath`, and capability flags for Pipecat, ffmpeg,
STT, and TTS.

## Troubleshooting

* **"Pipecat client bundle did not load"** — verify `GET /api/cabinet/voice/client.bundle.js` returns 200. The bundle is in `.claude/scripts/cabinet/voice/static/client.bundle.js`. If missing, re-vendor from upstream.
* **Browser can't reach WebSocket** — check `CABINET_VOICE_PORT` is open and `CABINET_VOICE_BIND` matches the host the browser hits. Default is loopback only. If `/voices` says the subprocess is stopped, start it from the page first.
* **Start returns conflict** — another Cabinet meeting owns the single active local voice subprocess. Stop that session or restart for the intended meeting.
* **Mic permission blocked** — Chrome only grants mic access on `https://` or `http://localhost`. If accessing from a remote machine, use SSH port-forwarding or set up TLS.
* **"The X agent took too long to respond"** — the bridge timeout fired before Phase 5a returned an `agent_done` event. Either Phase 5a is slow (check its logs), or the SSE stream got disconnected. Increase `CABINET_VOICE_BRIDGE_TIMEOUT_S` or restart the orchestration API.
* **"Cabinet declined this turn"** — kill switch refusal. Check `HOMIE_KILLSWITCH_CABINET` env state and the audit log.
* **No persona reply audio** — verify the Phase 4 TTS cascade has at least one configured provider (`ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID`, etc.).
* **LiveKit STT returns `401 Unauthorized` with `devkey/secret`** — local
  LiveKit dev credentials are valid for the local room server and browser
  token minting, but not for LiveKit Cloud inference. Use the default
  `CABINET_LIVEKIT_STT_PROVIDER=openai` path with `OPENAI_API_KEY`, or provide
  real LiveKit Cloud inference credentials before setting
  `CABINET_LIVEKIT_STT_PROVIDER=inference`.
* **LiveKit runner starts, then disconnects before testing** — direct
  `connect --room cabinet-<id>` can close after the room stays empty. Restart
  the runner immediately before joining from Chrome, or implement the next
  Python-owned LiveKit lifecycle slice so `/voices` owns runner start/status.
* **LiveKit manual mic test feels connected but no Cabinet turn appears** —
  check for `livekit_transcript_handoff` in the runner log and a matching
  `cabinet_transcripts` row before calling the test successful. If those are
  absent, the browser transport/STT path is still unproven even if room join
  and runner startup worked.

## Out of scope for Phase 6

* LiveKit spoken/TTS response.
* External participant adapter (Phase 6d).
* Hermes push-to-talk + VAD desktop mode.
* Cinematic intro music asset.
* Multiple simultaneous local voice subprocesses.
* Per-meeting dynamic port allocation.

These are tracked as separate sub-PRDs.

## Architecture references

* Upstream port sources: `https://github.com/seandearnaley/claudeclaw-os` — `warroom/{server,router,personas,agent_bridge,config}.py` + `src/warroom-html.ts` (BSD-2 attributed in `cabinet/voice/static/client.bundle.js` header).
* In-repo Phase 4 cascade: `.claude/chat/voice.py`.
* In-repo Phase 5a brain: `.claude/scripts/cabinet/text_orchestrator.py`.
* In-repo Phase 5b HTTP client: `.claude/scripts/integrations/cabinet_api.py`.
* Phase 6 voice surface: `.claude/scripts/cabinet/voice/`.
