# Cabinet Voice

Status: Single-session lifecycle and browser STT idle timer shipped
Owner: Python orchestration and Cabinet voice adapter
Last updated: 2026-06-02

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

The browser microphone adapter sends PCM16 audio into the Python voice pipeline.
`HomieSTT` handles utterance boundaries with VAD stop frames, a short
idle-silence flush, a wall-clock idle timer, and a long max-buffer safety net so
spoken turns do not wait for a follow-up phrase before transcription.

## Operator Entry Points

- Chat/Telegram: `/cabinet voice [id]`
- Dashboard: `/voices`
- Cabinet room: mic button on `/cabinet`
- API: `/api/cabinet/voice/ui`
- API lifecycle: `/api/cabinet/voice/status`, `/start`, `/stop`, `/restart`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/cabinet/voice/`, `.claude/scripts/cabinet/voice/lifecycle.py`, `.claude/scripts/dashboard_api.py` |
| Chat/router | `.claude/chat/core_handlers.py`, `.claude/scripts/integrations/cabinet_api.py` |
| Hono/dashboard server | `dashboard/server/src/routes/cabinet.ts`, `dashboard/server/src/middleware/auth.ts` |
| Dashboard web | `dashboard/web/src/pages/Voices.tsx`, `dashboard/web/src/pages/Cabinet.tsx`, `dashboard/web/src/lib/cabinet-voice-url.ts` |
| Tests | `.claude/scripts/tests/test_cabinet_voice_*.py`, `dashboard/server/src/__tests__/cabinet.test.ts`, `dashboard/web/src/__tests__/cabinet.test.tsx` |
| Docs/proof | `docs/cabinet-voice-setup.md`, `docs/cabinet-room-manual.md` |

## Safety Boundaries

- Voice does not maintain separate canonical roster truth.
- Voice routes participant turns through the Cabinet orchestration API and
  text orchestrator.
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
Open Voice is enabled when the tracked subprocess is ready for that room.

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_cabinet_voice_state_machine.py tests/test_cabinet_voice_lifecycle.py tests/test_dashboard_api_cabinet_voice.py tests/test_cabinet_voice_html.py -q
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
- Focused voice coverage command:
  `uv run pytest tests/test_cabinet_voice_state_machine.py tests/test_cabinet_voice_html.py tests/test_cabinet_voice_lifecycle.py tests/test_dashboard_api_cabinet_voice.py -q`

## Next Slices

- Run the real Chrome/Edge mic retest when the operator is available and confirm
  live logs show `stt_flush trigger=idle_timer` or `idle_silence` for the
  current phrase without needing a second phrase.
- Decide whether multi-session/per-meeting ports are needed after the
  single-session operator flow is stable.
- Keep future lifecycle expansion in Python; Hono/dashboard remain proxies and
  controls.
