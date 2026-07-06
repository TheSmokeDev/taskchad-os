"""Homie Mobile M9 — library endpoints (read-only, admin) + attach-any-file.

GET /api/skills       — installed skills via framework_registry.discover_skills
GET /api/files/list   — allowlisted-root directory listing, traversal-blocked
GET /api/files/read   — text file read, size-capped, binary-rejected
GET /api/system-jobs  — framework job state files (physical truth, Rule 2)
POST …/send document_base64 — file rides the turn as an Attachment
"""

from __future__ import annotations

import base64
import importlib
import json

import pytest


@pytest.fixture
def dash_client(tmp_path, monkeypatch):
    """Isolated app with a fake PROJECT_ROOT holding vault + docs + a skill."""
    from fastapi.testclient import TestClient

    import config

    project_root = tmp_path / "project"
    memory = project_root / "TheHomie" / "Memory"
    docs = project_root / "docs" / "manual"
    (memory / "concepts").mkdir(parents=True)
    docs.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("# Memory\nzanzibar note body", encoding="utf-8")
    (memory / "concepts" / "TEST-CONCEPT.md").write_text("# Concept", encoding="utf-8")
    (memory / ".hidden.md").write_text("secret", encoding="utf-8")
    (docs / "README.md").write_text("# Manual", encoding="utf-8")
    (memory / "binary.bin").write_bytes(b"\xff\xfe\x00\x01binary")
    # Outside-the-root file a traversal would try to reach.
    (project_root / ".env").write_text("SECRET=1", encoding="utf-8")

    skill_dir = project_root / ".claude" / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test skill for M9.\n---\n# Test", encoding="utf-8"
    )

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    heartbeat_state = state_dir / "heartbeat-state.json"
    heartbeat_state.write_text(
        json.dumps({"last_run": "2026-07-05T08:00:00", "runs": 42, "nested": {"drop": 1}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", tmp_path / "dashboard.db")
    monkeypatch.setattr(config, "CHAT_DB_PATH", tmp_path / "chat.db")
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", tmp_path / "orch.db")
    monkeypatch.setattr(config, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(config, "HEARTBEAT_STATE_FILE", heartbeat_state)
    monkeypatch.setattr(config, "REFLECTION_STATE_FILE", state_dir / "missing.json")
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


# ── skills ────────────────────────────────────────────────────────────────


def test_skills_list_returns_installed_skills(dash_client) -> None:
    r = dash_client.get("/api/skills")
    assert r.status_code == 200
    skills = r.json()["skills"]
    assert any(s["name"] == "test-skill" for s in skills)
    entry = next(s for s in skills if s["name"] == "test-skill")
    assert "test skill" in entry["description"].lower()
    assert entry["path"].endswith("SKILL.md")


# ── files ─────────────────────────────────────────────────────────────────


def test_files_list_root_dir(dash_client) -> None:
    r = dash_client.get("/api/files/list?root=memory")
    assert r.status_code == 200
    body = r.json()
    names = {e["name"]: e for e in body["entries"]}
    assert names["concepts"]["kind"] == "dir"
    assert names["MEMORY.md"]["kind"] == "file"
    assert ".hidden.md" not in names  # dotfiles skipped
    # Dirs sort before files.
    kinds = [e["kind"] for e in body["entries"]]
    assert kinds == sorted(kinds, key=lambda k: k == "file")


def test_files_read_returns_content(dash_client) -> None:
    r = dash_client.get("/api/files/read?root=memory&path=MEMORY.md")
    assert r.status_code == 200
    body = r.json()
    assert "zanzibar note body" in body["content"]
    assert body["truncated"] is False


def test_files_docs_root_works(dash_client) -> None:
    r = dash_client.get("/api/files/read?root=docs&path=README.md")
    assert r.status_code == 200
    assert "# Manual" in r.json()["content"]


def test_files_traversal_attacks_rejected(dash_client) -> None:
    # Classic dot-dot escape toward the project .env.
    for attack in [
        "../../.env",
        "..%2F..%2F.env",
        "concepts/../../../.env",
    ]:
        r = dash_client.get(f"/api/files/read?root=memory&path={attack}")
        assert r.status_code in (400, 404), attack
        assert "SECRET" not in r.text


def test_files_unknown_root_rejected(dash_client) -> None:
    r = dash_client.get("/api/files/list?root=etc")
    assert r.status_code == 400


def test_files_binary_rejected(dash_client) -> None:
    r = dash_client.get("/api/files/read?root=memory&path=binary.bin")
    assert r.status_code == 415


def test_files_no_write_route_exists(dash_client) -> None:
    for method in ("post", "put", "patch", "delete"):
        r = getattr(dash_client, method)("/api/files/read?root=memory&path=MEMORY.md")
        assert r.status_code in (404, 405)


# ── system jobs ───────────────────────────────────────────────────────────


def test_system_jobs_surfaces_state_files(dash_client) -> None:
    r = dash_client.get("/api/system-jobs")
    assert r.status_code == 200
    jobs = {j["name"]: j for j in r.json()["jobs"]}
    heartbeat = jobs["heartbeat"]
    assert heartbeat["exists"] is True
    assert heartbeat["state"]["last_run"] == "2026-07-05T08:00:00"
    assert heartbeat["state"]["runs"] == 42
    assert "nested" not in heartbeat["state"]  # shallow scalars only
    assert jobs["reflection"]["exists"] is False


# ── attach-any-file on send ───────────────────────────────────────────────


class _FakeChatRouter:
    def __init__(self) -> None:
        self.queued: list = []

    def _queue_incoming(self, adapter, incoming) -> None:
        self.queued.append(incoming)


class _FakeChatAdapter:
    def track(self, *, persona_id: str, conversation_id: str) -> None:
        pass


def test_send_document_attaches_file_and_prefixes_text(dash_client, monkeypatch) -> None:
    import dashboard_api

    fake_router = _FakeChatRouter()
    runtime = {"router": fake_router, "adapter": _FakeChatAdapter()}
    monkeypatch.setattr(dashboard_api, "_get_dashboard_chat_runtime", lambda: runtime)
    monkeypatch.setattr(dashboard_api, "_DASHBOARD_CHAT_RUNTIME", runtime)

    payload = base64.b64encode(b"quarterly numbers: 42").decode()
    r = dash_client.post(
        "/api/conversation/default/send",
        json={
            "text": "summarize this",
            "document_base64": payload,
            "document_name": "q3 report!!.txt",
        },
    )
    assert r.status_code == 200
    incoming = fake_router.queued[0]
    # Sanitizer collapses the `!!` run into one underscore.
    assert incoming.text.startswith("[Document received: q3 report_.txt]")
    assert len(incoming.attachments) == 1
    attachment = incoming.attachments[0]
    assert attachment.filename == "q3 report_.txt"
    with open(attachment.url, "rb") as fh:
        assert fh.read() == b"quarterly numbers: 42"


def test_send_document_is_main_only(dash_client) -> None:
    payload = base64.b64encode(b"x").decode()
    r = dash_client.post(
        "/api/conversation/sales/send",
        json={"text": "hi", "document_base64": payload, "document_name": "a.txt"},
    )
    assert r.status_code == 400
    assert "main-only" in r.json()["detail"]
