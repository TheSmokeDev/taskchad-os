"""Tests for runtime.openai_platform_auth — OpenClaw PR #100671 port.

Covers the fail-closed auth ordering for OpenAI Realtime voice:
configured key -> OPENAI_API_KEY env -> external Codex CLI OAuth login,
including JWT expiry handling, token refresh, write-back, and the
refresh-token race re-read.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from runtime import openai_platform_auth as opa


def _jwt(exp: int | None = None) -> str:
    """Build an unsigned JWT-shaped token with an optional exp claim."""

    def _b64(data: dict) -> str:
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    payload = {"sub": "test"} if exp is None else {"sub": "test", "exp": exp}
    return f"{_b64({'alg': 'none'})}.{_b64(payload)}.sig"


def _write_auth_json(
    path: Path,
    *,
    access: str,
    refresh: str = "rt-1",
    auth_mode: str = "chatgpt",
    account_id: str = "acct-1",
) -> None:
    path.write_text(
        json.dumps(
            {
                "auth_mode": auth_mode,
                "tokens": {
                    "id_token": "id-1",
                    "access_token": access,
                    "refresh_token": refresh,
                    "account_id": account_id,
                },
                "last_refresh": "2026-07-24T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    home = tmp_path / ".codex"
    home.mkdir()
    return home


# ─── Ordering ────────────────────────────────────────────────────────────


def test_configured_key_wins_over_env_and_oauth(codex_home: Path) -> None:
    _write_auth_json(codex_home / "auth.json", access=_jwt(int(time.time()) + 3600))

    auth = opa.resolve_openai_platform_auth(
        configured_api_key="sk-configured",
        env={"OPENAI_API_KEY": "sk-env"},
        codex_home=codex_home,
    )

    assert auth.token == "sk-configured"
    assert auth.source == opa.SOURCE_CONFIGURED


def test_blank_configured_key_fails_closed(codex_home: Path) -> None:
    """A configured-but-empty key blocks env AND OAuth fallback."""

    _write_auth_json(codex_home / "auth.json", access=_jwt(int(time.time()) + 3600))

    with pytest.raises(opa.OpenAIPlatformAuthError, match="set but empty"):
        opa.resolve_openai_platform_auth(
            configured_api_key="   ",
            env={"OPENAI_API_KEY": "sk-env"},
            codex_home=codex_home,
        )


def test_env_key_used_when_no_configured(codex_home: Path) -> None:
    auth = opa.resolve_openai_platform_auth(env={"OPENAI_API_KEY": "sk-env"}, codex_home=codex_home)

    assert auth.token == "sk-env"
    assert auth.source == opa.SOURCE_ENV


def test_codex_oauth_fallback_when_no_keys(codex_home: Path) -> None:
    access = _jwt(int(time.time()) + 3600)
    _write_auth_json(codex_home / "auth.json", access=access)

    auth = opa.resolve_openai_platform_auth(env={}, codex_home=codex_home)

    assert auth.token == access
    assert auth.source == opa.SOURCE_CODEX_OAUTH
    assert auth.expires_at is not None


def test_missing_everything_raises(codex_home: Path) -> None:
    with pytest.raises(opa.OpenAIPlatformAuthError, match="requires an OpenAI API key or Codex OAuth"):
        opa.resolve_openai_platform_auth(env={}, codex_home=codex_home)


def test_apikey_mode_auth_json_is_not_an_oauth_fallback(codex_home: Path) -> None:
    """Codex CLI api-key logins do not count as OAuth profiles."""

    (codex_home / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-codex-file"}),
        encoding="utf-8",
    )

    with pytest.raises(opa.OpenAIPlatformAuthError):
        opa.resolve_openai_platform_auth(env={}, codex_home=codex_home)


# ─── Expiry + refresh ────────────────────────────────────────────────────


def test_expired_token_refreshes_and_writes_back(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expired = _jwt(int(time.time()) - 60)
    _write_auth_json(codex_home / "auth.json", access=expired, refresh="rt-old")
    new_access = _jwt(int(time.time()) + 3600)
    posted: list[dict[str, str]] = []

    def fake_post(fields: dict[str, str]) -> dict:
        posted.append(fields)
        return {
            "access_token": new_access,
            "refresh_token": "rt-new",
            "expires_in": 3600,
            "id_token": "id-2",
        }

    monkeypatch.setattr(opa, "_post_token_form", fake_post)

    auth = opa.resolve_openai_platform_auth(env={}, codex_home=codex_home)

    assert auth.token == new_access
    assert posted == [
        {
            "grant_type": "refresh_token",
            "refresh_token": "rt-old",
            "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        }
    ]
    written = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    assert written["tokens"]["access_token"] == new_access
    assert written["tokens"]["refresh_token"] == "rt-new"
    assert written["tokens"]["id_token"] == "id-2"
    assert written["tokens"]["account_id"] == "acct-1"
    assert "last_refresh" in written


def test_refresh_race_rereads_cli_refreshed_file(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single-use refresh token consumed by the CLI concurrently: a failing
    refresh re-reads auth.json and accepts a newer valid token."""

    expired = _jwt(int(time.time()) - 60)
    auth_path = codex_home / "auth.json"
    _write_auth_json(auth_path, access=expired, refresh="rt-old")
    cli_refreshed = _jwt(int(time.time()) + 3600)

    def fake_post(fields: dict[str, str]) -> dict:
        # CLI wins the race and persists its own fresh pair before we retry.
        _write_auth_json(auth_path, access=cli_refreshed, refresh="rt-cli")
        raise opa.OpenAIPlatformAuthError("refresh failed (400): refresh_token_reused")

    monkeypatch.setattr(opa, "_post_token_form", fake_post)

    auth = opa.resolve_openai_platform_auth(env={}, codex_home=codex_home)

    assert auth.token == cli_refreshed
    assert auth.source == opa.SOURCE_CODEX_OAUTH


def test_refresh_failure_guides_to_codex_login(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_auth_json(codex_home / "auth.json", access=_jwt(int(time.time()) - 60))

    def fake_post(fields: dict[str, str]) -> dict:
        raise opa.OpenAIPlatformAuthError("refresh failed (401): invalid_grant")

    monkeypatch.setattr(opa, "_post_token_form", fake_post)

    with pytest.raises(opa.OpenAIPlatformAuthError, match="codex login"):
        opa.resolve_openai_platform_auth(env={}, codex_home=codex_home)


def test_refresh_missing_fields_raises(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_auth_json(codex_home / "auth.json", access=_jwt(int(time.time()) - 60))
    monkeypatch.setattr(opa, "_post_token_form", lambda fields: {"access_token": "x"})

    with pytest.raises(opa.OpenAIPlatformAuthError, match="missing fields"):
        opa.resolve_openai_platform_auth(env={}, codex_home=codex_home)


def test_non_jwt_access_token_uses_fallback_expiry(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opaque access tokens get mtime/last_refresh + 1h expiry; a fresh file
    is treated as valid without a refresh call."""

    auth_path = codex_home / "auth.json"
    _write_auth_json(auth_path, access="opaque-token")
    monkeypatch.setattr(
        opa,
        "_post_token_form",
        lambda fields: pytest.fail("refresh must not fire for a fresh file"),
    )

    auth = opa.resolve_openai_platform_auth(env={}, codex_home=codex_home)

    assert auth.token == "opaque-token"


# ─── Status (no secrets) ─────────────────────────────────────────────────


def test_status_reports_each_source(codex_home: Path) -> None:
    assert opa.openai_platform_auth_status(
        configured_api_key="sk-x", env={}, codex_home=codex_home
    )["source"] == opa.SOURCE_CONFIGURED
    assert opa.openai_platform_auth_status(env={"OPENAI_API_KEY": "sk-env"}, codex_home=codex_home)[
        "source"
    ] == opa.SOURCE_ENV

    _write_auth_json(codex_home / "auth.json", access=_jwt(int(time.time()) + 3600))
    oauth_status = opa.openai_platform_auth_status(env={}, codex_home=codex_home)
    assert oauth_status["source"] == opa.SOURCE_CODEX_OAUTH
    assert "valid" in oauth_status["detail"]

    empty = opa.openai_platform_auth_status(env={}, codex_home=codex_home / "missing")
    assert empty["configured"] is False
    assert empty["source"] is None


def test_status_marks_expired_token_for_refresh(codex_home: Path) -> None:
    _write_auth_json(codex_home / "auth.json", access=_jwt(int(time.time()) - 60))

    status = opa.openai_platform_auth_status(env={}, codex_home=codex_home)

    assert status["source"] == opa.SOURCE_CODEX_OAUTH
    assert "expired-will-refresh" in status["detail"]
