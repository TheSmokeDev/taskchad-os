# PRP-001D: Dashboard Amendment Rollback Proxy and UI

**Status:** implementation-ready after PRP-001C
**Scope:** Hono thin proxy plus bounded Audit React/Preact UI; one agent

## Goal and boundaries

Provide an operator UI over C without reimplementing domain states, identity, or HTTP mappings. No Python/domain/CLI/chat changes.

## Source anchors

- `dashboard/server/src/routes/jarvis.ts`: authenticated thin-proxy precedent.
- `dashboard/server/src/app.ts` and route mounting modules discovered there.
- `dashboard/web/src/pages/Audit.tsx`: current placeholder.
- `dashboard/server/package.json:10-15` and `dashboard/web/package.json:9-15`: real scripts are `test`, `typecheck`, and (web) `build`.

## Proxy contract

Add `dashboard/server/src/routes/amendments.ts` and mount it using existing auth middleware:

- GET browser route forwards query to Python `GET /api/amendments`.
- POST browser route forwards only `{reason}` to Python `POST /api/amendments/{encoded-id}/rollback`.
- Forward the established server-side framework authorization, never accept/construct actor, never log reason/content/token.
- Preserve upstream status and sanitized JSON body, including 401/403/404/409/422/423/500. Do not reinterpret domain status or return snapshot contents/absolute paths.

## UI contract

Replace only the bounded amendment section of `Audit.tsx` with table columns: ID, target, applied time, status, eligibility/reason, action. Loading, empty, and retryable error states are explicit. Roll Back is disabled when ineligible. Confirmation modal requires a trimmed non-empty reason and sends no actor. Disable duplicate submits. On 200 close modal and refetch; do not optimistically mark restored. On 409 retain context and show conflict; other failures show sanitized actionable text. Never render file or snapshot contents/paths.

## TDD and acceptance

1. Server tests `dashboard/server/src/__tests__/amendments.test.ts`: auth forwarding, encoded ID, query/body exactness, no actor, status/body preservation, mount, no sensitive logging.
2. Web tests `dashboard/web/src/__tests__/audit-amendments.test.tsx`: loading/empty/error; rows; ineligible disabled; modal cancel/reason validation; one request during submit; success refetch; 409 no optimistic success; absence of snapshot/content fields.
3. Implement proxy, then typed API client/types following current web patterns, then UI.

Acceptance evidence includes screenshots only from fixtures if requested; automated tests are authoritative. No live API/profile use.

## Validation (repository root)

```bash
cd dashboard/server
npm test -- src/__tests__/amendments.test.ts
npm run typecheck

cd ../web
npm test -- src/__tests__/audit-amendments.test.tsx
npm run typecheck
npm run build
```

Before completion run `npm test && npm run typecheck` in each package. These commands match the checked-in package scripts; there is no separate server `build` requirement beyond optional `npm run build`.

## Backout

Hide/disable the action first, then remove POST proxy mounting; retain read-only display if useful. Do not downgrade/remove C or A while pending/rolled-back rows exist. A UI rollback performs no ledger migration and deletes no snapshots.
