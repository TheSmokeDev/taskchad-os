# Cabinet Room Manual

This is the on-demand context manual for the Cabinet dashboard room. Load this when you need to understand what the Cabinet does, how a browser message becomes persona responses, which slice owns each behavior, or how to test a Cabinet change end to end.

## Table of Contents

1. What Cabinet Is
2. Operator Quickstart
3. Dashboard Surface Map
4. Vertical Slice Architecture
5. Runtime Turn Flow
6. Roster And Participant State
7. Audience And Slash Commands
8. Persona Identity And Tools
9. Voice Relationship
10. Testing And Validation
11. Common Failure Modes
12. File Ownership Map
13. Current Scope And Non-Goals

## 1. What Cabinet Is

Cabinet is a shared multi-persona room. The operator speaks once, and the room routes the turn to one or more Homie personas. Each participant answers in the same transcript, using its own profile identity, memory, runtime configuration, and tool policy.

Cabinet is text-first. Voice is an adapter over the same canonical room state, not a separate truth source.

The shipped browser room supports:

- one stable browser chat room per `chatId`
- persisted meeting state
- persisted transcript rows
- roster snapshot reuse
- mention targeting
- full-room fanout
- in-room slash commands
- participant add/remove/pin controls
- SSE progress events
- profile-backed participant execution

## 2. Operator Quickstart

Start the three local services:

```powershell
cd .claude/scripts
uv run python -m orchestration.run_api
```

```powershell
cd dashboard/server
$env:DASHBOARD_DEV_MODE_NO_AUTH='true'
npm run start
```

```powershell
cd dashboard/web
npm run dev -- --host 127.0.0.1
```

Open:

```text
http://127.0.0.1:5173/cabinet
```

Smoke tests from the browser:

```text
yooo everybody check in
```

Expected: every active participant responds.

```text
@content @finance you there?
```

Expected: only Content and Finance respond, each speaking as itself.

```text
/help
/all quick room check
/pin @marketing
/unpin
```

Expected: slash commands are handled by the server and are not sent to the LLM as prompt text.

## 3. Dashboard Surface Map

| Surface | Location | Purpose |
|---|---|---|
| Cabinet page | `dashboard/web/src/pages/Cabinet.tsx` | Main browser room. Owns layout, roster chips, meeting list, selected room state, and wiring to the stream/composer/transcript components. |
| Composer | `dashboard/web/src/components/CabinetComposer.tsx` | Builds `/api/cabinet/send` bodies. No mention defaults to `audience="all"`; mentions default to `audience="mentions"`. |
| Transcript | `dashboard/web/src/components/CabinetTranscript.tsx` | Renders baseline transcript rows plus live SSE events. Shows router decisions, typing states, agent replies, and errors. |
| Stream helper | `dashboard/web/src/lib/cabinet-stream.ts` | Connects browser `EventSource` to the Hono stream endpoint and handles transcript fetches. |
| Persona ID translation | `dashboard/web/src/lib/translate-personas.ts` | Translates public/browser `main` style IDs and backend `default` style IDs at the UI boundary. |
| Hono proxy | `dashboard/server/src/routes/cabinet.ts` | Forwards browser requests to the orchestration API and translates persona IDs at the boundary. |

The browser should stay a thin client. It can choose audience mode and render state, but room truth belongs to the backend.

## 4. Vertical Slice Architecture

Cabinet crosses five layers. Keep each concern in its owning slice.

| Layer | Owner | What It Owns | What It Must Not Own |
|---|---|---|---|
| Browser UI | `dashboard/web/src/pages` and `dashboard/web/src/components` | Usable room experience, composer body shape, visible roster controls, transcript rendering. | Runtime routing, roster mutation truth, profile execution, direct database access. |
| Hono dashboard server | `dashboard/server/src/routes/cabinet.ts` | Auth/dev-mode boundary, request proxying, persona ID translation, SSE byte streaming. | LLM calls, roster decisions, transcript persistence. |
| Orchestration API | `.claude/scripts/dashboard_api.py` | HTTP endpoints, chat scoping, command interception, meeting open/send/end/add/remove/pin, background turn kickoff. | Browser rendering and provider-specific runtime shortcuts. |
| Cabinet backend | `.claude/scripts/cabinet/` | Room state, roster snapshots, routing, participant execution, SSE channel events, transcript persistence. | Dashboard layout, Hono auth policy, external framework export rules. |
| Runtime layer | `.claude/scripts/runtime/` | Lane resolution, provider adapters, SDK option construction, tool allow/deny forwarding. | Cabinet-specific routing decisions or persona roster mutation. |

This is the rule of thumb: UI chooses intent, API validates and persists, Cabinet decides room behavior, runtime executes the selected participant.

## 5. Runtime Turn Flow

Browser happy path:

```text
CabinetComposer
  -> POST /api/cabinet/send on Hono :3141
  -> POST /api/cabinet/send on orchestration API :4322
  -> dashboard_api.cabinet_send()
  -> cabinet.room_commands parses slash commands, if any
  -> cabinet.text_orchestrator.handle_text_turn()
  -> room_state.load_meeting_roster()
  -> router decision / audience selection
  -> _run_agent_turn() per selected participant
  -> runtime.lane_router.run_with_runtime_lanes(RuntimeRequest)
  -> transcript rows + SSE events
  -> CabinetTranscript renders events
```

Key invariant: the chat process and orchestration API process are separate processes. Do not cross that boundary with in-process channel registries. Browser, Telegram, and other external surfaces must reach Cabinet through the orchestration HTTP API.

## 6. Roster And Participant State

Roster membership is snapshot-based.

| Data | Source |
|---|---|
| Active meeting roster | `cabinet_text_meetings.roster_json` |
| Broadcast order | `cabinet_meetings.broadcast_order` |
| Pinned participant | `cabinet_text_meetings.pinned_agent` |
| Transcript rows | `cabinet_transcripts` |
| Live progress | `cabinet.meeting_channel.MeetingChannel` SSE events |

`room_state.py` is the roster state owner. It loads the snapshot before falling back to live `get_roster()`. Add/remove operations update `cabinet_text_meetings.roster_json` and `cabinet_meetings.broadcast_order` in one transaction, then emit `meeting_state_update`.

The live persona registry is not the room truth once a meeting exists. It is only the fallback for creating or repairing a snapshot.

## 7. Audience And Slash Commands

Browser send body supports an additive audience contract:

| Input | Audience | Result |
|---|---|---|
| `@sales status?` | `mentions` | Sales responds. |
| `@sales @marketing plan?` | `mentions` | Sales and Marketing respond in roster order. |
| `what is everyone seeing?` | `all` | Every active participant responds. |
| Programmatic targets | `targets` + `targetAgentIds` | Only the explicit target IDs respond. |
| No explicit audience | `auto` | Backend uses mention, pin, sticky, router, and social fallback behavior. |

Slash commands are parsed server-side by `room_commands.py`:

| Command | Behavior |
|---|---|
| `/help` | Returns command help as a system note. |
| `/all <message>` | Sends `<message>` to every active participant. |
| `/add @id` | Adds an available persona to this meeting snapshot. |
| `/remove @id` | Removes a persona from this meeting snapshot. |
| `/pin @id` | Pins unmentioned turns to a participant. |
| `/unpin` | Clears the pin. |
| `/voice` | Opens or links the voice adapter for the same meeting. |
| `/end` | Ends the meeting. |

Command text must never enter the LLM prompt. The server handles it first, then either mutates room state or rewrites the turn into normal message text.

## 8. Persona Identity And Tools

Each non-default participant turn resolves the live Homie profile for that participant ID. The snapshot controls membership/order/display; the profile controls identity, memory, environment, runtime settings, tool allowlist, and voice config.

Every participant turn gets a Cabinet Room Identity Contract before profile memory. That contract says:

- answer as this participant only
- do not claim to be Main/default unless the selected participant is Main/default
- if multiple participants were mentioned, speak only for yourself
- do not say other tagged participants are unavailable or only part of a voice room
- do not mention routing internals, handoffs, or rebuild status unless asked about system status

Tool policy is default-deny. If a participant has no explicit Cabinet tool allowlist, the runtime request must preserve:

```python
allowed_tools = []
disallowed_tools = ["*"]
```

The Claude SDK adapter also passes the no-tools base set so the CLI does not expose its default tool surface. This prevents Cabinet personas from using `ToolSearch` or `SendMessage` inside the room instead of answering in the transcript.

## 9. Voice Relationship

Voice is not a parallel Cabinet brain. It is an ingress/egress adapter over the same room model.

Voice should:

- use the same meeting ID
- respect the same roster snapshot
- route selected spoken targets to the same `handle_text_turn()` path
- consume SSE events from the text orchestrator
- render or speak the selected participant responses

Voice should not:

- maintain separate canonical roster truth
- invoke LLMs directly
- override text-room persona identity
- describe text Cabinet participants as unavailable because voice is rebuilding

See `docs/cabinet-voice-setup.md` for voice-specific setup.

## 10. Testing And Validation

Focused backend tests:

```bash
cd .claude/scripts
uv run pytest tests/test_cabinet_room_state.py tests/test_cabinet_room_commands.py tests/test_cabinet_text_orchestrator.py tests/test_cabinet_profile_execution.py -q
```

Runtime/tool policy tests:

```bash
cd .claude/scripts
uv run pytest tests/test_cabinet_tool_policy.py tests/test_runtime_request_cabinet_fields.py -q
```

API tests:

```bash
cd .claude/scripts
uv run pytest tests/test_cabinet_api.py tests/test_cabinet_http_client.py -q
```

Dashboard tests:

```bash
cd dashboard/server
npm test -- cabinet.test.ts
npm run typecheck
```

```bash
cd dashboard/web
npm test -- cabinet.test.tsx
npm run typecheck
```

Live browser smoke:

1. Open `http://127.0.0.1:5173/cabinet`.
2. Send a no-mention message and confirm all active participants respond.
3. Send `@content @finance you there?`.
4. Confirm Content speaks as Content and Finance speaks as Finance.
5. Confirm no answer says "Main Homie", "not separate sessions", "voice War Room", "rebuild", or "handoff".
6. Try `/all`, `/pin`, `/unpin`, `/add`, and `/remove`.

SSE replay check:

```powershell
curl.exe --max-time 4 -N "http://127.0.0.1:3141/api/cabinet/stream?meetingId=<id>&chatId=cabinet-browser"
```

Transcript DB check:

```powershell
@'
import sqlite3
meeting_id = 15
conn = sqlite3.connect('.claude/data/dashboard.db')
for row in conn.execute(
    'select id, speaker, text from cabinet_transcripts where meeting_id=? order by id desc limit 20',
    (meeting_id,),
):
    print(row)
'@ | uv run python -
```

## 11. Common Failure Modes

| Symptom | Likely Cause | Fix |
|---|---|---|
| Browser says nothing happened | UI did not scroll, SSE disconnected, or API is down. | Check `/api/health` on ports 4322 and 3141, replay SSE, and inspect `cabinet_transcripts`. |
| Only one persona responds to `@a @b` | Audience mode was not `mentions`, or mention parsing/ID translation failed. | Inspect browser send body and Hono `@main`/`@default` translation. |
| Persona says "Main Homie here" | Cabinet identity prompt did not override stale profile memory. | Verify the RuntimeRequest system prompt contains `Cabinet Room Identity Contract`. |
| Persona says it sent pings or is waiting on itself | Tool surface leaked `ToolSearch`/`SendMessage`. | Verify default-deny no-tools path in `runtime/claude_sdk.py` and `cabinet/tool_policy.py`. |
| Removed participant still responds | Meeting roster snapshot and broadcast order drifted. | Use `room_state.py` transaction helpers; do not mutate only one table. |
| Voice and text disagree | Voice is using separate state or stale router files. | Route voice through the same meeting ID and `handle_text_turn()` path. |
| Slash command text reaches the LLM | Command interception happened too late. | Parse with `room_commands.py` before enqueueing the text turn. |

## 12. File Ownership Map

| File | Ownership |
|---|---|
| `.claude/scripts/cabinet/room_state.py` | Meeting roster snapshot load/mutate/serialize. |
| `.claude/scripts/cabinet/room_commands.py` | In-room slash command parsing and command payloads. |
| `.claude/scripts/cabinet/text_orchestrator.py` | Turn acceptance, routing, participant execution, transcript writes, SSE event emission. |
| `.claude/scripts/cabinet/text_router.py` | Router/classifier helpers for primary/intervener decisions. |
| `.claude/scripts/cabinet/tool_policy.py` | Cabinet tool and MCP allow/deny policy. |
| `.claude/scripts/dashboard_api.py` | Orchestration API endpoints and background task kickoff. |
| `.claude/scripts/integrations/cabinet_api.py` | HTTP client used by chat/router surfaces. |
| `dashboard/server/src/routes/cabinet.ts` | Hono proxy and persona ID translation. |
| `dashboard/web/src/pages/Cabinet.tsx` | Dashboard page composition and room-level UI state. |
| `dashboard/web/src/components/CabinetComposer.tsx` | Send body and audience choice. |
| `dashboard/web/src/components/CabinetTranscript.tsx` | Transcript and SSE event rendering. |
| `dashboard/web/src/lib/cabinet-stream.ts` | Browser stream and transcript client. |

## 13. Current Scope And Non-Goals

Current scope:

- local operator dashboard
- text Cabinet room
- persisted meetings and transcripts
- roster snapshot semantics
- profile-backed participant execution
- local voice adapter over the same room

Non-goals for this slice:

- multi-user external Cabinet meetings
- remote hosted dashboard deployment
- public internet auth hardening
- audience fanout beyond the local room contract
- memory ingestion from every Cabinet turn
- replacing the core runtime layer with Cabinet-specific provider shortcuts

When extending Cabinet, keep the room state canonical and preserve the vertical slice boundaries above.
