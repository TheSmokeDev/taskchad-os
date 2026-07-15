"""Detached/scheduled worker for :mod:`framework_update`."""

from __future__ import annotations

import argparse
import json
import os
import signal
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from framework_update import FrameworkUpdater, resolve_repo_root


class BotRestarter:
    def __init__(self) -> None:
        self.old_pid: int | None = None

    @staticmethod
    def _pid_file() -> Path:
        configured = os.getenv("HOMIE_UPDATE_BOT_PID_FILE", "").strip()
        if configured:
            return Path(configured)
        import config

        return Path(config.BOT_PID_FILE)

    def __call__(self) -> dict[str, Any]:
        pid_file = self._pid_file()
        try:
            self.old_pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"cannot resolve bot PID from {pid_file}: {exc}") from exc
        if self.old_pid == os.getpid():
            raise RuntimeError("updater worker cannot restart itself")
        os.kill(self.old_pid, signal.SIGTERM)
        deadline = time.monotonic() + float(os.getenv("HOMIE_UPDATE_RESTART_TIMEOUT", "60"))
        while time.monotonic() < deadline:
            try:
                os.kill(self.old_pid, 0)
            except ProcessLookupError:
                return {"old_pid": self.old_pid, "signal": "SIGTERM"}
            except PermissionError as exc:
                raise RuntimeError(f"cannot inspect bot PID {self.old_pid}: {exc}") from exc
            time.sleep(0.25)
        raise RuntimeError(f"bot PID {self.old_pid} did not exit before restart timeout")


class HealthVerifier:
    def __init__(self, restarter: BotRestarter) -> None:
        self.restarter = restarter

    def __call__(self) -> dict[str, Any]:
        from personas.services import get_health_check_port

        url = os.getenv(
            "HOMIE_UPDATE_HEALTH_URL", f"http://127.0.0.1:{get_health_check_port()}/health"
        ).strip()
        required_adapter = os.getenv("HOMIE_UPDATE_REQUIRED_ADAPTER", "").strip().lower()
        timeout = float(os.getenv("HOMIE_UPDATE_HEALTH_TIMEOUT", "120"))
        deadline = time.monotonic() + timeout
        last_detail = "no response"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                adapters = payload.get("adapters") or {}
                healthy = payload.get("status") in {"ok", "healthy"}
                adapter_ok = not required_adapter or adapters.get(required_adapter) is True
                if healthy and adapter_ok:
                    new_pid = None
                    try:
                        new_pid = int(self.restarter._pid_file().read_text().strip())
                    except (OSError, ValueError):
                        pass
                    if self.restarter.old_pid is None or new_pid != self.restarter.old_pid:
                        return {
                            "url": url,
                            "status": payload.get("status"),
                            "required_adapter": required_adapter or None,
                            "new_pid": new_pid,
                        }
                last_detail = f"status={payload.get('status')} adapters={adapters}"
            except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                last_detail = str(exc)
            time.sleep(2)
        raise RuntimeError(f"health verification timed out: {last_detail}")


def _receipt_message(receipt: dict[str, Any]) -> str:
    target = receipt.get("target_tag") or receipt.get("target_version") or "latest stable"
    if receipt.get("success"):
        revision = str(receipt.get("applied_revision") or "")[:8]
        return (
            f"The Homie update complete: {target} ({revision}). "
            f"Receipt {receipt.get('receipt_id')}."
        )
    return (
        f"The Homie update {receipt.get('status')}: "
        f"{receipt.get('blocker') or 'unknown error'}. "
        f"Receipt {receipt.get('receipt_id')}."
    )


def _notify_requester(requester: dict[str, str] | None, receipt: dict[str, Any]) -> None:
    if not requester:
        return
    platform = requester.get("platform", "").lower()
    channel = requester.get("channel", "")
    if not channel:
        return
    body = _receipt_message(receipt)
    try:
        import config

        if platform == "discord" and config.DISCORD_BOT_TOKEN:
            request = urllib.request.Request(
                f"https://discord.com/api/v10/channels/{channel}/messages",
                data=json.dumps({"content": body}).encode("utf-8"),
                headers={
                    "Authorization": f"Bot {config.DISCORD_BOT_TOKEN}",
                    "Content-Type": "application/json",
                    "User-Agent": "thehomie-safe-updater",
                },
                method="POST",
            )
            urllib.request.urlopen(request, timeout=10).close()
        elif platform == "telegram" and config.TELEGRAM_BOT_TOKEN:
            request = urllib.request.Request(
                f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                data=json.dumps({"chat_id": channel, "text": body}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(request, timeout=10).close()
    except Exception:
        # The receipt is durable; notification delivery is best-effort and must
        # never change the update/rollback result.
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply the latest stable Homie release")
    parser.add_argument("--repo", default=None)
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--requester-json", default="")
    parser.add_argument("--requester-file", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    requester = json.loads(args.requester_json) if args.requester_json else None
    if requester is None and args.requester_file:
        request_file = Path(args.requester_file)
        try:
            requester = json.loads(request_file.read_text(encoding="utf-8")) or None
        except (OSError, json.JSONDecodeError):
            requester = None
        finally:
            request_file.unlink(missing_ok=True)
    root = resolve_repo_root(args.repo)
    updater = FrameworkUpdater(root)
    restarter = BotRestarter() if args.restart else None
    verifier = HealthVerifier(restarter) if restarter else None
    receipt = updater.apply(
        requester=requester,
        scheduled=args.scheduled and requester is None,
        restart=restarter,
        health_check=verifier,
        lock_timeout=0.1,
    )
    payload = receipt.to_dict()
    if args.scheduled and not receipt.success and requester is None:
        admin_platform = os.getenv("HOMIE_UPDATE_ADMIN_PLATFORM", "").strip()
        admin_channel = os.getenv("HOMIE_UPDATE_ADMIN_CHANNEL", "").strip()
        if admin_platform and admin_channel:
            requester = {"platform": admin_platform, "channel": admin_channel, "thread": ""}
    _notify_requester(requester, payload)
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(_receipt_message(payload))
    return 0 if receipt.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
