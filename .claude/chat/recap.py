"""Session recap — summarize what's happened in the current session.

Ported from Hermes v0.18 ``hermes_cli/session_recap.py`` (algorithm verbatim).
Inspired by Claude Code's ``/recap`` command (v2.1.114, April 2026), which
shows a one-line summary of what happened while a terminal was unfocused so an
operator juggling multiple sessions can re-orient quickly.

Differences from Claude Code / re-anchors for The Homie:
    - Pure local computation over the persisted chat.db history. No LLM call,
      no auxiliary model, no prompt-cache invalidation. A recap is instant and
      free.
    - Works unchanged on CLI and every gateway platform (Telegram, Discord,
      Slack, ...) because they all call into this same ``build_recap`` helper.
    - Homie persists tool calls as ``asdict(RuntimeToolCall)`` —
      ``{"id","name","arguments","provider_type","status"}`` — NOT the OpenAI
      nested ``{"function":{"name","arguments"}}`` shape. ``build_recap`` reads
      ``tc["name"]`` + ``tc["arguments"]`` directly (see CRITICAL 2 in the PRP).
    - File-edit tool vocabulary is the Claude-SDK / Homie set:
      ``Write`` / ``Edit`` / ``Read`` (arg ``file_path``) and ``NotebookEdit``
      (arg ``notebook_path``).
"""
from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

# How many recent user/assistant turns we consider "recent activity".
_RECENT_TURN_WINDOW = 20

# How many characters of the latest user prompt to show.
_PROMPT_PREVIEW_CHARS = 140

# How many characters of the latest assistant text to show.
_ASSISTANT_PREVIEW_CHARS = 200

# How many recently-touched files to list.
_MAX_FILES_LISTED = 5

# Tool names that identify a file-editing action and the argument key that
# holds the path. Homie/Claude-SDK tool vocabulary (NOT the Hermes
# terminal/patch/write_file set).
_FILE_EDIT_TOOLS: Mapping[str, str] = {
    "Write": "file_path",
    "Edit": "file_path",
    "Read": "file_path",
    "NotebookEdit": "notebook_path",
}


def _coerce_text(value: Any) -> str:
    """Flatten assistant/user ``content`` into a plain string.

    Content can be a string or a list of content blocks (for multimodal or
    reasoning models). We concatenate every text-like block and ignore the
    rest.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
                continue
            if isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts)
    return str(value)


def _tool_call_name_and_args(tool_call: Any) -> tuple[str, Mapping[str, Any]]:
    """Extract ``(name, arguments_dict)`` from a persisted tool_call entry.

    Homie shape is ``asdict(RuntimeToolCall)``: ``{"name": "Write",
    "arguments": {"file_path": ...}, ...}``. ``arguments`` may be a dict, a
    JSON string, or ``None`` depending on provider. Return an empty dict if it
    cannot be parsed.
    """
    if not isinstance(tool_call, Mapping):
        return "", {}
    name = str(tool_call.get("name") or "") or ""
    raw_args = tool_call.get("arguments")
    if isinstance(raw_args, Mapping):
        return name, raw_args
    if isinstance(raw_args, str) and raw_args:
        try:
            import json

            parsed = json.loads(raw_args)
            if isinstance(parsed, Mapping):
                return name, parsed
        except Exception:
            return name, {}
    return name, {}


def _iter_assistant_tool_calls(
    messages: Sequence[Mapping[str, Any]],
) -> Iterable[tuple[str, Mapping[str, Any]]]:
    for msg in messages:
        if not isinstance(msg, Mapping):
            continue
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            name, args = _tool_call_name_and_args(tc)
            if name:
                yield name, args


def _count_visible_turns(
    messages: Sequence[Mapping[str, Any]],
) -> tuple[int, int, int]:
    """Return ``(user_turn_count, assistant_turn_count, tool_message_count)``."""
    users = assistants = tools = 0
    for msg in messages:
        if not isinstance(msg, Mapping):
            continue
        role = msg.get("role")
        if role == "user":
            users += 1
        elif role == "assistant":
            assistants += 1
        elif role == "tool":
            tools += 1
    return users, assistants, tools


def _latest_user_prompt(
    messages: Sequence[Mapping[str, Any]],
) -> str | None:
    for msg in reversed(messages):
        if isinstance(msg, Mapping) and msg.get("role") == "user":
            text = _coerce_text(msg.get("content")).strip()
            if text:
                return text
    return None


def _latest_assistant_text(
    messages: Sequence[Mapping[str, Any]],
) -> str | None:
    for msg in reversed(messages):
        if not isinstance(msg, Mapping):
            continue
        if msg.get("role") != "assistant":
            continue
        text = _coerce_text(msg.get("content")).strip()
        if text:
            return text
    return None


def _recent_window(
    messages: Sequence[Mapping[str, Any]], window: int = _RECENT_TURN_WINDOW
) -> list[Mapping[str, Any]]:
    """Return the tail slice of ``messages`` covering at most ``window``
    user+assistant turns (tool messages ride along inside the window).

    Iterating from the end, we count user and assistant messages and keep
    everything from the first message that falls within the window.
    """
    count = 0
    cut = 0
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, Mapping) and msg.get("role") in {"user", "assistant"}:
            count += 1
            if count >= window:
                cut = i
                break
    else:
        return list(messages)
    return list(messages[cut:])


def _shortened_path(path: str) -> str:
    """Show a path relative to cwd when possible, otherwise with ~ expansion."""
    if not path:
        return path
    try:
        abs_path = os.path.abspath(os.path.expanduser(path))
        cwd = os.getcwd()
        if abs_path == cwd:
            return "."
        if abs_path.startswith(cwd + os.sep):
            return abs_path[len(cwd) + 1 :]
        home = os.path.expanduser("~")
        if abs_path.startswith(home + os.sep):
            return "~/" + abs_path[len(home) + 1 :]
        return abs_path
    except Exception:
        return path


def _summarise_tool_activity(
    tool_calls: Sequence[tuple[str, Mapping[str, Any]]],
) -> tuple[list[tuple[str, int]], list[str]]:
    """Return ``(tool_counts_sorted, recently_edited_files)``.

    ``tool_counts_sorted`` is descending by count, keeping the full list so
    callers can truncate for display. ``recently_edited_files`` lists distinct
    paths (most recent first) from file-editing tools.
    """
    counter: Counter[str] = Counter()
    files_seen: list[str] = []
    files_set: set[str] = set()
    # Walk in reverse so "most recent first" drops out of order-preserved iteration.
    for name, args in reversed(list(tool_calls)):
        counter[name] += 1
        arg_key = _FILE_EDIT_TOOLS.get(name)
        if arg_key:
            path = args.get(arg_key)
            if isinstance(path, str) and path and path not in files_set:
                files_set.add(path)
                files_seen.append(_shortened_path(path))
    # Counter ignores order, so only files_seen needed the reversal; it is now
    # newest->oldest which is what we want for display.
    tool_counts = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return tool_counts, files_seen


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())  # collapse newlines for a compact one-liner
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_recap(
    messages: Sequence[Mapping[str, Any]],
    *,
    session_title: str | None = None,
    session_id: str | None = None,
    platform: str | None = None,
) -> str:
    """Build a multi-line recap of recent activity.

    Inputs:
        messages: the full conversation history as a list of chat-style dicts
            (``role``, ``content``, ``tool_calls``, ...).
        session_title: optional human title (from the session store).
        session_id: optional session id. May be a colon-bearing composite chat
            key (e.g. ``telegram:123:456``) — treated as an opaque string, never
            a path.
        platform: optional hint (``"cli"``, ``"telegram"``, ...). Does not
            change behavior today but is accepted for forward compat.

    The output is plain text designed to render well in both a terminal (with
    80-col wrapping) and a gateway message bubble.
    """
    _ = platform  # reserved for future use
    lines: list[str] = []

    header_bits: list[str] = ["Session recap"]
    if session_title:
        header_bits.append(f"— {session_title}")
    elif session_id:
        header_bits.append(f"— {session_id[:8]}")
    lines.append(" ".join(header_bits))

    if not messages:
        lines.append("  (nothing to recap — no messages yet)")
        return "\n".join(lines)

    users, assistants, tool_msgs = _count_visible_turns(messages)
    window = _recent_window(messages)
    win_users, win_assistants, _ = _count_visible_turns(window)

    scope = (
        f"{win_users} user turn{'s' if win_users != 1 else ''} / "
        f"{win_assistants} assistant repl{'ies' if win_assistants != 1 else 'y'}"
    )
    if (users, assistants) != (win_users, win_assistants):
        scope += f" (of {users}/{assistants} total)"
    lines.append(f"  Recent: {scope}, {tool_msgs} tool result{'s' if tool_msgs != 1 else ''}")

    tool_calls = list(_iter_assistant_tool_calls(window))
    tool_counts, files = _summarise_tool_activity(tool_calls)
    if tool_counts:
        top = ", ".join(f"{name}×{count}" for name, count in tool_counts[:5])
        extra = len(tool_counts) - 5
        if extra > 0:
            top += f" (+{extra} more)"
        lines.append(f"  Tools used: {top}")
    if files:
        shown = files[:_MAX_FILES_LISTED]
        extra = len(files) - len(shown)
        entry = ", ".join(shown)
        if extra > 0:
            entry += f" (+{extra} more)"
        lines.append(f"  Files touched: {entry}")

    latest_user = _latest_user_prompt(window)
    if latest_user:
        lines.append(f"  Last ask: {_truncate(latest_user, _PROMPT_PREVIEW_CHARS)}")

    latest_reply = _latest_assistant_text(window)
    if latest_reply:
        lines.append(f"  Last reply: {_truncate(latest_reply, _ASSISTANT_PREVIEW_CHARS)}")

    if len(lines) == 2:
        # Only the header + scope line — nothing substantive to show.
        lines.append("  (no assistant activity yet in this window)")

    return "\n".join(lines)


__all__ = ["build_recap"]
