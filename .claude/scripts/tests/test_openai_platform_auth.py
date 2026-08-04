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

import httpx
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


# ─── prefer_codex: the billing directive (voice-scoped, fail-closed) ──────
#
# Setting the flag means "use my subscription, do not meter me". A missing or
# broken Codex login therefore FAILS CLOSED — silently spending the operator's
# API-key credits is the exact surprise the directive exists to prevent, and a
# dead voice surface is the lesser harm. Same shape as the set-but-blank
# configured key, which has always failed closed rather than falling through.


def _assert_names_both_remedies(exc: Exception) -> None:
    text = str(exc)
    assert "codex login" in text, "must name the subscription remedy"
    assert "TALK_PREFER_CODEX_OAUTH" in text, "must name the stand-down remedy"


def test_prefer_codex_off_is_byte_identical_key_first(codex_home: Path) -> None:
    """Default-off parity: explicitly passing False changes nothing."""

    _write_auth_json(codex_home / "auth.json", access=_jwt(int(time.time()) + 3600))

    auth = opa.resolve_openai_platform_auth(
        env={"OPENAI_API_KEY": "sk-env"}, codex_home=codex_home, prefer_codex=False
    )

    assert auth.source == opa.SOURCE_ENV
    assert auth.token == "sk-env"


def test_prefer_codex_beats_env_key(codex_home: Path) -> None:
    """The money case: OPENAI_API_KEY stays set for other subsystems, voice
    still rides the subscription."""

    access = _jwt(int(time.time()) + 3600)
    _write_auth_json(codex_home / "auth.json", access=access)

    auth = opa.resolve_openai_platform_auth(
        env={"OPENAI_API_KEY": "sk-env"}, codex_home=codex_home, prefer_codex=True
    )

    assert auth.source == opa.SOURCE_CODEX_OAUTH
    assert auth.token == access


def test_prefer_codex_beats_configured_key(codex_home: Path) -> None:
    """The directive outranks ANY key, including the Talk-scoped one."""

    access = _jwt(int(time.time()) + 3600)
    _write_auth_json(codex_home / "auth.json", access=access)

    auth = opa.resolve_openai_platform_auth(
        configured_api_key="sk-configured",
        env={"OPENAI_API_KEY": "sk-env"},
        codex_home=codex_home,
        prefer_codex=True,
    )

    assert auth.source == opa.SOURCE_CODEX_OAUTH
    assert auth.token == access


def test_prefer_codex_fails_closed_when_there_is_no_oauth_login(codex_home: Path) -> None:
    """A usable key is NOT a fallback here — using it would meter the operator
    who explicitly asked not to be metered."""

    with pytest.raises(opa.OpenAIPlatformAuthError) as caught:
        opa.resolve_openai_platform_auth(
            configured_api_key="sk-configured",
            env={"OPENAI_API_KEY": "sk-env"},
            codex_home=codex_home,
            prefer_codex=True,
        )

    _assert_names_both_remedies(caught.value)
    assert "sk-configured" not in str(caught.value)
    assert "sk-env" not in str(caught.value)


def test_prefer_codex_fails_closed_when_the_login_is_apikey_mode(codex_home: Path) -> None:
    """An api-key-mode Codex login is not a subscription — still closed."""

    (codex_home / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-codex-file"}),
        encoding="utf-8",
    )

    with pytest.raises(opa.OpenAIPlatformAuthError) as caught:
        opa.resolve_openai_platform_auth(
            configured_api_key="sk-configured", codex_home=codex_home, prefer_codex=True
        )

    _assert_names_both_remedies(caught.value)


def test_prefer_codex_fails_closed_when_the_refresh_fails(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead refresh token stops voice; it does not silently start metering."""

    _write_auth_json(codex_home / "auth.json", access=_jwt(int(time.time()) - 60))
    calls: list[dict[str, str]] = []

    def fake_post(fields: dict[str, str]) -> dict:
        calls.append(fields)
        raise opa.OpenAIPlatformAuthError("refresh failed (401): invalid_grant")

    monkeypatch.setattr(opa, "_post_token_form", fake_post)

    with pytest.raises(opa.OpenAIPlatformAuthError) as caught:
        opa.resolve_openai_platform_auth(
            env={"OPENAI_API_KEY": "sk-env"}, codex_home=codex_home, prefer_codex=True
        )

    _assert_names_both_remedies(caught.value)
    assert len(calls) == 1, "one refresh attempt, no retry storm"


def test_prefer_codex_fails_closed_when_the_refresh_call_cannot_connect(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh is a network call — an outage must not read as a meter-me."""

    _write_auth_json(codex_home / "auth.json", access=_jwt(int(time.time()) - 60))
    monkeypatch.setattr(
        opa,
        "_post_token_form",
        lambda fields: (_ for _ in ()).throw(httpx.ConnectError("connection refused")),
    )

    with pytest.raises(opa.OpenAIPlatformAuthError) as caught:
        opa.resolve_openai_platform_auth(
            env={"OPENAI_API_KEY": "sk-env"}, codex_home=codex_home, prefer_codex=True
        )

    _assert_names_both_remedies(caught.value)


def test_prefer_codex_fails_closed_when_the_refresh_call_times_out(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_auth_json(codex_home / "auth.json", access=_jwt(int(time.time()) - 60))
    monkeypatch.setattr(
        opa,
        "_post_token_form",
        lambda fields: (_ for _ in ()).throw(httpx.TimeoutException("timed out")),
    )

    with pytest.raises(opa.OpenAIPlatformAuthError) as caught:
        opa.resolve_openai_platform_auth(
            configured_api_key="sk-configured", codex_home=codex_home, prefer_codex=True
        )

    _assert_names_both_remedies(caught.value)


def test_prefer_codex_fails_closed_when_the_stored_expiry_is_absurd(
    codex_home: Path,
) -> None:
    """A corrupt auth.json overflows the timestamp math instead of raising an
    auth error — it must still be a named refusal, not a 500 and not a meter."""

    _write_auth_json(codex_home / "auth.json", access=_jwt(10**400))

    with pytest.raises(opa.OpenAIPlatformAuthError) as caught:
        opa.resolve_openai_platform_auth(
            env={"OPENAI_API_KEY": "sk-env"}, codex_home=codex_home, prefer_codex=True
        )

    _assert_names_both_remedies(caught.value)


def test_prefer_codex_fails_closed_when_the_refresh_returns_an_absurd_expiry(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_auth_json(codex_home / "auth.json", access=_jwt(int(time.time()) - 60))
    monkeypatch.setattr(
        opa,
        "_post_token_form",
        lambda fields: {
            "access_token": "opaque",
            "refresh_token": "rt-new",
            "expires_in": float("inf"),
        },
    )

    with pytest.raises(opa.OpenAIPlatformAuthError) as caught:
        opa.resolve_openai_platform_auth(
            configured_api_key="sk-configured", codex_home=codex_home, prefer_codex=True
        )

    _assert_names_both_remedies(caught.value)


def test_prefer_codex_refreshes_an_expired_login_rather_than_refusing(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed is the LAST resort — a refreshable login is still used."""

    _write_auth_json(codex_home / "auth.json", access=_jwt(int(time.time()) - 60))
    new_access = _jwt(int(time.time()) + 3600)
    monkeypatch.setattr(
        opa,
        "_post_token_form",
        lambda fields: {
            "access_token": new_access,
            "refresh_token": "rt-new",
            "expires_in": 3600,
        },
    )

    auth = opa.resolve_openai_platform_auth(
        env={"OPENAI_API_KEY": "sk-env"}, codex_home=codex_home, prefer_codex=True
    )

    assert auth.source == opa.SOURCE_CODEX_OAUTH
    assert auth.token == new_access


def test_prefer_codex_ignores_a_blank_configured_key_when_the_login_works(
    codex_home: Path,
) -> None:
    """The key legs are never consulted under the directive — not even to
    fail on a misconfigured one."""

    access = _jwt(int(time.time()) + 3600)
    _write_auth_json(codex_home / "auth.json", access=access)

    auth = opa.resolve_openai_platform_auth(
        configured_api_key="   ",
        env={"OPENAI_API_KEY": "sk-env"},
        codex_home=codex_home,
        prefer_codex=True,
    )

    assert auth.source == opa.SOURCE_CODEX_OAUTH


def test_a_blank_configured_key_still_fails_closed_with_the_preference_off(
    codex_home: Path,
) -> None:
    """Parity guard for the sibling behaviour this one is modelled on."""

    _write_auth_json(codex_home / "auth.json", access=_jwt(int(time.time()) + 3600))

    with pytest.raises(opa.OpenAIPlatformAuthError, match="set but empty"):
        opa.resolve_openai_platform_auth(
            configured_api_key="   ",
            env={"OPENAI_API_KEY": "sk-env"},
            codex_home=codex_home,
        )


def test_resolver_never_reads_the_preference_from_the_environment(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope proof: a non-voice caller that does not pass the flag keeps the
    key-first order even with the operator knob exported process-wide."""

    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "true")
    _write_auth_json(codex_home / "auth.json", access=_jwt(int(time.time()) + 3600))

    auth = opa.resolve_openai_platform_auth(
        env={"OPENAI_API_KEY": "sk-env"}, codex_home=codex_home
    )

    assert auth.source == opa.SOURCE_ENV


def test_status_mirrors_the_preference(codex_home: Path) -> None:
    """Operator status must not contradict what the next session will use."""

    _write_auth_json(codex_home / "auth.json", access=_jwt(int(time.time()) + 3600))

    keys_first = opa.openai_platform_auth_status(
        env={"OPENAI_API_KEY": "sk-env"}, codex_home=codex_home
    )
    codex_only = opa.openai_platform_auth_status(
        configured_api_key="sk-x",
        env={"OPENAI_API_KEY": "sk-env"},
        codex_home=codex_home,
        prefer_codex=True,
    )

    assert keys_first["source"] == opa.SOURCE_ENV
    assert codex_only["source"] == opa.SOURCE_CODEX_OAUTH
    assert "valid" in codex_only["detail"]


def test_status_reports_unconfigured_when_the_directive_cannot_be_met(
    codex_home: Path,
) -> None:
    """A key on the box is not a usable source under the directive, so status
    must not name one — it would read as voice being fine."""

    status = opa.openai_platform_auth_status(
        configured_api_key="sk-x",
        env={"OPENAI_API_KEY": "sk-env"},
        codex_home=codex_home,
        prefer_codex=True,
    )

    assert status["configured"] is False
    assert status["source"] is None
    assert "codex login" in status["detail"]
    assert "TALK_PREFER_CODEX_OAUTH" in status["detail"]


def test_a_refusal_never_carries_the_oauth_response_body(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OAuth endpoint's body is untrusted and can echo the submitted
    refresh token; this message reaches operators over HTTP and Discord."""

    _write_auth_json(
        codex_home / "auth.json", access=_jwt(int(time.time()) - 60), refresh="rt-SECRET"
    )

    def fake_post(fields: dict[str, str]) -> dict:
        raise opa.OpenAIPlatformAuthError(
            "OpenAI Codex token refresh failed (400): "
            '{"error":"invalid_grant","refresh_token":"rt-SECRET"}'
        )

    monkeypatch.setattr(opa, "_post_token_form", fake_post)

    with pytest.raises(opa.OpenAIPlatformAuthError) as caught:
        opa.resolve_openai_platform_auth(env={}, codex_home=codex_home, prefer_codex=True)

    assert "rt-SECRET" not in str(caught.value)
    assert str(caught.value) == opa.PREFER_CODEX_UNAVAILABLE_MESSAGE
    assert caught.value.__cause__ is not None, "detail survives in the traceback"


def test_status_refuses_a_credential_resolution_cannot_use(codex_home: Path) -> None:
    """Status must not call a corrupt login valid while resolve refuses it —
    under the directive status is the operator's only advance warning."""

    _write_auth_json(codex_home / "auth.json", access=_jwt(10**400))

    status = opa.openai_platform_auth_status(
        env={"OPENAI_API_KEY": "sk-env"}, codex_home=codex_home, prefer_codex=True
    )

    assert status["configured"] is False
    assert status["detail"] == opa.PREFER_CODEX_UNAVAILABLE_MESSAGE
    with pytest.raises(opa.OpenAIPlatformAuthError):
        opa.resolve_openai_platform_auth(
            env={"OPENAI_API_KEY": "sk-env"}, codex_home=codex_home, prefer_codex=True
        )


def test_status_still_reports_a_usable_login_under_the_directive(
    codex_home: Path,
) -> None:
    """The strict check must not reject healthy credentials."""

    _write_auth_json(codex_home / "auth.json", access=_jwt(int(time.time()) + 3600))
    assert (
        opa.openai_platform_auth_status(codex_home=codex_home, prefer_codex=True)["source"]
        == opa.SOURCE_CODEX_OAUTH
    )

    _write_auth_json(codex_home / "auth.json", access=_jwt(int(time.time()) - 60))
    expired = opa.openai_platform_auth_status(codex_home=codex_home, prefer_codex=True)
    assert expired["source"] == opa.SOURCE_CODEX_OAUTH
    assert "expired-will-refresh" in expired["detail"]

    _write_auth_json(codex_home / "auth.json", access="opaque-token")
    assert (
        opa.openai_platform_auth_status(codex_home=codex_home, prefer_codex=True)["source"]
        == opa.SOURCE_CODEX_OAUTH
    )


def test_status_keeps_a_refreshable_credential_with_a_corrupt_past_expiry(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolution refreshes an expired credential BEFORE materializing its
    expiry, so a corrupt past exp is still usable — status must agree."""

    _write_auth_json(codex_home / "auth.json", access=_jwt(-(10**20)))

    status = opa.openai_platform_auth_status(codex_home=codex_home, prefer_codex=True)

    assert status["source"] == opa.SOURCE_CODEX_OAUTH
    assert "expired-will-refresh" in status["detail"]

    new_access = _jwt(int(time.time()) + 3600)
    monkeypatch.setattr(
        opa,
        "_post_token_form",
        lambda fields: {
            "access_token": new_access,
            "refresh_token": "rt-new",
            "expires_in": 3600,
        },
    )
    auth = opa.resolve_openai_platform_auth(
        env={"OPENAI_API_KEY": "sk-env"}, codex_home=codex_home, prefer_codex=True
    )

    assert auth.source == opa.SOURCE_CODEX_OAUTH, "status and resolve must agree"


def test_no_key_can_ever_be_reached_under_the_directive(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cardinal invariant, swept rather than sampled.

    Across every combination of key configuration and Codex-login health, the
    directive must yield the subscription or an error — never a metered key.
    A single silent key here is a billing violation, so this sweeps the matrix
    instead of trusting the branches that happen to have their own test.
    """

    auth_path = codex_home / "auth.json"

    def codex_valid() -> None:
        _write_auth_json(auth_path, access=_jwt(int(time.time()) + 3600))

    def codex_absent() -> None:
        auth_path.unlink(missing_ok=True)

    def codex_apikey_mode() -> None:
        auth_path.write_text(
            json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-codex-file"}),
            encoding="utf-8",
        )

    def codex_malformed() -> None:
        auth_path.write_text("{not json", encoding="utf-8")

    def codex_corrupt_expiry() -> None:
        _write_auth_json(auth_path, access=_jwt(10**400))

    def codex_expired() -> None:
        _write_auth_json(auth_path, access=_jwt(int(time.time()) - 60))

    refresh_outcomes = {
        "refresh-succeeds": None,
        "refresh-rejected": opa.OpenAIPlatformAuthError("refresh failed (401): invalid_grant"),
        "refresh-offline": httpx.ConnectError("connection refused"),
        "refresh-timeout": httpx.TimeoutException("timed out"),
    }
    key_setups = {
        "no-keys": (None, {}),
        "env-key": (None, {"OPENAI_API_KEY": "sk-env"}),
        "configured-key": ("sk-configured", {"OPENAI_API_KEY": "sk-env"}),
        "blank-configured-key": ("   ", {"OPENAI_API_KEY": "sk-env"}),
    }
    codex_states = {
        "valid": codex_valid,
        "absent": codex_absent,
        "apikey-mode": codex_apikey_mode,
        "malformed": codex_malformed,
        "corrupt-expiry": codex_corrupt_expiry,
        "expired": codex_expired,
    }

    for codex_name, seed in codex_states.items():
        for refresh_name, outcome in refresh_outcomes.items():
            for key_name, (configured, env) in key_setups.items():
                seed()
                if outcome is None:
                    monkeypatch.setattr(
                        opa,
                        "_post_token_form",
                        lambda fields: {
                            "access_token": _jwt(int(time.time()) + 3600),
                            "refresh_token": "rt-new",
                            "expires_in": 3600,
                        },
                    )
                else:
                    monkeypatch.setattr(
                        opa,
                        "_post_token_form",
                        lambda fields, _o=outcome: (_ for _ in ()).throw(_o),
                    )
                case = f"{codex_name}/{refresh_name}/{key_name}"

                try:
                    auth = opa.resolve_openai_platform_auth(
                        configured_api_key=configured,
                        env=env,
                        codex_home=codex_home,
                        prefer_codex=True,
                    )
                except opa.OpenAIPlatformAuthError as exc:
                    assert str(exc) == opa.PREFER_CODEX_UNAVAILABLE_MESSAGE, case
                    continue

                assert auth.source == opa.SOURCE_CODEX_OAUTH, f"metered key reached: {case}"

                status = opa.openai_platform_auth_status(
                    configured_api_key=configured,
                    env=env,
                    codex_home=codex_home,
                    prefer_codex=True,
                )
                assert status["source"] in (opa.SOURCE_CODEX_OAUTH, None), case


def test_a_type_error_in_the_codex_leg_still_fails_closed_cleanly(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch-tuple symmetry with the status path's strict check: a type that
    escaped here would bypass BOTH the both-remedies message and
    talk_session's `except OpenAIPlatformAuthError`, surfacing as a raw crash
    instead of a clean TalkAuthError."""

    def explode(_codex_home):
        raise TypeError("unsupported operand type for +: 'int' and 'str'")

    monkeypatch.setattr(opa, "_resolve_codex_oauth", explode)

    with pytest.raises(opa.OpenAIPlatformAuthError) as caught:
        opa.resolve_openai_platform_auth(
            env={"OPENAI_API_KEY": "sk-env"}, codex_home=codex_home, prefer_codex=True
        )

    assert str(caught.value) == opa.PREFER_CODEX_UNAVAILABLE_MESSAGE
    assert isinstance(caught.value.__cause__, TypeError)
