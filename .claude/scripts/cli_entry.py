"""Trampoline entry point for `thehomie` CLI.

pyproject.toml lives in .claude/scripts/ but cli.py lives in .claude/chat/.
This 5-line bridge adds the chat dir to sys.path and calls cli.main().
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "chat"))
sys.path.insert(0, str(Path(__file__).parent))

from cli import main  # noqa: E402

if __name__ == "__main__":
    main()
