"""Internal authenticated event ingest for the optional native hook adapter."""

from __future__ import annotations

import json
import os
import sys


def main():
    from personas import apply_persona_override

    apply_persona_override()
    from runtime.claude_function_hooks import ingest

    raw = sys.stdin.read(65537)
    if len(raw) > 65536:
        raise ValueError("hook envelope too large")
    payload = json.loads(raw)
    result = ingest(
        os.environ.get("HOMIE_COGNITION_HOOK_CONTEXT", ""),
        os.environ.get("HOMIE_COGNITION_HOOK_TOKEN", ""),
        payload,
    )
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__}))
        sys.exit(1)
