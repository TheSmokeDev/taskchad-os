"""Sync digests + eval notes into the Repo Scout persona's memory.

The Scout's Discord channel turns are tool-denied; his per-persona recall
(`<profile>/data/memory.db`) is the ONLY way he knows what the pipelines
produced. This copies each artifact into `<profile>/memory/research/
github-signal/` and refreshes the persona index (incremental — content-hash
skip makes re-runs cheap).

Fail-open everywhere: no profile knob, no profile dir, copy errors, or an
index failure never raise — the digest/eval pipelines must not care whether
the persona exists.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from github_signal.config import get_github_signal_settings  # noqa: E402


def sync_to_scout(source_paths: list[Path], *, profile: str | None = None) -> bool:
    """Copy artifacts into the scout profile's research memory + reindex.

    Returns True when at least the copy happened (the durable part); the
    index refresh is best-effort. Returns False when the sync is off or the
    profile is absent. Never raises.
    """
    try:
        if profile is None:
            profile = get_github_signal_settings().scout_profile
        if not profile:
            return False

        from personas import core as personas_core

        profile_root = personas_core.get_default_homie_root() / "profiles" / profile
        if not profile_root.is_dir():
            return False

        dest_dir = profile_root / "memory" / "research" / "github-signal"
        dest_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for source in source_paths:
            source = Path(source)
            if not source.is_file():
                continue
            shutil.copyfile(source, dest_dir / source.name)
            copied += 1
        if not copied:
            return False

        try:
            subprocess.run(
                ["uv", "run", "python", "memory_index.py", "-p", profile],
                cwd=str(_SCRIPTS_DIR),
                capture_output=True,
                timeout=600,
            )
        except Exception as exc:
            print(f"[scout_sync] index refresh failed (non-fatal): {exc}")
        return True
    except Exception as exc:
        print(f"[scout_sync] sync failed (non-fatal): {exc}")
        return False
