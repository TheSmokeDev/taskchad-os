"""Trampoline entry point for `thehomie` CLI.

pyproject.toml lives in .claude/scripts/ but cli.py lives in .claude/chat/.
This 5-line bridge adds the chat dir to sys.path and calls cli.main().
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "chat"))
sys.path.insert(0, str(Path(__file__).parent))

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

from cli import main  # noqa: E402

if __name__ == "__main__":
    main()
