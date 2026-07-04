"""
Persona Learning Tick — Scheduled fan-out for persona reflection pipelines.

Enumerates learning-enabled personas via call-time config reads and spawns
per-persona reflection (memory_reflect.py -p <name>) as subprocesses on
cheap background model tiers. One cron/scheduler entry for ALL personas.

CRITICAL: config.py:40 binds paths at import time. The tick itself runs as
the DEFAULT profile and NEVER loops profiles in-process — each persona
pipeline runs as a subprocess with HOMIE_HOME set by build_capability_scoped_env.

Usage:
    uv run python persona_learning_tick.py           # Run learning tick
    uv run python persona_learning_tick.py --test    # Dry run (no subprocess spawn)
    uv run python persona_learning_tick.py --once    # Single persona (first eligible)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override

apply_persona_override()

from config import (  # noqa: E402
    STATE_DIR,
    get_background_models,
    get_persona_learning_settings,
)
from personas import get_default_paths  # noqa: E402
from personas.capabilities import build_capability_scoped_env  # noqa: E402
from personas.lifecycle import list_profiles  # noqa: E402
from personas.services import is_active_default_profile, load_persona_config  # noqa: E402
from shared import load_state, save_state  # noqa: E402

# Inject .claude/chat for session store access
_CHAT_DIR = Path(__file__).resolve().parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from session import get_session_store  # noqa: E402

_SCRIPTS_DIR = Path(__file__).resolve().parent


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _persona_state_file(persona_name: str) -> Path:
    return STATE_DIR / f"persona-learning-{persona_name}-state.json"


def _count_attributed_rows_since(
    persona_id: str,
    since_iso: str | None,
    chat_db_path: Path,
) -> int:
    """Count sessions with this persona_id updated after the stamp.

    Uses the EXPLICIT install-DB path (the R1 keystone) so parent and child
    agree on the data source. Returns 0 on any error (fail-open).
    """
    try:
        store = get_session_store(chat_db_path=chat_db_path)
        sessions = store.list_active(persona_id=persona_id)
        if not since_iso:
            return len(sessions)
        count = 0
        for s in sessions:
            if s.updated_at:
                updated_str = (
                    s.updated_at.isoformat()
                    if isinstance(s.updated_at, datetime)
                    else str(s.updated_at)
                )
                if updated_str > since_iso:
                    count += 1
        return count
    except Exception:
        return 0


def _spawn_persona_pipeline(
    persona_name: str,
    profile_root: Path,
    *,
    test_mode: bool = False,
) -> tuple[bool, str]:
    """Spawn memory_reflect.py -p <persona> as a subprocess.

    Returns (success, message).
    """
    try:
        env = build_capability_scoped_env(persona_name, profile_root=profile_root)
    except Exception as exc:
        return False, f"env build failed: {exc}"

    cmd = [
        sys.executable,
        str(_SCRIPTS_DIR / "memory_reflect.py"),
        "-p", persona_name,
    ]
    if test_mode:
        cmd.append("--test")

    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(_SCRIPTS_DIR),
        )
        if result.returncode == 0:
            return True, "success"
        stderr_tail = (result.stderr or "")[-500:]
        return False, f"exit {result.returncode}: {stderr_tail}"
    except subprocess.TimeoutExpired:
        return False, "timeout (300s)"
    except Exception as exc:
        return False, f"spawn error: {exc}"


def run_tick(*, test_mode: bool = False, once: bool = False) -> None:
    """Main tick: enumerate learning-enabled personas, spawn pipelines."""
    settings = get_persona_learning_settings()
    if not settings.enabled:
        print(f"[{_now_iso()}] [persona-learning] disabled via PERSONA_LEARNING_ENABLED")
        return

    if not is_active_default_profile():
        print(f"[{_now_iso()}] PERSONA_LEARNING_TICK: must run under default profile, skipping")
        return

    install_db = get_default_paths()["data"] / "chat.db"

    profiles = list_profiles()
    named_profiles = [p for p in profiles if not p.is_default]

    if not named_profiles:
        print(f"[{_now_iso()}] PERSONA_LEARNING_TICK: no named profiles found, exiting")
        return

    bg_models = get_background_models()
    os.environ["SECOND_BRAIN_BACKGROUND_QUALITY_MODEL"] = bg_models["quality"]

    eligible: list[tuple[str, Path]] = []
    for profile in named_profiles:
        try:
            cfg = load_persona_config(profile.name)
        except Exception as exc:
            print(f"[{_now_iso()}] PERSONA_LEARNING_TICK [{profile.name}]: config error ({exc}), skip")
            continue

        learning = cfg.get("learning", {})
        if not isinstance(learning, dict):
            continue
        if not learning.get("enabled", False):
            continue

        eligible.append((profile.name, profile.path))

    if not eligible:
        print(f"[{_now_iso()}] PERSONA_LEARNING_TICK: no learning-enabled personas, exiting")
        return

    print(f"[{_now_iso()}] PERSONA_LEARNING_TICK: {len(eligible)} learning-enabled persona(s)")

    for persona_name, profile_root in eligible:
        state_file = _persona_state_file(persona_name)
        state = load_state(state_file)
        last_run = state.get("last_run")

        # Recency guard (PERSONA_LEARNING_TICK_INTERVAL): skip a persona that
        # ran more recently than the interval. Fail-open — an absent or
        # unparseable stamp never blocks a run.
        if last_run and settings.tick_interval_hours > 0:
            try:
                last_dt = datetime.fromisoformat(last_run)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                hours_since = (
                    datetime.now(timezone.utc) - last_dt
                ).total_seconds() / 3600.0
                if hours_since < settings.tick_interval_hours:
                    print(
                        f"[{_now_iso()}] PERSONA_LEARNING_TICK [{persona_name}]: "
                        f"recency guard ({hours_since:.1f}h < "
                        f"{settings.tick_interval_hours}h), skipping"
                    )
                    if once:
                        break
                    continue
            except Exception:
                pass  # fail-open: a bad stamp never blocks the run

        row_count = _count_attributed_rows_since(persona_name, last_run, install_db)
        if row_count == 0:
            print(f"[{_now_iso()}] PERSONA_LEARNING_TICK [{persona_name}]: PERSONA_REFLECT_SILENT (0 new rows since {last_run or 'never'})")
            if once:
                break
            continue

        print(f"[{_now_iso()}] PERSONA_LEARNING_TICK [{persona_name}]: START ({row_count} attributed rows)")

        if test_mode:
            print(f"[{_now_iso()}] PERSONA_LEARNING_TICK [{persona_name}]: --test mode, skipping spawn")
            state["last_run"] = datetime.now(timezone.utc).isoformat()
            state["result"] = "test_skip"
            state["rows_found"] = row_count
            save_state(state, state_file)
            if once:
                break
            continue

        success, message = _spawn_persona_pipeline(
            persona_name, profile_root, test_mode=test_mode
        )

        state["last_run"] = datetime.now(timezone.utc).isoformat()
        state["result"] = "success" if success else "failed"
        state["rows_found"] = row_count
        state["message"] = message
        save_state(state, state_file)

        if success:
            print(f"[{_now_iso()}] PERSONA_LEARNING_TICK [{persona_name}]: SUCCESS")
        else:
            print(f"[{_now_iso()}] PERSONA_LEARNING_TICK [{persona_name}]: FAILED — {message}")

        if once:
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Persona Learning Tick")
    parser.add_argument("--test", action="store_true", help="Dry run")
    parser.add_argument("--once", action="store_true", help="Process first eligible persona only")
    args = parser.parse_args()
    run_tick(test_mode=args.test, once=args.once)
