"""Phase 3 service-resolver helpers (PRP-7c — services-core workstream).

This module owns the profile-aware resolution of bot lifecycle paths
(pid file, lock file, Windows mutex name, log directory) and runtime
service ports (orchestration API, health check, WhatsApp webhook).
It also exposes the Telegram-token collision detector that gates bot
startup when two profiles accidentally share the same token.

Phase 1's frozen ``personas.__all__`` (12 helpers) is preserved — this
submodule adds NO new public exports there. Consumers import directly:

    from personas.services import get_bot_pid_path, allocate_port, ...

Anti-pattern enforcement (MEMORY.md "Code Review Patterns"):

* **Rule 1 — None sentinel for tunable parameters.** Every helper that
  takes a tunable input uses ``param: T | None = None`` and resolves
  inside the body. NO ``def fn(arg=BOT_PID_FILE)`` / ``arg=config.X``
  shapes anywhere in this module.
* **Rule 2 — Physical state, not meta.** ``_port_is_free`` calls
  ``socket.bind`` directly; ``is_active_default_profile`` routes through
  ``_activity.get_active_profile_name()`` which respects ``HOMIE_HOME``;
  ``_read_persisted_port`` reads the config.yaml file every call so
  callers see real on-disk state.
* **Rule 3 — Module-attribute lookup for monkeypatch propagation.**
  ``from . import activity as _activity`` at module top, then
  ``_activity.get_active_profile_name()`` at every call site. Tests that
  patch ``personas.activity.get_active_profile_name`` propagate.

PRP anchors: §"Implementation Blueprint" / §"Per-task pseudocode" / §1971-1986.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import re
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml  # M1 lock 2026-05-04 — replaces hand-rolled mini-parser

# Rule 3 / B2 fix: import the activity module so monkeypatching propagates.
# Top-level ``from .activity import get_active_profile_name`` would cache the
# function object — tests patching ``personas.activity.get_active_profile_name``
# would patch a name we no longer reference. Same enforcement Rule 3 already
# requires for ``runtime.langfuse_setup``.
from . import activity as _activity
from .core import (
    get_default_homie_root,
    get_default_paths,
    get_homie_home,
    get_persona_paths,
)

_logger = logging.getLogger(__name__)

ServiceName = Literal["orchestration_api", "health_check", "whatsapp_webhook"]


class ConfigShapeError(ValueError):
    """Raised when ``<profile>/config.yaml`` has an invalid shape.

    Inherits from ``ValueError`` for back-compat with existing
    ``except ValueError`` callers across the codebase (PRD-8 Phase 2 R3 NB1).

    Carries an optional field path in the message so the operator sees
    exactly which leaf is wrong (e.g. ``"cabinet.voice_id"`` vs.
    ``"invalid config"``).
    """


# Voice cascade providers known to the framework. Phase 2 ships the schema
# only; Phase 4 wires the actual provider clients. Keeping this list here
# (rather than in a separate constants module) preserves the rule that the
# personas slice is structural plumbing — operators authoring config.yaml
# get a clear "unknown provider" error at load time, not a vague KeyError
# at runtime.
_KNOWN_VOICE_PROVIDERS: frozenset[str] = frozenset(
    {
        "edge",
        "elevenlabs",
        "groq",
        "gradium",
        "openai",
        "google",
        "azure",
    }
)


# Legacy port defaults (load-bearing for Mission Control compat).
# Default profile preserves these forever; named profiles use them as
# the base for the deterministic offset hash.
_LEGACY_PORTS: dict[str, int] = {
    "orchestration_api": 4322,
    "health_check": 8787,
    "whatsapp_webhook": 8443,
}

# Env var names corresponding to each service.
_PORT_ENV_VARS: dict[str, str] = {
    "orchestration_api": "ORCHESTRATION_API_PORT",
    "health_check": "HEALTH_CHECK_PORT",
    "whatsapp_webhook": "WHATSAPP_WEBHOOK_PORT",
}

# Legacy mutex name — preserved for default profile back-compat FOREVER.
# Renaming would let two default-profile bots start simultaneously while a
# v1 mutex is held by the first. Acceptance test
# ``test_default_profile_preserves_legacy_mutex_name`` verifies this.
_LEGACY_MUTEX_NAME = "Global\\SecondBrainTelegramBot"


# ── PUBLIC HELPERS ──────────────────────────────────────────────────────


def is_active_default_profile() -> bool:
    """Return True iff the ACTIVE PROFILE is the default profile.

    R1 B2 / R2 NM2: NOT to be confused with ``personas.activity.is_default_profile()``,
    which only checks whether ``<install>/vault/memory/SOUL.md`` exists
    on disk (a physical-vault-presence test, NOT an active-selection test).

    On owner's install where SOUL.md exists AND
    ``HOMIE_HOME=~/.homie/profiles/sales`` is set, raw ``is_default_profile()``
    returns True (the install vault exists) — but the active profile is
    ``sales``, not ``default``. Using ``is_default_profile()`` to gate the
    legacy mutex / compat-shadow incorrectly grants those to the named
    profile and corrupts the default's PID file.

    This helper routes through ``activity.get_active_profile_name()``
    (which respects ``HOMIE_HOME`` at rank 2 per
    ``personas/activity.py:138-145``) and returns True ONLY when the active
    profile name resolves to ``"default"``.
    """
    return _activity.get_active_profile_name() == "default"


def get_bot_pid_path() -> Path:
    """Return the canonical bot pid path for the active profile.

    R3 NB1 fix: Default profile returns ``<install>/.claude/data/state/bot.pid`` —
    the AUTHORITATIVE ``shared.py:329`` ``BOT_PID_FILE = STATE_DIR / "bot.pid"``
    path per PRD §8.2 line 923 / §8.5 line 994 ("the authoritative one per
    ``shared.py:329``"). Launcher scripts (``run_chat.sh``, ``bot-status.sh``;
    ``run_chat.bat`` retired 2026-07) consolidate onto this path after the
    §8.5 refactor.

    The historical ``<install>/.claude/chat/bot.pid`` location is preserved
    as a WRITE-ONLY compatibility shadow at ``_compat_shadow_pid_path()`` for
    default profile only — see ``_should_write_compat_shadow()`` and
    ``shared.py:write_pid()``. The shadow is best-effort, fail-open, never a
    read source.

    Named profiles get ``$HOMIE_HOME/run/bot.pid`` (Phase 1 layout).

    Anti-pattern Rule 1: resolves on every call; no def-time bind.
    Anti-pattern Rule 2: read from physical paths via persona resolver,
    no sidecar registry.
    """
    if is_active_default_profile():
        # PRD §8.2 / §8.5 — authoritative path is shared.py:329 STATE_DIR / bot.pid.
        return get_default_paths()["state"] / "bot.pid"
    active = _activity.get_active_profile_name()
    return get_persona_paths(active)["run"] / "bot.pid"


def get_bot_lock_path() -> Path:
    """Return the canonical bot lock path for the active profile.

    R3 NNB6 + R2 NB3: Default profile keeps the legacy
    ``<install>/.claude/chat/bot.lock`` location per ``chat/main.py:165``
    (a fcntl LOCK_EX file used as the secondary instance lock).
    Named profiles get ``$HOMIE_HOME/run/bot.lock``.
    """
    if is_active_default_profile():
        # personas/services.py -> personas/ -> scripts/ -> .claude/ -> repo/
        repo_root = Path(__file__).resolve().parent.parent.parent.parent
        return repo_root / ".claude" / "chat" / "bot.lock"
    active = _activity.get_active_profile_name()
    return get_persona_paths(active)["run"] / "bot.lock"


def get_bot_mutex_name() -> str:
    """Return the Windows named mutex name for the active profile.

    R3 NNB3: profile-scoped to prevent multi-profile collision on Windows.
    R1 B2 fix: gate is ``is_active_default_profile()`` (active selection),
    NOT raw ``is_default_profile()`` (vault existence).

    Default profile preserves the literal legacy ``Global\\SecondBrainTelegramBot``
    name FOREVER — changing it would let a second default-profile bot start
    while the v1 mutex is held by the first instance.

    Named profiles use ``Global\\Homie-<sha256_16char_hex>`` where the hash
    is computed from the profile name (or HOMIE_HOME for ``custom``). 16
    hex chars = 64 bits of entropy; collision unlikely until ~4 billion
    profiles (acceptable).
    """
    if is_active_default_profile():
        return _LEGACY_MUTEX_NAME
    name = _activity.get_active_profile_name()
    if name == "custom":
        # Custom HOMIE_HOME fallback — use a stable hash of the path so two
        # different custom dirs get different mutex names.
        hash_input = str(get_homie_home()).encode("utf-8")
    else:
        hash_input = name.encode("utf-8")
    digest = hashlib.sha256(hash_input).hexdigest()[:16]
    return f"Global\\Homie-{digest}"


def get_log_dir() -> Path:
    """Return the canonical log directory for the active profile.

    Default profile: ``<install>/.claude/data`` (matches ``get_default_paths()["logs"]``).
    Named profile:   ``<profile_root>/logs``.
    """
    active = _activity.get_active_profile_name()
    return get_persona_paths(active)["logs"]


def allocate_port(
    service: str,
    *,
    profile_name: str | None = None,
) -> int:
    """Resolve a port number for *service* in the active or specified profile.

    Resolution order (R1 B3 + R2 NM3 — env override is rank 2a per
    PRD §7.8.1, BEFORE the legacy default fallback, AND boot-order
    independent because the helper reads the profile .env directly):

        1. Validate *service* is in ``_LEGACY_PORTS``.
        2a. Profile .env override via ``dotenv_values()`` — applies to ALL
            profiles including default. Read from disk every call so the
            helper is order-independent (R2 NM3).
        2b. ``os.environ[env_var]`` override — same precedence, kicks in
            when the operator sets the env var in the parent shell rather
            than the profile .env.
        2c. (default profile only) → return legacy hardcoded port
            (4322 / 8787 / 8443) so Mission Control's hardcoded reads
            keep working when no override is set.
        3. Persisted assignment in ``<profile_config>/config.yaml``.
        4. Deterministic offset from SHA256(profile_name) + linear probe
           if collision; persist the result.

    Anti-pattern Rule 1: ``profile_name=None`` → resolved inside body via
    ``_activity.get_active_profile_name()``. NEVER bind at def-time.

    Anti-pattern Rule 2: "is port free" check uses real ``socket.bind``
    (physical state). NEVER consults a sidecar "is_allocated" flag.

    R1 M2 fix: when ``profile_name`` is explicit, the persisted-assignment
    config path is resolved via ``_resolve_profile_config_path(profile_name)``,
    NOT ``get_homie_home()`` (which would write to the active profile's
    config.yaml when callers asked for a different profile's port).

    R2 NM3 fix: env override is read DIRECTLY from the profile's .env
    file via ``dotenv_values()`` BEFORE consulting ``os.environ``. This
    makes the helper self-contained — ``from orchestration.api import API_PORT``
    no longer depends on ``config`` having been imported under the active
    profile first.

    Raises:
        ValueError: if *service* is not a known service name.
        RuntimeError: if no free port can be found within probe range.
    """
    if service not in _LEGACY_PORTS:
        raise ValueError(f"Unknown service '{service}'. Known: {list(_LEGACY_PORTS.keys())}")
    if profile_name is None:
        profile_name = _activity.get_active_profile_name()
    env_var = _PORT_ENV_VARS[service]
    # Step 2a: profile .env override (R2 NM3 — read from disk every call,
    # so the helper is boot-order independent).
    env_val = _read_port_from_profile_env(profile_name, env_var)
    # Step 2b: os.environ override (kicks in when operator sets a shell env
    # var rather than putting it in profile .env).
    if not env_val:
        env_val = os.environ.get(env_var, "").strip()
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            # Corrupt env override; fall through silently to defaults.
            pass
    # Step 2c: default profile preserves legacy port (after env-override check).
    if profile_name == "default":
        return _LEGACY_PORTS[service]
    # Step 3: persisted assignment (M2 — write to the SPECIFIED profile's config).
    config_path = _resolve_profile_config_path(profile_name)
    persisted = _read_persisted_port(config_path, service)
    if persisted is not None:
        return persisted
    # Step 4: deterministic offset + linear probe.
    base = _LEGACY_PORTS[service]
    digest = hashlib.sha256(profile_name.encode("utf-8")).digest()
    offset = int.from_bytes(digest[:2], "big") % 1000  # 0..999
    candidate = base + offset
    while not _port_is_free(candidate):
        candidate += 1
        if candidate >= 65535:
            raise RuntimeError(f"No free port found near base={base} for service '{service}'")
    _write_persisted_port(config_path, service, candidate)
    return candidate


def get_orchestration_api_port() -> int:
    """Return the orchestration API port for the active profile."""
    return allocate_port("orchestration_api")


def get_health_check_port() -> int:
    """Return the health check port for the active profile."""
    return allocate_port("health_check")


def get_whatsapp_webhook_port() -> int:
    """Return the WhatsApp webhook port for the active profile."""
    return allocate_port("whatsapp_webhook")


def detect_telegram_token_collision(
    active_token: str | None = None,
) -> str | None:
    """Return the name of another profile sharing *active_token*, or None.

    R1 B2 carry-over: owner's most-common multi-profile mistake is cloning
    a profile WITH ``--clone-secrets`` and ending up with duplicate Telegram
    tokens. Telegram allows ONE polling process per token; the second bot
    startup gets HTTP 409 Conflict. This helper detects the collision at
    bot startup so the operator gets a clear error before Telegram bounces.

    R1 B4 fix: scan set is ``{default profile env_file via
    get_default_paths()["env_file"]} ∪ {profile env_file for profile in
    profiles_root}`` minus the active profile's env file. The default
    profile's env file is the install-dir ``.claude/scripts/.env`` and is
    NOT under ``~/.homie/profiles/``, so the pre-revision implementation
    silently skipped it.

    Reads .env files DIRECTLY from disk via ``dotenv_values`` (Rule 2 — no
    sidecar registry). Comparison is exact-string-match after strip.

    Anti-pattern Rule 1: ``active_token=None`` → resolved inside body via
    ``os.environ``. NEVER bind at def-time.

    Returns None when:
        * ``active_token`` is empty / None
        * no other profile env files exist
        * no other profile shares the token
        * any .env parse failure (FAIL-OPEN — bot startup proceeds rather
          than refusing on a corrupt other-profile .env file)
    """
    if active_token is None:
        active_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not active_token:
        return None
    active = _activity.get_active_profile_name()
    # R1 B4: build the candidate set explicitly. Include the default
    # profile's env file (NOT under ~/.homie/profiles/) plus every named
    # profile's env file. Exclude the active profile's own env file.
    candidates: list[tuple[str, Path]] = []
    if active != "default":
        # Default profile's env file is a candidate UNLESS we're it.
        try:
            default_env = get_default_paths()["env_file"]
        except Exception:
            default_env = None
        if default_env is not None and default_env.is_file():
            candidates.append(("default", default_env))
    profiles_root = get_default_homie_root() / "profiles"
    if profiles_root.is_dir():
        try:
            entries = sorted(profiles_root.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_dir():
                continue
            if entry.name == active:
                continue
            env_path = entry / ".env"
            if env_path.is_file():
                candidates.append((entry.name, env_path))
    for profile_name, env_path in candidates:
        other_token = _parse_env_token(env_path, "TELEGRAM_BOT_TOKEN")
        if other_token and other_token == active_token:
            return profile_name
    return None


def load_persona_config(
    persona_id: str | None = None,
    *,
    profile_root: Path | None = None,
) -> dict[str, Any]:
    """Read ``<profile>/config.yaml`` strictly for *persona_id* (or active profile).

    PRD-8 Phase 2 — public reader for the operator-extended config.yaml.
    Returns a dict with optional keys: ``ports``, ``persona``, ``model``,
    ``mcp``, ``cabinet``, ``voice``. Missing sections are absent from the
    dict (NOT ``None``, NOT empty dict).

    Path resolution reuses ``_resolve_profile_config_path()``:
      * default profile: ``paths['state'] / 'config.yaml'``
      * named profiles:  ``paths['state'].parent / 'config.yaml'``

    Anti-pattern Rule 1: ``persona_id=None`` resolves to the active profile
    via ``_activity.get_active_profile_name()`` at call time. NEVER bind
    ``persona_id`` at def time.

    Anti-pattern Rule 2: file content is read from disk on every call. No
    module-level cache.

    Anti-pattern Rule 3: ``_activity`` is referenced through the imported
    module attribute (services.py:43-48 pattern), so test monkey-patches of
    ``personas.activity.get_active_profile_name`` propagate.

    STRICT READ (R2 NB1): does NOT delegate to ``_read_yaml_safe()``. Calls
    ``config_path.read_text()`` + ``yaml.safe_load()`` directly inside a
    try/except, re-raises ``yaml.YAMLError`` as
    ``ConfigShapeError(f"yaml: {path}: {exc}")``. Operator typos like
    ``voice: [`` MUST surface — silently returning ``{}`` would mask a
    setup error and Phase 3 would treat it as an intentionally empty
    config.

    R3 NM1 — empty-dict back-compat applies ONLY when ``persona_id is None
    AND actual_id == "default"``. If ``HOMIE_HOME`` points at a named
    profile (e.g. ``~/.homie/profiles/sales``), ``_activity.get_active_profile_name()``
    returns ``"sales"`` per ``activity.py:129-175``. A missing
    ``config.yaml`` for an active named profile MUST raise
    ``FileNotFoundError`` — silently returning ``{}`` would mask a setup
    error.

    Raises:
        FileNotFoundError: if config.yaml file does not exist (with
            absolute path), EXCEPT when persona_id is None AND the
            resolved profile is "default" (default-profile bootstrap).
        ConfigShapeError: on YAML parse failure (message starts with
            ``"yaml:"`` and includes the file path) or schema mismatch
            (with field path).
    """
    # Rule 1 — None sentinel resolved at call time (not bound at def time).
    # Rule 3 — module-attribute lookup so monkeypatch propagates.
    actual_id = persona_id if persona_id is not None else _activity.get_active_profile_name()
    if profile_root is None:
        config_path = _resolve_profile_config_path(actual_id)
    else:
        # Scheduled workers deliberately root operational databases at the
        # repository while identity/config stay under ~/.homie/profiles.  Do
        # not let HOMIE_HOME silently redirect an explicitly selected persona.
        explicit_root = Path(profile_root).expanduser().resolve(strict=False)
        config_path = explicit_root / "config.yaml"

    if not config_path.is_file():
        # R3 NM1 — only the default profile permits empty-dict back-compat
        # (default-profile bootstrap). Active named profile + missing
        # config.yaml is a setup error.
        if persona_id is None and actual_id == "default":
            return {}
        raise FileNotFoundError(f"config.yaml not found for persona {actual_id!r}: {config_path}")

    # Rule 2 — read on every call. STRICT semantics: do NOT delegate to
    # _read_yaml_safe (which fail-opens to {}); operator typos must surface.
    try:
        text = config_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigShapeError(f"yaml: {config_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigShapeError(f"read: {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigShapeError(
            f"shape: {config_path}: top-level must be mapping, got {type(raw).__name__}"
        )

    # Validate each section (only when present). Missing sections are
    # ABSENT from the dict per criterion config_yaml_persona_section_validates.
    if "ports" in raw:
        _validate_ports_section(raw["ports"], config_path)
    if "persona" in raw:
        _validate_persona_section(raw["persona"], config_path)
    if "model" in raw:
        _validate_model_section(raw["model"], config_path)
    if "mcp" in raw:
        _validate_mcp_section(raw["mcp"], config_path)
    if "toolsets" in raw:
        _validate_toolsets_section(raw["toolsets"], config_path)
    if "tools" in raw:
        _validate_tools_section(raw["tools"], config_path)
    if "capability_blueprint" in raw:
        _validate_capability_blueprint_section(
            raw["capability_blueprint"], config_path
        )
    if "cabinet" in raw:
        _validate_cabinet_section(raw["cabinet"], config_path)
    if "voice" in raw:
        _validate_voice_section(raw["voice"], config_path)
    if "learning" in raw:
        _validate_learning_section(raw["learning"], config_path)
    if "curriculum" in raw:
        _validate_curriculum_section(raw["curriculum"], config_path)
    if "delegation" in raw:
        _validate_delegation_section(raw["delegation"], config_path)
    if "market_round" in raw:
        _validate_market_round_section(raw["market_round"], config_path)

    return raw


# ── PRIVATE HELPERS ─────────────────────────────────────────────────────


def _should_write_compat_shadow() -> bool:
    """Rule 3 toggle: feature flag through a helper, not inline boolean.

    R1 B2 fix: gate is ``is_active_default_profile()`` (active selection),
    NOT raw ``is_default_profile()`` (which only checks SOUL.md existence
    and silently mis-classifies named profiles on owner's install).

    R3 NB1 fix: returns ``is_active_default_profile()``, not unconditional
    False. Pass 2 incorrectly returned False after merging canonical +
    shadow into one path. Once R3 NB1 split them back per PRD §8.2/§8.5:

      * default profile's CANONICAL pid = ``<install>/.claude/data/state/bot.pid``
      * default profile's SHADOW pid    = ``<install>/.claude/chat/bot.pid``

    the shadow becomes a real best-effort write again. This helper gates
    that write so default profile writes BOTH paths; named profiles never
    write the shadow (would corrupt default's compat file).

    Tests monkeypatch THIS function — single Rule 3 gate point, no inline
    ``if is_active_default_profile():`` checks scattered through chat/main.py
    and shared.py.
    """
    return is_active_default_profile()


def _compat_shadow_pid_path() -> Path:
    """Historical script-side duplicate: ``<install>/.claude/chat/bot.pid``.

    R3 NB1 fix: this is the WRITE-ONLY compat shadow — NEVER the canonical
    pid path. Default profile's canonical pid is the authoritative
    ``<install>/.claude/data/state/bot.pid`` per PRD §8.2/§8.5; this path
    is the historical chat-side duplicate that pre-Phase-3 ``chat/main.py:91-99``
    wrote for external monitor compatibility.

    ``shared.py:write_pid()`` writes this path best-effort (try/except,
    fail-open) AFTER the canonical write succeeds, gated by
    ``_should_write_compat_shadow()`` (which returns True only for default
    profile). Named profiles MUST NOT touch this path because doing so
    would corrupt the default's compat-shadow file.

    Read paths (``shared.py:read_pid()``, ``chat/main.py:_is_bot_process_alive()``,
    ``bot-status.sh``) MUST NEVER trust this file — always read the canonical
    ``get_bot_pid_path()`` result.

    Resolves on every call (Rule 1 — no def-time bind).
    """
    # personas/services.py -> personas/ -> scripts/ -> .claude/ -> repo/
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    return repo_root / ".claude" / "chat" / "bot.pid"


def _port_is_free(port: int) -> bool:
    """Rule 2: physical socket.bind probe, not a registry consult.

    Sets ``SO_REUSEADDR`` before bind to avoid Windows TIME_WAIT phantoms.
    Returns False on any OSError (port in use, permission denied, etc.).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _read_port_from_profile_env(profile_name: str, env_key: str) -> str:
    """R2 NM3 fix — read *env_key* directly from the SPECIFIED profile's .env.

    Why this exists: ``apply_persona_override()`` in ``personas/boot.py``
    only sets ``HOMIE_HOME`` — it does NOT load the profile's ``.env`` into
    ``os.environ``. The ``.env`` load happens as a side effect of importing
    ``config.py`` (line 47: ``load_dotenv(ENV_FILE, override=True)``).

    If ``config`` was imported under a different profile earlier in the
    process, ``os.environ[env_key]`` may be missing or stale even after a
    later ``apply_persona_override()`` swaps ``HOMIE_HOME``. To make
    ``get_orchestration_api_port()`` (and the other port helpers) boot-order
    independent, we read the active profile's env file DIRECTLY via
    ``dotenv_values()`` and let the caller consult ``os.environ`` as a backup.

    Returns "" on any error (fail-open — caller falls through to legacy
    fallback or deterministic-offset path).
    """
    try:
        env_path = get_persona_paths(profile_name)["env_file"]
    except Exception:
        return ""
    if not env_path.is_file():
        return ""
    try:
        from dotenv import dotenv_values

        values = dotenv_values(str(env_path))
    except Exception:
        return ""
    return (values.get(env_key, "") or "").strip()


def _parse_env_token(env_path: Path, key: str) -> str:
    """Parse *key* from *env_path* using ``dotenv_values``.

    Returns "" on any error (FAIL-OPEN for bot startup — caller treats
    "" as no collision detected, so bot startup proceeds. The semantics
    intentionally favor letting a bot start over refusing on a corrupt
    .env file; the operator gets a clear error elsewhere if Telegram
    actually 409s. R1 minor — pre-revision text said "fail closed" which
    was the wrong terminology).
    """
    try:
        from dotenv import dotenv_values

        values = dotenv_values(str(env_path))
    except Exception:
        return ""
    return (values.get(key, "") or "").strip()


def _resolve_profile_config_path(profile_name: str) -> Path:
    """R1 M2 — resolve config.yaml path for a SPECIFIC profile.

    When the caller passes ``profile_name="sales"`` while the active env is
    default, we MUST write to sales' config.yaml (under
    ``~/.homie/profiles/sales/``), NOT to ``~/.homie/config.yaml`` (which is
    ``get_homie_home()``'s default-root behavior).

    For ``"default"`` profile, the persisted assignment lives in the install
    dir's ``.claude/data/state/config.yaml`` (mirrors STATE_DIR ownership).
    For named/custom profiles, it lives at ``<profile_root>/config.yaml``.
    """
    paths = get_persona_paths(profile_name)
    if profile_name == "default":
        return paths["state"] / "config.yaml"
    # paths["state"] for named/custom profiles == <profile_root>/state; we
    # want the profile root itself, which equals paths["state"].parent.
    return paths["state"].parent / "config.yaml"


def get_profile_config_path(profile_name: str | None = None) -> Path:
    """Return the existing profile-owned ``config.yaml`` path."""
    actual = profile_name if profile_name is not None else _activity.get_active_profile_name()
    return _resolve_profile_config_path(actual)


def read_profile_config(profile_name: str | None = None, *, strict: bool = False) -> dict[str, Any]:
    """Read the profile-owned ``config.yaml`` using the canonical YAML reader."""
    path = get_profile_config_path(profile_name)
    if strict:
        return _read_yaml_strict(path)
    return _read_yaml_safe(path)


def set_persona_learning(persona_id: str, enabled: bool) -> None:
    """Toggle ``learning.enabled`` in a persona's ``config.yaml``.

    Uses ``_read_yaml_strict`` + ``_minimal_yaml_write`` (strict-read RMW)
    so a malformed config.yaml surfaces as ``ConfigShapeError`` instead of
    being silently wiped. Same pattern as ``_write_persisted_port``.

    Raises ``ConfigShapeError`` on parse failure or shape violation.
    Creates the ``config.yaml`` (with a single ``learning`` block) when the
    profile has none — a missing file is treated as an empty config, not an
    error.
    """
    config_path = _resolve_profile_config_path(persona_id)
    data = _read_yaml_strict(config_path)
    learning = data.get("learning", {})
    if not isinstance(learning, dict):
        raise ConfigShapeError(
            f"shape: {config_path}: learning must be mapping, got {type(learning).__name__}"
        )
    learning["enabled"] = enabled
    data["learning"] = learning
    _minimal_yaml_write(config_path, data)


def set_persona_curriculum(
    persona_id: str,
    curriculum: dict[str, Any],
) -> None:
    """Replace one persona's ``curriculum`` section using strict-read RMW.

    The caller supplies the complete section deliberately: source removals and
    budget changes must be visible in one operator-owned write. Unknown
    top-level profile keys are preserved. Validation runs before the write, so
    a malformed source URL or unsafe limit cannot partially update the file.
    """
    config_path = _resolve_profile_config_path(persona_id)
    _validate_curriculum_section(curriculum, config_path)
    data = _read_yaml_strict(config_path)
    data["curriculum"] = curriculum
    _minimal_yaml_write(config_path, data)


def set_persona_curriculum_enabled(persona_id: str, enabled: bool) -> None:
    """Toggle ``curriculum.enabled`` while preserving its source registry."""
    config_path = _resolve_profile_config_path(persona_id)
    data = _read_yaml_strict(config_path)
    curriculum = data.get("curriculum", {})
    if not isinstance(curriculum, dict):
        raise ConfigShapeError(
            f"shape: {config_path}: curriculum must be mapping, got {type(curriculum).__name__}"
        )
    curriculum["enabled"] = enabled
    _validate_curriculum_section(curriculum, config_path)
    data["curriculum"] = curriculum
    _minimal_yaml_write(config_path, data)


def _read_persisted_port(config_path: Path, service: str) -> int | None:
    """Read ``ports.<service>`` from ``$HOMIE_HOME/config.yaml``; None if absent."""
    if not config_path.is_file():
        return None
    data = _minimal_yaml_read(config_path)
    ports = data.get("ports", {})
    if not isinstance(ports, dict):
        return None
    val = ports.get(service)
    if isinstance(val, int):
        return val
    return None


def _write_persisted_port(config_path: Path, service: str, port: int) -> None:
    """Write ``ports.<service> = port`` atomically; preserve other top-level keys.

    R3 NB1 fix (PRD-8 Phase 2): reads via ``_read_yaml_strict()`` so a
    malformed ``config.yaml`` surfaces as ``ConfigShapeError`` instead of
    being silently overwritten by the legacy ``_minimal_yaml_read()``
    fail-open ``{}`` path. Pre-fix, the operator typo ``voice: [`` followed
    by any later ``allocate_port()`` call would have destroyed the
    ``persona``/``model``/``cabinet``/``voice`` sections of the file.

    R4 NM3 carry-over: also raises when the existing ``ports`` value is a
    non-mapping (e.g. ``ports: "4322"`` parses successfully into a string,
    not a dict). Pre-R4, that path silently replaced the string with a
    fresh ports dict on the next allocate_port call — same data-loss class.
    """
    # _read_yaml_strict raises ConfigShapeError on parse failure or
    # non-mapping top-level. {} is returned ONLY if the file does not exist.
    data = _read_yaml_strict(config_path)
    ports = data.get("ports", {})
    if not isinstance(ports, dict):
        # R4 NM3: malformed top-level ``ports`` value is a setup error,
        # not a silent overwrite trigger. Refuse to clobber.
        raise ConfigShapeError(
            f"shape: {config_path}: ports must be mapping, got {type(ports).__name__}"
        )
    ports[service] = int(port)
    data["ports"] = ports
    _minimal_yaml_write(config_path, data)


def _read_yaml_safe(path: Path) -> dict[str, Any]:
    """Fail-open YAML read. Returns ``{}`` on missing file OR parse error.

    SAFE FOR READ-ONLY CALLERS ONLY (PRD-8 Phase 2 R3 NB1). Do NOT call
    from any path that subsequently writes the dict back — silent ``{}``
    on parse error will DESTROY the file. Use ``_read_yaml_strict()`` before
    any write-back operation.

    M1 lock 2026-05-04 — body uses ``yaml.safe_load``. Supports lists,
    nested dicts, and all standard YAML shapes — required for new sections
    like ``mcp.servers`` (list), ``voice.cascade`` (list), ``cabinet.tools``
    (list).

    Legacy alias ``_minimal_yaml_read`` is preserved at the bottom of this
    module so legacy READ-ONLY callers (``_read_persisted_port``) keep
    working without edits. Write callers must migrate to the strict variant.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _read_yaml_strict(path: Path) -> dict[str, Any]:
    """Strict YAML read — raises ``ConfigShapeError`` on parse failure
    or non-mapping top-level.

    REQUIRED before any write-back operation (port persistence, future
    operator-edit features). Caller distinguishes "file genuinely empty /
    missing" (returns ``{}``) from "file unparseable" (raises).

    R3 NB1 — without this, a malformed ``config.yaml`` (e.g. operator typo
    ``voice: [``) gets silently overwritten with a ports-only dict on the
    next ``allocate_port()`` call, destroying ``persona``/``model``/
    ``cabinet``/``voice`` sections.
    """
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        result = yaml.safe_load(text) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigShapeError(f"yaml: {path}: {exc}") from exc
    if not isinstance(result, dict):
        raise ConfigShapeError(
            f"shape: {path}: top-level must be mapping, got {type(result).__name__}"
        )
    return result


# Back-compat alias — legacy READ-ONLY callers (e.g. ``_read_persisted_port``)
# continue to call ``_minimal_yaml_read``. The underlying body is now
# ``yaml.safe_load`` per M1 lock. Tests at
# ``tests/test_persona_port_allocation.py`` exercise the alias directly.
_minimal_yaml_read = _read_yaml_safe


def _minimal_yaml_write(path: Path, data: dict[str, Any]) -> None:
    """Atomic YAML write — pyyaml-backed (M1 lock 2026-05-04).

    ``default_flow_style=False`` keeps maps/lists in block style (multi-line)
    so operator-authored YAML stays human-readable. ``sort_keys=False``
    preserves insertion order so round-tripping doesn't reshuffle authored
    keys alphabetically.

    Both flags are required by PRP-PRD-8 Phase 2 / criterion
    ``config_yaml_uses_pyyaml``.
    """
    text = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
    _atomic_write_text(path, text)


def _atomic_write_text(path: Path, text: str) -> None:
    """Tempfile + ``os.replace`` pattern (Windows-safe).

    Mirrors ``personas.activity.set_active_profile``'s atomic-write shape.
    The tempfile is closed (via ``with os.fdopen(...)``) BEFORE
    ``os.replace`` runs so Windows accepts the rename — pass-3 R4 NM1 fix.

    On error, the tempfile is unlinked best-effort and the original
    exception is re-raised.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=path.stem + "-",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_str, str(path))
    except Exception:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
        raise


# ── SCHEMA VALIDATORS (PRD-8 Phase 2 / WS1) ─────────────────────────────
#
# Each validator takes an already-parsed sub-dict and the config_path (used
# only for error messages). Validators MUST NOT re-invoke YAML parsing —
# the strict reader at ``load_persona_config()`` already did that.
#
# Error messages always include the field path (e.g. ``"cabinet.voice_id"``)
# so the operator sees exactly which leaf is wrong. All validators raise
# ``ConfigShapeError`` (a ``ValueError`` subclass) — back-compat with
# existing ``except ValueError`` callers.


def _shape_error(config_path: Path, field: str, actual: Any, expected: str) -> ConfigShapeError:
    """Construct a uniform ConfigShapeError with field path + path context."""
    return ConfigShapeError(f"{field}: {actual!r} (expected {expected}) in {config_path}")


def _validate_ports_section(value: Any, config_path: Path) -> None:
    """Validate the ``ports`` section: mapping of str → int."""
    if not isinstance(value, dict):
        raise _shape_error(config_path, "ports", value, "mapping")
    for key, val in value.items():
        if not isinstance(val, int) or isinstance(val, bool):
            raise _shape_error(config_path, f"ports.{key}", val, "int")


def _validate_persona_section(value: Any, config_path: Path) -> None:
    """Validate the ``persona`` section: mapping with optional string fields.

    Recognised fields (all optional, all str when present):
      * ``id`` / ``name`` / ``display_name`` / ``role``
    Unknown fields are accepted (forward-compat with operator authoring).
    """
    if not isinstance(value, dict):
        raise _shape_error(config_path, "persona", value, "mapping")
    for field in ("id", "name", "display_name", "role"):
        if field in value and not isinstance(value[field], str):
            raise _shape_error(config_path, f"persona.{field}", value[field], "str")


def _validate_model_section(value: Any, config_path: Path) -> None:
    """Validate the ``model`` section: mapping with optional string fields.

    Recognised fields:
      * ``preferred`` (str) — preferred model id
      * ``fallback`` (list[str]) — fallback chain
    """
    if not isinstance(value, dict):
        raise _shape_error(config_path, "model", value, "mapping")
    if "preferred" in value and not isinstance(value["preferred"], str):
        raise _shape_error(config_path, "model.preferred", value["preferred"], "str")
    if "fallback" in value:
        fallback = value["fallback"]
        if not isinstance(fallback, list):
            raise _shape_error(config_path, "model.fallback", fallback, "list")
        for idx, item in enumerate(fallback):
            if not isinstance(item, str):
                raise _shape_error(config_path, f"model.fallback[{idx}]", item, "str")


def _validate_mcp_section(value: Any, config_path: Path) -> None:
    """Validate the ``mcp`` section: mapping with optional list/mapping fields.

    Recognised fields:
      * ``servers`` (list[str] OR list[mapping]) — MCP server identifiers
        or full server config objects
    """
    if not isinstance(value, dict):
        raise _shape_error(config_path, "mcp", value, "mapping")
    if "servers" in value:
        servers = value["servers"]
        if not isinstance(servers, list):
            raise _shape_error(config_path, "mcp.servers", servers, "list")
        for idx, item in enumerate(servers):
            if not isinstance(item, (str, dict)):
                raise _shape_error(
                    config_path,
                    f"mcp.servers[{idx}]",
                    item,
                    "str or mapping",
                )


_CABINET_VOICE_PROVIDER_ENUM: frozenset[str] = frozenset(
    {
        "elevenlabs",
        "edge",
        "openai",
        "gemini",
        "mistral",
        "gradium",
        "kokoro",
        "kittentts",
        "macos_say",
    }
)


@dataclass(frozen=True)
class PersonaToolScope:
    """What one persona is allowed to reach, resolved from its config.

    Attributes:
        toolsets: Declared toolset names. Resolved against the live registry at
            assembly time — this object carries intent, never a tool list.
        tools: Individual tool grants (the escape hatch).
        used_deprecated_alias: True when the values came from ``cabinet.tools``.
            Surfaced rather than hidden so an operator can see WHY a persona
            has the scope it has, and so the migration has something to report.
    """

    toolsets: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    used_deprecated_alias: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.toolsets and not self.tools


def resolve_persona_tool_scope(config: dict[str, Any]) -> PersonaToolScope:
    """Resolve a persona's declared tool scope from its parsed config.

    Precedence — the NEW keys win outright:

    1. Top-level ``toolsets:`` / ``tools:`` if either is present.
    2. Otherwise ``cabinet.tools`` (deprecated alias), read as individual
       grants because that key held TOOL names, never toolset names.
    3. Otherwise empty — and empty means NO tools. Default-deny survives the
       rename; an absent key has never granted anything and must not start now.

    The new keys win as a PAIR rather than merging with the alias. Merging
    would make a profile mid-migration carry a scope that appears in neither
    key alone, so the effective grant would be invisible in the file the
    operator is reading.

    Surveyed 2026-07-27: all 25 live profiles have ``cabinet.tools`` empty or
    absent, so no profile's scope changes when this ships. The alias exists for
    forward-compat and for any profile edited before the rename lands, not to
    carry existing data.
    """
    if not isinstance(config, dict):
        return PersonaToolScope()

    raw_toolsets = config.get("toolsets")
    raw_tools = config.get("tools")
    if raw_toolsets is not None or raw_tools is not None:
        return PersonaToolScope(
            toolsets=_clean_name_tuple(raw_toolsets),
            tools=_clean_name_tuple(raw_tools),
        )

    cabinet = config.get("cabinet")
    if isinstance(cabinet, dict) and cabinet.get("tools") is not None:
        legacy = _clean_name_tuple(cabinet.get("tools"))
        if legacy:
            _logger.warning(
                "persona config uses the deprecated `cabinet.tools` key "
                "(%s). That name claimed to scope cabinet meetings while "
                "gating every persona turn on every surface. Move these to "
                "the top-level `tools:` list, or declare a `toolsets:` entry.",
                ", ".join(legacy),
            )
        return PersonaToolScope(tools=legacy, used_deprecated_alias=True)

    return PersonaToolScope()


def _clean_name_tuple(value: Any) -> tuple[str, ...]:
    """Strings only, stripped, blanks dropped, ORDER and duplicates preserved.

    Order is meaningful downstream (toolset resolution is order-sensitive for
    diagnostics), and silently deduplicating would hide a config mistake the
    operator should see.
    """
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _validate_toolsets_section(value: Any, config_path: Path) -> None:
    """Validate the persona-level ``toolsets:`` list (epic #236).

    The honestly-named replacement for ``cabinet.tools``. That key claimed to
    scope cabinet meetings while actually gating a persona's whole tool surface
    on every surface — the name lied, which is why it is being retired rather
    than extended.

    Names are validated for SHAPE here, not for existence. Whether a toolset is
    registered is a runtime question (`runtime.toolsets.TOOLSETS` is a live
    registry that plugins extend), and failing config load because a toolset
    has not been imported yet would make profile loading depend on import
    order. Unknown names resolve to nothing at assembly time — fail-closed,
    per the tool registry's silent-on-missing contract.
    """
    if not isinstance(value, list):
        raise _shape_error(config_path, "toolsets", value, "list")
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            raise _shape_error(config_path, f"toolsets[{idx}]", item, "str")
        if not item.strip():
            raise ConfigShapeError(
                f"toolsets[{idx}]: toolset name must not be blank in {config_path}"
            )


def _validate_tools_section(value: Any, config_path: Path) -> None:
    """Validate the persona-level ``tools:`` list — individual grants.

    An escape hatch for "this persona needs exactly one extra verb" without
    minting a whole toolset for it. Deliberately NOT the primary mechanism:
    toolsets are what make one persona differ from the other twenty-four, and a
    config that grants everything tool-by-tool has no scoping story left.
    """
    if not isinstance(value, list):
        raise _shape_error(config_path, "tools", value, "list")
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            raise _shape_error(config_path, f"tools[{idx}]", item, "str")
        if not item.strip():
            raise ConfigShapeError(f"tools[{idx}]: tool name must not be blank in {config_path}")


def _validate_capability_blueprint_section(value: Any, config_path: Path) -> None:
    """Validate compiler-owned, profile-local capability metadata."""

    if not isinstance(value, dict):
        raise _shape_error(config_path, "capability_blueprint", value, "mapping")
    allowed = {
        "schema_version",
        "template",
        "domain",
        "domain_packs",
        "operator_exec",
        "env_groups",
        "skill_groups",
        "skills",
        "scheduled_authorities",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigShapeError(
            "capability_blueprint: unknown field(s) "
            f"{', '.join(unknown)} in {config_path}"
        )
    version = value.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _shape_error(
            config_path, "capability_blueprint.schema_version", version, "int"
        )
    for field in ("template", "domain"):
        raw = value.get(field)
        if not isinstance(raw, str) or not raw.strip():
            raise _shape_error(
                config_path, f"capability_blueprint.{field}", raw, "non-empty str"
            )
    operator_exec = value.get("operator_exec")
    if not isinstance(operator_exec, bool):
        raise _shape_error(
            config_path,
            "capability_blueprint.operator_exec",
            operator_exec,
            "bool",
        )
    for field in (
        "domain_packs",
        "env_groups",
        "skill_groups",
        "skills",
        "scheduled_authorities",
    ):
        raw = value.get(field, [])
        if not isinstance(raw, list):
            raise _shape_error(
                config_path, f"capability_blueprint.{field}", raw, "list[str]"
            )
        for index, item in enumerate(raw):
            if not isinstance(item, str) or not item.strip():
                raise _shape_error(
                    config_path,
                    f"capability_blueprint.{field}[{index}]",
                    item,
                    "non-empty str",
                )


def _validate_cabinet_section(value: Any, config_path: Path) -> None:
    """Validate the ``cabinet`` section: mapping with optional fields.

    Recognised fields:
      * ``voice_id`` (str) — TTS voice identifier
      * ``voice_provider`` (str, enum) — Phase 6 cabinet voice provider key.
        Must be one of :data:`_CABINET_VOICE_PROVIDER_ENUM`.
      * ``voice_persona_prompt`` (str) — Phase 6 per-persona voice system
        prompt (replaces ClaudeClaw warroom/personas.AGENT_PERSONAS dict
        per Q5 single-config-yaml lock).
      * ``avatar_path`` (str) — Phase 6 per-persona avatar override path
        (relative to profile root or absolute). Bundled fallback at
        ``cabinet/voice/static/avatars/{persona_id}.png`` when unset.
      * ``tools`` (list[str]) — cabinet/warroom tool names
        (Q-naming lock: ClaudeClaw "warroom_tools" → our "cabinet.tools")
      * ``portfolio_context`` (bool) — cofounder v2 WS1: inject the
        operator-vault portfolio digest into this persona's cabinet turns
    """
    if not isinstance(value, dict):
        raise _shape_error(config_path, "cabinet", value, "mapping")
    if "portfolio_context" in value and not isinstance(value["portfolio_context"], bool):
        raise _shape_error(
            config_path,
            "cabinet.portfolio_context",
            value["portfolio_context"],
            "bool",
        )
    if "voice_id" in value and not isinstance(value["voice_id"], str):
        raise _shape_error(config_path, "cabinet.voice_id", value["voice_id"], "str")
    # PRD-8 Phase 6 — voice_provider enum validation.
    if "voice_provider" in value:
        provider = value["voice_provider"]
        if not isinstance(provider, str):
            raise _shape_error(config_path, "cabinet.voice_provider", provider, "str")
        if provider not in _CABINET_VOICE_PROVIDER_ENUM:
            raise ConfigShapeError(
                f"cabinet.voice_provider: {provider!r} is not a known voice "
                f"provider (known: {', '.join(sorted(_CABINET_VOICE_PROVIDER_ENUM))}) "
                f"in {config_path}"
            )
    if "voice_persona_prompt" in value and not isinstance(value["voice_persona_prompt"], str):
        raise _shape_error(
            config_path,
            "cabinet.voice_persona_prompt",
            value["voice_persona_prompt"],
            "str",
        )
    if "avatar_path" in value and not isinstance(value["avatar_path"], str):
        raise _shape_error(config_path, "cabinet.avatar_path", value["avatar_path"], "str")
    if "tools" in value:
        tools = value["tools"]
        if not isinstance(tools, list):
            raise _shape_error(config_path, "cabinet.tools", tools, "list")
        for idx, item in enumerate(tools):
            if not isinstance(item, str):
                raise _shape_error(config_path, f"cabinet.tools[{idx}]", item, "str")


def _validate_delegation_section(value: Any, config_path: Path) -> None:
    """Validate the ``delegation`` section (cofounder v2 WS3).

    The persona-side half of the delegation grain (Rule 4): a persona is a
    delegation target ONLY when this block exists, and repo-scoped work
    additionally requires the repo slug in ``repos``. Fail-closed by
    absence — no block means the cofounder cannot assign work here.

    Recognised fields:
      * ``repos`` (list[str]) — REPOSITORIES.md slugs this persona may be
        assigned repo work on. Empty list = non-repo work only.
    """
    if not isinstance(value, dict):
        raise _shape_error(config_path, "delegation", value, "mapping")
    if "repos" in value:
        repos = value["repos"]
        if not isinstance(repos, list):
            raise _shape_error(config_path, "delegation.repos", repos, "list")
        for idx, item in enumerate(repos):
            if not isinstance(item, str):
                raise _shape_error(config_path, f"delegation.repos[{idx}]", item, "str")


def _validate_voice_section(value: Any, config_path: Path) -> None:
    """Validate the ``voice`` section: mapping with optional cascade list.

    Q5 lock (PRPs/planning/PRD-8-phase-1-decisions.md:255) — cascade items
    accept TWO shapes:
      * bare provider name as a string (e.g. ``cascade: [edge, gradium]``)
      * mapping with at minimum a ``provider`` key for opt-in tuning
        (e.g. ``cascade: [{provider: elevenlabs, voice_id: ...}]``)
    Either shape's provider name must be in ``_KNOWN_VOICE_PROVIDERS``
    (Phase 4 wires the actual clients; Phase 2 ships the schema only).
    """
    if not isinstance(value, dict):
        raise _shape_error(config_path, "voice", value, "mapping")
    if "cascade" in value:
        cascade = value["cascade"]
        if not isinstance(cascade, list):
            raise _shape_error(config_path, "voice.cascade", cascade, "list")
        for idx, item in enumerate(cascade):
            if isinstance(item, str):
                provider = item
            elif isinstance(item, dict):
                provider = item.get("provider")
                if provider is None:
                    raise _shape_error(
                        config_path,
                        f"voice.cascade[{idx}].provider",
                        None,
                        "str (one of " + ", ".join(sorted(_KNOWN_VOICE_PROVIDERS)) + ")",
                    )
                if not isinstance(provider, str):
                    raise _shape_error(
                        config_path,
                        f"voice.cascade[{idx}].provider",
                        provider,
                        "str",
                    )
            else:
                raise _shape_error(
                    config_path,
                    f"voice.cascade[{idx}]",
                    item,
                    "str or mapping",
                )
            if provider not in _KNOWN_VOICE_PROVIDERS:
                raise ConfigShapeError(
                    f"voice.cascade[{idx}]: provider {provider!r} is "
                    f"unknown (known: "
                    f"{', '.join(sorted(_KNOWN_VOICE_PROVIDERS))}) "
                    f"in {config_path}"
                )


def _validate_learning_section(value: Any, config_path: Path) -> None:
    """Validate the ``learning`` section: mapping with optional ``enabled`` bool.

    Persona learning loop (PRP persona-learning-loop / US-005). The section
    is opt-in per persona; ``learning.enabled`` defaults OFF when absent.
    """
    if not isinstance(value, dict):
        raise _shape_error(config_path, "learning", value, "mapping")
    if "enabled" in value and not isinstance(value["enabled"], bool):
        raise _shape_error(config_path, "learning.enabled", value["enabled"], "bool")


_MARKET_ROUND_TOP_LEVEL = frozenset(
    {"enabled", "domain", "source", "cadence", "budgets", "model", "delivery"}
)
_MARKET_ROUND_SOURCE_KEYS = frozenset(
    {"debauchery_alias", "approved_guild_id", "discord_channels", "x_feeds"}
)
_MARKET_ROUND_CADENCE_KEYS = frozenset(
    {
        "every_hours",
        "discord_minute",
        "x_minute",
        "research_prefetch_times",
        "rollup_times",
        "timezone",
    }
)
_MARKET_ROUND_BUDGET_KEYS = frozenset(
    {
        "discord_messages_per_channel",
        "x_items_per_feed",
        "last30days_days",
        "last30days_runs_per_day",
        "max_evidence_chars",
    }
)
_MARKET_ROUND_MODEL_KEYS = frozenset({"tier", "judge_tier", "max_turns"})
_MARKET_ROUND_DELIVERY_KEYS = frozenset(
    {"enabled", "binding_file", "ping_on_call"}
)


def _reject_unknown_keys(
    value: dict[str, Any],
    allowed: frozenset[str],
    *,
    field: str,
    config_path: Path,
) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ConfigShapeError(
            f"{field}: unknown field(s) {', '.join(unknown)} in {config_path}"
        )


def _validate_market_round_section(value: Any, config_path: Path) -> None:
    """Validate the strict profile-private Crypto Homie round contract.

    This section contains tenant-specific source IDs, so the framework only
    validates its shape.  Runtime code keeps it in the named profile and the
    sanitizer excludes the physical profile tree.
    """
    if not isinstance(value, dict):
        raise _shape_error(config_path, "market_round", value, "mapping")
    _reject_unknown_keys(
        value, _MARKET_ROUND_TOP_LEVEL, field="market_round", config_path=config_path
    )

    if "enabled" in value and not isinstance(value["enabled"], bool):
        raise _shape_error(config_path, "market_round.enabled", value["enabled"], "bool")
    if "domain" in value:
        domain = value["domain"]
        if not isinstance(domain, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,62}", domain.strip()
        ):
            raise ConfigShapeError(
                f"market_round.domain: use a lowercase slug in {config_path}"
            )

    source = value.get("source", {})
    if not isinstance(source, dict):
        raise _shape_error(config_path, "market_round.source", source, "mapping")
    _reject_unknown_keys(
        source,
        _MARKET_ROUND_SOURCE_KEYS,
        field="market_round.source",
        config_path=config_path,
    )
    for key in ("debauchery_alias", "approved_guild_id"):
        if key in source and (not isinstance(source[key], str) or not source[key].strip()):
            raise _shape_error(
                config_path, f"market_round.source.{key}", source[key], "non-empty str"
            )
    channels = source.get("discord_channels", [])
    if not isinstance(channels, list):
        raise _shape_error(
            config_path, "market_round.source.discord_channels", channels, "list"
        )
    seen_channels: set[str] = set()
    for index, channel in enumerate(channels):
        field = f"market_round.source.discord_channels[{index}]"
        if not isinstance(channel, dict):
            raise _shape_error(config_path, field, channel, "mapping")
        _reject_unknown_keys(
            channel,
            frozenset({"id", "name", "tier"}),
            field=field,
            config_path=config_path,
        )
        channel_id = channel.get("id")
        if not isinstance(channel_id, str) or not channel_id.isdigit():
            raise _shape_error(config_path, f"{field}.id", channel_id, "digit string")
        if channel_id in seen_channels:
            raise ConfigShapeError(f"{field}.id: duplicate channel in {config_path}")
        seen_channels.add(channel_id)
        if channel.get("tier") not in {"primary", "secondary", "tertiary"}:
            raise ConfigShapeError(
                f"{field}.tier: expected primary, secondary, or tertiary in {config_path}"
            )
        if "name" in channel and not isinstance(channel["name"], str):
            raise _shape_error(config_path, f"{field}.name", channel["name"], "str")
    x_feeds = source.get("x_feeds", ["for_you", "following"])
    if not isinstance(x_feeds, list) or not x_feeds:
        raise _shape_error(config_path, "market_round.source.x_feeds", x_feeds, "non-empty list")
    if any(feed not in {"for_you", "following"} for feed in x_feeds):
        raise ConfigShapeError(
            f"market_round.source.x_feeds: expected for_you/following in {config_path}"
        )

    cadence = value.get("cadence", {})
    if not isinstance(cadence, dict):
        raise _shape_error(config_path, "market_round.cadence", cadence, "mapping")
    _reject_unknown_keys(
        cadence,
        _MARKET_ROUND_CADENCE_KEYS,
        field="market_round.cadence",
        config_path=config_path,
    )
    cadence_ranges = {
        "every_hours": (1, 24),
        "discord_minute": (0, 59),
        "x_minute": (0, 59),
    }
    for key, (minimum, maximum) in cadence_ranges.items():
        if key in cadence:
            raw = cadence[key]
            if isinstance(raw, bool) or not isinstance(raw, int) or not minimum <= raw <= maximum:
                raise ConfigShapeError(
                    f"market_round.cadence.{key}: expected {minimum}..{maximum} in {config_path}"
                )
    for key in ("research_prefetch_times", "rollup_times"):
        if key not in cadence:
            continue
        times = cadence[key]
        if not isinstance(times, list) or any(
            not isinstance(item, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item)
            for item in times
        ):
            raise _shape_error(config_path, f"market_round.cadence.{key}", times, "list[HH:MM]")
    if "timezone" in cadence and (
        not isinstance(cadence["timezone"], str) or not cadence["timezone"].strip()
    ):
        raise _shape_error(
            config_path, "market_round.cadence.timezone", cadence["timezone"], "non-empty str"
        )

    budgets = value.get("budgets", {})
    if not isinstance(budgets, dict):
        raise _shape_error(config_path, "market_round.budgets", budgets, "mapping")
    _reject_unknown_keys(
        budgets,
        _MARKET_ROUND_BUDGET_KEYS,
        field="market_round.budgets",
        config_path=config_path,
    )
    budget_ranges = {
        "discord_messages_per_channel": (1, 250),
        "x_items_per_feed": (1, 250),
        "last30days_days": (1, 30),
        "last30days_runs_per_day": (0, 2),
        "max_evidence_chars": (1_000, 100_000),
    }
    for key, (minimum, maximum) in budget_ranges.items():
        if key in budgets:
            raw = budgets[key]
            if isinstance(raw, bool) or not isinstance(raw, int) or not minimum <= raw <= maximum:
                raise ConfigShapeError(
                    f"market_round.budgets.{key}: expected {minimum}..{maximum} in {config_path}"
                )

    model = value.get("model", {})
    if not isinstance(model, dict):
        raise _shape_error(config_path, "market_round.model", model, "mapping")
    _reject_unknown_keys(
        model,
        _MARKET_ROUND_MODEL_KEYS,
        field="market_round.model",
        config_path=config_path,
    )
    for key in ("tier", "judge_tier"):
        if key in model and model[key] not in {"fast", "quality"}:
            raise ConfigShapeError(
                f"market_round.model.{key}: expected fast or quality in {config_path}"
            )
    if "max_turns" in model and model["max_turns"] != 6:
        raise ConfigShapeError(
            f"market_round.model.max_turns: scheduled rounds are fixed at 6 in {config_path}"
        )

    delivery = value.get("delivery", {})
    if not isinstance(delivery, dict):
        raise _shape_error(config_path, "market_round.delivery", delivery, "mapping")
    _reject_unknown_keys(
        delivery,
        _MARKET_ROUND_DELIVERY_KEYS,
        field="market_round.delivery",
        config_path=config_path,
    )
    for key in ("enabled", "ping_on_call"):
        if key in delivery and not isinstance(delivery[key], bool):
            raise _shape_error(
                config_path,
                f"market_round.delivery.{key}",
                delivery[key],
                "bool",
            )
    if "binding_file" in delivery and (
        not isinstance(delivery["binding_file"], str)
        or not delivery["binding_file"].strip()
    ):
        raise _shape_error(
            config_path,
            "market_round.delivery.binding_file",
            delivery["binding_file"],
            "non-empty str",
        )
    if delivery.get("enabled") is True and not str(
        delivery.get("binding_file") or ""
    ).strip():
        raise ConfigShapeError(
            "market_round.delivery.binding_file: required when delivery is enabled "
            f"in {config_path}"
        )


_CURRICULUM_POLICY_ENUM = frozenset({"full", "curated"})
_CURRICULUM_KIND_ENUM = frozenset({"youtube_channel", "okf_seed"})
_CURRICULUM_MODEL_TIER_ENUM = frozenset({"fast", "quality"})


def _validate_curriculum_section(value: Any, config_path: Path) -> None:
    """Validate the private per-persona curriculum contract.

    URLs are restricted to HTTPS. YouTube channel sources must name a YouTube
    host and OKF seeds must name a git HTTPS URL. Runtime code resolves channel
    IDs and confines local paths separately; this validation is the persisted
    configuration boundary.
    """
    from urllib.parse import urlparse

    if not isinstance(value, dict):
        raise _shape_error(config_path, "curriculum", value, "mapping")

    for key in ("enabled",):
        if key in value and not isinstance(value[key], bool):
            raise _shape_error(config_path, f"curriculum.{key}", value[key], "bool")

    if "domain" in value:
        domain = value["domain"]
        if not isinstance(domain, str):
            raise _shape_error(config_path, "curriculum.domain", domain, "str")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", domain.strip()):
            raise ConfigShapeError(f"curriculum.domain: use a lowercase slug in {config_path}")

    integer_ranges = {
        "schedule_hours": (1, 168),
        "backfill_limit": (0, 10_000),
        "metadata_batch_size": (1, 50),
        "daily_skims": (0, 100),
        "daily_deep_studies": (0, 25),
        "steady_daily_deep_studies": (0, 10),
    }
    for key, (minimum, maximum) in integer_ranges.items():
        if key not in value:
            continue
        raw = value[key]
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise _shape_error(config_path, f"curriculum.{key}", raw, "int")
        if not minimum <= raw <= maximum:
            raise ConfigShapeError(
                f"curriculum.{key}: expected {minimum}..{maximum}, got {raw} in {config_path}"
            )

    for key in ("admission_model_tier", "study_model_tier"):
        if key not in value:
            continue
        tier = value[key]
        if not isinstance(tier, str):
            raise _shape_error(config_path, f"curriculum.{key}", tier, "str")
        if tier not in _CURRICULUM_MODEL_TIER_ENUM:
            raise ConfigShapeError(f"curriculum.{key}: expected fast or quality in {config_path}")

    sources = value.get("sources", [])
    if not isinstance(sources, list):
        raise _shape_error(config_path, "curriculum.sources", sources, "list")
    seen_ids: set[str] = set()
    for index, source in enumerate(sources):
        path = f"curriculum.sources[{index}]"
        if not isinstance(source, dict):
            raise _shape_error(config_path, path, source, "mapping")
        source_id = source.get("id")
        if not isinstance(source_id, str):
            raise _shape_error(config_path, f"{path}.id", source_id, "str")
        source_id = source_id.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", source_id):
            raise ConfigShapeError(f"{path}.id: use a lowercase slug in {config_path}")
        if source_id in seen_ids:
            raise ConfigShapeError(f"{path}.id: duplicate source id {source_id!r} in {config_path}")
        seen_ids.add(source_id)

        kind = source.get("kind", "youtube_channel")
        if not isinstance(kind, str) or kind not in _CURRICULUM_KIND_ENUM:
            raise ConfigShapeError(
                f"{path}.kind: expected one of "
                f"{', '.join(sorted(_CURRICULUM_KIND_ENUM))} in {config_path}"
            )
        policy = source.get("policy", "curated")
        if not isinstance(policy, str) or policy not in _CURRICULUM_POLICY_ENUM:
            raise ConfigShapeError(f"{path}.policy: expected full or curated in {config_path}")
        url = source.get("url")
        if not isinstance(url, str):
            raise _shape_error(config_path, f"{path}.url", url, "str")
        parsed = urlparse(url.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise ConfigShapeError(
                f"{path}.url: only public HTTPS URLs are accepted in {config_path}"
            )
        if parsed.username is not None or parsed.password is not None:
            raise ConfigShapeError(
                f"{path}.url: credentials in URLs are not accepted in {config_path}"
            )
        host = parsed.hostname.casefold()
        if kind == "youtube_channel" and host not in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
        }:
            raise ConfigShapeError(
                f"{path}.url: YouTube channel source must use youtube.com in {config_path}"
            )
        if "seed_url" in source:
            seed_url = source["seed_url"]
            if not isinstance(seed_url, str):
                raise _shape_error(config_path, f"{path}.seed_url", seed_url, "str")
            seed = urlparse(seed_url.strip())
            if seed.scheme != "https" or not seed.hostname:
                raise ConfigShapeError(
                    f"{path}.seed_url: only public HTTPS URLs are accepted in {config_path}"
                )
            if seed.username is not None or seed.password is not None:
                raise ConfigShapeError(
                    f"{path}.seed_url: credentials in URLs are not accepted in {config_path}"
                )


# ── PRD-8 Phase 3 / WS2 (R1 B4) — validation helpers ─────────────────────
#
# Public schema validators for ``<profile>/config.yaml`` content.
# Consumed by ``dashboard_api.py`` PATCH handler so the dashboard slice
# NEVER imports ``yaml`` directly (Q5 lock — single YAML parser surface).
#
# These re-use the internal ``_validate_*_section`` helpers above; they do
# NOT duplicate validation logic. ``personas.__all__`` grows from 14 → 16
# in WS2 with explicit personas-owner sign-off.
#
# Anti-pattern compliance:
#  * Rule 1: no def-time bind to module-level constants — both helpers take
#    raw ``data`` / ``text`` and return / raise. No optional args.
#  * Rule 2: zero file I/O — the YAML PATCH path stages content in memory
#    before atomic write at the call site. These helpers never read or
#    cache from disk.


# Sentinel ``Path`` reused so the section validators (which require a path
# for error messages) get a stable, message-friendly value when no file
# context exists. Defined as a ``Path`` rather than ``str`` so the
# ``f-string`` formatting at the validators stays type-uniform.
_DICT_VALIDATION_PATH: Path = Path("<config-dict>")


def validate_config_dict(data: dict) -> None:
    """Validate a parsed ``config.yaml`` dict against the section schema.

    PRD-8 Phase 3 / WS2 (R1 B4) — public schema-only validator. Reuses the
    private ``_validate_*_section`` helpers above so dashboard PATCH paths
    pick up future schema additions automatically.

    Behavior:
      * Top-level must be a ``dict`` (not list, not None, not scalar).
      * Each known section (``ports``, ``persona``, ``model``, ``mcp``,
        ``cabinet``, ``voice``) is validated when present. Missing sections
        are accepted silently — operators may author partial configs.
      * Unknown keys at the top level are accepted (forward-compat).

    Raises ``ConfigShapeError`` on shape violation. The error message
    includes the offending field path; the path string is a literal
    sentinel ``<config-dict>`` so callers know the validation ran on
    in-memory data, not on a file.
    """
    if not isinstance(data, dict):
        raise ConfigShapeError(
            f"shape: top-level must be mapping, got {type(data).__name__} "
            f"in {_DICT_VALIDATION_PATH}"
        )

    if "ports" in data:
        _validate_ports_section(data["ports"], _DICT_VALIDATION_PATH)
    if "persona" in data:
        _validate_persona_section(data["persona"], _DICT_VALIDATION_PATH)
    if "model" in data:
        _validate_model_section(data["model"], _DICT_VALIDATION_PATH)
    if "mcp" in data:
        _validate_mcp_section(data["mcp"], _DICT_VALIDATION_PATH)
    if "toolsets" in data:
        _validate_toolsets_section(data["toolsets"], _DICT_VALIDATION_PATH)
    if "tools" in data:
        _validate_tools_section(data["tools"], _DICT_VALIDATION_PATH)
    if "capability_blueprint" in data:
        _validate_capability_blueprint_section(
            data["capability_blueprint"], _DICT_VALIDATION_PATH
        )
    if "cabinet" in data:
        _validate_cabinet_section(data["cabinet"], _DICT_VALIDATION_PATH)
    if "voice" in data:
        _validate_voice_section(data["voice"], _DICT_VALIDATION_PATH)
    if "learning" in data:
        _validate_learning_section(data["learning"], _DICT_VALIDATION_PATH)
    if "curriculum" in data:
        _validate_curriculum_section(data["curriculum"], _DICT_VALIDATION_PATH)
    if "delegation" in data:
        _validate_delegation_section(data["delegation"], _DICT_VALIDATION_PATH)
    if "market_round" in data:
        _validate_market_round_section(data["market_round"], _DICT_VALIDATION_PATH)


def validate_config_yaml_text(text: str) -> dict:
    """Parse + validate raw YAML text, returning the parsed dict on success.

    PRD-8 Phase 3 / WS2 (R1 B4) — single entry point for the dashboard
    PATCH /api/agents/{id}/files/config.yaml endpoint. Operator-authored
    YAML text comes in; validated dict goes out. The dashboard slice
    NEVER calls ``yaml.safe_load`` directly — it round-trips through this
    helper so any parser swap (PyYAML → ruamel, etc.) happens in ONE
    place.

    Behavior:
      * Empty text or ``null`` YAML → parsed as ``{}`` (empty config is
        legal — operator may scaffold then save).
      * YAML parse error → raises ``ConfigShapeError`` with prefix
        ``yaml: <config-text>: <yaml-error-detail>``.
      * Schema error → raises ``ConfigShapeError`` from the section
        validator (message includes the field path).
      * Top-level non-dict (e.g. text is just a list) → raises
        ``ConfigShapeError(shape: ...)``.

    Returns the validated dict on success.
    """
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigShapeError(f"yaml: <config-text>: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigShapeError(
            f"shape: top-level must be mapping, got {type(raw).__name__} in <config-text>"
        )

    # Re-use the dict validator so the two helpers share one validation
    # path. ``validate_config_dict`` raises ``ConfigShapeError`` directly;
    # we let it propagate untouched.
    validate_config_dict(raw)
    return raw


def merge_config_patch(
    current: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Deep-merge a compiler patch without discarding authored sections.

    Mappings merge recursively; all other values, including explicit empty
    lists, replace the prior value. The result is validated before it is
    returned and neither input is mutated.
    """

    if not isinstance(current, dict) or not isinstance(patch, dict):
        raise ConfigShapeError("config merge requires two mappings")

    def _merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
        merged = copy.deepcopy(left)
        for key, value in right.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = _merge(merged[key], value)
            else:
                merged[key] = copy.deepcopy(value)
        return merged

    result = _merge(current, patch)
    validate_config_dict(result)
    return result


def dump_config_yaml(data: dict[str, Any]) -> str:
    """Validate and deterministically serialize profile config YAML."""

    validate_config_dict(data)
    return yaml.safe_dump(
        data,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=False,
    )
