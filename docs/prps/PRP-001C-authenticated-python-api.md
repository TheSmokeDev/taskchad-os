# PRP-001C: Authenticated Amendment Rollback Python API

**Status:** implementation-ready after PRP-001A
**Scope:** Python FastAPI boundary, authentication/authorization, route policy; one agent

## Goal and boundaries

Expose A through the existing framework API on port 4322. This slice owns the exact remote actor contract and all HTTP mappings. It does not add CLI, Hono proxy, UI, or chat.

## Source anchors

- `.claude/scripts/dashboard_api.py:1-5,121-123`: dashboard `APIRouter`, inheriting `orchestration/api.py` bearer middleware.
- `.claude/scripts/orchestration/api.py:379-387`: resolved binding on `request.state` and deny-by-default policy enforcement.
- `.claude/scripts/orchestration/route_policy.py:19-24,166+,243-246`: exhaustive `(method, template)` table; `/api/jarvis/status` admin precedent.
- `.claude/scripts/tests/test_jarvis_dashboard_status.py`: API harness.

## Routes and exact identity contract

```http
GET /api/amendments?status=applied&proposal_id=<optional>
POST /api/amendments/{proposal_id}/rollback
Content-Type: application/json
{"reason":"operator explanation"}
```

Add both literal templates to `ROUTE_POLICY` as `admin`:

```python
("GET", "/api/amendments"): "admin"
("POST", "/api/amendments/{proposal_id}/rollback"): "admin"
```

The POST body has only `reason`; reject/ignore-no-extra-fields per the API's established strict model convention, but never accept `actor`. After middleware/admin policy succeeds, derive actor deterministically from the authenticated principal available on `request.state`: use the canonical binding identifier field established by the middleware (inspect the binding type during implementation), formatted `principal:<identifier>`. For the global admin-token path where no tenant binding exists, use constant `principal:global-admin`. Tests lock the actual field and both paths. Never derive actor from bearer text, body, forwarded headers, IP, or user-agent; never log token/reason/content.

GET accepts `status` in `applied|rollback_pending|rolled_back`, default all three. It calls A's non-healing list and filters presentation without writes. Return `{"items": [...]}` with no snapshot contents and no unrestricted absolute paths (return snapshot existence/eligibility, not `snapshot_path`).

## Complete HTTP mapping

| A reason/status | HTTP |
|---|---:|
| completed, reconciled, idempotent already restored | 200 |
| `proposal_not_found` | 404 |
| `duplicate_proposal_id` | 409 |
| `proposal_not_applied`, `target_hash_conflict`, rolled-back target drift | 409 |
| `invalid_proposal_id`, `invalid_actor`, `invalid_reason`, `missing_apply_hashes`, `target_not_allowed`, `target_path_invalid`, `target_missing`, `target_unreadable`, `snapshot_path_invalid`, `snapshot_missing`, `snapshot_unreadable`, `snapshot_hash_mismatch` | 422 |
| `lock_timeout` | 423 |
| `rescue_snapshot_failed`, `ledger_prepare_failed`, `target_restore_failed`, `target_verify_failed`, `ledger_finalize_failed` | 500 |

Unknown domain codes fail closed as sanitized 500. Unauthorized is 401 and authenticated non-admin/tenant is 403 before domain invocation.

## TDD and acceptance

Add `.claude/scripts/tests/test_amendment_api.py`: route-policy exhaustiveness; GET shape/status/filter and non-healing mock; POST body validation; actor derivation for binding/global admin; no body actor; 401/403 prove zero service calls; parameterized complete mapping; sanitized unknown/500; no local path/content/token leakage. Then implement router models/handlers and table entries.

## Validation (repository root)

```bash
cd .claude/scripts
uv run --extra dev pytest tests/test_amendment_api.py tests/test_jarvis_dashboard_status.py tests/test_route_policy.py -q
uv run --extra dev ruff check dashboard_api.py orchestration/route_policy.py tests/test_amendment_api.py
```

Use the actual route-policy test filename if reconnaissance shows a different name and record it in evidence. Run `uv run --extra dev pytest tests -q` before completion.

## Backout

Disable/remove POST first, leave GET diagnostic access if safe, then remove both routes and their `ROUTE_POLICY` entries in the same change (preserve table equality). Retain A's model/status/recovery code; reconcile every pending rollback before any older binary downgrade.
