"""Test Phase 2 / WS-chat — recap.py ``build_recap`` (pure, zero-LLM).

Asserts the Hermes ``session_recap`` algorithm ported into ``.claude/chat/
recap.py`` reads the HOMIE persisted tool-call shape (``{"name","arguments"}``,
arg key ``file_path``/``notebook_path``) — NOT the OpenAI nested
``{"function":{...}}`` shape — and covers each output branch: empty session,
no-tool session, tool histogram + files-touched, JSON-string arguments, the
20-turn window cap on long history, colon-bearing session ids, and the
prompt/reply truncation limits.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import recap  # type: ignore[import-not-found]  # noqa: E402
from recap import build_recap  # type: ignore[import-not-found]  # noqa: E402


def _assistant_with_tools(tool_calls: list[dict], content: str = "done") -> dict:
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


# ---------------------------------------------------------------------------
# Empty / trivial branches
# ---------------------------------------------------------------------------


def test_empty_messages_says_nothing_to_recap() -> None:
    out = build_recap([])
    assert "Session recap" in out
    assert "nothing to recap" in out


def test_no_tool_calls_has_no_tools_line_but_shows_exchange() -> None:
    messages = [
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": "hi there"},
    ]
    out = build_recap(messages)
    assert "Tools used:" not in out
    assert "Files touched:" not in out
    assert "Last ask: hey" in out
    assert "Last reply: hi there" in out


def test_only_tool_message_triggers_no_activity_fallback() -> None:
    # A window with only a tool result (no user/assistant text) → header + scope
    # only → the "(no assistant activity...)" fallback line.
    messages = [{"role": "tool", "content": "some tool output"}]
    out = build_recap(messages)
    assert "no assistant activity yet in this window" in out


# ---------------------------------------------------------------------------
# Homie tool-call shape — histogram + files touched
# ---------------------------------------------------------------------------


def test_tool_histogram_and_files_from_homie_shape() -> None:
    messages = [
        {"role": "user", "content": "edit the files"},
        _assistant_with_tools(
            [
                {"name": "Write", "arguments": {"file_path": "foo.py"}},
                {"name": "Read", "arguments": {"file_path": "bar.py"}},
                {"name": "Write", "arguments": {"file_path": "foo.py"}},
            ]
        ),
    ]
    out = build_recap(messages)
    assert "Tools used:" in out
    assert "Write×2" in out
    assert "Read×1" in out
    assert "Files touched:" in out
    assert "foo.py" in out
    assert "bar.py" in out


def test_notebook_edit_uses_notebook_path_arg() -> None:
    messages = [
        {"role": "user", "content": "update the notebook"},
        _assistant_with_tools(
            [{"name": "NotebookEdit", "arguments": {"notebook_path": "analysis.ipynb"}}]
        ),
    ]
    out = build_recap(messages)
    assert "NotebookEdit×1" in out
    assert "analysis.ipynb" in out


def test_arguments_as_json_string_is_parsed() -> None:
    messages = [
        {"role": "user", "content": "go"},
        _assistant_with_tools(
            [{"name": "Edit", "arguments": json.dumps({"file_path": "baz.py"})}]
        ),
    ]
    out = build_recap(messages)
    assert "Edit×1" in out
    assert "baz.py" in out


def test_openai_nested_function_shape_is_ignored() -> None:
    # A defensive check: the OLD Hermes {"function":{...}} shape must NOT be
    # read (no "name" key at top level → no tool counted).
    messages = [
        {"role": "user", "content": "go"},
        _assistant_with_tools(
            [{"function": {"name": "Write", "arguments": {"file_path": "x.py"}}}]
        ),
    ]
    out = build_recap(messages)
    assert "Tools used:" not in out
    assert "Files touched:" not in out


# ---------------------------------------------------------------------------
# Window cap + edge inputs
# ---------------------------------------------------------------------------


def test_long_history_caps_window_to_20_turns() -> None:
    messages: list[dict] = []
    for i in range(125):
        messages.append({"role": "user", "content": f"q{i}"})
        messages.append({"role": "assistant", "content": f"a{i}"})
    out = build_recap(messages)
    # 250 messages, 125/125 total; window caps to 20 turns (10 user / 10 asst).
    assert "10 user turns / 10 assistant replies" in out
    assert "(of 125/125 total)" in out


def test_colon_bearing_session_id_is_opaque_string() -> None:
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
    ]
    out = build_recap(messages, session_id="telegram:12345:67890")
    # session_id[:8] => "telegram"; treated as a string, never a path.
    assert "Session recap — telegram" in out


def test_last_ask_truncated_at_limit() -> None:
    long_prompt = "x" * 300
    messages = [
        {"role": "user", "content": long_prompt},
        {"role": "assistant", "content": "ok"},
    ]
    out = build_recap(messages)
    ask_line = next(line for line in out.splitlines() if "Last ask:" in line)
    assert "…" in ask_line
    # "  Last ask: " prefix + <=140 chars.
    assert len(ask_line) <= len("  Last ask: ") + recap._PROMPT_PREVIEW_CHARS


def test_last_reply_truncated_at_limit() -> None:
    long_reply = "y" * 400
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": long_reply},
    ]
    out = build_recap(messages)
    reply_line = next(line for line in out.splitlines() if "Last reply:" in line)
    assert "…" in reply_line
    assert len(reply_line) <= len("  Last reply: ") + recap._ASSISTANT_PREVIEW_CHARS


def test_content_as_block_list_is_flattened() -> None:
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "block prompt"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "block reply"}]},
    ]
    out = build_recap(messages)
    assert "Last ask: block prompt" in out
    assert "Last reply: block reply" in out
