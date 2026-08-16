"""
Shared Google OAuth token management for all Google integrations.

All Google services (Gmail, Calendar, Sheets, Docs, Drive) share a single OAuth token.
Token is stored as JSON and auto-refreshes when expired.

Setup:
1. Download OAuth credentials from Google Cloud Console → Desktop app
2. Save as .claude/scripts/integrations/google_credentials.json
3. Run: uv run python setup_auth.py
   (on headless machines: uv run python setup_auth.py --headless)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Add parent dir for config imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (
    GA4_REPORTING_TOKEN_FILE,
    GOOGLE_CREDENTIALS_FILE,
    GOOGLE_SCOPES,
    GOOGLE_TOKEN_FILE,
)


def get_google_credentials() -> Any:
    """
    Load Google OAuth credentials, refreshing if expired.

    Returns authenticated Credentials object usable for Gmail and Calendar APIs.
    Raises FileNotFoundError if credentials file is missing.
    Raises RuntimeError if token is invalid and re-auth is needed.
    """
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds: Credentials | None = None

    # Load existing token
    if GOOGLE_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
            str(GOOGLE_TOKEN_FILE), GOOGLE_SCOPES
        )

    # Refresh if expired
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Save refreshed token
            token_json: str = creds.to_json()  # type: ignore[no-untyped-call]
            GOOGLE_TOKEN_FILE.write_text(token_json, encoding="utf-8")
            return creds
        except RefreshError as e:
            raise RuntimeError(
                f"Google token refresh failed: {e}\n"
                "Run 'uv run python setup_auth.py' to re-authenticate."
            ) from e

    # Valid credentials exist
    if creds and creds.valid:
        return creds

    # Need initial auth flow
    raise RuntimeError(
        "No valid Google OAuth token found.\n"
        "Run 'uv run python setup_auth.py' to authenticate."
    )


def get_ga4_reporting_credentials() -> Any:
    """Load the dedicated, refreshable GA4 reporting token when configured.

    The optional ``GA4_REPORTING_TOKEN_FILE`` keeps website reporting separate
    from the shared Gmail/Calendar token. Existing installations without that
    setting retain the legacy shared-token behavior.
    """
    if not GA4_REPORTING_TOKEN_FILE:
        return get_google_credentials()

    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = Path(GA4_REPORTING_TOKEN_FILE).expanduser()
    scope = "https://www.googleapis.com/auth/analytics.readonly"
    if not token_path.exists():
        raise RuntimeError(
            f"GA4 reporting token not found: {token_path}\n"
            "Set GA4_REPORTING_TOKEN_FILE to a dedicated Analytics OAuth token."
        )

    creds: Credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
        str(token_path), [scope]
    )
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        except RefreshError as exc:
            raise RuntimeError(
                f"GA4 reporting token refresh failed: {exc}\n"
                "Re-authorize the dedicated Analytics OAuth token."
            ) from exc

    granted = set(creds.scopes or [])
    if scope not in granted or not creds.valid:
        raise RuntimeError(
            "GA4 reporting token is invalid or lacks analytics.readonly; "
            "re-authorize the dedicated Analytics OAuth token."
        )
    return creds


# The GA4 WRITE token (epic #465 1a PR 2). Deliberately a different file from
# the reporting token: a read credential must never be quietly promoted into
# an edit credential. The default path matches the ga4-ops skill's contract.
GA4_EDIT_SCOPE = "https://www.googleapis.com/auth/analytics.edit"
GA4_READONLY_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
GA4_EDIT_DEFAULT_TOKEN = "~/.config/YourBusiness/ga4-edit-token.json"


def get_ga4_admin_credentials() -> Any:
    """Load the dedicated GA4 EDIT token, failing CLOSED without analytics.edit.

    Mirrors the reporting-token discipline above with one sharper edge: a
    token that lacks ``analytics.edit`` is an error, never a fallback to a
    readonly token — the caller is a WRITE path (property/stream creation),
    and downgrading scopes would fail later, deeper, and less honestly.

    ``GA4_EDIT_TOKEN_FILE`` is read at call time (Rule 1) so an operator (or
    a test) re-pointing it takes effect on the next call.
    """
    import json
    import os

    raw = os.getenv("GA4_EDIT_TOKEN_FILE", "").strip() or GA4_EDIT_DEFAULT_TOKEN
    token_path = Path(raw).expanduser()

    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not token_path.exists():
        raise RuntimeError(
            f"GA4 edit token not found: {token_path}\n"
            "Authorize a dedicated token with analytics.edit (see the ga4-ops "
            "skill), or set GA4_EDIT_TOKEN_FILE to one."
        )

    # The GRANTED scopes live in the file, not on the Credentials object:
    # from_authorized_user_file(path, scopes) would happily REPORT the scopes
    # we asked for over the ones the token actually carries, which would make
    # this check vacuous. Read the grant physically (Rule 2), fail closed.
    try:
        granted = set(json.loads(token_path.read_text(encoding="utf-8")).get("scopes") or [])
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"GA4 edit token unreadable: {token_path}: {exc}") from exc
    if GA4_EDIT_SCOPE not in granted:
        raise RuntimeError(
            f"GA4 edit token at {token_path} lacks analytics.edit — refusing "
            "to run a write path on a readonly credential. Re-authorize with "
            "the analytics.edit scope."
        )

    creds: Credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
        str(token_path), sorted(granted)
    )
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        except RefreshError as exc:
            raise RuntimeError(
                f"GA4 edit token refresh failed: {exc}\n"
                "Re-authorize the dedicated Analytics edit token."
            ) from exc

    if not creds.valid:
        raise RuntimeError(
            "GA4 edit token is invalid; re-authorize the dedicated Analytics "
            "edit token."
        )
    return creds


def run_initial_auth(headless: bool = False) -> Any:
    """
    Run the interactive OAuth flow (one-time setup).

    Args:
        headless: If True, use manual copy-paste flow (no browser needed).
                  Prints a URL, user opens it locally, pastes back the auth code.
                  If False, opens a browser and runs a local callback server.

    Requires google_credentials.json to be present.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

    if not GOOGLE_CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"Google credentials file not found: {GOOGLE_CREDENTIALS_FILE}\n"
            "Download from Google Cloud Console → APIs & Services → Credentials → "
            "OAuth 2.0 Client ID → Desktop app → Download JSON"
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(GOOGLE_CREDENTIALS_FILE), GOOGLE_SCOPES
    )

    if headless:
        # Manual flow for headless/remote machines:
        # 1. Generate auth URL
        # 2. User opens in local browser, authorizes
        # 3. Google redirects to localhost (which fails — that's fine)
        # 4. User copies the full redirect URL and pastes it back
        flow.redirect_uri = "http://localhost:1"  # Use port 1 (won't actually listen)
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

        print("\n" + "=" * 60)
        print("  HEADLESS GOOGLE OAUTH SETUP")
        print("=" * 60)
        print(f"\n1. Open this URL in your browser:\n\n{auth_url}\n")
        print("2. Authorize the app and grant all requested permissions.")
        print("3. You'll be redirected to a page that FAILS to load (localhost:1).")
        print("   That's expected! Copy the FULL URL from your browser's address bar.")
        print("   It looks like: http://localhost:1/?state=...&code=...&scope=...")
        print()
        redirect_response = input("4. Paste the full redirect URL here: ").strip()

        # Extract the authorization code from the redirect URL
        flow.fetch_token(authorization_response=redirect_response)
        creds = flow.credentials
    else:
        creds = flow.run_local_server(port=0)

    # Save token
    GOOGLE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    GOOGLE_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    print(f"\nToken saved to {GOOGLE_TOKEN_FILE}")

    return creds


def is_google_authenticated() -> bool:
    """Check if a valid Google OAuth token exists (without triggering auth flow)."""
    if not GOOGLE_TOKEN_FILE.exists():
        return False

    try:
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
            str(GOOGLE_TOKEN_FILE), GOOGLE_SCOPES
        )
        # Token exists and either valid or has refresh_token to renew
        return creds.valid or bool(creds.refresh_token)
    except Exception:
        return False
