"""Early installation pins from a trusted, explicitly located local dotenv.

Only four filesystem controls are read. Provider credentials and persona
selection never flow through this bootstrap. It is stdlib-only so shell/CLI
boot helpers work before the full configuration graph is imported.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

DEPLOYMENT_PIN_KEYS = frozenset(
    {
        "HOMIE_DEFAULT_PROFILE_ROOT",
        "HOMIE_VAULT_DIR",
        "ORCHESTRATION_DB_PATH",
        "SECOND_BRAIN_RUNTIME_ACTIVITY_DB",
    }
)


def read_deployment_pins(env_file: Path, *, ignore_keys=()) -> dict[str, str]:
    """Read literal single-line paths only; never search cwd or evaluate code.

    Both quoted paths and ordinary unquoted dotenv paths/comments are accepted.
    Interpolation is deliberately refused for these early pins: the file has
    not been loaded yet, so resolving another dotenv variable would be ambiguous.
    """
    try:
        lines = Path(env_file).read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return {}
    result = {}
    for line in lines:
        key, separator, raw = line.strip().removeprefix("export ").partition("=")
        key = key.strip()
        if not separator or key not in DEPLOYMENT_PIN_KEYS or key in ignore_keys:
            continue
        value = raw.strip()
        if value.startswith(('"', "'")):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or (
                value[end + 1 :].strip() and not value[end + 1 :].strip().startswith("#")
            ):
                raise ValueError(f"{key} must contain one literal deployment path")
            value = value[1:end]
        else:
            value = (
                "" if value.startswith("#") else re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
            )
        if "\x00" in value or "${" in value or "$(" in value:
            raise ValueError(f"{key} must contain one literal deployment path")
        result[key] = value
    return result


def bootstrap_deployment_pins(
    scripts_dir: Path,
    *,
    profile_env_file: Path | None = None,
    include_orchestration: bool = True,
) -> dict[str, str]:
    """Bind local installation pins before resolving config. Process env wins."""
    pins = read_deployment_pins(Path(scripts_dir) / ".env", ignore_keys=os.environ)
    if profile_env_file is not None:
        profile = read_deployment_pins(profile_env_file, ignore_keys=os.environ)
        if profile.get("ORCHESTRATION_DB_PATH", "").strip():
            pins["ORCHESTRATION_DB_PATH"] = profile["ORCHESTRATION_DB_PATH"]
    if not include_orchestration:
        pins.pop("ORCHESTRATION_DB_PATH", None)
    for key, value in pins.items():
        os.environ.setdefault(key, value)
    return {key: os.environ[key] for key in DEPLOYMENT_PIN_KEYS if key in os.environ}
