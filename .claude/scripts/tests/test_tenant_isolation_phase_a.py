"""Phase A auth + convoy/team workspace-binding tests (Tenant Isolation v0).

Covers the two non-harness Phase A pieces:

1. AUTH TRUTH TABLE (orchestration middleware):
   - zero active non-admin tenant rows  -> legacy ORCHESTRATION_API_TOKEN
     behavior, byte-unchanged (back-compat).
   - >=1 active tenant row              -> bearer resolved by HASHED row lookup
     (NOT equality to the global token); two distinct tenant tokens coexist;
     the admin/global token still authenticates via its is_admin row.
   - raw token is never stored (only the sha256 hash).

2. CONVOY / TEAM / MAILBOX WORKSPACE BINDING (B4):
   - a tenant-B-bound token cannot READ or MUTATE a tenant-A convoy/team via the
     threaded routes (404 — not found in caller's workspace).
   - same-tenant access works (200).

The fixtures build the app with a tmp DB, seed admin + two tenant tokens via the
real OrchestrationDB methods, and drive the TestClient with the matching Bearer
headers — the same path the live operator + middleware take.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Tenant fixtures: A -> workspace 1, B -> workspace 2.
_ADMIN_TOKEN = "global-admin-token"
_TOKEN_A = "tenant-a-raw-token"
_TOKEN_B = "tenant-b-raw-token"
_WS_A = 1
_WS_B = 2


def _reload_api(db_path: Path):
    """Reload orchestration.api against *db_path* and return the module.

    Mirrors the existing test_orchestration_api.py client fixture: the DB path
    is patched, the module reloaded, and the service singletons re-pointed at
    the tmp DB.
    """
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


@pytest.fixture
def single_tenant_client(tmp_path, monkeypatch):
    """Zero tenant rows + ORCHESTRATION_API_TOKEN set — legacy back-compat mode.

    Yields (client, token). This is the DEFAULT deployment shape; the truth
    table must behave EXACTLY as the pre-Phase-A middleware did.
    """
    db_path = tmp_path / "st.db"
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", _ADMIN_TOKEN)
    monkeypatch.setenv("HOMIE_ALLOW_LIVE_AGENT_RUN", "1")
    api_mod = _reload_api(db_path)
    try:
        yield TestClient(api_mod.app), _ADMIN_TOKEN
    finally:
        api_mod._db.close()


@pytest.fixture
def multi_tenant_client(tmp_path, monkeypatch):
    """Admin + two tenant tokens seeded — multi-tenant mode engaged.

    Yields the api module so tests can drive the TestClient with per-tenant
    Bearer headers and inspect the DB. The admin row carries the existing
    ORCHESTRATION_API_TOKEN (admin bootstrap, R2 NM1).
    """
    from orchestration.tenant_auth import hash_token

    db_path = tmp_path / "mt.db"
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", _ADMIN_TOKEN)
    monkeypatch.setenv("HOMIE_ALLOW_LIVE_AGENT_RUN", "1")
    # Phase-A enforcement gate: MT enforcement engages ONLY on explicit opt-in.
    # The fixture opts in so these tests exercise the multi-tenant path; a default
    # deployment leaves this OFF (see test_enforcement_off_ignores_tenant_rows).
    monkeypatch.setenv("HOMIE_TENANT_ENFORCEMENT", "true")
    api_mod = _reload_api(db_path)
    db = api_mod._db
    # Admin bootstrap FIRST so the global token survives MT mode.
    db.insert_tenant_token(hash_token(_ADMIN_TOKEN), _WS_A, None, True, "admin")
    db.insert_tenant_token(hash_token(_TOKEN_A), _WS_A, '["persona-a"]', False, "tenant-a")
    db.insert_tenant_token(hash_token(_TOKEN_B), _WS_B, '["persona-b"]', False, "tenant-b")
    try:
        yield api_mod
    finally:
        db.close()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── Auth truth table ────────────────────────────────────────────────────────


def test_zero_rows_legacy_token_required_and_correct(single_tenant_client):
    """Zero tenant rows: legacy ORCHESTRATION_API_TOKEN gate, byte-unchanged.

    Missing header -> 401; wrong token -> 401; correct token -> 200. This is the
    exact contract the pre-Phase-A middleware enforced.
    """
    client, token = single_tenant_client
    assert client.get("/api/convoy").status_code == 401
    assert client.get("/api/convoy", headers=_auth("wrong")).status_code == 401
    assert client.get("/api/convoy", headers=_auth(token)).status_code == 200


def test_zero_rows_health_still_exempt(single_tenant_client):
    """The /api/health exemption is preserved in single-tenant mode."""
    client, _ = single_tenant_client
    assert client.get("/api/health").status_code == 200


def test_enforcement_off_ignores_tenant_rows(tmp_path, monkeypatch):
    """SECURITY GATE: tenant rows present but HOMIE_TENANT_ENFORCEMENT unset ->
    enforcement does NOT engage.

    This is the fix for the post-build BLOCKER: Phase A does not lock every route
    yet (callback / team mutators still default to workspace 1), so multi-tenant
    enforcement must NOT auto-activate just because a tenant row exists. With the
    flag OFF (the default), tenant rows are IGNORED, the legacy global-token gate
    runs byte-identically, and a tenant token cannot be used at all -> the
    half-locked leak surface is UNREACHABLE in a default deployment.
    """
    from orchestration.tenant_auth import hash_token

    db_path = tmp_path / "noenf.db"
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", _ADMIN_TOKEN)
    monkeypatch.setenv("HOMIE_ALLOW_LIVE_AGENT_RUN", "1")
    monkeypatch.delenv("HOMIE_TENANT_ENFORCEMENT", raising=False)  # default OFF
    api_mod = _reload_api(db_path)
    db = api_mod._db
    try:
        # Seed tenant rows — which, with enforcement OFF, MUST be inert.
        db.insert_tenant_token(hash_token(_ADMIN_TOKEN), _WS_A, None, True, "admin")
        db.insert_tenant_token(hash_token(_TOKEN_B), _WS_B, '["persona-b"]', False, "tenant-b")
        client = TestClient(api_mod.app)
        # Legacy global token still works (back-compat byte parity).
        assert client.get("/api/convoy", headers=_auth(_ADMIN_TOKEN)).status_code == 200
        # A tenant token is NOT resolved (enforcement off) -> just a non-global
        # bearer -> 401, exactly as legacy. Tenant B cannot reach anything.
        assert client.get("/api/convoy", headers=_auth(_TOKEN_B)).status_code == 401
        # And the leak route stays inaccessible to the tenant token too.
        assert client.post(
            "/api/executor/callback",
            headers=_auth(_TOKEN_B),
            json={"event_type": "subtask.completed", "convoy_id": 1, "subtask_id": 1,
                  "idempotency_key": "x", "payload": {}},
        ).status_code == 401
    finally:
        db.close()


def test_multi_tenant_two_tokens_resolve_distinct_workspaces(multi_tenant_client):
    """>=1 tenant row: both tenant tokens authenticate; the global token is NOT
    accepted by equality — only because it matches an is_admin row."""
    api_mod = multi_tenant_client
    client = TestClient(api_mod.app)

    # Both tenant tokens authenticate (200 on a workspace-scoped list).
    assert client.get("/api/convoy", headers=_auth(_TOKEN_A)).status_code == 200
    assert client.get("/api/convoy", headers=_auth(_TOKEN_B)).status_code == 200
    # The admin/global token authenticates via its is_admin row.
    assert client.get("/api/convoy", headers=_auth(_ADMIN_TOKEN)).status_code == 200
    # An unknown token is rejected.
    assert client.get("/api/convoy", headers=_auth("unknown")).status_code == 401
    # Missing header is rejected.
    assert client.get("/api/convoy").status_code == 401


def test_multi_tenant_resolution_is_by_hash_not_equality(multi_tenant_client):
    """A tenant token that is NOT the global token still resolves (hashed row
    lookup), proving the gate is not equality to ORCHESTRATION_API_TOKEN."""
    api_mod = multi_tenant_client
    client = TestClient(api_mod.app)
    # _TOKEN_A != _ADMIN_TOKEN, yet it authenticates — only a hashed-row lookup
    # makes this possible (an equality gate would 401 it).
    assert _TOKEN_A != _ADMIN_TOKEN
    assert client.get("/api/convoy", headers=_auth(_TOKEN_A)).status_code == 200


def test_raw_token_never_stored(multi_tenant_client):
    """Only the sha256 hash is persisted — the raw token never touches the DB."""
    api_mod = multi_tenant_client
    rows = api_mod._db.list_tenant_tokens()
    blob = "".join(str(dict(r)) for r in rows)
    for raw in (_ADMIN_TOKEN, _TOKEN_A, _TOKEN_B):
        assert raw not in blob, f"raw token {raw!r} leaked into the token store"
    # And the stored hash matches what the resolver computes.
    from orchestration.tenant_auth import hash_token

    stored_hashes = {r["token_sha256"] for r in rows}
    assert hash_token(_TOKEN_A) in stored_hashes
    assert hash_token(_TOKEN_B) in stored_hashes


def test_revoked_tenant_token_stops_resolving(multi_tenant_client):
    """Revocation is physical state: a revoked token 401s on the next request,
    with no cache to invalidate (Rule 2)."""
    api_mod = multi_tenant_client
    client = TestClient(api_mod.app)
    assert client.get("/api/convoy", headers=_auth(_TOKEN_B)).status_code == 200
    # Revoke tenant B's row.
    rows = api_mod._db.list_tenant_tokens()
    b_id = next(r["id"] for r in rows if r["label"] == "tenant-b")
    assert api_mod._db.revoke_token(b_id) is True
    # Tenant B now 401s. Tenant A (still active) keeps working.
    assert client.get("/api/convoy", headers=_auth(_TOKEN_B)).status_code == 401
    assert client.get("/api/convoy", headers=_auth(_TOKEN_A)).status_code == 200


def test_admin_token_survives_after_first_tenant(multi_tenant_client):
    """Admin bootstrap (R2 NM1): the existing global token still authenticates
    once MT mode is on, because it matches an is_admin=1 row."""
    api_mod = multi_tenant_client
    client = TestClient(api_mod.app)
    assert client.get("/api/convoy", headers=_auth(_ADMIN_TOKEN)).status_code == 200


# ── Convoy workspace binding (B4) ───────────────────────────────────────────


def _create_convoy(client, token: str, title: str) -> int:
    r = client.post(
        "/api/convoy",
        json={"title": title, "created_by": "sb"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    return r.json()["convoy"]["id"]


def test_convoy_created_under_caller_workspace_and_invisible_cross_tenant(
    multi_tenant_client,
):
    """A convoy created by tenant A is invisible to tenant B (list + get)."""
    api_mod = multi_tenant_client
    client = TestClient(api_mod.app)

    cid_a = _create_convoy(client, _TOKEN_A, "A's convoy")

    # Tenant A sees it; tenant B does not.
    list_a = client.get("/api/convoy", headers=_auth(_TOKEN_A)).json()
    list_b = client.get("/api/convoy", headers=_auth(_TOKEN_B)).json()
    assert any(c["id"] == cid_a for c in list_a)
    assert all(c["id"] != cid_a for c in list_b)

    # Tenant B GET on A's convoy id -> 404 (not found in B's workspace).
    assert client.get(f"/api/convoy/{cid_a}", headers=_auth(_TOKEN_B)).status_code == 404
    # Tenant A GET on its own convoy -> 200.
    assert client.get(f"/api/convoy/{cid_a}", headers=_auth(_TOKEN_A)).status_code == 200


def test_convoy_cross_tenant_mutations_are_404_and_no_op(multi_tenant_client):
    """Tenant B cannot mutate tenant A's convoy: delete / status / add-subtasks /
    ready all 404, and A's convoy survives unchanged."""
    api_mod = multi_tenant_client
    client = TestClient(api_mod.app)

    cid_a = _create_convoy(client, _TOKEN_A, "A's convoy")

    # DELETE cross-tenant -> 404, row survives.
    assert client.delete(f"/api/convoy/{cid_a}", headers=_auth(_TOKEN_B)).status_code == 404
    assert client.get(f"/api/convoy/{cid_a}", headers=_auth(_TOKEN_A)).status_code == 200

    # status transition cross-tenant -> 404 (B4: ws passed into the mutator,
    # so the row is "not found" in B's workspace, not silently mutated in ws 1).
    r = client.post(
        f"/api/convoy/{cid_a}/status",
        json={"status": "active"},
        headers=_auth(_TOKEN_B),
    )
    assert r.status_code == 404

    # add-subtasks cross-tenant -> 404, no rows added.
    r = client.post(
        f"/api/convoy/{cid_a}/subtasks",
        json={"subtasks": [{"title": "X"}]},
        headers=_auth(_TOKEN_B),
    )
    assert r.status_code == 404

    # ready (the one no-ws service method, parent-gated) cross-tenant -> 404.
    assert (
        client.get(f"/api/convoy/{cid_a}/ready", headers=_auth(_TOKEN_B)).status_code
        == 404
    )

    # Same-tenant ready works.
    assert (
        client.get(f"/api/convoy/{cid_a}/ready", headers=_auth(_TOKEN_A)).status_code
        == 200
    )


def test_convoy_subtask_routes_cross_tenant_404(multi_tenant_client):
    """Subtask-by-id routes (complete/fail/progress/transition/patch) reject a
    cross-tenant subtask via the ws-scoped parent gate."""
    api_mod = multi_tenant_client
    client = TestClient(api_mod.app)

    # A creates a convoy with one subtask.
    r = client.post(
        "/api/convoy",
        json={"title": "A", "created_by": "sb", "subtasks": [{"title": "T"}]},
        headers=_auth(_TOKEN_A),
    )
    assert r.status_code == 200
    body = r.json()
    cid_a = body["convoy"]["id"]
    sid_a = body["subtasks"][0]["id"]

    # Tenant B cannot complete / fail / transition / patch A's subtask -> 404.
    assert (
        client.post(
            f"/api/convoy/{cid_a}/subtask/{sid_a}/complete", headers=_auth(_TOKEN_B)
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/convoy/{cid_a}/subtask/{sid_a}/fail",
            json={},
            headers=_auth(_TOKEN_B),
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/convoy/{cid_a}/subtask/{sid_a}/transition",
            json={"status": "running"},
            headers=_auth(_TOKEN_B),
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"/api/convoy/{cid_a}/subtask/{sid_a}",
            json={"error_message": "x"},
            headers=_auth(_TOKEN_B),
        ).status_code
        == 404
    )


def test_mailbox_inbox_is_workspace_scoped(multi_tenant_client):
    """A message sent in tenant A's workspace is not visible in tenant B's
    inbox/convoy-message reads."""
    api_mod = multi_tenant_client
    client = TestClient(api_mod.app)

    send = client.post(
        "/api/mailbox/send",
        json={"from_agent": "a1", "recipients": ["a2"], "body": "secret-A"},
        headers=_auth(_TOKEN_A),
    )
    assert send.status_code == 200

    # Tenant A's recipient sees the message; tenant B's inbox for the same agent
    # id is empty (different workspace). The inbox returns MessageWithDeliveries,
    # so the body lives at m["message"]["body"].
    inbox_a = client.get("/api/mailbox/inbox/a2", headers=_auth(_TOKEN_A)).json()
    inbox_b = client.get("/api/mailbox/inbox/a2", headers=_auth(_TOKEN_B)).json()
    assert any("secret-A" in m["message"]["body"] for m in inbox_a)
    assert inbox_b == []


# ── Team workspace binding (B4) ─────────────────────────────────────────────


def _create_team(client, token: str, name: str) -> int:
    r = client.post(
        "/api/team",
        json={"team_name": name, "lead_agent_id": "lead"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    return r.json()["session"]["id"]


def test_team_created_under_caller_workspace_and_cross_tenant_404(
    multi_tenant_client,
):
    """A team created by tenant A is invisible to tenant B; B's GET on A's
    team_id is 404 (the ws-scoped get_team_session gate)."""
    api_mod = multi_tenant_client
    client = TestClient(api_mod.app)

    tid_a = _create_team(client, _TOKEN_A, "A-team")

    list_b = client.get("/api/team", headers=_auth(_TOKEN_B)).json()
    assert all(t["id"] != tid_a for t in list_b)

    assert client.get(f"/api/team/{tid_a}", headers=_auth(_TOKEN_B)).status_code == 404
    assert client.get(f"/api/team/{tid_a}", headers=_auth(_TOKEN_A)).status_code == 200


def test_team_memory_cross_tenant_404(multi_tenant_client):
    """The team-memory CRUD family is gated by the ws-scoped _require_team: a
    cross-tenant team_id 404s before any memory file is touched."""
    api_mod = multi_tenant_client
    client = TestClient(api_mod.app)

    tid_a = _create_team(client, _TOKEN_A, "A-team")

    # B cannot list / read / write / delete A's team memory.
    assert (
        client.get(f"/api/team/{tid_a}/memory", headers=_auth(_TOKEN_B)).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/team/{tid_a}/memory/notes.md",
            json={"content": "x"},
            headers=_auth(_TOKEN_B),
        ).status_code
        == 404
    )
    # A can list its own team memory (200, empty list).
    assert (
        client.get(f"/api/team/{tid_a}/memory", headers=_auth(_TOKEN_A)).status_code
        == 200
    )


# ── Team MUTATOR cross-tenant 404 + DB-state proof (codex BLOCKER fix) ───────
#
# The route_policy ADMITS a tenant token on /api/team/{team_id}/* (tenant_workspace);
# isolation is enforced by the handler threading workspace_id into the service so a
# cross-tenant team_id raises ValueError("...not found") → 404. Before the fix the
# handlers dropped workspace_id → the service resolved A's team at the DEFAULT ws 1
# → tenant B mutated A's team. These tests assert BOTH the 404 AND that A's physical
# row/member/status is UNCHANGED — so they FAIL (the mutation lands) without the fix.


def _team_row(api_mod, team_id):
    """Read the raw team_sessions row (physical state, Rule 2) for assertions."""
    return api_mod._db.conn.execute(
        "SELECT id, workspace_id, status FROM team_sessions WHERE id = ?", (team_id,)
    ).fetchone()


def _member_count(api_mod, team_id):
    return api_mod._db.conn.execute(
        "SELECT COUNT(*) AS n FROM team_members WHERE team_session_id = ?", (team_id,)
    ).fetchone()["n"]


def test_team_close_cross_tenant_404_and_status_unchanged(multi_tenant_client):
    """B → DELETE A's team = 404; A's team status stays 'active' in the DB."""
    api_mod = multi_tenant_client
    client = TestClient(api_mod.app)
    tid_a = _create_team(client, _TOKEN_A, "A-team")
    before = _team_row(api_mod, tid_a)
    assert before["status"] == "active" and before["workspace_id"] == _WS_A

    r = client.delete(f"/api/team/{tid_a}", headers=_auth(_TOKEN_B))
    assert r.status_code == 404
    after = _team_row(api_mod, tid_a)
    assert after["status"] == "active", "B closed A's team — workspace_id not threaded"
    assert after["workspace_id"] == _WS_A


def test_team_add_member_cross_tenant_404_and_no_member_added(multi_tenant_client):
    """B → add member to A's team = 404; A's team member count unchanged."""
    api_mod = multi_tenant_client
    client = TestClient(api_mod.app)
    tid_a = _create_team(client, _TOKEN_A, "A-team")
    before = _member_count(api_mod, tid_a)

    r = client.post(
        f"/api/team/{tid_a}/members",
        json={"agent_id": "intruder", "role": "worker"},
        headers=_auth(_TOKEN_B),
    )
    assert r.status_code == 404
    assert _member_count(api_mod, tid_a) == before, "B added a member to A's team"
    # And the intruder is not present under any guise.
    rows = api_mod._db.conn.execute(
        "SELECT agent_id FROM team_members WHERE team_session_id = ?", (tid_a,)
    ).fetchall()
    assert all(row["agent_id"] != "intruder" for row in rows)


def test_team_shutdown_cross_tenant_404_and_status_unchanged(multi_tenant_client):
    """B → request shutdown of A's team = 404; A's status stays 'active'."""
    api_mod = multi_tenant_client
    client = TestClient(api_mod.app)
    tid_a = _create_team(client, _TOKEN_A, "A-team")

    r = client.post(f"/api/team/{tid_a}/shutdown", headers=_auth(_TOKEN_B))
    assert r.status_code == 404
    assert _team_row(api_mod, tid_a)["status"] == "active", (
        "B moved A's team to shutdown_requested — workspace_id not threaded"
    )


def test_team_ping_cross_tenant_404(multi_tenant_client):
    """B → ping A's team = 404 (the ws-scoped get_team_session inside ping_activity)."""
    api_mod = multi_tenant_client
    client = TestClient(api_mod.app)
    tid_a = _create_team(client, _TOKEN_A, "A-team")

    r = client.post(f"/api/team/{tid_a}/ping", json={}, headers=_auth(_TOKEN_B))
    assert r.status_code == 404


def test_team_loop_tick_executor_cross_tenant_404(multi_tenant_client):
    """B → loop-step / tick / executor-step on A's team = 404 (ws-scoped team lookup).

    These run-* services call get_team_session(team_id, workspace_id=ws) as their
    FIRST step, so a cross-tenant team_id raises 'Team N not found' → 404 BEFORE any
    runtime work — no live call ever happens. We pass allow_live_agent_run=True so the
    handler's live-agent gate (which runs BEFORE the service) does NOT 403 first; that
    isolates the assertion to the WORKSPACE gate (404), not a live refusal (403)."""
    api_mod = multi_tenant_client
    client = TestClient(api_mod.app)
    tid_a = _create_team(client, _TOKEN_A, "A-team")

    # loop-step — allow_live_agent_run True so the 404 is the WS gate, not a 403.
    r1 = client.post(
        f"/api/team/{tid_a}/loop-step",
        json={"agent_id": "x", "allow_live_agent_run": True},
        headers=_auth(_TOKEN_B),
    )
    assert r1.status_code == 404, r1.text
    # tick
    r2 = client.post(
        f"/api/team/{tid_a}/tick",
        json={"allow_live_agent_run": True},
        headers=_auth(_TOKEN_B),
    )
    assert r2.status_code == 404, r2.text
    # executor-step
    r3 = client.post(
        f"/api/team/{tid_a}/executor-step",
        json={"agent_id": "x", "allow_live_agent_run": True},
        headers=_auth(_TOKEN_B),
    )
    assert r3.status_code == 404, r3.text

    # A's team is untouched (still active).
    assert _team_row(api_mod, tid_a)["status"] == "active"


def test_team_room_operating_room_handlers_thread_workspace_id(multi_tenant_client):
    """The boardroom + operating-room handlers thread workspace_id into the service.

    Deterministic (no live LLM call): we spy on the service's run_team_room /
    run_operating_room to capture the workspace_id the HANDLER passes, and assert it
    equals tenant B's workspace — proving B's boardroom team/convoy land in ws 2, not
    the default ws 1. (A full live run is covered by the manual two-bot smoke; here we
    pin the threading, which is the isolation-relevant part.)"""
    import orchestration.api as api_mod_ref

    captured: dict[str, int] = {}

    class _SpyRoom:
        def __init__(self, _db):
            pass

        def run_team_room(self, **kwargs):
            captured["room_ws"] = kwargs.get("workspace_id")
            raise ValueError("spy-short-circuit")  # 400, no live work

    class _SpyOps:
        def __init__(self, _db):
            pass

        def run_operating_room(self, **kwargs):
            captured["ops_ws"] = kwargs.get("workspace_id")
            raise ValueError("spy-short-circuit")

    client = TestClient(api_mod_ref.app)
    with patch.object(api_mod_ref, "TeamRoomWorkflowService", _SpyRoom), patch.object(
        api_mod_ref, "OperatingRoomService", _SpyOps
    ):
        client.post(
            "/api/team/room/run",
            json={"goal": "g", "allow_live_agent_run": True},
            headers=_auth(_TOKEN_B),
        )
        client.post(
            "/api/team/operating-room/run",
            json={"goal": "g", "allow_live_agent_run": True},
            headers=_auth(_TOKEN_B),
        )

    assert captured.get("room_ws") == _WS_B, (
        f"boardroom handler passed workspace_id={captured.get('room_ws')}, "
        f"expected B's {_WS_B} — a missing thread would default to ws 1"
    )
    assert captured.get("ops_ws") == _WS_B, (
        f"operating-room handler passed workspace_id={captured.get('ops_ws')}, "
        f"expected B's {_WS_B}"
    )
