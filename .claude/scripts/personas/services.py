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
import json
import logging
import os
import re
import socket
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml  # M1 lock 2026-05-04 — replaces hand-rolled mini-parser

# Rule 3 / B2 fix: import the activity module so monkeypatching propagates.
# Top-level ``from .activity import get_active_profile_name`` would cache the
# function object — tests patching ``personas.activity.get_active_profile_name``
# would patch a name we no longer reference. Same enforcement Rule 3 already
# requires for ``runtime.langfuse_setup``.
from . import activity as _activity

# Rule 3 again: the grant ledger, the refusal type, and the live-registry
# lookup are reached through the module so a test patching
# ``personas.toolset_grants.audit_attempt`` (or the registry behind it)
# propagates into the executor below.
from . import toolset_grants as _toolset_grants
from .core import (
    get_default_homie_root,
    get_default_paths,
    get_homie_home,
    get_persona_paths,
    reject_sentinel_persona_name,
    validate_persona_name,
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


def append_persona_learning_audit(
    persona_id: str,
    *,
    enabled: bool,
    actor: str,
) -> None:
    """Append one row to the persona-learning audit ledger (fail-open).

    Single source of truth for the row shape, so every door that flips
    ``learning.enabled`` writes the SAME record: the operator CLI verbs
    (``thehomie profile learning enable|disable``) and profile creation
    (``personas.lifecycle.create_profile``, issue #422). ``actor`` is what
    tells them apart — ``"cli_profile_learning"`` vs
    ``"lifecycle_create_profile"``.

    The ledger lives at ``<active-profile data dir>/persona_learning_audit
    .jsonl``: it records what the OPERATOR's process did, so it stays in
    the calling profile's data dir rather than the subject persona's.

    Fail-open by contract — this is a receipt, not the invariant. A ledger
    failure must never undo a config write that already landed or fail a
    profile creation that is already fully on disk; every swallow logs a
    warning receipt.

    ``config`` is imported INSIDE the body: ``config.py`` imports
    ``personas``, so a module-level import would close that cycle.
    """
    try:
        import config as _config  # noqa: PLC0415 — cycle-safe lazy import

        audit_path = Path(_config.DATA_DIR) / "persona_learning_audit.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "persona_id": persona_id,
            "action": "enable" if enabled else "disable",
            "enabled": enabled,
            "actor": actor,
        }
        with open(audit_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception as exc:
        _logger.warning(
            "persona learning audit row not written for %r (actor=%r): %s",
            persona_id,
            actor,
            exc,
        )


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


# ── TOOLSET SELF-PROVISIONING EXECUTOR (issue #426) ─────────────────────
#
# The only path that GRANTS a toolset from an operator turn. Command
# surfaces, a counter-offer approve tap, and any future skill intake all
# come through here, because this is where the four things that make a
# grant safe sit together: the operator turn that ordered it is recorded,
# the role is checked, the name is verified against the LIVE registry, and
# the write is a strict-read RMW that refuses to clobber a malformed file.
#
# It is NOT the only writer of the ``toolsets:`` key, and the comment used
# to claim it was. Blueprint provisioning renders the whole config from a
# template (``personas/blueprints.py`` -> ``personas/provisioning.py``).
# What is true post-#426-round-4: a reconcile can no longer ERASE an
# executor-owned grant — ``_render_managed_files`` unions
# ``toolset_grants.active_grants()`` (the ledger replayed: grants minus
# revokes) into the rendered config, so a granted toolset survives a
# blueprint rewrite and the provisioning receipt names what it preserved.
# Routing every blueprint toolset delta THROUGH this executor, so template
# adds/removes also carry operator-turn provenance, is issue #435.
#
# What it deliberately does NOT touch: a single per-tool gate. A grant
# widens which tools a persona can REACH; social writes, sends, spends,
# browser writes and integration actions keep their own default-deny gates
# unchanged. Reach is not action.
#
# ``persona_mutation`` is the existing operator kill-switch for persona
# persistent-state writes (dashboard soft/hard delete, avatar writes,
# curriculum bootstrap, cofounder persona seed). A toolset grant writes
# persona config state, so it honors the same switch rather than minting a
# new one — the switch only turns the surface OFF, never on.
#
# Two things a caller must know. (1) Both entrypoints do synchronous file
# IO; an async surface calls them through ``asyncio.to_thread`` so a slow
# disk cannot wedge the event loop. (2) ``actor_role`` is TRUSTED here — the
# executor checks it, it does not establish it. Resolve the role server-side
# from the authenticated surface and never from anything the caller (or a
# model) asserted; passing through a claimed role turns the admin gate into
# a formality (#427).
_TOOLSET_GRANT_KILL_SWITCH = "persona_mutation"

# How long a grant waits for another writer's read-modify-write to finish.
# The critical section is one small YAML read, one appended ledger line, and
# one small YAML write — milliseconds — so this is queue headroom, not a
# working budget. Bounded rather than infinite because the executor is called
# from a chat turn: a wedged holder must surface as an audited error, not a
# hung reply.
_TOOLSET_GRANT_LOCK_TIMEOUT_S = 10.0


def add_persona_toolset(
    persona_id: str,
    toolset: str,
    *,
    actor: str,
    actor_role: str,
    trigger_text: str,
    surface: str,
    channel_id: str,
    audit_path: Path | str | None = None,
) -> _toolset_grants.ToolsetGrantResult:
    """Grant one registered toolset to a named persona. Live next turn.

    Pattern-copy of ``set_persona_learning``: strict-read RMW via
    ``_read_yaml_strict`` + ``_minimal_yaml_write``, so a malformed
    config.yaml surfaces as ``ConfigShapeError`` instead of being silently
    wiped, and the write itself is atomic.

    ``actor``, ``trigger_text``, ``surface``, and ``channel_id`` are
    REQUIRED, not optional context. The epic's metric is "zero grants
    without a matching live operator turn"; making the turn — including the
    channel that carried it — part of the signature means a grant nobody
    ordered, or one nothing can trace back to a channel, cannot be
    expressed, and the ledger proves it by construction.

    ``actor_role`` MUST be resolved server-side by the caller from the
    authenticated surface — never trust a caller-asserted role (#427). This
    executor CHECKS the role; it cannot establish it. A surface that
    forwards a role claimed in a payload (or produced by a model) has
    reduced the admin gate to a formality.

    No cache invalidation, no runtime nudge: ``resolve_toolset()`` resolves
    from the registry on every call and ``resolve_persona_tool_scope()``
    re-reads the config, so the grant is live on the persona's next turn.

    Raises:
        ToolsetGrantRefusedError: unknown toolset (with nearest matches), unknown
            or invalid persona, missing operator turn, non-admin role, or
            the unsupported default profile. Nothing is written.
        ToolsetGrantAuditError: a REQUIRED ledger row could not be written —
            a refusal that could not be recorded (nothing was written), or a
            completed mutation whose outcome row failed (the config DID
            change; the correlation id is on disk as intent-only).
        ConfigShapeError: the persona's config.yaml is malformed, or its
            existing ``toolsets`` value is not a clean list of names. The
            file is left untouched.
        KillSwitchDisabled: the operator disabled ``persona_mutation``.
    """
    return _mutate_persona_toolset(
        _toolset_grants.OPERATION_GRANT,
        persona_id,
        toolset,
        actor=actor,
        actor_role=actor_role,
        trigger_text=trigger_text,
        surface=surface,
        channel_id=channel_id,
        audit_path=audit_path,
    )


def remove_persona_toolset(
    persona_id: str,
    toolset: str,
    *,
    actor: str,
    actor_role: str,
    trigger_text: str,
    surface: str,
    channel_id: str,
    audit_path: Path | str | None = None,
) -> _toolset_grants.ToolsetGrantResult:
    """Revoke one toolset from a named persona — same executor, same ledger.

    Reversibility is the safety argument for the whole feature, so removal
    ships alongside the grant rather than behind it.

    One deliberate asymmetry: a revoke does NOT check the live registry.
    Removing a name only ever shrinks the persona's reach, and a toolset
    that was granted and later unregistered must still be removable — a
    registry check there would strand the declaration forever. The config's
    own contents are the authority: a name the persona does not hold comes
    back as ``not_granted`` with what it does hold, not as a silent success.

    ``actor_role`` carries the same rule as the grant side: resolve it
    server-side from the authenticated surface, never from a caller-asserted
    claim (#427).
    """
    return _mutate_persona_toolset(
        _toolset_grants.OPERATION_REVOKE,
        persona_id,
        toolset,
        actor=actor,
        actor_role=actor_role,
        trigger_text=trigger_text,
        surface=surface,
        channel_id=channel_id,
        audit_path=audit_path,
    )


def add_persona_tool(
    persona_id: str,
    tool: str,
    *,
    actor: str,
    actor_role: str,
    trigger_text: str,
    surface: str,
    channel_id: str,
    audit_path: Path | str | None = None,
) -> _toolset_grants.ToolsetGrantResult:
    """Grant one registered TOOL to a named persona (epic #465 1c).

    The single-capability grain of :func:`add_persona_toolset`: same executor,
    same gates, same ledger — only the config key (``tools:``), the registry
    it validates against (``runtime.tool_registry``, not the toolset
    registry), and the ledger operation differ.

    Grant = reach-only: a ``dedicated_gate`` tool CAN be granted here,
    because granting it lets the persona PROPOSE actions through its
    dedicated approval gate — execution stays behind that gate either way.

    ``actor_role`` carries the same rule as the toolset side: resolved
    server-side by the caller, checked here, never established here.
    """
    return _mutate_persona_toolset(
        _toolset_grants.OPERATION_GRANT_TOOL,
        persona_id,
        tool,
        actor=actor,
        actor_role=actor_role,
        trigger_text=trigger_text,
        surface=surface,
        channel_id=channel_id,
        audit_path=audit_path,
        kind=_toolset_grants.KIND_TOOL,
    )


def remove_persona_tool(
    persona_id: str,
    tool: str,
    *,
    actor: str,
    actor_role: str,
    trigger_text: str,
    surface: str,
    channel_id: str,
    audit_path: Path | str | None = None,
) -> _toolset_grants.ToolsetGrantResult:
    """Revoke one tool from a named persona — same executor, same ledger.

    Same deliberate asymmetry as the toolset revoke: NO registry check, so a
    tool that was granted and later unregistered is still removable.
    """
    return _mutate_persona_toolset(
        _toolset_grants.OPERATION_REVOKE_TOOL,
        persona_id,
        tool,
        actor=actor,
        actor_role=actor_role,
        trigger_text=trigger_text,
        surface=surface,
        channel_id=channel_id,
        audit_path=audit_path,
        kind=_toolset_grants.KIND_TOOL,
    )


def describe_grant_failure(
    exc: Exception,
    *,
    persona_id: str = "",
    identity_reason: str = "",
) -> str:
    """One canonical mapping from a grant/revoke failure to operator text.

    Downstream convention flagged for #427-#429 (issue #435): every calling
    surface — a chat command, a future dashboard PATCH, a CLI — used to
    invent its own phrasing per exception type, which meant the SAME
    ``REASON_*`` code could read differently depending on which door raised
    it. This is the one place that decides the words; a caller just catches
    the three exception types this executor raises and hands the exception
    here.

    ``identity_reason`` is the extra clause a caller's own server-side
    authentication check produced (e.g. "this surface stamped you 'viewer',
    not admin") — appended only to a :data:`toolset_grants.REASON_NOT_AUTHORIZED`
    refusal, because that is the one reason the executor's own message
    ("requires the admin role") does not already explain.

    ``persona_id`` is only used for the :class:`ConfigShapeError` branch,
    which names the persona whose config.yaml is malformed; every other
    branch speaks from the exception's own message.
    """
    grants = _toolset_grants
    if isinstance(exc, grants.ToolsetGrantRefusedError):
        text = str(exc)
        if exc.reason == grants.REASON_NOT_AUTHORIZED and identity_reason:
            return f"{text} ({identity_reason})"
        return text
    if isinstance(exc, grants.ToolsetGrantAuditError):
        if getattr(exc, "applied", False):
            # The mutation already landed — only its ledger confirmation
            # failed. Never say "refused" about a change that is live.
            return str(exc)
        return f"refused: {exc}"
    if isinstance(exc, ConfigShapeError):
        return (
            f"refused: {persona_id}'s config.yaml is malformed, so nothing "
            f"was written. {exc}"
        )
    return f"{type(exc).__name__}: {exc}"


def _mutate_persona_toolset(
    operation: str,
    persona_id: str,
    toolset: str,
    *,
    actor: str,
    actor_role: str,
    trigger_text: str,
    surface: str,
    channel_id: str,
    audit_path: Path | str | None,
    kind: str = "toolset",
) -> _toolset_grants.ToolsetGrantResult:
    """Shared grant/revoke body, over BOTH grant grains (#465 1c).

    ``kind="toolset"`` (the default, and every pre-1c call) mutates config
    ``toolsets:`` under ``grant``/``revoke``; ``kind="tool"`` mutates config
    ``tools:`` under ``grant_tool``/``revoke_tool``. Every gate below is
    shared verbatim — admin role, persona_mutation kill switch, default
    profile refusal, cross-process file lock, strict-read RMW, atomic write,
    intent/outcome ledger pair, torn-write healing — because a capability
    grant is a scope mutation with exactly the same stakes as a bundle grant;
    only the config key, the registry it validates against, and the ledger
    operation differ.

    Ledger contract (round-4 correction — the old "exactly one row per exit"
    was false on the success path and could label a precondition as the
    completed outcome):

    * a NON-mutating exit (refusal, already-granted, not-granted, config
      shape, lock timeout) writes exactly ONE row and never touches the file;
    * a MUTATING exit writes TWO rows sharing a ``correlation_id`` — an
      ``intent`` row appended BEFORE the atomic replace, and the
      ``granted`` / ``revoked`` row appended only AFTER it returned. A failed
      replace therefore leaves ``intent`` + ``error``, never a false success;
    * the intent row is still the mutation's PRECONDITION: it uses the strict
      ``append_audit_record``, so an unwritable ledger aborts before the
      config is touched.

    An intent row with no matching outcome row reads as "authorized, started,
    never confirmed" — which is exactly what a torn write is.
    """
    grants = _toolset_grants
    persona = str(persona_id or "").strip()
    name = str(toolset or "").strip()
    who = str(actor or "").strip()
    role = str(actor_role or "").strip().lower()
    trigger = grants.normalize_trigger_text(trigger_text)
    surface = str(surface or "").strip()
    channel_id = str(channel_id or "").strip()
    kind = str(kind or "").strip() or grants.KIND_TOOLSET
    is_grant = operation in {grants.OPERATION_GRANT, grants.OPERATION_GRANT_TOOL}
    # The one noun that changes per grain — used in refusal text so an
    # operator is never told a "toolset" was at fault when they named a tool.
    word = "tool" if kind == grants.KIND_TOOL else "toolset"
    config_key = "tools" if kind == grants.KIND_TOOL else "toolsets"
    validate_section = (
        _validate_tools_section if kind == grants.KIND_TOOL else _validate_toolsets_section
    )

    correlation_id = grants.new_correlation_id()

    def _row_fields(
        outcome: str,
        *,
        reason: str,
        toolsets_after: tuple[str, ...],
        suggestions: tuple[str, ...],
        config_path: Path | str,
        error: str,
        correlation: str = "",
        audit_path_override: Path | str | None = None,
    ) -> dict[str, Any]:
        return {
            "operation": operation,
            "persona_id": persona,
            "toolset": name,
            "kind": kind,
            "outcome": outcome,
            "reason": reason,
            "actor": who,
            "actor_role": role,
            "surface": surface,
            "channel_id": channel_id,
            "trigger_text": trigger,
            "toolsets_after": toolsets_after,
            "suggestions": suggestions,
            "config_path": config_path,
            "error": error,
            # A repair row belongs to the TORN attempt it settles, not to
            # this one, so it can carry that attempt's correlation id.
            "correlation_id": correlation or correlation_id,
            # An explicitly injected ``audit_path`` from the CALLER always
            # wins; the override only fills in when the caller passed none.
            "audit_path": audit_path if audit_path is not None else audit_path_override,
        }

    def _audit(
        outcome: str,
        *,
        reason: str = "",
        toolsets_after: tuple[str, ...] = (),
        suggestions: tuple[str, ...] = (),
        config_path: Path | str = "",
        error: str = "",
        correlation: str = "",
    ) -> str:
        return grants.audit_attempt(
            **_row_fields(
                outcome,
                reason=reason,
                toolsets_after=toolsets_after,
                suggestions=suggestions,
                config_path=config_path,
                error=error,
                correlation=correlation,
            )
        )

    def _audit_strict(
        outcome: str,
        *,
        reason: str = "",
        toolsets_after: tuple[str, ...] = (),
        suggestions: tuple[str, ...] = (),
        config_path: Path | str = "",
        error: str = "",
        audit_path_override: Path | str | None = None,
    ) -> str:
        return grants.append_audit_record(
            **_row_fields(
                outcome,
                reason=reason,
                toolsets_after=toolsets_after,
                suggestions=suggestions,
                config_path=config_path,
                error=error,
                audit_path_override=audit_path_override,
            )
        )

    def _refuse(
        reason: str,
        message: str,
        *,
        suggestions: tuple[str, ...] = (),
        config_path: Path | str = "",
        audit_path_override: Path | str | None = None,
    ) -> None:
        # A refusal row is REQUIRED, not best-effort. The acceptance criterion
        # is "unknown toolset -> refusal audited", and a caller that catches
        # ToolsetGrantRefusedError has no way to tell an audited refusal from
        # a swallowed one — so an unwritable ledger must NOT come back as a
        # polished "no". It comes back as a distinct audit failure instead.
        try:
            _audit_strict(
                grants.OUTCOME_REFUSED,
                reason=reason,
                suggestions=suggestions,
                config_path=config_path,
                error=message,
                audit_path_override=audit_path_override,
            )
        except Exception as exc:  # noqa: BLE001 — re-raised as a distinct type
            _logger.error(
                "persona toolset %s: refusal (%s) could not be audited: %s: %s",
                operation,
                reason,
                type(exc).__name__,
                exc,
            )
            raise grants.ToolsetGrantAuditError(
                f"refusal could not be audited ({reason}): "
                f"{type(exc).__name__}: {exc}. Nothing was written.",
                reason=reason,
            ) from exc
        raise grants.ToolsetGrantRefusedError(message, reason=reason, suggestions=suggestions)

    # ── Contract gates. All BEFORE any read or write; none can partially
    # apply, and each leaves a refusal row naming what was missing.
    if kind not in {grants.KIND_TOOLSET, grants.KIND_TOOL}:
        _refuse(
            grants.REASON_INVALID_TOOLSET,
            f"refused: unknown grant kind {kind!r} — expected toolset or tool.",
        )
    if not persona:
        _refuse(
            grants.REASON_INVALID_PERSONA,
            "refused: no persona named — say which homie the grant is for.",
        )
    if not name:
        _refuse(
            grants.REASON_INVALID_TOOLSET,
            f"refused: no {word} named — say which one to {operation}.",
        )
    if not who or not trigger or not surface or not channel_id:
        _refuse(
            grants.REASON_MISSING_OPERATOR_TURN,
            f"refused: a {word} {operation} needs the live operator turn that "
            "ordered it (actor + trigger_text + surface + channel_id). A "
            "grant nobody ordered, or one with no channel to trace it back "
            "to, is not expressible.",
        )
    if role != grants.ADMIN_ROLE:
        _refuse(
            grants.REASON_NOT_AUTHORIZED,
            f"refused: {word} {operation} requires the "
            f"{grants.ADMIN_ROLE} role, got {role or 'none'!r}.",
        )

    try:
        from security import kill_switches  # noqa: PLC0415 — Rule 3 module attr
    except Exception as exc:  # noqa: BLE001 — see comment
        # Precedent: runtime/persona_tools.py:152-156. The switch is an
        # operator OFF control, not the thing that grants capability, so its
        # absence must not silently disable a working feature. Receipt only.
        _logger.warning(
            "persona toolset %s: kill-switch module unavailable (%s: %s)",
            operation,
            type(exc).__name__,
            exc,
        )
    else:
        try:
            kill_switches.requireEnabled(
                _TOOLSET_GRANT_KILL_SWITCH,
                caller=f"personas.{kind}_{operation}",
            )
        except kill_switches.KillSwitchDisabled as exc:
            _audit(
                grants.OUTCOME_REFUSED,
                reason=grants.REASON_KILL_SWITCH,
                error=str(exc),
            )
            raise

    # ── Q6 spike verdict (2026-08-12, verified against this tree).
    # chat/engine.py resolves persona tools ONLY when the active profile is
    # not "default" (`if _active_profile and _active_profile != "default"`),
    # so the main homie's chat surface never calls
    # build_persona_tool_payload and never reads config `toolsets:` — its
    # tools come from config.DEFAULT_AGENT_TOOLSET. Writing the grant would
    # change a file nothing reads. Per the architecture's decision rule,
    # v1 is scoped to named personas and main-homie self-grant is a filed
    # follow-up. tests/test_persona_toolset_grants.py pins the engine gate,
    # so this refusal fails loudly the day the gate goes away.
    if persona == "default":
        _refuse(
            grants.REASON_DEFAULT_PROFILE_UNSUPPORTED,
            "refused: the default profile's chat surface does not read "
            "config `toolsets:` — chat/engine.py resolves persona tools only "
            "for a non-default active profile, so the main homie's tools "
            "come from DEFAULT_AGENT_TOOLSET. Granting here would write a "
            "file nothing reads. Self-provisioning v1 covers named personas; "
            "main-homie self-grant is a filed follow-up.",
        )

    try:
        validate_persona_name(persona)
        # ``custom`` clears validate_persona_name on purpose — it is a legal
        # ACTIVE-profile value, so boot/readiness/inventory must keep
        # accepting it (core.py:62-66). It is NOT a legal grant TARGET:
        # ``get_persona_paths("custom")`` roots at the AMBIENT get_homie_home()
        # instead of <root>/profiles/<name>/, so a grant keyed to it writes
        # config.yaml and the ledger into whichever profile THIS process runs
        # as, under an id that belongs to no persona. ("default" never reaches
        # here — the gate above refuses it with a more specific reason.)
        # Guarding at the EXECUTOR, not at the chat command, is what makes
        # this hold for every current and future door: the #422 creation-door
        # rejection is the same guard on the provisioning side.
        reject_sentinel_persona_name(persona)
    except ValueError as exc:
        _refuse(grants.REASON_INVALID_PERSONA, f"refused: {exc}")

    # Rule 2: the persona exists iff its profile directory is on disk. A
    # typo'd name must not conjure a ghost profile — _atomic_write_text
    # mkdirs its parent, so without this guard "sles" would provision a new
    # profile tree out of a misspelling.
    config_path = _resolve_profile_config_path(persona)
    if not config_path.parent.is_dir():
        # Codex R3 MAJOR 2: this refusal used to CREATE the persona it was
        # refusing. The ledger is target-keyed, so it resolved to
        # ``profiles/<name>/data`` and ``append_audit_record`` mkdir'd that
        # parent — after which THIS very gate saw a real directory and the
        # second identical command wrote config.yaml. Two refused grants
        # provisioned a persona outside the lifecycle provisioner.
        #
        # A persona with no profile has no ledger of its own to write to, so
        # the row goes to the ambient ledger — precisely the "no target
        # persona to key on" case ``resolve_ledger_path``'s fallback already
        # exists for. The row is relocated, never dropped, and the resolver
        # itself stays pure (a physical-existence check inside it would make
        # the same call return different paths before and after provisioning,
        # which breaks callers that resolve the path up front).
        _refuse(
            grants.REASON_UNKNOWN_PERSONA,
            f"refused: no profile directory for {persona!r} at "
            f"{config_path.parent} — create the persona first.",
            config_path=config_path,
            audit_path_override=grants.resolve_ledger_path(None, ""),
        )

    if is_grant:
        # Grant = reach-only. The name must be REGISTERED — an unknown name
        # is a refusal with nearest matches — but a registered
        # ``dedicated_gate`` tool is NOT refused: the grant expands reach,
        # and its dedicated action gate still authorizes every execution
        # downstream (the 1a doctrine).
        known = (
            grants.known_tool_names()
            if kind == grants.KIND_TOOL
            else grants.known_toolset_names()
        )
        if not known:
            # An OUTAGE, not a bad name. ``known_toolset_names()`` fails closed
            # to () when ``runtime.toolsets`` will not import or TOOLSETS is
            # not a mapping, and every name misses an empty registry — so
            # without this branch a registry outage refuses a perfectly good
            # toolset as "'research_read' is not in the live toolset registry
            # (0 registered)", blaming the operator for a broken import and
            # sending them to fix a name that was never wrong. Revoke is
            # deliberately NOT gated here: taking reach BACK must keep working
            # while the registry is down.
            _refuse(
                grants.REASON_REGISTRY_UNAVAILABLE,
                f"refused: the {word} registry is unavailable, so no grant can "
                f"be validated right now — this is not a problem with "
                f"{name!r}. Nothing was written; try again once it loads. "
                "(Revoking still works.)",
                config_path=config_path,
            )
        if name not in known:
            suggestions = grants.nearest_names(name, names=known)
            hint = f" Nearest: {', '.join(suggestions)}." if suggestions else ""
            reason = (
                grants.REASON_UNKNOWN_TOOL
                if kind == grants.KIND_TOOL
                else grants.REASON_UNKNOWN_TOOLSET
            )
            _refuse(
                reason,
                f"refused: {name!r} is not in the live {word} registry "
                f"({len(known)} registered).{hint}",
                suggestions=suggestions,
                config_path=config_path,
            )

    # Function-local by necessity: ``shared`` imports ``personas.services`` at
    # module level, so a top-level ``from shared import file_lock`` here would
    # close an import cycle. Same shape as ``crypto_round.market_notes``. The
    # module-attribute read also keeps Rule 3 — a test patching
    # ``shared.file_lock`` propagates into this call.
    from shared import file_lock  # noqa: PLC0415 — see comment

    # ── Serialized read-modify-write, keyed on the config file itself.
    #
    # ``os.replace`` makes ONE write atomic; it does nothing for the
    # read → decide → append → write sequence around it. Two concurrent
    # executor calls against the same persona would each read the same base
    # list, each append a truthful-looking success row, and the second
    # replace would drop the first grant — an accepted grant that is NOT
    # live on the next turn, with an append-only ledger swearing it is.
    # That race is reachable by design: this executor is documented for
    # ``asyncio.to_thread`` callers, so two channel turns land here at once.
    #
    # The lock covers the ledger append too, not just the file write, so the
    # audit-before-mutation ordering the success row depends on survives
    # concurrency. ``shared.file_lock`` is msvcrt/fcntl on a real ``.lock``
    # sibling, so this serializes ACROSS PROCESSES — required, because each
    # persona bot is its own process.
    acquired = False
    try:
        with file_lock(config_path, timeout=_TOOLSET_GRANT_LOCK_TIMEOUT_S):
            acquired = True

            # ── Strict-read RMW. A parse failure or a non-list `toolsets:` raises
            # rather than replacing the operator's file with our own shape.
            try:
                data = _read_yaml_strict(config_path)
                # An absent key defaults to empty; an explicit ``toolsets: null`` is
                # a malformed declaration and must raise, not get normalized away —
                # collapsing both to ``[]`` here would let a bad file silently heal
                # itself into whatever the caller happens to grant next.
                existing = data[config_key] if config_key in data else []
                validate_section(existing, config_path)
            except ConfigShapeError as exc:
                _audit(
                    grants.OUTCOME_ERROR,
                    reason=grants.REASON_CONFIG_SHAPE,
                    config_path=config_path,
                    error=str(exc),
                )
                raise

            current = tuple(str(item).strip() for item in existing)

            # ── Ledger repair, run BEFORE the "nothing to do" row is written.
            #
            # Either operation can tear the same way: intent row lands, config
            # mutation lands, outcome append fails and raises WITHOUT rolling
            # the config back. The retry is the moment that becomes
            # observable, because physical state now proves which way the
            # attempt actually went — a revoke retry finds the name ABSENT, a
            # grant retry finds it PRESENT. Record the effective row that
            # never landed, correlated to that torn attempt, so the ledger
            # stops disagreeing with the file it describes. Rule 2: the config
            # is the truth, the replay is derived from it.
            #
            # Best-effort by design. For a revoke the safety property is
            # already enforced in `active_grants` (a revoke intent drops the
            # name). For a grant the stake is the opposite and milder — an
            # unhealed grant is INVISIBLE to the replay, so a reconcile simply
            # does not preserve it — and neither case may turn an honest
            # already_granted / not_granted into an exception.
            def _heal_torn_attempt(effective_outcome: str, reason: str) -> None:
                torn = grants.orphan_intent_correlation(
                    persona, name, operation, audit_path, kind=kind
                )
                if torn:
                    _audit(
                        effective_outcome,
                        reason=reason,
                        toolsets_after=current,
                        config_path=config_path,
                        correlation=torn,
                    )

            if is_grant:
                if name in current:
                    _heal_torn_attempt(
                        grants.OUTCOME_GRANTED,
                        grants.REASON_REPAIR_CONFIG_PRESENT,
                    )
                    audit_id = _audit(
                        grants.OUTCOME_ALREADY_GRANTED,
                        toolsets_after=current,
                        config_path=config_path,
                    )
                    return grants.ToolsetGrantResult(
                        persona_id=persona,
                        toolset=name,
                        operation=operation,
                        outcome=grants.OUTCOME_ALREADY_GRANTED,
                        changed=False,
                        toolsets=current,
                        config_path=config_path,
                        audit_id=audit_id,
                    )
                # Authored entries are preserved verbatim; only the new name is ours.
                updated = [*existing, name]
                success_outcome = grants.OUTCOME_GRANTED
            else:
                if name not in current:
                    _heal_torn_attempt(
                        grants.OUTCOME_REVOKED,
                        grants.REASON_REPAIR_CONFIG_ABSENT,
                    )
                    # Honest miss, not a silent success: hand back what the persona
                    # actually holds so a typo gets a useful reply.
                    audit_id = _audit(
                        grants.OUTCOME_NOT_GRANTED,
                        toolsets_after=current,
                        suggestions=current,
                        config_path=config_path,
                    )
                    return grants.ToolsetGrantResult(
                        persona_id=persona,
                        toolset=name,
                        operation=operation,
                        outcome=grants.OUTCOME_NOT_GRANTED,
                        changed=False,
                        toolsets=current,
                        config_path=config_path,
                        audit_id=audit_id,
                        suggestions=current,
                    )
                # Every occurrence goes. A hand-edited duplicate left behind would
                # be a revoke that silently did not revoke.
                updated = [item for item in existing if str(item).strip() != name]
                success_outcome = grants.OUTCOME_REVOKED

            validate_section(updated, config_path)
            data[config_key] = updated
            effective = tuple(str(item).strip() for item in updated)

            # ── Intent row: the mutation's PRECONDITION. Epic metric 5 ("zero
            # grants without a matching live operator turn") has to be
            # greppable by construction, so an unwritable ledger aborts here,
            # BEFORE config.yaml is touched. Strict `append_audit_record`, not
            # the best-effort `_audit`.
            #
            # It says "authorized and starting", NOT "done". Writing the
            # success outcome here instead is what round 4 caught: a failed
            # atomic replace left a `granted` row for a grant that never
            # landed, and an append-only safety ledger cannot call a
            # precondition the completed outcome.
            _audit_strict(
                grants.OUTCOME_INTENT,
                toolsets_after=effective,
                config_path=config_path,
            )

            try:
                _minimal_yaml_write(config_path, data)
            except OSError as exc:
                # Correlated to the intent row above, so the pair reads as
                # "authorized, attempted, failed" instead of a bare error.
                _audit(
                    grants.OUTCOME_ERROR,
                    reason=grants.REASON_WRITE_FAILED,
                    toolsets_after=effective,
                    config_path=config_path,
                    error=str(exc),
                )
                raise

            # ── Outcome row. Physical state HAS moved; only now may the
            # ledger say so. A failure here is not swallowed: the caller must
            # not be told a grant is recorded when it is not. The intent row
            # already on disk makes the state legible (started, unconfirmed),
            # and a retry is idempotent — it comes back `already_granted`.
            try:
                audit_id = _audit_strict(
                    success_outcome,
                    toolsets_after=effective,
                    config_path=config_path,
                )
            except Exception as exc:  # noqa: BLE001 — re-raised as a distinct type
                _logger.error(
                    "persona toolset %s: config written but outcome row failed "
                    "(correlation %s): %s: %s",
                    operation,
                    correlation_id,
                    type(exc).__name__,
                    exc,
                )
                raise grants.ToolsetGrantAuditError(
                    f"{success_outcome} applied to {persona!r} but its ledger "
                    f"row could not be written ({type(exc).__name__}: {exc}). "
                    f"Correlation {correlation_id} is on disk as intent-only.",
                    reason=grants.REASON_WRITE_FAILED,
                    applied=True,
                ) from exc

            return grants.ToolsetGrantResult(
                persona_id=persona,
                toolset=name,
                operation=operation,
                outcome=success_outcome,
                changed=True,
                toolsets=effective,
                config_path=config_path,
                audit_id=audit_id,
            )
    except TimeoutError as exc:
        # Only an ACQUISITION timeout is ours to label. ``TimeoutError`` is an
        # ``OSError`` subclass, so a timeout raised from inside the critical
        # section has already been audited by the branch that owns it — the
        # `acquired` flag keeps this from writing a second, wrong row and
        # breaking "every exit writes exactly one ledger row".
        if acquired:
            raise
        _audit(
            grants.OUTCOME_ERROR,
            reason=grants.REASON_LOCK_TIMEOUT,
            config_path=config_path,
            error=str(exc),
        )
        raise


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
        result = yaml.safe_load(text)
    except UnicodeDecodeError as exc:
        # A config with a corrupt byte is a malformed config, and the whole
        # point of this reader is that malformed means "raise, do not touch".
        # UnicodeDecodeError is a ValueError, not an OSError, so it used to
        # escape BOTH handlers below — past ConfigShapeError and therefore
        # past the executor's refusal audit, which only catches
        # ConfigShapeError. The exit was unaudited (round 7).
        raise ConfigShapeError(f"encoding: {path}: {exc}") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigShapeError(f"yaml: {path}: {exc}") from exc
    # ONLY a None parse is "empty". `... or {}` collapsed every FALSEY root —
    # `[]`, `false`, `0`, `''` — into an empty mapping that sailed through the
    # isinstance check below, so a caller would write its own shape over a
    # file it never understood. That is exactly the clobber this function
    # exists to prevent, and it defeated it for four root types (round 6).
    if result is None:
        return {}
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
    {
        "enabled",
        "domain",
        "source",
        "cadence",
        "budgets",
        "model",
        "delivery",
        "source_tape",
        "visual_desk",
        "paper_portfolio",
        "nft_intelligence",
    }
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
    {"enabled", "binding_file", "ping_on_call", "include_source_tape"}
)
_MARKET_ROUND_SOURCE_TAPE_KEYS = frozenset(
    {
        "enabled",
        "discord_primary_scrolls",
        "discord_secondary_scrolls",
        "x_scroll_attempts",
        "x_minimum_scrolls",
        "x_target_items",
        "refresh_minutes",
        "speaker_aliases",
        "priority_speakers",
    }
)
_MARKET_ROUND_VISUAL_DESK_KEYS = frozenset({"enabled", "timeframe", "bars", "venue"})
_MARKET_ROUND_NFT_INTELLIGENCE_KEYS = frozenset(
    {
        "enabled",
        "chains",
        "candidate_limit",
        "deep_verify_limit",
        "max_logs_per_candidate",
        "max_rpc_calls_per_candidate",
        "max_provider_calls_per_round",
        "provider_wall_clock_budget_seconds",
        "max_log_chunks_per_candidate",
        "max_block_search_calls",
        "confirmation_depth",
        "mint_recency_hours",
        "verification_ttl_minutes",
        "provider_timeout_seconds",
    }
)
_MARKET_ROUND_PAPER_PORTFOLIO_KEYS = frozenset(
    {
        "enabled",
        "starting_balance_usd_per_sleeve",
        "probe_risk_fraction",
        "standard_risk_fraction",
        "conviction_risk_fraction",
        "max_total_open_risk_fraction",
        "max_notional_multiple",
        "max_open_calls",
        "min_reward_risk",
        "daily_loss_limit_fraction",
        "max_drawdown_fraction",
        "exploration_calls_per_day",
        "round_trip_cost_bps",
        "btc_scalp",
    }
)
_MARKET_ROUND_BTC_SCALP_KEYS = frozenset(
    {
        "enabled",
        "min_leverage_multiple",
        "max_leverage_multiple",
        "profit_target_fraction",
        "profit_target_window_days",
        "allowed_horizons",
    }
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
            frozenset({"id", "name", "tier", "guild_id", "community", "community_name"}),
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
        if "guild_id" in channel and (
            not isinstance(channel["guild_id"], str) or not channel["guild_id"].isdigit()
        ):
            raise _shape_error(
                config_path, f"{field}.guild_id", channel["guild_id"], "digit string"
            )
        if "community" in channel and (
            not isinstance(channel["community"], str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", channel["community"].strip())
        ):
            raise ConfigShapeError(
                f"{field}.community: use a lowercase source slug in {config_path}"
            )
        if "community_name" in channel and (
            not isinstance(channel["community_name"], str)
            or not channel["community_name"].strip()
        ):
            raise _shape_error(
                config_path,
                f"{field}.community_name",
                channel["community_name"],
                "non-empty str",
            )
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
    for key in ("enabled", "ping_on_call", "include_source_tape"):
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

    source_tape = value.get("source_tape", {})
    if not isinstance(source_tape, dict):
        raise _shape_error(config_path, "market_round.source_tape", source_tape, "mapping")
    _reject_unknown_keys(
        source_tape,
        _MARKET_ROUND_SOURCE_TAPE_KEYS,
        field="market_round.source_tape",
        config_path=config_path,
    )
    if "enabled" in source_tape and not isinstance(source_tape["enabled"], bool):
        raise _shape_error(
            config_path, "market_round.source_tape.enabled", source_tape["enabled"], "bool"
        )
    tape_ranges = {
        "discord_primary_scrolls": (1, 20),
        "discord_secondary_scrolls": (1, 20),
        "x_scroll_attempts": (1, 20),
        "x_minimum_scrolls": (1, 20),
        "x_target_items": (1, 250),
        "refresh_minutes": (1, 120),
    }
    for key, (minimum, maximum) in tape_ranges.items():
        if key in source_tape:
            raw = source_tape[key]
            if (
                isinstance(raw, bool)
                or not isinstance(raw, int)
                or not minimum <= raw <= maximum
            ):
                raise ConfigShapeError(
                    f"market_round.source_tape.{key}: expected "
                    f"{minimum}..{maximum} in {config_path}"
                )
    if int(source_tape.get("x_minimum_scrolls", 5)) > int(
        source_tape.get("x_scroll_attempts", 10)
    ):
        raise ConfigShapeError(
            "market_round.source_tape.x_minimum_scrolls cannot exceed "
            f"x_scroll_attempts in {config_path}"
        )
    aliases = source_tape.get("speaker_aliases", {})
    if not isinstance(aliases, dict):
        raise _shape_error(
            config_path, "market_round.source_tape.speaker_aliases", aliases, "mapping"
        )
    for canonical, values in aliases.items():
        if not isinstance(canonical, str) or not canonical.strip():
            raise _shape_error(
                config_path,
                "market_round.source_tape.speaker_aliases key",
                canonical,
                "non-empty str",
            )
        if not isinstance(values, list) or any(
            not isinstance(item, str) or not item.strip() for item in values
        ):
            raise _shape_error(
                config_path,
                f"market_round.source_tape.speaker_aliases.{canonical}",
                values,
                "list[non-empty str]",
            )
    priority = source_tape.get("priority_speakers", [])
    if not isinstance(priority, list) or any(
        not isinstance(item, str) or not item.strip() for item in priority
    ):
        raise _shape_error(
            config_path,
            "market_round.source_tape.priority_speakers",
            priority,
            "list[non-empty str]",
        )
    if len({item.strip().casefold().lstrip("@") for item in priority}) != len(priority):
        raise ConfigShapeError(
            "market_round.source_tape.priority_speakers: duplicate normalized speaker "
            f"in {config_path}"
        )

    visual_desk = value.get("visual_desk", {})
    if not isinstance(visual_desk, dict):
        raise _shape_error(config_path, "market_round.visual_desk", visual_desk, "mapping")
    _reject_unknown_keys(
        visual_desk,
        _MARKET_ROUND_VISUAL_DESK_KEYS,
        field="market_round.visual_desk",
        config_path=config_path,
    )
    if "enabled" in visual_desk and not isinstance(visual_desk["enabled"], bool):
        raise _shape_error(
            config_path, "market_round.visual_desk.enabled", visual_desk["enabled"], "bool"
        )
    if visual_desk.get("timeframe", "4h") not in {"5m", "10m", "15m", "1h", "4h", "1d"}:
        raise ConfigShapeError(
            f"market_round.visual_desk.timeframe: unsupported timeframe in {config_path}"
        )
    bars = visual_desk.get("bars", 120)
    if isinstance(bars, bool) or not isinstance(bars, int) or not 40 <= bars <= 200:
        raise ConfigShapeError(
            f"market_round.visual_desk.bars: expected 40..200 in {config_path}"
        )
    if visual_desk.get("venue", "okx") not in {
        "okx",
        "mexc",
        "bybit",
        "kraken",
        "coinbase",
    }:
        raise ConfigShapeError(
            f"market_round.visual_desk.venue: unsupported public venue in {config_path}"
        )

    nft = value.get("nft_intelligence", {})
    if not isinstance(nft, dict):
        raise _shape_error(
            config_path, "market_round.nft_intelligence", nft, "mapping"
        )
    _reject_unknown_keys(
        nft,
        _MARKET_ROUND_NFT_INTELLIGENCE_KEYS,
        field="market_round.nft_intelligence",
        config_path=config_path,
    )
    if "enabled" in nft and not isinstance(nft["enabled"], bool):
        raise _shape_error(
            config_path,
            "market_round.nft_intelligence.enabled",
            nft["enabled"],
            "bool",
        )
    chains = nft.get("chains", ["robinhood"])
    if (
        not isinstance(chains, list)
        or not chains
        or any(not isinstance(item, str) or not item for item in chains)
    ):
        raise _shape_error(
            config_path,
            "market_round.nft_intelligence.chains",
            chains,
            "non-empty list[str]",
        )
    if len(set(chains)) != len(chains):
        raise ConfigShapeError(
            "market_round.nft_intelligence.chains: duplicates are not allowed "
            f"in {config_path}"
        )
    if chains != ["robinhood"]:
        raise ConfigShapeError(
            "market_round.nft_intelligence.chains: Phase 1 requires exactly "
            f"[robinhood] in {config_path}"
        )
    nft_integer_ranges = {
        "candidate_limit": (1, 25),
        "deep_verify_limit": (1, 10),
        "max_logs_per_candidate": (1, 500),
        "max_rpc_calls_per_candidate": (1, 64),
        "max_provider_calls_per_round": (1, 128),
        "provider_wall_clock_budget_seconds": (60, 720),
        "max_log_chunks_per_candidate": (1, 16),
        "max_block_search_calls": (1, 24),
        "confirmation_depth": (1, 256),
        "mint_recency_hours": (1, 168),
        "verification_ttl_minutes": (1, 1_440),
        "provider_timeout_seconds": (1, 30),
    }
    nft_defaults = {
        "candidate_limit": 12,
        "deep_verify_limit": 5,
        "max_logs_per_candidate": 200,
        "max_rpc_calls_per_candidate": 32,
        "max_provider_calls_per_round": 64,
        "provider_wall_clock_budget_seconds": 600,
        "max_log_chunks_per_candidate": 8,
        "max_block_search_calls": 12,
        "confirmation_depth": 12,
        "mint_recency_hours": 72,
        "verification_ttl_minutes": 120,
        "provider_timeout_seconds": 8,
    }
    for key, (minimum, maximum) in nft_integer_ranges.items():
        raw = nft.get(key, nft_defaults[key])
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise _shape_error(
                config_path,
                f"market_round.nft_intelligence.{key}",
                raw,
                "int",
            )
        if not minimum <= raw <= maximum:
            raise ConfigShapeError(
                f"market_round.nft_intelligence.{key}: expected "
                f"{minimum}..{maximum} in {config_path}"
            )
    if nft.get("deep_verify_limit", 5) > nft.get("candidate_limit", 12):
        raise ConfigShapeError(
            "market_round.nft_intelligence.deep_verify_limit cannot exceed "
            f"candidate_limit in {config_path}"
        )

    paper = value.get("paper_portfolio", {})
    if not isinstance(paper, dict):
        raise _shape_error(config_path, "market_round.paper_portfolio", paper, "mapping")
    _reject_unknown_keys(
        paper,
        _MARKET_ROUND_PAPER_PORTFOLIO_KEYS,
        field="market_round.paper_portfolio",
        config_path=config_path,
    )
    if "enabled" in paper and not isinstance(paper["enabled"], bool):
        raise _shape_error(
            config_path, "market_round.paper_portfolio.enabled", paper["enabled"], "bool"
        )

    numeric_ranges = {
        "starting_balance_usd_per_sleeve": (1.0, 1_000_000.0),
        "probe_risk_fraction": (0.001, 0.50),
        "standard_risk_fraction": (0.001, 0.50),
        "conviction_risk_fraction": (0.001, 0.50),
        "max_total_open_risk_fraction": (0.001, 1.0),
        "max_notional_multiple": (0.1, 100.0),
        "min_reward_risk": (1.0, 20.0),
        "daily_loss_limit_fraction": (0.001, 1.0),
        "max_drawdown_fraction": (0.001, 1.0),
        "round_trip_cost_bps": (0.0, 500.0),
    }
    resolved_numbers: dict[str, float] = {}
    defaults = {
        "starting_balance_usd_per_sleeve": 1_000.0,
        "probe_risk_fraction": 0.025,
        "standard_risk_fraction": 0.05,
        "conviction_risk_fraction": 0.075,
        "max_total_open_risk_fraction": 0.15,
        "max_notional_multiple": 5.0,
        "min_reward_risk": 1.5,
        "daily_loss_limit_fraction": 0.15,
        "max_drawdown_fraction": 0.30,
        "round_trip_cost_bps": 15.0,
    }
    for key, (minimum, maximum) in numeric_ranges.items():
        raw = paper.get(key, defaults[key])
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise _shape_error(
                config_path, f"market_round.paper_portfolio.{key}", raw, "number"
            )
        number = float(raw)
        if not minimum <= number <= maximum:
            raise ConfigShapeError(
                f"market_round.paper_portfolio.{key}: expected {minimum}..{maximum} "
                f"in {config_path}"
            )
        resolved_numbers[key] = number

    if not (
        resolved_numbers["probe_risk_fraction"]
        <= resolved_numbers["standard_risk_fraction"]
        <= resolved_numbers["conviction_risk_fraction"]
        <= resolved_numbers["max_total_open_risk_fraction"]
    ):
        raise ConfigShapeError(
            "market_round.paper_portfolio risk tiers must increase from probe through "
            f"conviction without exceeding total open risk in {config_path}"
        )
    if (
        resolved_numbers["daily_loss_limit_fraction"]
        > resolved_numbers["max_drawdown_fraction"]
    ):
        raise ConfigShapeError(
            "market_round.paper_portfolio daily loss limit cannot exceed max drawdown "
            f"in {config_path}"
        )
    for key, minimum, maximum, default in (
        ("max_open_calls", 1, 20, 3),
        ("exploration_calls_per_day", 0, 12, 1),
    ):
        raw = paper.get(key, default)
        if isinstance(raw, bool) or not isinstance(raw, int) or not minimum <= raw <= maximum:
            raise ConfigShapeError(
                f"market_round.paper_portfolio.{key}: expected {minimum}..{maximum} "
                f"in {config_path}"
            )

    btc_scalp = paper.get("btc_scalp", {})
    if not isinstance(btc_scalp, dict):
        raise _shape_error(
            config_path,
            "market_round.paper_portfolio.btc_scalp",
            btc_scalp,
            "mapping",
        )
    _reject_unknown_keys(
        btc_scalp,
        _MARKET_ROUND_BTC_SCALP_KEYS,
        field="market_round.paper_portfolio.btc_scalp",
        config_path=config_path,
    )
    if "enabled" in btc_scalp and not isinstance(btc_scalp["enabled"], bool):
        raise _shape_error(
            config_path,
            "market_round.paper_portfolio.btc_scalp.enabled",
            btc_scalp["enabled"],
            "bool",
        )
    scalp_numbers: dict[str, float] = {}
    for key, minimum, maximum, default in (
        ("min_leverage_multiple", 1.0, 20.0, 5.0),
        ("max_leverage_multiple", 1.0, 20.0, 10.0),
        ("profit_target_fraction", 0.001, 1.0, 0.10),
    ):
        raw = btc_scalp.get(key, default)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise _shape_error(
                config_path,
                f"market_round.paper_portfolio.btc_scalp.{key}",
                raw,
                "number",
            )
        number = float(raw)
        if not minimum <= number <= maximum:
            raise ConfigShapeError(
                f"market_round.paper_portfolio.btc_scalp.{key}: expected "
                f"{minimum}..{maximum} in {config_path}"
            )
        scalp_numbers[key] = number
    window_days = btc_scalp.get("profit_target_window_days", 3)
    if (
        isinstance(window_days, bool)
        or not isinstance(window_days, int)
        or not 1 <= window_days <= 30
    ):
        raise ConfigShapeError(
            "market_round.paper_portfolio.btc_scalp.profit_target_window_days: "
            f"expected 1..30 in {config_path}"
        )
    if scalp_numbers["min_leverage_multiple"] > scalp_numbers["max_leverage_multiple"]:
        raise ConfigShapeError(
            "market_round.paper_portfolio.btc_scalp minimum leverage cannot exceed "
            f"maximum leverage in {config_path}"
        )
    horizons = btc_scalp.get("allowed_horizons", ["5m", "10m", "15m", "1h"])
    if (
        not isinstance(horizons, list)
        or not horizons
        or any(not isinstance(item, str) or not item.strip() for item in horizons)
    ):
        raise _shape_error(
            config_path,
            "market_round.paper_portfolio.btc_scalp.allowed_horizons",
            horizons,
            "non-empty list[str]",
        )
    normalized_horizons = [item.strip().casefold() for item in horizons]
    allowed_horizons = {"5m", "10m", "15m", "1h"}
    if len(set(normalized_horizons)) != len(normalized_horizons):
        raise ConfigShapeError(
            "market_round.paper_portfolio.btc_scalp.allowed_horizons contains "
            f"duplicates in {config_path}"
        )
    if not set(normalized_horizons) <= allowed_horizons:
        raise ConfigShapeError(
            "market_round.paper_portfolio.btc_scalp.allowed_horizons supports only "
            f"5m, 10m, 15m, and 1h in {config_path}"
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
