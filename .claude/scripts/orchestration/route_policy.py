"""Route policy registry + real deny-by-default enforcement (Tenant Isolation v0 Phase B).

This is the WS2-owned authorization layer that turns "authenticated" into
"authorized". The shared ``orchestration/api.py`` middleware authenticates a
bearer into a ``TenantBinding`` (workspace_id + persona_scope + is_admin); THIS
module decides whether that binding is ALLOWED to reach the concrete route.

Three pieces:

1. ``resolve_route_template(request)`` — the NB1 mechanism (proven in the
   route-resolution spike). FastAPI/Starlette ``@app.middleware("http")`` runs
   BEFORE route matching, so ``request.scope["route"]`` is unset there. This
   helper manually replays Starlette route matching against ``app.routes`` —
   descending through ``include_router`` mounts (the dashboard is mounted that
   way at ``orchestration/api.py``) — to recover the LITERAL route template
   (e.g. ``/api/convoy/{convoy_id}``) from inside the middleware, with the exact
   path-parameter names and the HTTP method dimension.

2. ``ROUTE_POLICY`` — an explicit ``(method, template) -> Policy`` table covering
   EVERY route the app serves. It is the test-parameter source and the
   deny-by-default ground truth. A route absent from this table resolves to
   ``None`` and is DENIED for a bound tenant token (B2). The CI count invariant
   test asserts ``set(ROUTE_POLICY) == set(every real app+router route)`` so a
   new route cannot ship without a policy and silently default open.

3. ``enforce_policy(method, template, binding)`` — called by the middleware
   AFTER the binding resolves in multi-tenant mode. Returns a ``JSONResponse``
   (403) to DENY, or ``None`` to ALLOW. ``tenant_workspace`` / ``tenant_persona``
   policies allow at this layer (the per-handler id gate does the row-level
   scoping); ``admin`` / ``voice_query`` deny tenant tokens; ``public`` always
   allows; ``None`` (unregistered) denies.

Anti-pattern compliance:
    - Rule 2: ``resolve_route_template`` reads the LIVE ``request.app.routes``
      each call (physical state), never a cached snapshot — a route added at
      runtime resolves correctly and an absent one denies.
    - No tunable ``config.X`` is bound as a default arg (Rule 1 n/a — this module
      has no config knobs; the policy table is a frozen constant by design).
"""

from __future__ import annotations

from typing import Literal

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.routing import Match

from orchestration.tenant_auth import TenantBinding

# Policy vocabulary. Keep in lockstep with the WS2->WS3 contract.
Policy = Literal["public", "tenant_workspace", "tenant_persona", "admin", "voice_query"]


# ─────────────────────────────────────────────────────────────────────────────
# Route-template resolution from inside HTTP middleware (NB1 — proven mechanism).
# ─────────────────────────────────────────────────────────────────────────────
def resolve_route_template(request: Request) -> tuple[str, str] | None:
    """Resolve ``(method, route_template)`` for *request* from HTTP middleware.

    Works PRE-ROUTING (before ``request.scope["route"]`` is populated) by
    manually replaying Starlette route matching against the live app route
    table. Returns ``(method, template)`` on a full match, or ``None`` when no
    route matches (deny-by-default territory).
    """
    method = request.method
    scope = {
        "type": "http",
        "method": method,
        "path": request.url.path,
        "headers": request.headers.raw,
    }
    match = _match_route_template(request.app.routes, scope)
    if match is None:
        return None
    return (method, match)


def _iter_leaf_routes(routes):
    """Yield concrete leaf routes, descending through included-router mounts.

    ``app.include_router(...)`` does NOT flatten sub-routes into ``app.routes``
    in Starlette 1.3.x — it wraps them in an ``_IncludedRouter`` mount whose own
    ``matches()`` returns ``Match.FULL`` with an EMPTY child scope. The dashboard
    surface (75 routes) is mounted exactly this way, so the resolver MUST recurse
    into included routers to reach the real templates, or deny-by-default
    silently breaks on every dashboard route.
    """
    for route in routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None and hasattr(original_router, "routes"):
            yield from _iter_leaf_routes(original_router.routes)
            continue
        sub_routes = getattr(route, "routes", None)
        if sub_routes is not None and not hasattr(route, "endpoint"):
            yield from _iter_leaf_routes(sub_routes)
            continue
        yield route


def _match_route_template(routes, scope) -> str | None:
    """Return the matched leaf route's ``.path`` template, or ``None``.

    Only a ``Match.FULL`` (path AND method) counts — a path match with the wrong
    method yields ``Match.PARTIAL``, which we deliberately reject so two routes
    sharing a path under different methods resolve to the correct template.
    """
    for route in _iter_leaf_routes(routes):
        if getattr(route, "matches", None) is None:
            continue
        try:
            match_kind, child_scope = route.matches(scope)
        except Exception:  # noqa: BLE001 — a malformed route never breaks routing
            continue
        if match_kind is not Match.FULL:
            continue
        matched = child_scope.get("route")
        if matched is not None and getattr(matched, "path", None):
            return matched.path
        path = getattr(route, "path", None)
        if path:
            return path
    return None


def all_registered_routes(app) -> set[tuple[str, str]]:
    """Return ``{(method, template)}`` for every real leaf route on *app*.

    Used by the CI count invariant (``set(ROUTE_POLICY) == all_registered_routes``)
    so a new route shipping without a ROUTE_POLICY entry fails the suite instead
    of silently defaulting open. HEAD/OPTIONS are excluded (auto-added by
    Starlette, never independently policy-gated).
    """
    out: set[tuple[str, str]] = set()
    for route in _iter_leaf_routes(app.routes):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        for method in methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            out.add((method, path))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# The explicit policy table — keyed on the REAL FastAPI route templates.
#
# Built from the live route enumeration (NOT hand-copied from the PRP inventory,
# which used wrong dynamic-param names — R2 NB5). 113 declared routes + 4
# FastAPI auto-routes (/openapi.json, /docs, /docs/oauth2-redirect, /redoc)
# classified ``public`` = 117, balancing the CI count invariant.
#
# Policy meanings:
#   public          — no auth; tenant token allowed but the route returns no
#                     tenant data (health/info/templates/static/openapi).
#   tenant_workspace— gated on request.state.workspace_id; the handler's
#                     ws-scoped service call makes a cross-tenant id 404.
#   tenant_persona  — gated on request.state.persona_scope; the WS3 handler
#                     403s an out-of-scope persona_id (path/query/body).
#   admin           — admin/global token only; a tenant token 403s here.
#   voice_query     — query-param token routes; a tenant HEADER token 403s; the
#                     admin/global query-token path is unchanged.
# ─────────────────────────────────────────────────────────────────────────────
ROUTE_POLICY: dict[tuple[str, str], Policy] = {
    # ── FastAPI auto-routes (public; no tenant data) ────────────────────────
    ("GET", "/openapi.json"): "public",
    ("GET", "/docs"): "public",
    ("GET", "/docs/oauth2-redirect"): "public",
    ("GET", "/redoc"): "public",
    # ── Orchestration: convoy (tenant_workspace) ────────────────────────────
    ("POST", "/api/convoy"): "tenant_workspace",
    ("GET", "/api/convoy"): "tenant_workspace",
    ("GET", "/api/convoy/{convoy_id}"): "tenant_workspace",
    ("DELETE", "/api/convoy/{convoy_id}"): "tenant_workspace",
    ("POST", "/api/convoy/{convoy_id}/status"): "tenant_workspace",
    ("POST", "/api/convoy/{convoy_id}/subtasks"): "tenant_workspace",
    ("GET", "/api/convoy/{convoy_id}/ready"): "tenant_workspace",
    ("POST", "/api/convoy/{convoy_id}/subtask/{subtask_id}/dispatch"): "tenant_workspace",
    ("POST", "/api/convoy/{convoy_id}/subtask/{subtask_id}/complete"): "tenant_workspace",
    ("POST", "/api/convoy/{convoy_id}/subtask/{subtask_id}/fail"): "tenant_workspace",
    ("POST", "/api/convoy/{convoy_id}/subtask/{subtask_id}/progress"): "tenant_workspace",
    ("POST", "/api/convoy/{convoy_id}/subtask/{subtask_id}/transition"): "tenant_workspace",
    ("PATCH", "/api/convoy/{convoy_id}/subtask/{subtask_id}"): "tenant_workspace",
    # ── Orchestration: executors (admin — no tenant data / cross-ws by id) ──
    ("GET", "/api/executors"): "admin",
    ("POST", "/api/executor/callback"): "admin",
    # ── Orchestration: mailbox (tenant_workspace) ───────────────────────────
    ("POST", "/api/mailbox/send"): "tenant_workspace",
    ("GET", "/api/mailbox/inbox/{agent_id}"): "tenant_workspace",
    ("POST", "/api/mailbox/claim/{agent_id}"): "tenant_workspace",
    ("POST", "/api/mailbox/ack/{delivery_id}"): "tenant_workspace",
    ("GET", "/api/mailbox/convoy/{convoy_id}"): "tenant_workspace",
    # ── Orchestration: team (tenant_workspace) ──────────────────────────────
    ("POST", "/api/team"): "tenant_workspace",
    ("GET", "/api/team"): "tenant_workspace",
    ("POST", "/api/team/room/run"): "tenant_workspace",
    ("POST", "/api/team/operating-room/run"): "tenant_workspace",
    ("GET", "/api/capabilities/status"): "admin",
    ("GET", "/api/team/{team_id}"): "tenant_workspace",
    ("DELETE", "/api/team/{team_id}"): "tenant_workspace",
    ("POST", "/api/team/{team_id}/members"): "tenant_workspace",
    ("POST", "/api/team/{team_id}/shutdown"): "tenant_workspace",
    ("POST", "/api/team/{team_id}/ping"): "tenant_workspace",
    ("POST", "/api/team/{team_id}/loop-step"): "tenant_workspace",
    ("POST", "/api/team/{team_id}/tick"): "tenant_workspace",
    ("POST", "/api/team/{team_id}/executor-step"): "tenant_workspace",
    ("GET", "/api/team/{team_id}/memory"): "tenant_workspace",
    ("GET", "/api/team/{team_id}/memory/{filename}"): "tenant_workspace",
    ("POST", "/api/team/{team_id}/memory/{filename}"): "tenant_workspace",
    ("DELETE", "/api/team/{team_id}/memory/{filename}"): "tenant_workspace",
    # ── Dashboard: browser-viewer (admin — no tenant data) ──────────────────
    ("GET", "/api/browser-viewer/status"): "admin",
    ("GET", "/api/browser-viewer/screenshot"): "admin",
    ("POST", "/api/browser-viewer/stream/enable"): "admin",
    ("POST", "/api/browser-viewer/stream/disable"): "admin",
    # ── Dashboard: health/info (public) ─────────────────────────────────────
    ("GET", "/api/health"): "public",
    ("GET", "/api/jarvis/status"): "admin",
    ("GET", "/api/info"): "public",
    # ── Dashboard: agents / personas ────────────────────────────────────────
    ("GET", "/api/agents"): "tenant_persona",
    ("POST", "/api/agents"): "admin",
    ("DELETE", "/api/agents/{persona_id}"): "tenant_persona",
    ("DELETE", "/api/agents/{persona_id}/full"): "tenant_persona",
    ("GET", "/api/audit-log"): "admin",
    ("PUT", "/api/agents/{persona_id}/avatar"): "tenant_persona",
    ("DELETE", "/api/agents/{persona_id}/avatar"): "tenant_persona",
    ("POST", "/api/agents/{persona_id}/activate"): "tenant_persona",
    ("POST", "/api/agents/{persona_id}/deactivate"): "tenant_persona",
    ("POST", "/api/agents/{persona_id}/restart"): "tenant_persona",
    ("POST", "/api/agents/validate-id"): "tenant_persona",
    ("POST", "/api/agents/validate-token"): "admin",
    ("GET", "/api/agents/suggestions"): "admin",
    ("POST", "/api/agents/suggestions/refresh"): "admin",
    ("GET", "/api/agents/templates"): "public",
    ("GET", "/api/agents/model"): "admin",
    ("PATCH", "/api/agents/model"): "admin",
    ("GET", "/api/agents/{persona_id}"): "tenant_persona",
    ("PATCH", "/api/agents/{persona_id}/model"): "tenant_persona",
    ("GET", "/api/agents/{persona_id}/files"): "tenant_persona",
    ("PATCH", "/api/agents/{persona_id}/files/{filename}"): "tenant_persona",
    ("GET", "/api/agents/{persona_id}/files/history"): "tenant_persona",
    # B6-deferred: chat_sessions has no workspace_id column; tenant-scope unsafe → admin until B6
    ("GET", "/api/agents/{persona_id}/conversation"): "admin",
    # B6-deferred: chat_sessions has no workspace_id column; tenant-scope unsafe → admin until B6
    ("GET", "/api/agents/{persona_id}/tokens"): "admin",
    ("GET", "/api/agents/{persona_id}/tasks"): "tenant_persona",
    # ── Dashboard: work tasks (tenant_workspace) ────────────────────────────
    ("GET", "/api/work/tasks"): "tenant_workspace",
    ("POST", "/api/work/tasks"): "tenant_workspace",
    ("PATCH", "/api/work/tasks/{task_id}"): "tenant_workspace",
    ("POST", "/api/work/tasks/{task_id}/dispatch"): "tenant_workspace",
    # ── Dashboard: scheduled (admin — no workspace column, B6 v0) ───────────
    ("GET", "/api/scheduled"): "admin",
    ("POST", "/api/scheduled"): "admin",
    ("PATCH", "/api/scheduled/{task_id}"): "admin",
    ("DELETE", "/api/scheduled/{task_id}"): "admin",
    # ── Dashboard: memory / brain / hive-mind (tenant_persona) ──────────────
    ("GET", "/api/memory/graph"): "tenant_persona",
    ("GET", "/api/brain/graph"): "tenant_persona",
    ("GET", "/api/memories"): "tenant_persona",
    ("GET", "/api/tokens"): "admin",
    # B6-deferred: chat_sessions has no workspace_id column; tenant-scope unsafe → admin until B6
    ("GET", "/api/hive-mind/recent"): "admin",
    # ── Dashboard: settings (admin — global) ────────────────────────────────
    ("GET", "/api/dashboard/mobile-access"): "admin",
    ("GET", "/api/dashboard/settings"): "admin",
    ("PATCH", "/api/dashboard/settings"): "admin",
    # ── Dashboard: conversation (tenant_persona) ────────────────────────────
    ("GET", "/api/conversation/{persona_id}/history"): "tenant_persona",
    ("POST", "/api/conversation/{persona_id}/send"): "tenant_persona",
    ("GET", "/api/conversation/{persona_id}/stream"): "tenant_persona",
    # ── Pairing (Homie Mobile M2): claim/poll are pre-credential public
    # (self-authenticated by bootstrap/poll secrets); operator actions admin ──
    ("POST", "/api/pair/start"): "admin",
    ("POST", "/api/pair/claim"): "public",
    ("POST", "/api/pair/poll"): "public",
    ("GET", "/api/pair/pending"): "admin",
    ("POST", "/api/pair/approve/{pair_id}"): "admin",
    ("POST", "/api/pair/deny/{pair_id}"): "admin",
    # ── Voice round-trip (Homie Mobile M4) — mobile uses the admin credential;
    # no persona/workspace dimension on these STT/TTS helpers ──
    ("POST", "/api/voice/stt"): "admin",
    ("POST", "/api/voice/tts"): "admin",
    # ── Dashboard: cabinet (admin — no workspace column, B6 v0) ─────────────
    ("GET", "/api/cabinet/list"): "admin",
    ("POST", "/api/cabinet/new"): "admin",
    ("POST", "/api/cabinet/open"): "admin",
    ("POST", "/api/cabinet/warmup"): "admin",
    ("GET", "/api/cabinet/details"): "admin",
    ("GET", "/api/cabinet/participants/available"): "admin",
    ("POST", "/api/cabinet/participants/add"): "admin",
    ("POST", "/api/cabinet/participants/remove"): "admin",
    ("GET", "/api/cabinet/transcripts"): "admin",
    ("GET", "/api/cabinet/stream"): "admin",
    ("POST", "/api/cabinet/send"): "admin",
    ("POST", "/api/cabinet/abort"): "admin",
    ("POST", "/api/cabinet/pin"): "admin",
    ("POST", "/api/cabinet/unpin"): "admin",
    ("POST", "/api/cabinet/clear"): "admin",
    ("POST", "/api/cabinet/end"): "admin",
    # ── Dashboard: cabinet voice (NB2 — split, NOT a blanket exemption) ─────
    # Static JS is public; UI/session/control routes are voice_query (tenant
    # HEADER token 403s, admin query-token path unchanged); the per-persona
    # avatar route reads persona config so it is ADMIN, not exempt.
    ("GET", "/api/cabinet/voice/status"): "voice_query",
    ("POST", "/api/cabinet/voice/start"): "voice_query",
    ("POST", "/api/cabinet/voice/stop"): "voice_query",
    ("POST", "/api/cabinet/voice/restart"): "voice_query",
    ("GET", "/api/cabinet/voice/livekit/session"): "voice_query",
    ("GET", "/api/cabinet/voice/ui"): "voice_query",
    ("GET", "/api/cabinet/voice/client.bundle.js"): "public",
    ("GET", "/api/cabinet/voice/client.js"): "public",
    ("GET", "/api/cabinet/voice/avatars/{persona_id}.png"): "admin",
}


def resolve_policy(method: str, route_template: str | None) -> Policy | None:
    """Return the declared ``Policy`` for ``(method, template)``, or ``None``.

    ``None`` means the route is UNREGISTERED — deny-by-default territory. The
    middleware turns ``None`` into a 403 for a valid bound tenant token (B2).
    """
    if route_template is None:
        return None
    return ROUTE_POLICY.get((method, route_template))


def enforce_policy(
    method: str,
    route_template: str | None,
    binding: TenantBinding,
) -> JSONResponse | None:
    """Authorize *binding* against the route's policy. None = ALLOW, JSONResponse = DENY.

    Called by the middleware AFTER the binding resolves in multi-tenant mode.
    The id-level scoping (which workspace / which persona) is enforced by the
    per-handler gate; THIS layer enforces the route-class decision:

    - unregistered route (policy is None)  -> 403 (DENY-BY-DEFAULT, B2)
    - ``public``                            -> allow (no tenant data)
    - ``admin``     + tenant token          -> 403 "admin-only"
    - ``voice_query`` + tenant HEADER token -> 403 "admin-only"
    - ``admin``/``voice_query`` + admin     -> allow (admin binding)
    - ``tenant_workspace``/``tenant_persona`` -> allow (handler does the id gate)

    An admin binding (``is_admin=True``) is allowed everywhere a policy exists —
    the operator/global token retains full reach once MT mode engages (NM1).
    """
    policy = resolve_policy(method, route_template)
    if policy is None:
        return JSONResponse({"detail": "route has no tenant policy"}, status_code=403)
    if policy == "public":
        return None
    if binding.is_admin:
        # Admin/global token retains full reach across every classified route.
        return None
    if policy in ("admin", "voice_query"):
        return JSONResponse({"detail": "admin-only"}, status_code=403)
    # tenant_workspace / tenant_persona: authorized at this layer; the concrete
    # handler enforces the workspace_id / persona_scope row-level gate.
    return None
