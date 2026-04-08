"""
Session Start Context Injection Hook

Called by Claude Code when a session starts. Reads key memory files and
outputs a context summary as JSON on stdout, which Claude Code injects
into the assistant's context.

This hook does NO API calls — pure local file reads for speed (<15s).
"""

from __future__ import annotations

import json
import sys
import time as _time
from pathlib import Path

# Add scripts directory to path for config imports
_scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_scripts_dir))

from runtime.bootstrap import build_session_start_context  # noqa: E402
from shared import log_hook_execution  # noqa: E402

def main() -> None:
    """Main hook entry point. Reads stdin, builds context, outputs JSON on stdout."""
    _start = _time.time()

    # Read hook input from stdin
    try:
        hook_input: dict[str, object] = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        # If we can't parse input, output empty context and exit cleanly
        log_hook_execution("session-start", "unknown", "ERROR", _time.time() - _start, str(e))
        sys.exit(0)

    source = hook_input.get("source", "startup")
    if not isinstance(source, str):
        source = "startup"

    # Build context from memory files
    context = build_session_start_context(source)

    if not context.strip():
        log_hook_execution("session-start", source, "SKIP", _time.time() - _start, "empty context")
        sys.exit(0)

    # Output JSON for Claude Code to inject as context
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }

    # CRITICAL: Only valid JSON on stdout. No other output.
    json.dump(output, sys.stdout)
    log_hook_execution("session-start", source, "OK", _time.time() - _start, f"{len(context)} chars")


if __name__ == "__main__":
    main()
