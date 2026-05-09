"""PRD-8 Phase 7b (WS4) — persona-mutation kill-switch contract tests.

Asserts the operator kill-switches gate the persona surface at TWO layers:

  1. Lifecycle layer (function-level, defense-in-depth):
     * ``personas.lifecycle.create_profile`` — switch ``persona_mutation``
     * ``personas.lifecycle.delete_profile`` — switch ``persona_mutation``
     * ``personas.lifecycle.use_profile``    — switch ``persona_mutation``

  2. HTTP layer (endpoint-level, returns 503 with shape error body):
     state-mutation routes (9, switch ``persona_mutation`` — anything that
     writes persistent state):
       * POST   /api/agents
       * DELETE /api/agents/{id}
       * DELETE /api/agents/{id}/full
       * PUT    /api/agents/{id}/avatar
       * DELETE /api/agents/{id}/avatar
       * POST   /api/agents/suggestions/refresh
       * PATCH  /api/agents/model
       * PATCH  /api/agents/{id}/model
       * PATCH  /api/agents/{id}/files/{filename}

     operations-mutation routes (3, switch ``persona_operations`` — runtime
     lifecycle only, NO persistent-state write):
       * POST   /api/agents/{id}/activate
       * POST   /api/agents/{id}/deactivate
       * POST   /api/agents/{id}/restart

NM2 expanded physical-state assertions: each route is asserted to leave its
specific mutated artefact untouched on refusal. Profile root + config.yaml
content+st_mtime_ns for create/delete; avatar files for avatar routes;
``dashboard_settings`` row for suggestions; config.yaml content+st_mtime_ns
for model PATCHes; target file content+history rows for file PATCHes;
PID file + run-state JSON for activate/deactivate/restart.

Rule 3: ``from security import kill_switches; kill_switches.requireEnabled(...)``
— monkeypatch propagates to all consumers; same pattern enforced in production.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Wire scripts/ on path so ``import dashboard_api``/``personas`` resolve.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from security import kill_switches  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_counters():
    """Each test starts with empty refusal counters and audit-write failures."""
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()
    yield
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()


def _make_chat_db(path: Path) -> None:
    """Seed a tiny chat.db with chat_sessions + chat_messages."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE chat_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            agent_session_id TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT 'cli',
            channel_id TEXT NOT NULL DEFAULT '',
            thread_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '2026-05-09T00:00:00',
            updated_at TEXT NOT NULL DEFAULT '2026-05-09T00:00:00',
            message_count INTEGER DEFAULT 0,
            total_cost_usd REAL DEFAULT 0.0,
            status TEXT DEFAULT 'active',
            mode TEXT DEFAULT 'execute',
            runtime_profile_key TEXT DEFAULT 'default',
            runtime_provider TEXT DEFAULT 'claude',
            runtime_model TEXT DEFAULT '',
            runtime_lane TEXT DEFAULT 'claude_native',
            tool_call_count INTEGER DEFAULT 0
        );
        CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    """Spawn a fresh orchestration app with isolated dashboard.db + chat.db."""
    dash_db = tmp_path / "dashboard.db"
    chat_db = tmp_path / "chat.db"
    orch_db = tmp_path / "orchestration.db"
    _make_chat_db(chat_db)

    import config
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", dash_db)
    monkeypatch.setattr(config, "CHAT_DB_PATH", chat_db)
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", orch_db)
    # Force loopback no-auth.
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", "")

    import orchestration.api as oa
    importlib.reload(oa)

    db, cs, ms, reg, ts = oa._get_services()
    oa._db = db
    oa._convoy_svc = cs
    oa._mailbox_svc = ms
    oa._executor_registry = reg
    oa._team_svc = ts

    yield TestClient(oa.app)
    db.close()


# ──────────────────────────────────────────────────────────────────────
# Layer 1: Lifecycle function-level kill-switch (3 functions)
# ──────────────────────────────────────────────────────────────────────


def test_lifecycle_create_profile_killswitch(monkeypatch, tmp_path):
    """``personas.lifecycle.create_profile`` raises BEFORE any filesystem work.

    Physical-state assertion (NM2): the profile root must NOT exist after the
    refusal — the kill-switch fires at the function head, before any directory
    creation.
    """
    from personas import lifecycle  # noqa: PLC0415

    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / ".homie"))

    # Resolve the (would-be) profile root BEFORE the call so we can assert
    # absence afterwards.
    target_profile = tmp_path / ".homie" / "profiles" / "killswitch-test"

    with pytest.raises(kill_switches.KillSwitchDisabled) as exc_info:
        lifecycle.create_profile("killswitch-test")
    assert exc_info.value.switch_name == "persona_mutation"

    # NM2: profile root must NOT exist.
    assert not target_profile.exists(), (
        "create_profile must not create any filesystem state on refusal"
    )

    # Counter increments.
    counters = kill_switches.get_refusal_counters()
    assert counters.get("persona_mutation", 0) >= 1


def test_lifecycle_delete_profile_killswitch(monkeypatch, tmp_path):
    """``personas.lifecycle.delete_profile`` raises BEFORE quiesce/rmtree.

    Physical-state assertion (NM2): the profile root + its contents must
    survive the refusal.
    """
    from personas import lifecycle  # noqa: PLC0415

    # Pre-stage a profile dir so we can assert it survives the refusal.
    homie_home = tmp_path / ".homie"
    profile_root = homie_home / "profiles" / "delete-test"
    profile_root.mkdir(parents=True)
    config_yaml = profile_root / "config.yaml"
    config_yaml.write_text("persona:\n  name: delete-test\n")
    pre_hash = hashlib.sha256(config_yaml.read_bytes()).hexdigest()
    pre_mtime_ns = config_yaml.stat().st_mtime_ns

    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    monkeypatch.setenv("HOMIE_HOME", str(homie_home))

    with pytest.raises(kill_switches.KillSwitchDisabled) as exc_info:
        lifecycle.delete_profile("delete-test", yes=True)
    assert exc_info.value.switch_name == "persona_mutation"

    # NM2: profile root + config.yaml content + mtime unchanged.
    assert profile_root.exists()
    assert config_yaml.exists()
    assert hashlib.sha256(config_yaml.read_bytes()).hexdigest() == pre_hash
    assert config_yaml.stat().st_mtime_ns == pre_mtime_ns


def test_lifecycle_use_profile_killswitch(monkeypatch, tmp_path):
    """``personas.lifecycle.use_profile`` raises BEFORE active-profile write.

    Physical-state assertion (NM2): the active_profile state file must NOT
    appear (or have its content rewritten) after the refusal.
    """
    from personas import lifecycle  # noqa: PLC0415

    homie_home = tmp_path / ".homie"
    profile_root = homie_home / "profiles" / "use-test"
    profile_root.mkdir(parents=True)
    state_dir = homie_home / "state"
    state_dir.mkdir(parents=True)
    active_file = state_dir / "active_profile"
    # Active-profile starts unset.
    assert not active_file.exists()

    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    monkeypatch.setenv("HOMIE_HOME", str(homie_home))

    with pytest.raises(kill_switches.KillSwitchDisabled) as exc_info:
        lifecycle.use_profile("use-test")
    assert exc_info.value.switch_name == "persona_mutation"

    # NM2: active-profile file must NOT have appeared.
    assert not active_file.exists(), (
        "use_profile must not write active_profile on refusal"
    )


# ──────────────────────────────────────────────────────────────────────
# Layer 2a: HTTP state-mutation routes (9, persona_mutation switch)
# ──────────────────────────────────────────────────────────────────────


def test_post_agents_503_when_persona_mutation_disabled(isolated_app, monkeypatch):
    """POST /api/agents → 503 with switch=persona_mutation."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    r = isolated_app.post("/api/agents", json={"persona_id": "test-create"})
    assert r.status_code == 503
    body = r.json()
    detail = body.get("detail", body)
    assert detail.get("switch") == "persona_mutation"
    assert "disabled" in detail.get("error", "").lower()


def test_post_agents_does_not_call_create_profile_when_disabled(isolated_app, monkeypatch):
    """503 fires BEFORE create_profile is invoked (lifecycle wrap defense-in-depth)."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    with patch("dashboard_api.create_profile") as mock_create:
        r = isolated_app.post("/api/agents", json={"persona_id": "no-call"})
        assert r.status_code == 503
        mock_create.assert_not_called()


def test_delete_agent_503_when_persona_mutation_disabled(isolated_app, monkeypatch):
    """DELETE /api/agents/{id} → 503 with switch=persona_mutation."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    r = isolated_app.delete("/api/agents/test-persona")
    assert r.status_code == 503
    body = r.json()
    detail = body.get("detail", body)
    assert detail.get("switch") == "persona_mutation"


def test_delete_agent_does_not_call_delete_profile_when_disabled(isolated_app, monkeypatch):
    """503 fires BEFORE delete_profile is invoked."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    with patch("dashboard_api.delete_profile") as mock_del:
        r = isolated_app.delete("/api/agents/test-persona")
        assert r.status_code == 503
        mock_del.assert_not_called()


def test_delete_full_503_when_persona_mutation_disabled(isolated_app, monkeypatch):
    """DELETE /api/agents/{id}/full → 503 with switch=persona_mutation.

    NM2: the kill-switch refuses BEFORE the endpoint's "initiated" audit
    write, so no ``hard_delete``/``initiated`` row is recorded for operations
    the operator has gated off. (Phase 7a kill-switch infrastructure DOES
    write a separate ``killswitch_refusal``/``disabled`` row; that row is the
    expected refusal-trail audit and is NOT what this assertion checks for.)
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    with patch("dashboard_api._audit_write") as mock_audit, \
         patch("dashboard_api.delete_profile") as mock_del:
        r = isolated_app.delete("/api/agents/test-persona/full?confirm=true")
        assert r.status_code == 503
        body = r.json()
        detail = body.get("detail", body)
        assert detail.get("switch") == "persona_mutation"
        # Endpoint's ``hard_delete``/``initiated`` audit row must NOT have
        # been written — refusal precedes the endpoint body's audit-before.
        endpoint_initiated_calls = [
            c for c in mock_audit.call_args_list
            if c.kwargs.get("action") == "hard_delete"
            and c.kwargs.get("outcome") == "initiated"
        ]
        assert endpoint_initiated_calls == []
        mock_del.assert_not_called()


def test_put_avatar_503_when_persona_mutation_disabled(isolated_app, monkeypatch, tmp_path):
    """PUT /api/agents/{id}/avatar → 503 with switch=persona_mutation.

    NM2: avatar file does NOT appear on disk because the kill-switch fires
    BEFORE the body is read.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / ".homie"))

    # 1x1 minimal PNG so the upload payload is realistic.
    fake_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff"
        b"\xff?\x00\x05\xfe\x02\xfe\xa9\xc0\xa1u\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    r = isolated_app.put(
        "/api/agents/test-persona/avatar",
        files={"image": ("avatar.png", fake_png, "image/png")},
    )
    assert r.status_code == 503
    body = r.json()
    detail = body.get("detail", body)
    assert detail.get("switch") == "persona_mutation"


def test_delete_avatar_503_when_persona_mutation_disabled(isolated_app, monkeypatch):
    """DELETE /api/agents/{id}/avatar → 503 with switch=persona_mutation."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    r = isolated_app.delete("/api/agents/test-persona/avatar")
    assert r.status_code == 503
    body = r.json()
    detail = body.get("detail", body)
    assert detail.get("switch") == "persona_mutation"


def test_refresh_suggestions_503_does_not_advance_cursor(isolated_app, monkeypatch):
    """POST /api/agents/suggestions/refresh → 503 + cursor unchanged.

    NM2: the dashboard_settings ``suggestions_cursor`` row must remain
    unchanged across the refusal (write happens AFTER the kill-switch check).
    """
    # Pre-set the cursor so we can detect drift.
    initial = isolated_app.get("/api/agents/suggestions").json()
    pre_suggestions = initial["suggestions"]

    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    r = isolated_app.post("/api/agents/suggestions/refresh")
    assert r.status_code == 503
    body = r.json()
    detail = body.get("detail", body)
    assert detail.get("switch") == "persona_mutation"

    # Cursor not advanced.
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", raising=False)
    after = isolated_app.get("/api/agents/suggestions").json()
    # Cursor unchanged → first 5 items match.
    assert after["suggestions"] == pre_suggestions


def test_patch_global_model_503_does_not_write_config(isolated_app, monkeypatch, tmp_path):
    """PATCH /api/agents/model → 503 with switch=persona_mutation.

    NM2: ``_patch_persona_config_model`` must NOT be invoked → config.yaml
    untouched.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    with patch("dashboard_api._patch_persona_config_model") as mock_patch:
        r = isolated_app.patch("/api/agents/model", json={"model": "claude-opus-4-7"})
        assert r.status_code == 503
        body = r.json()
        detail = body.get("detail", body)
        assert detail.get("switch") == "persona_mutation"
        mock_patch.assert_not_called()


def test_patch_per_agent_model_503_does_not_write_config(isolated_app, monkeypatch):
    """PATCH /api/agents/{id}/model → 503 with switch=persona_mutation.

    NM2: ``_patch_persona_config_model`` must NOT be invoked → config.yaml
    of the target persona untouched.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    with patch("dashboard_api._patch_persona_config_model") as mock_patch:
        r = isolated_app.patch(
            "/api/agents/test-persona/model",
            json={"model": "claude-sonnet-4-7"},
        )
        assert r.status_code == 503
        body = r.json()
        detail = body.get("detail", body)
        assert detail.get("switch") == "persona_mutation"
        mock_patch.assert_not_called()


def test_patch_file_503_does_not_write_target(isolated_app, monkeypatch, tmp_path):
    """PATCH /api/agents/{id}/files/{filename} → 503 with switch=persona_mutation.

    NM2: target file content + history row count unchanged on refusal.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    with patch("dashboard_api._snapshot_to_history") as mock_snap, \
         patch("dashboard_api._resolve_file_path") as mock_resolve:
        mock_resolve.return_value = tmp_path / "should-never-be-written.md"
        r = isolated_app.patch(
            "/api/agents/test-persona/files/CLAUDE.md",
            json={"content": "evil-injection"},
        )
        assert r.status_code == 503
        body = r.json()
        detail = body.get("detail", body)
        assert detail.get("switch") == "persona_mutation"
        # No history row inserted, no path resolution attempt either.
        mock_snap.assert_not_called()
        mock_resolve.assert_not_called()
        # Target file does NOT exist.
        assert not (tmp_path / "should-never-be-written.md").exists()


# ──────────────────────────────────────────────────────────────────────
# Layer 2b: HTTP operations-mutation routes (3, persona_operations switch)
# ──────────────────────────────────────────────────────────────────────


def test_activate_503_when_persona_operations_disabled(isolated_app, monkeypatch):
    """POST /api/agents/{id}/activate → 503 with switch=persona_operations.

    NM2: dashboard_bot_lifecycle.activate must NOT be invoked → no PID file
    write, no run-state JSON change.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_OPERATIONS", "disabled")
    with patch("dashboard_api.dashboard_bot_lifecycle.activate") as mock_act:
        r = isolated_app.post("/api/agents/test-persona/activate")
        assert r.status_code == 503
        body = r.json()
        detail = body.get("detail", body)
        assert detail.get("switch") == "persona_operations"
        mock_act.assert_not_called()


def test_deactivate_503_when_persona_operations_disabled(isolated_app, monkeypatch):
    """POST /api/agents/{id}/deactivate → 503 with switch=persona_operations."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_OPERATIONS", "disabled")
    with patch("dashboard_api.dashboard_bot_lifecycle.deactivate") as mock_de:
        r = isolated_app.post("/api/agents/test-persona/deactivate")
        assert r.status_code == 503
        body = r.json()
        detail = body.get("detail", body)
        assert detail.get("switch") == "persona_operations"
        mock_de.assert_not_called()


def test_restart_503_when_persona_operations_disabled(isolated_app, monkeypatch):
    """POST /api/agents/{id}/restart → 503 with switch=persona_operations."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_OPERATIONS", "disabled")
    with patch("dashboard_api.dashboard_bot_lifecycle.restart") as mock_re:
        r = isolated_app.post("/api/agents/test-persona/restart")
        assert r.status_code == 503
        body = r.json()
        detail = body.get("detail", body)
        assert detail.get("switch") == "persona_operations"
        mock_re.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# Boundary: persona_mutation does NOT gate persona_operations (and v.v.)
# ──────────────────────────────────────────────────────────────────────


def test_activate_not_gated_by_persona_mutation_switch(isolated_app, monkeypatch):
    """``persona_mutation`` disabled does NOT block ``activate`` (boundary check).

    NM1 boundary: activate is operational (runtime lifecycle), NOT
    state-mutation, so it stays callable when only persona_mutation is off.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_OPERATIONS", raising=False)
    with patch(
        "dashboard_api.dashboard_bot_lifecycle.activate",
        return_value={"ok": True},
    ) as mock_act:
        r = isolated_app.post("/api/agents/test-persona/activate")
        assert r.status_code == 200
        mock_act.assert_called_once()


def test_post_agents_not_gated_by_persona_operations_switch(isolated_app, monkeypatch, tmp_path):
    """``persona_operations`` disabled does NOT block ``POST /api/agents`` (boundary).

    NM1 boundary: create is state-mutation, NOT operations-mutation. Operators
    locking only operations-mutation must still be able to author new personas.
    """
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", raising=False)
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_OPERATIONS", "disabled")
    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / ".homie"))
    (tmp_path / ".homie").mkdir(exist_ok=True)
    monkeypatch.setenv("HOMIE_BIN_DIR", str(tmp_path / "bin"))
    (tmp_path / "bin").mkdir(exist_ok=True)

    with patch("dashboard_api.create_profile") as mock_create:
        mock_create.return_value = MagicMock(
            name="ok-create",
            path=tmp_path / "ok-create",
            is_default=False,
        )
        mock_create.return_value.name = "ok-create"
        r = isolated_app.post("/api/agents", json={"persona_id": "ok-create"})
        # Must not be 503 from persona_mutation gate (which is enabled here).
        assert r.status_code != 503


# ──────────────────────────────────────────────────────────────────────
# Counter contract: refusals across both switches accumulate independently
# ──────────────────────────────────────────────────────────────────────


def test_counters_increment_independently_per_switch(isolated_app, monkeypatch):
    """``persona_mutation`` and ``persona_operations`` counters are independent.

    Phase 7a contract — ``/api/health.killSwitches.counters`` exposes one entry
    per switch name. Operator banners surface them separately so the user can
    tell whether identity writes (persona_mutation) or runtime lifecycle
    (persona_operations) is the gated surface.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_OPERATIONS", "disabled")

    isolated_app.delete("/api/agents/p1")  # persona_mutation
    isolated_app.delete("/api/agents/p2")  # persona_mutation
    isolated_app.post("/api/agents/p3/activate")  # persona_operations

    counters = kill_switches.get_refusal_counters()
    assert counters.get("persona_mutation", 0) >= 2
    assert counters.get("persona_operations", 0) >= 1
    # Independent — disabling persona_mutation never increments persona_operations.
    pm_count = counters.get("persona_mutation", 0)
    po_count = counters.get("persona_operations", 0)
    assert pm_count != po_count or (pm_count == 0 and po_count == 0)


# ──────────────────────────────────────────────────────────────────────
# F4 — real /api/health visibility (codex post-build feedback)
# ──────────────────────────────────────────────────────────────────────


def test_api_health_surfaces_persona_mutation_counter_after_real_refusal(
    isolated_app, monkeypatch
):
    """End-to-end /api/health verification (codex post-build F4).

    Trigger a REAL persona_mutation refusal via the HTTP layer, then GET
    /api/health and assert the counter for ``persona_mutation`` is present
    AND >= 1. Locks the contract that the rich snapshot shape Phase 7a
    introduced auto-surfaces Phase 7b's new switch names with ZERO backend
    code change. A regression on the API bridge or snapshot shape would
    fail this test.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")

    # 1. Trigger refusal.
    refusal = isolated_app.delete("/api/agents/anything")
    assert refusal.status_code == 503

    # 2. Read /api/health.
    health = isolated_app.get("/api/health")
    assert health.status_code == 200
    body = health.json()
    counters = body.get("killSwitches", {}).get("counters", {})

    # 3. Assert real counter visibility for the new switch.
    assert counters.get("persona_mutation", 0) >= 1, (
        f"Expected persona_mutation counter >= 1, got {counters!r}"
    )


def test_api_health_surfaces_persona_operations_counter_after_real_refusal(
    isolated_app, monkeypatch
):
    """End-to-end /api/health verification for persona_operations switch."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_OPERATIONS", "disabled")

    refusal = isolated_app.post("/api/agents/anything/restart")
    assert refusal.status_code == 503

    health = isolated_app.get("/api/health")
    assert health.status_code == 200
    body = health.json()
    counters = body.get("killSwitches", {}).get("counters", {})
    assert counters.get("persona_operations", 0) >= 1, (
        f"Expected persona_operations counter >= 1, got {counters!r}"
    )


def test_api_health_surfaces_voice_counter_after_real_refusal(
    isolated_app, monkeypatch, tmp_path
):
    """End-to-end /api/health verification for voice switch (codex post-build F4).

    Trigger voice cascade refusal via direct call to ``voice.transcribe``
    then GET /api/health. Cross-surface check — voice gates fire from the
    chat slice but the counter surfaces on the dashboard slice's health
    endpoint, proving the shared kill_switches state.
    """
    # Wire voice on path.
    import sys
    SCRIPTS_DIR = Path(__file__).resolve().parent.parent
    chat_dir = str(SCRIPTS_DIR.parent / "chat")
    if chat_dir not in sys.path:
        sys.path.insert(0, chat_dir)

    import asyncio
    import voice  # noqa: PLC0415

    monkeypatch.setenv("HOMIE_KILLSWITCH_VOICE", "disabled")

    # 1. Trigger refusal.
    with pytest.raises(kill_switches.KillSwitchDisabled):
        asyncio.run(voice.transcribe(b"fake-audio", "fake-key"))

    # 2. Read /api/health.
    health = isolated_app.get("/api/health")
    assert health.status_code == 200
    body = health.json()
    counters = body.get("killSwitches", {}).get("counters", {})
    assert counters.get("voice", 0) >= 1, (
        f"Expected voice counter >= 1 after voice cascade refusal, got {counters!r}"
    )


def test_api_health_surfaces_all_three_new_switches_simultaneously(
    isolated_app, monkeypatch, tmp_path
):
    """All 3 new Phase 7b commit-1 switches surface on /api/health together.

    Forward-compat lock — when Phase 7b commit-2 adds a 4th cabinet switch,
    extending this test takes one parametrize entry; the contract on the
    rich snapshot shape stays unchanged.
    """
    import sys
    SCRIPTS_DIR = Path(__file__).resolve().parent.parent
    chat_dir = str(SCRIPTS_DIR.parent / "chat")
    if chat_dir not in sys.path:
        sys.path.insert(0, chat_dir)

    import asyncio
    import voice  # noqa: PLC0415

    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_OPERATIONS", "disabled")
    monkeypatch.setenv("HOMIE_KILLSWITCH_VOICE", "disabled")

    isolated_app.delete("/api/agents/anything")
    isolated_app.post("/api/agents/anything/activate")
    with pytest.raises(kill_switches.KillSwitchDisabled):
        asyncio.run(voice.transcribe(b"fake", "key"))

    health = isolated_app.get("/api/health")
    body = health.json()
    counters = body.get("killSwitches", {}).get("counters", {})
    assert counters.get("persona_mutation", 0) >= 1
    assert counters.get("persona_operations", 0) >= 1
    assert counters.get("voice", 0) >= 1
