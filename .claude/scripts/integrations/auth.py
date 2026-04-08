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

from config import GOOGLE_CREDENTIALS_FILE, GOOGLE_SCOPES, GOOGLE_TOKEN_FILE


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
