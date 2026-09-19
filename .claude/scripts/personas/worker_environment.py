"""Bind detached domain workers to their persona and explicit shared ledgers.

This boot helper must run before importing config or domain services. It does
not inspect or move any database. Explicit deployment/domain pins always win.
"""

from __future__ import annotations

import os
from pathlib import Path

from personas import core


def bootstrap_scoped_worker(
    persona_id: str,
    *,
    profile_env: Path | None = None,
    runtime_home: Path | None = None,
    data_pins: dict[str, str] | None = None,
) -> Path:
    """Load scoped credentials while retaining the former shared desk paths.

    ``runtime_home`` is the legacy .claude directory argument. It selects shared
    desk files only; HOMIE_HOME is always the selected persona's actual home.
    """
    from dotenv import load_dotenv

    inherited_home = os.getenv("HOMIE_HOME", "").strip()
    inherited_profile = Path(inherited_home).expanduser() if inherited_home else None
    if profile_env is not None:
        selected = Path(profile_env)
    elif (
        inherited_profile is not None
        and inherited_profile.parent.name == "profiles"
        and inherited_profile.name == persona_id
        and os.getenv("HOMIE_NAME") == persona_id
    ):
        selected = inherited_profile / ".env"
    else:
        selected = Path.home() / ".homie" / "profiles" / persona_id / ".env"
    from .deployment import bootstrap_deployment_pins, read_deployment_pins

    profile_pins = read_deployment_pins(selected)
    if profile_pins.get("ORCHESTRATION_DB_PATH", "").strip():
        os.environ["ORCHESTRATION_DB_PATH"] = profile_pins["ORCHESTRATION_DB_PATH"]
    bootstrap_deployment_pins(Path(__file__).resolve().parent.parent)
    if selected.is_file():
        load_dotenv(selected, override=False)
    shared = (
        Path(runtime_home).expanduser().resolve(strict=False) / "data"
        if runtime_home is not None
        else core.get_default_paths()["data"]
    )
    os.environ.setdefault("ORCHESTRATION_DB_PATH", str(shared / "orchestration.db"))
    for env_key, relative_path in (data_pins or {}).items():
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("worker data pins must be relative to the shared data root")
        os.environ.setdefault(env_key, str(shared / relative))
    os.environ["HOMIE_HOME"] = str(selected.parent.resolve(strict=False))
    os.environ["HOMIE_NAME"] = persona_id
    return selected
