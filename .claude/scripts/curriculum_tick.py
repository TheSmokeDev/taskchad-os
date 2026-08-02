"""Persistent six-hour curriculum scheduler with per-persona subprocesses."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from personas import apply_persona_override

apply_persona_override()

from curriculum.config import get_curriculum_settings  # noqa: E402
from personas.capabilities import build_capability_scoped_env  # noqa: E402
from personas.lifecycle import list_profiles  # noqa: E402
from personas.services import is_active_default_profile  # noqa: E402
from security import kill_switches  # noqa: E402
from shared import load_state, save_state  # noqa: E402

_SCRIPTS_DIR = Path(__file__).resolve().parent


def run_parent(*, test_mode: bool = False, once: bool = False) -> int:
    if not is_active_default_profile():
        print("CURRICULUM_TICK: parent must run under the default profile")
        return 2
    if kill_switches.is_disabled("persona_curriculum"):
        print("CURRICULUM_TICK: persona_curriculum kill switch disabled")
        return 0
    failures = 0
    eligible = 0
    for profile in list_profiles():
        if profile.is_default:
            continue
        try:
            settings = get_curriculum_settings(profile.name)
        except Exception as exc:
            print(f"CURRICULUM_TICK [{profile.name}]: config error: {exc}")
            failures += 1
            continue
        if not settings.enabled:
            continue
        eligible += 1
        state_path = profile.path / "state" / "curriculum-tick.json"
        state = load_state(state_path)
        if not _due(state.get("last_success"), settings.schedule_hours):
            print(f"CURRICULUM_TICK [{profile.name}]: recency guard")
            if once:
                break
            continue
        if test_mode:
            print(f"CURRICULUM_TICK [{profile.name}]: would run")
            if once:
                break
            continue
        state["last_attempt"] = _now()
        save_state(state, state_path)
        success, detail = _spawn(profile.name, profile.path)
        state = load_state(state_path)
        state["last_result"] = detail
        if success:
            state["last_success"] = _now()
        save_state(state, state_path)
        print(
            f"CURRICULUM_TICK [{profile.name}]: "
            f"{'success' if success else 'failed'} — {detail}"
        )
        if not success:
            failures += 1
        if once:
            break
    if not eligible:
        print("CURRICULUM_TICK: no enabled persona curricula")
    return 1 if failures else 0


def _spawn(persona_id: str, profile_root: Path) -> tuple[bool, str]:
    try:
        env = build_capability_scoped_env(
            persona_id, profile_root=profile_root
        )
        result = subprocess.run(
            [
                sys.executable,
                str(_SCRIPTS_DIR / "curriculum_tick.py"),
                "--persona",
                persona_id,
            ],
            cwd=str(_SCRIPTS_DIR),
            env=env,
            text=True,
            capture_output=True,
            timeout=3600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "timeout after 3600s"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    detail = (result.stdout or result.stderr or "").strip()[-2000:]
    return result.returncode == 0, detail or f"exit={result.returncode}"


def run_child(persona_id: str) -> int:
    from curriculum.service import get_curriculum_service

    payload = asyncio.run(get_curriculum_service(persona_id).run_once())
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload.get("success") else 1


def _due(last_success: str | None, interval_hours: int) -> bool:
    if not last_success:
        return True
    try:
        parsed = datetime.fromisoformat(last_success)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        elapsed = datetime.now(UTC) - parsed.astimezone(UTC)
        return elapsed.total_seconds() >= interval_hours * 3600
    except (TypeError, ValueError):
        return True


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.persona:
        return run_child(args.persona)
    return run_parent(test_mode=args.test, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
