"""
One-time auth setup for all direct platform integrations.

Walks through Google OAuth, Asana PAT validation, and Slack bot token validation.

Usage:
    uv run python setup_auth.py          # Full interactive setup
    uv run python setup_auth.py --check  # Status check only (no auth flows)
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override

apply_persona_override()

from config import (  # noqa: E402
    ASANA_ACCESS_TOKEN,
    ASANA_WORKSPACE_ID,
    GOOGLE_CREDENTIALS_FILE,
    PERSONAL_GMAIL_ACCOUNT,
    PERSONAL_GMAIL_SCOPES,
    PERSONAL_GMAIL_TOKEN_PATH,
    SLACK_BOT_TOKEN,
    ensure_directories,
)


def print_header(title: str) -> None:
    """Print a section header."""
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def print_status(name: str, ok: bool, detail: str = "") -> None:
    """Print a status line."""
    icon = "[OK]" if ok else "[--]"
    suffix = f" - {detail}" if detail else ""
    print(f"  {icon} {name}{suffix}")


def check_google(check_only: bool = False, headless: bool = False) -> bool:
    """Check/setup Google OAuth for Gmail + Calendar + GSC + GA4 + Sheets + Docs + Drive."""
    print_header("Google OAuth (Gmail + Calendar + GSC + GA4 + Sheets + Docs + Drive)")

    from integrations.auth import is_google_authenticated

    if is_google_authenticated():
        print_status("Google OAuth", True, "Token exists and is valid/refreshable")

        # Quick validation - try building a service
        try:
            from integrations.auth import get_google_credentials

            creds = get_google_credentials()
            from googleapiclient.discovery import build  # type: ignore[import-untyped]

            # Test Gmail
            gmail = build("gmail", "v1", credentials=creds)
            profile = gmail.users().getProfile(userId="me").execute()
            print_status("Gmail", True, f"Connected as {profile.get('emailAddress', '?')}")

            # Test Calendar
            calendar = build("calendar", "v3", credentials=creds)
            cal_list = calendar.calendarList().list(maxResults=1).execute()
            num_cals = len(cal_list.get("items", []))
            print_status("Calendar", True, f"Access confirmed ({num_cals} calendars visible)")

            # Test GSC
            try:
                from config import GSC_SITE_URL
                gsc = build("searchconsole", "v1", credentials=creds)
                sites = gsc.sites().list().execute()
                site_urls = [s.get("siteUrl", "") for s in sites.get("siteEntry", [])]
                if GSC_SITE_URL and GSC_SITE_URL in site_urls:
                    print_status("Search Console", True, f"Access to {GSC_SITE_URL}")
                elif site_urls:
                    print_status("Search Console", True, f"{len(site_urls)} sites visible")
                else:
                    print_status("Search Console", False, "No sites accessible")
            except Exception as e:
                print_status("Search Console", False, str(e))

            # Test GA4
            try:
                from config import GA4_PROPERTY_ID
                ga4 = build("analyticsdata", "v1beta", credentials=creds)
                pid = GA4_PROPERTY_ID
                if pid and not pid.startswith("properties/"):
                    pid = f"properties/{pid}"
                if pid:
                    metadata = ga4.properties().getMetadata(name=f"{pid}/metadata").execute()
                    num_metrics = len(metadata.get("metrics", []))
                    print_status("GA4 Analytics", True, f"Access to {pid} ({num_metrics} metrics)")
                else:
                    print_status("GA4 Analytics", False, "GA4_PROPERTY_ID not set in .env")
            except Exception as e:
                print_status("GA4 Analytics", False, str(e))

            return True
        except Exception as e:
            print_status("API validation", False, str(e))
            return False

    if check_only:
        print_status("Google OAuth", False, "Not authenticated")
        if not GOOGLE_CREDENTIALS_FILE.exists():
            print(f"\n  Missing: {GOOGLE_CREDENTIALS_FILE}")
            print("  Download from Google Cloud Console:")
            print("    1. Go to https://console.cloud.google.com")
            print("    2. Select/create project, enable Gmail + Calendar APIs")
            print("    3. Create OAuth 2.0 Client ID (Desktop app)")
            print("    4. Download JSON, save as google_credentials.json in:")
            print(f"       {GOOGLE_CREDENTIALS_FILE.parent}")
        else:
            print("\n  Credentials file found but no token yet.")
            print("  Run without --check to authenticate.")
        return False

    # Interactive setup
    if not GOOGLE_CREDENTIALS_FILE.exists():
        print(f"  Google credentials file not found: {GOOGLE_CREDENTIALS_FILE}")
        print()
        print("  To set up Google OAuth:")
        print("    1. Go to https://console.cloud.google.com")
        print("    2. Create/select project, enable Gmail API + Calendar API")
        print("    3. Configure OAuth consent screen:")
        print('       - User type: "External" (custom domain)')
        print('       - Publish to "Production" (non-sensitive scopes, no verification needed)')
        print("    4. Create OAuth 2.0 Client ID -> Desktop application")
        print("    5. Download JSON -> save as:")
        print(f"       {GOOGLE_CREDENTIALS_FILE}")
        print()
        input("  Press Enter when ready (or Ctrl+C to skip)...")

        if not GOOGLE_CREDENTIALS_FILE.exists():
            print_status("Google OAuth", False, "Credentials file still not found")
            return False

    # Run OAuth flow
    mode = "headless (manual URL)" if headless else "browser-based"
    print(f"  Starting {mode} OAuth flow...")
    try:
        from integrations.auth import run_initial_auth

        creds = run_initial_auth(headless=headless)
        print_status("Google OAuth", True, "Authenticated successfully!")

        # Validate
        from googleapiclient.discovery import build

        gmail = build("gmail", "v1", credentials=creds)
        profile = gmail.users().getProfile(userId="me").execute()
        print_status("Gmail", True, f"Connected as {profile.get('emailAddress', '?')}")

        calendar = build("calendar", "v3", credentials=creds)
        cal_list = calendar.calendarList().list(maxResults=1).execute()
        print_status("Calendar", True, "Access confirmed")

        # Validate GSC
        try:
            from config import GSC_SITE_URL
            gsc = build("searchconsole", "v1", credentials=creds)
            sites = gsc.sites().list().execute()
            site_urls = [s.get("siteUrl", "") for s in sites.get("siteEntry", [])]
            if GSC_SITE_URL and GSC_SITE_URL in site_urls:
                print_status("Search Console", True, f"Access to {GSC_SITE_URL}")
            else:
                print_status("Search Console", False, "No matching site — add your-calendar@gmail.com as GSC user")
        except Exception as e:
            print_status("Search Console", False, str(e))

        # Validate GA4
        try:
            from config import GA4_PROPERTY_ID
            ga4 = build("analyticsdata", "v1beta", credentials=creds)
            pid = GA4_PROPERTY_ID
            if pid and not pid.startswith("properties/"):
                pid = f"properties/{pid}"
            if pid:
                metadata = ga4.properties().getMetadata(name=f"{pid}/metadata").execute()
                print_status("GA4 Analytics", True, f"Access to {pid}")
            else:
                print_status("GA4 Analytics", False, "GA4_PROPERTY_ID not set")
        except Exception as e:
            print_status("GA4 Analytics", False, str(e))

        return True
    except Exception as e:
        print_status("Google OAuth", False, str(e))
        return False


def check_asana(check_only: bool = False) -> bool:
    """Check/validate Asana Personal Access Token."""
    print_header("Asana (Personal Access Token)")

    if not ASANA_ACCESS_TOKEN:
        print_status("Asana", False, "ASANA_ACCESS_TOKEN not set in .env")
        print()
        print("  To set up Asana:")
        print("    1. Go to https://app.asana.com/0/developer-console")
        print("    2. Create Personal Access Token")
        print("    3. Add to .claude/scripts/.env:")
        print("       ASANA_ACCESS_TOKEN=your_token_here")
        return False

    # Validate token
    try:
        import asana  # type: ignore[import-untyped]
        from asana.rest import ApiException  # type: ignore[import-untyped]

        configuration = asana.Configuration()
        configuration.access_token = ASANA_ACCESS_TOKEN
        api_client = asana.ApiClient(configuration)
        users_api = asana.UsersApi(api_client)

        me = users_api.get_user("me", opts={"opt_fields": "name,email"})
        name = me.get("name", "?") if isinstance(me, dict) else getattr(me, "name", "?")
        email = me.get("email", "") if isinstance(me, dict) else getattr(me, "email", "")
        print_status("Asana", True, f"Connected as {name} ({email})")

        # Validate workspace access
        workspaces_api = asana.WorkspacesApi(api_client)
        ws = workspaces_api.get_workspace(ASANA_WORKSPACE_ID, opts={"opt_fields": "name"})
        ws_name = ws.get("name", "?") if isinstance(ws, dict) else getattr(ws, "name", "?")
        print_status("Workspace", True, f"{ws_name} ({ASANA_WORKSPACE_ID})")

        return True
    except ApiException as e:
        print_status("Asana", False, f"API error: {e}")
        return False
    except Exception as e:
        print_status("Asana", False, str(e))
        return False


def check_slack(check_only: bool = False) -> bool:
    """Check/validate Slack bot token."""
    print_header("Slack (Bot Token)")

    if not SLACK_BOT_TOKEN:
        print_status("Slack", False, "SLACK_BOT_TOKEN not set in .env")
        print()
        print("  To set up Slack:")
        print("    1. Go to https://api.slack.com/apps -> Create New App -> From Scratch")
        print('    2. Name: "The Homie", select your workspace')
        print("    3. OAuth & Permissions -> Add Bot Token Scopes:")
        print("       channels:read, channels:history, chat:write, chat:write.public, users:read")
        print("    4. Install to Workspace -> Copy Bot User OAuth Token")
        print("    5. Add to .claude/scripts/.env:")
        print("       SLACK_BOT_TOKEN=xoxb-...")
        return False

    # Validate token
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError

        client = WebClient(token=SLACK_BOT_TOKEN)
        auth = client.auth_test()

        bot_name = auth.get("user", "?")
        team = auth.get("team", "?")
        print_status("Slack", True, f"Connected as {bot_name} in {team}")

        return True
    except SlackApiError as e:
        print_status("Slack", False, f"API error: {e.response['error']}")
        return False
    except Exception as e:
        print_status("Slack", False, str(e))
        return False


def setup_personal_gmail(headless: bool = False) -> bool:
    """Authenticate personal Gmail (owner6392lastname@gmail.com) with gmail.readonly scope."""
    from pathlib import Path

    print_header(f"Personal Gmail (read-only) — {PERSONAL_GMAIL_ACCOUNT}")

    token_path = Path(PERSONAL_GMAIL_TOKEN_PATH)

    # Check if already authenticated
    if token_path.exists():
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials

            creds = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
                str(token_path), PERSONAL_GMAIL_SCOPES
            )
            if creds.valid or creds.refresh_token:
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    token_path.write_text(creds.to_json(), encoding="utf-8")  # type: ignore[no-untyped-call]

                from googleapiclient.discovery import build  # type: ignore[import-untyped]

                gmail = build("gmail", "v1", credentials=creds)
                profile = gmail.users().getProfile(userId="me").execute()
                connected_as = profile.get("emailAddress", "?")
                print_status("Personal Gmail", True, f"Connected as {connected_as}")
                return True
        except Exception as e:
            print_status("Personal Gmail", False, f"Token invalid: {e} — re-authenticating")

    if not GOOGLE_CREDENTIALS_FILE.exists():
        print_status("Personal Gmail", False, f"Missing credentials file: {GOOGLE_CREDENTIALS_FILE}")
        print("  Use the same google_credentials.json as the AI account.")
        print("  Download from Google Cloud Console → APIs & Services → Credentials")
        return False

    print(f"  Starting OAuth flow for {PERSONAL_GMAIL_ACCOUNT}...")
    print("  IMPORTANT: When the browser opens, sign in as YOUR personal Google account,")
    print(f"  not the AI service account. Expected: {PERSONAL_GMAIL_ACCOUNT}")
    print()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

        flow = InstalledAppFlow.from_client_secrets_file(
            str(GOOGLE_CREDENTIALS_FILE), PERSONAL_GMAIL_SCOPES
        )

        if headless:
            flow.redirect_uri = "http://localhost:1"
            auth_url, _ = flow.authorization_url(
                prompt="consent", access_type="offline",
                login_hint=PERSONAL_GMAIL_ACCOUNT,
            )
            print(f"\n1. Open this URL:\n\n{auth_url}\n")
            print("2. Sign in as your personal Gmail account.")
            print("3. After authorizing, copy the full redirect URL (starts with http://localhost:1/?...)")
            redirect_response = input("4. Paste the full redirect URL here: ").strip()
            flow.fetch_token(authorization_response=redirect_response)
            creds = flow.credentials
        else:
            creds = flow.run_local_server(port=0, login_hint=PERSONAL_GMAIL_ACCOUNT)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")  # type: ignore[no-untyped-call]
        print(f"\nToken saved to {token_path}")

        from googleapiclient.discovery import build  # type: ignore[import-untyped]

        gmail = build("gmail", "v1", credentials=creds)
        profile = gmail.users().getProfile(userId="me").execute()
        connected_as = profile.get("emailAddress", "?")
        print_status("Personal Gmail", True, f"Authenticated as {connected_as}")
        return True
    except Exception as e:
        print_status("Personal Gmail", False, str(e))
        return False


def main() -> None:
    """Run auth setup."""
    parser = argparse.ArgumentParser(description="Set up direct platform integrations")
    parser.add_argument("--check", action="store_true", help="Check status only (no auth flows)")
    parser.add_argument("--headless", action="store_true",
                        help="Use manual URL copy-paste flow (for remote/headless machines)")
    parser.add_argument("--personal", action="store_true",
                        help="Authenticate personal Gmail only (owner6392lastname@gmail.com, readonly)")
    args = parser.parse_args()

    ensure_directories()

    # Personal Gmail only mode
    if args.personal:
        print_header("The Homie - Personal Gmail Auth")
        print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        ok = setup_personal_gmail(headless=args.headless)
        sys.exit(0 if ok else 1)

    print_header("The Homie - Direct Integrations Setup")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    mode = "Status Check" if args.check else ("Headless Setup" if args.headless else "Interactive Setup")
    print(f"  Mode: {mode}")

    results = {
        "Google": check_google(check_only=args.check, headless=args.headless),
        "Asana": check_asana(check_only=args.check),
        "Slack": check_slack(check_only=args.check),
        "Personal Gmail": setup_personal_gmail(headless=args.headless) if not args.check else (
            Path(PERSONAL_GMAIL_TOKEN_PATH).exists()
        ),
    }

    print_header("Summary")
    for name, ok in results.items():
        print_status(name, ok)

    configured = sum(1 for ok in results.values() if ok)
    total = len(results)
    print(f"\n  {configured}/{total} integrations configured")

    if configured < total and not args.check:
        print("\n  Re-run with --check to see what's still needed.")

    sys.exit(0 if configured == total else 1)


if __name__ == "__main__":
    main()
