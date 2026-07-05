"""M5 (Homie Mobile persona switcher): web persona turn + conversation_send routing.

The dashboard/mobile `/api/conversation/{persona_id}/send` must answer AS the
named persona (web_persona_runtime), persist with persona_id attribution
(Act 5 corpus-safety class), and keep the default persona on the untouched
router path. Mirrors tests/test_discord_persona_persist_turn.py.
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

CHAT_DIR = Path(__file__).resolve().parents[2] / "chat"
if str(CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(CHAT_DIR))

from models import Channel, IncomingMessage, Platform, Thread, User  # noqa: E402
from runtime.base import RuntimeResult  # noqa: E402
from session import get_session_store  # noqa: E402
from web_persona_runtime import run_web_persona_turn  # noqa: E402


def _incoming(conversation_id: str = "dashboard-sales") -> IncomingMessage:
    return IncomingMessage(
        text="what leads do we have?",
        user=User(Platform.WEB, "mobile-user", "Mobile"),
        channel=Channel(Platform.WEB, conversation_id, is_dm=True),
        platform=Platform.WEB,
        thread=Thread(conversation_id),
    )


def _fake_result(**overrides):
    defaults = dict(
        text="sales answer",
        runtime_lane="claude_native",
        provider="claude",
        model="haiku",
        profile_key="test",
        session_id="sid-1",
    )
    defaults.update(overrides)
    return RuntimeResult(**defaults)


def _write_profile(homie_root: Path, persona_id: str) -> Path:
    profile_root = homie_root / "profiles" / persona_id
    memory_dir = profile_root / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (profile_root / "run").mkdir(parents=True, exist_ok=True)
    (profile_root / "skills").mkdir(parents=True, exist_ok=True)
    (profile_root / "config.yaml").write_text(
        f"persona:\n  display_name: {persona_id.title()}\n  role: test\n",
        encoding="utf-8",
    )
    (memory_dir / "SOUL.md").write_text("# Soul\ntest", encoding="utf-8")
    (memory_dir / "MEMORY.md").write_text("# Memory\ntest", encoding="utf-8")
    return profile_root


@pytest.fixture
def persona_env(tmp_path, monkeypatch):
    homie_root = tmp_path / ".homie"
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    matrix_path = tmp_path / "matrix.yaml"
    matrix_path.write_text(
        "env_groups: {}\nskill_groups: {}\nprofiles:\n  sales:\n    env_groups: []\n    skill_groups: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOMIE_PERSONA_CAPABILITY_MATRIX", str(matrix_path))
    _write_profile(homie_root, "sales")
    return tmp_path


# ── Full web persona turn ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_web_turn_persists_persona_id_under_web_session_key(persona_env):
    """The web turn persists under web:{cid}:{cid} WITH persona attribution."""
    store = get_session_store(persona_env / "chat.db")

    async def fake_run(req):
        return _fake_result()

    with patch("runtime.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        text = await run_web_persona_turn(
            incoming=_incoming("dashboard-sales"),
            persona_id="sales",
            session_store=store,
            project_root=persona_env,
        )

    assert text == "sales answer"
    session = store.get("web", "dashboard-sales", "dashboard-sales")
    assert session is not None
    assert session.session_id == "web:dashboard-sales:dashboard-sales"
    assert session.persona_id == "sales"
    messages = store.list_recent_messages("web:dashboard-sales:dashboard-sales", limit=10)
    roles = [m.role for m in messages]
    assert "user" in roles and "assistant" in roles


@pytest.mark.asyncio
async def test_web_turn_prompt_identifies_persona(persona_env):
    """The RuntimeRequest system prompt binds the persona identity, no tools."""
    store = get_session_store(persona_env / "chat.db")
    seen = {}

    async def fake_run(req):
        seen["req"] = req
        return _fake_result()

    with patch("runtime.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        await run_web_persona_turn(
            incoming=_incoming(),
            persona_id="sales",
            session_store=store,
            project_root=persona_env,
        )

    req = seen["req"]
    assert "`sales`" in req.system_prompt
    assert "Answer as this persona only" in req.system_prompt
    assert req.allowed_tools == []
    assert req.disallowed_tools == ["*"]
    assert req.max_turns == 1
    assert req.metadata["persona_id"] == "sales"


# ── Grep gates (same invariants as the Discord persona path) ─────────


def test_web_persona_turn_stays_no_tools_max_turns_1() -> None:
    src = (CHAT_DIR / "web_persona_runtime.py").read_text(encoding="utf-8")
    assert "max_turns=1" in src
    assert "allowed_tools=[]" in src
    assert 'disallowed_tools=["*"]' in src


def test_web_persona_runtime_reuses_discord_persist_helper() -> None:
    """One Act-5-safe persistence implementation, not a divergent copy."""
    src = (CHAT_DIR / "web_persona_runtime.py").read_text(encoding="utf-8")
    assert "from discord_persona_runtime import _persist_turn" in src


# ── conversation_send routing (dashboard_api) ────────────────────────


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    """Fresh orchestration app with isolated DB paths and loopback no-auth
    (slim mirror of test_dashboard_api.isolated_app)."""
    import config

    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", tmp_path / "dashboard.db")
    monkeypatch.setattr(config, "CHAT_DB_PATH", tmp_path / "chat.db")
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", tmp_path / "orchestration.db")
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


def _send_body(**overrides):
    body = {
        "text": "hola sales",
        "conversation_id": "dashboard-sales",
        "client_message_id": "t-1",
        "user_id": "mobile-user",
        "display_name": "Mobile",
        "source": "interactive",
    }
    body.update(overrides)
    return body


def test_persona_send_rejects_buttons(isolated_app):
    r = isolated_app.post(
        "/api/conversation/sales/send",
        json=_send_body(text=None, button_custom_id="btn-1"),
    )
    assert r.status_code == 400
    assert "buttons" in r.json()["detail"]


def test_persona_send_rejects_camera(isolated_app):
    r = isolated_app.post(
        "/api/conversation/sales/send",
        json=_send_body(image_base64="AAAA"),
    )
    assert r.status_code == 400
    assert "main-only" in r.json()["detail"]


def test_persona_send_routes_to_web_persona_runtime(isolated_app, monkeypatch):
    """A non-default persona send queues the persona turn (NOT the router) and
    the SSE buffer ends at: user_message -> processing -> assistant_message,
    with the reply replacing the Thinking... event."""
    import dashboard_api
    import web_persona_runtime

    calls = {}

    async def fake_turn(*, incoming, persona_id, session_store, project_root):
        calls["persona_id"] = persona_id
        calls["text"] = incoming.text
        return "answer as sales"

    monkeypatch.setattr(web_persona_runtime, "run_web_persona_turn", fake_turn)

    def _fail_runtime():  # the default-only router path must stay cold
        raise AssertionError("persona send must not build the dashboard router runtime")

    monkeypatch.setattr(dashboard_api, "_get_dashboard_chat_runtime", _fail_runtime)
    dashboard_api._SSE_REPLAY_BUFFERS.pop(("sales", "dashboard-sales"), None)

    r = isolated_app.post("/api/conversation/sales/send", json=_send_body())
    assert r.status_code == 200
    assert r.json()["queued"] is True
    assert r.json()["persona_id"] == "sales"

    deadline = time.time() + 5
    buf = []
    while time.time() < deadline:
        buf = dashboard_api._sse_buffer_for("sales", "dashboard-sales")
        if any(ev_type == "assistant_message" for _, ev_type, _ in buf):
            break
        time.sleep(0.02)

    types = [ev_type for _, ev_type, _ in buf]
    assert types == ["user_message", "processing", "assistant_message"], types
    assert calls["persona_id"] == "sales"
    assert "hola sales" in calls["text"]
    assert '"replaces_event_id": 2' in buf[2][2]
    assert "answer as sales" in buf[2][2]


def test_persona_send_failure_becomes_sse_error_event(isolated_app, monkeypatch):
    """A persona-turn crash surfaces as an SSE error event, never an unhandled task."""
    import dashboard_api
    import web_persona_runtime

    async def boom(**kwargs):
        raise RuntimeError("lane exploded")

    monkeypatch.setattr(web_persona_runtime, "run_web_persona_turn", boom)
    dashboard_api._SSE_REPLAY_BUFFERS.pop(("sales", "dashboard-sales"), None)

    r = isolated_app.post("/api/conversation/sales/send", json=_send_body())
    assert r.status_code == 200

    deadline = time.time() + 5
    buf = []
    while time.time() < deadline:
        buf = dashboard_api._sse_buffer_for("sales", "dashboard-sales")
        if any(ev_type == "error" for _, ev_type, _ in buf):
            break
        time.sleep(0.02)

    types = [ev_type for _, ev_type, _ in buf]
    assert "error" in types, types
    assert "lane exploded" in buf[-1][2]
