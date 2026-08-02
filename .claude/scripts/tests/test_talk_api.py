"""Talk mode endpoint tests — /api/talk/status + /api/talk/session.

TestClient against a bare FastAPI app mounting talk_api.router (the
orchestration auth middleware is covered separately by the cabinet voice
auth tests). The upstream client-secret POST is stubbed; Codex OAuth is
exercised through a temp CODEX_HOME auth.json — no network, no secrets.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import talk_session


def _jwt(exp: int) -> str:
    def _b64(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{_b64({'alg': 'none'})}.{_b64({'sub': 't', 'exp': exp})}.sig"


@pytest.fixture
def talk_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Bare app + talk router with hermetic env and a stub soul file."""

    monkeypatch.delenv("TALK_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TALK_OPENAI_MODEL", raising=False)
    monkeypatch.delenv("TALK_OPENAI_VOICE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_VOICE", raising=False)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    soul = tmp_path / "SOUL.md"
    soul.write_text("# SOUL\nYou keep owner's operating system honest.", encoding="utf-8")
    monkeypatch.setattr(config, "SOUL_FILE", soul)
    # The voice prompt reads identity files through cognition.identity_payload,
    # which resolves them from MEMORY_DIR — patching SOUL_FILE alone would let
    # this test read the REAL vault and assert against whatever happens to be
    # in it.
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)

    import talk_api  # noqa: PLC0415

    app = FastAPI()
    app.include_router(talk_api.router)
    return TestClient(app)


def _codex_login(monkeypatch: pytest.MonkeyPatch, *, expired: bool = False) -> None:
    codex_home = Path(os.environ["CODEX_HOME"])
    exp = int(time.time()) + (-600 if expired else 3600)
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": _jwt(exp),
                    "refresh_token": "rt-1",
                    "account_id": "acct-1",
                },
            }
        ),
        encoding="utf-8",
    )


def _stub_mint(
    monkeypatch: pytest.MonkeyPatch, payload: dict | None = None
) -> list[dict]:
    """Stub the upstream mint; return a list capturing (token, session) calls."""

    calls: list[dict] = []

    def fake_post(auth_token: str, session: dict) -> dict:
        calls.append({"token": auth_token, "session": session})
        return payload or {"client_secret": {"value": "ek-test-secret", "expires_at": 1_893_456_000}}

    monkeypatch.setattr(talk_session, "_post_client_secret", fake_post)
    return calls


# ─── /api/talk/status ────────────────────────────────────────────────────


def test_status_not_configured(talk_client: TestClient) -> None:
    r = talk_client.get("/api/talk/status")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["configured"] is False
    assert body["source"] is None
    assert body["model"] == talk_session.DEFAULT_TALK_MODEL
    assert body["voice"] == talk_session.DEFAULT_TALK_VOICE
    assert "cedar" in body["voices"]
    assert body["killSwitchVoiceDisabled"] is False
    assert "token" not in json.dumps(body).lower()


def test_status_reports_codex_oauth(talk_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _codex_login(monkeypatch)

    body = talk_client.get("/api/talk/status").json()

    assert body["configured"] is True
    assert body["source"] == "codex-oauth"


# ─── /api/talk/session ───────────────────────────────────────────────────


def test_session_mint_with_configured_key(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TALK_OPENAI_API_KEY", "sk-talk-configured")
    calls = _stub_mint(monkeypatch)

    r = talk_client.post("/api/talk/session", json={})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["clientSecret"] == "ek-test-secret"
    assert body["expiresAt"] == 1_893_456_000_000
    assert body["offerUrl"] == "https://api.openai.com/v1/realtime/calls"
    assert body["model"] == talk_session.DEFAULT_TALK_MODEL
    assert body["voice"] == talk_session.DEFAULT_TALK_VOICE
    assert body["authSource"] == "configured"

    mint = calls[0]
    assert mint["token"] == "sk-talk-configured"
    session = mint["session"]
    assert session["type"] == "realtime"
    assert session["model"] == talk_session.DEFAULT_TALK_MODEL
    assert "speaking live over a voice call" in session["instructions"]
    assert "keep owner's operating system honest" in session["instructions"]
    assert session["audio"]["input"]["turn_detection"]["type"] == "server_vad"
    assert session["audio"]["input"]["transcription"]["model"] == "gpt-4o-mini-transcribe"
    assert session["audio"]["output"]["voice"] == talk_session.DEFAULT_TALK_VOICE


def test_session_mint_via_codex_oauth(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No API keys anywhere -> external Codex CLI login mints the secret."""

    _codex_login(monkeypatch)
    calls = _stub_mint(monkeypatch, payload={"value": "ek-oauth", "expires_at": 1_893_456_000})

    r = talk_client.post("/api/talk/session", json={})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["authSource"] == "codex-oauth"
    assert body["clientSecret"] == "ek-oauth"
    assert calls[0]["token"].split(".")[0]  # the JWT access token reached upstream


def test_session_voice_and_model_overrides(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TALK_OPENAI_API_KEY", "sk-talk")
    _stub_mint(monkeypatch)

    r = talk_client.post("/api/talk/session", json={"voice": "marin", "model": "gpt-realtime-x"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["voice"] == "marin"
    assert body["model"] == "gpt-realtime-x"


def test_session_rejects_unknown_voice(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TALK_OPENAI_API_KEY", "sk-talk")
    _stub_mint(monkeypatch)

    r = talk_client.post("/api/talk/session", json={"voice": "not-a-voice"})

    assert r.status_code == 400
    assert "not-a-voice" in r.json()["detail"]["error"]


def test_session_without_any_auth_is_503(talk_client: TestClient) -> None:
    r = talk_client.post("/api/talk/session", json={})

    assert r.status_code == 503
    assert "requires an OpenAI API key or Codex OAuth" in r.json()["detail"]["error"]


def test_session_upstream_401_has_remediation(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TALK_OPENAI_API_KEY", "sk-talk")

    def fake_post(auth_token: str, session: dict) -> dict:
        raise talk_session.TalkUpstreamError("OpenAI Realtime client secret failed (401): nope")

    monkeypatch.setattr(talk_session, "_post_client_secret", fake_post)

    r = talk_client.post("/api/talk/session", json={})

    assert r.status_code == 502
    assert "configured OpenAI API key was rejected" in r.json()["detail"]["error"]


def test_session_blocked_by_voice_killswitch(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMIE_KILLSWITCH_VOICE", "disabled")
    monkeypatch.setenv("TALK_OPENAI_API_KEY", "sk-talk")

    r = talk_client.post("/api/talk/session", json={})

    assert r.status_code == 503
    assert r.json()["detail"]["switch"] == "voice"


# ─── /api/talk/tool ──────────────────────────────────────────────────────


def test_tool_route_executes_and_returns_output(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import talk_api  # noqa: PLC0415

    monkeypatch.setattr(
        talk_api.talk_tools,
        "execute_talk_tool",
        lambda name, arguments: f"ran {name} with {arguments}",
    )

    r = talk_client.post(
        "/api/talk/tool", json={"name": "memory_search", "arguments": {"query": "lane"}}
    )

    assert r.status_code == 200
    assert r.json()["output"] == "ran memory_search with {'query': 'lane'}"


def test_tool_route_unknown_tool_is_400(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import talk_api  # noqa: PLC0415
    import talk_tools  # noqa: PLC0415

    def raise_unknown(name, arguments):
        raise talk_tools.TalkToolError(f"unknown talk tool: {name!r}")

    monkeypatch.setattr(talk_api.talk_tools, "execute_talk_tool", raise_unknown)

    r = talk_client.post("/api/talk/tool", json={"name": "nuke_everything", "arguments": {}})

    assert r.status_code == 400
    assert "unknown talk tool" in r.json()["detail"]["error"]


def test_tool_route_blocked_by_voice_killswitch(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMIE_KILLSWITCH_VOICE", "disabled")

    r = talk_client.post("/api/talk/tool", json={"name": "memory_search", "arguments": {}})

    assert r.status_code == 503
    assert r.json()["detail"]["switch"] == "voice"


def test_flush_route_passes_transcript_and_returns_receipt(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import talk_api  # noqa: PLC0415

    seen: dict = {}

    def fake_start(transcript, *, session_id, started_at):
        seen.update(
            transcript=transcript, session_id=session_id, started_at=started_at
        )
        return {"status": "started", "contextFile": "session-flush-talk-x-20260801-100000.md"}

    monkeypatch.setattr(talk_api.talk_flush, "start_session_flush", fake_start)

    r = talk_client.post(
        "/api/talk/flush",
        json={
            "sessionId": "abc",
            "startedAt": "2026-08-01T10:00:00Z",
            "transcript": [
                {"role": "user", "text": "hello"},
                {"role": "assistant", "text": "hey"},
            ],
        },
    )

    assert r.status_code == 200
    assert r.json() == {
        "ok": True,
        "status": "started",
        "contextFile": "session-flush-talk-x-20260801-100000.md",
    }
    assert seen["session_id"] == "abc"
    assert seen["started_at"] == "2026-08-01T10:00:00Z"
    assert seen["transcript"] == [
        {"role": "user", "text": "hello"},
        {"role": "assistant", "text": "hey"},
    ]


def test_flush_route_tolerates_empty_body(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Teardown fire-and-forget must never 422 — every field is optional."""

    import talk_api  # noqa: PLC0415

    monkeypatch.setattr(
        talk_api.talk_flush,
        "start_session_flush",
        lambda transcript, *, session_id, started_at: {
            "status": "skipped",
            "reason": "fewer than 2 turns",
        },
    )

    r = talk_client.post("/api/talk/flush", json={})

    assert r.status_code == 200
    assert r.json()["status"] == "skipped"


def test_flush_route_blocked_by_voice_killswitch(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMIE_KILLSWITCH_VOICE", "disabled")

    r = talk_client.post("/api/talk/flush", json={"sessionId": "abc", "transcript": []})

    assert r.status_code == 503
    assert r.json()["detail"]["switch"] == "voice"


def test_skill_run_route_returns_run_state(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import talk_api  # noqa: PLC0415

    monkeypatch.setattr(
        talk_api.talk_tools,
        "get_skill_run",
        lambda run_id: {"status": "done", "output": "digest", "skill": "vault-ops", "input": "", "ts": 1.0},
    )

    r = talk_client.get("/api/talk/skill-runs/7")

    assert r.status_code == 200
    body = r.json()
    assert body["runId"] == 7
    assert body["status"] == "done"
    assert body["output"] == "digest"


def test_skill_run_route_unknown_id_is_404(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import talk_api  # noqa: PLC0415

    monkeypatch.setattr(talk_api.talk_tools, "get_skill_run", lambda run_id: None)

    r = talk_client.get("/api/talk/skill-runs/999")

    assert r.status_code == 404


def test_run_route_returns_run_state(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import talk_api  # noqa: PLC0415

    monkeypatch.setattr(
        talk_api.talk_runs,
        "get_run",
        lambda run_id: {
            "kind": "archon",
            "label": "archon-clutch",
            "status": "done",
            "output": "finished with status completed",
            "meta": {"archon_run_id": "run-9"},
            "ts": 1.0,
            "updated": 2.0,
        },
    )

    r = talk_client.get("/api/talk/runs/12")

    assert r.status_code == 200
    body = r.json()
    assert body["runId"] == 12
    assert body["kind"] == "archon"
    assert body["status"] == "done"
    assert "completed" in body["output"]


def test_run_route_unknown_id_is_404(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import talk_api  # noqa: PLC0415

    monkeypatch.setattr(talk_api.talk_runs, "get_run", lambda run_id: None)

    r = talk_client.get("/api/talk/runs/999")

    assert r.status_code == 404
    assert "unknown run" in r.json()["detail"]["error"]


def test_runs_list_route(talk_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import talk_api  # noqa: PLC0415

    monkeypatch.setattr(
        talk_api.talk_runs,
        "list_runs",
        lambda limit=10, include_history=False: [
            {"runId": 2, "kind": "agent", "label": "audit", "status": "running"}
        ],
    )

    r = talk_client.get("/api/talk/runs")

    assert r.status_code == 200
    assert r.json()["runs"][0]["kind"] == "agent"


def test_runs_list_route_forwards_limit_and_history(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Runs panel's ?limit=50&history=true reaches the registry intact."""

    import talk_api  # noqa: PLC0415

    seen: dict = {}

    def fake_list(limit=10, include_history=False):
        seen.update(limit=limit, include_history=include_history)
        return []

    monkeypatch.setattr(talk_api.talk_runs, "list_runs", fake_list)

    r = talk_client.get("/api/talk/runs?limit=50&history=true")

    assert r.status_code == 200
    assert seen == {"limit": 50, "include_history": True}


def test_a_per_tool_kill_switch_is_503_not_500(
    talk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codex R4 major: a switch firing INSIDE a handler must not 500.

    The voice guard runs BEFORE the handler, so a per-tool switch (e.g.
    archon_dispatch) raises after it — and the route mapped only
    TalkToolError, so KillSwitchDisabled escaped unhandled and took the whole
    Realtime tool channel down with it.
    """

    import talk_tools
    from security import kill_switches

    def boom(_name, _args):
        raise kill_switches.KillSwitchDisabled(
            switch_name="archon_dispatch",
            reason="kill-switch 'archon_dispatch' is disabled by operator",
        )

    monkeypatch.setattr(talk_tools, "execute_talk_tool", boom)

    response = talk_client.post(
        "/api/talk/tool", json={"name": "run_archon", "arguments": {}}
    )

    assert response.status_code == 503
    assert response.json()["detail"]["switch"] == "archon_dispatch"
