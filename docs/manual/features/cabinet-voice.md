# Cabinet Voice

Status: Pipecat lifecycle stable; LiveKit live mic proof deferred
Owner: Python orchestration and Cabinet voice adapter
Last updated: 2026-06-04

## What It Does

Cabinet Voice lets an operator open a browser voice room for an existing
Cabinet meeting. The voice surface is an adapter over the same Cabinet meeting
ID, roster snapshot, text orchestrator, transcript stream, and participant
tool policy used by the text room.

The current slice adds a Python-owned single-session lifecycle supervisor. The
dashboard can see voice process status, start one local voice subprocess for
the active Cabinet meeting, stop it, or restart it. Hono and the dashboard stay
thin over the orchestration API; Python remains the source of truth for process
state.

The browser microphone adapter sends PCM16 audio into the Python Pipecat voice
pipeline. `HomieSTT` handles utterance boundaries with VAD stop frames, a short
idle-silence flush, a wall-clock idle timer, and a long max-buffer safety net so
spoken turns do not wait for a follow-up phrase before transcription.

A parallel LiveKit local transport lane is now wired beside Pipecat. The
dashboard can request a room-scoped browser token for `ws://127.0.0.1:7880`,
join the LiveKit room, and publish the microphone. A Python LiveKit Agents
runner can join the same room, receive final STT user-turn events, and post
those transcripts into the same Cabinet text orchestrator with `is_voice=True`,
`audience="auto"`, and no forced target. Spoken/TTS response over LiveKit is
not shipped yet. LiveKit is not the default operator voice path yet; the manual
Chrome mic proof is deferred until the runner lifecycle and observability are
controlled from Python instead of a separate timed CLI process.

## Operator Entry Points

- Chat/Telegram: `/cabinet voice [id]`
- Dashboard: `/voices`
- Cabinet room: mic button on `/cabinet`
- API: `/api/cabinet/voice/ui`
- API lifecycle: `/api/cabinet/voice/status`, `/start`, `/stop`, `/restart`
- LiveKit spike API: `/api/cabinet/voice/livekit/session`
- LiveKit transcript runner:
  `python -m cabinet.voice.livekit_agent --meeting-id <id> --chat-id <chat>`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/cabinet/voice/`, `.claude/scripts/cabinet/voice/lifecycle.py`, `.claude/scripts/cabinet/voice/livekit_session.py`, `.claude/scripts/cabinet/voice/livekit_agent.py`, `.claude/scripts/dashboard_api.py` |
| Chat/router | `.claude/chat/core_handlers.py`, `.claude/scripts/integrations/cabinet_api.py` |
| Hono/dashboard server | `dashboard/server/src/routes/cabinet.ts`, `dashboard/server/src/middleware/auth.ts` |
| Dashboard web | `dashboard/web/src/pages/Voices.tsx`, `dashboard/web/src/pages/Cabinet.tsx`, `dashboard/web/src/lib/cabinet-voice-url.ts` |
| Tests | `.claude/scripts/tests/test_cabinet_voice_*.py`, `dashboard/server/src/__tests__/cabinet.test.ts`, `dashboard/web/src/__tests__/cabinet.test.tsx` |
| Docs/proof | `docs/cabinet-voice-setup.md`, `docs/cabinet-room-manual.md` |

## Safety Boundaries

- Voice does not maintain separate canonical roster truth.
- Voice routes participant turns through the Cabinet orchestration API and
  text orchestrator.
- Unaddressed voice turns use Cabinet's `auto` routing path. Explicit spoken
  targets, pinned targets, and broadcast triggers still bypass the text router
  only when the operator provided that target.
- Hono and dashboard stay thin over Python-owned URL/static/avatar/lifecycle
  endpoints.
- Participant turns preserve the default-deny Cabinet tool/runtime policy.
- The lifecycle supervisor is intentionally single-session for now. It does not
  allocate per-meeting ports or run multiple simultaneous voice rooms.
- STT flush timing, idle timers, and audio corruption guards live in Python.
  The browser page only captures microphone frames and passes PCM bytes to the
  transport.

## How To Run It

```powershell
cd .claude/scripts
uv run thehomie chat -q "/cabinet voice" -Q
```

Dashboard:

```text
http://127.0.0.1:5173/voices
```

The `/voices` page creates or reuses the Cabinet room, polls lifecycle status,
and exposes Start, Stop, and Restart controls for the local voice subprocess.
Open Voice is enabled when the tracked subprocess is ready for that room. The
same page also exposes Join LiveKit for the local OSS transport spike; run a
local LiveKit server first and set LiveKit credentials before using that path.

LiveKit local spike:

```powershell
livekit-server --dev
cd .claude/scripts
uv sync --extra livekit
uv run --extra livekit python -m cabinet.voice.livekit_agent --meeting-id 16 --chat-id cabinet-browser
```

Default local dev credentials are `LIVEKIT_API_KEY=devkey` and
`LIVEKIT_API_SECRET=secret` when the LiveKit dev server is started with
`--dev`. Do not commit those values; set them in the local process environment.
The runner defaults to LiveKit CLI `connect --room cabinet-<meetingId>`; pass
explicit LiveKit CLI args after `--chat-id` when using another mode. Local
transcription defaults to OpenAI STT through the LiveKit OpenAI plugin and
Silero VAD, and requires `OPENAI_API_KEY` in `.env` or the process
environment. Set `CABINET_LIVEKIT_STT_PROVIDER=inference` only when using real
LiveKit Cloud inference credentials.

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_cabinet_voice_html.py tests/test_cabinet_voice_architectural_locks.py tests/test_cabinet_voice_agent_bridge.py tests/test_cabinet_voice_state_machine.py tests/test_cabinet_voice_router.py tests/test_cabinet_voice_personas.py tests/test_cabinet_voice_livekit.py tests/test_cabinet_voice_lifecycle.py tests/test_cabinet_voice_integration.py tests/test_dashboard_api_cabinet_voice.py -q
```

```powershell
cd dashboard/server
npm run test -- src/__tests__/cabinet.test.ts src/__tests__/auth.test.ts src/__tests__/routes-manifest.test.ts
npm run typecheck
```

```powershell
cd dashboard/web
npm run test -- src/__tests__/cabinet.test.tsx src/__tests__/donor-route-manifest.test.ts
npm run typecheck
```

## Public Export Status

This page is public-framework documentation and is exported through the
sanitizer manual allowlist. The deep setup guide is also public-safe. Private
proof artifacts and local process state remain outside the public manual.

## Latest Proof

- State-machine coverage proves the idle timer flushes a current phrase without
  requiring a next audio frame and that continued speech resets the timer before
  transcription.
- LiveKit spike coverage proves the token endpoint validates Cabinet meetings,
  returns room-scoped browser metadata without exposing API secrets, wires a
  transcript-only LiveKit Agents session with provider-selectable STT turn
  handling, and hands final transcripts to Cabinet with `audience="auto"` and
  no forced target.
- Broader voice coverage command passed `138` tests:
  `uv run pytest tests/test_cabinet_voice_html.py tests/test_cabinet_voice_architectural_locks.py tests/test_cabinet_voice_agent_bridge.py tests/test_cabinet_voice_state_machine.py tests/test_cabinet_voice_router.py tests/test_cabinet_voice_personas.py tests/test_cabinet_voice_livekit.py tests/test_cabinet_voice_lifecycle.py tests/test_cabinet_voice_integration.py tests/test_dashboard_api_cabinet_voice.py -q`
- LiveKit runtime proof on 2026-06-04 is intentionally partial: local token
  minting and the Python runner startup worked, including OpenAI STT plus
  Silero VAD registration. The manual Chrome test did not produce confirmed
  `livekit_transcript_handoff` or Cabinet transcript proof, so the LiveKit mic
  UX remains deferred.

## Next Slices

- Run the real Chrome/Edge mic retest when the operator is available and confirm
  live logs show `stt_flush trigger=idle_timer` or `idle_silence` for the
  current phrase without needing a second phrase.
- Add Python-owned LiveKit runner lifecycle/status to `/voices` before the next
  manual LiveKit mic test, so Join LiveKit can start or verify the runner,
  expose log/status errors, and avoid empty-room idle-close timing traps.
- Prove a final LiveKit browser transcript reaches the Cabinet room from the
  real mic transport.
- Add LiveKit spoken/TTS response after the transcript path is proven.
- Decide whether multi-session/per-meeting ports are needed after the
  single-session operator flow is stable.
- Keep future lifecycle expansion in Python; Hono/dashboard remain proxies and
  controls.
