"""
Live chat checker for Claude/Codex turns.

Claude uses this via a native UserPromptSubmit hook. Codex does not have an
equivalent project hook here, so Codex should call the same script from
AGENTS.md at the start of every turn.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "data" / "state"
CHAT_FILE = BASE_DIR / "discussions" / "live-chat.jsonl"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check live chat for unread messages from other participants.")
    parser.add_argument(
        "--agent",
        default="claude",
        help="Participant name for this reader, used for filtering and state tracking.",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        help="Optional explicit state file. Defaults to an agent-specific file in .claude/data/state/.",
    )
    return parser.parse_args()


def normalize_agent(agent: str) -> str:
    normalized = agent.strip().lower()
    return normalized or "claude"


def state_file_for_agent(agent: str) -> Path:
    # Keep Claude's legacy state file so the existing hook does not lose its cursor.
    if agent == "claude":
        return STATE_DIR / "live-chat-pos.json"
    return STATE_DIR / f"live-chat-pos-{agent}.json"


def load_last_pos(state_file: Path) -> int:
    if not state_file.exists():
        return 0
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        return max(0, int(state.get("pos", 0)))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def save_pos(state_file: Path, agent: str, pos: int) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({"agent": agent, "pos": max(0, pos)}, ensure_ascii=True),
        encoding="utf-8",
    )


def read_new_lines(last_pos: int) -> tuple[list[str], int]:
    if not CHAT_FILE.exists():
        return [], last_pos

    file_size = CHAT_FILE.stat().st_size
    safe_pos = 0 if last_pos > file_size else last_pos

    with CHAT_FILE.open("r", encoding="utf-8") as handle:
        handle.seek(safe_pos)
        lines = handle.readlines()
        new_pos = handle.tell()

    return lines, new_pos


def format_messages(agent: str, lines: list[str]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        author = str(message.get("from", "")).strip().lower()
        if author == agent:
            continue

        messages.append(message)
    return messages


def main() -> None:
    args = parse_args()
    agent = normalize_agent(args.agent)
    state_file = args.state_file or state_file_for_agent(agent)
    last_pos = load_last_pos(state_file)
    lines, new_pos = read_new_lines(last_pos)

    save_pos(state_file, agent, new_pos)

    if not lines:
        return

    messages = format_messages(agent, lines)
    if not messages:
        return

    print("[LIVE CHAT] New messages since last check:")
    for message in messages:
        timestamp = str(message.get("ts", ""))[11:19]
        author = str(message.get("from", "unknown")).upper()
        text = str(message.get("msg", ""))
        line = f"  [{timestamp}] {author}: {text}"
        console_encoding = sys.stdout.encoding or "utf-8"
        safe_line = line.encode(console_encoding, errors="replace").decode(console_encoding)
        print(safe_line)
    print("[/LIVE CHAT]")
    print(
        f"IMPORTANT: If a reply is needed, send it in the live chat first: "
        f'python .claude/discussions/live_chat.py send {agent} "your response"'
    )
    print("After handling the live chat, continue with the user's task.")


if __name__ == "__main__":
    main()
