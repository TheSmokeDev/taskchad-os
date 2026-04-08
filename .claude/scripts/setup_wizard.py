"""Interactive setup wizard for The Homie.

Usage:
    uv run python setup_wizard.py          # Full interactive setup
    uv run python setup_wizard.py --check  # Validate existing setup
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
ENV_FILE = SCRIPTS_DIR / ".env"
ENV_TEMPLATE = SCRIPTS_DIR.parent.parent / "deploy" / "env.template"


def check_prerequisites() -> list[str]:
    """Check all prerequisites are met. Returns list of issues."""
    issues = []
    if sys.version_info < (3, 12):
        issues.append(f"Python 3.12+ required, found {sys.version}")
    if shutil.which("uv") is None:
        issues.append("uv not found — install from https://docs.astral.sh/uv/")
    if not ENV_FILE.exists():
        issues.append(f".env not found at {ENV_FILE}")
    return issues


def create_env_from_template() -> bool:
    """Copy env.template to .env if .env doesn't exist."""
    if ENV_FILE.exists():
        print(f".env already exists at {ENV_FILE}")
        return False
    if ENV_TEMPLATE.exists():
        shutil.copy2(ENV_TEMPLATE, ENV_FILE)
        print(f"Created .env from template at {ENV_FILE}")
        return True
    else:
        ENV_FILE.write_text(
            "# The Homie Configuration\n# See deploy/env.template for all options\n"
        )
        return True


def validate_tokens() -> dict[str, str]:
    """Test configured tokens. Returns {platform: status}."""
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE, override=True)
    results = {}

    # Telegram
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if token:
        try:
            import urllib.request

            resp = urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/getMe", timeout=10
            )
            data = json.loads(resp.read())
            results["Telegram"] = f"OK (@{data['result']['username']})"
        except Exception as e:
            results["Telegram"] = f"FAIL ({e})"
    else:
        results["Telegram"] = "not configured"

    # Discord
    token = os.getenv("DISCORD_BOT_TOKEN", "")
    results["Discord"] = "configured" if token else "not configured"

    # WhatsApp
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    results["WhatsApp"] = "configured" if token else "not configured"

    # Slack
    token = os.getenv("SLACK_BOT_TOKEN", "")
    results["Slack"] = "configured" if token else "not configured"

    return results


def interactive_setup() -> None:
    """Run the full interactive setup wizard."""
    print("=" * 60)
    print("The Homie — Setup Wizard")
    print("=" * 60)

    # Step 1: Prerequisites
    print("\n1. Checking prerequisites...")
    issues = check_prerequisites()
    if issues:
        for issue in issues:
            print(f"   ISSUE: {issue}")
    else:
        print("   All prerequisites met.")

    # Step 2: .env file
    print("\n2. Environment configuration...")
    if not ENV_FILE.exists():
        create_env_from_template()
        print(f"   Edit {ENV_FILE} with your tokens, then re-run this wizard.")
        return

    # Step 3: Validate tokens
    print("\n3. Validating configured platforms...")
    results = validate_tokens()
    for plat, status in results.items():
        if "OK" in status or status == "configured":
            icon = "OK"
        elif "not" in status:
            icon = "--"
        else:
            icon = "!!"
        print(f"   {icon} {plat}: {status}")

    # Step 4: Test bot startup
    print("\n4. Testing bot startup...")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR.parent / "chat" / "main.py"), "--test"],
        capture_output=True,
        text=True,
        cwd=str(SCRIPTS_DIR),
    )
    if result.returncode == 0:
        print("   Bot startup test passed.")
    else:
        print(f"   Bot startup test FAILED:\n{result.stderr[-500:]}")

    print("\n" + "=" * 60)
    print("Setup complete. Run: cd .claude/chat && bash run_chat.sh")
    print("=" * 60)


if __name__ == "__main__":
    if "--check" in sys.argv:
        issues = check_prerequisites()
        if issues:
            for i in issues:
                print(f"ISSUE: {i}")
            sys.exit(1)
        else:
            print("All checks passed.")
    else:
        interactive_setup()
