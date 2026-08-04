"""OpenAI Platform auth resolution with Codex OAuth fallback.

Port of the OpenClaw PR #100671 ("Reuse Codex OAuth for OpenAI Realtime
voice") auth ordering for Homie voice surfaces. Resolution order:

1. Explicit configured key (``TALK_OPENAI_API_KEY`` style, caller-supplied).
   A configured key that is present but blank fails closed — no fallback.
2. ``OPENAI_API_KEY`` environment variable.
3. External Codex CLI login (``$CODEX_HOME/auth.json`` or ``~/.codex/auth.json``
   in ChatGPT token mode), refreshing the access token against
   ``https://auth.openai.com/oauth/token`` when expired and writing the
   refreshed tokens back so the Codex CLI keeps working.

API-key sources always win over OAuth by default; OAuth availability and
billing follow the authenticated account's Realtime entitlement. Callers that
want the subscription instead pass ``prefer_codex=True`` — a per-call billing
directive that makes the Codex login the ONLY source, so a box that must keep
``OPENAI_API_KEY`` set for other subsystems can still run voice off a Codex
sign-in without ever being metered by surprise. That path fails closed, not
back to a key. The returned token is a bearer credential — never log it.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import httpx

REALTIME_AUTH_REQUIRED_MESSAGE = (
    "OpenAI Realtime voice requires an OpenAI API key or Codex OAuth sign-in"
)

#: Fail-closed message for the ``prefer_codex`` opt-in. Names BOTH remedies —
#: restoring the subscription and standing down the directive — because the
#: only alternative to failing here is billing the operator by surprise.
PREFER_CODEX_UNAVAILABLE_MESSAGE = (
    "OpenAI Realtime voice is pinned to your Codex OAuth subscription "
    "(TALK_PREFER_CODEX_OAUTH is on) and no usable Codex sign-in was found. "
    "Run `codex login` to restore the subscription, or unset "
    "TALK_PREFER_CODEX_OAUTH to allow the OpenAI API key (metered billing)."
)

_CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
# Public client id of the official Codex CLI (same id OpenClaw/Codex use).
_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CODEX_FALLBACK_EXPIRY_S = 60 * 60
_REFRESH_MARGIN_S = 60
_TOKEN_REQUEST_TIMEOUT_S = 30.0

SOURCE_CONFIGURED = "configured"
SOURCE_ENV = "env"
SOURCE_CODEX_OAUTH = "codex-oauth"


class OpenAIPlatformAuthError(RuntimeError):
    """Raised when no OpenAI Platform credential can be resolved."""


@dataclass(frozen=True, slots=True)
class OpenAIPlatformAuth:
    """A resolved OpenAI Platform bearer credential."""

    token: str
    source: str  # SOURCE_CONFIGURED | SOURCE_ENV | SOURCE_CODEX_OAUTH
    detail: str
    expires_at: datetime | None = None


def _read_env(env: Mapping[str, str] | None, key: str) -> str | None:
    source = os.environ if env is None else env
    raw = source.get(key)
    if raw is None:
        return None
    trimmed = raw.strip()
    return trimmed or None


def _codex_auth_path(codex_home: Path | None = None) -> Path:
    if codex_home is not None:
        return codex_home / "auth.json"
    configured = os.environ.get("CODEX_HOME", "").strip()
    base = Path(configured) if configured else Path.home() / ".codex"
    return base / "auth.json"


def _decode_jwt_expiry_s(token: str) -> int | None:
    """Read the ``exp`` claim from a JWT without verifying the signature."""

    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + padding))
    except Exception:
        return None
    exp = data.get("exp")
    return exp if isinstance(exp, (int, float)) else None


def _auth_json_uses_chatgpt_tokens(data: dict) -> bool:
    """Mirror OpenClaw ``codexAuthJsonUsesChatGptTokens``."""

    auth_mode = data.get("auth_mode")
    if isinstance(auth_mode, str) and auth_mode.strip():
        return auth_mode.strip().lower() in {"chatgpt", "chatgptauthtokens"}
    return not isinstance(data.get("OPENAI_API_KEY"), str)


@dataclass(slots=True)
class _CodexOauthCredential:
    access: str
    refresh: str
    expires_s: int
    account_id: str | None
    id_token: str | None


def _parse_codex_oauth_credential(data: dict, fallback_expiry_s: int) -> _CodexOauthCredential | None:
    if not _auth_json_uses_chatgpt_tokens(data):
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not isinstance(access, str) or not access:
        return None
    if not isinstance(refresh, str) or not refresh:
        return None
    expires_s = _decode_jwt_expiry_s(access)
    if expires_s is None:
        expires_s = fallback_expiry_s
    account_id = tokens.get("account_id")
    id_token = tokens.get("id_token")
    return _CodexOauthCredential(
        access=access,
        refresh=refresh,
        expires_s=expires_s,
        account_id=account_id if isinstance(account_id, str) else None,
        id_token=id_token if isinstance(id_token, str) else None,
    )


def _read_codex_auth_json(auth_path: Path) -> dict | None:
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _fallback_expiry_s(auth_path: Path) -> int:
    """Expiry estimate when the access token carries no JWT ``exp``.

    Codex CLI access tokens live about an hour; anchor on the auth.json
    mtime, which every token refresh (CLI or ours) bumps (mirrors OpenClaw's
    file-based branch).
    """

    try:
        base = auth_path.stat().st_mtime
    except OSError:
        base = time.time()
    return int(base) + _CODEX_FALLBACK_EXPIRY_S


def _post_token_form(fields: dict[str, str]) -> dict:
    """POST a form to the OpenAI OAuth token endpoint. Isolated for tests."""

    response = httpx.post(
        _CODEX_TOKEN_URL,
        data=fields,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=_TOKEN_REQUEST_TIMEOUT_S,
    )
    if response.status_code != 200:
        raise OpenAIPlatformAuthError(
            f"OpenAI Codex token refresh failed ({response.status_code}): "
            f"{response.text[:300] or response.reason_phrase}"
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise OpenAIPlatformAuthError(
            "OpenAI Codex token refresh returned non-JSON response"
        ) from exc
    if not isinstance(payload, dict):
        raise OpenAIPlatformAuthError("OpenAI Codex token refresh returned invalid response")
    return payload


def _write_auth_json(auth_path: Path, data: dict) -> None:
    """Atomically persist refreshed tokens, preserving file permissions."""

    fd, tmp_name = tempfile.mkstemp(
        prefix="auth-", suffix=".json", dir=str(auth_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(tmp_name, auth_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _refresh_codex_credential(
    auth_path: Path, data: dict, credential: _CodexOauthCredential
) -> _CodexOauthCredential:
    try:
        payload = _post_token_form(
            {
                "grant_type": "refresh_token",
                "refresh_token": credential.refresh,
                "client_id": _CODEX_CLIENT_ID,
            }
        )
    except OpenAIPlatformAuthError as exc:
        # Refresh-token race: the Codex CLI may have refreshed concurrently,
        # consuming the single-use refresh token we just tried. Re-read the
        # file once — if it now holds a different, valid access token, use it.
        reread = _read_codex_auth_json(auth_path)
        if reread is not None:
            reparsed = _parse_codex_oauth_credential(
                reread, _fallback_expiry_s(auth_path)
            )
            if (
                reparsed is not None
                and reparsed.access != credential.access
                and reparsed.expires_s > time.time() + _REFRESH_MARGIN_S
            ):
                return reparsed
        raise OpenAIPlatformAuthError(
            f"{REALTIME_AUTH_REQUIRED_MESSAGE}. Codex token refresh failed — "
            f"run `codex login` to refresh your ChatGPT sign-in. Detail: {exc}"
        ) from exc

    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    if (
        not isinstance(access, str)
        or not access
        or not isinstance(refresh, str)
        or not refresh
        or not isinstance(expires_in, (int, float))
    ):
        raise OpenAIPlatformAuthError(
            "OpenAI Codex token refresh response missing fields "
            "(access_token, refresh_token, expires_in)"
        )

    expires_s = int(time.time()) + int(expires_in)
    refreshed = _CodexOauthCredential(
        access=access,
        refresh=refresh,
        expires_s=expires_s,
        account_id=credential.account_id,
        id_token=(
            payload.get("id_token")
            if isinstance(payload.get("id_token"), str)
            else credential.id_token
        ),
    )

    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    data["tokens"] = {
        **tokens,
        "access_token": refreshed.access,
        "refresh_token": refreshed.refresh,
        **({"id_token": refreshed.id_token} if refreshed.id_token else {}),
        **({"account_id": refreshed.account_id} if refreshed.account_id else {}),
    }
    data["last_refresh"] = datetime.now(timezone.utc).isoformat()
    try:
        _write_auth_json(auth_path, data)
    except OSError:
        # Persisting is best-effort: the in-memory token is still valid for
        # this session even if the write-back fails (read-only profile dir).
        pass
    return refreshed


def _resolve_codex_oauth(codex_home: Path | None = None) -> OpenAIPlatformAuth | None:
    auth_path = _codex_auth_path(codex_home)
    data = _read_codex_auth_json(auth_path)
    if data is None:
        return None
    credential = _parse_codex_oauth_credential(data, _fallback_expiry_s(auth_path))
    if credential is None:
        return None
    if credential.expires_s <= time.time() + _REFRESH_MARGIN_S:
        credential = _refresh_codex_credential(auth_path, data, credential)
    return OpenAIPlatformAuth(
        token=credential.access,
        source=SOURCE_CODEX_OAUTH,
        detail="external Codex CLI login (ChatGPT OAuth)",
        expires_at=datetime.fromtimestamp(credential.expires_s, tz=timezone.utc),
    )


def _resolve_key_sources(
    configured_api_key: str | None, env: Mapping[str, str] | None
) -> OpenAIPlatformAuth | None:
    """The API-key legs: configured key then ``OPENAI_API_KEY``.

    ``None`` when neither is set. A configured key that is present but blank
    still fails closed here — that guard belongs to the key legs, not to the
    ordering, so it survives every preference.
    """

    if configured_api_key is not None:
        configured = configured_api_key.strip()
        if not configured:
            raise OpenAIPlatformAuthError(
                f"{REALTIME_AUTH_REQUIRED_MESSAGE}. A configured OpenAI API key "
                "is set but empty — fix or remove it to allow other auth sources."
            )
        return OpenAIPlatformAuth(
            token=configured,
            source=SOURCE_CONFIGURED,
            detail="explicitly configured OpenAI API key",
        )

    env_key = _read_env(env, "OPENAI_API_KEY")
    if env_key:
        return OpenAIPlatformAuth(
            token=env_key,
            source=SOURCE_ENV,
            detail="OPENAI_API_KEY environment variable",
        )

    return None


def _codex_status_detail(codex_home: Path | None, *, strict: bool = False) -> str | None:
    """Codex OAuth status detail, or ``None`` when no credential parses.

    ``strict`` additionally rejects a credential resolution could parse but
    could not USE — an absurd ``exp`` materializes fine as an int and then
    blows up converting to a datetime. Status must never call such a login
    "valid" while resolution refuses it; that gap is only load-bearing under
    the fail-closed directive, where status is the operator's only warning.

    The check follows resolution's ORDER, not just its rules: an expired
    credential is refreshed before its expiry is materialized, so a corrupt
    PAST expiry is still refreshable and must not be reported as unusable.
    """

    auth_path = _codex_auth_path(codex_home)
    data = _read_codex_auth_json(auth_path)
    if data is None:
        return None
    credential = _parse_codex_oauth_credential(data, _fallback_expiry_s(auth_path))
    if credential is None:
        return None
    expired = credential.expires_s <= time.time() + _REFRESH_MARGIN_S
    if strict and not expired:
        # Only a credential that would be returned AS-IS has to survive this.
        try:
            datetime.fromtimestamp(credential.expires_s, tz=timezone.utc)
        except (ValueError, ArithmeticError, OSError, TypeError):
            return None
    state = "expired-will-refresh" if expired else "valid"
    return f"external Codex CLI login (ChatGPT OAuth), token {state}"


def resolve_openai_platform_auth(
    *,
    configured_api_key: str | None = None,
    env: Mapping[str, str] | None = None,
    codex_home: Path | None = None,
    prefer_codex: bool = False,
) -> OpenAIPlatformAuth:
    """Resolve an OpenAI Platform bearer token in fail-closed order.

    Default order: explicit configured key -> ``OPENAI_API_KEY`` -> external
    Codex OAuth login. A configured key that is present but blank fails closed
    instead of silently falling through to another source.

    ``prefer_codex`` is the caller's opt-in to run off the Codex OAuth login
    (the ChatGPT subscription) INSTEAD of a key. It is a billing directive, not
    a ranking: when the Codex login is absent, unusable, or fails to refresh,
    resolution FAILS CLOSED rather than switching to a metered API key behind
    the operator's back. The error names both remedies so the next move is an
    informed choice. Same shape as the set-but-blank configured key, which has
    always failed closed rather than falling through.

    The flag is per-call — it is never read from the environment here, so
    callers that do not pass it are unaffected.
    """

    if prefer_codex:
        try:
            oauth = _resolve_codex_oauth(codex_home)
        except (
            OpenAIPlatformAuthError,
            httpx.HTTPError,
            OSError,
            ValueError,
            ArithmeticError,
            TypeError,
        ) as exc:
            # Everything the Codex leg can fail with lands here: a dead refresh
            # token, a NETWORK failure on the refresh call, and a corrupt
            # auth.json whose absurd `exp` overflows the timestamp math rather
            # than raising an auth error. All of them mean the same thing to
            # the operator — the subscription is not usable right now — so all
            # of them get the same both-remedies message instead of a silent
            # switch to a metered key. The tuple mirrors the one the status
            # path's strict check already anticipates — a type slipping through
            # here would bypass BOTH the message and talk_session's
            # `except OpenAIPlatformAuthError`, surfacing as a raw crash.
            #
            # The cause is chained (`from exc`) but NEVER concatenated: the
            # refresh failure carries the OAuth endpoint's raw response body,
            # which is untrusted and can echo the submitted refresh token. This
            # message reaches operators over HTTP and Discord — the traceback
            # is where the detail belongs.
            raise OpenAIPlatformAuthError(PREFER_CODEX_UNAVAILABLE_MESSAGE) from exc
        if oauth is None:
            raise OpenAIPlatformAuthError(PREFER_CODEX_UNAVAILABLE_MESSAGE)
        return oauth

    key_auth = _resolve_key_sources(configured_api_key, env)
    if key_auth is not None:
        return key_auth

    oauth = _resolve_codex_oauth(codex_home)
    if oauth is not None:
        return oauth

    raise OpenAIPlatformAuthError(REALTIME_AUTH_REQUIRED_MESSAGE)


def openai_platform_auth_status(
    *,
    configured_api_key: str | None = None,
    env: Mapping[str, str] | None = None,
    codex_home: Path | None = None,
    prefer_codex: bool = False,
) -> dict:
    """Report which auth source would be used, without exposing any token.

    Mirrors ``resolve_openai_platform_auth``, including the ``prefer_codex``
    opt-in: under the directive the Codex login is the ONLY source, so a
    missing one reports unconfigured with the both-remedies message instead of
    naming a key that resolution will refuse to use.
    """

    if prefer_codex:
        codex_detail = _codex_status_detail(codex_home, strict=True)
        if codex_detail is not None:
            return {"configured": True, "source": SOURCE_CODEX_OAUTH, "detail": codex_detail}
        return {"configured": False, "source": None, "detail": PREFER_CODEX_UNAVAILABLE_MESSAGE}

    if configured_api_key is not None and configured_api_key.strip():
        return {"configured": True, "source": SOURCE_CONFIGURED, "detail": "explicitly configured OpenAI API key"}
    if _read_env(env, "OPENAI_API_KEY"):
        return {"configured": True, "source": SOURCE_ENV, "detail": "OPENAI_API_KEY environment variable"}

    codex_detail = _codex_status_detail(codex_home)
    if codex_detail is not None:
        return {"configured": True, "source": SOURCE_CODEX_OAUTH, "detail": codex_detail}

    return {"configured": False, "source": None, "detail": REALTIME_AUTH_REQUIRED_MESSAGE}


__all__ = [
    "OpenAIPlatformAuth",
    "OpenAIPlatformAuthError",
    "PREFER_CODEX_UNAVAILABLE_MESSAGE",
    "REALTIME_AUTH_REQUIRED_MESSAGE",
    "SOURCE_CONFIGURED",
    "SOURCE_CODEX_OAUTH",
    "SOURCE_ENV",
    "openai_platform_auth_status",
    "resolve_openai_platform_auth",
]
