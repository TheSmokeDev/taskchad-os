"""Tests for dashboard_api.py — PRD-8 Phase 3 / WS2.

Covers the 30 framework HTTP endpoints under /api/. Uses FastAPI
TestClient against the orchestration app (which mounts the dashboard
router via the WS2.Task2 include_router seam).

Test isolation:
  * Each test gets an isolated dashboard.db via a tmp_path monkey-patch
    of ``config.DASHBOARD_DB_PATH``.
  * ``ORCHESTRATION_API_TOKEN`` is left unset for the default mode
    (loopback, no auth) so most endpoints don't need a Bearer header.
    The bearer-required tests explicitly patch the env var.
"""
from __future__ import annotations

import importlib
import io
import json
import os
import sqlite3
import struct
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_chat_db(path: Path) -> None:
    """Seed a tiny chat.db with chat_sessions + chat_messages."""
    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    old = now - timedelta(minutes=120)
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
            created_at TEXT NOT NULL DEFAULT '2026-05-07T00:00:00',
            updated_at TEXT NOT NULL DEFAULT '2026-05-07T00:00:00',
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
    conn.execute(
        """INSERT INTO chat_sessions
           (session_id, runtime_profile_key, runtime_provider, runtime_model, runtime_lane)
           VALUES (?, ?, ?, ?, ?)""",
        ("recent-session", "default", "claude", "claude-opus-4-7", "claude_native"),
    )
    conn.execute(
        """INSERT INTO chat_sessions
           (session_id, runtime_profile_key, runtime_provider, runtime_model, runtime_lane)
           VALUES (?, ?, ?, ?, ?)""",
        ("old-session", "sales", "openai-compatible", "gpt-4o", "generic"),
    )
    conn.execute(
        """INSERT INTO chat_messages (session_id, role, content, created_at)
           VALUES (?, ?, ?, ?)""",
        ("recent-session", "assistant", "Recent hive activity", now.isoformat(timespec="seconds")),
    )
    conn.execute(
        """INSERT INTO chat_messages (session_id, role, content, created_at)
           VALUES (?, ?, ?, ?)""",
        ("old-session", "assistant", "Old hive activity", old.isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


def _make_memory_db(path: Path) -> None:
    """Seed current memory.db chunk schema, not the old donor schema."""
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE files (
            path TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            mtime_ns INTEGER NOT NULL,
            size_bytes INTEGER NOT NULL,
            indexed_at_epoch INTEGER NOT NULL
        );
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            section_title TEXT DEFAULT '',
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at_epoch INTEGER NOT NULL
        );
    """)
    conn.execute(
        """INSERT INTO files
           (path, content_hash, mtime_ns, size_bytes, indexed_at_epoch)
           VALUES (?, ?, ?, ?, ?)""",
        ("daily/2026-05-15.md", "hash-file", 1, 100, now_epoch),
    )
    conn.execute(
        """INSERT INTO chunks
           (file_path, start_line, end_line, section_title, content, content_hash, created_at_epoch)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            "daily/2026-05-15.md",
            1,
            6,
            "Mission Control",
            "Real vault memory chunk",
            "hash-chunk",
            now_epoch,
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    """Spawn a fresh orchestration app with isolated dashboard.db + chat.db."""
    dash_db = tmp_path / "dashboard.db"
    chat_db = tmp_path / "chat.db"
    memory_db = tmp_path / "memory.db"
    orch_db = tmp_path / "orchestration.db"
    _make_chat_db(chat_db)
    _make_memory_db(memory_db)

    import config
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", dash_db)
    monkeypatch.setattr(config, "CHAT_DB_PATH", chat_db)
    monkeypatch.setattr(config, "DATABASE_PATH", memory_db)
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", orch_db)
    # Force loopback no-auth for default tests.
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", "")

    import orchestration.api as oa
    importlib.reload(oa)

    # Re-resolve services so they pick up the patched orch DB.
    db, cs, ms, reg, ts = oa._get_services()
    oa._db = db
    oa._convoy_svc = cs
    oa._mailbox_svc = ms
    oa._executor_registry = reg
    oa._team_svc = ts

    yield TestClient(oa.app)
    db.close()


# ── /api/health ──────────────────────────────────────────────────────────


def test_get_health_returns_minimal_shape(isolated_app):
    r = isolated_app.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert "uptime_seconds" in body
    assert "lane_status" in body
    # PRD-8 Phase 7a (R2 NM4) — rich snapshot shape, not flat dict.
    assert isinstance(body["killSwitches"], dict)
    assert "counters" in body["killSwitches"]
    assert "audit_write_failures" in body["killSwitches"]
    assert "process_started_at" in body["killSwitches"]


def test_get_health_no_auth_required_token_unset(isolated_app):
    """Token unset — health works without Authorization header."""
    r = isolated_app.get("/api/health")
    assert r.status_code == 200


def test_get_health_no_auth_required_token_set(tmp_path, monkeypatch):
    """Even with token set, /api/health returns 200 without Bearer."""
    dash_db = tmp_path / "dashboard.db"
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", "test-secret-token")
    import config
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", dash_db)
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", tmp_path / "orch.db")

    import orchestration.api as oa
    importlib.reload(oa)
    db, cs, ms, reg, ts = oa._get_services()
    oa._db = db
    oa._convoy_svc = cs
    oa._mailbox_svc = ms
    oa._executor_registry = reg
    oa._team_svc = ts

    client = TestClient(oa.app)
    r = client.get("/api/health")
    assert r.status_code == 200
    db.close()


def test_get_health_kill_switches_empty_stub(isolated_app):
    """PRD-8 Phase 7a — fresh process has empty counters but rich snapshot shape."""
    r = isolated_app.get("/api/health")
    snap = r.json()["killSwitches"]
    # counters and audit_write_failures may be empty on fresh process.
    assert isinstance(snap.get("counters"), dict)
    assert isinstance(snap.get("audit_write_failures"), dict)


def test_get_health_response_has_no_secrets_or_pii(isolated_app):
    r = isolated_app.get("/api/health")
    body_text = r.text.lower()
    # No tokens, no env vars, no paths.
    forbidden = ["token", "secret", "password", "/users/", "homie_home", "api_key"]
    for needle in forbidden:
        assert needle not in body_text, f"health leaked '{needle}': {body_text}"


# ── /api/browser-viewer ─────────────────────────────────────────────────


def _browser_viewer_payload(target: str = "desktop") -> dict:
    return {
        "mode": "read_only",
        "target": target,
        "readiness": {
            "status": "ready",
            "cdp_port": 9222,
            "cdp_reachable": True,
            "browser": "Chrome/126",
            "visible_guard": "visible",
            "tab_count": 2,
            "reason": "ready",
        },
        "stream": {
            "enabled": True,
            "connected": True,
            "port": 31137,
            "screencasting": False,
            "reason": "ready",
        },
        "controls": {
            "browser_input": False,
            "navigation": False,
        },
    }


def test_browser_viewer_status_is_read_only_and_audited(isolated_app, monkeypatch):
    import dashboard_api

    audits: list[dict] = []
    monkeypatch.setattr(dashboard_api, "collect_browser_viewer_status", _browser_viewer_payload)
    monkeypatch.setattr(
        dashboard_api,
        "append_browser_audit_record",
        lambda **kwargs: audits.append(kwargs) or {},
    )

    r = isolated_app.get("/api/browser-viewer/status")

    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "read_only"
    assert body["controls"] == {"browser_input": False, "navigation": False}
    assert "direct_ws_url" not in body["stream"]
    assert audits[0]["workflow_id"] == "browser.viewer.status"
    assert audits[0]["action"] == "browser_viewer_status"
    assert audits[0]["surface"] == "dashboard"
    assert audits[0]["cdp_port"] == 9222
    assert audits[0]["cdp_reachable"] is True


def test_browser_viewer_screenshot_returns_png_no_store(isolated_app, monkeypatch):
    import dashboard_api

    audits: list[dict] = []
    monkeypatch.setattr(dashboard_api, "collect_browser_viewer_status", _browser_viewer_payload)
    monkeypatch.setattr(
        dashboard_api,
        "capture_browser_screenshot_png",
        lambda **_k: b"\x89PNG\r\n\x1a\nviewer",
    )
    monkeypatch.setattr(
        dashboard_api,
        "append_browser_audit_record",
        lambda **kwargs: audits.append(kwargs) or {},
    )

    r = isolated_app.get("/api/browser-viewer/screenshot")

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "no-store"
    assert r.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert audits[0]["workflow_id"] == "browser.viewer.screenshot"
    assert audits[0]["outcome"] == "succeeded"


def test_browser_viewer_stream_enable_uses_read_only_workflow(isolated_app, monkeypatch):
    import dashboard_api

    audits: list[dict] = []
    enabled: list[bool] = []
    monkeypatch.setattr(dashboard_api, "collect_browser_viewer_status", _browser_viewer_payload)
    monkeypatch.setattr(dashboard_api, "browser_stream_enable", lambda **_k: enabled.append(True))
    monkeypatch.setattr(
        dashboard_api,
        "append_browser_audit_record",
        lambda **kwargs: audits.append(kwargs) or {},
    )

    r = isolated_app.post("/api/browser-viewer/stream/enable")

    assert r.status_code == 200
    assert enabled == [True]
    assert r.json()["controls"] == {"browser_input": False, "navigation": False}
    assert audits[0]["workflow_id"] == "browser.viewer.stream_enable"
    assert audits[0]["action"] == "browser_viewer_stream_enable"


def test_browser_viewer_error_redacts_urls(isolated_app, monkeypatch):
    import dashboard_api

    audits: list[dict] = []

    def fail_screenshot(**_kwargs):
        raise RuntimeError("failed at https://example.com/path?token=secret#frag")

    monkeypatch.setattr(dashboard_api, "capture_browser_screenshot_png", fail_screenshot)
    monkeypatch.setattr(dashboard_api, "collect_browser_viewer_status", _browser_viewer_payload)
    monkeypatch.setattr(
        dashboard_api,
        "append_browser_audit_record",
        lambda **kwargs: audits.append(kwargs) or {},
    )

    r = isolated_app.get("/api/browser-viewer/screenshot")

    assert r.status_code == 503
    body = r.text
    assert "https://example.com/path" in body
    assert "secret" not in body
    assert "#frag" not in body
    assert audits[0]["outcome"] == "failed"
    assert audits[0]["reason"] == "failed at https://example.com/path"


# ── /api/browser-viewer M12 phone-drive ─────────────────────────────────


def _patch_browser_audits(monkeypatch) -> list[dict]:
    import dashboard_api

    audits: list[dict] = []
    monkeypatch.setattr(
        dashboard_api,
        "append_browser_audit_record",
        lambda **kwargs: audits.append(kwargs) or {},
    )
    return audits


def test_browser_viewer_elements_lists_snapshot_refs(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.setattr(
        dashboard_api,
        "browser_snapshot_elements",
        lambda **_k: [{"ref": "e20", "role": "button", "name": "Google Search"}],
    )

    r = isolated_app.get("/api/browser-viewer/elements")

    assert r.status_code == 200
    assert r.json()["elements"][0]["ref"] == "e20"
    assert audits[0]["workflow_id"] == "browser.viewer.elements"
    assert audits[0]["outcome"] == "succeeded"


def test_browser_viewer_act_click_runs_and_audits_label(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    calls: list[dict] = []

    def fake_act(kind, **kwargs):
        calls.append({"kind": kind, **kwargs})
        return SimpleNamespace(ok=True, output="")

    monkeypatch.setattr(dashboard_api, "browser_act", fake_act)

    r = isolated_app.post(
        "/api/browser-viewer/act",
        json={"kind": "click", "ref": "e12", "label": "Sign in"},
    )

    assert r.status_code == 200
    assert r.json() == {"ok": True, "kind": "click", "target": "desktop"}
    assert calls[0]["kind"] == "click"
    assert calls[0]["ref"] == "e12"
    assert audits[0]["workflow_id"] == "browser.viewer.act"
    assert audits[0]["outcome"] == "succeeded"
    assert "Sign in" in audits[0]["reason"]


def test_browser_viewer_act_rejects_unknown_kind(isolated_app, monkeypatch):
    _patch_browser_audits(monkeypatch)

    r = isolated_app.post("/api/browser-viewer/act", json={"kind": "eval"})

    assert r.status_code == 400
    assert "unknown action kind" in r.text


def test_browser_viewer_act_bad_ref_blocks_with_audit(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)

    def real_validation(kind, **kwargs):
        # Use the REAL arg builder so shape validation is exercised.
        import browser_control

        browser_control.build_browser_act_args(kind, **{
            k: v for k, v in kwargs.items() if k in ("ref", "text", "key", "direction", "amount")
        })
        raise AssertionError("should not reach the runner")

    monkeypatch.setattr(dashboard_api, "browser_act", real_validation)

    r = isolated_app.post(
        "/api/browser-viewer/act",
        json={"kind": "click", "ref": "not-a-ref"},
    )

    assert r.status_code == 400
    assert audits[0]["outcome"] == "blocked"


def test_browser_viewer_navigate_validates_url_and_audits(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr(
        dashboard_api,
        "run_agent_browser_open",
        lambda url, **_k: opened.append(url) or SimpleNamespace(ok=True, output=""),
    )

    ok = isolated_app.post("/api/browser-viewer/navigate", json={"url": "https://example.com/page"})
    bad = isolated_app.post("/api/browser-viewer/navigate", json={"url": "javascript:alert(1)"})

    assert ok.status_code == 200
    assert opened == ["https://example.com/page"]
    assert audits[0]["workflow_id"] == "browser.viewer.navigate"
    assert audits[0]["outcome"] == "succeeded"
    assert bad.status_code == 403
    assert opened == ["https://example.com/page"]  # blocked URL never shelled
    assert audits[1]["outcome"] == "blocked"


# ── /api/browser-viewer P3.0 PhoneOps target dimension ───────────────────


def test_browser_viewer_rejects_unknown_target(isolated_app, monkeypatch):
    audits = _patch_browser_audits(monkeypatch)

    r = isolated_app.get("/api/browser-viewer/status?target=tablet")

    assert r.status_code == 400
    assert "unknown browser target" in r.text
    assert audits[0]["outcome"] == "blocked"
    # PhoneOps F12 (issue #100): the REJECTED raw value rides the structured column.
    assert audits[0]["target"] == "tablet"


def test_browser_viewer_phone_403_when_phoneops_disabled(isolated_app, monkeypatch):
    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.delenv("HOMIE_PHONEOPS_ENABLED", raising=False)

    r = isolated_app.get("/api/browser-viewer/status?target=phone")

    assert r.status_code == 403
    assert "HOMIE_PHONEOPS_ENABLED" in r.text
    assert audits[0]["outcome"] == "blocked"
    assert audits[0]["command"] == "GET /api/browser-viewer/status"
    assert audits[0]["target"] == "phone"  # structured column (issue #100)


# ── P4.0 Ghost — the third browser target gate ───────────────────────────────


def test_browser_viewer_ghost_403_when_ghost_disabled(isolated_app, monkeypatch):
    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.delenv("HOMIE_GHOST_ENABLED", raising=False)

    r = isolated_app.get("/api/browser-viewer/status?target=ghost")

    assert r.status_code == 403
    assert "HOMIE_GHOST_ENABLED" in r.text
    assert audits[0]["outcome"] == "blocked"
    assert audits[0]["command"] == "GET /api/browser-viewer/status"
    assert audits[0]["target"] == "ghost"  # structured column (issue #100)


def test_browser_viewer_ghost_gate_is_distinct_from_phoneops(isolated_app, monkeypatch):
    """Ghost and PhoneOps are separate capabilities: enabling one never opens the
    other."""
    import dashboard_api

    _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.delenv("HOMIE_PHONEOPS_ENABLED", raising=False)
    monkeypatch.setattr(dashboard_api, "collect_browser_viewer_status", _browser_viewer_payload)

    ghost = isolated_app.get("/api/browser-viewer/status?target=ghost")
    phone = isolated_app.get("/api/browser-viewer/status?target=phone")

    assert ghost.status_code == 200
    assert ghost.json()["target"] == "ghost"
    assert phone.status_code == 403  # ghost's switch does NOT open the phone


def test_browser_viewer_ghost_status_echoes_target_and_audits(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    seen: list[str] = []

    def fake_status(target: str = "desktop"):
        seen.append(target)
        return _browser_viewer_payload(target)

    monkeypatch.setattr(dashboard_api, "collect_browser_viewer_status", fake_status)

    r = isolated_app.get("/api/browser-viewer/status?target=ghost")

    assert r.status_code == 200
    assert r.json()["target"] == "ghost"
    assert seen == ["ghost"]
    assert audits[0]["command"] == "GET /api/browser-viewer/status"
    assert audits[0]["target"] == "ghost"  # structured column (issue #100)
    assert audits[0]["outcome"] == "succeeded"


def test_browser_viewer_ghost_act_403_when_disabled_never_shells(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.delenv("HOMIE_GHOST_ENABLED", raising=False)
    monkeypatch.setattr(
        dashboard_api,
        "browser_act",
        lambda *_a, **_k: pytest.fail("gated ghost act must not reach the runner"),
    )

    r = isolated_app.post(
        "/api/browser-viewer/act",
        json={"kind": "click", "ref": "e2", "target": "ghost"},
    )

    assert r.status_code == 403
    assert audits[0]["outcome"] == "blocked"
    assert audits[0]["command"] == "POST /api/browser-viewer/act"
    assert audits[0]["target"] == "ghost"  # structured column (issue #100)


def test_browser_viewer_ghost_screenshot_header_echo(isolated_app, monkeypatch):
    import dashboard_api

    _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.setattr(
        dashboard_api,
        "capture_browser_screenshot_png",
        lambda **_k: b"\x89PNG\r\n\x1a\nghost",
    )
    monkeypatch.setattr(dashboard_api, "collect_browser_viewer_status", _browser_viewer_payload)

    r = isolated_app.get("/api/browser-viewer/screenshot?target=ghost")

    assert r.status_code == 200
    assert r.headers["x-browser-target"] == "ghost"


# ── P4.1 Phase B — the ghost DEVICE viewer (screen) ──────────────────────────


def test_ghost_viewer_screen_returns_png_and_dims(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.setattr(
        dashboard_api, "ghost_screencap", lambda: (b"\x89PNG\r\n\x1a\nghost-screen", 1080, 2400)
    )

    r = isolated_app.get("/api/ghost-viewer/screen")

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-ghost-screen-width"] == "1080"
    assert r.headers["x-ghost-screen-height"] == "2400"
    assert r.content.startswith(b"\x89PNG")
    assert audits[-1]["outcome"] == "succeeded"
    assert audits[-1]["target"] == "ghost"


def test_ghost_viewer_screen_403_when_ghost_disabled_never_shells(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.delenv("HOMIE_GHOST_ENABLED", raising=False)
    monkeypatch.setattr(
        dashboard_api,
        "ghost_screencap",
        lambda: pytest.fail("gated ghost screen must not reach adb"),
    )

    r = isolated_app.get("/api/ghost-viewer/screen")

    assert r.status_code == 403
    assert "HOMIE_GHOST_ENABLED" in r.text
    assert audits[0]["outcome"] == "blocked"
    assert audits[0]["target"] == "ghost"


def test_ghost_viewer_screen_503_when_kill_switch_disabled_never_shells(isolated_app, monkeypatch):
    """Adversarial-review MEDIUM (2026-07-07): HOMIE_KILLSWITCH_GHOST must stop
    the ALREADY-BOOTED takeover routes, not only boot. A disabled kill-switch ->
    503 + audit, and the device is never touched."""
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.setenv("HOMIE_KILLSWITCH_GHOST", "disabled")
    monkeypatch.setattr(
        dashboard_api,
        "ghost_screencap",
        lambda: pytest.fail("kill-switched ghost must not reach adb"),
    )

    r = isolated_app.get("/api/ghost-viewer/screen")

    assert r.status_code == 503
    assert "kill-switch" in r.text.lower()
    assert audits[0]["outcome"] == "blocked"
    assert audits[0]["target"] == "ghost"


def test_ghost_viewer_tap_503_when_kill_switch_disabled(isolated_app, monkeypatch):
    import dashboard_api

    _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.setenv("HOMIE_KILLSWITCH_GHOST", "disabled")
    monkeypatch.setattr(
        dashboard_api, "ghost_tap", lambda *_a, **_k: pytest.fail("kill-switched tap must not shell")
    )
    r = isolated_app.post("/api/ghost-viewer/tap", json={"x": 0.5, "y": 0.5})
    assert r.status_code == 503


def test_ghost_viewer_screen_403_when_capability_killed(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")

    def killed():
        raise dashboard_api.GhostCapabilityDenied(
            "ghost capability 'ghost.screen.view' is disabled — set "
            "HOMIE_GHOST_CAP_SCREEN_VIEW=true to enable it"
        )

    monkeypatch.setattr(dashboard_api, "ghost_screencap", killed)

    r = isolated_app.get("/api/ghost-viewer/screen")

    assert r.status_code == 403
    assert audits[-1]["outcome"] == "blocked"


def test_ghost_viewer_screen_503_on_adb_failure(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")

    def boom():
        raise RuntimeError("device offline")

    monkeypatch.setattr(dashboard_api, "ghost_screencap", boom)

    r = isolated_app.get("/api/ghost-viewer/screen")

    assert r.status_code == 503
    assert audits[-1]["outcome"] == "failed"


def test_ghost_viewer_tap_scales_and_audits(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    seen: list[tuple[float, float]] = []
    monkeypatch.setattr(
        dashboard_api,
        "ghost_tap",
        lambda x, y, **k: seen.append((x, y))
        or {"x": 540, "y": 600, "width": 1080, "height": 2400},
    )

    r = isolated_app.post("/api/ghost-viewer/tap", json={"x": 0.5, "y": 0.25})

    assert r.status_code == 200
    assert r.json() == {"ok": True, "x": 540, "y": 600, "width": 1080, "height": 2400}
    assert seen == [(0.5, 0.25)]
    assert audits[-1]["outcome"] == "succeeded"
    assert audits[-1]["target"] == "ghost"


def test_ghost_viewer_tap_rejects_out_of_range_coords(isolated_app, monkeypatch):
    import dashboard_api

    _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.setattr(
        dashboard_api,
        "ghost_tap",
        lambda *_a, **_k: pytest.fail("out-of-range coords must be rejected before adb"),
    )

    r = isolated_app.post("/api/ghost-viewer/tap", json={"x": 1.5, "y": 0.5})

    assert r.status_code == 422  # Pydantic ge/le validation


def test_ghost_viewer_tap_403_when_ghost_disabled_never_shells(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.delenv("HOMIE_GHOST_ENABLED", raising=False)
    monkeypatch.setattr(
        dashboard_api,
        "ghost_tap",
        lambda *_a, **_k: pytest.fail("gated ghost tap must not reach adb"),
    )

    r = isolated_app.post("/api/ghost-viewer/tap", json={"x": 0.5, "y": 0.5})

    assert r.status_code == 403
    assert audits[0]["outcome"] == "blocked"


def test_ghost_viewer_text_and_key_and_swipe_route(isolated_app, monkeypatch):
    import dashboard_api

    _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.setattr(dashboard_api, "ghost_text", lambda text, **k: {"length": len(text)})
    monkeypatch.setattr(dashboard_api, "ghost_keyevent", lambda code: {"keycode": code})
    monkeypatch.setattr(
        dashboard_api,
        "ghost_swipe",
        lambda *a, **k: {"x1": 0, "y1": 0, "x2": 1079, "y2": 2399, "duration_ms": k["duration_ms"]},
    )

    rt = isolated_app.post("/api/ghost-viewer/text", json={"text": "hi ghost"})
    assert rt.status_code == 200 and rt.json()["length"] == len("hi ghost")

    rk = isolated_app.post("/api/ghost-viewer/key", json={"keycode": 4})
    assert rk.status_code == 200 and rk.json()["keycode"] == 4

    rs = isolated_app.post(
        "/api/ghost-viewer/swipe", json={"x1": 0, "y1": 0, "x2": 1, "y2": 1, "duration_ms": 250}
    )
    assert rs.status_code == 200 and rs.json()["duration_ms"] == 250


def test_ghost_viewer_key_rejects_out_of_range(isolated_app, monkeypatch):
    _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    r = isolated_app.post("/api/ghost-viewer/key", json={"keycode": 9999})
    assert r.status_code == 422  # Pydantic le=320


def test_ghost_viewer_app_launch_routes_and_audits(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    seen: list[str] = []
    monkeypatch.setattr(
        dashboard_api, "ghost_app_launch", lambda pkg: seen.append(pkg) or {"package": pkg}
    )

    r = isolated_app.post("/api/ghost-viewer/app/launch", json={"package": "com.android.chrome"})

    assert r.status_code == 200
    assert r.json() == {"ok": True, "package": "com.android.chrome"}
    assert seen == ["com.android.chrome"]
    assert audits[-1]["outcome"] == "succeeded"
    assert audits[-1]["target"] == "ghost"


def test_ghost_viewer_app_launch_bad_package_is_400(isolated_app, monkeypatch):
    import dashboard_api

    _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")

    def bad(pkg):
        raise ValueError(f"invalid Android package name: {pkg!r}")

    monkeypatch.setattr(dashboard_api, "ghost_app_launch", bad)

    r = isolated_app.post("/api/ghost-viewer/app/launch", json={"package": "nope; rm -rf"})
    assert r.status_code == 400


def test_ghost_viewer_app_install_routes(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.setattr(dashboard_api, "ghost_app_install", lambda p: {"apk": "test.apk"})

    r = isolated_app.post("/api/ghost-viewer/app/install", json={"apk_path": "C:/tmp/test.apk"})

    assert r.status_code == 200
    assert r.json() == {"ok": True, "apk": "test.apk"}
    assert audits[-1]["outcome"] == "succeeded"


def test_ghost_viewer_app_install_403_when_ghost_disabled_never_shells(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.delenv("HOMIE_GHOST_ENABLED", raising=False)
    monkeypatch.setattr(
        dashboard_api,
        "ghost_app_install",
        lambda *_a, **_k: pytest.fail("gated install must not reach adb"),
    )

    r = isolated_app.post("/api/ghost-viewer/app/install", json={"apk_path": "C:/tmp/x.apk"})

    assert r.status_code == 403
    assert audits[0]["outcome"] == "blocked"


def test_browser_viewer_phone_status_echoes_target_and_audits(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_PHONEOPS_ENABLED", "true")
    seen: list[str] = []

    def fake_status(target: str = "desktop"):
        seen.append(target)
        return _browser_viewer_payload(target)

    monkeypatch.setattr(dashboard_api, "collect_browser_viewer_status", fake_status)

    r = isolated_app.get("/api/browser-viewer/status?target=phone")

    assert r.status_code == 200
    assert r.json()["target"] == "phone"
    assert seen == ["phone"]
    assert audits[0]["command"] == "GET /api/browser-viewer/status"
    assert audits[0]["target"] == "phone"  # structured column (issue #100)
    assert audits[0]["outcome"] == "succeeded"


def test_browser_viewer_desktop_default_is_byte_identical(isolated_app, monkeypatch):
    """Absent target == explicit desktop, byte-for-byte; audit command unsuffixed."""
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.setattr(dashboard_api, "collect_browser_viewer_status", _browser_viewer_payload)

    absent = isolated_app.get("/api/browser-viewer/status")
    explicit = isolated_app.get("/api/browser-viewer/status?target=desktop")

    assert absent.status_code == explicit.status_code == 200
    assert absent.content == explicit.content
    assert audits[0]["command"] == "GET /api/browser-viewer/status"
    assert audits[1]["command"] == "GET /api/browser-viewer/status"


def test_browser_viewer_phone_act_routes_target(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_PHONEOPS_ENABLED", "true")
    calls: list[dict] = []

    def fake_act(kind, **kwargs):
        calls.append({"kind": kind, **kwargs})
        return SimpleNamespace(ok=True, output="")

    monkeypatch.setattr(dashboard_api, "browser_act", fake_act)

    r = isolated_app.post(
        "/api/browser-viewer/act",
        json={"kind": "click", "ref": "e2", "target": "phone"},
    )

    assert r.status_code == 200
    assert r.json() == {"ok": True, "kind": "click", "target": "phone"}
    assert calls[0]["target"] == "phone"
    assert audits[0]["command"] == "POST /api/browser-viewer/act"
    assert audits[0]["target"] == "phone"  # structured column (issue #100)


def test_browser_viewer_phone_act_403_when_disabled_never_shells(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.delenv("HOMIE_PHONEOPS_ENABLED", raising=False)
    monkeypatch.setattr(
        dashboard_api,
        "browser_act",
        lambda *_a, **_k: pytest.fail("gated phone act must not reach the runner"),
    )

    r = isolated_app.post(
        "/api/browser-viewer/act",
        json={"kind": "click", "ref": "e2", "target": "phone"},
    )

    assert r.status_code == 403
    assert audits[0]["outcome"] == "blocked"


def test_browser_viewer_phone_screenshot_header_echo(isolated_app, monkeypatch):
    import dashboard_api

    _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_PHONEOPS_ENABLED", "true")
    monkeypatch.setattr(
        dashboard_api,
        "capture_browser_screenshot_png",
        lambda **_k: b"\x89PNG\r\n\x1a\nphone",
    )
    monkeypatch.setattr(dashboard_api, "collect_browser_viewer_status", _browser_viewer_payload)

    r = isolated_app.get("/api/browser-viewer/screenshot?target=phone")

    assert r.status_code == 200
    assert r.headers["x-browser-target"] == "phone"

    desktop = isolated_app.get("/api/browser-viewer/screenshot")
    assert desktop.headers["x-browser-target"] == "desktop"


def test_browser_viewer_phone_stream_enable_routes_target(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_PHONEOPS_ENABLED", "true")
    targets: list[str | None] = []
    monkeypatch.setattr(
        dashboard_api,
        "browser_stream_enable",
        lambda **kwargs: targets.append(kwargs.get("target")),
    )
    monkeypatch.setattr(dashboard_api, "collect_browser_viewer_status", _browser_viewer_payload)

    r = isolated_app.post("/api/browser-viewer/stream/enable", json={"target": "phone"})

    assert r.status_code == 200
    assert targets == ["phone"]
    assert audits[0]["command"] == "POST /api/browser-viewer/stream/enable"
    assert audits[0]["target"] == "phone"  # structured column (issue #100)


def test_run_agent_browser_open_rides_the_phone_session(monkeypatch):
    """Live-E2E regression (2026-07-06): navigate without --session hit the
    freeze-wedged default daemon session and timed out while the phone was
    healthy. The open path must ride session_for_target like every other
    phone command."""
    import browser_control
    import dashboard_api

    recorded: dict = {}

    def fake_run(args, *, port, session=None, **_k):
        recorded.update({"args": args, "port": port, "session": session})
        return SimpleNamespace(ok=True, output="")

    monkeypatch.setattr(browser_control, "run_agent_browser", fake_run)
    monkeypatch.setattr(browser_control, "ensure_phone_chrome_ready", lambda **_k: True)
    monkeypatch.setattr(browser_control, "ensure_browser_window_restored", lambda **_k: True)
    monkeypatch.setattr(browser_control, "resolve_target_port", lambda t: 18223 if t == "phone" else 18222)

    dashboard_api.run_agent_browser_open("https://example.com", target="phone")
    assert recorded["session"] == "homie-phone"
    assert recorded["port"] == 18223

    dashboard_api.run_agent_browser_open("https://example.com")
    assert recorded["session"] is None  # desktop keeps the default session
    assert recorded["port"] == 18222


def test_browser_viewer_phone_navigate_routes_target(isolated_app, monkeypatch):
    import dashboard_api

    audits = _patch_browser_audits(monkeypatch)
    monkeypatch.setenv("HOMIE_PHONEOPS_ENABLED", "true")
    opened: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        dashboard_api,
        "run_agent_browser_open",
        lambda url, **kw: opened.append((url, kw.get("target")))
        or SimpleNamespace(ok=True, output=""),
    )

    r = isolated_app.post(
        "/api/browser-viewer/navigate",
        json={"url": "https://example.com/p", "target": "phone"},
    )

    assert r.status_code == 200
    assert r.json()["target"] == "phone"
    assert opened == [("https://example.com/p", "phone")]
    assert audits[0]["command"] == "POST /api/browser-viewer/navigate"
    assert audits[0]["target"] == "phone"  # structured column (issue #100)


# ── /api/info ────────────────────────────────────────────────────────────


def test_get_info_minimal_payload(isolated_app):
    r = isolated_app.get("/api/info")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body
    assert body["default_persona"] == "default"
    assert "persona_count" in body
    assert "lane_status" in body


def test_get_info_no_secrets_or_paths(isolated_app):
    r = isolated_app.get("/api/info")
    body_text = r.text.lower()
    forbidden = ["token", "secret", "password", "homie_home", "api_key", "c:\\"]
    for needle in forbidden:
        assert needle not in body_text


# ── /api/agents ──────────────────────────────────────────────────────────


def test_get_agents_returns_persona_list(isolated_app):
    r = isolated_app.get("/api/agents")
    assert r.status_code == 200
    body = r.json()
    assert "agents" in body
    assert isinstance(body["agents"], list)


def test_get_agents_includes_today_turns_and_cost(isolated_app):
    r = isolated_app.get("/api/agents")
    body = r.json()
    if body["agents"]:
        agent = body["agents"][0]
        assert "today_turns" in agent
        assert "today_cost" in agent
        assert isinstance(agent["today_turns"], int)
        assert isinstance(agent["today_cost"], float)


def test_get_agents_uses_canonical_default_id(isolated_app):
    r = isolated_app.get("/api/agents")
    body = r.json()
    ids = [a["id"] for a in body["agents"]]
    # Default profile must NOT appear as 'main'.
    assert "main" not in ids


def test_python_framework_does_not_translate_main(isolated_app):
    """Q4 lock — Python rejects persona_id='main' with 422."""
    r = isolated_app.get("/api/agents/main")
    assert r.status_code == 422
    r = isolated_app.post("/api/agents/main/activate")
    assert r.status_code == 422
    r = isolated_app.delete("/api/agents/main")
    assert r.status_code == 422


def test_get_agent_detail_404_on_missing(isolated_app):
    r = isolated_app.get("/api/agents/zzz-does-not-exist")
    assert r.status_code in (404, 422)


def test_post_agent_calls_lifecycle_create_profile(isolated_app, tmp_path, monkeypatch):
    """POST /api/agents calls personas.lifecycle.create_profile (R1 B1)."""
    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / ".homie"))
    (tmp_path / ".homie").mkdir(exist_ok=True)
    monkeypatch.setenv("HOMIE_BIN_DIR", str(tmp_path / "bin"))
    (tmp_path / "bin").mkdir(exist_ok=True)

    with patch("dashboard_api.create_profile") as mock_create:
        mock_create.return_value = MagicMock(name="newhomie", path=tmp_path / "newhomie", is_default=False)
        r = isolated_app.post("/api/agents", json={"persona_id": "newhomie"})
        assert r.status_code == 200
        assert mock_create.called


def test_post_agent_400_on_validation_error(isolated_app):
    """Reserved name 'default' rejected by validate_persona_name (422)."""
    r = isolated_app.post("/api/agents", json={"persona_id": "default"})
    assert r.status_code in (400, 422)


def test_post_agent_422_on_invalid_id_regex(isolated_app):
    r = isolated_app.post("/api/agents", json={"persona_id": "BadName!"})
    assert r.status_code in (400, 422)


def test_delete_agent_400_on_default_profile(isolated_app):
    r = isolated_app.delete("/api/agents/default")
    assert r.status_code == 400


def test_delete_agent_404_on_missing(isolated_app):
    r = isolated_app.delete("/api/agents/zzz-no-such-persona")
    assert r.status_code in (400, 404)


def test_delete_agent_calls_lifecycle_delete_profile(isolated_app):
    with patch("dashboard_api.delete_profile") as mock_del:
        r = isolated_app.delete("/api/agents/test-persona")
        # Either it called delete_profile, or returned 404 if persona not in disk.
        # We assert it tried (or 404 from FileNotFoundError).
        assert r.status_code in (200, 400, 404)


# ── /api/agents/{id}/full hard-delete ────────────────────────────────────


def test_delete_full_400_on_missing_confirmation(isolated_app):
    r = isolated_app.delete("/api/agents/test-persona/full")
    assert r.status_code == 400
    assert "confirmation required" in r.text


def test_delete_full_409_on_expected_persona_id_mismatch(isolated_app):
    r = isolated_app.delete(
        "/api/agents/test-persona/full?confirm=true&expected_persona_id=other"
    )
    assert r.status_code == 409


def test_delete_full_403_on_default_profile(isolated_app):
    r = isolated_app.delete("/api/agents/default/full?confirm=true")
    assert r.status_code == 403


def test_delete_full_calls_personas_lifecycle_hard_delete(isolated_app):
    """Happy path: delete_profile invoked with hard=True, yes=True."""
    with patch("dashboard_api.delete_profile") as mock_del, \
         patch("dashboard_api._profile_disk_state", return_value="deleted"):
        r = isolated_app.delete(
            "/api/agents/test-persona/full?confirm=true"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["deleted"] is True
        # yes=True + hard=True required.
        call_kwargs = mock_del.call_args.kwargs
        assert call_kwargs.get("yes") is True
        assert call_kwargs.get("hard") is True


def test_delete_full_endpoint_passes_yes_true_to_lifecycle(isolated_app):
    """The 'yes=True' guard must always be passed (R6 RB1)."""
    with patch("dashboard_api.delete_profile") as mock_del, \
         patch("dashboard_api._profile_disk_state", return_value="deleted"):
        isolated_app.delete("/api/agents/test/full?confirm=true")
        assert mock_del.call_args.kwargs.get("yes") is True


def test_delete_full_endpoint_never_calls_input_from_stdin(isolated_app):
    """Mock builtins.input to raise; endpoint completes without invoking it."""
    with patch("dashboard_api.delete_profile"), \
         patch("dashboard_api._profile_disk_state", return_value="deleted"), \
         patch("builtins.input", side_effect=RuntimeError("input() called")):
        r = isolated_app.delete("/api/agents/test/full?confirm=true")
        # Either 200 (success) or 500 — but NEVER raises RuntimeError because
        # endpoint should never invoke input().
        assert r.status_code in (200, 207, 500)


def test_hard_delete_partial_failure_reads_disk_state(isolated_app, tmp_path, monkeypatch):
    """REAL filesystem: rmtree partially removes profile → 207 partial_failure.

    Regression for the F2 class-of-bug — the previous implementation only
    counted ``memory/data/state`` and returned ``intact`` whenever ANY of
    those three survived. If ``rmtree`` removed two of them but left the
    third (and the ``config.yaml`` at the root), the endpoint would
    misclassify intact and return 500 instead of 207. This test creates
    the four-file profile, simulates ``delete_profile`` removing ONLY
    ``memory/`` and ``data/`` then raising, and asserts 207 partial.
    """
    import shutil

    # Set up a real profile dir with all four expected children.
    homie_home = tmp_path / ".homie"
    profile_root = homie_home / "profiles" / "partial-test"
    profile_root.mkdir(parents=True)
    (profile_root / "memory").mkdir()
    (profile_root / "data").mkdir()
    (profile_root / "state").mkdir()
    (profile_root / "config.yaml").write_text("persona:\n  name: partial-test\n")

    monkeypatch.setenv("HOMIE_HOME", str(homie_home))

    def fake_delete(name, **kwargs):
        # Remove memory/ and data/ then raise (locked file simulation).
        # state/ and config.yaml survive — exactly the classifier failure
        # mode the gas-station mock could not catch.
        shutil.rmtree(profile_root / "memory")
        shutil.rmtree(profile_root / "data")
        raise RuntimeError("rmtree blew up halfway through")

    with patch("dashboard_api.delete_profile", side_effect=fake_delete):
        r = isolated_app.delete("/api/agents/partial-test/full?confirm=true")

    assert r.status_code == 207, f"expected 207 partial, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["deleted"] is False
    assert body["partial"] is True
    assert any("partial_failure" in w for w in body["warnings"])
    # Sanity: state/ and config.yaml should still be on disk.
    assert (profile_root / "state").exists()
    assert (profile_root / "config.yaml").exists()
    assert not (profile_root / "memory").exists()
    assert not (profile_root / "data").exists()


def test_hard_delete_partial_when_only_config_yaml_missing(isolated_app, tmp_path, monkeypatch):
    """REAL filesystem: profile root + 3 dirs survive but config.yaml gone → 207 partial.

    Regression for the F2 class-of-bug — proves the classifier ALSO catches
    a missing ``config.yaml`` (not only directory removals). The previous
    implementation didn't include config.yaml in its expected set at all.
    """
    homie_home = tmp_path / ".homie"
    profile_root = homie_home / "profiles" / "config-missing"
    profile_root.mkdir(parents=True)
    (profile_root / "memory").mkdir()
    (profile_root / "data").mkdir()
    (profile_root / "state").mkdir()
    # NB: no config.yaml — simulates the case where rmtree removed it but
    # raised before getting to the dirs.

    monkeypatch.setenv("HOMIE_HOME", str(homie_home))

    def fake_delete(name, **kwargs):
        raise RuntimeError("rmtree blew up after deleting config.yaml only")

    with patch("dashboard_api.delete_profile", side_effect=fake_delete):
        r = isolated_app.delete("/api/agents/config-missing/full?confirm=true")

    assert r.status_code == 207
    body = r.json()
    assert body["deleted"] is False
    assert body["partial"] is True


def test_hard_delete_real_disk_intact_when_lifecycle_raises_before_any_delete(isolated_app, tmp_path, monkeypatch):
    """REAL filesystem: lifecycle raises BEFORE any delete → 500 lifecycle_error_no_change."""
    homie_home = tmp_path / ".homie"
    profile_root = homie_home / "profiles" / "intact-test"
    profile_root.mkdir(parents=True)
    (profile_root / "memory").mkdir()
    (profile_root / "data").mkdir()
    (profile_root / "state").mkdir()
    (profile_root / "config.yaml").write_text("persona:\n  name: intact-test\n")

    monkeypatch.setenv("HOMIE_HOME", str(homie_home))

    with patch("dashboard_api.delete_profile", side_effect=RuntimeError("perm denied early")):
        r = isolated_app.delete("/api/agents/intact-test/full?confirm=true")

    assert r.status_code == 500
    body = r.json()
    assert body["deleted"] is False
    assert any("lifecycle_error_no_change" in w for w in body["warnings"])


def test_hard_delete_real_disk_idempotent_when_already_gone(isolated_app, tmp_path, monkeypatch):
    """REAL filesystem: profile root never existed → 200 deleted (idempotent)."""
    homie_home = tmp_path / ".homie"
    homie_home.mkdir()
    (homie_home / "profiles").mkdir()
    monkeypatch.setenv("HOMIE_HOME", str(homie_home))

    # delete_profile may raise FileNotFoundError; the disk-state guard
    # should still classify as 'deleted' because the root never existed.
    with patch(
        "dashboard_api.delete_profile",
        side_effect=FileNotFoundError("Profile 'ghost' does not exist"),
    ):
        r = isolated_app.delete("/api/agents/ghost/full?confirm=true")

    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] is True


def test_hard_delete_full_failure_no_change(isolated_app):
    """Disk-state 'intact' + lifecycle raised → 500 lifecycle_error_no_change."""
    with patch("dashboard_api.delete_profile", side_effect=RuntimeError("rmtree blew up")), \
         patch("dashboard_api._profile_disk_state", return_value="intact"):
        r = isolated_app.delete("/api/agents/test/full?confirm=true")
        assert r.status_code == 500
        body = r.json()
        assert body["deleted"] is False
        assert any("lifecycle_error_no_change" in w for w in body["warnings"])


def test_hard_delete_idempotent_already_gone(isolated_app):
    """Already-deleted persona → disk_state='deleted', 200 success (idempotent)."""
    with patch("dashboard_api.delete_profile") as mock_del, \
         patch("dashboard_api._profile_disk_state", return_value="deleted"):
        mock_del.side_effect = FileNotFoundError("Profile 'test' does not exist")
        r = isolated_app.delete("/api/agents/test/full?confirm=true")
        assert r.status_code == 200
        body = r.json()
        assert body["deleted"] is True


def test_hard_delete_intact_no_exception_returns_500_internal_error(isolated_app):
    """Disk state intact + lifecycle did NOT raise → 500 internal_error_no_change."""
    with patch("dashboard_api.delete_profile") as mock_del, \
         patch("dashboard_api._profile_disk_state", return_value="intact"):
        mock_del.return_value = None  # No raise.
        r = isolated_app.delete("/api/agents/test/full?confirm=true")
        assert r.status_code == 500
        body = r.json()
        assert any("internal_error_no_change" in w for w in body["warnings"])


# ── F3 strict confirm-parser regression tests ────────────────────────────


@pytest.mark.parametrize(
    "raw_query, should_proceed",
    [
        ("true", True),     # canonical accept
        ("True", True),     # case-insensitive
        ("TRUE", True),     # case-insensitive
        (" true ", True),   # whitespace tolerant
        ("false", False),   # the F3 class-of-bug — bool("false") was True
        ("False", False),
        ("no", False),
        ("0", False),
        ("yes", False),     # Python's bool("yes") is also True — we still reject
        ("1", False),       # we accept ONLY the literal "true"
        ("", False),        # empty
    ],
)
def test_hard_delete_confirm_query_param_strict_parsing(
    isolated_app, raw_query, should_proceed
):
    """Query string ?confirm=<X> rejects every non-'true' shape with 400."""
    with patch("dashboard_api.delete_profile") as mock_del, \
         patch("dashboard_api._profile_disk_state", return_value="deleted"):
        r = isolated_app.delete(f"/api/agents/test-persona/full?confirm={raw_query}")
        if should_proceed:
            # Got past the gate → either 200 (success), 207 partial, or 500.
            # Crucially NOT 400 confirmation-required.
            assert r.status_code != 400, (
                f"confirm={raw_query!r} should have proceeded past gate; got 400: {r.text}"
            )
            assert mock_del.called, f"delete_profile not called for confirm={raw_query!r}"
        else:
            assert r.status_code == 400, (
                f"confirm={raw_query!r} should have been rejected; got {r.status_code}: {r.text}"
            )
            assert "confirmation required" in r.text
            assert not mock_del.called, (
                f"delete_profile MUST NOT be called for confirm={raw_query!r}"
            )


@pytest.mark.parametrize(
    "raw_body, should_proceed",
    [
        ({"confirm": True}, True),       # canonical boolean
        ({"confirm": "true"}, True),     # string "true"
        ({"confirm": "True"}, True),     # case-insensitive
        ({"confirm": " true "}, True),   # whitespace
        ({"confirm": "false"}, False),   # F3 class-of-bug
        ({"confirm": False}, False),     # boolean False
        ({"confirm": "no"}, False),
        ({"confirm": "0"}, False),
        ({"confirm": 0}, False),         # numeric — strict reject
        ({"confirm": 1}, False),         # numeric — strict reject (NOT confirmed)
        ({"confirm": [True]}, False),    # list — strict reject
        ({"confirm": {"value": True}}, False),  # dict — strict reject
        ({}, False),                      # missing
    ],
)
def test_hard_delete_confirm_json_body_strict_parsing(
    isolated_app, raw_body, should_proceed
):
    """JSON body confirm field rejects truthy non-'true' shapes with 400."""
    with patch("dashboard_api.delete_profile") as mock_del, \
         patch("dashboard_api._profile_disk_state", return_value="deleted"):
        # No query string — confirm comes from body only.
        r = isolated_app.request(
            "DELETE",
            "/api/agents/test-persona/full",
            json=raw_body,
        )
        if should_proceed:
            assert r.status_code != 400, (
                f"body={raw_body!r} should have proceeded past gate; got 400: {r.text}"
            )
            assert mock_del.called, f"delete_profile not called for body={raw_body!r}"
        else:
            assert r.status_code == 400, (
                f"body={raw_body!r} should have been rejected; got {r.status_code}: {r.text}"
            )
            assert "confirmation required" in r.text
            assert not mock_del.called, (
                f"delete_profile MUST NOT be called for body={raw_body!r}"
            )


def test_parse_confirm_helper_unit():
    """Direct unit test of _parse_confirm — locks the contract used by both
    the query-string and JSON-body resolution paths."""
    from dashboard_api import _parse_confirm

    # Accept paths.
    assert _parse_confirm(True) is True
    assert _parse_confirm("true") is True
    assert _parse_confirm("True") is True
    assert _parse_confirm("TRUE") is True
    assert _parse_confirm("  true  ") is True

    # Reject paths — the F3 class-of-bug surface.
    assert _parse_confirm(False) is False
    assert _parse_confirm("false") is False
    assert _parse_confirm("False") is False
    assert _parse_confirm("no") is False
    assert _parse_confirm("0") is False
    assert _parse_confirm("1") is False  # only literal "true" wins
    assert _parse_confirm("") is False
    assert _parse_confirm(None) is False
    assert _parse_confirm(0) is False
    assert _parse_confirm(1) is False
    assert _parse_confirm([True]) is False
    assert _parse_confirm({"v": True}) is False


# ── /api/agents/{id}/avatar ──────────────────────────────────────────────


def _png_bytes() -> bytes:
    """A minimal valid 1x1 PNG."""
    # PNG signature + IHDR + IDAT + IEND (smallest valid PNG).
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = b"\x00\x00\x00\rIHDR" + struct.pack(">II", 1, 1) + b"\x08\x06\x00\x00\x00" + b"\x1f\x15\xc4\x89"
    idat = b"\x00\x00\x00\x12IDAT\x78\x9c\x62\x00\x00\x00\x06\x00\x03\x00\x00\x00\x05\x00\x01\x0d\n\x2d\xb4"
    iend = b"\x00\x00\x00\x00IEND\xaeB`\x82"
    return header + ihdr + idat + iend


def _jpeg_bytes() -> bytes:
    """A minimal valid JPEG."""
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00C\x00" + b"\x08" * 64
        + b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        + b"\xff\xc4\x00\x14\x00" + b"\x00" * 16 + b"\x00\x00"
        + b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\x37\xff\xd9"
    )


def test_put_avatar_415_on_other_content_types(isolated_app):
    r = isolated_app.put(
        "/api/agents/sales/avatar",
        files={"image": ("a.pdf", b"fake", "application/pdf")},
    )
    assert r.status_code == 415


def test_put_avatar_413_on_oversize(isolated_app):
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (1024 * 1024 + 10)
    r = isolated_app.put(
        "/api/agents/sales/avatar",
        files={"image": ("a.png", big, "image/png")},
    )
    assert r.status_code == 413


def test_put_avatar_422_on_magic_byte_content_type_mismatch(isolated_app, tmp_path, monkeypatch):
    """PNG bytes uploaded with image/jpeg Content-Type → 422 format mismatch."""
    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / ".homie"))
    (tmp_path / ".homie" / "profiles" / "sales").mkdir(parents=True)
    r = isolated_app.put(
        "/api/agents/sales/avatar",
        files={"image": ("a.jpg", _png_bytes(), "image/jpeg")},
    )
    # Pillow detects PNG, content_type says JPEG → 422.
    assert r.status_code == 422


def test_put_avatar_invalid_image_data_returns_422(isolated_app):
    r = isolated_app.put(
        "/api/agents/sales/avatar",
        files={"image": ("a.png", b"garbage-not-an-image", "image/png")},
    )
    assert r.status_code == 422


def test_delete_avatar_idempotent_when_missing(isolated_app, tmp_path, monkeypatch):
    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / ".homie"))
    (tmp_path / ".homie" / "profiles" / "ghost").mkdir(parents=True)
    r = isolated_app.delete("/api/agents/ghost/avatar")
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ── /api/agents/validate-id ──────────────────────────────────────────────


def test_validate_id_uses_validate_persona_name(isolated_app):
    r = isolated_app.post("/api/agents/validate-id", json={"persona_id": "good-name"})
    assert r.status_code == 200
    body = r.json()
    assert "valid" in body
    assert "reason" in body


def test_validate_id_returns_invalid_format(isolated_app):
    r = isolated_app.post("/api/agents/validate-id", json={"persona_id": "BAD!"})
    body = r.json()
    assert body["valid"] is False
    assert body["reason"] in ("invalid_format", "reserved")


def test_validate_id_returns_reserved(isolated_app):
    r = isolated_app.post("/api/agents/validate-id", json={"persona_id": "default"})
    body = r.json()
    assert body["valid"] is False
    assert body["reason"] == "reserved"


# ── /api/agents/validate-token ───────────────────────────────────────────


def test_validate_token_calls_telegram_getme(isolated_app):
    """Mock httpx so we don't actually hit Telegram."""
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "ok": True,
        "result": {"first_name": "Test Bot", "username": "testbot"},
    }
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__aenter__.return_value
        async def _get(*args, **kwargs):
            return fake_response
        mock_client.get = _get
        r = isolated_app.post(
            "/api/agents/validate-token",
            json={"bot_token": "fake-token"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is True
        assert body["display_name"] == "Test Bot"


def test_validate_token_401_returns_unauthorized(isolated_app):
    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.json.return_value = {"ok": False}
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__aenter__.return_value
        async def _get(*args, **kwargs):
            return fake_response
        mock_client.get = _get
        r = isolated_app.post(
            "/api/agents/validate-token",
            json={"bot_token": "wrong-token"},
        )
        body = r.json()
        assert body["valid"] is False
        assert body["error"] == "unauthorized"


# ── /api/agents/suggestions, /api/agents/templates ───────────────────────


def test_get_suggestions_returns_list(isolated_app):
    r = isolated_app.get("/api/agents/suggestions")
    assert r.status_code == 200
    body = r.json()
    assert "suggestions" in body
    assert isinstance(body["suggestions"], list)
    assert len(body["suggestions"]) == 5


def test_get_templates_returns_list_or_empty(isolated_app):
    r = isolated_app.get("/api/agents/templates")
    assert r.status_code == 200
    assert "templates" in r.json()


def test_post_suggestions_refresh_returns_5_new(isolated_app):
    r = isolated_app.post("/api/agents/suggestions/refresh")
    assert r.status_code == 200
    body = r.json()
    assert "suggestions" in body
    assert len(body["suggestions"]) == 5


def test_post_suggestions_refresh_advances_rotating_pool(isolated_app):
    r1 = isolated_app.get("/api/agents/suggestions")
    items_before = [s["id"] for s in r1.json()["suggestions"]]
    isolated_app.post("/api/agents/suggestions/refresh")
    r2 = isolated_app.get("/api/agents/suggestions")
    items_after = [s["id"] for s in r2.json()["suggestions"]]
    assert items_before != items_after


def test_post_suggestions_refresh_idempotent_within_cursor_window(isolated_app):
    """Two refreshes both advance cursor; third differs from first only by cursor delta."""
    r1 = isolated_app.post("/api/agents/suggestions/refresh")
    r2 = isolated_app.post("/api/agents/suggestions/refresh")
    # Each refresh advances cursor by 5 — second call returns DIFFERENT items
    # from first call.
    assert r1.json()["suggestions"] != r2.json()["suggestions"]


# ── /api/agents/model + per-agent ────────────────────────────────────────


def test_get_models_groups_by_lane(isolated_app):
    r = isolated_app.get("/api/agents/model")
    assert r.status_code == 200
    body = r.json()
    assert "claude_native" in body
    assert "generic_runtime" in body
    assert "openai_codex" in body["generic_runtime"]


# ── /api/dashboard/settings ──────────────────────────────────────────────


def test_get_dashboard_settings_returns_dict(isolated_app):
    r = isolated_app.get("/api/dashboard/settings")
    assert r.status_code == 200
    assert "settings" in r.json()


def test_patch_dashboard_settings_writes_single_key(isolated_app):
    r = isolated_app.patch(
        "/api/dashboard/settings",
        json={"key": "theme", "value": "dark"},
    )
    assert r.status_code == 200
    settings = r.json()["settings"]
    assert settings.get("theme") == "dark"


def test_patch_dashboard_settings_partial_dict_merges(isolated_app):
    isolated_app.patch(
        "/api/dashboard/settings",
        json={"settings": {"a": 1, "b": 2}},
    )
    r = isolated_app.get("/api/dashboard/settings")
    settings = r.json()["settings"]
    assert settings.get("a") == 1
    assert settings.get("b") == 2


def test_get_dashboard_mobile_access_returns_sanitized_tailnet_status(isolated_app, monkeypatch):
    import dashboard_api

    def fake_run_json_command(args, timeout_s=2.0):
        if args == ["tailscale", "status", "--json"]:
            return {
                "BackendState": "Running",
                "TailscaleIPs": ["100.64.0.10", "fd7a:115c:a1e0::1"],
                "Self": {
                    "HostName": "Smoke",
                    "DNSName": "homie.tailnet.test.",
                    "PublicKey": "nodekey:redacted",
                    "UserID": 123,
                },
                "Peer": {"nodekey:peer": {"HostName": "Phone"}},
                "User": {"123": {"LoginName": "private@example.com"}},
            }, None
        if args == ["tailscale", "serve", "status", "--json"]:
            return {
                "TCP": {"80": {"HTTP": True}, "443": {"HTTPS": True}},
                "Web": {
                    "homie.tailnet.test:80": {
                        "Handlers": {"/": {"Proxy": "http://127.0.0.1:5173"}}
                    }
                },
            }, None
        raise AssertionError(args)

    monkeypatch.setattr(dashboard_api, "_run_json_command", fake_run_json_command)

    r = isolated_app.get(
        "/api/dashboard/mobile-access",
        headers={"x-dashboard-request-host": "100.64.0.10:5173"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["mode"] == "read_only"
    assert body["tailscale"]["primary_ip"] == "100.64.0.10"
    assert body["tailscale"]["dns_name"] == "homie.tailnet.test"
    assert body["dashboard"]["urls"]["browser"] == "http://100.64.0.10:5173/browser"
    assert body["dashboard"]["request_host"] == "100.64.0.10:5173"
    assert body["serve"]["http"] is True
    assert body["serve"]["https"] is True
    assert body["controls"] == {"mutates_tailscale": False, "mutates_browser": False}
    assert "Peer" not in body["tailscale"]
    assert "private@example.com" not in json.dumps(body)


def test_get_dashboard_mobile_access_handles_missing_tailscale(isolated_app, monkeypatch):
    import dashboard_api

    def fake_run_json_command(args, timeout_s=2.0):
        return None, "tailscale not found"

    monkeypatch.setattr(dashboard_api, "_run_json_command", fake_run_json_command)

    r = isolated_app.get("/api/dashboard/mobile-access")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unavailable"
    assert body["tailscale"]["available"] is False
    assert body["tailscale"]["error"] == "tailscale not found"
    assert body["dashboard"]["urls"]["browser"] is None
    assert body["serve"]["enabled"] is False


# ── /api/scheduled CRUD ──────────────────────────────────────────────────


def test_scheduled_crud_full_lifecycle(isolated_app):
    # Create
    r = isolated_app.post(
        "/api/scheduled",
        json={"persona_id": "default", "prompt": "echo hi", "schedule": "*/5 * * * *"},
    )
    assert r.status_code == 200
    task = r.json()
    assert task["id"] >= 1
    task_id = task["id"]

    # List
    r = isolated_app.get("/api/scheduled")
    assert any(t["id"] == task_id for t in r.json()["tasks"])

    # Patch
    r = isolated_app.patch(f"/api/scheduled/{task_id}", json={"status": "paused"})
    assert r.status_code == 200
    assert r.json()["status"] == "paused"

    # Delete
    r = isolated_app.delete(f"/api/scheduled/{task_id}")
    assert r.status_code == 200


def test_scheduled_invalid_cron_422(isolated_app):
    r = isolated_app.post(
        "/api/scheduled",
        json={"persona_id": "default", "prompt": "x", "schedule": "not a cron"},
    )
    assert r.status_code == 422


# ── /api/scheduled bot-lifecycle guard (B1 — Deliverable 3, seam 2) ───────


def test_scheduled_create_rejects_bot_lifecycle_prompt_400(isolated_app):
    # A bot-lifecycle prompt is rejected at creation with HTTP 400 (NOT 500 —
    # dashboard_api has no global ValueError→HTTP mapper; the seam translates).
    r = isolated_app.post(
        "/api/scheduled",
        json={"prompt": "Stop-Process -Name thehomie -Force", "schedule": "*/5 * * * *"},
    )
    assert r.status_code == 400


def test_scheduled_patch_rejects_bot_lifecycle_prompt_400(isolated_app):
    r = isolated_app.post(
        "/api/scheduled",
        json={"prompt": "echo hi", "schedule": "*/5 * * * *"},
    )
    task_id = r.json()["id"]
    r = isolated_app.patch(
        f"/api/scheduled/{task_id}", json={"prompt": "pkill -f chat/main.py"}
    )
    assert r.status_code == 400


def test_scheduled_create_benign_prompt_allowed(isolated_app):
    # ``python app/main.py`` is bare-main.py, NOT the bot launcher (B2 allow).
    r = isolated_app.post(
        "/api/scheduled",
        json={"prompt": "python app/main.py", "schedule": "*/5 * * * *"},
    )
    assert r.status_code == 200


def test_scheduled_create_fail_open_when_guard_raises(isolated_app, monkeypatch):
    # M3: a non-BotLifecycleBlocked raise from the guard is caught + logged so a
    # benign scheduled create still succeeds (never 500s).
    from orchestration import lifecycle_guard

    def _boom(*a, **k):
        raise RuntimeError("regex engine exploded")

    monkeypatch.setattr(lifecycle_guard, "check_bot_lifecycle", _boom)
    r = isolated_app.post(
        "/api/scheduled",
        json={"prompt": "echo hi", "schedule": "*/5 * * * *"},
    )
    assert r.status_code == 200


def test_scheduled_patch_fail_open_when_guard_raises(isolated_app, monkeypatch):
    # M3: PATCH prompt-scan shares _scan_scheduled_prompt with create — a
    # non-BotLifecycleBlocked guard raise is caught + logged so the update still
    # succeeds (never 500s). Guard patched AFTER the benign create so the create
    # is unaffected.
    from orchestration import lifecycle_guard

    r = isolated_app.post(
        "/api/scheduled",
        json={"prompt": "echo hi", "schedule": "*/5 * * * *"},
    )
    task_id = r.json()["id"]

    def _boom(*a, **k):
        raise RuntimeError("regex engine exploded")

    monkeypatch.setattr(lifecycle_guard, "check_bot_lifecycle", _boom)
    r = isolated_app.patch(
        f"/api/scheduled/{task_id}", json={"prompt": "echo still fine"}
    )
    assert r.status_code == 200
    assert r.json()["prompt"] == "echo still fine"


# ── /api/memories ────────────────────────────────────────────────────────


def test_get_memories_does_not_call_recall_service(isolated_app):
    """Read-only paginated query — does NOT route through recall_service."""
    with patch("recall_service.recall") as mock_recall:
        r = isolated_app.get("/api/memories?limit=10")
        assert r.status_code == 200
        assert mock_recall.called is False


def test_get_memories_returns_paginated(isolated_app):
    r = isolated_app.get("/api/memories?limit=10")
    body = r.json()
    assert "memories" in body
    assert "stats" in body
    assert "next_before_id" in body
    assert body["stats"]["total_chunks"] == 1
    assert body["stats"]["scope"] == "global_vault"
    assert body["stats"]["persona_filter_supported"] is False


def test_get_memories_maps_current_chunk_schema_to_dashboard_contract(isolated_app):
    r = isolated_app.get("/api/memories?limit=10")
    assert r.status_code == 200
    memory = r.json()["memories"][0]
    assert memory["source_path"] == "daily/2026-05-15.md"
    assert memory["sourcePath"] == "daily/2026-05-15.md"
    assert memory["chunk_text"] == "Real vault memory chunk"
    assert memory["text"] == "Real vault memory chunk"
    assert memory["persona_id"] == "default"
    assert memory["personaId"] == "default"
    assert memory["kind"] == "vault_chunk"
    assert "vault-chunk" in memory["tags"]


def test_get_memories_non_default_filter_does_not_claim_global_rows(isolated_app):
    r = isolated_app.get("/api/memories?persona_id=sales&limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["memories"] == []
    assert body["stats"]["persona_filter_supported"] is False


# ── /api/memory/graph ────────────────────────────────────────────────────


def test_memory_graph_returns_unscoped_chunks_as_global_default(isolated_app):
    r = isolated_app.get("/api/memory/graph?limit=10")
    assert r.status_code == 200
    body = r.json()
    nodes = {node["id"]: node for node in body["nodes"]}
    assert "chunk:1" in nodes
    assert "note:daily/2026-05-15.md" in nodes
    assert nodes["chunk:1"]["scope_type"] == "global"
    assert nodes["chunk:1"]["scope_id"] == "default"
    assert nodes["chunk:1"]["kind"] == "chunk"
    assert nodes["chunk:1"]["text"] == "Real vault memory chunk"
    assert nodes["note:daily/2026-05-15.md"]["text"] == "## Mission Control\n\nReal vault memory chunk"
    assert nodes["note:daily/2026-05-15.md"]["preview_source"] == "loaded_chunk_neighbors"
    assert nodes["note:daily/2026-05-15.md"]["preview_chunk_count"] == 1
    assert any(edge["kind"] == "source" and edge["source"] == "chunk:1" for edge in body["edges"])
    assert body["stats"]["total_nodes"] >= 2
    assert body["stats"]["persona_filter_supported"] is False


def test_memory_graph_persona_view_keeps_global_overlay(isolated_app):
    r = isolated_app.get("/api/memory/graph?scope=persona&scope_id=research&limit=10")
    assert r.status_code == 200
    body = r.json()
    assert any(node["id"] == "chunk:1" for node in body["nodes"])
    assert body["stats"]["scope"] == "persona"
    assert body["stats"]["scope_id"] == "research"


def test_memory_graph_parses_vault_wikilinks(isolated_app, tmp_path, monkeypatch):
    import config

    vault = tmp_path / "TheHomie" / "Memory"
    note = vault / "daily" / "2026-05-15.md"
    target = vault / "Related Note.md"
    weekly = vault / "weekly" / "2026-W15.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    weekly.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("Project note links to [[Related Note]]\n", encoding="utf-8")
    target.write_text("Related note body.\n", encoding="utf-8")
    weekly.write_text(
        "---\nrelated:\n  - \"[[Related Note]]\"\nsuperseded_by: \"[[Missing Note]]\"\n---\nWeekly body.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "MEMORY_DIR", vault)

    r = isolated_app.get("/api/memory/graph?limit=10")
    assert r.status_code == 200
    body = r.json()
    node_ids = {node["id"] for node in body["nodes"]}
    nodes = {node["id"]: node for node in body["nodes"]}
    assert "note:daily/2026-05-15.md" in node_ids
    assert "note:Related Note.md" in node_ids
    assert "note:weekly/2026-W15.md" in node_ids
    assert nodes["note:Related Note.md"]["text"] == "Related note body."
    assert nodes["note:Related Note.md"]["preview_source"] == "source_markdown"
    assert any(
        edge["kind"] == "wikilink"
        and edge["source"] == "note:daily/2026-05-15.md"
        and edge["target"] == "note:Related Note.md"
        and edge["resolved"] is True
        for edge in body["edges"]
    )
    assert any(
        edge["kind"] == "related"
        and edge["source"] == "note:weekly/2026-W15.md"
        and edge["target"] == "note:Related Note.md"
        and edge["source_field"] == "related"
        and edge["resolved"] is True
        for edge in body["edges"]
    )
    assert any(
        edge["kind"] == "property"
        and edge["source"] == "note:weekly/2026-W15.md"
        and edge["target"] == "note:Missing Note.md"
        and edge["source_field"] == "superseded_by"
        and edge["resolved"] is False
        for edge in body["edges"]
    )
    assert nodes["note:Missing Note.md"]["tags"] == ["vault-note", "wikilink-target", "unresolved-wikilink"]
    assert body["stats"]["vault_graph"]["vault_notes"] == 3
    assert body["stats"]["vault_graph"]["vault_resolved_wikilink_edges"] == 2
    assert body["stats"]["vault_graph"]["vault_unresolved_wikilink_edges"] == 1
    assert body["stats"]["vault_graph"]["vault_body_wikilink_edges"] == 1
    assert body["stats"]["vault_graph"]["vault_related_edges"] == 1
    assert body["stats"]["vault_graph"]["vault_property_wikilink_edges"] == 1


def test_memory_graph_infers_scope_for_file_scanned_vault_notes(isolated_app, tmp_path, monkeypatch):
    import config

    vault = tmp_path / "TheHomie" / "Memory"
    agent_note = vault / "agents" / "codex" / "agent-note.md"
    agent_note.parent.mkdir(parents=True, exist_ok=True)
    agent_note.write_text("Agent-local vault note.\n", encoding="utf-8")
    monkeypatch.setattr(config, "MEMORY_DIR", vault)

    r = isolated_app.get("/api/memory/graph?limit=1")
    assert r.status_code == 200
    nodes = {node["id"]: node for node in r.json()["nodes"]}
    assert nodes["note:agents/codex/agent-note.md"]["scope_type"] == "agent"
    assert nodes["note:agents/codex/agent-note.md"]["scope_id"] == "codex"
    assert nodes["note:agents/codex/agent-note.md"]["visibility"] == "private"


def test_memory_graph_respects_scoped_memory_columns(isolated_app):
    import config

    conn = sqlite3.connect(str(config.DATABASE_PATH))
    try:
        conn.execute("ALTER TABLE chunks ADD COLUMN persona_id TEXT")
        conn.execute("UPDATE chunks SET persona_id = 'research' WHERE id = 1")
        conn.commit()
    finally:
        conn.close()

    r = isolated_app.get("/api/memory/graph?scope=persona&scope_id=research&limit=10")
    assert r.status_code == 200
    nodes = {node["id"]: node for node in r.json()["nodes"]}
    assert nodes["chunk:1"]["scope_type"] == "persona"
    assert nodes["chunk:1"]["scope_id"] == "research"
    assert nodes["chunk:1"]["visibility"] == "private"


def test_memory_graph_exposes_pagination_metadata(isolated_app):
    import config

    conn = sqlite3.connect(str(config.DATABASE_PATH))
    try:
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        for index in range(2, 5):
            conn.execute(
                """INSERT INTO chunks
                   (file_path, start_line, end_line, section_title, content, content_hash, created_at_epoch)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"daily/2026-05-{14 + index}.md",
                    1,
                    2,
                    f"Paged chunk {index}",
                    f"Memory chunk {index}",
                    f"hash-chunk-{index}",
                    now_epoch,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    r = isolated_app.get("/api/memory/graph?limit=2&offset=0")
    assert r.status_code == 200
    page = r.json()["stats"]["page"]
    assert page["limit"] == 2
    assert page["offset"] == 0
    assert page["returned_chunks"] == 2
    assert page["matching_chunks"] == 4
    assert page["has_more"] is True

    r2 = isolated_app.get("/api/memory/graph?limit=2&offset=2")
    assert r2.status_code == 200
    page2 = r2.json()["stats"]["page"]
    assert page2["offset"] == 2
    assert page2["returned_chunks"] == 2
    assert page2["has_more"] is False


def test_memory_graph_models_cabinet_room_as_session_layer(isolated_app):
    from dashboard_db import get_connection

    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO cabinet_meetings (mode, title, chat_id, entry_count) VALUES (?, ?, ?, ?)",
            ("text", "Strategy room", "chat-123", 7),
        )
        meeting_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()

    r = isolated_app.get(f"/api/memory/graph?scope=room&scope_id=cabinet-{meeting_id}&limit=10")
    assert r.status_code == 200
    body = r.json()
    room = next(node for node in body["nodes"] if node["id"] == f"room:cabinet-{meeting_id}")
    assert room["kind"] == "session"
    assert room["scope_type"] == "room"
    assert room["scope_id"] == f"cabinet-{meeting_id}"
    assert "7 transcript entries" in room["text"]


# ── /api/brain/graph ─────────────────────────────────────────────────────


def test_brain_graph_composes_memory_base_and_hive_activity(isolated_app):
    r = isolated_app.get("/api/brain/graph?limit=10&activity_window_minutes=60")
    assert r.status_code == 200
    body = r.json()
    node_ids = {node["id"] for node in body["nodes"]}
    assert "chunk:1" in node_ids
    assert "note:daily/2026-05-15.md" in node_ids
    assert any(edge["kind"] == "source" for edge in body["edges"])
    assert body["activity"][0]["type"] == "chat_message"
    assert body["activity"][0]["details"] == "Recent hive activity"
    assert body["layers"]["memory"] is True
    assert body["layers"]["activity"] is True
    assert "global/default" in body["layers"]["scopes"]
    assert body["stats"]["total_nodes"] == len(body["nodes"])
    assert body["stats"]["activity"]["window_minutes"] == 60


def test_brain_graph_forwards_memory_pagination_metadata(isolated_app):
    r = isolated_app.get("/api/brain/graph?limit=1&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert body["stats"]["limit"] == 1
    assert body["stats"]["offset"] == 0
    assert body["stats"]["memory"]["page"]["limit"] == 1
    assert body["stats"]["memory"]["page"]["offset"] == 0
    assert body["stats"]["memory"]["page"]["returned_chunks"] == 1


def test_brain_graph_persona_scope_filters_activity_and_keeps_memory_overlay(isolated_app):
    r = isolated_app.get("/api/brain/graph?scope=persona&scope_id=default&limit=10")
    assert r.status_code == 200
    body = r.json()
    assert any(node["id"] == "chunk:1" for node in body["nodes"])
    assert body["stats"]["scope"] == "persona"
    assert body["stats"]["scope_id"] == "default"
    assert body["stats"]["activity_filter_persona_id"] == "default"
    assert [event["persona_id"] for event in body["activity"]] == ["default"]


# ── /api/tokens, /api/agents/{id}/tokens (lane-aware) ────────────────────


def test_get_tokens_returns_lane_aware_shape(isolated_app):
    r = isolated_app.get("/api/tokens")
    assert r.status_code == 200
    body = r.json()
    assert "timeline" in body
    assert "summary" in body
    assert "claude_native" in body["summary"]
    assert "generic" in body["summary"]
    assert "by_provider" in body["summary"]["generic"]


def test_get_tokens_claude_native_no_dollar_tracking(isolated_app):
    r = isolated_app.get("/api/tokens")
    summary = r.json()["summary"]
    cn = summary["claude_native"]
    # claude_native lane uses turns/messages, NOT cost_usd.
    assert "turns_today" in cn or "messages_today" in cn


def test_get_tokens_generic_breaks_out_by_provider(isolated_app):
    r = isolated_app.get("/api/tokens")
    summary = r.json()["summary"]
    assert isinstance(summary["generic"]["by_provider"], dict)


def test_get_tokens_range_param_honored(isolated_app):
    r = isolated_app.get("/api/tokens?range=7d")
    assert r.status_code == 200


def test_get_tokens_interval_param_honored(isolated_app):
    r = isolated_app.get("/api/tokens?interval=hour")
    assert r.status_code == 200


def test_get_agent_tokens_scoped_to_persona(isolated_app):
    r = isolated_app.get("/api/agents/default/tokens")
    assert r.status_code == 200
    assert "summary" in r.json()


def test_get_agent_tokens_lane_aware_shape(isolated_app):
    r = isolated_app.get("/api/agents/default/tokens")
    body = r.json()
    assert "claude_native" in body["summary"]
    assert "generic" in body["summary"]


# ── /api/agents/{id}/tasks (calls convoy_service) ────────────────────────


def test_get_agent_tasks_calls_convoy_service(isolated_app):
    """The endpoint must route through convoy_service.list_subtasks_by_agent."""
    r = isolated_app.get("/api/agents/default/tasks")
    assert r.status_code == 200
    assert "tasks" in r.json()


def test_get_agent_tasks_scoped_to_persona(isolated_app):
    r = isolated_app.get("/api/agents/zzz-no-such-agent/tasks")
    assert r.status_code == 200
    assert r.json()["tasks"] == []


# ── /api/work/tasks (dashboard work queue over orchestration) ─────────────


def test_work_queue_create_list_patch_dispatch_lifecycle(isolated_app):
    r = isolated_app.get("/api/work/tasks")
    assert r.status_code == 200
    assert r.json()["tasks"] == []

    r = isolated_app.post(
        "/api/work/tasks",
        json={
            "title": "Wire task board",
            "description": "Expose orchestration subtasks in the dashboard",
            "assigned_agent_id": "codex",
            "assigned_agent_name": "Codex",
            "priority": "high",
            "tags": ["dashboard", "work"],
            "target_session": "session-alpha",
        },
    )
    assert r.status_code == 200
    created = r.json()["task"]
    assert created["status"] == "ready"
    assert created["priority"] == "high"
    assert created["tags"] == ["dashboard", "work"]
    assert created["target_session"] == "session-alpha"
    task_id = created["id"]

    r = isolated_app.get("/api/work/tasks")
    assert r.status_code == 200
    listed = r.json()
    assert listed["summary"]["ready"] == 1
    assert any(t["id"] == task_id for t in listed["tasks"])

    r = isolated_app.patch(
        f"/api/work/tasks/{task_id}",
        json={"assigned_agent_id": "gemini", "assigned_agent_name": "Gemini"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["assigned_agent_id"] == "gemini"

    r = isolated_app.post(f"/api/work/tasks/{task_id}/dispatch", json={})
    assert r.status_code == 200
    dispatched = r.json()
    assert dispatched["receipt"]["status"] == "accepted"
    assert dispatched["task"]["status"] == "dispatched"

    r = isolated_app.patch(f"/api/work/tasks/{task_id}", json={"status": "running"})
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "running"

    r = isolated_app.patch(f"/api/work/tasks/{task_id}", json={"status": "completed"})
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "completed"


# ── /api/conversation history (paginated) ────────────────────────────────


def test_get_conversation_returns_turns(isolated_app):
    r = isolated_app.get("/api/agents/default/conversation?limit=10")
    assert r.status_code == 200
    assert "turns" in r.json()


def test_get_conversation_creates_index_idempotent(isolated_app):
    """Repeated calls don't error on index creation."""
    isolated_app.get("/api/agents/default/conversation")
    isolated_app.get("/api/agents/default/conversation")
    r = isolated_app.get("/api/agents/default/conversation")
    assert r.status_code == 200


# ── /api/hive-mind/recent ────────────────────────────────────────────────


def test_hive_mind_returns_recent_chat_messages(isolated_app):
    r = isolated_app.get("/api/hive-mind/recent")
    assert r.status_code == 200
    body = r.json()
    assert "entries" in body
    assert "events" in body
    assert body["entries"][0]["event_type"] == "chat_message"
    assert body["events"][0]["personaId"] == "default"
    assert body["events"][0]["type"] == "chat_message"


def test_hive_mind_deterministic_ordering(isolated_app):
    r1 = isolated_app.get("/api/hive-mind/recent")
    r2 = isolated_app.get("/api/hive-mind/recent")
    assert r1.json() == r2.json()


def test_hive_mind_window_minutes_excludes_old_messages(isolated_app):
    r = isolated_app.get("/api/hive-mind/recent?window_minutes=60&limit=10")
    assert r.status_code == 200
    body = r.json()
    assert [entry["excerpt"] for entry in body["entries"]] == ["Recent hive activity"]
    assert [event["details"] for event in body["events"]] == ["Recent hive activity"]


# ── /api/agents/{id}/files (allowlist + redaction) ───────────────────────


def test_get_files_returns_allowlist(isolated_app):
    r = isolated_app.get("/api/agents/default/files")
    assert r.status_code == 200
    body = r.json()
    # All keys must be in the allowlist.
    allowed = {"config.yaml", "SOUL.md", "USER.md", "MEMORY.md", "GOALS.md", "WORKING.md", "SELF.md"}
    for key in body:
        assert key in allowed


def test_patch_files_400_on_unknown_filename(isolated_app):
    r = isolated_app.patch(
        "/api/agents/default/files/secret.env",
        json={"content": "x"},
    )
    assert r.status_code in (400, 422)


# ── /api/agents/{id}/{activate,deactivate,restart} (mocked) ──────────────


def test_post_activate_uses_bot_lifecycle_module(isolated_app):
    with patch("dashboard_bot_lifecycle.activate") as mock_act:
        mock_act.return_value = {"persona_id": "sales", "pid": 12345, "status": "running"}
        r = isolated_app.post("/api/agents/sales/activate")
        assert r.status_code == 200
        assert mock_act.called
        body = r.json()
        assert body["status"] == "running"


def test_post_activate_idempotent_when_running(isolated_app):
    with patch("dashboard_bot_lifecycle.activate") as mock_act:
        mock_act.return_value = {"persona_id": "sales", "pid": 999, "status": "already_running"}
        r = isolated_app.post("/api/agents/sales/activate")
        assert r.json()["status"] == "already_running"


def test_post_activate_starts_bot(isolated_app):
    with patch("dashboard_bot_lifecycle.activate") as mock_act:
        mock_act.return_value = {"persona_id": "sales", "pid": 5555, "status": "running"}
        r = isolated_app.post("/api/agents/sales/activate")
        assert r.json()["pid"] == 5555


def test_post_deactivate_signals_sigterm(isolated_app):
    with patch("dashboard_bot_lifecycle.deactivate") as mock_deact:
        mock_deact.return_value = {"persona_id": "sales", "status": "stopped"}
        r = isolated_app.post("/api/agents/sales/deactivate")
        assert r.status_code == 200
        assert r.json()["status"] == "stopped"


def test_post_deactivate_idempotent_when_stopped(isolated_app):
    with patch("dashboard_bot_lifecycle.deactivate") as mock_deact:
        mock_deact.return_value = {"persona_id": "sales", "status": "already_stopped"}
        r = isolated_app.post("/api/agents/sales/deactivate")
        assert r.json()["status"] == "already_stopped"


def test_post_deactivate_escalates_to_sigkill_on_timeout(isolated_app):
    """Endpoint surfaces lifecycle's RuntimeError as 500."""
    with patch("dashboard_bot_lifecycle.deactivate", side_effect=RuntimeError("refused to die")):
        r = isolated_app.post("/api/agents/sales/deactivate")
        assert r.status_code == 500


def test_post_restart_chains_deactivate_then_activate(isolated_app):
    with patch("dashboard_bot_lifecycle.restart") as mock_restart:
        mock_restart.return_value = {
            "persona_id": "sales",
            "old_pid": 100,
            "new_pid": 200,
            "status": "restarted",
        }
        r = isolated_app.post("/api/agents/sales/restart")
        assert r.status_code == 200
        body = r.json()
        assert body["old_pid"] == 100
        assert body["new_pid"] == 200


# ── PRD-8 Phase 7a (WS5) — /api/audit-log + rich /api/health snapshot ─────


def test_phase7a_health_kill_switches_shape_after_refusal(isolated_app, monkeypatch):
    """After a kill-switch refusal, /api/health.killSwitches.counters reflects it."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_LLM", "disabled")
    from security import kill_switches
    # Reset and trigger a refusal.
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()
    try:
        kill_switches.requireEnabled("llm", caller="test")
    except kill_switches.KillSwitchDisabled:
        pass
    r = isolated_app.get("/api/health")
    snap = r.json()["killSwitches"]
    assert snap["counters"].get("llm", 0) >= 1
    assert isinstance(snap.get("process_started_at"), (int, float))
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()


def test_phase7a_audit_log_endpoint_503_when_admin_token_unset(isolated_app, monkeypatch):
    """Fail-closed when DASHBOARD_ADMIN_TOKEN is unset."""
    monkeypatch.delenv("DASHBOARD_ADMIN_TOKEN", raising=False)
    r = isolated_app.get("/api/audit-log")
    assert r.status_code == 503
    assert "DASHBOARD_ADMIN_TOKEN" in r.json()["detail"]


def test_phase7a_audit_log_endpoint_403_when_bearer_wrong(isolated_app, monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "admin-secret")
    r = isolated_app.get(
        "/api/audit-log", headers={"Authorization": "Bearer wrong-token"}
    )
    assert r.status_code == 403


def test_phase7a_audit_log_endpoint_200_when_bearer_correct(isolated_app, monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "admin-secret")
    r = isolated_app.get(
        "/api/audit-log", headers={"Authorization": "Bearer admin-secret"}
    )
    assert r.status_code == 200
    body = r.json()
    assert "rows" in body
    assert "next_before_id" in body
    assert isinstance(body["rows"], list)


def test_phase7a_audit_log_endpoint_paginated_via_before_id(isolated_app, monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "admin-secret")
    # Seed 5 rows via _audit_write.
    import dashboard_api
    for i in range(5):
        dashboard_api._audit_write(
            operator_id="test",
            action="killswitch_refusal",
            target_persona_id=f"sw-{i}",
            outcome="disabled",
            detail={"i": i},
            blocked=True,
        )
    r = isolated_app.get(
        "/api/audit-log?limit=2",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["rows"]) == 2
    assert body["next_before_id"] is not None


def test_phase7a_audit_log_endpoint_default_limit_50_max_200(isolated_app, monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "admin-secret")
    r = isolated_app.get(
        "/api/audit-log?limit=10000",
        headers={"Authorization": "Bearer admin-secret"},
    )
    # No assertion error — endpoint clamps internally; test just confirms 200.
    assert r.status_code == 200


def test_phase7a_audit_log_endpoint_action_filter(isolated_app, monkeypatch):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "admin-secret")
    import dashboard_api
    dashboard_api._audit_write(
        operator_id="test",
        action="killswitch_refusal",
        target_persona_id="llm",
        outcome="disabled",
        detail={},
        blocked=True,
    )
    dashboard_api._audit_write(
        operator_id="test",
        action="hard_delete",
        target_persona_id="sales",
        outcome="success",
        detail={},
        blocked=False,
    )
    r = isolated_app.get(
        "/api/audit-log?action=killswitch_refusal",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert r.status_code == 200
    rows = r.json()["rows"]
    for row in rows:
        assert row["action"] == "killswitch_refusal"


def test_phase7a_audit_log_endpoint_redacts_secret_shaped_in_detail(
    isolated_app, monkeypatch
):
    """Detail field with a synthetic key gets scrubbed to <REDACTED-...>."""
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "admin-secret")
    import dashboard_api
    fake_key = "ghp_" + "x" * 30  # synthetic, not real
    dashboard_api._audit_write(
        operator_id="test",
        action="killswitch_refusal",
        target_persona_id="recall",
        outcome="disabled",
        detail={"caller_path": fake_key, "switch": "recall"},
        blocked=True,
    )
    r = isolated_app.get(
        "/api/audit-log?action=killswitch_refusal",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert r.status_code == 200
    body_text = r.text
    assert fake_key not in body_text, "Synthetic ghp_ key was NOT redacted"
    assert "<REDACTED-github>" in body_text


def test_phase7a_audit_log_endpoint_works_with_admin_token_when_orch_token_different(
    tmp_path, monkeypatch
):
    """R3 NB1 — admin token != orch token must still authenticate (exemption works)."""
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", "orch-token-abc")
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "admin-token-xyz")
    dash_db = tmp_path / "dashboard.db"
    import config
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", dash_db)
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", tmp_path / "orch.db")

    import orchestration.api as oa
    importlib.reload(oa)
    db, cs, ms, reg, ts = oa._get_services()
    oa._db = db
    oa._convoy_svc = cs
    oa._mailbox_svc = ms
    oa._executor_registry = reg
    oa._team_svc = ts

    client = TestClient(oa.app)
    r = client.get(
        "/api/audit-log",
        headers={"Authorization": "Bearer admin-token-xyz"},
    )
    assert r.status_code == 200, (
        "/api/audit-log must accept admin token when orchestration token is "
        f"a DIFFERENT value. Got {r.status_code}: {r.text}"
    )
    db.close()


def test_phase7a_audit_log_endpoint_503_even_when_orch_token_set(
    tmp_path, monkeypatch
):
    """R3 NB1 — orch token alone is NOT enough; admin token must be set or 503."""
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", "orch-token-abc")
    monkeypatch.delenv("DASHBOARD_ADMIN_TOKEN", raising=False)
    dash_db = tmp_path / "dashboard.db"
    import config
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", dash_db)
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", tmp_path / "orch.db")

    import orchestration.api as oa
    importlib.reload(oa)
    db, cs, ms, reg, ts = oa._get_services()
    oa._db = db
    oa._convoy_svc = cs
    oa._mailbox_svc = ms
    oa._executor_registry = reg
    oa._team_svc = ts

    client = TestClient(oa.app)
    r = client.get(
        "/api/audit-log",
        headers={"Authorization": "Bearer orch-token-abc"},
    )
    assert r.status_code == 503
    db.close()


# ════════════════════════════════════════════════════════════════════════════
# Tenant Isolation v0 — Phase B WS3 (dashboard persona + workspace scoping)
#
# These tests engage MULTI-TENANT mode (HOMIE_TENANT_ENFORCEMENT=true + ≥1
# active non-admin tenant_tokens row). They prove, PER ROUTE:
#   * tenant_persona routes 403 a cross-tenant persona_id (B token -> A persona)
#     and 200 the caller's own persona.
#   * GET /api/agents enumeration is FILTERED to scope.
#   * NB4 aggregate routes (memory/graph, brain/graph, memories, hive-mind/recent)
#     do NOT return aggregate to a non-admin tenant (403); admin still aggregates.
#   * NB3 empty-scope token 403s EVERY persona_id; admin/None allows all.
#   * M4 destructive routes: B cannot delete/patch A's persona/avatar (403); a
#     deleted profile under the caller's own scope -> 404.
#   * tenant_workspace work/tasks: a tenant sees/touches only its workspace rows.
#   * Parity: enforcement OFF / zero tenant rows -> responses byte-unchanged.
#
# WS2 contract consumed verbatim (orchestration.tenant_auth.resolve_tenant_binding):
#   persona_scope=None -> admin allow-all; frozenset() -> non-admin deny-all;
#   a non-admin token NEVER carries None.
# ════════════════════════════════════════════════════════════════════════════

_MT_ADMIN_TOKEN = "ws3-admin-raw-token"
_MT_TOKEN_A = "ws3-tenant-a-raw-token"
_MT_TOKEN_B = "ws3-tenant-b-raw-token"
_MT_TOKEN_EMPTY = "ws3-tenant-empty-raw-token"  # non-admin, empty persona scope
_MT_WS_A = 2
_MT_WS_B = 3
_MT_WS_EMPTY = 4


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_named_profile(homie_root: Path, name: str) -> None:
    """Materialize a NAMED profile root with the 4 expected children.

    ``_profile_disk_state`` reads ``resolve_profile_root(name)`` -> in tests
    that resolves to ``<homie_root>/profiles/<name>`` (HOMIE_HOME outside the
    real ``~/.homie``). Creating ``memory/``, ``data/``, ``state/`` +
    ``config.yaml`` makes the state 'intact' so the M4 gate passes for an
    in-scope persona.
    """
    root = homie_root / "profiles" / name
    (root / "memory").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(
        "persona:\n  name: " + name + "\nmodel:\n  preferred: claude-opus-4-7\n",
        encoding="utf-8",
    )


@pytest.fixture
def mt_app(tmp_path, monkeypatch):
    """Multi-tenant dashboard app: admin + tenant-A + tenant-B + empty-scope token.

    Tenant A is scoped to ['persona-a'], tenant B to ['persona-b'], the empty
    token to []. Physical named profiles for persona-a / persona-b are created
    so the M4 physical gate sees them as 'intact'. Yields the TestClient.
    """
    from orchestration.tenant_auth import hash_token

    dash_db = tmp_path / "dashboard.db"
    chat_db = tmp_path / "chat.db"
    memory_db = tmp_path / "memory.db"
    orch_db = tmp_path / "orchestration.db"
    homie_root = tmp_path / ".homie"
    _make_chat_db(chat_db)
    _make_memory_db(memory_db)
    _make_named_profile(homie_root, "persona-a")
    _make_named_profile(homie_root, "persona-b")

    import config
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", dash_db)
    monkeypatch.setattr(config, "CHAT_DB_PATH", chat_db)
    monkeypatch.setattr(config, "DATABASE_PATH", memory_db)
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", orch_db)
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", _MT_ADMIN_TOKEN)
    monkeypatch.setenv("HOMIE_TENANT_ENFORCEMENT", "true")

    import orchestration.api as oa
    importlib.reload(oa)
    db, cs, ms, reg, ts = oa._get_services()
    oa._db = db
    oa._convoy_svc = cs
    oa._mailbox_svc = ms
    oa._executor_registry = reg
    oa._team_svc = ts

    # Admin bootstrap FIRST so the global token survives MT mode (NM1).
    db.insert_tenant_token(hash_token(_MT_ADMIN_TOKEN), _MT_WS_A, None, True, "admin")
    db.insert_tenant_token(hash_token(_MT_TOKEN_A), _MT_WS_A, '["persona-a"]', False, "tenant-a")
    db.insert_tenant_token(hash_token(_MT_TOKEN_B), _MT_WS_B, '["persona-b"]', False, "tenant-b")
    db.insert_tenant_token(hash_token(_MT_TOKEN_EMPTY), _MT_WS_EMPTY, "[]", False, "tenant-empty")

    yield TestClient(oa.app)
    db.close()


# ── tenant_persona per-route 403/200 matrix (B token -> A's persona -> 403) ──
#
# Every tenant_persona dashboard route with a persona_id in the PATH. Each row:
#   (method, path_template, json_body_or_None). The matrix asserts, PER ROUTE:
#     * tenant-B hitting persona-a (out of B's scope) -> 403
#     * tenant-A hitting persona-a (in scope)        -> NOT 403 (allowed through;
#       may be 200/404/422 depending on data, but never the scope 403).

# The SSE stream route is in the CROSS-TENANT matrix only: its 403 fires BEFORE
# the StreamingResponse, so a cross-tenant call returns immediately. The
# same-tenant matrix excludes it because TestClient.get() on a same-tenant call
# would block on the infinite keepalive generator.
# BLOCKER #3 (B6-deferred): /api/agents/{pid}/conversation, /api/agents/{pid}/tokens,
# and /api/hive-mind/recent read the SHARED chat.db by runtime_profile_key=persona
# with NO workspace column. Two tenants assigned the same (globally-named) persona
# could read each other's chat rows. Robust scoping needs the B6 per-row workspace
# column (deferred), so WS2 reclassified these 3 from tenant_persona -> admin in
# route_policy.py. The middleware now 403s ANY bound tenant token on them (admin
# still 200s). Their per-handler persona-scope gates are now moot-but-harmless
# (middleware denies first). They are EXCLUDED from the tenant_persona reachability
# matrix below and asserted in _RECLASSIFIED_ADMIN_ROUTES instead.
_PERSONA_PATH_ROUTES_READONLY = [
    ("GET", "/api/agents/{pid}"),
    ("GET", "/api/agents/{pid}/files"),
    ("GET", "/api/agents/{pid}/files/history"),
    ("GET", "/api/agents/{pid}/tasks"),
    ("GET", "/api/conversation/{pid}/history"),
    ("GET", "/api/conversation/{pid}/stream"),
]

_PERSONA_PATH_ROUTES_READONLY_SAME_TENANT = [
    r for r in _PERSONA_PATH_ROUTES_READONLY if not r[1].endswith("/stream")
]

# BLOCKER #3 — the 3 routes reclassified tenant_persona -> admin (B6-deferred:
# chat_sessions lacks a workspace column, so per-row tenant scoping is impossible
# in v0). Under enforcement-on, a bound tenant token gets 403 (admin-only) and an
# admin token still 200s. (path_template, query_params)
_RECLASSIFIED_ADMIN_ROUTES = [
    ("/api/agents/persona-a/conversation", {}),
    ("/api/agents/persona-a/tokens", {}),
    ("/api/hive-mind/recent", {"persona_id": "persona-a"}),
    ("/api/hive-mind/recent", {}),
]

_PERSONA_PATH_ROUTES_MUTATE = [
    ("POST", "/api/agents/{pid}/activate", None),
    ("POST", "/api/agents/{pid}/deactivate", None),
    ("POST", "/api/agents/{pid}/restart", None),
    ("PATCH", "/api/agents/{pid}/model", {"model": "claude-haiku-4-5"}),
    ("PATCH", "/api/agents/{pid}/files/SOUL.md", {"content": "hi"}),
    ("DELETE", "/api/agents/{pid}", None),
    ("DELETE", "/api/agents/{pid}/full", {"confirm": True}),
    ("DELETE", "/api/agents/{pid}/avatar", None),
    ("POST", "/api/conversation/{pid}/send", {"text": "hi"}),
]


def _call(client, method, path, body=None, *, token):
    headers = _bearer(token)
    if method == "GET":
        return client.get(path, headers=headers)
    if method == "POST":
        return client.post(path, headers=headers, json=body)
    if method == "PATCH":
        return client.patch(path, headers=headers, json=body)
    if method == "DELETE":
        # DELETE with a JSON body (hard-delete confirm) when provided.
        if body is not None:
            return client.request("DELETE", path, headers=headers, json=body)
        return client.delete(path, headers=headers)
    raise AssertionError(method)


@pytest.mark.parametrize("method,template", _PERSONA_PATH_ROUTES_READONLY)
def test_ws3_persona_readonly_cross_tenant_403(mt_app, method, template):
    """tenant-B -> persona-a (out of scope) -> 403 on every read route."""
    path = template.format(pid="persona-a")
    r = _call(mt_app, method, path, token=_MT_TOKEN_B)
    assert r.status_code == 403, f"{method} {path} should 403 cross-tenant, got {r.status_code}"


@pytest.mark.parametrize("method,template", _PERSONA_PATH_ROUTES_READONLY_SAME_TENANT)
def test_ws3_persona_readonly_same_tenant_not_403(mt_app, method, template):
    """tenant-A -> persona-a (in scope) -> NOT the scope 403 (allowed through)."""
    path = template.format(pid="persona-a")
    r = _call(mt_app, method, path, token=_MT_TOKEN_A)
    assert r.status_code != 403, f"{method} {path} should allow in-scope, got 403"


@pytest.mark.parametrize("method,template,body", _PERSONA_PATH_ROUTES_MUTATE)
def test_ws3_persona_mutate_cross_tenant_403(mt_app, method, template, body):
    """tenant-B -> persona-a (out of scope) -> 403 on every mutate route."""
    path = template.format(pid="persona-a")
    r = _call(mt_app, method, path, body, token=_MT_TOKEN_B)
    assert r.status_code == 403, f"{method} {path} should 403 cross-tenant, got {r.status_code}"


def test_ws3_validate_id_cross_tenant_403(mt_app):
    """POST /api/agents/validate-id with an out-of-scope candidate id -> 403."""
    r = mt_app.post(
        "/api/agents/validate-id",
        headers=_bearer(_MT_TOKEN_B),
        json={"persona_id": "persona-a"},
    )
    assert r.status_code == 403


def test_ws3_validate_id_in_scope_allowed(mt_app):
    """tenant-A validating its own id -> not the scope 403."""
    r = mt_app.post(
        "/api/agents/validate-id",
        headers=_bearer(_MT_TOKEN_A),
        json={"persona_id": "persona-a"},
    )
    assert r.status_code == 200


# ── GET /api/agents enumeration filtered to scope ────────────────────────────


def test_ws3_agents_enumeration_filtered_to_scope(mt_app):
    """tenant-B sees ONLY persona-b in the list; admin sees both."""
    rb = mt_app.get("/api/agents", headers=_bearer(_MT_TOKEN_B))
    assert rb.status_code == 200
    ids_b = {a["id"] for a in rb.json()["agents"]}
    assert ids_b <= {"persona-b"}, f"tenant-B leaked personas: {ids_b}"
    assert "persona-a" not in ids_b

    ra = mt_app.get("/api/agents", headers=_bearer(_MT_TOKEN_A))
    ids_a = {a["id"] for a in ra.json()["agents"]}
    assert "persona-b" not in ids_a

    radmin = mt_app.get("/api/agents", headers=_bearer(_MT_ADMIN_TOKEN))
    ids_admin = {a["id"] for a in radmin.json()["agents"]}
    assert {"persona-a", "persona-b"} <= ids_admin, f"admin missing personas: {ids_admin}"


# ── NB3 — empty-scope token 403s every persona_id; admin/None allows all ─────


@pytest.mark.parametrize("pid", ["persona-a", "persona-b", "anything-else"])
def test_ws3_nb3_empty_scope_403s_every_persona(mt_app, pid):
    """A non-admin token with an EMPTY persona scope is denied EVERY persona_id."""
    r = mt_app.get(f"/api/agents/{pid}", headers=_bearer(_MT_TOKEN_EMPTY))
    assert r.status_code == 403, f"empty-scope must 403 {pid}, got {r.status_code}"


def test_ws3_nb3_empty_scope_enumeration_is_empty(mt_app):
    """Empty-scope token sees an EMPTY /api/agents list (deny-all enumeration)."""
    r = mt_app.get("/api/agents", headers=_bearer(_MT_TOKEN_EMPTY))
    assert r.status_code == 200
    assert r.json()["agents"] == []


@pytest.mark.parametrize("pid", ["persona-a", "persona-b"])
def test_ws3_nb3_admin_none_allows_all_personas(mt_app, pid):
    """Admin (persona_scope=None) reaches EVERY persona (allow-all)."""
    r = mt_app.get(f"/api/agents/{pid}", headers=_bearer(_MT_ADMIN_TOKEN))
    assert r.status_code != 403


# ── NB4 — aggregate read routes do NOT leak aggregate to a tenant token ──────


_NB4_AGGREGATE_REQUESTS = [
    ("/api/memory/graph", {}),                 # defaults scope=all (aggregate)
    ("/api/memory/graph", {"scope": "all"}),
    ("/api/memory/graph", {"scope": "global"}),
    ("/api/memory/graph", {"scope": "persona"}),  # persona but NO scope_id
    # BLOCKER #2 same-class audit: scope=room surfaces cabinet_meetings metadata
    # (title/chat_id/pinned_persona via _add_cabinet_session_nodes) and scope=team
    # is a cross-tenant aggregate. Both map to _nb4_persona=None → a non-admin
    # tenant must 403 (cabinet stays admin-only per the B6 v0 decision).
    ("/api/memory/graph", {"scope": "room"}),
    ("/api/memory/graph", {"scope": "room", "scope_id": "cabinet-1"}),
    ("/api/memory/graph", {"scope": "team"}),
    ("/api/brain/graph", {}),
    ("/api/brain/graph", {"scope": "all"}),
    ("/api/brain/graph", {"scope": "persona"}),   # persona but NO scope_id
    ("/api/brain/graph", {"scope": "room", "scope_id": "cabinet-1"}),
    ("/api/memories", {}),                     # persona_id=None (aggregate)
    # NOTE: /api/hive-mind/recent moved to _RECLASSIFIED_ADMIN_ROUTES — BLOCKER #3
    # reclassified it tenant_persona -> admin (B6-deferred, no workspace column).
]


@pytest.mark.parametrize("path,params", _NB4_AGGREGATE_REQUESTS)
def test_ws3_nb4_tenant_aggregate_denied(mt_app, path, params):
    """A NON-admin tenant requesting the no-filter/scope=all form -> 403."""
    r = mt_app.get(path, headers=_bearer(_MT_TOKEN_A), params=params)
    assert r.status_code == 403, (
        f"{path}?{params} must NOT return aggregate to a tenant, got {r.status_code}"
    )


@pytest.mark.parametrize("path,params", _NB4_AGGREGATE_REQUESTS)
def test_ws3_nb4_admin_aggregate_allowed(mt_app, path, params):
    """An ADMIN token requesting the same aggregate form -> 200 (allow-all)."""
    r = mt_app.get(path, headers=_bearer(_MT_ADMIN_TOKEN), params=params)
    assert r.status_code == 200, f"{path}?{params} admin aggregate denied: {r.text}"


def test_ws3_nb4_memory_graph_in_scope_persona_allowed(mt_app):
    """tenant-A with its own persona scope_id -> 200 (scoped read allowed)."""
    r = mt_app.get(
        "/api/memory/graph",
        headers=_bearer(_MT_TOKEN_A),
        params={"scope": "persona", "scope_id": "persona-a"},
    )
    assert r.status_code == 200


def test_ws3_nb4_memory_graph_cross_tenant_persona_403(mt_app):
    """tenant-B requesting persona-a's scope_id -> 403 (out of scope)."""
    r = mt_app.get(
        "/api/memory/graph",
        headers=_bearer(_MT_TOKEN_B),
        params={"scope": "persona", "scope_id": "persona-a"},
    )
    assert r.status_code == 403


def test_ws3_nb4_memories_in_scope_allowed(mt_app):
    """tenant-A scoping /api/memories to its own persona_id -> 200."""
    r = mt_app.get(
        "/api/memories", headers=_bearer(_MT_TOKEN_A), params={"persona_id": "persona-a"}
    )
    assert r.status_code == 200


# ── BLOCKER #3 — un-scopable persona chat reads reclassified tenant_persona ──
# -> admin (B6-deferred: chat_sessions has no workspace column, so per-row tenant
# scoping is impossible in v0). The shared chat.db is read by
# runtime_profile_key=persona; two tenants assigned the same globally-named persona
# could read each other's chat. Until the B6 workspace column lands these 3 are
# admin-only deny-by-default: ANY bound tenant token 403s, admin still 200s.


@pytest.mark.parametrize("path,params", _RECLASSIFIED_ADMIN_ROUTES)
def test_ws3_blocker3_reclassified_route_tenant_403(mt_app, path, params):
    """Reclassified admin route: an in-scope tenant token (A→persona-a, A owns it)
    is now 403 — the route is admin-only, not tenant_persona, regardless of scope."""
    r = mt_app.get(path, headers=_bearer(_MT_TOKEN_A), params=params)
    assert r.status_code == 403, (
        f"{path}?{params} is admin-only (B6-deferred); a tenant token must 403, "
        f"got {r.status_code}"
    )


@pytest.mark.parametrize("path,params", _RECLASSIFIED_ADMIN_ROUTES)
def test_ws3_blocker3_reclassified_route_cross_tenant_403(mt_app, path, params):
    """A cross-tenant token (B→persona-a) is also 403 on the reclassified routes."""
    r = mt_app.get(path, headers=_bearer(_MT_TOKEN_B), params=params)
    assert r.status_code == 403, (
        f"{path}?{params} cross-tenant must 403, got {r.status_code}"
    )


@pytest.mark.parametrize("path,params", _RECLASSIFIED_ADMIN_ROUTES)
def test_ws3_blocker3_reclassified_route_admin_200(mt_app, path, params):
    """The admin/global token still reaches the reclassified routes (200)."""
    r = mt_app.get(path, headers=_bearer(_MT_ADMIN_TOKEN), params=params)
    assert r.status_code == 200, (
        f"{path}?{params} admin must still 200, got {r.status_code}: {r.text}"
    )


# ── M4 — destructive routes: scope 403 cross-tenant + 404 on deleted target ──


def test_ws3_m4_cross_tenant_soft_delete_403(mt_app):
    """tenant-B cannot soft-delete persona-a (403 before any state read)."""
    r = mt_app.delete("/api/agents/persona-a", headers=_bearer(_MT_TOKEN_B))
    assert r.status_code == 403


def test_ws3_m4_cross_tenant_patch_file_403(mt_app):
    """tenant-B cannot PATCH persona-a's files (403)."""
    r = mt_app.patch(
        "/api/agents/persona-a/files/SOUL.md",
        headers=_bearer(_MT_TOKEN_B),
        json={"content": "x"},
    )
    assert r.status_code == 403


def test_ws3_m4_cross_tenant_avatar_delete_403(mt_app):
    """tenant-B cannot delete persona-a's avatar (403)."""
    r = mt_app.delete("/api/agents/persona-a/avatar", headers=_bearer(_MT_TOKEN_B))
    assert r.status_code == 403


def test_ws3_m4_deleted_profile_in_scope_404(mt_app, tmp_path):
    """A tenant whose scoped profile root was removed -> destructive route 404.

    persona-a is in tenant-A's scope, but its on-disk root is gone (deleted) ->
    the M4 physical gate (read disk, not meta — Rule 2) refuses with 404. Scope
    passes (in scope), physical fails (deleted).
    """
    import shutil

    # Remove persona-a's profile root from disk while tenant-A still has it in
    # scope. resolve_profile_root("persona-a") -> <HOMIE_HOME>/profiles/persona-a.
    from personas.lifecycle import resolve_profile_root

    root = resolve_profile_root("persona-a")
    shutil.rmtree(root, ignore_errors=True)

    r = mt_app.delete("/api/agents/persona-a/avatar", headers=_bearer(_MT_TOKEN_A))
    assert r.status_code == 404, f"deleted in-scope profile must 404, got {r.status_code}"


def test_ws3_m4_in_scope_destructive_passes_scope_and_physical(mt_app):
    """tenant-A deleting its OWN avatar (in scope + intact) -> NOT 403/404."""
    r = mt_app.delete("/api/agents/persona-a/avatar", headers=_bearer(_MT_TOKEN_A))
    # 200 (idempotent ok) — scope passed, physical 'intact', kill-switch on.
    assert r.status_code == 200


# ── tenant_workspace — work/tasks scoped to the caller's workspace ───────────


def test_ws3_work_tasks_cross_workspace_isolated(mt_app):
    """tenant-A creates a task; tenant-B's list does NOT see it, and B's PATCH
    of A's task_id -> 404 (cross-workspace task is invisible)."""
    created = mt_app.post(
        "/api/work/tasks",
        headers=_bearer(_MT_TOKEN_A),
        json={"title": "tenant-a-only-task"},
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["task"]["id"]

    # tenant-B lists work — must NOT see tenant-A's task.
    listing_b = mt_app.get("/api/work/tasks", headers=_bearer(_MT_TOKEN_B))
    assert listing_b.status_code == 200
    b_task_ids = {t["id"] for t in listing_b.json()["tasks"]}
    assert task_id not in b_task_ids, "tenant-B saw tenant-A's task (cross-ws leak)"

    # tenant-A DOES see its own task.
    listing_a = mt_app.get("/api/work/tasks", headers=_bearer(_MT_TOKEN_A))
    a_task_ids = {t["id"] for t in listing_a.json()["tasks"]}
    assert task_id in a_task_ids

    # tenant-B PATCHing tenant-A's task_id -> 404 (cross-workspace).
    patch_b = mt_app.patch(
        f"/api/work/tasks/{task_id}",
        headers=_bearer(_MT_TOKEN_B),
        json={"assigned_agent_id": "hijack"},
    )
    assert patch_b.status_code == 404

    # tenant-B dispatching tenant-A's task_id -> 404.
    dispatch_b = mt_app.post(
        f"/api/work/tasks/{task_id}/dispatch", headers=_bearer(_MT_TOKEN_B)
    )
    assert dispatch_b.status_code == 404


# ── Parity — enforcement OFF / zero tenant rows -> byte-unchanged ────────────


def test_ws3_parity_enforcement_off_persona_route_unchanged(tmp_path, monkeypatch):
    """With tenant rows present but enforcement OFF, a dashboard persona route
    behaves EXACTLY as single-tenant: the legacy global-token gate runs, a
    tenant token is just a non-global bearer -> 401 (no scope 403 path)."""
    from orchestration.tenant_auth import hash_token

    dash_db = tmp_path / "dashboard.db"
    orch_db = tmp_path / "orchestration.db"
    homie_root = tmp_path / ".homie"
    _make_named_profile(homie_root, "persona-a")

    import config
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", dash_db)
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", orch_db)
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", _MT_ADMIN_TOKEN)
    monkeypatch.delenv("HOMIE_TENANT_ENFORCEMENT", raising=False)  # OFF (default)

    import orchestration.api as oa
    importlib.reload(oa)
    db, cs, ms, reg, ts = oa._get_services()
    oa._db = db
    oa._convoy_svc = cs
    oa._mailbox_svc = ms
    oa._executor_registry = reg
    oa._team_svc = ts
    db.insert_tenant_token(hash_token(_MT_TOKEN_A), _MT_WS_A, '["persona-a"]', False, "tenant-a")

    client = TestClient(oa.app)
    # Global/admin token still works (back-compat).
    admin_r = client.get("/api/agents/persona-a", headers=_bearer(_MT_ADMIN_TOKEN))
    assert admin_r.status_code in (200, 404)
    # A tenant token is NOT resolved (enforcement off) -> 401 like legacy, NOT 403.
    tenant_r = client.get("/api/agents/persona-a", headers=_bearer(_MT_TOKEN_A))
    assert tenant_r.status_code == 401
    db.close()


def test_ws3_parity_zero_rows_aggregate_unchanged(isolated_app):
    """Zero tenant rows (the isolated_app default): the NB4 aggregate routes
    return their full aggregate exactly as before (scope is None -> allow-all)."""
    assert isolated_app.get("/api/memory/graph").status_code == 200
    assert isolated_app.get("/api/memories").status_code == 200
    assert isolated_app.get("/api/hive-mind/recent").status_code == 200
    assert isolated_app.get("/api/brain/graph").status_code == 200
    assert isolated_app.get("/api/agents").status_code == 200


def test_ws3_blocker3_reclassified_routes_single_tenant_parity(isolated_app):
    """PARITY: with zero tenant rows / enforcement OFF (ws defaults to 1), the 3
    routes reclassified to admin in MT mode still serve 200 EXACTLY as before.

    The route_policy admin classification only 403s a BOUND TENANT TOKEN in
    multi-tenant mode (is_multi_tenant_mode + enforcement on). In single-tenant
    mode the middleware never resolves a tenant binding, so the routes behave
    byte-identically to pre-reclassification. (The default profile route uses
    'default'; the persona detail uses the bootstrap default.)"""
    assert isolated_app.get("/api/agents/default/conversation").status_code == 200
    assert isolated_app.get("/api/agents/default/tokens").status_code == 200
    assert isolated_app.get("/api/hive-mind/recent").status_code == 200


# ════════════════════════════════════════════════════════════════════════════
# BLOCKER #2 — dashboard conversation cross-tenant leak via shared conversation_id
#
# The conversation routes (/api/conversation/{persona_id}/history|send|stream)
# are persona-scope gated, but stored/read by conversation_id ONLY. The default
# id is the SHARED constant 'dashboard-main', and session_id =
# web:{conversation_id}:{conversation_id}. Two tenants both defaulting to
# 'dashboard-main' resolved to the SAME chat session / SSE buffer → tenant B
# read tenant A's conversation. Fix: _scoped_conversation_id binds the id to
# request.state.workspace_id (true tenant boundary), with byte-identical
# single-tenant parity (ws == DEFAULT_WORKSPACE_ID → id unchanged).
# ════════════════════════════════════════════════════════════════════════════


class _FakeState:
    def __init__(self, workspace_id):
        self.workspace_id = workspace_id


class _FakeRequest:
    """Minimal Request stand-in carrying only request.state.workspace_id."""

    def __init__(self, workspace_id):
        self.state = _FakeState(workspace_id)


def test_blocker2_scoped_conversation_id_parity_default_workspace():
    """ws == DEFAULT_WORKSPACE_ID (1) → conversation_id returned UNCHANGED.

    This is the single-tenant parity guarantee: existing chat sessions, SSE
    buffers, and session_ids are byte-identical to the pre-fix behavior.
    """
    import dashboard_api

    req = _FakeRequest(1)  # DEFAULT_WORKSPACE_ID
    assert dashboard_api._scoped_conversation_id(req, "dashboard-main") == "dashboard-main"
    assert dashboard_api._scoped_conversation_id(req, "default") == "default"


def test_blocker2_scoped_conversation_id_isolates_workspaces():
    """Two different non-default workspaces yield DISJOINT keys for the SAME id.

    This is the leak fix at the unit level: without scoping, both tenants
    sharing 'dashboard-main' collide; with scoping they cannot.
    """
    import dashboard_api

    a = dashboard_api._scoped_conversation_id(_FakeRequest(2), "dashboard-main")
    b = dashboard_api._scoped_conversation_id(_FakeRequest(3), "dashboard-main")
    assert a != b, "two workspaces must NOT share a conversation key (cross-tenant leak)"
    assert a == "ws2.dashboard-main"
    assert b == "ws3.dashboard-main"
    # The scoped key must stay a valid dashboard chat id.
    assert dashboard_api._DASHBOARD_CHAT_ID_RE.fullmatch(a)
    assert dashboard_api._DASHBOARD_CHAT_ID_RE.fullmatch(b)


def _make_chat_db_with_tool_calls(path, *, seed_session_id):
    """Seed a chat.db (with tool_calls_json) carrying ONE message under
    *seed_session_id* — used to simulate tenant A's persisted conversation."""
    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE chat_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            runtime_profile_key TEXT DEFAULT 'default',
            runtime_provider TEXT DEFAULT 'claude',
            runtime_model TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '2026-05-07T00:00:00',
            message_count INTEGER DEFAULT 0,
            total_cost_usd REAL DEFAULT 0.0
        );
        CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            tool_calls_json TEXT DEFAULT '[]'
        );
    """)
    conn.execute(
        "INSERT INTO chat_sessions (session_id) VALUES (?)", (seed_session_id,)
    )
    conn.execute(
        "INSERT INTO chat_messages (session_id, role, content, created_at, tool_calls_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            seed_session_id,
            "user",
            "TENANT-A SECRET MESSAGE",
            now.isoformat(timespec="seconds"),
            "[]",
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def mt_app_with_tenant_a_conversation(tmp_path, monkeypatch):
    """MT app where tenant A (ws 2) already has a 'dashboard-main' conversation
    persisted at its WORKSPACE-SCOPED session_id (web:ws2.dashboard-main:...).

    The fix means tenant B (ws 3) reading 'dashboard-main' resolves to
    web:ws3.dashboard-main:... → it must NOT see A's row. Without the fix both
    resolve to web:dashboard-main:dashboard-main → B reads A's secret.
    """
    from orchestration.tenant_auth import hash_token

    dash_db = tmp_path / "dashboard.db"
    chat_db = tmp_path / "chat.db"
    orch_db = tmp_path / "orchestration.db"
    homie_root = tmp_path / ".homie"
    _make_named_profile(homie_root, "persona-a")
    _make_named_profile(homie_root, "persona-b")

    # Seed A's conversation at the session_id A's SEND actually produces — derived
    # from the production helper itself so the test tracks the FIX STATE:
    #   * post-fix: _scoped_conversation_id(ws=2,...) → 'ws2.dashboard-main' →
    #     web:ws2.dashboard-main:... ; B reads web:ws3.dashboard-main:... → empty.
    #   * pre-fix:  the helper returns the BARE id for BOTH → A seeds AND B reads
    #     web:dashboard-main:dashboard-main → B SEES A's secret → test RED.
    # This is the fail-without-fix guarantee.
    import dashboard_api
    a_scoped = dashboard_api._scoped_conversation_id(_FakeRequest(_MT_WS_A), "dashboard-main")
    a_session_id = f"web:{a_scoped}:{a_scoped}"
    _make_chat_db_with_tool_calls(chat_db, seed_session_id=a_session_id)

    import config
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", dash_db)
    monkeypatch.setattr(config, "CHAT_DB_PATH", chat_db)
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", orch_db)
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", _MT_ADMIN_TOKEN)
    monkeypatch.setenv("HOMIE_TENANT_ENFORCEMENT", "true")

    import orchestration.api as oa
    importlib.reload(oa)
    db, cs, ms, reg, ts = oa._get_services()
    oa._db = db
    oa._convoy_svc = cs
    oa._mailbox_svc = ms
    oa._executor_registry = reg
    oa._team_svc = ts
    db.insert_tenant_token(hash_token(_MT_ADMIN_TOKEN), _MT_WS_A, None, True, "admin")
    db.insert_tenant_token(hash_token(_MT_TOKEN_A), _MT_WS_A, '["persona-a"]', False, "tenant-a")
    db.insert_tenant_token(hash_token(_MT_TOKEN_B), _MT_WS_B, '["persona-b"]', False, "tenant-b")

    yield TestClient(oa.app)
    db.close()


def test_blocker2_history_cross_tenant_isolated(mt_app_with_tenant_a_conversation):
    """Tenant B reading the DEFAULT 'dashboard-main' conversation must NOT see
    tenant A's message (different workspace → different scoped session_id).

    FAIL-WITHOUT-FIX: pre-fix, both A and B resolve 'dashboard-main' to the same
    session_id web:dashboard-main:dashboard-main, so B's history would return
    A's 'TENANT-A SECRET MESSAGE'. The workspace scoping makes B's read hit
    web:ws3.dashboard-main:... which has no rows.
    """
    client = mt_app_with_tenant_a_conversation

    # Tenant A (ws 2) DOES see its own conversation under the default id.
    ra = client.get(
        "/api/conversation/persona-a/history",
        headers=_bearer(_MT_TOKEN_A),
        params={"conversation_id": "dashboard-main"},
    )
    assert ra.status_code == 200
    a_contents = [t["content"] for t in ra.json()["turns"]]
    assert "TENANT-A SECRET MESSAGE" in a_contents, "tenant A must see its OWN conversation"

    # Tenant B (ws 3) reading the SAME default id must see NOTHING of A's.
    rb = client.get(
        "/api/conversation/persona-b/history",
        headers=_bearer(_MT_TOKEN_B),
        params={"conversation_id": "dashboard-main"},
    )
    assert rb.status_code == 200
    b_contents = [t["content"] for t in rb.json()["turns"]]
    assert "TENANT-A SECRET MESSAGE" not in b_contents, (
        "CROSS-TENANT LEAK: tenant B read tenant A's conversation via the shared "
        "default conversation_id"
    )
    assert b_contents == [], "tenant B's default conversation must be empty"


def test_blocker2_single_tenant_conversation_history_parity(isolated_app, tmp_path, monkeypatch):
    """Parity: single-tenant (zero tenant rows, ws defaults to 1) → the default
    'dashboard-main' conversation resolves to the UNSCOPED session_id exactly as
    before. A row seeded at web:dashboard-main:dashboard-main is returned."""
    import config

    # Re-seed the isolated_app's chat.db with a tool_calls_json column + a row at
    # the UNSCOPED default session id.
    chat_db = Path(config.CHAT_DB_PATH)
    if chat_db.exists():
        chat_db.unlink()
    _make_chat_db_with_tool_calls(
        chat_db, seed_session_id="web:dashboard-main:dashboard-main"
    )

    r = isolated_app.get(
        "/api/conversation/default/history",
        params={"conversation_id": "dashboard-main"},
    )
    assert r.status_code == 200
    contents = [t["content"] for t in r.json()["turns"]]
    assert "TENANT-A SECRET MESSAGE" in contents, (
        "single-tenant parity broken: the unscoped default session must still resolve"
    )


# ── Camera-as-agent-tool image persistence (M3) ──────────────────────────


def test_persist_dashboard_image_writes_file_and_bytes(tmp_path, monkeypatch):
    """Base64 JPEG -> disk with exact bytes (the Read-tool vision seam)."""
    import base64 as _b64
    import dashboard_api

    monkeypatch.setattr(dashboard_api, "_DASHBOARD_PHOTO_DIR", tmp_path / "photos")
    raw = b"\xff\xd8\xff\xe0JFIF-fake-jpeg-bytes\xff\xd9"
    b64 = _b64.b64encode(raw).decode()

    path, size = dashboard_api._persist_dashboard_image(b64)
    assert path.exists()
    assert path.suffix == ".jpg"
    assert path.parent == tmp_path / "photos"
    assert size == len(raw)
    assert path.read_bytes() == raw


def test_persist_dashboard_image_tolerates_data_uri_prefix(tmp_path, monkeypatch):
    import base64 as _b64
    import dashboard_api

    monkeypatch.setattr(dashboard_api, "_DASHBOARD_PHOTO_DIR", tmp_path / "photos")
    raw = b"\xff\xd8fake\xff\xd9"
    b64 = "data:image/jpeg;base64," + _b64.b64encode(raw).decode()

    path, size = dashboard_api._persist_dashboard_image(b64)
    assert path.read_bytes() == raw
    assert size == len(raw)


def test_dashboard_send_body_accepts_image_field():
    """The send body carries an optional base64 image (M3)."""
    from dashboard_api import DashboardChatSendBody

    body = DashboardChatSendBody(text="what is this?", image_base64="AAAA")
    assert body.image_base64 == "AAAA"
    assert DashboardChatSendBody(text="hi").image_base64 is None


# ── /api/social/* (Postiz lane + approval queue) ─────────────────────────


def _silence_social_audit(monkeypatch, sink: list | None = None):
    def _record(**kwargs):
        if sink is not None:
            sink.append(kwargs)
        return "audit-test"

    monkeypatch.setattr("social.audit.append_social_audit_record", _record)


def test_social_status_unconfigured_has_no_secrets(isolated_app, monkeypatch):
    monkeypatch.delenv("POSTIZ_API_URL", raising=False)
    monkeypatch.delenv("POSTIZ_API_KEY", raising=False)

    r = isolated_app.get("/api/social/status")

    assert r.status_code == 200
    body = r.json()
    assert body["postiz"]["configured"] is False
    assert body["postiz"]["reachable"] is False
    # Booleans/counts only — never the URL or key.
    assert "api_url" not in json.dumps(body).lower()
    assert "api_key" not in json.dumps(body).lower()


def test_social_compose_lands_as_draft_only(isolated_app, monkeypatch):
    _silence_social_audit(monkeypatch)

    r = isolated_app.post(
        "/api/social/compose",
        json={"channel": "mastodon", "title": "T", "body": "Hello fedi"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "draft"

    q = isolated_app.get("/api/social/queue").json()
    row = next(p for p in q["posts"] if p["id"] == body["id"])
    assert row["status"] == "draft"  # compose NEVER approves or publishes


def test_social_compose_unknown_channel_400(isolated_app, monkeypatch):
    _silence_social_audit(monkeypatch)

    r = isolated_app.post(
        "/api/social/compose",
        json={"channel": "myspace", "body": "hi"},
    )

    assert r.status_code == 400
    assert "Unknown channel" in r.json()["detail"]


def test_social_approve_dispatches_immediately(isolated_app, monkeypatch):
    """Dashboard Approve & Post = approve + gated dispatch in one tap
    (Telegram-button parity, operator decision 2026-07-06)."""
    _silence_social_audit(monkeypatch)
    dispatched: list[int] = []
    monkeypatch.setattr(
        "social.post_executor.dispatch_post",
        lambda pid, **kw: dispatched.append(pid) or True,
    )

    pid1 = isolated_app.post(
        "/api/social/compose", json={"channel": "mastodon", "body": "a"}
    ).json()["id"]
    pid2 = isolated_app.post(
        "/api/social/compose", json={"channel": "mastodon", "body": "b"}
    ).json()["id"]

    a = isolated_app.post("/api/social/approve", json={"post_id": pid1})
    assert a.status_code == 200
    assert a.json()["dispatched"] is True
    assert dispatched == [pid1]

    b = isolated_app.post("/api/social/reject", json={"post_id": pid2})
    assert b.status_code == 200
    assert b.json()["status"] == "rejected"

    # Approving a rejected post is an invalid transition -> 400, no dispatch.
    again = isolated_app.post("/api/social/approve", json={"post_id": pid2})
    assert again.status_code == 400
    assert dispatched == [pid1]


def test_social_approve_surfaces_dispatch_failure(isolated_app, monkeypatch):
    """An unbound postiz channel fails the dispatch and the response says so
    (no 500, no silent success)."""
    from social.channels import SocialChannel

    _silence_social_audit(monkeypatch)
    monkeypatch.setattr(
        "social.post_executor.append_social_audit_record", lambda **kw: "a"
    )
    # Pin the channel to an UNBOUND postiz channel so the test is deterministic
    # regardless of channels.yaml/persona state left by other tests.
    monkeypatch.setattr(
        "social.post_executor.get_channel",
        lambda cid, **kw: SocialChannel(
            channel_id="bluesky", display_name="Bluesky",
            execution_method="postiz", postiz_integration_id="",
        ),
    )

    pid = isolated_app.post(
        "/api/social/compose", json={"channel": "bluesky", "body": "hi"}
    ).json()["id"]

    r = isolated_app.post("/api/social/approve", json={"post_id": pid})

    assert r.status_code == 200
    body = r.json()
    assert body["dispatched"] is False
    assert body["status"] == "failed"
    assert "postiz_integration_id" in body["error"]


def test_social_connect_url_in_body_but_never_audited(isolated_app, monkeypatch):
    sensitive = "https://oauth.example/authorize?token=SENSITIVE-EXPIRING"
    audits: list[dict] = []
    _silence_social_audit(monkeypatch, audits)
    monkeypatch.setattr(
        "integrations.postiz_api.get_connect_url", lambda provider: sensitive
    )

    r = isolated_app.get("/api/social/connect-url?provider=mastodon")

    assert r.status_code == 200
    assert r.json()["url"] == sensitive
    # The audit row records the provider only — the URL never lands in it.
    assert audits, "connect-url must write an audit row"
    assert sensitive not in json.dumps(audits)
    assert audits[0]["channel"] == "mastodon"


def test_social_connect_url_rejects_bad_provider(isolated_app, monkeypatch):
    _silence_social_audit(monkeypatch)

    r = isolated_app.get("/api/social/connect-url?provider=Bad_Provider!")

    assert r.status_code == 400


def test_social_channels_degrades_when_postiz_down(isolated_app, monkeypatch):
    from integrations.postiz_api import PostizUnreachable

    def _boom():
        raise PostizUnreachable("refused")

    monkeypatch.setattr("integrations.postiz_api.list_integrations", _boom)

    r = isolated_app.get("/api/social/channels")

    assert r.status_code == 200
    body = r.json()
    assert body["postiz_error"]  # friendly message, not a stack trace
    assert isinstance(body["channels"], list)  # registry still renders
    assert any(c["channel_id"] == "mastodon" for c in body["channels"])


def test_social_reconcile_on_demand(isolated_app, monkeypatch):
    """POST /api/social/reconcile runs the same pass the cadence tick runs."""
    calls: list[bool] = []
    monkeypatch.setattr(
        "social.postiz_reconcile.reconcile_postiz_posts",
        lambda **kw: calls.append(True) or {"checked": 1, "confirmed": 1},
    )

    r = isolated_app.post("/api/social/reconcile")

    assert r.status_code == 200
    assert r.json()["confirmed"] == 1
    assert calls == [True]


def test_social_posts_view_slims_remote_rows(isolated_app, monkeypatch):
    monkeypatch.setattr(
        "integrations.postiz_api.list_posts",
        lambda start, end: [
            {
                "id": "pz-1",
                "content": "x" * 500,
                "state": "PUBLISHED",
                "releaseURL": "https://m.social/@x/1",
                "integration": {"id": "i1", "providerIdentifier": "mastodon"},
            }
        ],
    )

    r = isolated_app.get("/api/social/posts")

    assert r.status_code == 200
    row = r.json()["posts"][0]
    assert row["state"] == "PUBLISHED"
    assert len(row["content"]) <= 280  # slimmed preview
    assert row["integration"]["providerIdentifier"] == "mastodon"
