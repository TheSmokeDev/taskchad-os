"""PRD-8 Phase 6 v2 fix-pass 2026-05-10 — cabinet voice endpoint tests (M5).

FastAPI TestClient coverage for the 4 new ``/api/cabinet/voice/*`` routes
mounted on dashboard_api (and exempted-with-query-param-token by the
orchestration auth middleware). Plus the B1 + B2 + M4 + M3 regression
tests called out by the verifier consensus.

Covers contract criteria:
  * voice_meeting_html_endpoint_serves_html
  * voice_meeting_client_bundle_endpoint_serves_bundle
  * voice_meeting_avatar_endpoint_serves_persona_image
  * (B1) /api/cabinet/voice/* query-param token auth
  * (B2) Q4 main↔default canonical default usage
  * (M4) PNG magic-byte rejection on bad operator override
  * (M3) broadcast_order populated at meeting create

Test pattern mirrors test_cabinet_api.py — TestClient against a fresh
FastAPI(app).include_router(dashboard_api.router) for the dashboard
surface; orchestration.api.app for the auth-middleware integration.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import config  # noqa: E402


# ─── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def dash_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Dashboard router-only client (no auth middleware).

    Same shape as test_cabinet_api.py:client — fresh DB, clean cabinet
    channel registry, dashboard_api.router mounted on a bare FastAPI
    app. This isolates the dashboard endpoint behavior from the
    orchestration auth middleware (which is exercised separately in the
    auth-middleware fixture below).
    """
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", str(db_path))
    from cabinet import meeting_channel as channels_mod  # noqa: PLC0415
    channels_mod._reset_channels()
    from dashboard_db import get_connection as _get_conn  # noqa: PLC0415
    _get_conn().close()
    import dashboard_api  # noqa: PLC0415
    app = FastAPI()
    app.include_router(dashboard_api.router)
    return TestClient(app)


def _create_meeting(client: TestClient, chat_id: str = "test-chat") -> int:
    r = client.post("/api/cabinet/new", json={"chatId": chat_id})
    assert r.status_code == 200, r.text
    return r.json()["meetingId"]


# ─── voice_meeting_html_endpoint_serves_html (criterion + happy path) ─────


def test_voice_ui_endpoint_returns_html(dash_client: TestClient) -> None:
    """GET /api/cabinet/voice/ui returns 200 + text/html body containing
    the rendered voice meeting page."""
    meeting_id = _create_meeting(dash_client, "tg-1")
    r = dash_client.get(
        "/api/cabinet/voice/ui",
        params={"token": "ignored", "meetingId": meeting_id, "chatId": "tg-1"},
    )
    assert r.status_code == 200, r.text
    assert "text/html" in r.headers["content-type"].lower()
    assert "<!DOCTYPE html>" in r.text
    assert f"Meeting #{meeting_id}" in r.text


def test_voice_ui_404_on_missing_meeting(dash_client: TestClient) -> None:
    r = dash_client.get(
        "/api/cabinet/voice/ui",
        params={"token": "x", "meetingId": 99999, "chatId": ""},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "meeting_not_found"


def test_voice_ui_403_on_chat_mismatch(dash_client: TestClient) -> None:
    meeting_id = _create_meeting(dash_client, "tg-A")
    r = dash_client.get(
        "/api/cabinet/voice/ui",
        params={"token": "x", "meetingId": meeting_id, "chatId": "tg-B"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "chat_mismatch"


def test_voice_ui_410_on_ended_meeting(
    dash_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meeting_id = _create_meeting(dash_client, "tg-end")
    # End the meeting directly via the public endpoint so the row's
    # ended_at is non-NULL.
    r = dash_client.post(
        "/api/cabinet/end", json={"meetingId": meeting_id, "chatId": "tg-end"}
    )
    assert r.status_code == 200
    r2 = dash_client.get(
        "/api/cabinet/voice/ui",
        params={"token": "x", "meetingId": meeting_id, "chatId": "tg-end"},
    )
    assert r2.status_code == 410
    assert r2.json()["detail"] == "meeting_ended"


# ─── voice_meeting_client_bundle_endpoint_serves_bundle ───────────────────


def test_client_bundle_endpoint_serves_bundle(dash_client: TestClient) -> None:
    """GET /api/cabinet/voice/client.bundle.js returns the vendored
    Pipecat bundle with the right MIME + cache headers."""
    r = dash_client.get("/api/cabinet/voice/client.bundle.js")
    assert r.status_code == 200, r.text
    assert "application/javascript" in r.headers["content-type"].lower()
    # Sanity: bundle is non-trivial size (~430KB upstream).
    assert len(r.content) > 1000


def test_client_js_endpoint_serves_source(dash_client: TestClient) -> None:
    """GET /api/cabinet/voice/client.js returns the 12-LOC esbuild source."""
    r = dash_client.get("/api/cabinet/voice/client.js")
    assert r.status_code == 200, r.text
    assert "application/javascript" in r.headers["content-type"].lower()


# ─── voice_meeting_avatar_endpoint_serves_persona_image ───────────────────


def test_avatar_endpoint_bundled_fallback(dash_client: TestClient) -> None:
    """GET /api/cabinet/voice/avatars/research.png returns the bundled PNG."""
    r = dash_client.get("/api/cabinet/voice/avatars/research.png")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    # Bundled file is non-trivial size (vendored default).
    assert len(r.content) > 100


def test_avatar_endpoint_invalid_persona_id_400(dash_client: TestClient) -> None:
    """Persona id with shell metachars is rejected pre-FS lookup."""
    r = dash_client.get("/api/cabinet/voice/avatars/..%2Fetc%2Fpasswd.png")
    # FastAPI normalizes the URL — ends up either 400 (whitelist reject)
    # or 404 (file-not-found), both acceptable; never 200.
    assert r.status_code in {400, 404}


def test_avatar_endpoint_404_on_unknown_with_no_fallback(
    dash_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If both per-persona override and bundled file are missing, the
    handler returns 404 with detail='avatar_missing'.

    Force-miss by pointing _CABINET_VOICE_STATIC_DIR at an empty tmp dir.
    """
    import dashboard_api  # noqa: PLC0415
    empty_static = tmp_path / "empty-static"
    (empty_static / "avatars").mkdir(parents=True)
    monkeypatch.setattr(
        dashboard_api,
        "_CABINET_VOICE_STATIC_DIR",
        empty_static,
    )
    r = dash_client.get("/api/cabinet/voice/avatars/zzz_unknown.png")
    assert r.status_code == 404
    assert r.json()["detail"] == "avatar_missing"


# ─── B2 — Q4 canonical default lock (avatar URL emission) ────────────────


def test_voice_html_emits_canonical_default_not_main() -> None:
    """B2 Q4 lock — voice_html.get_voice_meeting_html() renders the
    default agent tile with canonical wire string ``default``, not
    upstream's ``main``. The avatar URL therefore points at
    /api/cabinet/voice/avatars/default.png and resolves through
    personas.load_persona_config('default') without a translation hop.
    """
    from cabinet.voice.voice_html import get_voice_meeting_html  # noqa: PLC0415
    html = get_voice_meeting_html(
        token="t",
        meeting_id=7,
        chat_id="c",
        ws_port=7860,
    )
    assert "avatars/default.png" in html, (
        "voice_html must emit canonical 'default' wire string for the main "
        "agent tile (B2 Q4 lock); upstream 'main' must NOT appear in the "
        "agent-card avatar URLs."
    )
    assert 'avatars/main.png?token=' not in html, (
        "voice_html must NOT emit the upstream 'main' wire string — Q4 "
        "translation locks the boundary at the HTML emission site."
    )


def test_avatar_endpoint_serves_default_png(dash_client: TestClient) -> None:
    """GET /api/cabinet/voice/avatars/default.png returns the bundled
    default avatar (copied from main.png in the B2 fix-pass).

    Note: the bundled main.png upstream is actually a JPEG with `.png`
    extension; the route serves the file as image/png regardless (matches
    upstream behavior). The M4 magic-byte check applies ONLY to the
    operator override path, NOT bundled defaults — bundled defaults are
    trusted (vendored at build time)."""
    r = dash_client.get("/api/cabinet/voice/avatars/default.png")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    # Sanity: the file is non-trivial size (the vendored default).
    assert len(r.content) > 100


# ─── M4 — PNG magic-byte verification on operator override ───────────────


def test_avatar_override_rejects_non_png(
    dash_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4 — if config.yaml.cabinet.avatar_path points at a non-PNG file
    (operator typo, symlink swap, etc.), the handler logs and falls
    through to the bundled default instead of streaming arbitrary bytes
    with image/png Content-Type.
    """
    import dashboard_api  # noqa: PLC0415

    bogus = tmp_path / "fake.png"
    bogus.write_bytes(b"<html>not a png</html>\x00\x00")

    def _fake_load(persona_id: str):
        return {"cabinet": {"avatar_path": str(bogus)}}

    def _fake_resolve(_persona_id: str) -> Path:
        return tmp_path

    monkeypatch.setattr(dashboard_api.personas, "load_persona_config", _fake_load)
    monkeypatch.setattr(dashboard_api, "resolve_profile_root", _fake_resolve)

    r = dash_client.get("/api/cabinet/voice/avatars/research.png")
    # Falls through to the bundled research.png. Bundled files are
    # vendored upstream and trusted at build time; the M4 magic-byte
    # check applies ONLY to operator overrides. The point of this test
    # is that the bogus override DID get rejected (we got the bundled
    # bytes, not the operator's text content).
    assert r.status_code == 200, r.text
    assert b"not a png" not in r.content
    assert len(r.content) > 100


# ─── M3 — broadcast_order populated at meeting create ────────────────────


def test_cabinet_new_populates_broadcast_order(
    dash_client: TestClient, tmp_path: Path
) -> None:
    """M3 — POST /api/cabinet/new writes a JSON-serialized broadcast_order
    snapshot to the cabinet_meetings row. Phase 6 voice subprocess reads
    this column to drive broadcast turns in stable order; without it the
    bridge falls back to a hardcoded constant that doesn't reflect the
    actual roster.
    """
    meeting_id = _create_meeting(dash_client, "tg-broadcast")
    db_path = config.DASHBOARD_DB_PATH
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT broadcast_order FROM cabinet_meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    raw = row[0]
    assert raw is not None, (
        "broadcast_order must be populated at create time, NOT NULL "
        "(M3 fix locked-in)."
    )
    parsed = json.loads(raw)
    assert isinstance(parsed, list), (
        f"broadcast_order must serialize to a JSON list; got {type(parsed).__name__}"
    )


def test_voice_server_load_broadcast_order_helper_handles_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3 — _load_broadcast_order_from_db returns None for pre-migration
    rows (NULL column) so the bridge falls back to the hardcoded
    BROADCAST_ORDER constant in agent_bridge.py instead of crashing.
    """
    db_path = tmp_path / "voice_server_helper.db"
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", str(db_path))
    from dashboard_db import get_connection as _get_conn  # noqa: PLC0415
    conn = _get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO cabinet_meetings (mode, chat_id) VALUES (?, ?)",
            ("text", "x"),
        )
        meeting_id = cur.lastrowid
        # Force broadcast_order=NULL (no second-arg in INSERT).
        conn.commit()
    finally:
        conn.close()
    from cabinet.voice.voice_server import _load_broadcast_order_from_db  # noqa: PLC0415
    assert _load_broadcast_order_from_db(meeting_id) is None


def test_voice_server_load_broadcast_order_returns_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3 — _load_broadcast_order_from_db parses populated JSON list."""
    db_path = tmp_path / "voice_server_helper2.db"
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", str(db_path))
    from dashboard_db import get_connection as _get_conn  # noqa: PLC0415
    conn = _get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO cabinet_meetings (mode, chat_id, broadcast_order) "
            "VALUES (?, ?, ?)",
            ("text", "x", json.dumps(["default", "research", "comms"])),
        )
        meeting_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    from cabinet.voice.voice_server import _load_broadcast_order_from_db  # noqa: PLC0415
    out = _load_broadcast_order_from_db(meeting_id)
    assert out == ["default", "research", "comms"]


# ─── B1 — orchestration auth middleware exemption (token modes) ──────────


@pytest.fixture
def auth_app_token_unset(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Orchestration app with ORCHESTRATION_API_TOKEN unset.

    Voice UI must pass through the auth middleware in loopback no-token
    mode — same shape as the rest of the local API.
    """
    monkeypatch.delenv("ORCHESTRATION_API_TOKEN", raising=False)
    import importlib  # noqa: PLC0415
    import orchestration.api as api_mod  # noqa: PLC0415
    importlib.reload(api_mod)
    return TestClient(api_mod.app)


@pytest.fixture
def auth_app_token_set(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Orchestration app with ORCHESTRATION_API_TOKEN=set-token.

    Voice UI passes only when query-param ``token`` matches the env value;
    other paths still require Authorization: Bearer.
    """
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", "set-token")
    import importlib  # noqa: PLC0415
    import orchestration.api as api_mod  # noqa: PLC0415
    importlib.reload(api_mod)
    return TestClient(api_mod.app)


def test_voice_ui_token_unset_loopback_passes(auth_app_token_unset: TestClient) -> None:
    """B1 — token-unset deployment: middleware is permissive on
    /api/cabinet/voice/* (loopback no-token mode)."""
    # We expect to reach the route handler, which then 404s because the
    # meeting does not exist. The point of this test is that we PASSED
    # the middleware (NOT 401).
    r = auth_app_token_unset.get(
        "/api/cabinet/voice/ui",
        params={"token": "", "meetingId": 99999, "chatId": ""},
    )
    assert r.status_code != 401, (
        f"Voice UI must NOT return 401 in token-unset mode. Got {r.status_code}: {r.text}"
    )


def test_voice_ui_token_set_correct_query_param_passes(
    auth_app_token_set: TestClient,
) -> None:
    """B1 — token-set deployment: query-param token matching env value
    passes the middleware."""
    r = auth_app_token_set.get(
        "/api/cabinet/voice/ui",
        params={"token": "set-token", "meetingId": 99999, "chatId": ""},
    )
    assert r.status_code != 401, (
        f"Voice UI with correct query-param token must NOT return 401. "
        f"Got {r.status_code}: {r.text}"
    )


def test_voice_ui_token_set_wrong_query_param_401(
    auth_app_token_set: TestClient,
) -> None:
    """B1 — token-set deployment: wrong query-param token is rejected
    BEFORE the route handler runs. Critical for the docstring claim
    that the middleware enforces the boundary."""
    r = auth_app_token_set.get(
        "/api/cabinet/voice/ui",
        params={"token": "wrong-token", "meetingId": 99999, "chatId": ""},
    )
    assert r.status_code == 401, (
        f"Voice UI with wrong query-param token MUST return 401. "
        f"Got {r.status_code}: {r.text}"
    )


def test_voice_client_bundle_token_set_wrong_query_param_401(
    auth_app_token_set: TestClient,
) -> None:
    """B1 — same auth contract applies to the bundle and avatar routes.
    All paths under /api/cabinet/voice/* honor the query-param token
    check uniformly."""
    r = auth_app_token_set.get(
        "/api/cabinet/voice/client.bundle.js",
        params={"token": "wrong-token"},
    )
    assert r.status_code == 401


# ─── B1-R2 — token URL-encoding in rendered HTML query strings ───────────


def test_voice_html_url_encodes_special_token_chars() -> None:
    """B1-R2 — voice_html.get_voice_meeting_html() must URL-encode the
    token before embedding it in bundle/avatar query strings, NOT just
    HTML-escape it.

    Class-of-bug: a token containing ``&`` or ``=`` would pass through
    ``html.escape`` unchanged (those chars are safe in HTML attribute
    context) but split the browser-parsed query string. Result: the
    bundle URL ``?token=a&b=c`` becomes ``token=a`` + ``b=c`` to the
    server, middleware sees ``token=a``, returns 401, browser fails to
    load the Pipecat client SDK.

    Fix locks: ``urllib.parse.quote(token, safe='')`` percent-encodes
    all reserved chars; the embedded URLs survive browser parsing.
    """
    from cabinet.voice.voice_html import get_voice_meeting_html  # noqa: PLC0415

    html = get_voice_meeting_html(
        token="a&b=c+d/e",
        meeting_id=7,
        chat_id="chat-x",
        ws_port=7860,
    )

    # Bundle URL must NOT contain raw ``&`` or ``=`` adjacent to the
    # token value (those would split the query). The percent-encoded
    # form is ``a%26b%3Dc%2Bd%2Fe``.
    assert "client.bundle.js?token=a%26b%3Dc%2Bd%2Fe" in html, (
        "Bundle URL must URL-encode special chars in token query value. "
        "Found bundle script tag does not contain percent-encoded form."
    )
    # Avatar URLs (per agent tile) must use the same percent-encoded form.
    assert "avatars/default.png?token=a%26b%3Dc%2Bd%2Fe" in html, (
        "Avatar URL must URL-encode special chars in token query value. "
        "Found agent tile avatar URL does not contain percent-encoded form."
    )
    # Defense: NO raw ``token=a&b=c`` query split should appear in the
    # bundle/avatar URLs. (The inline JS uses encodeURIComponent at
    # runtime — that path is fine; the server-rendered URLs are the
    # ones that needed the fix.)
    assert 'client.bundle.js?token=a&amp;b=c' not in html, (
        "Bundle URL still has HTML-escaped raw '&' — that browser-decodes "
        "to ?token=a&b=c which fails the middleware token check."
    )


# ─── R2-M1 — avatar fallback precedence (default.png before main.png) ────


def test_avatar_fallback_chain_includes_default_png(
    dash_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """R2-M1 — when a persona has no bundled avatar, the lookup
    precedence is:

        operator-override → bundled {persona}.png → bundled default.png →
        bundled main.png → 404

    Q4 added ``default.png`` as the canonical fallback. The handler must
    try it BEFORE ``main.png`` so unknown personas hit the canonical
    Q4 fallback even when ``main.png`` is later removed.
    """
    from dashboard_api import _CABINET_VOICE_STATIC_DIR  # noqa: PLC0415

    avatars_dir = _CABINET_VOICE_STATIC_DIR / "avatars"
    default_png = avatars_dir / "default.png"
    assert default_png.is_file(), (
        "default.png must exist as the Q4 canonical fallback — added in "
        "fix-pass commit 3be0c17."
    )

    # Request a persona id that has no bundled file. The handler should
    # serve default.png (200 + image/png), not 404.
    r = dash_client.get("/api/cabinet/voice/avatars/UnknownPersonaXYZ.png")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    # Body must equal the default.png bytes (proving fallback hit
    # default.png and not main.png — they're byte-equal copies, but the
    # docstring lookup order says default.png is step 3, main.png is
    # step 4, and the test asserts step 3 fires).
    assert r.content == default_png.read_bytes(), (
        "Unknown-persona avatar request must serve default.png bytes "
        "(Q4 canonical fallback, step 3 in lookup precedence)."
    )


# ─── Phase 6 follow-up: dynamic UI tile roster resolution ────────────────


def test_voice_ui_renders_tiles_from_broadcast_order_snapshot(
    dash_client: TestClient,
) -> None:
    """PRD-8 Phase 6 follow-up 2026-05-10 — the voice UI page must render
    tiles from the meeting's ``broadcast_order`` snapshot (NOT the
    hardcoded ClaudeClaw 5-stub default).

    Closes the UI-vs-routing gap surfaced by the live-test verification:
    previously the page showed Research/Comms/Content/Ops tiles even
    when only Main was registered, leading to 'Research is typing'
    indicators on personas that never received turns.
    """
    meeting_id = _create_meeting(dash_client, "tg-tiles")
    r = dash_client.get(
        "/api/cabinet/voice/ui",
        params={"token": "", "meetingId": meeting_id, "chatId": "tg-tiles"},
    )
    assert r.status_code == 200, r.text
    html = r.text
    # Inspect what the meeting's broadcast_order actually contains.
    db_path = config.DASHBOARD_DB_PATH
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT broadcast_order FROM cabinet_meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    broadcast_ids = json.loads(row[0]) if row[0] else []
    # Every id in broadcast_order should appear as a tile.
    for pid in broadcast_ids:
        assert f'id="agent-{pid}"' in html, (
            f"Tile for persona {pid!r} (from broadcast_order snapshot) "
            f"missing from voice UI HTML."
        )
    # When broadcast_order has fewer than 5 personas (the hardcoded
    # default count), the stub personas NOT in the snapshot must NOT
    # appear — otherwise we're back to the UI-vs-routing gap.
    if len(broadcast_ids) < 5:
        stub_ids = {"research", "comms", "content", "ops"}
        snapshot_ids = set(broadcast_ids)
        for stub in stub_ids - snapshot_ids:
            assert f'id="agent-{stub}"' not in html, (
                f"Stub tile {stub!r} rendered despite NOT being in "
                f"broadcast_order snapshot {broadcast_ids!r} — UI-vs-routing "
                f"gap reopened."
            )


def test_voice_ui_falls_back_to_default_when_broadcast_order_null(
    dash_client: TestClient, tmp_path: Path
) -> None:
    """Backwards-compat: meetings created BEFORE the Phase 6
    ``broadcast_order`` migration land had NULL in the column. The voice
    UI must still render with the hardcoded 5-stub default for those
    pre-migration meetings. Simulated by manually NULL-ing the column
    on a freshly-created meeting."""
    meeting_id = _create_meeting(dash_client, "tg-null-bo")
    # Force broadcast_order to NULL to simulate a pre-Phase-6 meeting.
    db_path = config.DASHBOARD_DB_PATH
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE cabinet_meetings SET broadcast_order = NULL WHERE id = ?",
            (meeting_id,),
        )
        conn.commit()
    finally:
        conn.close()
    r = dash_client.get(
        "/api/cabinet/voice/ui",
        params={"token": "", "meetingId": meeting_id, "chatId": "tg-null-bo"},
    )
    assert r.status_code == 200, r.text
    html = r.text
    # Hardcoded 5-stub default must render verbatim for pre-Phase-6 meetings.
    for agent_id in ("default", "research", "comms", "content", "ops"):
        assert f'id="agent-{agent_id}"' in html, (
            f"Hardcoded stub {agent_id!r} missing from fallback render "
            f"when broadcast_order=NULL (backwards-compat for pre-Phase-6 meetings)."
        )
