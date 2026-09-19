"""Scheduled daily brief: a persona posts its own card to its own channel.

Opt-in is PHYSICAL, not configured (Rule 2): a persona receives a daily brief
iff ``<profile>/memory/BRIEF.md`` exists and carries an ``## Instruction``
section. Delete the file and the brief stops. There is no config key to drift
out of sync with the file that actually drives the prompt.

The tick runs as the DEFAULT profile, spawns each eligible persona as its own
subprocess (never an in-process profile switch — ``config.py`` binds paths at
import time), and posts the result to that persona's bound Discord channel via
the REST API. REST, not a gateway connection: the running bot owns the only
gateway session and must not be disturbed by a scheduled job.

Fail-open per persona — one bad brief never costs the roster its morning.
Exit is non-zero when any persona failed, so the scheduler wrapper can log it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from personas import apply_persona_override

apply_persona_override()

import config  # noqa: E402
from personas.capabilities import build_capability_scoped_env  # noqa: E402
from personas.lifecycle import list_profiles  # noqa: E402
from personas.services import is_active_default_profile  # noqa: E402
from security import kill_switches  # noqa: E402
from shared import load_state, save_state  # noqa: E402

_SCRIPTS_DIR = Path(__file__).resolve().parent

# Discord hard-caps a message at 2000 characters. Leave room for the marker so
# a long card is visibly cut rather than rejected by the API.
_DISCORD_LIMIT = 2000
_TRUNCATION_MARKER = "\n\n… (card truncated)"

_INSTRUCTION_RE = re.compile(
    r"^##\s+Instruction\s*$(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _settings() -> tuple[bool, int, int]:
    """Resolve knobs at call time (Rule 1 — never bound at import)."""
    enabled = os.getenv("PERSONA_BRIEF_ENABLED", "true").strip().lower() != "false"
    interval = int(os.getenv("PERSONA_BRIEF_INTERVAL_HOURS", "20"))
    timeout = int(os.getenv("PERSONA_BRIEF_TIMEOUT", "600"))
    return enabled, interval, timeout


def read_instruction(profile_root: Path) -> str:
    """Return the BRIEF.md ``## Instruction`` body, or "" when not eligible.

    Absent file, unreadable file, or a file with no Instruction section all
    mean "this persona has no daily brief" — never an exception.
    """
    brief = profile_root / "memory" / "BRIEF.md"
    try:
        text = brief.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _INSTRUCTION_RE.search(text)
    if not match:
        return ""
    return match.group(1).strip()


def resolve_channel(persona_id: str) -> str:
    """Return the persona's ENABLED Discord channel id, or "".

    Mirrors the adapter's contract: a row with no ``enabled`` key counts as
    enabled (absent means on), an explicit ``false`` means off.
    """
    try:
        raw = json.loads(
            (config.DATA_DIR / "discord-channel-bindings.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError):
        return ""
    channels = raw.get("channels")
    if not isinstance(channels, dict):
        return ""
    for channel_id, row in channels.items():
        if not isinstance(row, dict):
            continue
        if str(row.get("persona") or "").strip() != persona_id:
            continue
        if row.get("enabled", True) is False:
            continue
        return str(channel_id).strip()
    return ""


def _generate(persona_id: str, profile_root: Path, instruction: str,
              timeout: int) -> tuple[str, str]:
    """Run the persona once. Returns (text, error) — exactly one is truthy."""
    try:
        env = build_capability_scoped_env(persona_id, profile_root=profile_root)
        result = subprocess.run(
            [
                sys.executable, "-m", "cli_entry", "chat",
                "-q", instruction,
                "--source", "cron",
            ],
            cwd=str(_SCRIPTS_DIR),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "", f"timeout after {timeout}s"
    except Exception as exc:  # noqa: BLE001 — fail-open per persona
        return "", f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-400:]
        return "", f"exit={result.returncode} {detail}"
    text = _strip_cli_noise(result.stdout or "")
    if not text:
        return "", "empty response"
    return text, ""


def _strip_cli_noise(stdout: str) -> str:
    """Drop the CLI's bracketed log lines and trailing receipt block."""
    lines: list[str] = []
    for line in stdout.splitlines():
        if re.match(r"^\[\d{4}-\d{2}-\d{2} ", line):
            continue
        if re.match(r"^(session_id|lane|provider|model|cost_usd|tool_calls):", line):
            continue
        if line.strip() == "---":
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _post(channel_id: str, text: str) -> tuple[str, str]:
    """POST one message. Returns (message_id, error)."""
    token = (os.getenv("DISCORD_BOT_TOKEN") or "").strip()
    if not token:
        return "", "DISCORD_BOT_TOKEN not set"
    if len(text) > _DISCORD_LIMIT:
        keep = _DISCORD_LIMIT - len(_TRUNCATION_MARKER)
        text = text[:keep].rstrip() + _TRUNCATION_MARKER
    try:
        import httpx

        resp = httpx.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}"},
            json={"content": text},
            timeout=30.0,
        )
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}: {exc}"
    if resp.status_code not in (200, 201):
        return "", f"http={resp.status_code} {resp.text[:200]}"
    return str(resp.json().get("id") or ""), ""


def _due(last_success: str | None, interval_hours: int) -> bool:
    if not last_success:
        return True
    try:
        parsed = datetime.fromisoformat(last_success)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        elapsed = datetime.now(UTC) - parsed.astimezone(UTC)
        return elapsed.total_seconds() >= interval_hours * 3600
    except (TypeError, ValueError):
        return True


def run_tick(*, test_mode: bool = False, once: bool = False) -> int:
    if not is_active_default_profile():
        print("PERSONA_BRIEF: parent must run under the default profile")
        return 2
    enabled, interval, timeout = _settings()
    if not enabled:
        print("PERSONA_BRIEF: disabled via PERSONA_BRIEF_ENABLED")
        return 0
    if kill_switches.is_disabled("persona_brief"):
        print("PERSONA_BRIEF: persona_brief kill switch disabled")
        return 0

    failures = 0
    eligible = 0
    for profile in list_profiles():
        if profile.is_default:
            continue
        instruction = read_instruction(profile.path)
        if not instruction:
            continue
        eligible += 1
        name = profile.name

        channel = resolve_channel(name)
        if not channel:
            print(f"PERSONA_BRIEF [{name}]: no enabled Discord binding — skipped")
            failures += 1
            continue

        state_path = config.STATE_DIR / f"persona-brief-{name}-state.json"
        try:
            state = load_state(state_path)
        except OSError as exc:
            print(f"PERSONA_BRIEF [{name}]: state unreadable ({exc}) — skipped")
            failures += 1
            continue

        if not _due(state.get("last_success"), interval):
            print(f"PERSONA_BRIEF [{name}]: recency guard")
            if once:
                break
            continue

        if test_mode:
            print(
                f"PERSONA_BRIEF [{name}]: would post to {channel} "
                f"({len(instruction)} char instruction)"
            )
            if once:
                break
            continue

        state["last_attempt"] = _now()
        _save(state, state_path, name)

        text, err = _generate(name, profile.path, instruction, timeout)
        if err:
            print(f"PERSONA_BRIEF [{name}]: generate failed — {err}")
            state["last_result"] = f"generate_failed: {err}"
            _save(state, state_path, name)
            failures += 1
            if once:
                break
            continue

        message_id, err = _post(channel, text)
        if err:
            print(f"PERSONA_BRIEF [{name}]: post failed — {err}")
            state["last_result"] = f"post_failed: {err}"
            _save(state, state_path, name)
            failures += 1
            if once:
                break
            continue

        # last_success advances ONLY on a confirmed post. A generated-but-
        # undelivered card must retry tomorrow, not be marked done.
        state["last_success"] = _now()
        state["last_result"] = f"posted message_id={message_id}"
        _save(state, state_path, name)
        print(
            f"PERSONA_BRIEF [{name}]: posted {len(text)} chars "
            f"-> channel {channel} (message_id={message_id})"
        )
        if once:
            break

    if not eligible:
        print("PERSONA_BRIEF: no persona has a BRIEF.md Instruction section")
    return 1 if failures else 0


def _save(state: dict, path: Path, name: str) -> None:
    """Stamp bookkeeping; a stamp failure is reported, never raised."""
    try:
        save_state(state, path)
    except OSError as exc:
        print(f"PERSONA_BRIEF [{name}]: state write failed ({exc})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true",
                        help="dry run — resolve and report, never generate or post")
    parser.add_argument("--once", action="store_true",
                        help="stop after the first eligible persona")
    args = parser.parse_args()
    return run_tick(test_mode=args.test, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
