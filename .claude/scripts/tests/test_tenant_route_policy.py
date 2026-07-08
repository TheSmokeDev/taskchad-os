"""Route-policy registry + middleware deny-by-default tests (Tenant Isolation v0 Phase B / WS2).

Three layers proven here:

1. THE RESOLVER (NB1 — migrated from the route-resolution spike): the
   ``resolve_route_template`` helper recovers the LITERAL FastAPI template from
   inside HTTP middleware, descending through ``include_router`` mounts, with the
   exact path-parameter names and the method dimension. This is the mechanism the
   real middleware uses; the spike that proved it (``test_nb1_route_resolution_spike.py``)
   is folded here and deleted.

2. THE CI COUNT INVARIANT (B2/B3/NB5): ``set(ROUTE_POLICY)`` MUST equal the set of
   every real leaf route on the live app (orchestration + the included dashboard
   router). A new route shipping without a policy fails the suite instead of
   silently defaulting open.

3. REAL DENY-BY-DEFAULT + the voice split (B2/NB2): a VALID BOUND tenant token on
   an UNREGISTERED route → 403; on an ``admin``/``voice_query`` route → 403; the
   per-persona avatar route does NOT bypass the policy layer via the voice prefix.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from orchestration.route_policy import (  # noqa: E402
    ROUTE_POLICY,
    all_registered_routes,
    enforce_policy,
    resolve_policy,
    resolve_route_template,
)
from orchestration.tenant_auth import TenantBinding, hash_token  # noqa: E402

_ADMIN_TOKEN = "global-admin-token"
_TOKEN_A = "tenant-a-raw-token"
_TOKEN_B = "tenant-b-raw-token"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — the resolver mechanism (folded NB1 spike).
# ─────────────────────────────────────────────────────────────────────────────
def _build_resolver_app():
    """A FastAPI app whose middleware captures the resolved template per request.

    Mirrors the real shape: directly-decorated dynamic routes, a method-overlap
    pair, a nested dynamic route (exact param names), an INCLUDED router (the
    dashboard mount gotcha), and a Depends probe for the post-routing fallback.
    """
    app = FastAPI()
    captured: dict[str, tuple[str, str] | None] = {}

    @app.middleware("http")
    async def capture_mw(request: Request, call_next):
        captured["_route_in_scope"] = request.scope.get("route")
        captured[request.url.path] = resolve_route_template(request)
        return await call_next(request)

    @app.get("/api/convoy/{convoy_id}")
    def get_convoy(convoy_id: int):
        return {"convoy_id": convoy_id}

    @app.post("/api/convoy/{convoy_id}")
    def post_convoy(convoy_id: int):
        return {"posted": convoy_id}

    @app.post("/api/convoy/{convoy_id}/subtask/{subtask_id}/complete")
    def complete_subtask(convoy_id: int, subtask_id: int):
        return {"convoy_id": convoy_id, "subtask_id": subtask_id}

    @app.get("/api/convoy")
    def list_convoys():
        return []

    def _post_routing_tpl(request: Request) -> str:
        route = request.scope.get("route")
        return route.path if route is not None else "<none>"

    @app.get("/api/depends-probe/{thing_id}")
    def depends_probe(thing_id: str, tpl: str = Depends(_post_routing_tpl)):
        return {"resolved_via_depends": tpl}

    dash = APIRouter()

    @dash.get("/api/agents/{persona_id}")
    def get_agent(persona_id: str):
        return {"persona_id": persona_id}

    @dash.delete("/api/agents/{persona_id}/files/{file_name}")
    def delete_agent_file(persona_id: str, file_name: str):
        return {"persona_id": persona_id, "file_name": file_name}

    app.include_router(dash)
    return TestClient(app), captured


def test_scope_route_unset_in_middleware_is_the_blocker():
    """Baseline (NB1): request.scope['route'] is None inside HTTP middleware."""
    client, captured = _build_resolver_app()
    assert client.get("/api/convoy/123").status_code == 200
    assert captured["_route_in_scope"] is None


def test_resolver_returns_template_not_raw_url():
    client, captured = _build_resolver_app()
    assert client.get("/api/convoy/123").status_code == 200
    assert captured["/api/convoy/123"] == ("GET", "/api/convoy/{convoy_id}")


def test_resolver_nested_dynamic_exact_param_names():
    client, captured = _build_resolver_app()
    assert client.post("/api/convoy/7/subtask/42/complete").status_code == 200
    assert captured["/api/convoy/7/subtask/42/complete"] == (
        "POST",
        "/api/convoy/{convoy_id}/subtask/{subtask_id}/complete",
    )


def test_resolver_method_dimension_disambiguates():
    client, captured = _build_resolver_app()
    assert client.get("/api/convoy/123").status_code == 200
    assert captured["/api/convoy/123"] == ("GET", "/api/convoy/{convoy_id}")
    assert client.post("/api/convoy/999").status_code == 200
    assert captured["/api/convoy/999"] == ("POST", "/api/convoy/{convoy_id}")


def test_resolver_descends_into_included_router():
    """The _IncludedRouter gotcha — the dashboard-mount regression guard."""
    client, captured = _build_resolver_app()
    assert client.get("/api/agents/persona-abc").status_code == 200
    assert captured["/api/agents/persona-abc"] == ("GET", "/api/agents/{persona_id}")
    assert client.delete("/api/agents/p/files/notes.md").status_code == 200
    assert captured["/api/agents/p/files/notes.md"] == (
        "DELETE",
        "/api/agents/{persona_id}/files/{file_name}",
    )


def test_resolver_static_route():
    client, captured = _build_resolver_app()
    assert client.get("/api/convoy").status_code == 200
    assert captured["/api/convoy"] == ("GET", "/api/convoy")


def test_resolver_unregistered_route_is_none():
    client, captured = _build_resolver_app()
    client.get("/api/this-route-does-not-exist")
    assert captured["/api/this-route-does-not-exist"] is None


def test_partial_method_match_is_rejected():
    """A path that exists under a DIFFERENT method resolves to None (Match.PARTIAL
    is not Match.FULL) — so the method dimension is real."""
    client, captured = _build_resolver_app()
    # /api/depends-probe/{thing_id} only exists for GET; POST is a wrong-method
    # path match → PARTIAL → resolver returns None.
    client.post("/api/depends-probe/xyz")
    assert captured["/api/depends-probe/xyz"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — the CI count invariant against the REAL app.
# ─────────────────────────────────────────────────────────────────────────────
def _reload_real_api(db_path: Path):
    with patch("config.ORCHESTRATION_DB_PATH", db_path):
        import orchestration.api as api_mod

        importlib.reload(api_mod)
        db, cs, ms, reg, ts = api_mod._get_services()
        api_mod._db = db
        api_mod._convoy_svc = cs
        api_mod._mailbox_svc = ms
        api_mod._executor_registry = reg
        api_mod._team_svc = ts
        return api_mod


def test_route_policy_covers_every_real_route(tmp_path, monkeypatch):
    """CI INVARIANT (B2/B3/NB5): ROUTE_POLICY keys == every real app+router route.

    Both directions: no real route is missing a policy (else it would silently
    default open in MT mode), and no ROUTE_POLICY key is stale (points at a route
    that no longer exists). The dashboard router MUST be mounted for this to be
    meaningful — assert a known dashboard route resolved.
    """
    monkeypatch.setenv("HOMIE_ALLOW_LIVE_AGENT_RUN", "1")
    api_mod = _reload_real_api(tmp_path / "inv.db")
    try:
        real = all_registered_routes(api_mod.app)
        # The dashboard router is included — prove the walker descended the mount.
        assert ("GET", "/api/agents/{persona_id}") in real, (
            "dashboard router not mounted/enumerated — the invariant would be "
            "vacuously satisfied on orchestration routes only"
        )
        declared = set(ROUTE_POLICY)
        missing = real - declared
        stale = declared - real
        assert not missing, f"routes with NO policy (default-open risk): {sorted(missing)}"
        assert not stale, f"stale ROUTE_POLICY keys (route removed): {sorted(stale)}"
    finally:
        api_mod._db.close()


def test_route_policy_count_is_134(tmp_path, monkeypatch):
    """130 declared routes + 4 FastAPI auto-routes = 134 (R2 count lock).

    History: 117 -> 123 on 2026-07-04 (+6 pairing routes, Homie Mobile M2);
    123 -> 125 on 2026-07-05 (+2 voice STT/TTS routes, Homie Mobile M4);
    125 -> 127 on 2026-07-05 (+2 conversation stop/steer routes, Homie
    Mobile M7 cockpit); 127 -> 130 on 2026-07-05 (+3 read-only sessions
    routes, M8); 130 -> 134 on 2026-07-05 (+4 read-only library routes —
    skills, files list/read, system-jobs — M9); 134 -> 137 on 2026-07-05
    (+3 phone-drive browser routes — elements, act, navigate — M12);
    137 -> 145 on 2026-07-06 (+8 social routes — status, channels, queue,
    posts, compose, connect-url, approve, reject — Postiz Social tab);
    145 -> 146 on 2026-07-06 (+1 on-demand social reconcile route);
    146 -> 147 on 2026-07-07 (+1 ghost-viewer screen route — the ghost
    DEVICE takeover surface, P4.1 Phase B B2); 147 -> 151 on 2026-07-07
    (+4 ghost-viewer input routes — tap/text/swipe/key, P4.1 Phase B B3);
    151 -> 153 on 2026-07-07 (+2 ghost-viewer app routes — launch/install,
    P4.1 Phase B B4).
    """
    monkeypatch.setenv("HOMIE_ALLOW_LIVE_AGENT_RUN", "1")
    api_mod = _reload_real_api(tmp_path / "count.db")
    try:
        assert len(all_registered_routes(api_mod.app)) == 153
        assert len(ROUTE_POLICY) == 153
    finally:
        api_mod._db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3a — enforce_policy unit truth (no app needed).
# ─────────────────────────────────────────────────────────────────────────────
_TENANT = TenantBinding(workspace_id=2, persona_scope=frozenset({"p"}), is_admin=False)
_ADMIN = TenantBinding(workspace_id=1, persona_scope=None, is_admin=True)


def test_enforce_unregistered_route_denies_tenant():
    deny = enforce_policy("GET", "/api/not-a-real-route", _TENANT)
    assert deny is not None and deny.status_code == 403


def test_enforce_unregistered_route_denies_even_admin():
    """Deny-by-default is absolute: an unregistered route 403s even an admin —
    a new route must be classified, not silently reachable."""
    deny = enforce_policy("GET", "/api/not-a-real-route", _ADMIN)
    assert deny is not None and deny.status_code == 403


def test_enforce_admin_route_denies_tenant_allows_admin():
    assert enforce_policy("GET", "/api/executors", _TENANT) is not None
    assert enforce_policy("GET", "/api/executors", _ADMIN) is None


def test_enforce_voice_query_denies_tenant_header_token():
    """NB2: a tenant header token on a voice_query route is admin-only → 403."""
    deny = enforce_policy("GET", "/api/cabinet/voice/status", _TENANT)
    assert deny is not None and deny.status_code == 403


def test_enforce_voice_avatar_is_admin_not_exempt():
    """NB2: the per-persona avatar route reads persona config → admin (a tenant
    token 403s); it is NOT a public/exempt static asset."""
    assert resolve_policy("GET", "/api/cabinet/voice/avatars/{persona_id}.png") == "admin"
    deny = enforce_policy("GET", "/api/cabinet/voice/avatars/{persona_id}.png", _TENANT)
    assert deny is not None and deny.status_code == 403


def test_enforce_public_allows_everyone():
    assert enforce_policy("GET", "/api/health", _TENANT) is None
    assert enforce_policy("GET", "/api/health", _ADMIN) is None


def test_enforce_tenant_workspace_allows_at_policy_layer():
    """tenant_workspace/tenant_persona pass the policy layer; the per-handler id
    gate does the row-level scoping."""
    assert enforce_policy("GET", "/api/convoy", _TENANT) is None
    assert enforce_policy("GET", "/api/agents/{persona_id}", _TENANT) is None


def test_voice_static_js_is_public():
    assert resolve_policy("GET", "/api/cabinet/voice/client.bundle.js") == "public"
    assert resolve_policy("GET", "/api/cabinet/voice/client.js") == "public"


# ─────────────────────────────────────────────────────────────────────────────
# NB3 — the WS2→WS3 persona_scope contract: a non-admin token NEVER carries None.
# ─────────────────────────────────────────────────────────────────────────────
def _bind_db(tmp_path):
    from orchestration.db import OrchestrationDB

    return OrchestrationDB(str(tmp_path / "nb3.db"))


def test_nb3_non_admin_null_scope_fails_closed_to_empty_frozenset(tmp_path):
    """A non-admin row with NULL persona_scope must resolve to frozenset() (deny-all),
    NOT None (which the dashboard reads as admin allow-all). WS2 must never hand
    WS3 an ambiguous None on a non-admin token."""
    from orchestration.tenant_auth import resolve_tenant_binding

    db = _bind_db(tmp_path)
    try:
        db.insert_tenant_token(hash_token("nonadmin-null"), 2, None, False, "t")
        binding = resolve_tenant_binding(db, "nonadmin-null")
        assert binding is not None
        assert binding.is_admin is False
        assert binding.persona_scope == frozenset(), (
            "non-admin NULL scope must fail closed to deny-all, not None/allow-all"
        )
    finally:
        db.close()


def test_nb3_admin_null_scope_stays_none_allow_all(tmp_path):
    """An ADMIN row with NULL scope keeps persona_scope=None — admin allow-all is
    the legitimate meaning of None (only admin/single-tenant carry it)."""
    from orchestration.tenant_auth import resolve_tenant_binding

    db = _bind_db(tmp_path)
    try:
        db.insert_tenant_token(hash_token("admin-null"), 1, None, True, "a")
        binding = resolve_tenant_binding(db, "admin-null")
        assert binding is not None and binding.is_admin is True
        assert binding.persona_scope is None
    finally:
        db.close()


def test_nb3_non_admin_scoped_token_keeps_its_personas(tmp_path):
    """A non-admin row WITH a scope resolves to exactly that frozenset."""
    from orchestration.tenant_auth import resolve_tenant_binding

    db = _bind_db(tmp_path)
    try:
        db.insert_tenant_token(hash_token("scoped"), 2, '["p1","p2"]', False, "s")
        binding = resolve_tenant_binding(db, "scoped")
        assert binding is not None
        assert binding.persona_scope == frozenset({"p1", "p2"})
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3b — REAL deny-by-default through the live middleware (a dummy route NOT
# in ROUTE_POLICY + a valid bound tenant token → 403).
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def mt_app_with_dummy_route(tmp_path, monkeypatch):
    """Multi-tenant app + a dummy unregistered route, for the deny-by-default proof."""
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", _ADMIN_TOKEN)
    monkeypatch.setenv("HOMIE_ALLOW_LIVE_AGENT_RUN", "1")
    monkeypatch.setenv("HOMIE_TENANT_ENFORCEMENT", "true")
    api_mod = _reload_real_api(tmp_path / "deny.db")
    db = api_mod._db
    db.insert_tenant_token(hash_token(_ADMIN_TOKEN), 1, None, True, "admin")
    db.insert_tenant_token(hash_token(_TOKEN_A), 2, '["persona-a"]', False, "tenant-a")

    # Register a dummy route AFTER MT mode is on. It is deliberately NOT in
    # ROUTE_POLICY, so a valid bound tenant token must be denied-by-default.
    @api_mod.app.get("/api/__dummy_unregistered__")
    def _dummy():
        return {"ok": True}

    try:
        yield api_mod
    finally:
        db.close()


def test_real_deny_by_default_bound_tenant_on_unregistered_route(mt_app_with_dummy_route):
    """B2 CRUX: a VALID BOUND tenant token on a route with NO policy → 403."""
    client = TestClient(mt_app_with_dummy_route.app)
    # Sanity: the tenant token DOES authenticate on a registered route.
    assert client.get("/api/convoy", headers=_auth(_TOKEN_A)).status_code == 200
    # But the unregistered route fails CLOSED for the same bound token.
    r = client.get("/api/__dummy_unregistered__", headers=_auth(_TOKEN_A))
    assert r.status_code == 403
    assert "no tenant policy" in r.json()["detail"]


def test_admin_reaches_unregistered_route_is_also_denied(mt_app_with_dummy_route):
    """Even the admin token is denied on an unregistered route — the policy table
    is the single source of truth, no implicit admin escape hatch."""
    client = TestClient(mt_app_with_dummy_route.app)
    r = client.get("/api/__dummy_unregistered__", headers=_auth(_ADMIN_TOKEN))
    assert r.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3c — NB2 voice split through the LIVE middleware (query-token branch).
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def mt_app_voice(tmp_path, monkeypatch):
    """Multi-tenant app with the admin/global token set, for the voice-split proof."""
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", _ADMIN_TOKEN)
    monkeypatch.setenv("HOMIE_ALLOW_LIVE_AGENT_RUN", "1")
    monkeypatch.setenv("HOMIE_TENANT_ENFORCEMENT", "true")
    api_mod = _reload_real_api(tmp_path / "voice.db")
    db = api_mod._db
    db.insert_tenant_token(hash_token(_ADMIN_TOKEN), 1, None, True, "admin")
    db.insert_tenant_token(hash_token(_TOKEN_B), 2, '["pb"]', False, "tenant-b")
    try:
        yield api_mod
    finally:
        db.close()


def test_voice_query_route_tenant_header_token_403(mt_app_voice):
    """NB2: a tenant HEADER token on a voice_query route is admin-only → 403."""
    client = TestClient(mt_app_voice.app)
    r = client.get("/api/cabinet/voice/status", headers=_auth(_TOKEN_B))
    assert r.status_code == 403


def test_voice_query_route_admin_query_token_passes_policy(mt_app_voice):
    """NB2: the admin/global QUERY token still reaches voice_query routes (the
    operator browser path is unchanged) — NOT a policy 401/403."""
    client = TestClient(mt_app_voice.app)
    r = client.get(f"/api/cabinet/voice/status?token={_ADMIN_TOKEN}")
    assert r.status_code != 401
    if r.status_code == 403:
        assert r.json().get("detail") != "admin-only"


def test_voice_avatar_route_does_not_bypass_via_prefix(mt_app_voice):
    """NB2 CRUX: the per-persona avatar route is ADMIN, not voice_query — a tenant
    HEADER token 403s admin-only (it does NOT slip through the voice prefix), and
    the admin QUERY-token shortcut does NOT apply (avatar needs an admin header)."""
    client = TestClient(mt_app_voice.app)
    r_tenant = client.get("/api/cabinet/voice/avatars/pb.png", headers=_auth(_TOKEN_B))
    assert r_tenant.status_code == 403
    assert r_tenant.json().get("detail") == "admin-only"
    # The voice_query query-token shortcut must NOT apply to the admin avatar route.
    r_query = client.get(f"/api/cabinet/voice/avatars/pb.png?token={_ADMIN_TOKEN}")
    assert r_query.status_code == 401


def test_health_public_no_token_in_mt_mode(mt_app_voice):
    """/api/health is public — reachable tokenless even in multi-tenant mode."""
    client = TestClient(mt_app_voice.app)
    assert client.get("/api/health").status_code == 200
