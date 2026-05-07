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

import hashlib
import os
import socket
import tempfile
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
_KNOWN_VOICE_PROVIDERS: frozenset[str] = frozenset({
    "edge",
    "elevenlabs",
    "groq",
    "gradium",
    "openai",
    "google",
    "azure",
})


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
    ``shared.py:329``"). Bash scripts (``run_chat.sh``, ``run_chat.bat``,
    ``bot-status.sh``) consolidate onto this path after the §8.5 refactor.

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
        raise ValueError(
            f"Unknown service '{service}'. Known: {list(_LEGACY_PORTS.keys())}"
        )
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
            raise RuntimeError(
                f"No free port found near base={base} for service '{service}'"
            )
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


def load_persona_config(persona_id: str | None = None) -> dict[str, Any]:
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
    actual_id = (
        persona_id if persona_id is not None else _activity.get_active_profile_name()
    )
    config_path = _resolve_profile_config_path(actual_id)

    if not config_path.is_file():
        # R3 NM1 — only the default profile permits empty-dict back-compat
        # (default-profile bootstrap). Active named profile + missing
        # config.yaml is a setup error.
        if persona_id is None and actual_id == "default":
            return {}
        raise FileNotFoundError(
            f"config.yaml not found for persona {actual_id!r}: {config_path}"
        )

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
            f"shape: {config_path}: top-level must be mapping, "
            f"got {type(raw).__name__}"
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
    if "cabinet" in raw:
        _validate_cabinet_section(raw["cabinet"], config_path)
    if "voice" in raw:
        _validate_voice_section(raw["voice"], config_path)

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


def _write_persisted_port(
    config_path: Path, service: str, port: int
) -> None:
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
            f"shape: {config_path}: ports must be mapping, "
            f"got {type(ports).__name__}"
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


def _shape_error(
    config_path: Path, field: str, actual: Any, expected: str
) -> ConfigShapeError:
    """Construct a uniform ConfigShapeError with field path + path context."""
    return ConfigShapeError(
        f"{field}: {actual!r} (expected {expected}) in {config_path}"
    )


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
            raise _shape_error(
                config_path, f"persona.{field}", value[field], "str"
            )


def _validate_model_section(value: Any, config_path: Path) -> None:
    """Validate the ``model`` section: mapping with optional string fields.

    Recognised fields:
      * ``preferred`` (str) — preferred model id
      * ``fallback`` (list[str]) — fallback chain
    """
    if not isinstance(value, dict):
        raise _shape_error(config_path, "model", value, "mapping")
    if "preferred" in value and not isinstance(value["preferred"], str):
        raise _shape_error(
            config_path, "model.preferred", value["preferred"], "str"
        )
    if "fallback" in value:
        fallback = value["fallback"]
        if not isinstance(fallback, list):
            raise _shape_error(config_path, "model.fallback", fallback, "list")
        for idx, item in enumerate(fallback):
            if not isinstance(item, str):
                raise _shape_error(
                    config_path, f"model.fallback[{idx}]", item, "str"
                )


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


def _validate_cabinet_section(value: Any, config_path: Path) -> None:
    """Validate the ``cabinet`` section: mapping with optional fields.

    Recognised fields:
      * ``voice_id`` (str) — TTS voice identifier
      * ``tools`` (list[str]) — cabinet/warroom tool names
        (Q-naming lock: ClaudeClaw "warroom_tools" → our "cabinet.tools")
    """
    if not isinstance(value, dict):
        raise _shape_error(config_path, "cabinet", value, "mapping")
    if "voice_id" in value and not isinstance(value["voice_id"], str):
        raise _shape_error(
            config_path, "cabinet.voice_id", value["voice_id"], "str"
        )
    if "tools" in value:
        tools = value["tools"]
        if not isinstance(tools, list):
            raise _shape_error(config_path, "cabinet.tools", tools, "list")
        for idx, item in enumerate(tools):
            if not isinstance(item, str):
                raise _shape_error(
                    config_path, f"cabinet.tools[{idx}]", item, "str"
                )


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
                        "str (one of "
                        + ", ".join(sorted(_KNOWN_VOICE_PROVIDERS))
                        + ")",
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
    if "cabinet" in data:
        _validate_cabinet_section(data["cabinet"], _DICT_VALIDATION_PATH)
    if "voice" in data:
        _validate_voice_section(data["voice"], _DICT_VALIDATION_PATH)


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
            f"shape: top-level must be mapping, got {type(raw).__name__} "
            f"in <config-text>"
        )

    # Re-use the dict validator so the two helpers share one validation
    # path. ``validate_config_dict`` raises ``ConfigShapeError`` directly;
    # we let it propagate untouched.
    validate_config_dict(raw)
    return raw
