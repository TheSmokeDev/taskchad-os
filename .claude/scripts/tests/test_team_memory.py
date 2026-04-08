"""Team memory tests — Phase 7.

Covers path scoping, secret guardrails, traversal protection, private vs
team distinction, and API endpoints.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


@pytest.fixture
def vault_root(tmp_path, monkeypatch):
    """Redirect team/agent memory to a temp vault for isolation."""
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("VAULT_ROOT", str(root))
    return root


# ── Path resolution ───────────────────────────────────────────────────────


def test_get_team_memory_path_uses_team_id(vault_root):
    from orchestration import team_memory

    # Fix 2: keyed by numeric team_id so distinct sessions with same name stay isolated
    path = team_memory.get_team_memory_path(42)
    assert path == vault_root / "teams" / "team-42"


def test_get_agent_memory_path_sanitizes_id(vault_root):
    from orchestration import team_memory

    path = team_memory.get_agent_memory_path("agent@123")
    assert path == vault_root / "agents" / "agent_123"


# ── Team memory write/read/list ───────────────────────────────────────────


def test_write_team_memory_creates_file(vault_root):
    from orchestration import team_memory

    path = team_memory.write_team_memory(1, "notes.md", "# Hello\n")
    assert path.exists()
    assert path.read_text() == "# Hello\n"
    assert path.parent == vault_root / "teams" / "team-1"


def test_read_team_memory_returns_content(vault_root):
    from orchestration import team_memory

    team_memory.write_team_memory(1, "notes.md", "body text")
    assert team_memory.read_team_memory(1, "notes.md") == "body text"


def test_list_team_memory_returns_filenames(vault_root):
    from orchestration import team_memory

    team_memory.write_team_memory(1, "a.md", "one")
    team_memory.write_team_memory(1, "b.md", "two")
    assert team_memory.list_team_memory(1) == ["a.md", "b.md"]


def test_list_team_memory_missing_team_returns_empty(vault_root):
    from orchestration import team_memory

    assert team_memory.list_team_memory(999) == []


def test_write_team_memory_refuses_duplicate_without_overwrite(vault_root):
    from orchestration import team_memory

    team_memory.write_team_memory(1, "notes.md", "v1")
    with pytest.raises(FileExistsError):
        team_memory.write_team_memory(1, "notes.md", "v2")


def test_write_team_memory_overwrite_allowed_with_flag(vault_root):
    from orchestration import team_memory

    team_memory.write_team_memory(1, "notes.md", "v1")
    team_memory.write_team_memory(1, "notes.md", "v2", overwrite=True)
    assert team_memory.read_team_memory(1, "notes.md") == "v2"


def test_delete_team_memory_removes_file(vault_root):
    from orchestration import team_memory

    team_memory.write_team_memory(1, "notes.md", "body")
    assert team_memory.delete_team_memory(1, "notes.md") is True
    assert team_memory.delete_team_memory(1, "notes.md") is False


# ── Secret guardrail ──────────────────────────────────────────────────────


def test_secret_generic_api_key_refused(vault_root):
    from orchestration import team_memory

    with pytest.raises(ValueError, match="secrets"):
        team_memory.write_team_memory(
            1, "leak.md", "api_key = abcdef1234567890xyz",
        )


def test_secret_openai_key_pattern_refused(vault_root):
    from orchestration import team_memory

    with pytest.raises(ValueError, match="secrets"):
        team_memory.write_team_memory(
            1, "leak.md", "Token: sk-abc123456789012345678901234567890",
        )


def test_secret_langfuse_key_refused(vault_root):
    from orchestration import team_memory

    with pytest.raises(ValueError, match="secrets"):
        team_memory.write_team_memory(
            1, "leak.md", "sk-lf-your-secret-key",
        )


def test_secret_paperclip_key_refused(vault_root):
    from orchestration import team_memory

    with pytest.raises(ValueError, match="secrets"):
        team_memory.write_team_memory(
            1, "leak.md", "pcp_07b56518a772817554b051ebcfaaf2ee05",
        )


def test_clean_content_allowed(vault_root):
    from orchestration import team_memory

    path = team_memory.write_team_memory(
        1, "notes.md",
        "# Meeting Notes\n\nDiscussed the rollout plan.\nNo blockers.\n",
    )
    assert path.exists()


def test_scan_for_secrets_returns_patterns(vault_root):
    from orchestration import team_memory

    result = team_memory.scan_for_secrets("bearer: supersecrettokenvalue123")
    assert result  # non-empty


def test_scan_for_secrets_clean_returns_empty(vault_root):
    from orchestration import team_memory

    assert team_memory.scan_for_secrets("Just a plain note about the roadmap.") == []


# ── Path traversal ────────────────────────────────────────────────────────


def test_traversal_slash_filename_refused(vault_root):
    from orchestration import team_memory

    with pytest.raises(ValueError, match="Invalid"):
        team_memory.write_team_memory(1, "../../../etc/passwd", "x")


def test_traversal_backslash_filename_refused(vault_root):
    from orchestration import team_memory

    with pytest.raises(ValueError, match="Invalid"):
        team_memory.write_team_memory("alpha", "..\\parent\\file.md", "x")


def test_traversal_dotdot_refused(vault_root):
    from orchestration import team_memory

    with pytest.raises(ValueError, match="Invalid"):
        team_memory.write_team_memory(1, "..foo.md", "x")


def test_empty_filename_refused(vault_root):
    from orchestration import team_memory

    with pytest.raises(ValueError):
        team_memory.write_team_memory(1, "", "x")


# ── Agent (private) memory ────────────────────────────────────────────────


def test_agent_memory_writes_to_agent_path(vault_root):
    from orchestration import team_memory

    path = team_memory.write_agent_memory("agent-1", "scratch.md", "private")
    assert path.exists()
    assert path.parent == vault_root / "agents" / "agent-1"


def test_agent_memory_skips_secret_scan(vault_root):
    """Private memory isn't shared, so credential patterns are allowed."""
    from orchestration import team_memory

    # Would be refused in team memory — allowed here
    path = team_memory.write_agent_memory(
        "agent-1", "creds.md", "api_key = mysecretkey123456789",
    )
    assert path.exists()


def test_agent_memory_still_validates_filename(vault_root):
    from orchestration import team_memory

    with pytest.raises(ValueError, match="Invalid"):
        team_memory.write_agent_memory("agent-1", "../escape.md", "x")


def test_agent_memory_isolated_from_team_memory(vault_root):
    """Writing team memory does NOT touch agent memory and vice versa."""
    from orchestration import team_memory

    team_memory.write_team_memory(1, "shared.md", "team stuff")
    team_memory.write_agent_memory("alpha", "private.md", "agent stuff")

    assert team_memory.list_team_memory(1) == ["shared.md"]
    assert team_memory.list_agent_memory("alpha") == ["private.md"]


# ── API endpoint tests ────────────────────────────────────────────────────


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    """API client with isolated DB and vault."""
    db_path = tmp_path / "test_team_memory_api.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_ROOT", str(vault))

    with patch("config.ORCHESTRATION_DB_PATH", db_path):
        import importlib
        import orchestration.api as api_mod

        importlib.reload(api_mod)
        db, cs, ms, reg, ts = api_mod._get_services()
        api_mod._db = db
        api_mod._convoy_svc = cs
        api_mod._mailbox_svc = ms
        api_mod._executor_registry = reg
        api_mod._team_svc = ts

        from fastapi.testclient import TestClient
        yield TestClient(api_mod.app)
        db.close()


def _create_team(client, name: str = "alpha-team") -> int:
    r = client.post("/api/team", json={
        "team_name": name,
        "lead_agent_id": "lead-1",
    })
    assert r.status_code == 200, r.text
    return r.json()["session"]["id"]


def test_api_write_and_read_team_memory(api_client):
    team_id = _create_team(api_client)
    w = api_client.post(
        f"/api/team/{team_id}/memory/plan.md",
        json={"content": "# Plan\n- step 1\n"},
    )
    assert w.status_code == 200
    assert w.json()["status"] == "written"

    r = api_client.get(f"/api/team/{team_id}/memory/plan.md")
    assert r.status_code == 200
    assert r.json()["content"] == "# Plan\n- step 1\n"


def test_api_write_secret_content_returns_422(api_client):
    team_id = _create_team(api_client)
    r = api_client.post(
        f"/api/team/{team_id}/memory/leak.md",
        json={"content": "api_key = sk-abc123456789012345xyz"},
    )
    assert r.status_code == 422
    assert "secrets" in r.json()["detail"].lower()


def test_api_write_duplicate_returns_409(api_client):
    team_id = _create_team(api_client)
    api_client.post(
        f"/api/team/{team_id}/memory/plan.md",
        json={"content": "v1"},
    )
    r = api_client.post(
        f"/api/team/{team_id}/memory/plan.md",
        json={"content": "v2"},
    )
    assert r.status_code == 409


def test_api_write_overwrite_succeeds(api_client):
    team_id = _create_team(api_client)
    api_client.post(
        f"/api/team/{team_id}/memory/plan.md",
        json={"content": "v1"},
    )
    r = api_client.post(
        f"/api/team/{team_id}/memory/plan.md",
        json={"content": "v2", "overwrite": True},
    )
    assert r.status_code == 200
    got = api_client.get(f"/api/team/{team_id}/memory/plan.md").json()
    assert got["content"] == "v2"


def test_api_list_team_memory(api_client):
    team_id = _create_team(api_client)
    api_client.post(
        f"/api/team/{team_id}/memory/one.md", json={"content": "a"},
    )
    api_client.post(
        f"/api/team/{team_id}/memory/two.md", json={"content": "b"},
    )
    r = api_client.get(f"/api/team/{team_id}/memory")
    assert r.status_code == 200
    assert r.json()["files"] == ["one.md", "two.md"]


def test_api_read_missing_file_returns_404(api_client):
    team_id = _create_team(api_client)
    r = api_client.get(f"/api/team/{team_id}/memory/ghost.md")
    assert r.status_code == 404


def test_api_delete_team_memory(api_client):
    team_id = _create_team(api_client)
    api_client.post(
        f"/api/team/{team_id}/memory/plan.md", json={"content": "body"},
    )
    r = api_client.delete(f"/api/team/{team_id}/memory/plan.md")
    assert r.status_code == 200
    assert r.json()["status"] == "deleted"
    # Now gone
    assert api_client.get(f"/api/team/{team_id}/memory/plan.md").status_code == 404


def test_api_unknown_team_returns_404(api_client):
    r = api_client.get("/api/team/9999/memory")
    assert r.status_code == 404
