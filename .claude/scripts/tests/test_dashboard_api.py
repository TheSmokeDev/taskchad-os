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
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ── Helpers ──────────────────────────────────────────────────────────────


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
    assert body["killSwitches"] == {}


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
    r = isolated_app.get("/api/health")
    assert r.json()["killSwitches"] == {}


def test_get_health_response_has_no_secrets_or_pii(isolated_app):
    r = isolated_app.get("/api/health")
    body_text = r.text.lower()
    # No tokens, no env vars, no paths.
    forbidden = ["token", "secret", "password", "/users/", "homie_home", "api_key"]
    for needle in forbidden:
        assert needle not in body_text, f"health leaked '{needle}': {body_text}"


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
    assert "entries" in r.json()


def test_hive_mind_deterministic_ordering(isolated_app):
    r1 = isolated_app.get("/api/hive-mind/recent")
    r2 = isolated_app.get("/api/hive-mind/recent")
    assert r1.json() == r2.json()


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
