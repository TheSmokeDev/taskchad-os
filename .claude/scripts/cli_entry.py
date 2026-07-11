"""Trampoline entry point for `thehomie` CLI.

pyproject.toml lives in .claude/scripts/ but cli.py lives in .claude/chat/.
This 5-line bridge adds the chat dir to sys.path and calls cli.main().
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "chat"))
sys.path.insert(0, str(Path(__file__).parent))

# Console-script boundary hardening (real terminal invocations only — tests
# import `cli.main` directly and never pass through this module):
# 1. Force UTF-8 stdio so cp1252 Windows consoles don't mangle vault content
#    to `?` (recall output carries em-dashes/box chars from notes).
# 2. Mark the console-entry context so cli.py may hard-exit after printing,
#    skipping ProactorEventLoop transport `__del__` teardown spew.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
os.environ["THEHOMIE_CONSOLE_ENTRY"] = "1"

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

from cli import main  # noqa: E402

if __name__ == "__main__":
    main()
