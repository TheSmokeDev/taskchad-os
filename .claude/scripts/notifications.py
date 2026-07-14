"""
Notification utilities for The Homie heartbeat.

Cross-platform: Windows (toast), macOS (osascript), Linux (notify-send).
Falls back to console output if native notifications aren't available.
"""

from __future__ import annotations

import platform
import subprocess

from integrations.capabilities import IntegrationPolicyError, require_integration_action


def send_toast_notification(
    title: str,
    message: str,
    *,
    caller: str = "notifications.send_toast_notification",
) -> dict[str, str] | None:
    """
    Send a native desktop notification (cross-platform).

    Desktop toasts are truncated to 200 chars (OS display limits).
    Slack receives the full message.

    Args:
        title: Notification title
        message: Notification body

    Returns:
        Slack result dict with 'channel' and 'ts' if Slack succeeded, else None.
    """
    truncated = message[:200]
    system = platform.system()

    # Also send to Slack as additional channel (fire-and-forget) — full message
    slack_result = send_slack_notification(title, message, caller=caller)

    try:
        if system == "Windows":
            _notify_windows(title, truncated)
        elif system == "Darwin":
            _notify_macos(title, truncated)
        elif system == "Linux":
            _notify_linux(title, truncated)
    except Exception as e:
        print(f"Notification failed ({system}): {e}")
        # Fallback: console
        send_console_notification(title, message)

    return slack_result


def _notify_windows(title: str, message: str) -> bool:
    """Windows toast via PowerShell WinRT (replaces win10toast-click).

    win10toast-click drives a Win32 message pump that crashes on modern
    Python/Windows (WNDPROC "WPARAM is simple ... got NoneType", then
    pywintypes.error in Shell_NotifyIcon on cleanup) — seen live 2026-07-14
    when the watchdog's "bot is DOWN" toast fired. The WinRT toast API has
    no message pump. Must be windows powershell.exe (5.1): pwsh 7 dropped
    WinRT type projections. Title/body travel as env vars so no user text
    is ever interpolated into the script.
    """
    import os

    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$t = $x.GetElementsByTagName('text');"
        "$t.Item(0).AppendChild($x.CreateTextNode($env:HOMIE_TOAST_TITLE)) | Out-Null;"
        "$t.Item(1).AppendChild($x.CreateTextNode($env:HOMIE_TOAST_BODY)) | Out-Null;"
        "$n = [Windows.UI.Notifications.ToastNotification]::new($x);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'"
        ").Show($n)"
    )
    env = {**os.environ, "HOMIE_TOAST_TITLE": title, "HOMIE_TOAST_BODY": message}
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
        timeout=15,
        env=env,
    )
    return True


def _notify_macos(title: str, message: str) -> bool:
    """macOS notification via osascript."""
    # Escape backslashes and double quotes to prevent AppleScript injection
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_message}" with title "{safe_title}"'
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
    return True


def _notify_linux(title: str, message: str) -> bool:
    """Linux notification via notify-send."""
    subprocess.run(["notify-send", title, message], check=True, capture_output=True)
    return True


def send_slack_notification(
    title: str,
    message: str,
    channel: str | None = None,
    *,
    caller: str = "notifications.send_slack_notification",
) -> dict[str, str] | None:
    """
    Send notification to Slack channel.

    Args:
        title: Notification title (shown in bold)
        message: Notification body
        channel: Target channel (defaults to SLACK_NOTIFICATION_CHANNEL from config)

    Returns:
        Dict with 'channel' and 'ts' if sent successfully, None otherwise.
    """
    try:
        require_integration_action(
            "slack",
            "send",
            surface="internal",
            caller=caller,
        )
        from config import SLACK_BOT_TOKEN, SLACK_NOTIFICATION_CHANNEL
        from integrations.slack_api import send_notification

        if not SLACK_BOT_TOKEN:
            return None

        target = channel or SLACK_NOTIFICATION_CHANNEL
        formatted = f"*{title}*\n{message}"
        return send_notification(target, formatted, surface="internal", caller=caller)
    except IntegrationPolicyError as e:
        print(f"Slack notification blocked by policy: {e}")
        return None
    except Exception as e:
        print(f"Slack notification failed: {e}")
        return None


def send_console_notification(title: str, message: str) -> None:
    """Send notification to console (fallback/testing)."""
    print(f"\n{'=' * 60}")
    print(f"[{title}]")
    print(f"{'=' * 60}")
    print(message)
    print(f"{'=' * 60}\n")
