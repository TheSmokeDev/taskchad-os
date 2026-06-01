# Cabinet Voice

Status: V1 launcher over shipped voice adapter
Owner: Python orchestration and Cabinet voice adapter
Last updated: 2026-06-01

## What It Does

Cabinet Voice lets an operator open a browser voice room for an existing
Cabinet meeting. The voice surface is an adapter over the same Cabinet meeting
ID, roster snapshot, text orchestrator, transcript stream, and participant
tool policy used by the text room.

V1 is a launcher/control-plane slice. It creates or opens the current Cabinet
meeting and builds the Python-owned voice URL. It does not own the voice
subprocess lifecycle.

## Operator Entry Points

- Chat/Telegram: `/cabinet voice [id]`
- Dashboard: `/voices`
- Cabinet room: mic button on `/cabinet`
- API: `/api/cabinet/voice/ui`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/cabinet/voice/`, `.claude/scripts/dashboard_api.py` |
| Chat/router | `.claude/chat/core_handlers.py`, `.claude/scripts/integrations/cabinet_api.py` |
| Hono/dashboard server | `dashboard/server/src/routes/cabinet.ts`, `dashboard/server/src/middleware/auth.ts` |
| Dashboard web | `dashboard/web/src/pages/Voices.tsx`, `dashboard/web/src/pages/Cabinet.tsx`, `dashboard/web/src/lib/cabinet-voice-url.ts` |
| Tests | `.claude/scripts/tests/test_cabinet_voice_*.py`, `dashboard/server/src/__tests__/cabinet.test.ts`, `dashboard/web/src/__tests__/cabinet.test.tsx` |
| Docs/proof | `docs/cabinet-voice-setup.md`, `docs/cabinet-room-manual.md` |

## Safety Boundaries

- Voice does not maintain separate canonical roster truth.
- Voice routes participant turns through the Cabinet orchestration API and
  text orchestrator.
- Hono and dashboard stay thin over Python-owned URL/static/avatar endpoints.
- Participant turns preserve the default-deny Cabinet tool/runtime policy.
- V1 does not start, stop, or supervise the voice subprocess.

## How To Run It

```powershell
cd .claude/scripts
uv run thehomie chat -q "/cabinet voice" -Q
```

Dashboard:

```text
http://127.0.0.1:5173/voices
```

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_dashboard_api_cabinet_voice.py tests/test_cabinet_voice_html.py -q
```

```powershell
cd dashboard/server
npm run test -- src/__tests__/cabinet.test.ts src/__tests__/auth.test.ts src/__tests__/routes-manifest.test.ts
npm run typecheck
```

```powershell
cd dashboard/web
npm run test -- src/__tests__/cabinet.test.tsx
npm run typecheck
```

## Public Export Status

This page is public-framework documentation and is exported through the
sanitizer manual allowlist. The deep setup guide is also public-safe. Private
proof artifacts and local process state remain outside the public manual.

## Next Slices

- Add Python-owned voice subprocess status and start controls.
- Surface those controls thinly in Hono and `/voices`.
- Keep lifecycle supervision separate from the V1 launcher URL builder.
