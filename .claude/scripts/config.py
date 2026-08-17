"""
Configuration for The Homie heartbeat system.

Path constants are resolved through the personas resolver
(``personas.get_persona_paths(personas.get_active_profile_name())``) so the
default profile keeps its install-dir layout while named/custom profiles
land under ``~/.homie/profiles/<name>/`` or ``HOMIE_HOME`` respectively.

PRP-7a Workstream 2 (config-refactor):
    - Default profile (HOMIE_HOME unset) returns the legacy install-dir paths
      via ``personas.get_default_paths()``. ``HOMIE_VAULT_DIR`` env override is
      preserved on ``MEMORY_DIR`` (PRP-7a R1 B5).
    - ``ENV_FILE`` is now a public module-level constant. WS3 entry points
      will consume ``from config import ENV_FILE`` to replace bare
      ``load_dotenv()`` and parent-path ``.env`` math.
    - ``BOT_PID_FILE`` / ``BOT_LOCK_FILE`` are pre-stubbed to the default
      install-dir layout (Phase 3 / PRP-7c owns the full consolidation).
    - Anti-pattern Rule 1 enforcement: ``personas.X`` values are read at
      import time only; nothing is bound as a function default arg.
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import personas

# === Persona-resolved paths (PRP-7a Workstream 2) ===
# Resolve once at import time. Default profile ("default") returns the legacy
# install-dir paths via ``personas.get_default_paths()`` — HOMIE_VAULT_DIR
# override on MEMORY_DIR is preserved (PRP-7a R1 B5). Named/custom profiles
# land under ``~/.homie/profiles/<name>/`` or ``HOMIE_HOME`` respectively.
_paths = personas.get_persona_paths(personas.get_active_profile_name())

# === Path constants ===
# ENV_FILE is the canonical .env path for the active profile. WS3 entry points
# import this to replace bare ``load_dotenv()`` and ``Path(...) / ".env"`` math.
ENV_FILE: Path = _paths["env_file"]

# Load environment variables from the active profile's .env file.
#
# Kill switches survive this load. The dotenv is applied with override=True, so
# without the capture/restore below a profile's own .env could set
# HOMIE_KILLSWITCH_<X>=enabled and quietly undo an operator's emergency stop in
# every process that loads this profile. Absent/enabled is the permissive
# direction and "disabled" is the refusal, so restoring only the DISABLED
# values can never grant a capability back — it can only keep one withheld.
from security.kill_switches import is_disabled_value  # noqa: E402

_operator_disabled_switches = {
    key: value
    for key, value in os.environ.items()
    if key.upper().startswith("HOMIE_KILLSWITCH_") and is_disabled_value(value)
}

load_dotenv(ENV_FILE, override=True)

if _operator_disabled_switches:
    os.environ.update(_operator_disabled_switches)

# Repo / install-dir locations — kept for back-compat (``runtime/bootstrap.py``,
# hooks, etc. import ``PROJECT_ROOT`` and ``SCRIPTS_DIR`` from config).
SCRIPTS_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPTS_DIR.parent.parent
CLAUDE_DIR = PROJECT_ROOT / ".claude"

# Vault location — override with HOMIE_VAULT_DIR. The personas resolver
# (``personas.get_default_paths()``) reads this env var on every call and
# applies it to the ``memory`` key for the default profile (PRP-7a R1 B5).
# For named/custom profiles, the override is ignored — ``memory`` lives under
# the profile root.
MEMORY_DIR = _paths["memory"]

# Memory file paths
SOUL_FILE = MEMORY_DIR / "SOUL.md"
USER_FILE = MEMORY_DIR / "USER.md"
MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
HEARTBEAT_FILE = MEMORY_DIR / "HEARTBEAT.md"
DAILY_DIR = MEMORY_DIR / "daily"
GOALS_FILE = MEMORY_DIR / "GOALS.md"
WEEKLY_DIR = MEMORY_DIR / "weekly"

# === Owner Identity ===
OWNER_NAME = os.getenv("OWNER_NAME", "")

# === Data Directory (databases, model caches) ===
DATA_DIR = _paths["data"]
DATABASE_PATH = DATA_DIR / "memory.db"
DATABASE_URL = os.getenv("DATABASE_URL", "")

# === Multi-vault recall registry (DB-per-vault) ===
# The recall index (memory.db) historically covered ONLY the thehomie Homie
# vault. These env-resolved paths let recall + indexing address coding-vault
# too, with its own SQLite DB under DATA_DIR. Framework code
# must NOT hardcode personal vault paths — they come from env (scrubbed on the
# public export). Unset env => None => that vault is simply unavailable.
# (unified-vault was merged into the thehomie vault on 2026-07-11.)
HOMIE_CODING_VAULT_DIR = (
    Path(os.getenv("HOMIE_CODING_VAULT_DIR")) if os.getenv("HOMIE_CODING_VAULT_DIR") else None
)

# thehomie keeps memory.db (back-compat); the others get suffixed DBs.
_VAULT_MEMORY_DIRS: dict[str, "Path | None"] = {
    "thehomie": MEMORY_DIR,
    "coding-vault": HOMIE_CODING_VAULT_DIR,
}
_VAULT_DB_PATHS: dict[str, Path] = {
    "thehomie": DATABASE_PATH,
    "coding-vault": DATA_DIR / "memory.coding-vault.db",
}
VAULT_NAMES = tuple(_VAULT_MEMORY_DIRS.keys())


def iter_persona_vaults() -> dict[str, tuple[Path, Path]]:
    """Live persona vaults as ``{id: (memory_dir, db_path)}`` — the read-only
    shelf entries behind ``resolve_vault`` / ``list_vault_names`` (issue #466).

    Physical enumeration per call (Rule 2, no module-scope cache): a persona
    created moments ago is addressable on the next call; a deleted one drops
    out immediately. A profile missing ``memory/`` or ``data/`` drops out (the
    deleted/half-provisioned case). Empty outside the default profile — the
    apartments read path is ONE-WAY (main→persona), so a persona process never
    resolves the estate, by name or by fan-out.
    """
    from personas import activity  # late-bind so monkeypatch propagates (Rule 3)
    from personas.core import list_persona_profile_ids

    try:
        if activity.get_active_profile_name() != "default":
            return {}
    except Exception:
        return {}
    vaults: dict[str, tuple[Path, Path]] = {}
    for pid in list_persona_profile_ids():
        try:
            paths = personas.get_persona_paths(pid)
            memory_dir = paths["memory"]
            data_dir = paths["data"]
            if not memory_dir.is_dir() or not data_dir.is_dir():
                continue
        except OSError:
            continue
        vaults[pid] = (memory_dir, data_dir / "memory.db")
    return vaults


def list_vault_names() -> tuple[str, ...]:
    """Static pair + live persona vault ids, resolved per call (Rule 1)."""
    return tuple(_VAULT_MEMORY_DIRS) + tuple(
        pid for pid in iter_persona_vaults() if pid not in _VAULT_MEMORY_DIRS
    )


def resolve_vault(name: str) -> "tuple[Path | None, Path]":
    """Vault name -> (memory_dir, db_path).

    The static registry wins FIRST — a persona whose id collides with
    ``thehomie``/``coding-vault`` is SHADOWED, not merged. ``memory_dir``
    is None when the vault's env path is unset (vault not configured on this
    machine) or the name is unknown. ``db_path`` is always defined.
    """
    if name in _VAULT_MEMORY_DIRS:
        return _VAULT_MEMORY_DIRS[name], _VAULT_DB_PATHS.get(name, DATABASE_PATH)
    persona = iter_persona_vaults().get(name)
    if persona is not None:
        return persona
    return None, DATABASE_PATH


def is_readonly_vault(name_or_dir: str | Path | None) -> bool:
    """True iff the argument addresses a persona vault (by plain id or by
    memory dir). Static vaults win first — in a persona process ``MEMORY_DIR``
    IS that persona's own memory dir, and the static match keeps that
    process's own recall byte-identical (never read-only against itself).
    """
    if name_or_dir is None:
        return False
    if str(name_or_dir) in _VAULT_MEMORY_DIRS:
        return False
    # Every static answer is settled BEFORE iter_persona_vaults(), which stats
    # the whole roster. This is the recall hot path: the default turn asks
    # about MEMORY_DIR (a Path, not a name), and enumerating N profiles to
    # answer a question the static registry already answers costs an iterdir
    # plus two stats per persona on every search.
    target = Path(name_or_dir).resolve()
    for vdir in _VAULT_MEMORY_DIRS.values():
        if vdir and Path(vdir).resolve() == target:
            return False
    persona_vaults = iter_persona_vaults()
    if str(name_or_dir) in persona_vaults:
        return True
    return any(
        Path(memory_dir).resolve() == target
        for memory_dir, _db in persona_vaults.values()
    )


def resolve_db_path(memory_dir: "Path | str | None" = None) -> Path:
    """Map a memory_dir to its per-vault SQLite DB (Rule 1: None sentinel resolved
    at call time).

    Defaults to the thehomie DB (``DATABASE_PATH``) when memory_dir is None or
    matches the thehomie vault — keeping the legacy single-vault path
    byte-identical. A known non-default vault dir maps to its suffixed DB; an
    unknown dir gets its own derived DB so an unindexed override never silently
    reads the wrong vault's data.
    """
    if memory_dir is None:
        return DATABASE_PATH
    md = Path(memory_dir).resolve()
    for _name, vdir in _VAULT_MEMORY_DIRS.items():
        if vdir and Path(vdir).resolve() == md:
            return _VAULT_DB_PATHS[_name]

    # Self-contained vault root (profile layout): ``<root>/memory`` with its DB
    # co-located at the sibling ``<root>/data/memory.db`` — exactly
    # ``personas.get_persona_paths``'s contract (memory/data siblings under
    # profile_root). Without this, every persona ``memory`` dir slugs to the
    # SAME ``DATA_DIR/memory.memory.db`` in the MAIN vault (name collision +
    # wrong root), silently reading/writing the wrong index — the cross-vault
    # pollution the slug fallback was meant to prevent. Guard is structural
    # (name == "memory") AND physical (sibling data/ exists — Rule 2), so it
    # only fires for a real vault root; every other unknown dir keeps the
    # legacy slug DB byte-identically. Registered vaults never reach here.
    if md.name == "memory" and (md.parent / "data").is_dir():
        return md.parent / "data" / "memory.db"

    import re as _re

    slug = _re.sub(r"[^A-Za-z0-9._-]+", "-", md.name) or "vault"
    return DATA_DIR / f"memory.{slug}.db"

# State files — per-machine operational data, NOT synced via Obsidian
STATE_DIR = _paths["state"]
HEARTBEAT_STATE_FILE = STATE_DIR / "heartbeat-state.json"

# Bot lifecycle paths (PRP-7c Phase 3 / R2 NB1): delegated to
# ``personas.services`` via the module-level ``__getattr__`` at the bottom
# of this file. ``BOT_PID_FILE`` / ``BOT_LOCK_FILE`` resolve at attribute
# access time so a profile swap mid-process takes effect immediately and
# the resolver stays the single source of truth.

# === Reflection Configuration ===
REFLECTION_STATE_FILE = STATE_DIR / "reflection-state.json"
REFLECTION_HOUR = int(os.getenv("REFLECTION_HOUR", "8"))

# === Weekly Synthesis Configuration ===
WEEKLY_STATE_FILE = STATE_DIR / "weekly-state.json"
WEEKLY_HOUR = int(os.getenv("WEEKLY_HOUR", "20"))  # Sunday 8 PM

# === Dream Consolidation Configuration ===
DREAM_STATE_FILE = STATE_DIR / "dream-state.json"
DREAM_MIN_INTERVAL_HOURS = int(os.getenv("DREAM_MIN_INTERVAL_HOURS", "12"))
DREAM_SIGNAL_THRESHOLD = int(os.getenv("DREAM_SIGNAL_THRESHOLD", "4"))

# === Hermes Scout Configuration ===
upstream_watch_ENABLED = os.getenv("upstream_watch_ENABLED", "true").lower() == "true"
upstream_watch_REPO = os.getenv("upstream_watch_REPO", "NousResearch/hermes-agent")
upstream_watch_STATE_FILE = STATE_DIR / "upstream_watch-state.json"

# === CLI Update-Check Configuration ===
UPDATE_CHECK_STATE_FILE = STATE_DIR / "update-check-state.json"
UPDATE_CHECK_MIN_INTERVAL_HOURS = int(os.getenv("UPDATE_CHECK_MIN_INTERVAL_HOURS", "24"))
UPDATE_CHECK_REPO = os.getenv("UPDATE_CHECK_REPO", "TheSmokeDev/taskchad-os")

# === Memory Recall Configuration ===
RECALL_ENABLED = os.getenv("RECALL_ENABLED", "true").lower() == "true"
RECALL_MIN_SCORE = float(os.getenv("RECALL_MIN_SCORE", "0.3"))
# Keyword-only recall floor. Raw FTS5 scores are 1/(1+|bm25|) — real hits land
# at ~0.05-0.17, so the hybrid-scale RECALL_MIN_SCORE (0.3) would filter nearly
# everything (the 2026-07-15 zero-results bug). Two score scales, two floors.
RECALL_KEYWORD_MIN_SCORE = float(os.getenv("RECALL_KEYWORD_MIN_SCORE", "0.02"))
RECALL_MAX_RESULTS = int(os.getenv("RECALL_MAX_RESULTS", "3"))
RECALL_MIN_MSG_LEN = int(os.getenv("RECALL_MIN_MSG_LEN", "20"))

# Background job recall limits (heartbeat, reflection, weekly)
RECALL_BACKGROUND_MAX_RESULTS = int(os.getenv("RECALL_BACKGROUND_MAX", "3"))
RECALL_BACKGROUND_MAX_CHARS = int(os.getenv("RECALL_BACKGROUND_CHARS", "2000"))

# LLM re-ranking (Tier 1 queries only)
RECALL_RERANK_ENABLED = os.getenv("RECALL_RERANK_ENABLED", "true").lower() == "true"
RECALL_RERANK_TOP_N = int(os.getenv("RECALL_RERANK_TOP_N", "10"))
RECALL_RERANK_TIMEOUT_S = float(os.getenv("RECALL_RERANK_TIMEOUT_S", "3.0"))

# Wiki-link graph cache (issue #129). When false, get_cached_memory_graph()
# always rebuilds — still off-loop, just uncached (pre-fix behavior minus the
# event-loop block). Operator rollback lever, no code change needed.
RECALL_GRAPH_CACHE_ENABLED = os.getenv("RECALL_GRAPH_CACHE_ENABLED", "true").lower() == "true"

# === Evolve (Self-Improvement Loop) ===
# Phase 2.4: when true, `evolve run` and `evolve propose` default to emitting
# Langfuse-tagged spans under user_id="evolve-replay" so experimental traces
# can be filtered out of production cost reports. Override per-invocation
# with --trace / --no-trace.
EVOLVE_TRACE_REPLAYS = os.getenv("EVOLVE_TRACE_REPLAYS", "false").lower() == "true"

# === Embedding Configuration ===
# BGE-base-en-v1.5 via FastEmbed / ONNX (swapped from EmbeddingGemma-300m 2026-04-22).
# Rationale: public Apache-2.0 model (no HF_TOKEN / gated license), ONNX-only runtime
# (drops sentence-transformers + torch, ~1 GB install savings), deterministic across
# platforms (load-bearing for the Evolve replay harness), MTEB retrieval parity with
# EmbeddingGemma on English. Native 768-dim, no Matryoshka truncation needed.
# Query side uses BGE's "Represent this sentence for searching..." prompt; passage
# side is unprompted per BGE v1.5 spec. Handled inside embeddings.py.
EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"
EMBEDDING_DIMENSIONS = 768
# Cross-platform default — override via EMBEDDING_CACHE_DIR env var (e.g. a larger
# drive on Windows). Matches the path documented in Section 03 of CLAUDE.md.
EMBEDDING_CACHE_DIR = Path(os.getenv("EMBEDDING_CACHE_DIR", str(DATA_DIR / "models")))

# === Integration Configuration (Phase 5) ===
INTEGRATIONS_DIR = _paths["credentials"]

# Google OAuth (shared token for all Google services; account identity lives in USER.md)
GOOGLE_CREDENTIALS_FILE = INTEGRATIONS_DIR / "google_credentials.json"
GOOGLE_TOKEN_FILE = INTEGRATIONS_DIR / "google_token.json"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
]

# Personal Gmail (your-calendar@gmail.com — read-only, separate token)
PERSONAL_GMAIL_ACCOUNT = os.getenv("PERSONAL_GMAIL_ACCOUNT", "your-calendar@gmail.com")
PERSONAL_GMAIL_TOKEN_PATH = os.getenv(
    "PERSONAL_GMAIL_TOKEN", str(INTEGRATIONS_DIR / "google_token_owner.json")
)
PERSONAL_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Asana
ASANA_ACCESS_TOKEN = os.getenv("ASANA_ACCESS_TOKEN", "")
ASANA_WORKSPACE_ID = os.getenv("ASANA_WORKSPACE_ID", "")
ASANA_PROJECT_ID = os.getenv("ASANA_PROJECT_ID", "")

# Asana user mapping — friendly name to GID (format: "name:gid,name:gid")
_asana_users_raw = os.getenv("ASANA_USERS", "")
ASANA_USERS: dict[str, str] = {}
if _asana_users_raw:
    for pair in _asana_users_raw.split(","):
        pair = pair.strip()
        if ":" in pair:
            name, gid = pair.split(":", 1)
            ASANA_USERS[name.strip().lower()] = gid.strip()

# Slack
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_NOTIFICATION_CHANNEL = os.getenv("SLACK_NOTIFICATION_CHANNEL", "#thehomie")
SLACK_MONITORED_CHANNELS = os.getenv("SLACK_MONITORED_CHANNELS", "thehomie").split(",")
SLACK_OWNER_USER_ID = os.getenv("SLACK_OWNER_USER_ID", "")

# Chat Interface
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")
CHAT_DB_PATH = DATA_DIR / "chat.db"
ORCHESTRATION_DB_PATH = DATA_DIR / "orchestration.db"
# Dashboard (PRD-8 Phase 3 / WS1) — operator-facing dashboard slice.
# DASHBOARD_DB_PATH env-overridable so tests can point at a tmp file without
# re-rooting HOMIE_HOME. Default mirrors CHAT_DB_PATH / ORCHESTRATION_DB_PATH
# (DATA_DIR / 'dashboard.db' = .claude/data/dashboard.db on the default
# profile). R1 B6 lock — DATA_DIR-rooted, NOT HOMIE_HOME-rooted.
DASHBOARD_DB_PATH = Path(
    os.getenv("DASHBOARD_DB_PATH", str(DATA_DIR / "dashboard.db"))
)
# PRD-8 Phase 3 / WS2 (R3 NM1) — bot lifecycle SIGTERM grace window before
# escalating to SIGKILL. Env-overridable so operators can tune for slow-
# shutdown bots without code changes. Consumed by
# .claude/scripts/dashboard_bot_lifecycle.py via the None-sentinel pattern
# (Rule 1 — every public function takes ``grace_seconds: int | None = None``
# and resolves to this constant inside the body, never at def time).
DASHBOARD_BOT_GRACE_SECONDS = int(os.getenv("DASHBOARD_BOT_GRACE_SECONDS", "5"))
CHAT_MAX_TURNS = int(os.getenv("CHAT_MAX_TURNS", "25"))
CHAT_MAX_BUDGET_USD = float(os.getenv("CHAT_MAX_BUDGET_USD", "2.0"))
CHAT_ENGINE_TIMEOUT_SECONDS = float(os.getenv("CHAT_ENGINE_TIMEOUT_SECONDS", "900"))
# doc-upload-truthful-reads Phase 2 — attachment full-read caps + attachment-turn
# timeout. Consumers resolve these at CALL TIME via None-sentinel params
# (Rule 1) so /reload takes effect without a restart.
CHAT_ATTACHMENT_MAX_BYTES = int(os.getenv("CHAT_ATTACHMENT_MAX_BYTES", str(8 * 1024 * 1024)))
CHAT_ATTACHMENT_MAX_CHARS = int(os.getenv("CHAT_ATTACHMENT_MAX_CHARS", "100000"))
CHAT_ATTACHMENT_TOTAL_MAX_CHARS = int(os.getenv("CHAT_ATTACHMENT_TOTAL_MAX_CHARS", "120000"))
CHAT_ENGINE_ATTACHMENT_TIMEOUT_SECONDS = float(
    os.getenv("CHAT_ENGINE_ATTACHMENT_TIMEOUT_SECONDS", "300")
)
CHAT_ALLOWED_USERS = os.getenv("CHAT_ALLOWED_USERS", SLACK_OWNER_USER_ID).split(",")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_telegram_users_raw = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
TELEGRAM_ALLOWED_USER_IDS: list[int] = [
    int(uid.strip()) for uid in _telegram_users_raw.split(",") if uid.strip()
]

# Voice (STT + TTS)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
VOICE_STT_MODEL = os.getenv("VOICE_STT_MODEL", "whisper-1")
VOICE_STT_PROVIDERS = os.getenv("VOICE_STT_PROVIDERS", "")
VOICE_STT_ENABLE_OPENAI = os.getenv("VOICE_STT_ENABLE_OPENAI", "")
VOICE_TTS_ENGINE = os.getenv("VOICE_TTS_ENGINE", "edge")  # "edge" or "openai"
VOICE_TTS_VOICE_EDGE = os.getenv("VOICE_TTS_VOICE_EDGE", "en-US-AndrewMultilingualNeural|+14%")
VOICE_TTS_VOICE_OPENAI = os.getenv("VOICE_TTS_VOICE_OPENAI", "alloy")

# Calendar
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "")

# Google Search Console
GSC_SITE_URL = os.getenv("GSC_SITE_URL", "")

# Google Analytics (GA4)
GA4_PROPERTY_ID = os.getenv("GA4_PROPERTY_ID", "")
GA4_REPORTING_TOKEN_FILE = os.getenv("GA4_REPORTING_TOKEN_FILE", "")

# === Extension System ===
# Discovery order: configured paths > bundled repo-local > user-global
EXTENSIONS_EXTRA_PATH = os.getenv("EXTENSIONS_PATH", "")    # additional extension search path
EXTENSIONS_BUNDLED_PATH = str(CLAUDE_DIR / "extensions")     # always searched
EXTENSIONS_ALLOW = os.getenv("EXTENSIONS_ALLOW", "")         # comma-separated, empty = allow all
EXTENSIONS_DENY = os.getenv("EXTENSIONS_DENY", "")           # comma-separated
EXTENSIONS_ENABLED = os.getenv("EXTENSIONS_ENABLED", "true").lower() == "true"

# === Natural-language intent auto-dispatch (Smart Data Queries router path) ===
# When false, natural-language messages never auto-run a data/action command;
# they go straight to the engine. Explicit slash commands are unaffected.
# See .claude/sections/04_smart_data_queries.md.
INTENT_AUTODISPATCH_ENABLED = os.getenv("INTENT_AUTODISPATCH_ENABLED", "true").lower() == "true"

# Circle
CIRCLE_ADMIN_TOKEN = os.getenv("CIRCLE_ADMIN_TOKEN", "")
CIRCLE_HEADLESS_TOKEN = os.getenv("CIRCLE_HEADLESS_TOKEN", "")
CIRCLE_MEMBER_EMAIL = os.getenv("CIRCLE_MEMBER_EMAIL", "")
CIRCLE_COMMUNITY_MEMBER_ID = int(os.getenv("CIRCLE_COMMUNITY_MEMBER_ID") or "0")

# === Drafts & Habits ===
DRAFTS_DIR = MEMORY_DIR / "drafts"
DRAFTS_ACTIVE_DIR = DRAFTS_DIR / "active"
DRAFTS_SENT_DIR = DRAFTS_DIR / "sent"
DRAFTS_EXPIRED_DIR = DRAFTS_DIR / "expired"
HABITS_FILE = MEMORY_DIR / "HABITS.md"
DRAFT_EXPIRY_HOURS = int(os.getenv("DRAFT_EXPIRY_HOURS", "24"))

# === Search Configuration ===
SEARCH_CHUNK_MAX_TOKENS = 400
SEARCH_CHUNK_OVERLAP_TOKENS = 80
SEARCH_VECTOR_WEIGHT = 0.7
SEARCH_KEYWORD_WEIGHT = 0.3
SEARCH_DEFAULT_LIMIT = 10
SEARCH_MIN_SCORE = 0.2

# === Cognition Configuration (Move 1) ===
# Tier gate
TIER1_MAX_QUERIES = int(os.getenv("TIER1_MAX_QUERIES", "3"))
TIER1_MAX_RESULTS = int(os.getenv("TIER1_MAX_RESULTS", "5"))
TIER1_GRAPH_MAX_HOPS = int(os.getenv("TIER1_GRAPH_MAX_HOPS", "1"))
TIER1_GRAPH_MAX_NEIGHBORS = int(os.getenv("TIER1_GRAPH_MAX_NEIGHBORS", "5"))

# Region token budgets (max_tokens — converted to chars via *4 internally)
# Total assembled prompt must fit under ~27K chars (Windows CreateProcess limit).
# ~6500 tokens * 4 = ~26K chars + ~3K overhead = fits under limit.
REGION_BUDGETS = {
    "identity": int(os.getenv("REGION_BUDGET_IDENTITY", "1500")),
    # Issue #484: SAFETY.md hard boundaries (spend ceilings, default-deny
    # surfaces). 700 tokens (2800 chars) fits the largest existing authored
    # SAFETY.md (the main vault's, 2473 bytes) without truncation. BASE
    # reallocation taken ENTIRELY from DURABLE_MEMORY (2000 -> 1300) —
    # like-for-like durable identity content, and (unlike prefetched, empty
    # on light turns) a region whose real content is budget-binding on the
    # main vault, so the freed chars are physical: the real light-turn
    # append was ~1000 chars under the 27K win32 clamp, and the 2473-char
    # SAFETY.md must displace real content, not paper budget.
    #
    # The safety+durable pair sums to the pre-#484 durable 2000 ONLY at
    # DEFAULT (unweighted) budgets. Under mental-process weights the pair
    # diverges (durable scales, e.g. PLANNING x1.5, while `safety` is
    # weight-EXEMPT — pinned at base by
    # cognition.regions.PROCESS_WEIGHT_EXEMPT_REGIONS, because a hard
    # constraint must never be down-weighted by mood). The enforced
    # invariant is therefore NOT net-zero-across-modes; it is: the safety
    # region renders whole, un-truncated, at position 2 (inside the win32
    # head-keep window) in EVERY process mode — proven by physical render
    # tests in tests/test_persona_safety_wiring.py. DEFAULTS only; env
    # override path unchanged.
    "safety": int(os.getenv("REGION_BUDGET_SAFETY", "700")),
    # Living Self Act 1 (M4): SELF_MODEL 400->700, USER_INFERENCES 300->500,
    # PREFETCHED 3000->2500 — net-zero BASE-budget reallocation (-500 +300 +200
    # == 0) so the now-clean SELF.md + operator-belief regions get room while the
    # final 27K win32 clamp guarantees no new overflow. DEFAULTS only; the env
    # override path is unchanged.
    "self_model": int(os.getenv("REGION_BUDGET_SELF_MODEL", "700")),
    "user_model": int(os.getenv("REGION_BUDGET_USER_MODEL", "1000")),
    # #484: 2000 -> 1300 funds the `safety` region above (base-budget pair:
    # safety 700 + durable 1300 == the pre-#484 durable 2000 at DEFAULT;
    # weighted modes diverge — see the safety comment above).
    "durable_memory": int(os.getenv("REGION_BUDGET_MEMORY", "1300")),
    "continuity": int(os.getenv("REGION_BUDGET_CONTINUITY", "500")),
    "recalled_memory": int(os.getenv("REGION_BUDGET_RECALLED", "750")),
    "procedural_memory": int(os.getenv("REGION_BUDGET_PROCEDURAL", "500")),
    "prefetched_context": int(os.getenv("REGION_BUDGET_PREFETCHED", "2500")),
    "user_inferences": int(os.getenv("REGION_BUDGET_USER_INFERENCES", "500")),
    "working_memory": int(os.getenv("REGION_BUDGET_WORKING_MEMORY", "600")),
    # Cofounder v2 Part C — the lean agenda-status region for the default
    # chat (today's line statuses only; absent when no agenda exists). Kept
    # small on purpose: the win32 27k append envelope is nearly full at the
    # existing region caps.
    "portfolio": int(os.getenv("REGION_BUDGET_PORTFOLIO", "200")),
    # Living Self Act 3: the gated cognitive-pass monologue. 500 tokens
    # (~2000 chars) caps a runaway monologue. #172: the monologue no longer
    # renders through assemble_regions (it rides the prompt-suffix transport
    # instead) — engine.py's extraction site applies this same budget via
    # truncate_region directly. Without this row the cap would fall back to
    # DEFAULT_REGION_BUDGETS.get == 1000.
    "internal": int(os.getenv("REGION_BUDGET_INTERNAL_MONOLOGUE", "500")),
    "recent_conversation": int(os.getenv("REGION_BUDGET_RECENT_CONVERSATION", "24000")),
}

RECENT_CONVERSATION_COUNT = int(os.getenv("RECENT_CONVERSATION_COUNT", "80"))
RECENT_CONVERSATION_MESSAGE_MAX_CHARS = int(
    os.getenv("RECENT_CONVERSATION_MESSAGE_MAX_CHARS", "2000")
)

# Staging store
STAGING_STORE_PATH = STATE_DIR / "memory-candidates.jsonl"
AMENDMENT_LEDGER_FILE = STATE_DIR / "amendment-proposals.jsonl"

# Living Self Act 3 — proactive action queue (append-only JSONL, physical state,
# Rule 2). The cognitive pass queues operator_notification proposals here; the
# queue is read fresh each call by ProactiveActionQueue. Dispatch/drain is Act 4.
PROACTIVE_ACTION_QUEUE_FILE = STATE_DIR / "proactive-actions.jsonl"

# Cognitive-pass decision receipts (append-only JSONL). bot.log truncates on
# every restart and the Langfuse span is unreadable when the server is down,
# so this file is the only restart-surviving record of whether the pass fires
# (2026-08-11: zero receipts existed anywhere; the pass was untunable blind).
COGNITIVE_PASS_RECEIPTS_FILE = STATE_DIR / "cognitive-pass-receipts.jsonl"

# Living Self Act 4 — the scheduled evolve loop's belief-decision artifacts land
# here (sibling to the recall harness's reports/ dir). `evolve_loop.py
# propose-belief` writes one decision-<proposal.id>.json per candidate run; the
# recall `propose` subcommand keeps writing to the recall reports dir (it has a
# real ReportDelta). Physical audit trail (Rule 2), NOT a recall ReportDelta.
BELIEF_EVOLVE_DECISION_DIR = DATA_DIR / "evolve" / "belief"
# Bounded auto-apply per scheduled run + Autonomous Amendments section cap (refs #58)
AMENDMENT_APPLY_LIMIT = int(os.getenv("AMENDMENT_APPLY_LIMIT", "3"))
AMENDMENT_SECTION_CAP = int(os.getenv("AMENDMENT_SECTION_CAP", "20"))
COGNITIVE_DRIFT_LEDGER_FILE = STATE_DIR / "cognitive-drift-findings.jsonl"
STAGING_MAX_CAPTURES_PER_TURN = int(os.getenv("STAGING_MAX_CAPTURES", "3"))
STAGING_DECAY_DAYS = int(os.getenv("STAGING_DECAY_DAYS", "30"))

# Auto-capture
CAPTURE_MIN_LENGTH = 10
CAPTURE_MAX_LENGTH = 500

# Self-model file
SELF_FILE = MEMORY_DIR / "SELF.md"

# === Cognition Configuration (Move 2) ===

# Promotion pipeline
PROMOTION_CONFIDENCE_THRESHOLD = float(os.getenv("PROMOTION_CONFIDENCE_MIN", "0.7"))
PROMOTION_EVIDENCE_MINIMUM = int(os.getenv("PROMOTION_EVIDENCE_MIN", "2"))
PROMOTION_SELF_MODEL_EVIDENCE_MINIMUM = int(os.getenv("PROMOTION_SELF_MODEL_EVIDENCE_MIN", "1"))
PROMOTION_STATE_FILE = STATE_DIR / "promotion-state.json"

# Continuity
CONTINUITY_DIR = STATE_DIR / "continuity"
CONTINUITY_MAX_OPEN_LOOPS = int(os.getenv("CONTINUITY_MAX_LOOPS", "5"))
CONTINUITY_MAX_DECISIONS = int(os.getenv("CONTINUITY_MAX_DECISIONS", "5"))
SESSION_TURN_THRESHOLD = int(os.getenv("SESSION_TURN_THRESHOLD", "0"))

# Compaction
COMPACTION_RECOVERY_DIR = STATE_DIR / "compaction-recovery"
COMPACTION_RECOVERY_RETENTION_DAYS = int(os.getenv("COMPACTION_RETENTION_DAYS", "7"))
COMPACTION_FLUSH_TIMEOUT_SECONDS = int(os.getenv("COMPACTION_FLUSH_TIMEOUT", "30"))

# Graph intelligence
MOC_LINK_THRESHOLD = int(os.getenv("MOC_LINK_THRESHOLD", "15"))

# === Cognition Configuration (Move 3) ===

# Mental processes
PROCESS_DETECTION_MIN_LENGTH = int(os.getenv("PROCESS_MIN_LENGTH", "15"))
PROCESS_WEIGHT_MIN = float(os.getenv("PROCESS_WEIGHT_MIN", "0.5"))
PROCESS_WEIGHT_MAX = float(os.getenv("PROCESS_WEIGHT_MAX", "2.0"))

# Skill generation
SKILL_GENERATION_DIR = CLAUDE_DIR / "skills" / "generated"
SKILL_TRIGGER_TOOL_CALLS = int(os.getenv("SKILL_TRIGGER_TOOLS", "5"))
SKILL_INDEX_MAX_ENTRIES = int(os.getenv("SKILL_INDEX_MAX", "20"))

# === Platform Configuration (Move 4) ===

# Discord
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
_discord_guilds_raw = os.getenv("DISCORD_ALLOWED_GUILDS", "")
DISCORD_ALLOWED_GUILDS: list[str] = [
    g.strip() for g in _discord_guilds_raw.split(",") if g.strip()
]
_discord_users_raw = os.getenv("DISCORD_ALLOWED_USERS", "")
DISCORD_ALLOWED_USERS: list[str] = [
    u.strip() for u in _discord_users_raw.split(",") if u.strip()
]
# When true, the bot auto-listens to EVERY channel in its allowed guild(s)
# without needing an @mention. Scope it with DISCORD_ALLOWED_GUILDS.
DISCORD_WATCH_ALL_GUILD_CHANNELS: bool = (
    os.getenv("DISCORD_WATCH_ALL_GUILD_CHANNELS", "").strip().lower()
    in ("1", "true", "yes", "on")
)

# WhatsApp (Meta Cloud API)
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
# Sender allowlist, same shape and semantics as the Telegram/Discord lists:
# empty = accept anyone (the adapter's documented behavior, and what shipped
# before this var existed). Set it and WhatsApp becomes fail-closed — off-list
# senders are refused at ingress AND stamped `viewer` rather than `admin`.
_whatsapp_numbers_raw = os.getenv("WHATSAPP_ALLOWED_NUMBERS", "")
WHATSAPP_ALLOWED_NUMBERS: list[str] = [
    number.strip() for number in _whatsapp_numbers_raw.split(",") if number.strip()
]
# WHATSAPP_WEBHOOK_PORT and HEALTH_CHECK_PORT are profile-aware and resolved
# lazily through ``personas.services`` via the module-level ``__getattr__``
# at the bottom of this file (PRP-7c Phase 3 / R2 NB1).

# Self-model inference tracking
INFERENCE_STATE_FILE = STATE_DIR / "self-model-inferences.json"
INFERENCE_DECAY_DAYS = int(os.getenv("INFERENCE_DECAY_DAYS", "14"))
INFERENCE_CONFIRM_BOOST = float(os.getenv("INFERENCE_CONFIRM_BOOST", "0.1"))
INFERENCE_DECAY_RATE = float(os.getenv("INFERENCE_DECAY_RATE", "0.05"))
INFERENCE_MIN_CONFIDENCE = float(os.getenv("INFERENCE_MIN_CONFIDENCE", "0.3"))
INFERENCE_PROMPT_MIN_CONFIDENCE = float(os.getenv("INFERENCE_PROMPT_MIN_CONFIDENCE", "0.5"))
INFERENCE_PROMPT_CAP = int(os.getenv("INFERENCE_PROMPT_CAP", "10"))

# === Authentication ===
# Claude Agent SDK inherits auth from Claude Code CLI automatically.
# No API key needed - uses credentials stored in ~/.claude/.credentials.json
# Task Scheduler runs as your user, so it has access to your credentials.

# === Heartbeat Configuration ===
HEARTBEAT_INTERVAL_MINUTES = int(os.getenv("HEARTBEAT_INTERVAL_MINUTES", "30"))
HEARTBEAT_ACTIVE_START = os.getenv("HEARTBEAT_ACTIVE_HOURS_START", "08:00")
HEARTBEAT_ACTIVE_END = os.getenv("HEARTBEAT_ACTIVE_HOURS_END", "22:00")
HEARTBEAT_TIMEZONE = os.getenv("HEARTBEAT_TIMEZONE", "America/Chicago")

# === Daily Log Template ===
DAILY_LOG_SECTIONS = ["Sessions", "Heartbeats", "Memory Maintenance"]

# Note: Model is determined by the claude_code system prompt preset
# No need to override - uses your subscription's default model


LOCAL_TZ = ZoneInfo(HEARTBEAT_TIMEZONE)


def now_local() -> datetime:
    """Return the current time in the configured timezone (HEARTBEAT_TIMEZONE)."""
    return datetime.now(LOCAL_TZ)


def get_today_log_path() -> Path:
    """Get path to today's daily log (based on local date)."""
    today = now_local().strftime("%Y-%m-%d")
    return DAILY_DIR / f"{today}.md"


def is_within_active_hours() -> bool:
    """Check if current time is within active hours (local timezone).

    String compare on ``"%H:%M"``, so it CANNOT express a window that crosses
    midnight (``"23:00" <= "01:00"`` is False for every minute of the night).
    Fine for the heartbeat's 08:00-22:00; use ``is_within_waking_window`` for
    any window that wraps.
    """
    current_time = now_local().strftime("%H:%M")
    return HEARTBEAT_ACTIVE_START <= current_time <= HEARTBEAT_ACTIVE_END


def is_within_waking_window(now: datetime | None = None) -> bool:
    """Is the operator awake right now? Handles windows that cross midnight.

    The desk pings the operator directly, so it needs his waking hours, not the
    heartbeat's business hours -- and his window (08:00-02:00) wraps, which the
    string compare above structurally cannot represent.

    A wrapping window is the union of two spans rather than one range: start ->
    midnight, and midnight -> end. Same-day windows keep the ordinary single
    span, so setting an end later than the start behaves exactly as expected.

    Env + ``now`` are both resolved at CALL time (Rule 1). Unparseable bounds
    fail OPEN (awake): a malformed env var must not silently mute the desk,
    because a muted desk is indistinguishable from a quiet one.
    """
    start_raw = os.getenv("DESK_WAKING_START", "08:00").strip() or "08:00"
    end_raw = os.getenv("DESK_WAKING_END", "02:00").strip() or "02:00"

    def _minutes(label: str, value: str) -> int | None:
        try:
            hh, mm = value.split(":", 1)
            h, m = int(hh), int(mm)
        except (ValueError, AttributeError):
            return None
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return h * 60 + m

    start = _minutes("start", start_raw)
    end = _minutes("end", end_raw)
    if start is None or end is None:
        return True  # fail open -- never mute on a typo

    current = now if now is not None else now_local()
    minute = current.hour * 60 + current.minute

    if start == end:
        return True  # a zero-width window is a config mistake; assume always-on
    if start < end:
        return start <= minute < end          # ordinary same-day window
    return minute >= start or minute < end    # wraps midnight


def get_heartbeat_disabled_sources() -> frozenset[str]:
    """Rule-1 call-time resolver for HEARTBEAT_DISABLED_SOURCES.

    Comma-separated source keys (e.g. ``asana,slack``) the operator does not
    use. A disabled source is skipped entirely: no probe, no context section,
    no blocker candidate — so an unused integration cannot generate daily
    token_missing noise (operator confirmed no Asana/Slack, 2026-08-11).
    """

    raw = os.getenv("HEARTBEAT_DISABLED_SOURCES", "")
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


class HeartbeatBlockerSettings(NamedTuple):
    """Effective heartbeat blocker-escalation knobs (call-time resolved)."""

    promote_days: int
    window_days: int
    repromote_days: int
    max_active: int
    promote_allowlist: frozenset[str]


def get_heartbeat_blocker_settings(
    promote_days: int | None = None,
    window_days: int | None = None,
    repromote_days: int | None = None,
    max_active: int | None = None,
    promote_allowlist: str | set[str] | frozenset[str] | None = None,
) -> HeartbeatBlockerSettings:
    """Resolve heartbeat blocker-escalation knobs at CALL TIME (Rule 1).

    Every arg uses the None-sentinel pattern: explicit values pass through;
    ``None`` resolves the matching ``HEARTBEAT_BLOCKER_*`` env var inside the
    body. These knobs deliberately do NOT exist as module-level constants —
    env overrides (and ``monkeypatch.setenv`` in tests) take effect on the
    next call with no module reload and no ``reload_config()`` involvement.

    The allowlist accepts a comma-separated string or an iterable of
    signatures and is returned as a frozenset.
    """
    if promote_days is None:
        promote_days = int(os.getenv("HEARTBEAT_BLOCKER_PROMOTE_DAYS", "3"))
    if window_days is None:
        window_days = int(os.getenv("HEARTBEAT_BLOCKER_WINDOW_DAYS", "7"))
    if repromote_days is None:
        repromote_days = int(os.getenv("HEARTBEAT_BLOCKER_REPROMOTE_DAYS", "3"))
    if max_active is None:
        max_active = int(os.getenv("HEARTBEAT_BLOCKER_MAX_ACTIVE", "3"))
    if promote_allowlist is None:
        promote_allowlist = os.getenv(
            "HEARTBEAT_BLOCKER_PROMOTE_ALLOWLIST",
            "google:oauth_invalid_grant,asana:auth_failed,slack:auth_failed",
        )
    if isinstance(promote_allowlist, str):
        allowlist = frozenset(
            sig.strip() for sig in promote_allowlist.split(",") if sig.strip()
        )
    else:
        allowlist = frozenset(
            str(sig).strip() for sig in promote_allowlist if str(sig).strip()
        )
    return HeartbeatBlockerSettings(
        promote_days=promote_days,
        window_days=window_days,
        repromote_days=repromote_days,
        max_active=max_active,
        promote_allowlist=allowlist,
    )


class HeartbeatObservationSettings(NamedTuple):
    """Effective heartbeat ambient-observation knobs (call-time resolved)."""

    groups: tuple[str, ...]
    max_per_run: int
    busy_day_min: int
    urgent_email_min: int
    unread_min: int
    evening_hour: int
    blocker_min_days: int


def get_heartbeat_observation_settings(
    groups: str | tuple[str, ...] | list[str] | None = None,
    max_per_run: int | None = None,
    busy_day_min: int | None = None,
    urgent_email_min: int | None = None,
    unread_min: int | None = None,
    evening_hour: int | None = None,
    blocker_min_days: int | None = None,
) -> HeartbeatObservationSettings:
    """Resolve heartbeat ambient-observation knobs at CALL TIME (Rule 1).

    Every arg uses the None-sentinel pattern: explicit values pass through;
    ``None`` resolves the matching ``HEARTBEAT_OBSERVATION_*`` env var inside
    the body. These knobs deliberately do NOT exist as module-level constants.

    ``groups`` accepts a comma-separated string or an iterable of group names
    and is returned as an order-preserving lowercased tuple (empties dropped).
    The default is the locked 2026-06-12 operator decision — ALL groups on,
    including ``blockers``. The env knob is narrowing/kill-switch only: an
    empty string disables ambient observations entirely.

    The living_memory-side knobs (``HEARTBEAT_OBSERVATION_CAP`` /
    ``HEARTBEAT_OBSERVATION_DEDUP_DAYS`` / ``HEARTBEAT_OBSERVATION_AGE_DAYS``)
    are deliberately NOT in this resolver — they body-resolve inside
    ``living_memory`` (ownership split, no duplicated resolution).
    """
    if groups is None:
        groups = os.getenv(
            "HEARTBEAT_OBSERVATION_GROUPS",
            "calendar,email,finance,tasks,community,blockers",
        )
    if isinstance(groups, str):
        parsed_groups = tuple(
            g.strip().lower() for g in groups.split(",") if g.strip()
        )
    else:
        parsed_groups = tuple(
            str(g).strip().lower() for g in groups if str(g).strip()
        )
    if max_per_run is None:
        max_per_run = int(os.getenv("HEARTBEAT_OBSERVATION_MAX_PER_RUN", "3"))
    if busy_day_min is None:
        busy_day_min = int(os.getenv("HEARTBEAT_OBSERVATION_BUSY_DAY_MIN", "5"))
    if urgent_email_min is None:
        urgent_email_min = int(
            os.getenv("HEARTBEAT_OBSERVATION_URGENT_EMAIL_MIN", "1")
        )
    if unread_min is None:
        unread_min = int(os.getenv("HEARTBEAT_OBSERVATION_UNREAD_MIN", "50"))
    if evening_hour is None:
        evening_hour = int(os.getenv("HEARTBEAT_OBSERVATION_EVENING_HOUR", "18"))
    if blocker_min_days is None:
        blocker_min_days = int(
            os.getenv("HEARTBEAT_OBSERVATION_BLOCKER_MIN_DAYS", "2")
        )
    return HeartbeatObservationSettings(
        groups=parsed_groups,
        max_per_run=max_per_run,
        busy_day_min=busy_day_min,
        urgent_email_min=urgent_email_min,
        unread_min=unread_min,
        evening_hour=evening_hour,
        blocker_min_days=blocker_min_days,
    )


class EpisodeSettings(NamedTuple):
    """Effective episode writer/dream-digest knobs (call-time resolved)."""

    min_chars: int
    max_per_day: int
    dream_max_files: int
    dream_max_chars_per: int
    dream_max_total_chars: int


def get_episode_settings(
    min_chars: int | None = None,
    max_per_day: int | None = None,
    dream_max_files: int | None = None,
    dream_max_chars_per: int | None = None,
    dream_max_total_chars: int | None = None,
) -> EpisodeSettings:
    """Resolve episode knobs at CALL TIME (Rule 1) — Living Mind Act 3.

    Every arg uses the None-sentinel pattern: explicit values pass through;
    ``None`` resolves the matching ``EPISODE_*`` env var inside the body.
    These knobs deliberately do NOT exist as module-level constants — env
    overrides (and ``monkeypatch.setenv`` in tests) take effect on the next
    call with no module reload.

    Knobs:
        EPISODE_MIN_CHARS (80) — minimum parsed-body chars for a NEW episode.
        EPISODE_MAX_PER_DAY (20) — cap on NEW episode files per lifecycle-date
            (counted against physical ``episodes/{date}-*.md`` files, Rule 2);
            same-key updates are exempt.
        EPISODE_DREAM_MAX_FILES (10) — newest-first cap on episodes fed to
            the dream consolidate phase.
        EPISODE_DREAM_MAX_CHARS_PER (600) — per-episode digest excerpt cap.
        EPISODE_DREAM_MAX_TOTAL_CHARS (4000) — total digest cap.
    """
    if min_chars is None:
        min_chars = int(os.getenv("EPISODE_MIN_CHARS", "80"))
    if max_per_day is None:
        max_per_day = int(os.getenv("EPISODE_MAX_PER_DAY", "20"))
    if dream_max_files is None:
        dream_max_files = int(os.getenv("EPISODE_DREAM_MAX_FILES", "10"))
    if dream_max_chars_per is None:
        dream_max_chars_per = int(os.getenv("EPISODE_DREAM_MAX_CHARS_PER", "600"))
    if dream_max_total_chars is None:
        dream_max_total_chars = int(
            os.getenv("EPISODE_DREAM_MAX_TOTAL_CHARS", "4000")
        )
    return EpisodeSettings(
        min_chars=min_chars,
        max_per_day=max_per_day,
        dream_max_files=dream_max_files,
        dream_max_chars_per=dream_max_chars_per,
        dream_max_total_chars=dream_max_total_chars,
    )


class BotLivenessSettings(NamedTuple):
    """Effective in-bot adapter-liveness knobs (call-time resolved)."""

    enabled: bool
    interval_seconds: int
    probe_timeout_seconds: float
    failure_threshold: int
    reconnect_attempts: int
    fail_fast: bool
    startup_grace_seconds: float
    diagnostics_ttl_seconds: float
    warmup_seconds: float


def get_bot_liveness_settings(
    enabled: bool | None = None,
    interval_seconds: int | None = None,
    probe_timeout_seconds: float | None = None,
    failure_threshold: int | None = None,
    reconnect_attempts: int | None = None,
    fail_fast: bool | None = None,
    startup_grace_seconds: float | None = None,
    diagnostics_ttl_seconds: float | None = None,
    warmup_seconds: float | None = None,
) -> BotLivenessSettings:
    """Resolve adapter-liveness knobs at CALL TIME (Rule 1).

    Every arg uses the None-sentinel pattern: explicit values pass through;
    ``None`` resolves the matching ``BOT_LIVENESS_*`` / ``BOT_HEALTH_*`` env
    var inside the body. These knobs deliberately do NOT exist as module-level
    constants so env overrides (and ``monkeypatch.setenv`` in tests) take
    effect on the next call with no module reload.

    Knobs:
        BOT_LIVENESS_ENABLED (true) — master switch for the probe loop.
        BOT_LIVENESS_INTERVAL_SECONDS (60) — seconds between probe rounds.
        BOT_LIVENESS_PROBE_TIMEOUT_SECONDS (10) — hard cap per adapter probe;
            a hung probe MUST NOT wedge the supervisor that watches for wedges.
        BOT_LIVENESS_FAILURE_THRESHOLD (3) — consecutive failed probes before
            an adapter is declared unhealthy (rides out transient API blips).
        BOT_LIVENESS_RECONNECT_ATTEMPTS (1) — in-process reconnects tried
            before fail-fast.
        BOT_LIVENESS_FAIL_FAST (true) — exit non-zero when reconnect fails, so
            the external watchdog / service supervisor restarts a clean process.
            Safe ONLY because bot_watchdog.py restarts an unreachable bot.
        BOT_LIVENESS_STARTUP_GRACE_SECONDS (60) — window in which an adapter that
            has not finished connect() yet is skipped rather than counted as
            dead. The supervisor and the router start concurrently; without this
            the first probe races adapter connect. Past the window a
            never-connected adapter IS counted as a failure.
        BOT_HEALTH_DIAGNOSTICS_TTL_SECONDS (30) — age at which the cached
            diagnostics snapshot is refreshed OFF the /health request path.
        BOT_HEALTH_WARMUP_SECONDS (90) — uptime below which a bot with no
            diagnostics snapshot yet reports ``status: "warming"``.
    """
    if enabled is None:
        enabled = os.getenv("BOT_LIVENESS_ENABLED", "true").lower() == "true"
    if interval_seconds is None:
        interval_seconds = int(os.getenv("BOT_LIVENESS_INTERVAL_SECONDS", "60"))
    if probe_timeout_seconds is None:
        probe_timeout_seconds = float(
            os.getenv("BOT_LIVENESS_PROBE_TIMEOUT_SECONDS", "10")
        )
    if failure_threshold is None:
        failure_threshold = int(os.getenv("BOT_LIVENESS_FAILURE_THRESHOLD", "3"))
    if reconnect_attempts is None:
        reconnect_attempts = int(os.getenv("BOT_LIVENESS_RECONNECT_ATTEMPTS", "1"))
    if fail_fast is None:
        fail_fast = os.getenv("BOT_LIVENESS_FAIL_FAST", "true").lower() == "true"
    if startup_grace_seconds is None:
        startup_grace_seconds = float(
            os.getenv("BOT_LIVENESS_STARTUP_GRACE_SECONDS", "60")
        )
    if diagnostics_ttl_seconds is None:
        diagnostics_ttl_seconds = float(
            os.getenv("BOT_HEALTH_DIAGNOSTICS_TTL_SECONDS", "30")
        )
    if warmup_seconds is None:
        warmup_seconds = float(os.getenv("BOT_HEALTH_WARMUP_SECONDS", "90"))
    return BotLivenessSettings(
        enabled=enabled,
        interval_seconds=interval_seconds,
        probe_timeout_seconds=probe_timeout_seconds,
        failure_threshold=failure_threshold,
        reconnect_attempts=reconnect_attempts,
        fail_fast=fail_fast,
        startup_grace_seconds=startup_grace_seconds,
        diagnostics_ttl_seconds=diagnostics_ttl_seconds,
        warmup_seconds=warmup_seconds,
    )


class BotWatchdogSettings(NamedTuple):
    """Effective external-watchdog knobs (call-time resolved)."""

    enabled: bool
    health_url: str
    timeout_seconds: float
    failure_threshold: int
    max_restarts_per_hour: int
    grace_seconds: float
    staleness_seconds: float = 7200.0


def get_bot_watchdog_settings(
    enabled: bool | None = None,
    health_url: str | None = None,
    timeout_seconds: float | None = None,
    failure_threshold: int | None = None,
    max_restarts_per_hour: int | None = None,
    grace_seconds: float | None = None,
    staleness_seconds: float | None = None,
) -> BotWatchdogSettings:
    """Resolve external-watchdog knobs at CALL TIME (Rule 1).

    ``health_url`` defaults to the ACTIVE profile's health port resolved through
    the module ``__getattr__`` (never a module-level constant — a profile swap
    must move the watchdog's target with it).

    Knobs:
        BOT_WATCHDOG_ENABLED (true) — master switch; false makes every poll a
            no-op report so the scheduled task can stay registered.
        BOT_WATCHDOG_HEALTH_URL (http://127.0.0.1:{HEALTH_CHECK_PORT}/health)
        BOT_WATCHDOG_TIMEOUT_SECONDS (10) — HTTP timeout. A /health that cannot
            answer inside this window counts as UNREACHABLE (the pre-fix bot
            blocked its own event loop for ~3.4s per request).
        BOT_WATCHDOG_FAILURE_THRESHOLD (2) — consecutive bad polls before a
            restart fires. Counted across ``--once`` runs via the state file.
        BOT_WATCHDOG_MAX_RESTARTS_PER_HOUR (5) — rolling-hour restart budget;
            exhausting it notifies the operator instead of looping.
        BOT_WATCHDOG_GRACE_SECONDS (300) — post-restart quiet window, and the
            uptime beyond which a still-"warming" bot counts as wedged.
        BOT_WATCHDOG_STALENESS_SECONDS (7200) — a critical adapter whose
            last_update_at is older than this while ANOTHER adapter is fresh
            counts as event-stale (degraded). Both-quiet is NOT stale.
    """
    if enabled is None:
        enabled = os.getenv("BOT_WATCHDOG_ENABLED", "true").lower() == "true"
    if health_url is None:
        health_url = os.getenv("BOT_WATCHDOG_HEALTH_URL", "").strip()
        if not health_url:
            # Same resolver the module ``__getattr__`` uses for HEALTH_CHECK_PORT.
            # Called directly (not via the bare global) because PEP 562 module
            # __getattr__ does NOT fire for global-name lookup inside this module,
            # and imported inside the body so a profile swap moves the target.
            from personas.services import get_health_check_port

            health_url = f"http://127.0.0.1:{get_health_check_port()}/health"
    if timeout_seconds is None:
        timeout_seconds = float(os.getenv("BOT_WATCHDOG_TIMEOUT_SECONDS", "10"))
    if failure_threshold is None:
        failure_threshold = int(os.getenv("BOT_WATCHDOG_FAILURE_THRESHOLD", "2"))
    if max_restarts_per_hour is None:
        max_restarts_per_hour = int(
            os.getenv("BOT_WATCHDOG_MAX_RESTARTS_PER_HOUR", "5")
        )
    if grace_seconds is None:
        grace_seconds = float(os.getenv("BOT_WATCHDOG_GRACE_SECONDS", "300"))
    if staleness_seconds is None:
        staleness_seconds = float(os.getenv("BOT_WATCHDOG_STALENESS_SECONDS", "7200"))
    return BotWatchdogSettings(
        enabled=enabled,
        health_url=health_url,
        timeout_seconds=timeout_seconds,
        failure_threshold=failure_threshold,
        max_restarts_per_hour=max_restarts_per_hour,
        grace_seconds=grace_seconds,
        staleness_seconds=staleness_seconds,
    )


BOT_WATCHDOG_STATE_FILE = STATE_DIR / "bot-watchdog-state.json"


class BotAutostartSettings(NamedTuple):
    """Effective bot-autostart knobs (call-time resolved)."""

    task_name: str
    timeout_seconds: float


def get_bot_autostart_settings(
    task_name: str | None = None,
    timeout_seconds: float | None = None,
) -> BotAutostartSettings:
    """Resolve bot-autostart knobs at CALL TIME (Rule 1).

    Knobs:
        BOT_AUTOSTART_TASK_NAME (SecondBrain-BotStart) — the Windows Task
            Scheduler task name the toggle registers/unregisters. The
            enabled/disabled state itself is NEVER stored here — it is read
            from the physical OS task registry (Rule 2).
        BOT_AUTOSTART_TIMEOUT_SECONDS (60) — subprocess timeout for the
            schtasks/PowerShell calls.
    """
    if task_name is None:
        task_name = os.getenv("BOT_AUTOSTART_TASK_NAME", "SecondBrain-BotStart").strip()
    if timeout_seconds is None:
        timeout_seconds = float(os.getenv("BOT_AUTOSTART_TIMEOUT_SECONDS", "60"))
    return BotAutostartSettings(
        task_name=task_name,
        timeout_seconds=timeout_seconds,
    )


class InferenceExtractionSettings(NamedTuple):
    """Effective operator-belief extraction + dedup knobs (call-time resolved)."""

    dedup_threshold: float
    extraction_enabled: bool
    max_claims: int
    min_chars: int
    write_time_contradiction: bool  # WS3 #84 — opt-in write-time contradiction step (default OFF)


def get_inference_extraction_settings(
    dedup_threshold: float | None = None,
    extraction_enabled: bool | None = None,
    max_claims: int | None = None,
    min_chars: int | None = None,
    write_time_contradiction: bool | None = None,
) -> InferenceExtractionSettings:
    """Resolve operator-belief extraction knobs at CALL TIME (Rule 1) — Living Self Act 1.

    Every arg uses the None-sentinel pattern: explicit values pass through;
    ``None`` resolves the matching ``INFERENCE_*`` env var inside the body.
    None of these values become import-time globals — env overrides (and
    ``monkeypatch.setenv`` in tests) take effect on the next call with no
    module reload. ``_cosine_similar`` and ``extract_operator_beliefs`` read
    this resolver at call time.

    Knobs:
        INFERENCE_DEDUP_THRESHOLD (0.72) — cosine threshold above which a fresh
            belief strengthens an existing record instead of inserting a new one.
            0.72 sits in the EMPIRICALLY-MEASURED BGE-base-en-v1.5 gap for this
            corpus's short belief phrasings (measured this session against the
            live model): paraphrase pairs land 0.759-0.900 (e.g. "prefers concise
            answers" / "likes short replies" == 0.787; "wants dark mode" /
            "prefers a dark theme" == 0.900) while distinct-but-topical beliefs
            land 0.532-0.660 (e.g. "prefers concise answers" / "prefers dark
            mode" == 0.614). 0.72 is above every observed distinct pair (max
            0.660) and below every observed paraphrase pair (min 0.759), so it
            converges real paraphrases without fusing distinct beliefs. (The
            PRP's pre-build 0.82 estimate assumed a 0.85-0.95 paraphrase band
            that the real model does NOT produce for these short phrasings — 0.82
            would have left most paraphrases un-merged. The value stays a Rule-1
            knob so it is tunable without a code change.) Conservative-by-default:
            when in doubt, DON'T merge — a missed merge costs one slow
            convergence; a wrong merge fuses two real beliefs.
        INFERENCE_EXTRACTION_ENABLED ("true") — kill switch for the reflection
            operator-belief extractor.
        INFERENCE_EXTRACTION_MAX_CLAIMS (8) — cap on claims emitted per
            reflection run.
        INFERENCE_EXTRACTION_MIN_CHARS (12) — floor on a single claim's length.
        INFERENCE_WRITE_TIME_CONTRADICTION ("false") — WS3 #84 opt-in. When ON,
            a newly-WRITTEN operator belief that lands topically-near an existing
            ACTIVE belief (cosine in the conflict band) is resolved against it
            IMMEDIATELY at write — reusing the EXACT nightly judge/policy
            (``belief_conflicts.judge_contradictions`` + ``apply_contradictions``)
            — instead of waiting for the 8 AM pass. DEFAULT OFF keeps the written
            corpus byte-identical and fires zero judge calls; the nightly
            ``belief_conflicts`` pass remains the backstop. NOTE: this is a
            write-time-only opt-in stacked ON TOP of ``CONTRADICTION_ENABLED`` —
            ``CONTRADICTION_ENABLED=false`` is a SECOND kill switch that also
            disables the write-time step (the shared ``get_contradiction_settings``
            ``.enabled`` gate short-circuits the reused primitives).
    """
    if dedup_threshold is None:
        dedup_threshold = float(os.getenv("INFERENCE_DEDUP_THRESHOLD", "0.72"))
    if extraction_enabled is None:
        extraction_enabled = (
            os.getenv("INFERENCE_EXTRACTION_ENABLED", "true").lower() == "true"
        )
    if max_claims is None:
        max_claims = int(os.getenv("INFERENCE_EXTRACTION_MAX_CLAIMS", "8"))
    if min_chars is None:
        min_chars = int(os.getenv("INFERENCE_EXTRACTION_MIN_CHARS", "12"))
    if write_time_contradiction is None:
        write_time_contradiction = (
            os.getenv("INFERENCE_WRITE_TIME_CONTRADICTION", "false").lower() == "true"
        )
    return InferenceExtractionSettings(
        dedup_threshold=dedup_threshold,
        extraction_enabled=extraction_enabled,
        max_claims=max_claims,
        min_chars=min_chars,
        write_time_contradiction=write_time_contradiction,
    )


class EntityGuardrailSettings(NamedTuple):
    """Effective link-economy guardrail knobs (call-time resolved) — Karpathy port."""

    enabled: bool
    page_min_mentions: int
    edit_ceiling: int
    link_cap: int


def get_entity_guardrail_settings(
    enabled: bool | None = None,
    page_min_mentions: int | None = None,
    edit_ceiling: int | None = None,
    link_cap: int | None = None,
) -> EntityGuardrailSettings:
    """Resolve entity-compilation link-economy guardrail knobs at CALL TIME (Rule 1).

    Every arg uses the None-sentinel pattern: explicit values pass through;
    ``None`` resolves the matching ``ENTITY_*`` env var inside the body, so env
    overrides (and ``monkeypatch.setenv`` in tests) take effect on the next call
    with no module reload. ``entity_extractor.compile_entities`` reads this
    resolver at call time.

    Knobs (all DEFAULT-OFF / conservative — the scheduled compile + full-vault
    lint pipelines stay byte-identical until an operator flips
    ``ENTITY_GUARDRAILS_ENABLED``):
        ENTITY_GUARDRAILS_ENABLED ("false") — master switch for the ≥N-mention
            create gate, the per-run edit ceiling, and the per-page link cap.
        ENTITY_PAGE_MIN_MENTIONS (2) — distinct sources that must mention an
            entity before its concept page is created (staged in the mention
            ledger until then).
        ENTITY_EDIT_CEILING (5) — max concept-page WRITES per compile run;
            further updates are skipped (the page + its link stay valid).
        ENTITY_LINK_CAP (8) — max ``related:`` graph edges per concept page and
            per source note.
    """
    if enabled is None:
        enabled = os.getenv("ENTITY_GUARDRAILS_ENABLED", "false").lower() == "true"
    if page_min_mentions is None:
        page_min_mentions = int(os.getenv("ENTITY_PAGE_MIN_MENTIONS", "2"))
    if edit_ceiling is None:
        edit_ceiling = int(os.getenv("ENTITY_EDIT_CEILING", "5"))
    if link_cap is None:
        link_cap = int(os.getenv("ENTITY_LINK_CAP", "8"))
    return EntityGuardrailSettings(
        enabled=enabled,
        page_min_mentions=page_min_mentions,
        edit_ceiling=edit_ceiling,
        link_cap=link_cap,
    )


def get_lint_delta_enabled(enabled: bool | None = None) -> bool:
    """Resolve the ``LINT_DELTA_ENABLED`` knob at CALL TIME (Rule 1).

    ``None`` resolves ``LINT_DELTA_ENABLED`` ("false") inside the body so env
    overrides take effect with no module reload. ``vault_lint.run_lint`` reads
    this (lazily, with an ``os.getenv`` fallback for dependency-light subprocess
    callers) to decide whether to run the incremental delta path.
    """
    if enabled is None:
        enabled = os.getenv("LINT_DELTA_ENABLED", "false").lower() == "true"
    return enabled


class ContradictionSettings(NamedTuple):
    """Effective belief-contradiction knobs (call-time resolved) — Living Self Act 2."""

    enabled: bool
    pair_min_cosine: float
    pair_max_cosine: float  # defaults to the dedup threshold when env unset (coupling)
    max_pairs: int  # cap on pairs sent to the JUDGE
    max_eligible: int  # cap on eligible records BEFORE the upper-triangle (M3)
    min_records: int
    allow_explicit_vs_explicit: bool  # B1 gate; default false


def get_contradiction_settings(
    enabled: bool | None = None,
    pair_min_cosine: float | None = None,
    pair_max_cosine: float | None = None,
    max_pairs: int | None = None,
    max_eligible: int | None = None,
    min_records: int | None = None,
    allow_explicit_vs_explicit: bool | None = None,
) -> ContradictionSettings:
    """Resolve belief-contradiction knobs at CALL TIME (Rule 1) — Living Self Act 2.

    Mirrors ``get_inference_extraction_settings``: every arg uses the
    None-sentinel pattern (explicit values pass through; ``None`` resolves the
    matching ``CONTRADICTION_*`` env var inside the body), bool knobs via
    ``.lower() == "true"``. NONE of these become import-time globals — env
    overrides and ``monkeypatch.setenv`` take effect on the next call with no
    module reload. ``belief_conflicts`` reads this resolver at call time.

    Knobs:
        CONTRADICTION_ENABLED ("true") — kill switch for the whole pass.
        CONTRADICTION_PAIR_MIN_COSINE (0.45) — lower bound; below this two
            beliefs are unrelated.
            MEASURED THIS SESSION against the live BGE-base-en-v1.5 model
            (G3 closure — the opposed-valence band was the one Act-1 value left
            unmeasured). Opposed-valence belief pairs that SURVIVE Act-1 dedup as
            two distinct records (cosine < the 0.72 dedup threshold) land
            0.664-0.691 ("ship lean" / "build enterprise" == 0.664; "want
            frequent check-ins" / "want to be left alone" == 0.680; "move fast
            and iterate" / "prefer careful upfront planning" == 0.691).
            Distinct-but-topical (non-opposed) beliefs land 0.523-0.649 and
            unrelated beliefs land 0.387-0.452. A 0.45 floor admits every
            surviving-opposed pair AND the distinct-topical band while excluding
            unrelated noise — so the LLM judge sees the real candidates and is
            spared the obviously-unrelated. (The KEY structural finding: opposed
            pairs with cosine >= 0.72 — "prefers concise" / "wants verbose" ==
            0.746; "prefers dark mode" / "prefers light mode" == 0.931; "trusts"
            / "distrusts automated tests" == 0.869 — are MERGED by Act-1 dedup
            into ONE record on ingest, so they can never reach the judge as two
            records. The engine's window is therefore exactly "survived dedup" =
            [pair_min_cosine, dedup_threshold), and 0.45 is below the weakest
            surviving-opposed pair (0.664).) Stays a Rule-1 knob so it can be
            lowered without a code change if a real opposed pair ever lands below
            it.
        CONTRADICTION_PAIR_MAX_COSINE (= the dedup threshold, 0.72) — upper
            bound: at/above the dedup threshold the pair was ALREADY merged into
            one record by Act-1 dedup (measured: every opposed pair >= 0.72 is a
            single record on ingest), so no two-record conflict can live there.
            COUPLED to ``get_inference_extraction_settings().dedup_threshold`` by
            default (resolved INSIDE the body at call time so the band and the
            merge boundary never drift), but it is its own env-overridable knob.
        CONTRADICTION_MAX_PAIRS (20) — cap on pairs sent to the JUDGE per
            reflection.
        CONTRADICTION_MAX_ELIGIBLE (100) — cap on the eligible set (recency desc,
            then confidence desc) BEFORE the O(N^2) upper-triangle (M3), so the
            pair build stays bounded (<=4,950 dot-products/night at the cap) as
            the corpus grows over months.
        CONTRADICTION_MIN_RECORDS (2) — floor: <2 eligible records -> nothing to
            compare.
        CONTRADICTION_ALLOW_EXPLICIT_VS_EXPLICIT ("false") — B1 gate. Default
            OFF: an explicit<->explicit conflict is HELD on both (no drop),
            surfaced for operator resolution; only the operator may flip it on (a
            deliberate audited choice). DEFAULT never lowers an operator-stated
            belief.
    """
    if enabled is None:
        enabled = os.getenv("CONTRADICTION_ENABLED", "true").lower() == "true"
    if pair_min_cosine is None:
        pair_min_cosine = float(os.getenv("CONTRADICTION_PAIR_MIN_COSINE", "0.45"))
    if pair_max_cosine is None:
        env_max = os.getenv("CONTRADICTION_PAIR_MAX_COSINE")
        if env_max is not None:
            pair_max_cosine = float(env_max)
        else:
            # Coupling (Rule 1 honored): call-time read of the dedup threshold so
            # the candidate band's upper bound tracks the merge boundary. NOT a
            # module-level constant.
            pair_max_cosine = get_inference_extraction_settings().dedup_threshold
    if max_pairs is None:
        max_pairs = int(os.getenv("CONTRADICTION_MAX_PAIRS", "20"))
    if max_eligible is None:
        max_eligible = int(os.getenv("CONTRADICTION_MAX_ELIGIBLE", "100"))
    if min_records is None:
        min_records = int(os.getenv("CONTRADICTION_MIN_RECORDS", "2"))
    if allow_explicit_vs_explicit is None:
        allow_explicit_vs_explicit = (
            os.getenv("CONTRADICTION_ALLOW_EXPLICIT_VS_EXPLICIT", "false").lower()
            == "true"
        )
    return ContradictionSettings(
        enabled=enabled,
        pair_min_cosine=pair_min_cosine,
        pair_max_cosine=pair_max_cosine,
        max_pairs=max_pairs,
        max_eligible=max_eligible,
        min_records=min_records,
        allow_explicit_vs_explicit=allow_explicit_vs_explicit,
    )


class BeliefEvolveSettings(NamedTuple):
    """Effective belief-evolve knobs (call-time resolved) — Living Self Act 4."""

    enabled: bool  # kill switch for the whole evolve loop (both subcommands)
    min_supporting_paths: int  # cited paths that must CONFINE + EXIST + be non-empty
    min_overlap: float  # deterministic token-overlap floor for support
    max_bytes: int  # M4 read bound: oversized -> non-supporting; reads capped to this
    min_correctness: float  # judge correctness floor for adoption
    min_fidelity: float  # judge evidence-fidelity floor for adoption
    corpus_path: str | None  # None -> evolve/belief_regression_corpus.json sibling
    max_attempts: int  # retry-budget cap per candidate (nightly dream Phase 5)
    max_adoptions_per_night: int  # adoption throttle per nightly run
    max_candidates_per_night: int  # cap on FRESH LLM-authored candidates per run
    candidate_min_confidence: float  # nightly-candidate confidence hint (advisory)


def get_belief_evolve_settings(
    enabled: bool | None = None,
    min_supporting_paths: int | None = None,
    min_overlap: float | None = None,
    max_bytes: int | None = None,
    min_correctness: float | None = None,
    min_fidelity: float | None = None,
    corpus_path: str | None = None,
    max_attempts: int | None = None,
    max_adoptions_per_night: int | None = None,
    max_candidates_per_night: int | None = None,
    candidate_min_confidence: float | None = None,
) -> BeliefEvolveSettings:
    """Resolve belief-evolve knobs at CALL TIME (Rule 1) — Living Self Act 4.

    Mirrors ``get_contradiction_settings`` / ``get_cognitive_pass_settings``:
    every arg uses the None-sentinel pattern (explicit values pass through;
    ``None`` resolves the matching ``EVOLVE_*`` / ``BELIEF_*`` env var inside the
    body), bool knobs via ``.lower() == "true"``, floats via ``float(...)``, ints
    via ``int(...)``. NONE of these become import-time globals — env overrides and
    ``monkeypatch.setenv`` take effect on the NEXT call with no module reload.
    ``evolve_loop`` / ``evidence_gate`` / ``judge`` read this resolver at call time.

    Knobs:
        EVOLVE_ENABLED ("true") — kill switch for the whole evolve loop. Checked
            at the ENTRYPOINT of BOTH ``propose`` and ``propose_belief``: disabled
            -> write NO artifact, mutate NOTHING, exit cleanly with a visible
            print (mirrors the ``settings.enabled`` early-return in
            ``judge_contradictions``).
        BELIEF_EVIDENCE_MIN_SUPPORTING_PATHS (1) — min cited paths that must
            CONFINE under a trusted root + EXIST + be non-empty for support.
        BELIEF_EVIDENCE_MIN_OVERLAP (0.10, float) — deterministic token-overlap
            floor for support (the cheap NECESSARY pre-filter; M2 — measures
            shared VOCABULARY, NOT genuine support; the LLM judge is the
            sufficient support-decider, this is the cheapest of three layers).
        BELIEF_EVIDENCE_MAX_BYTES (524288, int — 512 KiB) — M4 read bound: a
            cited evidence file larger than this is treated as non-supporting (no
            read); reads are capped to this many bytes even from an in-range file,
            and the cap is re-applied to any injected ``read_text`` return (the
            fake reader bypasses ``stat``). Bounds the arbitrary-file-read / OOM /
            judge-prompt-injection surface.
        BELIEF_JUDGE_MIN_CORRECTNESS (0.6, float) — judge correctness floor for
            adoption (the scheduled LLM judge, never the hot path).
        BELIEF_JUDGE_MIN_FIDELITY (0.6, float) — judge evidence-fidelity floor
            for adoption.
        BELIEF_REGRESSION_CORPUS_PATH (None -> the sibling
            ``evolve/belief_regression_corpus.json``) — Rule-2 path to the
            deterministic falsifiable-check corpus (data, extendable without a
            code change).
        BELIEF_MAX_ATTEMPTS (3, int) — retry-budget cap per candidate. A candidate
            whose ``attempts`` reaches this on a nightly dream Phase-5 run is
            downgraded to terminal (``retryable=False``,
            ``outcome_reason="retry_budget_exhausted"``) instead of being re-judged
            forever after a transient judge-provider outage.
        BELIEF_MAX_ADOPTIONS_PER_NIGHT (2, int) — adoption throttle. Phase 5 stops
            processing candidates once this many are adopted in a single run;
            remaining candidates wait for the next night (never judged this run).
        BELIEF_MAX_CANDIDATES_PER_NIGHT (3, int) — cap on FRESH LLM-authored
            candidates parsed from the consolidation response per run (the retry
            queue is NOT bounded by this — ``max_attempts`` bounds that instead).
        BELIEF_CANDIDATE_MIN_CONFIDENCE (0.75, float) — advisory confidence hint
            surfaced in the nightly consolidation prompt for identity-grade
            candidates (the UNCHANGED apply-time 0.75 policy gate is the real floor).
    """
    if enabled is None:
        enabled = os.getenv("EVOLVE_ENABLED", "true").lower() == "true"
    if min_supporting_paths is None:
        min_supporting_paths = int(
            os.getenv("BELIEF_EVIDENCE_MIN_SUPPORTING_PATHS", "1")
        )
    if min_overlap is None:
        min_overlap = float(os.getenv("BELIEF_EVIDENCE_MIN_OVERLAP", "0.10"))
    if max_bytes is None:
        max_bytes = int(os.getenv("BELIEF_EVIDENCE_MAX_BYTES", "524288"))
    if min_correctness is None:
        min_correctness = float(os.getenv("BELIEF_JUDGE_MIN_CORRECTNESS", "0.6"))
    if min_fidelity is None:
        min_fidelity = float(os.getenv("BELIEF_JUDGE_MIN_FIDELITY", "0.6"))
    if corpus_path is None:
        env_corpus = os.getenv("BELIEF_REGRESSION_CORPUS_PATH")
        corpus_path = env_corpus if env_corpus else None
    if max_attempts is None:
        max_attempts = int(os.getenv("BELIEF_MAX_ATTEMPTS", "3"))
    if max_adoptions_per_night is None:
        max_adoptions_per_night = int(os.getenv("BELIEF_MAX_ADOPTIONS_PER_NIGHT", "2"))
    if max_candidates_per_night is None:
        max_candidates_per_night = int(os.getenv("BELIEF_MAX_CANDIDATES_PER_NIGHT", "3"))
    if candidate_min_confidence is None:
        candidate_min_confidence = float(
            os.getenv("BELIEF_CANDIDATE_MIN_CONFIDENCE", "0.75")
        )
    return BeliefEvolveSettings(
        enabled=enabled,
        min_supporting_paths=min_supporting_paths,
        min_overlap=min_overlap,
        max_bytes=max_bytes,
        min_correctness=min_correctness,
        min_fidelity=min_fidelity,
        corpus_path=corpus_path,
        max_attempts=max_attempts,
        max_adoptions_per_night=max_adoptions_per_night,
        max_candidates_per_night=max_candidates_per_night,
        candidate_min_confidence=candidate_min_confidence,
    )


class CalledShotsSettings(NamedTuple):
    """Effective called-shots knobs (call-time resolved) — epic #186 T1."""

    enabled: bool  # feature soft-toggle (the kill-switch is the hard gate)
    db_path: str  # SQLite ledger file (own DB — WAL, single writer)
    stale_age_days: int  # T3 sweep: open shots older than this are stale
    mirror_enabled: bool  # write the human-readable vault mirror note per shot
    mirror_dir: str  # vault dir for mirror notes (derived state, never truth)


def get_called_shots_settings(
    enabled: bool | None = None,
    db_path: str | None = None,
    stale_age_days: int | None = None,
    mirror_enabled: bool | None = None,
    mirror_dir: str | None = None,
) -> CalledShotsSettings:
    """Resolve called-shots knobs at CALL TIME (Rule 1) — epic #186 T1.

    Mirrors ``get_belief_evolve_settings``: None-sentinel args resolve the
    matching ``CALLED_SHOTS_*`` env var inside the body, so env overrides and
    ``monkeypatch.setenv`` take effect on the NEXT call with no module reload.

    Knobs:
        CALLED_SHOTS_ENABLED ("true") — soft toggle for the AUTONOMOUS EMISSION
            surfaces only (T2's challenge, T3's stale-nag + callback injection).
            Operator-initiated reconcile/track_record/list_open ride the
            kill-switch ONLY — soft-OFF must never strand open shots the
            operator can't settle. The HARD gate on every ledger entrypoint is
            the operator kill-switch HOMIE_KILLSWITCH_CALLED_SHOTS (default-ON:
            absent env = enabled; the switch only turns it OFF).
        CALLED_SHOTS_DB_PATH (DATA_DIR/called_shots.db) — the ledger SQLite file.
        CALLED_SHOTS_STALE_AGE_DAYS (14, int) — T3 stale-open-shot sweep age.
        CALLED_SHOTS_MIRROR_ENABLED ("true") — per-shot vault mirror notes.
        CALLED_SHOTS_MIRROR_DIR (MEMORY_DIR/called-shots) — mirror note dir.
    """
    if enabled is None:
        enabled = os.getenv("CALLED_SHOTS_ENABLED", "true").lower() == "true"
    if db_path is None:
        db_path = os.getenv("CALLED_SHOTS_DB_PATH", "") or str(
            DATA_DIR / "called_shots.db"
        )
    if stale_age_days is None:
        # Malformed env degrades to the default with a visible receipt (the
        # heartbeat _int_env pattern) — a garbage value must never propagate a
        # ValueError through every ledger entrypoint.
        _raw_stale = os.getenv("CALLED_SHOTS_STALE_AGE_DAYS", "14")
        try:
            stale_age_days = int(_raw_stale)
        except (TypeError, ValueError):
            print(
                f"CALLED_SHOTS_STALE_AGE_DAYS={_raw_stale!r} is not an int; "
                "using default 14",
                flush=True,
            )
            stale_age_days = 14
    if mirror_enabled is None:
        mirror_enabled = (
            os.getenv("CALLED_SHOTS_MIRROR_ENABLED", "true").lower() == "true"
        )
    if mirror_dir is None:
        mirror_dir = os.getenv("CALLED_SHOTS_MIRROR_DIR", "") or str(
            MEMORY_DIR / "called-shots"
        )
    return CalledShotsSettings(
        enabled=enabled,
        db_path=db_path,
        stale_age_days=stale_age_days,
        mirror_enabled=mirror_enabled,
        mirror_dir=mirror_dir,
    )


class CryptoPlaysSettings(NamedTuple):
    """Effective crypto-play ledger paths, resolved at call time (issue #203)."""

    db_path: str
    mirror_enabled: bool
    mirror_dir: str


def get_crypto_plays_settings(
    db_path: str | None = None,
    mirror_enabled: bool | None = None,
    mirror_dir: str | None = None,
) -> CryptoPlaysSettings:
    """Resolve the private crypto-play ledger settings at call time.

    ``HOMIE_KILLSWITCH_CRYPTO_PLAYS`` is the only enablement control and is
    enforced by every service entrypoint.  These settings contain paths and
    the derived vault-mirror preference only:

    - ``CRYPTO_PLAYS_DB_PATH`` (``DATA_DIR/crypto_plays.db``)
    - ``CRYPTO_PLAYS_MIRROR_ENABLED`` (``true``)
    - ``CRYPTO_PLAYS_MIRROR_DIR`` (``MEMORY_DIR/crypto-plays``)
    """

    if db_path is None:
        db_path = os.getenv("CRYPTO_PLAYS_DB_PATH", "") or str(
            DATA_DIR / "crypto_plays.db"
        )
    if mirror_enabled is None:
        mirror_enabled = (
            os.getenv("CRYPTO_PLAYS_MIRROR_ENABLED", "true").lower() == "true"
        )
    if mirror_dir is None:
        mirror_dir = os.getenv("CRYPTO_PLAYS_MIRROR_DIR", "") or str(
            MEMORY_DIR / "crypto-plays"
        )
    return CryptoPlaysSettings(
        db_path=db_path,
        mirror_enabled=mirror_enabled,
        mirror_dir=mirror_dir,
    )


class CryptoAnchorSettings(NamedTuple):
    """Freshness bounds on a play's CALL-TIME price anchor (crypto Wave 4)."""

    max_age_seconds: float
    max_future_skew_seconds: float


#: A call-time anchor read more than this long before the play row is created
#: is refused. The live ledger's disease was measuring moves from prices
#: captured 4-46 hours after the call (median 24h, 2026-07-26); a window in
#: minutes turns "I fetched this yesterday" into a contract error.
CRYPTO_ANCHOR_DEFAULT_MAX_AGE_SECONDS = 300.0
#: Tolerated clock skew for an anchor stamped slightly ahead of the insert.
CRYPTO_ANCHOR_DEFAULT_MAX_FUTURE_SKEW_SECONDS = 60.0


def get_crypto_anchor_settings(
    max_age_seconds: float | None = None,
    max_future_skew_seconds: float | None = None,
) -> CryptoAnchorSettings:
    """Resolve the call-time anchor freshness bounds at call time.

    - ``CRYPTO_PLAYS_ANCHOR_MAX_AGE_SECONDS`` (``300``)
    - ``CRYPTO_PLAYS_ANCHOR_MAX_FUTURE_SKEW_SECONDS`` (``60``)

    A malformed or non-positive env value falls back to the default rather than
    widening the window: an unparseable bound must never become "no bound".
    """

    if max_age_seconds is None:
        max_age_seconds = _positive_float_or(
            os.getenv("CRYPTO_PLAYS_ANCHOR_MAX_AGE_SECONDS", ""),
            CRYPTO_ANCHOR_DEFAULT_MAX_AGE_SECONDS,
        )
    if max_future_skew_seconds is None:
        max_future_skew_seconds = _positive_float_or(
            os.getenv("CRYPTO_PLAYS_ANCHOR_MAX_FUTURE_SKEW_SECONDS", ""),
            CRYPTO_ANCHOR_DEFAULT_MAX_FUTURE_SKEW_SECONDS,
        )
    return CryptoAnchorSettings(
        max_age_seconds=float(max_age_seconds),
        max_future_skew_seconds=float(max_future_skew_seconds),
    )


def _positive_float_or(raw: object, fallback: float) -> float:
    """Positive finite float, or the fallback.  Never widens to zero/inf."""

    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(value) or value <= 0:
        return fallback
    return value


class CryptoLookaheadSettings(NamedTuple):
    """Effective look-ahead detector bounds, resolved at call time."""

    enabled: bool
    tolerance: float
    max_observations: int
    max_items: int
    recursive_depths: tuple[int, ...]
    drift_tolerance_pct: float


#: freqtrade `optimize/analysis/recursive.py` recomputes at exactly this ladder
#: and diffs the last row. Kept verbatim so a drift number here is comparable
#: to one produced upstream.
CRYPTO_LOOKAHEAD_DEFAULT_DEPTHS = (199, 399, 499, 999, 1999)

#: Hard ceiling on the score-equality tolerance. `math.isclose(rel_tol=1.0)`
#: matches EVERYTHING, so any value at or above 1 silently converts the
#: detector into a rubber stamp. Rejecting only `inf` stopped the one value
#: that would have been obvious. Mirrored by `cognition.crypto_lookahead`.
CRYPTO_LOOKAHEAD_TOLERANCE_CEILING = 1e-3


def _finite_or(value: object, fallback: float) -> float:
    """Non-negative finite float, or the fallback."""

    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(parsed) or parsed < 0:
        return fallback
    return parsed


def _banded_or(value: object, fallback: float, ceiling: float) -> float:
    """Finite float inside [0, ceiling), or the fallback. Loosening is refused."""

    parsed = _finite_or(value, fallback)
    if parsed >= ceiling:
        return fallback
    return parsed


def _parse_lookahead_depths(raw: str) -> tuple[int, ...]:
    """Parse a comma ladder; a malformed entry falls back to the default.

    A partly-parsed ladder would silently shrink the depth sweep and make a
    recursive indicator look stabler than it is, so the fallback is all-or-
    nothing rather than best-effort.
    """

    parts = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
    if not parts:
        return CRYPTO_LOOKAHEAD_DEFAULT_DEPTHS
    try:
        depths = tuple(sorted({int(chunk) for chunk in parts}))
    except ValueError:
        return CRYPTO_LOOKAHEAD_DEFAULT_DEPTHS
    if any(depth <= 0 for depth in depths):
        return CRYPTO_LOOKAHEAD_DEFAULT_DEPTHS
    return depths


def get_crypto_lookahead_settings(
    enabled: bool | None = None,
    tolerance: float | None = None,
    max_observations: int | None = None,
    max_items: int | None = None,
    recursive_depths: tuple[int, ...] | None = None,
    drift_tolerance_pct: float | None = None,
) -> CryptoLookaheadSettings:
    """Resolve the crypto look-ahead detector settings at call time.

    ``CRYPTO_LOOKAHEAD_ENABLED`` is default-ON and only turns the detector OFF.
    A disabled detector returns the explicit UNKNOWN verdict, which BLOCKS —
    turning it off removes the proof, never the requirement.

    - ``CRYPTO_LOOKAHEAD_ENABLED`` (``true``)
    - ``CRYPTO_LOOKAHEAD_TOLERANCE`` (``1e-9``) — numeric score equality
    - ``CRYPTO_LOOKAHEAD_MAX_OBSERVATIONS`` (``20000``) — per-replay bound
    - ``CRYPTO_LOOKAHEAD_MAX_ITEMS`` (``5000``) — per-batch bound
    - ``CRYPTO_LOOKAHEAD_RECURSIVE_DEPTHS`` (``199,399,499,999,1999``)
    - ``CRYPTO_LOOKAHEAD_DRIFT_TOLERANCE_PCT`` (``0.01``)
    """

    if enabled is None:
        enabled = os.getenv("CRYPTO_LOOKAHEAD_ENABLED", "true").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
            "disabled",
        )
    if tolerance is None:
        try:
            tolerance = float(os.getenv("CRYPTO_LOOKAHEAD_TOLERANCE", "") or 1e-9)
        except ValueError:
            tolerance = 1e-9
    if max_observations is None:
        try:
            max_observations = int(
                os.getenv("CRYPTO_LOOKAHEAD_MAX_OBSERVATIONS", "") or 20_000
            )
        except ValueError:
            max_observations = 20_000
    if max_items is None:
        try:
            max_items = int(os.getenv("CRYPTO_LOOKAHEAD_MAX_ITEMS", "") or 5_000)
        except ValueError:
            max_items = 5_000
    if recursive_depths is None:
        recursive_depths = _parse_lookahead_depths(
            os.getenv("CRYPTO_LOOKAHEAD_RECURSIVE_DEPTHS", "")
        )
    if drift_tolerance_pct is None:
        try:
            drift_tolerance_pct = float(
                os.getenv("CRYPTO_LOOKAHEAD_DRIFT_TOLERANCE_PCT", "") or 0.01
            )
        except ValueError:
            drift_tolerance_pct = 0.01
    # A non-finite tolerance would make every score compare equal, which reads
    # as CLEAN — the one direction this detector must never fail in.
    return CryptoLookaheadSettings(
        enabled=bool(enabled),
        tolerance=_banded_or(tolerance, 1e-9, CRYPTO_LOOKAHEAD_TOLERANCE_CEILING),
        max_observations=max(1, int(max_observations)),
        max_items=max(1, int(max_items)),
        recursive_depths=tuple(recursive_depths),
        drift_tolerance_pct=_finite_or(drift_tolerance_pct, 0.01),
    )


class CryptoProofSettings(NamedTuple):
    """Effective backtest promotion policy, resolved at call time."""

    max_p_value: float
    min_trades: int
    min_bars: int
    min_consistency_rate: float
    min_prob_positive: float
    permutation_iterations: int
    bootstrap_iterations: int
    walk_forward_folds: int
    min_bars_per_fold: int
    confidence_level: float
    seed: int
    require_run_card: bool


#: Hard bounds on the two gate-critical knobs. Env may TIGHTEN them; it can
#: never loosen them past these. Same shape as CRYPTO_EYES_MAX_ACTIONS_CEILING,
#: and re-clamped inside ``cognition.crypto_proof`` so an injected settings
#: object cannot widen them either. The trade floor is backtrader
#: `analyzers/sqn.py:31-85` — below N=30 the banding stops meaning anything.
CRYPTO_PROOF_MAX_P_VALUE_CEILING = 0.10
CRYPTO_PROOF_MIN_TRADES_FLOOR = 30


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Parse one bounded int knob; a malformed value degrades to the default."""

    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        print(f"{name}={raw!r} is not an int; using default {default}", flush=True)
        return default
    if not minimum <= parsed <= maximum:
        print(f"{name}={raw!r} is out of range; using default {default}", flush=True)
        return default
    return parsed


def get_crypto_proof_settings(
    max_p_value: float | None = None,
    min_trades: int | None = None,
    min_bars: int | None = None,
    min_consistency_rate: float | None = None,
    min_prob_positive: float | None = None,
    permutation_iterations: int | None = None,
    bootstrap_iterations: int | None = None,
    walk_forward_folds: int | None = None,
    min_bars_per_fold: int | None = None,
    confidence_level: float | None = None,
    seed: int | None = None,
    require_run_card: bool | None = None,
) -> CryptoProofSettings:
    """Resolve the proof-harness promotion policy at CALL TIME (Rule 1).

    Knobs (all optional; every default is the STRICT direction):

        CRYPTO_PROOF_MAX_P_VALUE (0.05) — permutation p-value bound, clamped
            to CRYPTO_PROOF_MAX_P_VALUE_CEILING.
        CRYPTO_PROOF_MIN_TRADES (30) — SQN's N>=30 reliability floor, clamped
            to CRYPTO_PROOF_MIN_TRADES_FLOOR.
        CRYPTO_PROOF_MIN_BARS (200) — minimum bars before a verdict is a
            finding rather than noise.
        CRYPTO_PROOF_MIN_CONSISTENCY_RATE (0.6) — walk-forward floor.
        CRYPTO_PROOF_MIN_PROB_POSITIVE (0.9) — bootstrap P(Sharpe > 0) floor.
        CRYPTO_PROOF_PERMUTATION_ITERATIONS (1000).
        CRYPTO_PROOF_BOOTSTRAP_ITERATIONS (1000).
        CRYPTO_PROOF_WALK_FORWARD_FOLDS (5).
        CRYPTO_PROOF_MIN_BARS_PER_FOLD (30).
        CRYPTO_PROOF_CONFIDENCE_LEVEL (0.95).
        CRYPTO_PROOF_SEED (20260726) — permutation/bootstrap seed, so a receipt
            is reproducible.
        CRYPTO_PROOF_REQUIRE_RUN_CARD ("true") — a complete hash chain is part
            of the definition of proven; turning it off cannot make a verdict
            more permissive than UNEVALUATED elsewhere, it only removes the
            chain requirement for local experiments.
    """

    if max_p_value is None:
        max_p_value = _finite_or(os.getenv("CRYPTO_PROOF_MAX_P_VALUE", ""), 0.05)
        if max_p_value <= 0.0:
            max_p_value = 0.05
    if min_trades is None:
        min_trades = _bounded_int(
            "CRYPTO_PROOF_MIN_TRADES", 30, minimum=1, maximum=1_000_000
        )
    if min_bars is None:
        min_bars = _bounded_int(
            "CRYPTO_PROOF_MIN_BARS", 200, minimum=2, maximum=10_000_000
        )
    if min_consistency_rate is None:
        min_consistency_rate = _finite_or(
            os.getenv("CRYPTO_PROOF_MIN_CONSISTENCY_RATE", ""), 0.6
        )
    if min_prob_positive is None:
        min_prob_positive = _finite_or(
            os.getenv("CRYPTO_PROOF_MIN_PROB_POSITIVE", ""), 0.9
        )
    if permutation_iterations is None:
        permutation_iterations = _bounded_int(
            "CRYPTO_PROOF_PERMUTATION_ITERATIONS", 1000, minimum=1, maximum=1_000_000
        )
    if bootstrap_iterations is None:
        bootstrap_iterations = _bounded_int(
            "CRYPTO_PROOF_BOOTSTRAP_ITERATIONS", 1000, minimum=1, maximum=1_000_000
        )
    if walk_forward_folds is None:
        walk_forward_folds = _bounded_int(
            "CRYPTO_PROOF_WALK_FORWARD_FOLDS", 5, minimum=2, maximum=100
        )
    if min_bars_per_fold is None:
        min_bars_per_fold = _bounded_int(
            "CRYPTO_PROOF_MIN_BARS_PER_FOLD", 30, minimum=2, maximum=1_000_000
        )
    if confidence_level is None:
        confidence_level = _finite_or(
            os.getenv("CRYPTO_PROOF_CONFIDENCE_LEVEL", ""), 0.95
        )
        if not 0.0 < confidence_level < 1.0:
            confidence_level = 0.95
    if seed is None:
        seed = _bounded_int("CRYPTO_PROOF_SEED", 20260726, minimum=0, maximum=2**31 - 1)
    if require_run_card is None:
        require_run_card = (
            os.getenv("CRYPTO_PROOF_REQUIRE_RUN_CARD", "true").lower() == "true"
        )

    return CryptoProofSettings(
        max_p_value=min(float(max_p_value), CRYPTO_PROOF_MAX_P_VALUE_CEILING),
        min_trades=max(int(min_trades), CRYPTO_PROOF_MIN_TRADES_FLOOR),
        min_bars=int(min_bars),
        min_consistency_rate=min(max(float(min_consistency_rate), 0.0), 1.0),
        min_prob_positive=min(max(float(min_prob_positive), 0.0), 1.0),
        permutation_iterations=int(permutation_iterations),
        bootstrap_iterations=int(bootstrap_iterations),
        walk_forward_folds=int(walk_forward_folds),
        min_bars_per_fold=int(min_bars_per_fold),
        confidence_level=float(confidence_level),
        seed=int(seed),
        require_run_card=bool(require_run_card),
    )


class CryptoEyesSettings(NamedTuple):
    """Effective crypto persona live-look settings, resolved at call time."""

    db_path: str
    max_actions: int
    timeout_seconds: float
    default_query: str
    discord_guild: str
    discord_channels: str


#: A look with no named subject falls back to this live X search.
CRYPTO_EYES_DEFAULT_QUERY = (
    "(crypto OR solana OR ethereum OR onchain) min_faves:40 -filter:replies"
)

#: Hard ceilings on one live look. The spec is 8 browser actions and 90
#: seconds; env vars and explicit arguments may only lower them. Mirrored at
#: the execution boundary by ``crypto_eyes_driver.LookBudget``, which clamps
#: again so an injected settings object cannot raise them.
CRYPTO_EYES_MAX_ACTIONS_CEILING = 8
CRYPTO_EYES_TIMEOUT_CEILING_S = 90.0


def get_crypto_eyes_settings(
    db_path: str | None = None,
    max_actions: int | None = None,
    timeout_seconds: float | None = None,
    default_query: str | None = None,
    discord_guild: str | None = None,
    discord_channels: str | None = None,
) -> CryptoEyesSettings:
    """Resolve the crypto persona's live-look settings at call time.

    ``HOMIE_KILLSWITCH_CRYPTO_EYES`` is the only enablement control (absent =
    ON; the switch only turns the look OFF) and is enforced by the driver and
    the receipt store, not here. These settings are bounds and targets:

    There is deliberately NO CDP-port knob: the look attaches to 18222 and the
    ``crypto-persona-look`` session as constants in the driver. An
    env-overridable "fixed" port is not fixed, and 9222 is unusable on this
    host.

    - ``CRYPTO_EYES_DB_PATH`` (``DATA_DIR/crypto_looks.db``)
    - ``CRYPTO_EYES_MAX_ACTIONS`` (``8``, ceiling ``8``) — actions per look
    - ``CRYPTO_EYES_TIMEOUT_SECONDS`` (``90``, ceiling ``90``) — wall clock
    - ``CRYPTO_EYES_DEFAULT_QUERY`` — subject-less look fallback search
    - ``CRYPTO_EYES_DISCORD_GUILD`` / ``CRYPTO_EYES_DISCORD_CHANNELS``,
      falling back to the alpha desk's already-configured source surface
      (``DISCORD_ALPHA_SOURCE_GUILD`` / ``DISCORD_ALPHA_CHANNELS``).
    """

    if db_path is None:
        db_path = os.getenv("CRYPTO_EYES_DB_PATH", "") or str(
            DATA_DIR / "crypto_looks.db"
        )
    if max_actions is None:
        try:
            max_actions = int(os.getenv("CRYPTO_EYES_MAX_ACTIONS", "") or 8)
        except ValueError:
            max_actions = 8
    # Ceilings, not suggestions: env AND explicit arguments are clamped here,
    # and the execution boundary (LookBudget) clamps again so an injected
    # settings object cannot raise them either.
    max_actions = max(1, min(int(max_actions), CRYPTO_EYES_MAX_ACTIONS_CEILING))
    if timeout_seconds is None:
        try:
            timeout_seconds = float(
                os.getenv("CRYPTO_EYES_TIMEOUT_SECONDS", "") or 90.0
            )
        except ValueError:
            timeout_seconds = 90.0
    timeout_seconds = max(
        1.0, min(float(timeout_seconds), CRYPTO_EYES_TIMEOUT_CEILING_S)
    )
    if default_query is None:
        default_query = (
            os.getenv("CRYPTO_EYES_DEFAULT_QUERY", "").strip()
            or CRYPTO_EYES_DEFAULT_QUERY
        )
    if discord_guild is None:
        discord_guild = (
            os.getenv("CRYPTO_EYES_DISCORD_GUILD", "").strip()
            or os.getenv("DISCORD_ALPHA_SOURCE_GUILD", "").strip()
        )
    if discord_channels is None:
        discord_channels = (
            os.getenv("CRYPTO_EYES_DISCORD_CHANNELS", "").strip()
            or os.getenv("DISCORD_ALPHA_CHANNELS", "").strip()
        )
    return CryptoEyesSettings(
        db_path=db_path,
        max_actions=max_actions,
        timeout_seconds=timeout_seconds,
        default_query=default_query,
        discord_guild=discord_guild,
        discord_channels=discord_channels,
    )


class CryptoOrderGuardSettings(NamedTuple):
    """Effective Wave-8 execution-guard settings, resolved at call time."""

    state_path: str
    halt_path: str
    live_armed: bool
    submit_timeout_seconds: float
    max_reconcile_retries: int
    lock_timeout_seconds: float
    ticket_ttl_seconds: float


#: The ONE token that arms live execution. Anything else — unset, empty,
#: "true", "1", "yes" — leaves the guard in dry-run. A boolean-ish knob invites
#: a stray `=1` in a copied .env to arm a funded wallet.
CRYPTO_ORDER_GUARD_LIVE_TOKEN = "enabled"


def get_crypto_order_guard_settings(
    state_path: str | None = None,
    halt_path: str | None = None,
    live_armed: bool | None = None,
    submit_timeout_seconds: float | None = None,
    max_reconcile_retries: int | None = None,
    lock_timeout_seconds: float | None = None,
    ticket_ttl_seconds: float | None = None,
) -> CryptoOrderGuardSettings:
    """Resolve the crypto execution-guard settings at call time.

    ``HOMIE_KILLSWITCH_CRYPTO_ORDER_GUARD`` is the enablement control (absent =
    ON; the switch only turns the guard OFF) and is enforced by the guard, not
    here. Arming live execution is a SEPARATE, explicitly-named gate:

    - ``CRYPTO_ORDER_GUARD_STATE_PATH`` (``STATE_DIR/crypto-order-guard.json``)
      — the physical request/day ledger. Idempotency and the day counter both
      read it fresh; it is never cached.
    - ``CRYPTO_ORDER_GUARD_HALT_PATH`` (``STATE_DIR/live/HALT``) — the
      filesystem halt sentinel. Existence is the halt.
    - ``CRYPTO_ORDER_GUARD_LIVE`` — must equal ``enabled`` exactly
      (case-insensitive) to arm live execution. Default: dry-run only.
    - ``CRYPTO_ORDER_GUARD_SUBMIT_TIMEOUT_SECONDS`` (``20``, 1-120) — hard wall
      on the async submit path.
    - ``CRYPTO_ORDER_GUARD_MAX_RECONCILE_RETRIES`` (``1``, 0-5) — how many times
      an AUTHORITATIVELY-absent order may be re-armed after a timeout.
    - ``CRYPTO_ORDER_GUARD_LOCK_TIMEOUT_SECONDS`` (``10``, 0.5-120) — ledger
      cross-process lock wait.
    - ``CRYPTO_ORDER_GUARD_TICKET_TTL_SECONDS`` (``300``, 5-3600) — how long an
      armed ticket may sit before ``submit`` refuses it. Without a TTL a ticket
      is a standing permission, and an 8-hour-old one placed without complaint.
    """

    if state_path is None:
        state_path = os.getenv("CRYPTO_ORDER_GUARD_STATE_PATH", "") or str(
            STATE_DIR / "crypto-order-guard.json"
        )
    if halt_path is None:
        halt_path = os.getenv("CRYPTO_ORDER_GUARD_HALT_PATH", "") or str(
            STATE_DIR / "live" / "HALT"
        )
    if live_armed is None:
        live_armed = (
            os.getenv("CRYPTO_ORDER_GUARD_LIVE", "").strip().lower()
            == CRYPTO_ORDER_GUARD_LIVE_TOKEN
        )
    if submit_timeout_seconds is None:
        try:
            submit_timeout_seconds = float(
                os.getenv("CRYPTO_ORDER_GUARD_SUBMIT_TIMEOUT_SECONDS", "") or 20.0
            )
        except ValueError:
            submit_timeout_seconds = 20.0
    submit_timeout_seconds = max(1.0, min(float(submit_timeout_seconds), 120.0))
    if max_reconcile_retries is None:
        try:
            max_reconcile_retries = int(
                os.getenv("CRYPTO_ORDER_GUARD_MAX_RECONCILE_RETRIES", "") or 1
            )
        except ValueError:
            max_reconcile_retries = 1
    max_reconcile_retries = max(0, min(int(max_reconcile_retries), 5))
    if lock_timeout_seconds is None:
        try:
            lock_timeout_seconds = float(
                os.getenv("CRYPTO_ORDER_GUARD_LOCK_TIMEOUT_SECONDS", "") or 10.0
            )
        except ValueError:
            lock_timeout_seconds = 10.0
    lock_timeout_seconds = max(0.5, min(float(lock_timeout_seconds), 120.0))
    if ticket_ttl_seconds is None:
        try:
            ticket_ttl_seconds = float(
                os.getenv("CRYPTO_ORDER_GUARD_TICKET_TTL_SECONDS", "") or 300.0
            )
        except ValueError:
            ticket_ttl_seconds = 300.0
    ticket_ttl_seconds = max(5.0, min(float(ticket_ttl_seconds), 3600.0))
    return CryptoOrderGuardSettings(
        state_path=state_path,
        halt_path=halt_path,
        live_armed=bool(live_armed),
        submit_timeout_seconds=submit_timeout_seconds,
        max_reconcile_retries=max_reconcile_retries,
        lock_timeout_seconds=lock_timeout_seconds,
        ticket_ttl_seconds=ticket_ttl_seconds,
    )


class CryptoExecutionSettings(NamedTuple):
    """Effective Wave-8 execution-CLIENT settings, resolved at call time."""

    venue_id: str
    market_type: str
    request_timeout_seconds: float
    place_timeout_seconds: float
    ip_allowlist_claimed: bool
    withdrawals_attested_disabled: bool
    first_run_max_notional_usd: float


def get_crypto_execution_settings(
    venue_id: str | None = None,
    market_type: str | None = None,
    request_timeout_seconds: float | None = None,
    place_timeout_seconds: float | None = None,
    ip_allowlist_claimed: bool | None = None,
    withdrawals_attested_disabled: bool | None = None,
    first_run_max_notional_usd: float | None = None,
) -> CryptoExecutionSettings:
    """Resolve the ccxt execution-client settings at call time.

    This module holds NO credentials and reads none: there is deliberately no
    ``*_API_KEY`` knob here. An authenticated exchange is injected by the
    caller or the client stays in dry-run.

    - ``CRYPTO_EXECUTION_VENUE_ID`` (``binance``) / ``CRYPTO_EXECUTION_MARKET_TYPE``
      (``swap``) — which ccxt venue the OFFLINE capability probe describes.
    - ``CRYPTO_EXECUTION_REQUEST_TIMEOUT_SECONDS`` (``20``, 1-120) — per-request
      wall pushed down into ccxt itself.
    - ``CRYPTO_EXECUTION_PLACE_TIMEOUT_SECONDS`` (``30``, 1-180) — hard wall on
      the async place/resolve path. It cannot kill the worker thread, so a
      breach surfaces as "reconcile required", never as a resubmittable ticket.
    - ``CRYPTO_EXECUTION_IP_ALLOWLISTED`` (``false``) — the operator's CLAIM that
      the key is IP-allowlisted. Absent = refuse live.
    - ``CRYPTO_EXECUTION_WITHDRAWALS_DISABLED_ATTESTED`` (``false``) — the
      operator's CLAIM that withdrawals are off, used ONLY when the venue
      cannot be asked. A physical read showing withdrawals ON always wins.
    - ``CRYPTO_EXECUTION_FIRST_RUN_MAX_NOTIONAL_USD`` (``25``, 1-100000) — the
      "minimum sizes on first live runs" hygiene item, as a live-only ceiling.
    """

    if venue_id is None:
        venue_id = os.getenv("CRYPTO_EXECUTION_VENUE_ID", "").strip() or "binance"
    if market_type is None:
        market_type = os.getenv("CRYPTO_EXECUTION_MARKET_TYPE", "").strip() or "swap"
    if request_timeout_seconds is None:
        try:
            request_timeout_seconds = float(
                os.getenv("CRYPTO_EXECUTION_REQUEST_TIMEOUT_SECONDS", "") or 20.0
            )
        except ValueError:
            request_timeout_seconds = 20.0
    request_timeout_seconds = max(1.0, min(float(request_timeout_seconds), 120.0))
    if place_timeout_seconds is None:
        try:
            place_timeout_seconds = float(
                os.getenv("CRYPTO_EXECUTION_PLACE_TIMEOUT_SECONDS", "") or 30.0
            )
        except ValueError:
            place_timeout_seconds = 30.0
    place_timeout_seconds = max(1.0, min(float(place_timeout_seconds), 180.0))
    if ip_allowlist_claimed is None:
        ip_allowlist_claimed = (
            os.getenv("CRYPTO_EXECUTION_IP_ALLOWLISTED", "false").strip().lower()
            == "true"
        )
    if withdrawals_attested_disabled is None:
        withdrawals_attested_disabled = (
            os.getenv("CRYPTO_EXECUTION_WITHDRAWALS_DISABLED_ATTESTED", "false")
            .strip()
            .lower()
            == "true"
        )
    if first_run_max_notional_usd is None:
        try:
            first_run_max_notional_usd = float(
                os.getenv("CRYPTO_EXECUTION_FIRST_RUN_MAX_NOTIONAL_USD", "") or 25.0
            )
        except ValueError:
            first_run_max_notional_usd = 25.0
    first_run_max_notional_usd = max(
        1.0, min(float(first_run_max_notional_usd), 100_000.0)
    )
    return CryptoExecutionSettings(
        venue_id=venue_id,
        market_type=market_type,
        request_timeout_seconds=request_timeout_seconds,
        place_timeout_seconds=place_timeout_seconds,
        ip_allowlist_claimed=bool(ip_allowlist_claimed),
        withdrawals_attested_disabled=bool(withdrawals_attested_disabled),
        first_run_max_notional_usd=first_run_max_notional_usd,
    )


class CryptoRiskSettings(NamedTuple):
    """Effective Wave-6 risk-gate settings, resolved at call time."""

    halt_path: str
    mandate_path: str
    mandate_max_consent_days: int
    lookback_minutes: int
    stop_duration_minutes: int
    unlock_at: str
    stoploss_trade_limit: int
    stoploss_required_profit: float
    stoploss_only_per_side: bool
    stoploss_only_per_pair: bool
    cooldown_minutes: int
    drawdown_trade_limit: int
    max_allowed_drawdown: float
    starting_balance_usd: float


#: Hard ceiling on how long ONE mandate may authorize autonomous trading.
#: ``CRYPTO_RISK_MANDATE_MAX_DAYS`` may only LOWER it. The ceiling is a module
#: constant rather than an env value because "authorization decays" is the
#: whole point: an env knob that could raise it to 3650 would convert a bounded
#: grant back into a permanent one.
CRYPTO_RISK_MANDATE_MAX_DAYS_CEILING = 90

def _crypto_risk_unlock_at_ok(value: str) -> bool:
    """``HH:MM`` on a 24-hour clock, zero-padded. Anything else is ignored."""

    hour, _, minute = value.partition(":")
    if len(hour) != 2 or len(minute) != 2 or not hour.isdigit() or not minute.isdigit():
        return False
    return 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59


def get_crypto_risk_settings(
    halt_path: str | None = None,
    mandate_path: str | None = None,
    mandate_max_consent_days: int | None = None,
    lookback_minutes: int | None = None,
    stop_duration_minutes: int | None = None,
    unlock_at: str | None = None,
    stoploss_trade_limit: int | None = None,
    stoploss_required_profit: float | None = None,
    stoploss_only_per_side: bool | None = None,
    stoploss_only_per_pair: bool | None = None,
    cooldown_minutes: int | None = None,
    drawdown_trade_limit: int | None = None,
    max_allowed_drawdown: float | None = None,
    starting_balance_usd: float | None = None,
) -> CryptoRiskSettings:
    """Resolve the Wave-6 risk-gate settings at call time (Rule 1).

    These bound the circuit breakers (``cognition.crypto_protections``), the
    filesystem halt sentinel (``crypto_halt``), and the trading mandate
    (``cognition.crypto_mandate``). None of them ENABLE anything — every gate
    they configure is a refusal surface.

    - ``CRYPTO_RISK_HALT_PATH`` — the ONE physical halt sentinel. Falls back to
      ``CRYPTO_ORDER_GUARD_HALT_PATH`` before the default so an operator who
      relocated the Wave-8 guard's sentinel does not end up with two files and
      a desk that only half-stops. Default ``STATE_DIR/live/HALT``.
    - ``CRYPTO_RISK_MANDATE_PATH`` (``STATE_DIR/live/mandate.json``) — read
      fresh on every check; never cached, because expiry has to be real.
    - ``CRYPTO_RISK_MANDATE_MAX_DAYS`` (``30``, ceiling
      ``CRYPTO_RISK_MANDATE_MAX_DAYS_CEILING``) — a mandate whose consent
      window is longer is INVALID, which denies.
    - ``CRYPTO_RISK_LOOKBACK_MINUTES`` (``60``) / ``CRYPTO_RISK_STOP_DURATION_MINUTES``
      (``60``) — freqtrade ``IProtection`` window and lock length.
    - ``CRYPTO_RISK_UNLOCK_AT`` (``""``) — optional ``HH:MM`` fixed unlock
      time; an unparseable value is ignored with a receipt.
    - ``CRYPTO_RISK_STOPLOSS_TRADE_LIMIT`` (``4``),
      ``CRYPTO_RISK_STOPLOSS_REQUIRED_PROFIT`` (``0.0``),
      ``CRYPTO_RISK_STOPLOSS_ONLY_PER_SIDE`` (``true``),
      ``CRYPTO_RISK_STOPLOSS_ONLY_PER_PAIR`` (``false``) — StoplossGuard.
      ``only_per_side`` defaults ON because this operator trades both
      directions: a run of long stop-outs must lock LONGS and leave shorts
      open, not freeze the book.
    - ``CRYPTO_RISK_COOLDOWN_MINUTES`` (``60``) — CooldownPeriod, the
      revenge-trade blocker.
    - ``CRYPTO_RISK_DRAWDOWN_TRADE_LIMIT`` (``5``),
      ``CRYPTO_RISK_MAX_ALLOWED_DRAWDOWN`` (``0.20``),
      ``CRYPTO_RISK_STARTING_BALANCE_USD`` (``0`` = unknown) — MaxDrawdown.
      With no starting balance the drawdown is peak-relative and therefore
      uncomputable while cumulative profit is still negative; that case
      resolves to UNKNOWN, which blocks.
    """

    if halt_path is None:
        halt_path = (
            os.getenv("CRYPTO_RISK_HALT_PATH", "").strip()
            or os.getenv("CRYPTO_ORDER_GUARD_HALT_PATH", "").strip()
            or str(STATE_DIR / "live" / "HALT")
        )
    if mandate_path is None:
        mandate_path = os.getenv("CRYPTO_RISK_MANDATE_PATH", "").strip() or str(
            STATE_DIR / "live" / "mandate.json"
        )
    if mandate_max_consent_days is None:
        try:
            mandate_max_consent_days = int(
                os.getenv("CRYPTO_RISK_MANDATE_MAX_DAYS", "") or 30
            )
        except ValueError:
            mandate_max_consent_days = 30
    mandate_max_consent_days = max(
        1, min(int(mandate_max_consent_days), CRYPTO_RISK_MANDATE_MAX_DAYS_CEILING)
    )
    if lookback_minutes is None:
        try:
            lookback_minutes = int(os.getenv("CRYPTO_RISK_LOOKBACK_MINUTES", "") or 60)
        except ValueError:
            lookback_minutes = 60
    lookback_minutes = max(1, min(int(lookback_minutes), 60 * 24 * 30))
    if stop_duration_minutes is None:
        try:
            stop_duration_minutes = int(
                os.getenv("CRYPTO_RISK_STOP_DURATION_MINUTES", "") or 60
            )
        except ValueError:
            stop_duration_minutes = 60
    stop_duration_minutes = max(1, min(int(stop_duration_minutes), 60 * 24 * 30))
    if unlock_at is None:
        unlock_at = os.getenv("CRYPTO_RISK_UNLOCK_AT", "").strip()
    unlock_at = str(unlock_at).strip()
    if unlock_at and not _crypto_risk_unlock_at_ok(unlock_at):
        print(
            f"[config] invalid CRYPTO_RISK_UNLOCK_AT={unlock_at!r}; ignoring",
            flush=True,
        )
        unlock_at = ""
    if stoploss_trade_limit is None:
        try:
            stoploss_trade_limit = int(
                os.getenv("CRYPTO_RISK_STOPLOSS_TRADE_LIMIT", "") or 4
            )
        except ValueError:
            stoploss_trade_limit = 4
    stoploss_trade_limit = max(1, min(int(stoploss_trade_limit), 1_000))
    if stoploss_required_profit is None:
        try:
            stoploss_required_profit = float(
                os.getenv("CRYPTO_RISK_STOPLOSS_REQUIRED_PROFIT", "") or 0.0
            )
        except ValueError:
            stoploss_required_profit = 0.0
    stoploss_required_profit = float(stoploss_required_profit)
    if not math.isfinite(stoploss_required_profit):
        stoploss_required_profit = 0.0
    if stoploss_only_per_side is None:
        stoploss_only_per_side = (
            os.getenv("CRYPTO_RISK_STOPLOSS_ONLY_PER_SIDE", "true").strip().lower()
            == "true"
        )
    if stoploss_only_per_pair is None:
        stoploss_only_per_pair = (
            os.getenv("CRYPTO_RISK_STOPLOSS_ONLY_PER_PAIR", "false").strip().lower()
            == "true"
        )
    if cooldown_minutes is None:
        try:
            cooldown_minutes = int(os.getenv("CRYPTO_RISK_COOLDOWN_MINUTES", "") or 60)
        except ValueError:
            cooldown_minutes = 60
    cooldown_minutes = max(1, min(int(cooldown_minutes), 60 * 24 * 30))
    if drawdown_trade_limit is None:
        try:
            drawdown_trade_limit = int(
                os.getenv("CRYPTO_RISK_DRAWDOWN_TRADE_LIMIT", "") or 5
            )
        except ValueError:
            drawdown_trade_limit = 5
    drawdown_trade_limit = max(1, min(int(drawdown_trade_limit), 1_000))
    if max_allowed_drawdown is None:
        try:
            max_allowed_drawdown = float(
                os.getenv("CRYPTO_RISK_MAX_ALLOWED_DRAWDOWN", "") or 0.20
            )
        except ValueError:
            max_allowed_drawdown = 0.20
    max_allowed_drawdown = float(max_allowed_drawdown)
    if not math.isfinite(max_allowed_drawdown) or max_allowed_drawdown <= 0.0:
        max_allowed_drawdown = 0.20
    max_allowed_drawdown = min(max_allowed_drawdown, 1.0)
    if starting_balance_usd is None:
        try:
            starting_balance_usd = float(
                os.getenv("CRYPTO_RISK_STARTING_BALANCE_USD", "") or 0.0
            )
        except ValueError:
            starting_balance_usd = 0.0
    starting_balance_usd = float(starting_balance_usd)
    if not math.isfinite(starting_balance_usd) or starting_balance_usd < 0.0:
        starting_balance_usd = 0.0
    return CryptoRiskSettings(
        halt_path=halt_path,
        mandate_path=mandate_path,
        mandate_max_consent_days=mandate_max_consent_days,
        lookback_minutes=lookback_minutes,
        stop_duration_minutes=stop_duration_minutes,
        unlock_at=unlock_at,
        stoploss_trade_limit=stoploss_trade_limit,
        stoploss_required_profit=stoploss_required_profit,
        stoploss_only_per_side=bool(stoploss_only_per_side),
        stoploss_only_per_pair=bool(stoploss_only_per_pair),
        cooldown_minutes=cooldown_minutes,
        drawdown_trade_limit=drawdown_trade_limit,
        max_allowed_drawdown=max_allowed_drawdown,
        starting_balance_usd=starting_balance_usd,
    )


class CryptoCandlesSettings(NamedTuple):
    """Effective candle-store knobs, resolved at call time (crypto TA Wave 0)."""

    exchange_id: str
    market_type: str
    symbol: str
    timeframes: tuple[str, ...]
    store_dir: str
    fetch_budget_s: float
    request_timeout_s: float
    async_grace_s: float
    page_limit: int
    max_pages: int
    max_empty_pages: int
    max_attempts: int
    retry_base_delay_s: float


#: The desk's default instrument: a LEVERAGED linear perpetual, not spot.
CRYPTO_CANDLES_DEFAULT_SYMBOL = "BTC/USDT:USDT"
#: Scalp frames first — the persona reads 5m/15m and takes 1h for context.
CRYPTO_CANDLES_DEFAULT_TIMEFRAMES = ("5m", "15m", "1h")
#: Hard ceiling on one fetch's wall clock. A hung exchange inside a persona
#: prefetch is the failure class that froze the bot's event loop on
#: 2026-07-13, so env vars and explicit arguments may only LOWER this.
CRYPTO_CANDLES_BUDGET_CEILING_S = 30.0


def get_crypto_candles_settings(
    exchange_id: str | None = None,
    market_type: str | None = None,
    symbol: str | None = None,
    timeframes: tuple[str, ...] | None = None,
    store_dir: str | None = None,
    fetch_budget_s: float | None = None,
    request_timeout_s: float | None = None,
    async_grace_s: float | None = None,
    page_limit: int | None = None,
    max_pages: int | None = None,
    max_empty_pages: int | None = None,
    max_attempts: int | None = None,
    retry_base_delay_s: float | None = None,
) -> CryptoCandlesSettings:
    """Resolve the candle store's knobs at CALL TIME (Rule 1).

    There is no API key knob and no secret: every endpoint the candle store
    touches is public, and nothing in that slice can place or fund an order.

    - ``CRYPTO_CANDLES_EXCHANGE`` (``okx``) — ccxt exchange id. Binance and
      Bybit both refuse this operator's location outright (HTTP 451 / 403 on
      the public endpoints, no key involved), so a Binance default renders the
      whole chart read UNAVAILABLE here. OKX serves the same ``BTC/USDT:USDT``
      linear perp and answers. Kraken and Coinbase are also reachable but are
      spot-only for this symbol shape.
    - ``CRYPTO_CANDLES_MARKET_TYPE`` (``swap``) — ccxt ``defaultType``
    - ``CRYPTO_CANDLES_SYMBOL`` (``BTC/USDT:USDT``)
    - ``CRYPTO_CANDLES_TIMEFRAMES`` (``5m,15m,1h``) — first entry is the default
    - ``CRYPTO_CANDLES_STORE_DIR`` (``DATA_DIR/crypto_candles``)
    - ``CRYPTO_CANDLES_BUDGET_SECONDS`` (``12``, ceiling ``30``) — whole fetch
    - ``CRYPTO_CANDLES_REQUEST_TIMEOUT_SECONDS`` (``8``) — one HTTP attempt
    - ``CRYPTO_CANDLES_ASYNC_GRACE_SECONDS`` (``3``) — added to the budget for
      the ``asyncio.wait_for`` belt so the inner path can report a real reason
    - ``CRYPTO_CANDLES_PAGE_LIMIT`` (``100``) — candles per fetch. This is a
      CORRECTNESS floor, not a throughput knob. With no explicit ``since_ms``
      the fetch window is sized ``span * page_limit * max_pages`` and the loop
      walks FORWARD, so it only reaches the present when each page really does
      advance ``page_limit`` bars. An exchange that silently caps below the
      request falls short by that ratio — and asking for MORE pages moves the
      window start further back, so the answer gets staler, not fresher. At 500
      against OKX (real cap 100) the newest 1h bar came back a month old.
      100 is honoured everywhere measured. Raise it only for an exchange whose
      real cap you have checked.
    - ``CRYPTO_CANDLES_MAX_PAGES`` (``4``) — pages per fetch call
    - ``CRYPTO_CANDLES_MAX_EMPTY_PAGES`` (``3``) — consecutive empty pages the
      pagination loop steps over before giving up (an empty page is NOT
      end-of-data; a Binance maintenance window outlives one page span)
    - ``CRYPTO_CANDLES_MAX_ATTEMPTS`` (``3``) — transient retries per page
    - ``CRYPTO_CANDLES_RETRY_BASE_DELAY_SECONDS`` (``0.5``)
    """

    if exchange_id is None:
        exchange_id = os.getenv("CRYPTO_CANDLES_EXCHANGE", "").strip() or "okx"
    if market_type is None:
        market_type = os.getenv("CRYPTO_CANDLES_MARKET_TYPE", "").strip() or "swap"
    if symbol is None:
        symbol = (
            os.getenv("CRYPTO_CANDLES_SYMBOL", "").strip()
            or CRYPTO_CANDLES_DEFAULT_SYMBOL
        )
    if timeframes is None:
        raw_timeframes = os.getenv("CRYPTO_CANDLES_TIMEFRAMES", "").strip()
        parsed = tuple(
            entry.strip() for entry in raw_timeframes.split(",") if entry.strip()
        )
        timeframes = parsed or CRYPTO_CANDLES_DEFAULT_TIMEFRAMES
    if store_dir is None:
        store_dir = os.getenv("CRYPTO_CANDLES_STORE_DIR", "") or str(
            DATA_DIR / "crypto_candles"
        )
    if fetch_budget_s is None:
        try:
            fetch_budget_s = float(
                os.getenv("CRYPTO_CANDLES_BUDGET_SECONDS", "") or 12.0
            )
        except ValueError:
            fetch_budget_s = 12.0
    # Ceiling, not a suggestion: an injected settings object cannot raise it.
    fetch_budget_s = max(1.0, min(float(fetch_budget_s), CRYPTO_CANDLES_BUDGET_CEILING_S))
    if request_timeout_s is None:
        try:
            request_timeout_s = float(
                os.getenv("CRYPTO_CANDLES_REQUEST_TIMEOUT_SECONDS", "") or 8.0
            )
        except ValueError:
            request_timeout_s = 8.0
    request_timeout_s = max(0.5, min(float(request_timeout_s), fetch_budget_s))
    if async_grace_s is None:
        try:
            async_grace_s = float(
                os.getenv("CRYPTO_CANDLES_ASYNC_GRACE_SECONDS", "") or 3.0
            )
        except ValueError:
            async_grace_s = 3.0
    async_grace_s = max(0.5, min(float(async_grace_s), 15.0))
    if page_limit is None:
        try:
            page_limit = int(os.getenv("CRYPTO_CANDLES_PAGE_LIMIT", "") or 100)
        except ValueError:
            page_limit = 500
    page_limit = max(1, min(int(page_limit), 1000))
    if max_pages is None:
        try:
            max_pages = int(os.getenv("CRYPTO_CANDLES_MAX_PAGES", "") or 4)
        except ValueError:
            max_pages = 4
    max_pages = max(1, int(max_pages))
    if max_empty_pages is None:
        try:
            max_empty_pages = int(os.getenv("CRYPTO_CANDLES_MAX_EMPTY_PAGES", "") or 3)
        except ValueError:
            max_empty_pages = 3
    max_empty_pages = max(0, int(max_empty_pages))
    if max_attempts is None:
        try:
            max_attempts = int(os.getenv("CRYPTO_CANDLES_MAX_ATTEMPTS", "") or 3)
        except ValueError:
            max_attempts = 3
    max_attempts = max(1, int(max_attempts))
    if retry_base_delay_s is None:
        try:
            retry_base_delay_s = float(
                os.getenv("CRYPTO_CANDLES_RETRY_BASE_DELAY_SECONDS", "") or 0.5
            )
        except ValueError:
            retry_base_delay_s = 0.5
    retry_base_delay_s = max(0.0, min(float(retry_base_delay_s), 10.0))
    return CryptoCandlesSettings(
        exchange_id=exchange_id,
        market_type=market_type,
        symbol=symbol,
        timeframes=tuple(timeframes),
        store_dir=store_dir,
        fetch_budget_s=fetch_budget_s,
        request_timeout_s=request_timeout_s,
        async_grace_s=async_grace_s,
        page_limit=page_limit,
        max_pages=max_pages,
        max_empty_pages=max_empty_pages,
        max_attempts=max_attempts,
        retry_base_delay_s=retry_base_delay_s,
    )


class CryptoTaSettings(NamedTuple):
    """Effective chart-reading knobs, resolved at call time (crypto TA Wave 1)."""

    symbol: str
    base_timeframe: str
    closed_only: bool
    recursive_warmup_multiplier: float
    max_staleness_bars: int
    max_context_chars: int
    async_timeout_s: float


#: Recursive smoothing never forgets its seed, it only decays it. At this
#: multiple of an indicator's nominal window the seed carries roughly
#: ``e ** -5`` (~0.7%) of the answer, which is where the number stops depending
#: on how deep the fetch happened to go. Raise it after freqtrade's
#: ``optimize/analysis/recursive.py`` measures the real floor on live candles.
CRYPTO_TA_DEFAULT_WARMUP_MULT = 5.0


def get_crypto_ta_settings(
    symbol: str | None = None,
    base_timeframe: str | None = None,
    closed_only: bool | None = None,
    recursive_warmup_multiplier: float | None = None,
    max_staleness_bars: int | None = None,
    max_context_chars: int | None = None,
    async_timeout_s: float | None = None,
) -> CryptoTaSettings:
    """Resolve the chart reader's knobs at CALL TIME (Rule 1).

    - ``CRYPTO_TA_SYMBOL`` — defaults to the candle store's symbol
    - ``CRYPTO_TA_BASE_TIMEFRAME`` (``5m``) — the frame slower frames merge onto
    - ``CRYPTO_TA_CLOSED_ONLY`` (``true``) — serve closed candles only; the
      forming candle's indicators change every second and cannot be
      cross-checked against a chart
    - ``CRYPTO_TA_RECURSIVE_WARMUP_MULT`` (``5``, clamped ``1``-``50``) —
      warm-up multiple for recursive indicators (RSI, EMA, MACD, ATR, ADX, KDJ)
    - ``CRYPTO_TA_MAX_STALENESS_BARS`` (``3``, ``0`` disables) — how far behind
      the read instant the newest closed candle may be before the read refuses
      rather than quoting an old number as current
    - ``CRYPTO_TA_MAX_CHARS`` (``3000``, floor ``512``) — prefetch payload cap
    - ``CRYPTO_TA_ASYNC_TIMEOUT_SECONDS`` (``5``, clamped ``0.5``-``60``)
    """

    if symbol is None:
        symbol = (
            os.getenv("CRYPTO_TA_SYMBOL", "").strip()
            or get_crypto_candles_settings().symbol
        )
    if base_timeframe is None:
        base_timeframe = os.getenv("CRYPTO_TA_BASE_TIMEFRAME", "").strip() or "5m"
    if closed_only is None:
        closed_only = os.getenv("CRYPTO_TA_CLOSED_ONLY", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    if recursive_warmup_multiplier is None:
        try:
            recursive_warmup_multiplier = float(
                os.getenv("CRYPTO_TA_RECURSIVE_WARMUP_MULT", "")
                or CRYPTO_TA_DEFAULT_WARMUP_MULT
            )
        except ValueError:
            recursive_warmup_multiplier = CRYPTO_TA_DEFAULT_WARMUP_MULT
    # Floor of 1.0: the multiplier may only ever RAISE the warm-up bar. A value
    # below 1 would publish an indicator before it had enough candles to exist.
    recursive_warmup_multiplier = max(1.0, min(float(recursive_warmup_multiplier), 50.0))
    if max_staleness_bars is None:
        try:
            max_staleness_bars = int(os.getenv("CRYPTO_TA_MAX_STALENESS_BARS", "") or 3)
        except ValueError:
            max_staleness_bars = 3
    max_staleness_bars = max(0, int(max_staleness_bars))
    if max_context_chars is None:
        try:
            max_context_chars = int(os.getenv("CRYPTO_TA_MAX_CHARS", "") or 3000)
        except ValueError:
            max_context_chars = 3000
    max_context_chars = max(512, int(max_context_chars))
    if async_timeout_s is None:
        try:
            async_timeout_s = float(
                os.getenv("CRYPTO_TA_ASYNC_TIMEOUT_SECONDS", "") or 5.0
            )
        except ValueError:
            async_timeout_s = 5.0
    async_timeout_s = max(0.5, min(float(async_timeout_s), 60.0))
    return CryptoTaSettings(
        symbol=symbol,
        base_timeframe=base_timeframe,
        closed_only=closed_only,
        recursive_warmup_multiplier=recursive_warmup_multiplier,
        max_staleness_bars=max_staleness_bars,
        max_context_chars=max_context_chars,
        async_timeout_s=async_timeout_s,
    )


class CryptoLevelsSettings(NamedTuple):
    """Effective swing/fib/CME-gap knobs, resolved at call time (crypto TA Wave 2)."""

    swing_left: int
    swing_right: int
    fib_ratios: tuple[float, ...]
    price_tick: float
    cme_timezone: str
    cme_close_weekday: int
    cme_close_hour: int
    cme_close_minute: int
    cme_open_weekday: int
    cme_open_hour: int
    cme_open_minute: int
    cme_lookback_weeks: int
    boundary_tolerance_spans: float
    stale_after_spans: float
    async_timeout_s: float


#: TradingView's default retracement set minus the 0/1 anchors, which the level
#: builder always emits separately as the swing endpoints.
CRYPTO_LEVELS_DEFAULT_FIB_RATIOS = (0.236, 0.382, 0.5, 0.618, 0.786)
#: CME Globex crypto-futures week: closes Friday 16:00 America/Chicago, reopens
#: Sunday 17:00. Both are WALL-CLOCK instants in a DST-observing zone, so the
#: UTC offset moves by an hour twice a year. Weekday numbers are Python's
#: ``date.weekday()`` (Monday=0).
CRYPTO_LEVELS_CME_TIMEZONE = "America/Chicago"
CRYPTO_LEVELS_CME_CLOSE_WEEKDAY = 4
CRYPTO_LEVELS_CME_OPEN_WEEKDAY = 6


def get_crypto_levels_settings(
    swing_left: int | None = None,
    swing_right: int | None = None,
    fib_ratios: tuple[float, ...] | None = None,
    price_tick: float | None = None,
    cme_timezone: str | None = None,
    cme_close_weekday: int | None = None,
    cme_close_hour: int | None = None,
    cme_close_minute: int | None = None,
    cme_open_weekday: int | None = None,
    cme_open_hour: int | None = None,
    cme_open_minute: int | None = None,
    cme_lookback_weeks: int | None = None,
    boundary_tolerance_spans: float | None = None,
    stale_after_spans: float | None = None,
    async_timeout_s: float | None = None,
) -> CryptoLevelsSettings:
    """Resolve the level-builder's knobs at CALL TIME (Rule 1).

    Every endpoint below is arithmetic over a candle frame the caller already
    holds; nothing here reaches the network or carries a secret.

    - ``CRYPTO_LEVELS_SWING_LEFT`` (``3``) — bars a pivot must dominate behind it
    - ``CRYPTO_LEVELS_SWING_RIGHT`` (``3``) — bars a pivot must dominate ahead of
      it. This is also the CONFIRMATION LAG: a pivot is not confirmed until this
      many bars have printed after it, which is what keeps the detector out of
      the future.
    - ``CRYPTO_LEVELS_FIB_RATIOS`` (``0.236,0.382,0.5,0.618,0.786``)
    - ``CRYPTO_LEVELS_PRICE_TICK`` (``0.1``) — binance BTC USDT-perp price
      precision; the quote grid levels are rounded onto
    - ``CRYPTO_LEVELS_CME_TIMEZONE`` (``America/Chicago``)
    - ``CRYPTO_LEVELS_CME_CLOSE_WEEKDAY`` / ``_HOUR`` / ``_MINUTE`` (``4``/``16``/``0``)
    - ``CRYPTO_LEVELS_CME_OPEN_WEEKDAY`` / ``_HOUR`` / ``_MINUTE`` (``6``/``17``/``0``)
    - ``CRYPTO_LEVELS_CME_LOOKBACK_WEEKS`` (``8``) — session boundaries scanned
    - ``CRYPTO_LEVELS_BOUNDARY_TOLERANCE_SPANS`` (``1.0``) — how far a candle may
      sit from a session instant and still be read as that instant's price
    - ``CRYPTO_LEVELS_STALE_AFTER_SPANS`` (``3.0``) — a frame whose last candle
      is older than this many timeframe spans cannot support an UNFILLED
      verdict; the fill state degrades to UNKNOWN instead
    - ``CRYPTO_LEVELS_ASYNC_TIMEOUT_SECONDS`` (``5``) — off-loop deadline
    """

    if swing_left is None:
        try:
            swing_left = int(os.getenv("CRYPTO_LEVELS_SWING_LEFT", "") or 3)
        except ValueError:
            swing_left = 3
    swing_left = max(1, min(int(swing_left), 500))
    if swing_right is None:
        try:
            swing_right = int(os.getenv("CRYPTO_LEVELS_SWING_RIGHT", "") or 3)
        except ValueError:
            swing_right = 3
    swing_right = max(1, min(int(swing_right), 500))
    if fib_ratios is None:
        raw_ratios = os.getenv("CRYPTO_LEVELS_FIB_RATIOS", "").strip()
        parsed: list[float] = []
        for entry in raw_ratios.split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                value = float(entry)
            except ValueError:
                continue
            if 0.0 < value < 1.0:
                parsed.append(value)
        fib_ratios = tuple(sorted(set(parsed))) or CRYPTO_LEVELS_DEFAULT_FIB_RATIOS
    if price_tick is None:
        try:
            price_tick = float(os.getenv("CRYPTO_LEVELS_PRICE_TICK", "") or 0.1)
        except ValueError:
            price_tick = 0.1
    price_tick = float(price_tick)
    if not price_tick > 0.0:
        price_tick = 0.1
    if cme_timezone is None:
        cme_timezone = (
            os.getenv("CRYPTO_LEVELS_CME_TIMEZONE", "").strip()
            or CRYPTO_LEVELS_CME_TIMEZONE
        )
    if cme_close_weekday is None:
        try:
            cme_close_weekday = int(
                os.getenv("CRYPTO_LEVELS_CME_CLOSE_WEEKDAY", "")
                or CRYPTO_LEVELS_CME_CLOSE_WEEKDAY
            )
        except ValueError:
            cme_close_weekday = CRYPTO_LEVELS_CME_CLOSE_WEEKDAY
    if cme_open_weekday is None:
        try:
            cme_open_weekday = int(
                os.getenv("CRYPTO_LEVELS_CME_OPEN_WEEKDAY", "")
                or CRYPTO_LEVELS_CME_OPEN_WEEKDAY
            )
        except ValueError:
            cme_open_weekday = CRYPTO_LEVELS_CME_OPEN_WEEKDAY
    cme_close_weekday = int(cme_close_weekday) % 7
    cme_open_weekday = int(cme_open_weekday) % 7
    if cme_close_hour is None:
        try:
            cme_close_hour = int(os.getenv("CRYPTO_LEVELS_CME_CLOSE_HOUR", "") or 16)
        except ValueError:
            cme_close_hour = 16
    if cme_close_minute is None:
        try:
            cme_close_minute = int(os.getenv("CRYPTO_LEVELS_CME_CLOSE_MINUTE", "") or 0)
        except ValueError:
            cme_close_minute = 0
    if cme_open_hour is None:
        try:
            cme_open_hour = int(os.getenv("CRYPTO_LEVELS_CME_OPEN_HOUR", "") or 17)
        except ValueError:
            cme_open_hour = 17
    if cme_open_minute is None:
        try:
            cme_open_minute = int(os.getenv("CRYPTO_LEVELS_CME_OPEN_MINUTE", "") or 0)
        except ValueError:
            cme_open_minute = 0
    cme_close_hour = max(0, min(int(cme_close_hour), 23))
    cme_close_minute = max(0, min(int(cme_close_minute), 59))
    cme_open_hour = max(0, min(int(cme_open_hour), 23))
    cme_open_minute = max(0, min(int(cme_open_minute), 59))
    if cme_lookback_weeks is None:
        try:
            cme_lookback_weeks = int(
                os.getenv("CRYPTO_LEVELS_CME_LOOKBACK_WEEKS", "") or 8
            )
        except ValueError:
            cme_lookback_weeks = 8
    cme_lookback_weeks = max(1, min(int(cme_lookback_weeks), 260))
    if boundary_tolerance_spans is None:
        try:
            boundary_tolerance_spans = float(
                os.getenv("CRYPTO_LEVELS_BOUNDARY_TOLERANCE_SPANS", "") or 1.0
            )
        except ValueError:
            boundary_tolerance_spans = 1.0
    boundary_tolerance_spans = max(0.0, min(float(boundary_tolerance_spans), 100.0))
    if stale_after_spans is None:
        try:
            stale_after_spans = float(
                os.getenv("CRYPTO_LEVELS_STALE_AFTER_SPANS", "") or 3.0
            )
        except ValueError:
            stale_after_spans = 3.0
    stale_after_spans = max(1.0, min(float(stale_after_spans), 1000.0))
    if async_timeout_s is None:
        try:
            async_timeout_s = float(
                os.getenv("CRYPTO_LEVELS_ASYNC_TIMEOUT_SECONDS", "") or 5.0
            )
        except ValueError:
            async_timeout_s = 5.0
    async_timeout_s = max(0.5, min(float(async_timeout_s), 60.0))
    return CryptoLevelsSettings(
        swing_left=swing_left,
        swing_right=swing_right,
        fib_ratios=tuple(fib_ratios),
        price_tick=price_tick,
        cme_timezone=cme_timezone,
        cme_close_weekday=cme_close_weekday,
        cme_close_hour=cme_close_hour,
        cme_close_minute=cme_close_minute,
        cme_open_weekday=cme_open_weekday,
        cme_open_hour=cme_open_hour,
        cme_open_minute=cme_open_minute,
        cme_lookback_weeks=cme_lookback_weeks,
        boundary_tolerance_spans=boundary_tolerance_spans,
        stale_after_spans=stale_after_spans,
        async_timeout_s=async_timeout_s,
    )


class CryptoLeverageSettings(NamedTuple):
    """Effective liquidation/sizing knobs, resolved at call time (crypto TA Wave 3)."""

    tiers_path: str
    tiers_cache_enabled: bool
    margin_mode: str
    default_notional: float
    default_risk_fraction: float
    price_increment: float
    size_increment: float
    async_timeout_s: float


#: Vendored freqtrade artifact (`freqtrade/exchange/binance_leverage_tiers.json`,
#: 846 pairs). Local file, no key, no network — the whole point of Wave 3.
CRYPTO_LEVERAGE_TIERS_FILENAME = "binance_leverage_tiers.json"
#: Isolated is the only mode the local math can answer honestly. Cross needs the
#: mark price and maintenance margin of every OTHER open position in the wallet,
#: and this framework holds no position book.
CRYPTO_LEVERAGE_SUPPORTED_MARGIN_MODES = ("isolated",)


def get_crypto_leverage_settings(
    tiers_path: str | None = None,
    tiers_cache_enabled: bool | None = None,
    margin_mode: str | None = None,
    default_notional: float | None = None,
    default_risk_fraction: float | None = None,
    price_increment: float | None = None,
    size_increment: float | None = None,
    async_timeout_s: float | None = None,
) -> CryptoLeverageSettings:
    """Resolve the leverage-math knobs at CALL TIME (Rule 1).

    Nothing here reaches the network and nothing here can place an order: the
    maintenance-margin table is a local JSON file and every function is
    arithmetic over it.

    - ``CRYPTO_LEVERAGE_TIERS_PATH`` (``.claude/scripts/data/binance_leverage_tiers.json``)
    - ``CRYPTO_LEVERAGE_TIERS_CACHE_ENABLED`` (``true``) — the parsed table is
      re-validated against the file's physical ``(mtime_ns, size)`` on every
      read, so a stale cache cannot outlive an edited table (Rule 2)
    - ``CRYPTO_LEVERAGE_MARGIN_MODE`` (``isolated``) — anything else refuses
    - ``CRYPTO_LEVERAGE_DEFAULT_NOTIONAL`` (``10000``) — position notional used
      to pick a maintenance tier when the caller names no size; the result
      flags that the notional was assumed
    - ``CRYPTO_LEVERAGE_DEFAULT_RISK_FRACTION`` (``0.01``) — 1% of equity
    - ``CRYPTO_LEVERAGE_PRICE_INCREMENT`` (``0.1``) — BTC/USDT perp tick
    - ``CRYPTO_LEVERAGE_SIZE_INCREMENT`` (``0.001``) — BTC/USDT perp lot
    - ``CRYPTO_LEVERAGE_ASYNC_TIMEOUT_SECONDS`` (``5``)
    """

    if tiers_path is None:
        tiers_path = os.getenv("CRYPTO_LEVERAGE_TIERS_PATH", "").strip() or str(
            SCRIPTS_DIR / "data" / CRYPTO_LEVERAGE_TIERS_FILENAME
        )
    if tiers_cache_enabled is None:
        tiers_cache_enabled = (
            os.getenv("CRYPTO_LEVERAGE_TIERS_CACHE_ENABLED", "true").strip().lower()
            != "false"
        )
    if margin_mode is None:
        margin_mode = (
            os.getenv("CRYPTO_LEVERAGE_MARGIN_MODE", "").strip().lower() or "isolated"
        )
    if default_notional is None:
        try:
            default_notional = float(
                os.getenv("CRYPTO_LEVERAGE_DEFAULT_NOTIONAL", "") or 10_000.0
            )
        except ValueError:
            default_notional = 10_000.0
    default_notional = max(1.0, float(default_notional))
    if default_risk_fraction is None:
        try:
            default_risk_fraction = float(
                os.getenv("CRYPTO_LEVERAGE_DEFAULT_RISK_FRACTION", "") or 0.01
            )
        except ValueError:
            default_risk_fraction = 0.01
    default_risk_fraction = max(0.0001, min(float(default_risk_fraction), 1.0))
    if price_increment is None:
        try:
            price_increment = float(
                os.getenv("CRYPTO_LEVERAGE_PRICE_INCREMENT", "") or 0.1
            )
        except ValueError:
            price_increment = 0.1
    price_increment = max(1e-12, float(price_increment))
    if size_increment is None:
        try:
            size_increment = float(
                os.getenv("CRYPTO_LEVERAGE_SIZE_INCREMENT", "") or 0.001
            )
        except ValueError:
            size_increment = 0.001
    size_increment = max(0.0, float(size_increment))
    if async_timeout_s is None:
        try:
            async_timeout_s = float(
                os.getenv("CRYPTO_LEVERAGE_ASYNC_TIMEOUT_SECONDS", "") or 5.0
            )
        except ValueError:
            async_timeout_s = 5.0
    async_timeout_s = max(0.5, min(float(async_timeout_s), 60.0))
    return CryptoLeverageSettings(
        tiers_path=tiers_path,
        tiers_cache_enabled=tiers_cache_enabled,
        margin_mode=margin_mode,
        default_notional=default_notional,
        default_risk_fraction=default_risk_fraction,
        price_increment=price_increment,
        size_increment=size_increment,
        async_timeout_s=async_timeout_s,
    )



class CryptoDeskPriceSettings(NamedTuple):
    """Effective desk-prefetch price-block knobs, resolved at call time."""

    enabled: bool
    timeframes: tuple[str, ...]
    max_chars: int
    fetch_budget_s: float
    prefetch_timeout_s: float
    fib_timeframe: str
    gap_timeframe: str


def get_crypto_desk_price_settings(
    enabled: bool | None = None,
    timeframes: tuple[str, ...] | None = None,
    max_chars: int | None = None,
    fetch_budget_s: float | None = None,
    prefetch_timeout_s: float | None = None,
    fib_timeframe: str | None = None,
    gap_timeframe: str | None = None,
) -> CryptoDeskPriceSettings:
    """Resolve the desk snapshot's price block at CALL TIME (Rule 1).

    - ``CRYPTO_DESK_PRICE_ENABLED`` (``true``) — attach the live price block
    - ``CRYPTO_DESK_PRICE_TIMEFRAMES`` (``5m,15m,1h``) — frames to read
    - ``CRYPTO_DESK_PRICE_MAX_CHARS`` (``4000``, floor ``1200``) — block cap;
      the floor is what lets the fib/gap/liquidation reservation fit
    - ``CRYPTO_DESK_PRICE_FETCH_BUDGET_SECONDS`` (``6``) — per-frame fetch budget
    - ``CRYPTO_DESK_PREFETCH_TIMEOUT_SECONDS`` (``25``, clamped ``1``-``120``) —
      the HARD deadline the router applies to the whole off-loop snapshot
    - ``CRYPTO_DESK_PRICE_FIB_TIMEFRAME`` (``1h``) — frame the fib anchors read
    - ``CRYPTO_DESK_PRICE_GAP_TIMEFRAME`` (``1h``) — frame the CME scan reads
    """

    if enabled is None:
        enabled = os.getenv("CRYPTO_DESK_PRICE_ENABLED", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    if timeframes is None:
        raw = os.getenv("CRYPTO_DESK_PRICE_TIMEFRAMES", "").strip()
        parsed = tuple(part.strip() for part in raw.split(",") if part.strip())
        timeframes = parsed or ("5m", "15m", "1h")
    if max_chars is None:
        try:
            max_chars = int(os.getenv("CRYPTO_DESK_PRICE_MAX_CHARS", "") or 4000)
        except ValueError:
            max_chars = 4000
    max_chars = max(1_200, int(max_chars))
    if fetch_budget_s is None:
        try:
            fetch_budget_s = float(
                os.getenv("CRYPTO_DESK_PRICE_FETCH_BUDGET_SECONDS", "") or 6.0
            )
        except ValueError:
            fetch_budget_s = 6.0
    fetch_budget_s = max(0.5, min(float(fetch_budget_s), CRYPTO_CANDLES_BUDGET_CEILING_S))
    if prefetch_timeout_s is None:
        try:
            prefetch_timeout_s = float(
                os.getenv("CRYPTO_DESK_PREFETCH_TIMEOUT_SECONDS", "") or 25.0
            )
        except ValueError:
            prefetch_timeout_s = 25.0
    prefetch_timeout_s = max(1.0, min(float(prefetch_timeout_s), 120.0))
    if fib_timeframe is None:
        fib_timeframe = os.getenv("CRYPTO_DESK_PRICE_FIB_TIMEFRAME", "").strip() or "1h"
    if gap_timeframe is None:
        gap_timeframe = os.getenv("CRYPTO_DESK_PRICE_GAP_TIMEFRAME", "").strip() or "1h"
    return CryptoDeskPriceSettings(
        enabled=enabled,
        timeframes=tuple(timeframes),
        max_chars=max_chars,
        fetch_budget_s=fetch_budget_s,
        prefetch_timeout_s=prefetch_timeout_s,
        fib_timeframe=fib_timeframe,
        gap_timeframe=gap_timeframe,
    )


class CryptoReflectionSettings(NamedTuple):
    """Effective Wave-4 reflection knobs, resolved at call time."""

    enabled: bool
    min_graded_plays: int
    max_lessons: int
    max_values: int
    max_lesson_chars: int
    retrieval_limit: int
    min_similarity: float
    anchor_price_field: str
    anchor_at_field: str
    async_timeout_s: float


def get_crypto_reflection_settings(
    enabled: bool | None = None,
    min_graded_plays: int | None = None,
    max_lessons: int | None = None,
    max_values: int | None = None,
    max_lesson_chars: int | None = None,
    retrieval_limit: int | None = None,
    min_similarity: float | None = None,
    anchor_price_field: str | None = None,
    anchor_at_field: str | None = None,
    async_timeout_s: float | None = None,
) -> CryptoReflectionSettings:
    """Resolve the per-role reflection knobs at CALL TIME (Rule 1).

    Nothing here reaches the network, a database, or a model. The pass is
    arithmetic plus string assembly over rows the caller already read.

    - ``CRYPTO_REFLECTION_ENABLED`` (``true``) — default-ON; only turns the
      faculty OFF, and OFF renders as the explicit UNKNOWN state
    - ``CRYPTO_REFLECTION_MIN_GRADED_PLAYS`` (``5``, floor ``1``) — below this
      the pass answers INSUFFICIENT_HISTORY instead of drawing a lesson from a
      handful of grades
    - ``CRYPTO_REFLECTION_MAX_LESSONS`` (``50``, clamped ``1``-``500``)
    - ``CRYPTO_REFLECTION_MAX_VALUES`` (``64``, clamped ``1``-``512``) — cap on
      the condition/formula values one entry snapshot may freeze
    - ``CRYPTO_REFLECTION_MAX_LESSON_CHARS`` (``320``, clamped ``40``-``2000``)
    - ``CRYPTO_REFLECTION_RETRIEVAL_LIMIT`` (``3``, clamped ``1``-``20``)
    - ``CRYPTO_REFLECTION_MIN_SIMILARITY`` (``0.15``, clamped ``0``-``1``)
    - ``CRYPTO_REFLECTION_ANCHOR_PRICE_FIELD`` (``price_at_call_usd``) — the
      ledger column holding the call-time price. ``cognition.crypto_plays``
      owns that column; this knob follows a rename without a code change
    - ``CRYPTO_REFLECTION_ANCHOR_AT_FIELD`` (``price_at_call_at``)
    - ``CRYPTO_REFLECTION_ASYNC_TIMEOUT_SECONDS`` (``10``, clamped
      ``0.5``-``120``)
    """

    if enabled is None:
        enabled = os.getenv(
            "CRYPTO_REFLECTION_ENABLED", "true"
        ).strip().lower() not in {"0", "false", "no", "off"}
    if min_graded_plays is None:
        try:
            min_graded_plays = int(
                os.getenv("CRYPTO_REFLECTION_MIN_GRADED_PLAYS", "") or 5
            )
        except ValueError:
            min_graded_plays = 5
    min_graded_plays = max(1, int(min_graded_plays))
    if max_lessons is None:
        try:
            max_lessons = int(os.getenv("CRYPTO_REFLECTION_MAX_LESSONS", "") or 50)
        except ValueError:
            max_lessons = 50
    max_lessons = max(1, min(int(max_lessons), 500))
    if max_values is None:
        try:
            max_values = int(os.getenv("CRYPTO_REFLECTION_MAX_VALUES", "") or 64)
        except ValueError:
            max_values = 64
    max_values = max(1, min(int(max_values), 512))
    if max_lesson_chars is None:
        try:
            max_lesson_chars = int(
                os.getenv("CRYPTO_REFLECTION_MAX_LESSON_CHARS", "") or 320
            )
        except ValueError:
            max_lesson_chars = 320
    max_lesson_chars = max(40, min(int(max_lesson_chars), 2_000))
    if retrieval_limit is None:
        try:
            retrieval_limit = int(
                os.getenv("CRYPTO_REFLECTION_RETRIEVAL_LIMIT", "") or 3
            )
        except ValueError:
            retrieval_limit = 3
    retrieval_limit = max(1, min(int(retrieval_limit), 20))
    if min_similarity is None:
        try:
            min_similarity = float(
                os.getenv("CRYPTO_REFLECTION_MIN_SIMILARITY", "") or 0.15
            )
        except ValueError:
            min_similarity = 0.15
    if not math.isfinite(float(min_similarity)):
        min_similarity = 0.15
    min_similarity = max(0.0, min(float(min_similarity), 1.0))
    if anchor_price_field is None:
        anchor_price_field = (
            os.getenv("CRYPTO_REFLECTION_ANCHOR_PRICE_FIELD", "").strip()
            or "price_at_call_usd"
        )
    if anchor_at_field is None:
        anchor_at_field = (
            os.getenv("CRYPTO_REFLECTION_ANCHOR_AT_FIELD", "").strip()
            or "price_at_call_at"
        )
    if async_timeout_s is None:
        try:
            async_timeout_s = float(
                os.getenv("CRYPTO_REFLECTION_ASYNC_TIMEOUT_SECONDS", "") or 10.0
            )
        except ValueError:
            async_timeout_s = 10.0
    if not math.isfinite(float(async_timeout_s)):
        async_timeout_s = 10.0
    async_timeout_s = max(0.5, min(float(async_timeout_s), 120.0))
    return CryptoReflectionSettings(
        enabled=enabled,
        min_graded_plays=min_graded_plays,
        max_lessons=max_lessons,
        max_values=max_values,
        max_lesson_chars=max_lesson_chars,
        retrieval_limit=retrieval_limit,
        min_similarity=min_similarity,
        anchor_price_field=anchor_price_field,
        anchor_at_field=anchor_at_field,
        async_timeout_s=async_timeout_s,
    )


class CryptoShadowSettings(NamedTuple):
    """Effective Shadow-Account attribution knobs, resolved at call time."""

    enabled: bool
    min_settled: int
    allowed_per_signal: int
    residual_tolerance_usd: float
    max_items: int
    max_chars: int
    async_timeout_s: float


def get_crypto_shadow_settings(
    enabled: bool | None = None,
    min_settled: int | None = None,
    allowed_per_signal: int | None = None,
    residual_tolerance_usd: float | None = None,
    max_items: int | None = None,
    max_chars: int | None = None,
    async_timeout_s: float | None = None,
) -> CryptoShadowSettings:
    """Resolve the delta-PnL attribution knobs at CALL TIME (Rule 1).

    - ``CRYPTO_SHADOW_ENABLED`` (``true``) — default-ON; only turns the
      attribution OFF, and OFF renders as the explicit UNKNOWN state
    - ``CRYPTO_SHADOW_MIN_SETTLED`` (``10``, floor ``1``) — below this the
      attribution answers INSUFFICIENT_SAMPLE rather than five tidy buckets
      over a handful of trades
    - ``CRYPTO_SHADOW_ALLOWED_PER_SIGNAL`` (``1``, floor ``1``) — entries one
      signal may carry before the surplus counts as overtrading
    - ``CRYPTO_SHADOW_RESIDUAL_TOLERANCE_USD`` (``0.01``, floor ``1e-9``) —
      how far the residual may sit from the direct missed total before the
      decomposition is declared broken
    - ``CRYPTO_SHADOW_MAX_ITEMS`` (``500``, clamped ``1``-``5000``)
    - ``CRYPTO_SHADOW_MAX_CHARS`` (``1200``, floor ``200``)
    - ``CRYPTO_SHADOW_ASYNC_TIMEOUT_SECONDS`` (``10``, clamped ``0.5``-``120``)
    """

    if enabled is None:
        enabled = os.getenv(
            "CRYPTO_SHADOW_ENABLED", "true"
        ).strip().lower() not in {"0", "false", "no", "off"}
    if min_settled is None:
        try:
            min_settled = int(os.getenv("CRYPTO_SHADOW_MIN_SETTLED", "") or 10)
        except ValueError:
            min_settled = 10
    min_settled = max(1, int(min_settled))
    if allowed_per_signal is None:
        try:
            allowed_per_signal = int(
                os.getenv("CRYPTO_SHADOW_ALLOWED_PER_SIGNAL", "") or 1
            )
        except ValueError:
            allowed_per_signal = 1
    allowed_per_signal = max(1, int(allowed_per_signal))
    if residual_tolerance_usd is None:
        try:
            residual_tolerance_usd = float(
                os.getenv("CRYPTO_SHADOW_RESIDUAL_TOLERANCE_USD", "") or 0.01
            )
        except ValueError:
            residual_tolerance_usd = 0.01
    if not math.isfinite(float(residual_tolerance_usd)):
        residual_tolerance_usd = 0.01
    residual_tolerance_usd = max(1e-9, float(residual_tolerance_usd))
    if max_items is None:
        try:
            max_items = int(os.getenv("CRYPTO_SHADOW_MAX_ITEMS", "") or 500)
        except ValueError:
            max_items = 500
    max_items = max(1, min(int(max_items), 5_000))
    if max_chars is None:
        try:
            max_chars = int(os.getenv("CRYPTO_SHADOW_MAX_CHARS", "") or 1200)
        except ValueError:
            max_chars = 1200
    max_chars = max(200, int(max_chars))
    if async_timeout_s is None:
        try:
            async_timeout_s = float(
                os.getenv("CRYPTO_SHADOW_ASYNC_TIMEOUT_SECONDS", "") or 10.0
            )
        except ValueError:
            async_timeout_s = 10.0
    if not math.isfinite(float(async_timeout_s)):
        async_timeout_s = 10.0
    async_timeout_s = max(0.5, min(float(async_timeout_s), 120.0))
    return CryptoShadowSettings(
        enabled=enabled,
        min_settled=min_settled,
        allowed_per_signal=allowed_per_signal,
        residual_tolerance_usd=residual_tolerance_usd,
        max_items=max_items,
        max_chars=max_chars,
        async_timeout_s=async_timeout_s,
    )


class CryptoPaperSettings(NamedTuple):
    """Effective paper-ladder knobs, resolved at call time (crypto TA Wave 7)."""

    armed_tiers: tuple[str, ...]
    exchange_id: str
    market_type: str
    symbol: str
    credential_env: str
    api_key_env_var: str
    api_secret_env_var: str
    book_depth: int
    book_max_age_s: float
    slippage_cap_bps: float
    fetch_budget_s: float
    request_timeout_s: float
    async_grace_s: float
    max_attempts: int
    retry_base_delay_s: float
    db_path: str


#: Every rung ships DISARMED. Arming a rung is the explicit named gate that the
#: default-deny mutation policy requires, and the default is the empty set.
CRYPTO_PAPER_TIER_NAMES = ("validate_only", "sandbox", "demo_real_prices")
#: Credential environments the ladder recognises. ``none`` is the default and
#: means no key exists, which every rung reports as `credentials-missing`
#: rather than pretending the rung was exercised.
CRYPTO_PAPER_CREDENTIAL_ENVS = ("none", "live", "testnet", "demo")


def get_crypto_paper_settings(
    armed_tiers: tuple[str, ...] | None = None,
    exchange_id: str | None = None,
    market_type: str | None = None,
    symbol: str | None = None,
    credential_env: str | None = None,
    api_key_env_var: str | None = None,
    api_secret_env_var: str | None = None,
    book_depth: int | None = None,
    book_max_age_s: float | None = None,
    slippage_cap_bps: float | None = None,
    fetch_budget_s: float | None = None,
    request_timeout_s: float | None = None,
    async_grace_s: float | None = None,
    max_attempts: int | None = None,
    retry_base_delay_s: float | None = None,
    db_path: str | None = None,
) -> CryptoPaperSettings:
    """Resolve the paper-ladder knobs at CALL TIME (Rule 1).

    No knob here holds a secret. ``CRYPTO_PAPER_API_KEY_ENV_VAR`` names the
    variable a future execution client would read; the value never passes
    through this module, the ladder, or any receipt.

    - ``CRYPTO_PAPER_ARMED_TIERS`` ("" — DISARMED) — comma-separated subset of
      ``validate_only,sandbox,demo_real_prices``. Unset means every preflight
      DENIES; this is the default-deny gate for the one part of Wave 7 that
      can reach a venue.
    - ``CRYPTO_PAPER_EXCHANGE`` (``binance``) — ccxt exchange id
    - ``CRYPTO_PAPER_MARKET_TYPE`` (``swap``) — ccxt ``defaultType``
    - ``CRYPTO_PAPER_SYMBOL`` (``BTC/USDT:USDT``)
    - ``CRYPTO_PAPER_CREDENTIAL_ENV`` (``none``) — which world the configured
      credential belongs to. A mismatch against the resolved HOST class is
      refused before any request is built.
    - ``CRYPTO_PAPER_API_KEY_ENV_VAR`` / ``CRYPTO_PAPER_API_SECRET_ENV_VAR``
      (``CRYPTO_PAPER_API_KEY`` / ``CRYPTO_PAPER_API_SECRET``) — variable NAMES
    - ``CRYPTO_PAPER_BOOK_DEPTH`` (``50``, clamped 1-1000) — L2 levels fetched
    - ``CRYPTO_PAPER_BOOK_MAX_AGE_SECONDS`` (``10``; ``0`` disables) — a book
      older than this prices a market that has already moved
    - ``CRYPTO_PAPER_SLIPPAGE_CAP_BPS`` (``0`` — OFF) — freqtrade's flattering
      clamp on the walked fill. Default-off on purpose (backtrader's rule:
      name the unrealistic behaviour and ship it disabled); when it bites, the
      receipt carries both the capped and the uncapped price.
    - ``CRYPTO_PAPER_FETCH_BUDGET_SECONDS`` (``8``, ceiling ``30``)
    - ``CRYPTO_PAPER_REQUEST_TIMEOUT_SECONDS`` (``6``)
    - ``CRYPTO_PAPER_ASYNC_GRACE_SECONDS`` (``3``)
    - ``CRYPTO_PAPER_MAX_ATTEMPTS`` (``3``) — transient retries per fetch
    - ``CRYPTO_PAPER_RETRY_BASE_DELAY_SECONDS`` (``0.5``)
    - ``CRYPTO_PAPER_DB_PATH`` (``DATA_DIR/crypto_paper.db``)
    """

    if armed_tiers is None:
        raw_armed = os.getenv("CRYPTO_PAPER_ARMED_TIERS", "").strip()
        armed_tiers = tuple(
            entry.strip().lower()
            for entry in raw_armed.split(",")
            if entry.strip()
        )
    # An unrecognised rung name never arms anything: it is dropped, so a typo
    # in the env fails CLOSED instead of arming an adjacent rung.
    armed_tiers = tuple(
        entry for entry in armed_tiers if entry in CRYPTO_PAPER_TIER_NAMES
    )
    if exchange_id is None:
        exchange_id = os.getenv("CRYPTO_PAPER_EXCHANGE", "").strip().lower() or "binance"
    if market_type is None:
        market_type = os.getenv("CRYPTO_PAPER_MARKET_TYPE", "").strip() or "swap"
    if symbol is None:
        symbol = (
            os.getenv("CRYPTO_PAPER_SYMBOL", "").strip()
            or CRYPTO_CANDLES_DEFAULT_SYMBOL
        )
    if credential_env is None:
        credential_env = (
            os.getenv("CRYPTO_PAPER_CREDENTIAL_ENV", "").strip().lower() or "none"
        )
    if credential_env not in CRYPTO_PAPER_CREDENTIAL_ENVS:
        print(
            f"[config] CRYPTO_PAPER_CREDENTIAL_ENV={credential_env!r} unknown; "
            "degrading to 'none'",
            flush=True,
        )
        credential_env = "none"
    if api_key_env_var is None:
        api_key_env_var = (
            os.getenv("CRYPTO_PAPER_API_KEY_ENV_VAR", "").strip()
            or "CRYPTO_PAPER_API_KEY"
        )
    if api_secret_env_var is None:
        api_secret_env_var = (
            os.getenv("CRYPTO_PAPER_API_SECRET_ENV_VAR", "").strip()
            or "CRYPTO_PAPER_API_SECRET"
        )
    if book_depth is None:
        try:
            book_depth = int(os.getenv("CRYPTO_PAPER_BOOK_DEPTH", "") or 50)
        except ValueError:
            book_depth = 50
    book_depth = max(1, min(int(book_depth), 1000))
    if book_max_age_s is None:
        try:
            book_max_age_s = float(
                os.getenv("CRYPTO_PAPER_BOOK_MAX_AGE_SECONDS", "") or 10.0
            )
        except ValueError:
            book_max_age_s = 10.0
    book_max_age_s = max(0.0, float(book_max_age_s))
    if slippage_cap_bps is None:
        try:
            slippage_cap_bps = float(
                os.getenv("CRYPTO_PAPER_SLIPPAGE_CAP_BPS", "") or 0.0
            )
        except ValueError:
            slippage_cap_bps = 0.0
    slippage_cap_bps = max(0.0, float(slippage_cap_bps))
    if fetch_budget_s is None:
        try:
            fetch_budget_s = float(
                os.getenv("CRYPTO_PAPER_FETCH_BUDGET_SECONDS", "") or 8.0
            )
        except ValueError:
            fetch_budget_s = 8.0
    # Same ceiling as the candle store: a hung exchange inside a persona
    # prefetch is the failure class that froze the bot's event loop.
    fetch_budget_s = max(1.0, min(float(fetch_budget_s), CRYPTO_CANDLES_BUDGET_CEILING_S))
    if request_timeout_s is None:
        try:
            request_timeout_s = float(
                os.getenv("CRYPTO_PAPER_REQUEST_TIMEOUT_SECONDS", "") or 6.0
            )
        except ValueError:
            request_timeout_s = 6.0
    request_timeout_s = max(0.5, min(float(request_timeout_s), fetch_budget_s))
    if async_grace_s is None:
        try:
            async_grace_s = float(
                os.getenv("CRYPTO_PAPER_ASYNC_GRACE_SECONDS", "") or 3.0
            )
        except ValueError:
            async_grace_s = 3.0
    async_grace_s = max(0.5, min(float(async_grace_s), 15.0))
    if max_attempts is None:
        try:
            max_attempts = int(os.getenv("CRYPTO_PAPER_MAX_ATTEMPTS", "") or 3)
        except ValueError:
            max_attempts = 3
    max_attempts = max(1, int(max_attempts))
    if retry_base_delay_s is None:
        try:
            retry_base_delay_s = float(
                os.getenv("CRYPTO_PAPER_RETRY_BASE_DELAY_SECONDS", "") or 0.5
            )
        except ValueError:
            retry_base_delay_s = 0.5
    retry_base_delay_s = max(0.0, min(float(retry_base_delay_s), 10.0))
    if db_path is None:
        db_path = os.getenv("CRYPTO_PAPER_DB_PATH", "").strip() or str(
            DATA_DIR / "crypto_paper.db"
        )
    return CryptoPaperSettings(
        armed_tiers=tuple(armed_tiers),
        exchange_id=exchange_id,
        market_type=market_type,
        symbol=symbol,
        credential_env=credential_env,
        api_key_env_var=api_key_env_var,
        api_secret_env_var=api_secret_env_var,
        book_depth=book_depth,
        book_max_age_s=book_max_age_s,
        slippage_cap_bps=slippage_cap_bps,
        fetch_budget_s=fetch_budget_s,
        request_timeout_s=request_timeout_s,
        async_grace_s=async_grace_s,
        max_attempts=max_attempts,
        retry_base_delay_s=retry_base_delay_s,
        db_path=db_path,
    )


# Process-lifetime flag for the one-time live-mode arming receipt (print-once
# observability — never consulted for behavior, so Rule 2 is untouched).
_CALLED_SHOTS_LIVE_RECEIPT_EMITTED = False


class CalledShotsChallengeSettings(NamedTuple):
    """Effective challenge-surface knobs (call-time resolved) — epic #186 T2."""

    mode: str  # "silent" (default — record candidates, no reply challenge) | "live"
    min_chars: int  # message-length floor for staked-position detection
    max_receipts: int  # recall hits gathered as receipts per detected position
    dedup_cache_size: int  # per-session recent-position cache cap
    receipts_timeout_s: float  # hard wall on the receipts recall leg (fired branch only)


def get_called_shots_challenge_settings(
    mode: str | None = None,
    min_chars: int | None = None,
    max_receipts: int | None = None,
    dedup_cache_size: int | None = None,
    receipts_timeout_s: float | None = None,
) -> CalledShotsChallengeSettings:
    """Resolve challenge-surface knobs at CALL TIME (Rule 1) — epic #186 T2.

    Knobs:
        CALLED_SHOTS_CHALLENGE_MODE ("silent") — "silent" records detected
            candidate shots (reviewable/voidable) with NO reply challenge; this
            is the architecture doc's Spike-2 measurement phase, default-ON.
            "live" arms the reply-challenge wire. The ARMING BAR is the
            architecture doc's Spike 2 as written — a historical-turn replay
            with a measured false-positive rate; the spike harness's bundled
            sample set is only a regression lock on the patterns, never the
            arming evidence. Any other value degrades to "silent" with a
            receipt.
        CALLED_SHOTS_CHALLENGE_MIN_CHARS (60, int) — detection length floor.
        CALLED_SHOTS_CHALLENGE_MAX_RECEIPTS (3, int) — receipts per position.
        CALLED_SHOTS_CHALLENGE_DEDUP_CACHE (16, int) — per-session cache cap.
        CALLED_SHOTS_CHALLENGE_RECEIPTS_TIMEOUT_S (3.0, float) — hard wall on
            the receipts recall leg; the gather runs ONLY inside the pass's
            fired branch under its own asyncio.wait_for (gate-closed turns
            never gather).
    Malformed numeric envs degrade to defaults with a receipt (never raise —
    the challenge surface must stay fail-open end to end).
    """
    if mode is None:
        mode = os.getenv("CALLED_SHOTS_CHALLENGE_MODE", "silent").strip().lower()
    if mode not in ("silent", "live"):
        print(
            f"[config] CALLED_SHOTS_CHALLENGE_MODE={mode!r} unknown; "
            "degrading to 'silent'",
            flush=True,
        )
        mode = "silent"

    def _guarded_int(value: int | None, env: str, default: int) -> int:
        if value is not None:
            return value
        raw = os.getenv(env, str(default))
        try:
            return int(raw)
        except ValueError:
            print(
                f"[config] {env}={raw!r} is not an int; using {default}",
                flush=True,
            )
            return default

    if receipts_timeout_s is None:
        _raw_timeout = os.getenv("CALLED_SHOTS_CHALLENGE_RECEIPTS_TIMEOUT_S", "3.0")
        try:
            receipts_timeout_s = float(_raw_timeout)
        except ValueError:
            print(
                f"[config] CALLED_SHOTS_CHALLENGE_RECEIPTS_TIMEOUT_S="
                f"{_raw_timeout!r} is not a float; using 3.0",
                flush=True,
            )
            receipts_timeout_s = 3.0

    # One-time-per-process arming receipt (Kimi suggestion — observability,
    # not an interlock): live mode should announce itself and its bar once.
    global _CALLED_SHOTS_LIVE_RECEIPT_EMITTED
    if mode == "live" and not _CALLED_SHOTS_LIVE_RECEIPT_EMITTED:
        _CALLED_SHOTS_LIVE_RECEIPT_EMITTED = True
        print(
            "[config] CALLED_SHOTS_CHALLENGE_MODE=live — reply-challenge wire "
            "ARMED. Arming bar: the architecture doc's historical-turn replay "
            "(the bundled spike set is only a regression lock).",
            flush=True,
        )

    # Kimi L3: clamp to sane floors — degenerate values are intentional
    # decisions, not accidents (0 receipts would silently disarm live mode;
    # a 0 cache would disable dedup; a 0s timeout would starve every gather).
    return CalledShotsChallengeSettings(
        mode=mode,
        min_chars=_guarded_int(min_chars, "CALLED_SHOTS_CHALLENGE_MIN_CHARS", 60),
        max_receipts=max(1, _guarded_int(
            max_receipts, "CALLED_SHOTS_CHALLENGE_MAX_RECEIPTS", 3,
        )),
        dedup_cache_size=max(1, _guarded_int(
            dedup_cache_size, "CALLED_SHOTS_CHALLENGE_DEDUP_CACHE", 16,
        )),
        receipts_timeout_s=max(0.5, receipts_timeout_s),
    )


class CognitivePassSettings(NamedTuple):
    """Effective cognitive-pass knobs (call-time resolved) — Living Self Act 3."""

    enabled: bool
    fire_processes: frozenset[str]  # process VALUES that fire the pass (default {"planning"})
    min_chars: int  # message-length floor below which even a substantive turn stays one call
    max_actions_per_turn: int  # proactive-action cap per turn
    timeout_s: float  # hard wall on the monologue round-trip (M2)
    model: str  # processor model-tier hint for the monologue (F2; default "fast" = haiku)


def get_cognitive_pass_settings(
    enabled: bool | None = None,
    fire_processes: frozenset[str] | None = None,
    min_chars: int | None = None,
    max_actions_per_turn: int | None = None,
    timeout_s: float | None = None,
    model: str | None = None,
) -> CognitivePassSettings:
    """Resolve gated-cognitive-pass knobs at CALL TIME (Rule 1) — Living Self Act 3.

    Mirrors ``get_contradiction_settings`` / ``get_session_brief_settings``:
    every arg uses the None-sentinel pattern (explicit values pass through;
    ``None`` resolves the matching ``COGNITIVE_PASS_*`` env var inside the
    body), bool knobs via ``.lower() == "true"``. NONE of these become
    import-time globals — env overrides and ``monkeypatch.setenv`` take effect
    on the next call with no module reload. ``cognitive_pass`` reads this
    resolver at call time.

    Knobs:
        COGNITIVE_PASS_ENABLED ("true") — kill switch for the whole pass.
        COGNITIVE_PASS_FIRE_PROCESSES ("planning") — comma-separated process
            VALUES that fire the pass (parsed to a frozenset of lowercased,
            stripped, non-empty names). DEFAULT is never in it by construction,
            so the dominant trivial-turn case never fires. Widen to
            "planning,execution" etc. by env without a code change.
        COGNITIVE_PASS_MIN_CHARS (40) — message-length floor below which even a
            substantive-process turn stays one call (a belt against a short
            message that trips a process signal, e.g. "do it" -> EXECUTION).
        COGNITIVE_PASS_MAX_ACTIONS_PER_TURN (1) — cap on proactive actions
            queued per turn (rate-limits the default-deny operator_notification
            wire alongside the queue's own dedupe).
        COGNITIVE_PASS_TIMEOUT_S (5.0) — hard wall on the monologue round-trip
            (M2). The monologue is a real provider call; ``asyncio.wait_for``
            bounds it so a hung/slow provider times out -> bare turn, never a
            stalled reply. Tightened from 8.0 -> 5.0 (F6) now that F1+F2 make the
            monologue a cheap, budgeted haiku call (a ~23K-char append on the
            fast tier, not a ~90K-char append on the default tier) — a tighter
            ceiling better honors the cognition-budget intent; operator-tunable.
        COGNITIVE_PASS_MODEL ("fast") — the processor model-tier hint for the
            monologue (F2). ``"fast"`` maps to claude-haiku-4-5 via
            ``runtime_bridge._PROCESSOR_MODEL_HINTS``; a "think before replying"
            pass is a classic cheap-model job, so the default avoids the
            expensive reply profile that would ~2x the per-turn input cost.
            ``"claude"`` (default profile) / ``"quality"`` (sonnet) are the other
            documented tiers; operator-tunable.
    """
    if enabled is None:
        enabled = os.getenv("COGNITIVE_PASS_ENABLED", "true").lower() == "true"
    if fire_processes is None:
        raw = os.getenv("COGNITIVE_PASS_FIRE_PROCESSES", "planning")
        fire_processes = frozenset(
            part.strip().lower()
            for part in raw.split(",")
            if part.strip()
        )
    if min_chars is None:
        min_chars = int(os.getenv("COGNITIVE_PASS_MIN_CHARS", "40"))
    if max_actions_per_turn is None:
        max_actions_per_turn = int(os.getenv("COGNITIVE_PASS_MAX_ACTIONS_PER_TURN", "1"))
    if timeout_s is None:
        timeout_s = float(os.getenv("COGNITIVE_PASS_TIMEOUT_S", "5.0"))
    if model is None:
        model = os.getenv("COGNITIVE_PASS_MODEL", "fast").strip() or "fast"
    return CognitivePassSettings(
        enabled=enabled,
        fire_processes=fire_processes,
        min_chars=min_chars,
        max_actions_per_turn=max_actions_per_turn,
        timeout_s=timeout_s,
        model=model,
    )


def get_background_models(
    fast: str | None = None,
    quality: str | None = None,
) -> dict[str, str]:
    """Resolve cheap models for scheduled/background jobs at CALL TIME (Rule 1).

    Background jobs (heartbeat, daily reflection, weekly synthesis, dream) must
    NOT inherit the operator's interactive flagship model
    (``SECOND_BRAIN_CLAUDE_MODEL``, e.g. Opus). A cron job that reasons over
    pre-gathered data has no business burning Opus tokens ~48x/day. Two tiers:

        fast    — frequent/light jobs (the heartbeat family: reasoning pass,
                  alert formatter, HARO pitch). Default ``"haiku"``.
        quality — deep, infrequent synthesis (reflection, weekly, dream) that
                  rewrites durable memory. Default ``"sonnet"``.

    Lane note: these are Claude-lane model aliases applied via
    ``RuntimeRequest.model``. On generic lanes (Codex/Gemini) ``request.model``
    is ignored and the provider's own configured model is used — making those
    cheap per-lane is separate (provider-model env knobs / the pinned-fallback
    follow-up). None-sentinel args resolve the env at call time so
    ``monkeypatch.setenv`` / a live ``.env`` edit take effect with no reload.

    Knobs:
        SECOND_BRAIN_BACKGROUND_FAST_MODEL ("haiku")
        SECOND_BRAIN_BACKGROUND_QUALITY_MODEL ("sonnet")
    """
    if fast is None:
        fast = os.getenv("SECOND_BRAIN_BACKGROUND_FAST_MODEL", "haiku").strip() or "haiku"
    if quality is None:
        quality = os.getenv("SECOND_BRAIN_BACKGROUND_QUALITY_MODEL", "sonnet").strip() or "sonnet"
    return {"fast": fast, "quality": quality}


class PersonaLearningSettings(NamedTuple):
    """Effective persona-learning-tick knobs (call-time resolved)."""

    enabled: bool
    tick_interval_hours: float
    silent_skip_window_hours: float


def get_persona_learning_settings(
    enabled: bool | None = None,
    tick_interval_hours: float | None = None,
    silent_skip_window_hours: float | None = None,
) -> PersonaLearningSettings:
    """Resolve persona-learning-tick knobs at CALL TIME (Rule 1).

    The persona learning tick (``persona_learning_tick.py``) enumerates
    learning-enabled personas and spawns per-persona reflection pipelines.
    These knobs control the global tick behaviour; per-persona opt-in lives
    in each profile's ``config.yaml`` (``learning.enabled``).

    Knobs:
        PERSONA_LEARNING_ENABLED ("true") — global kill switch for the tick.
        PERSONA_LEARNING_TICK_INTERVAL ("12") — minimum hours between full
            tick runs (recency guard, same pattern as dream-state).
        PERSONA_LEARNING_SILENT_SKIP_WINDOW ("24") — hours: if a persona
            has zero attributed rows newer than this window, skip it with no
            model call (``PERSONA_REFLECT_SILENT``).

    None-sentinel pattern: explicit values pass through; ``None`` resolves
    the matching env var inside the body so ``monkeypatch.setenv`` takes
    effect on the next call with no module reload.
    """
    if enabled is None:
        enabled = os.getenv("PERSONA_LEARNING_ENABLED", "true").lower() == "true"
    # Both windows feed ``timedelta``. A bare ``float()`` accepted garbage
    # ("12h" -> ValueError out of the resolver) AND accepted NaN/inf, which
    # parse cleanly and then raise ValueError/OverflowError inside
    # ``_resolve_since_boundary`` — a boundary owner the tick calls OUTSIDE
    # either counter's fail-open, so one environment typo aborted the whole
    # persona fan-out rather than degrading this persona to zero.
    if tick_interval_hours is None:
        tick_interval_hours = _finite_or_default(
            _safe_float_env("PERSONA_LEARNING_TICK_INTERVAL", 12.0), 12.0
        )
    if silent_skip_window_hours is None:
        silent_skip_window_hours = _finite_or_default(
            _safe_float_env("PERSONA_LEARNING_SILENT_SKIP_WINDOW", 24.0), 24.0
        )
    return PersonaLearningSettings(
        enabled=enabled,
        tick_interval_hours=tick_interval_hours,
        silent_skip_window_hours=silent_skip_window_hours,
    )


def _safe_int_env(name: str, default: int) -> int:
    """``int(os.getenv(name))`` that degrades to *default* on garbage input.

    A knob resolver reading an operator-editable env var must never let a
    typo (``PERSONA_NOTES_WINDOW_HOURS=garbage``) raise ``ValueError`` out of
    a call-time settings resolver and abort the caller — that is a fail-open
    contract violation, not a configuration error worth crashing over.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _safe_float_env(name: str, default: float) -> float:
    """``float(os.getenv(name))`` that degrades to *default* on garbage input."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _finite_or_default(value: float, default: float) -> float:
    """Reject NaN/inf, which parse cleanly and only explode downstream.

    ``float("nan")`` and ``float("1e309")`` are not the garbage
    ``_safe_float_env`` catches — they are valid floats that raise
    ``ValueError``/``OverflowError`` the moment something builds a
    ``timedelta`` from them, far from the typo that produced them and after
    the fail-open boundary that would have contained it.
    """
    return value if math.isfinite(value) else default


def _positive_or_default(value: int, default: int) -> int:
    """A non-positive cap is invalid; degrade to the documented default.

    "Uncapped" is an explicit ``None`` everywhere in this file, never a number,
    so 0 or a negative here can only be a typo — and clamping it to 0 turns a
    typo into a silent, permanent data loss (zero admitted, run reported clean,
    watermark consumed).
    """
    return value if value > 0 else default


class PersonaNotesSettings(NamedTuple):
    """Effective persona note-corpus knobs (call-time resolved)."""

    max_files: int
    max_chars_per_file: int
    max_total_chars: int
    window_hours: float


def get_persona_notes_settings(
    max_files: int | None = None,
    max_chars_per_file: int | None = None,
    max_total_chars: int | None = None,
    window_hours: float | None = None,
) -> PersonaNotesSettings:
    """Resolve persona note-corpus knobs at CALL TIME (Rule 1) — issue #425.

    Bounds the distiller corpus assembled from a persona's OWN work notes
    (``PERSONA_NOTE_DIRS`` = ``experience/`` + ``market/``). Caps mirror the
    episode-digest prior art (``get_episode_settings``): a newest-first file
    cap, a per-file excerpt cap, and a total-chars budget — so a persona that
    logged 200 assignments overnight cannot blow the reflection prompt.

    Knobs:
        PERSONA_NOTES_MAX_FILES (10) — newest-first cap on note files fed to
            one reflection.
        PERSONA_NOTES_MAX_CHARS_PER_FILE (4000) — per-file excerpt cap. The
            excerpt keeps the FRESHEST END of the file (the ``get_recent_logs``
            truncate shape), because sections are appended chronologically.
        PERSONA_NOTES_MAX_TOTAL_CHARS (12000) — total corpus budget.
        PERSONA_NOTES_WINDOW_HOURS (24) — fallback freshness window when the
            tick did not pass ``--notes-since`` (cold start, manual run).

    None-sentinel pattern: explicit values pass through; ``None`` resolves the
    matching env var inside the body so ``monkeypatch.setenv`` takes effect on
    the next call with no module reload.

    Bounded call-time parsing: a garbage env value (``PERSONA_NOTES_WINDOW_
    HOURS=garbage``) must degrade to the documented default, never raise
    ``ValueError`` out of this resolver and abort the reflection run. Every
    resolved value is also clamped to a safe minimum — a negative or zero
    cap is not a valid "uncapped" signal here (``list_fresh_notes`` and the
    corpus truncation both treat "uncapped" as an explicit ``None``, never a
    number).
    """
    # A non-positive cap is INVALID here, not "uncapped" (uncapped is an
    # explicit ``None``) — and clamping it to 0 was worse than rejecting it:
    # zero admits zero notes, the distiller then reports a clean empty run, and
    # the parent stamps its watermark past notes nothing ever read. One typo
    # (`PERSONA_NOTES_MAX_FILES=-5`) silently and permanently lost a persona's
    # backlog. Degrade to the documented default instead, the same shape the
    # non-finite window below uses.
    if max_files is None:
        max_files = _positive_or_default(
            _safe_int_env("PERSONA_NOTES_MAX_FILES", 10), 10
        )
    if max_chars_per_file is None:
        max_chars_per_file = _positive_or_default(
            _safe_int_env("PERSONA_NOTES_MAX_CHARS_PER_FILE", 4000), 4000
        )
    if max_total_chars is None:
        max_total_chars = _positive_or_default(
            _safe_int_env("PERSONA_NOTES_MAX_TOTAL_CHARS", 12000), 12000
        )
    if window_hours is None:
        # NaN/inf reach here as valid floats and only explode downstream, where
        # ``timedelta(hours=inf)`` raises out of the boundary resolver.
        window_hours = _finite_or_default(
            max(0.0, _safe_float_env("PERSONA_NOTES_WINDOW_HOURS", 24.0)), 24.0
        )
    return PersonaNotesSettings(
        max_files=max_files,
        max_chars_per_file=max_chars_per_file,
        max_total_chars=max_total_chars,
        window_hours=window_hours,
    )


class PersonaDreamSettings(NamedTuple):
    """Effective persona-dream-tick knobs (call-time resolved)."""

    enabled: bool
    tick_interval_hours: float
    timeout_seconds: float
    max_wall_clock_seconds: float
    days: int


def get_persona_dream_settings(
    enabled: bool | None = None,
    tick_interval_hours: float | None = None,
    timeout_seconds: float | None = None,
    max_wall_clock_seconds: float | None = None,
    days: int | None = None,
) -> PersonaDreamSettings:
    """Resolve persona-dream-tick knobs at CALL TIME (Rule 1).

    The persona dream tick (``persona_dream_tick.py``) is the sibling of the
    persona LEARNING tick: it fans the FULL dream cycle out over every named
    profile by spawning ``memory_dream.py -p <name>``. Cadences differ
    (nightly dream vs 12h-interval reflect), so the knobs are separate —
    ``PERSONA_LEARNING_*`` never gates the dream fan-out and vice versa.

    Knobs:
        PERSONA_DREAM_ENABLED ("true") — global kill switch for the fan-out.
            Per-persona spend is already gated by the child's own
            ``DREAM_SILENT`` fast path (zero LLM calls without signal), so
            this is a fire-extinguisher, not a routine cost lever.
        PERSONA_DREAM_TICK_INTERVAL ("20") — minimum hours between fan-out
            attempts FOR ONE persona (parent-side recency guard). 20 rather
            than 24 so an early/late nightly trigger is never swallowed; the
            child keeps its own ``DREAM_MIN_INTERVAL_HOURS`` guard on top.
        PERSONA_DREAM_TIMEOUT ("900") — seconds a single persona's dream
            subprocess may run before it is killed (consolidate + prune +
            belief-evolve is a 3-LLM-call chain; the main dream's scheduled
            task allows 30 min for the same work).
        PERSONA_DREAM_MAX_WALL_CLOCK ("0" = unlimited) — seconds the whole
            serial fan-out may consume. Defaults to UNLIMITED because the
            doctrine is every named persona every night: with a 3600s cap and a
            28-persona roster, a night where each child takes a few minutes
            silently drops the tail, and "rotate the tail tomorrow" is not a
            nightly cadence. This is an operator override for a box that must
            hard-stop, not a routine lever — the child's DREAM_SILENT fast path
            (zero LLM calls, seconds) is what actually bounds a normal night.
            When a cap DOES truncate, the personas it never reached are named
            in the tick output and counted in its summary, never silently
            dropped, and they are retried oldest-first the next night.
        PERSONA_DREAM_DAYS ("7") — days of the persona's own daily logs the
            child dream scans (mirrors ``memory_dream.py --days``).

    None-sentinel pattern: explicit values pass through; ``None`` resolves
    the matching env var inside the body so ``monkeypatch.setenv`` takes
    effect on the next call with no module reload.
    """
    if enabled is None:
        enabled = os.getenv("PERSONA_DREAM_ENABLED", "true").lower() == "true"
    if tick_interval_hours is None:
        tick_interval_hours = float(os.getenv("PERSONA_DREAM_TICK_INTERVAL", "20"))
    if timeout_seconds is None:
        timeout_seconds = float(os.getenv("PERSONA_DREAM_TIMEOUT", "900"))
    if max_wall_clock_seconds is None:
        max_wall_clock_seconds = float(
            os.getenv("PERSONA_DREAM_MAX_WALL_CLOCK", "0")
        )
    if days is None:
        days = int(os.getenv("PERSONA_DREAM_DAYS", "7"))
    return PersonaDreamSettings(
        enabled=enabled,
        tick_interval_hours=tick_interval_hours,
        timeout_seconds=timeout_seconds,
        max_wall_clock_seconds=max_wall_clock_seconds,
        days=days,
    )


class PhoneOpsSettings(NamedTuple):
    """Effective PhoneOps knobs (call-time resolved)."""

    enabled: bool


def get_phoneops_settings(enabled: bool | None = None) -> PhoneOpsSettings:
    """Resolve the PhoneOps master switch at CALL TIME (Rule 1) — P3.0.

    HOMIE_PHONEOPS_ENABLED ("false") — default OFF: a ``phone`` browser target
    with the switch off is refused (403) at the dashboard API gate, so absent
    config is byte-identical desktop-only M12 behavior. The None-sentinel
    pattern means ``monkeypatch.setenv`` takes effect on the next call with no
    module reload.
    """
    if enabled is None:
        enabled = os.getenv("HOMIE_PHONEOPS_ENABLED", "false").lower() == "true"
    return PhoneOpsSettings(enabled=enabled)


class GhostSettings(NamedTuple):
    """Effective Ghost Phone knobs (call-time resolved)."""

    enabled: bool


def get_ghost_settings(enabled: bool | None = None) -> GhostSettings:
    """Resolve the Ghost Phone master switch at CALL TIME (Rule 1) — P4.0.

    HOMIE_GHOST_ENABLED ("false") — default OFF: a ``ghost`` browser target with
    the switch off is refused (403) at the dashboard API gate, exactly like the
    PhoneOps gate but as a DISTINCT capability (the ghost is a dedicated device
    the operator owns, separate from driving the personal phone). The
    None-sentinel pattern means ``monkeypatch.setenv`` takes effect on the next
    call with no module reload.
    """
    if enabled is None:
        enabled = os.getenv("HOMIE_GHOST_ENABLED", "false").lower() == "true"
    return GhostSettings(enabled=enabled)


class SessionBriefSettings(NamedTuple):
    """Effective session-opening-brief knobs (call-time resolved)."""

    enabled: bool
    away_hours: float
    min_fresh_items: int
    max_per_section: int
    max_chars: int


def get_session_brief_settings(
    enabled: bool | None = None,
    away_hours: float | None = None,
    min_fresh_items: int | None = None,
    max_per_section: int | None = None,
    max_chars: int | None = None,
) -> SessionBriefSettings:
    """Resolve session-opening-brief knobs at CALL TIME (Rule 1) — Living Mind Act 4.

    Every arg uses the None-sentinel pattern: explicit values pass through;
    ``None`` resolves the matching ``SESSION_BRIEF_*`` env var inside the
    body. None of these values become import-time globals — env overrides
    (and ``monkeypatch.setenv`` in tests) take effect on the next call with
    no module reload.

    Knobs:
        SESSION_BRIEF_ENABLED ("true") — kill switch for the brief.
        SESSION_BRIEF_AWAY_HOURS ("8") — away-gate threshold in hours,
            INCLUSIVE boundary (exactly the threshold fires).
        SESSION_BRIEF_MIN_FRESH_ITEMS ("1") — boredom threshold; fewer fresh
            change-source items than this -> total silence, no brief.
        SESSION_BRIEF_MAX_PER_SECTION ("5") — per-source item cap
            (observations, episodes, threads, amendments each).
        SESSION_BRIEF_MAX_CHARS ("2400") — total block cap with priority
            semantics (instruction reserved; one item per fired fresh source
            reserved; context-only threads dropped first).
    """
    if enabled is None:
        enabled = os.getenv("SESSION_BRIEF_ENABLED", "true").lower() == "true"
    if away_hours is None:
        away_hours = float(os.getenv("SESSION_BRIEF_AWAY_HOURS", "8"))
    if min_fresh_items is None:
        min_fresh_items = int(os.getenv("SESSION_BRIEF_MIN_FRESH_ITEMS", "1"))
    if max_per_section is None:
        max_per_section = int(os.getenv("SESSION_BRIEF_MAX_PER_SECTION", "5"))
    if max_chars is None:
        max_chars = int(os.getenv("SESSION_BRIEF_MAX_CHARS", "2400"))
    return SessionBriefSettings(
        enabled=enabled,
        away_hours=away_hours,
        min_fresh_items=min_fresh_items,
        max_per_section=max_per_section,
        max_chars=max_chars,
    )


class CabinetRelaySettings(NamedTuple):
    """Effective cabinet→chat relay knobs (call-time resolved)."""

    enabled: bool
    max_turns: int


def get_cabinet_relay_settings(
    enabled: bool | None = None,
    max_turns: int | None = None,
) -> CabinetRelaySettings:
    """Resolve cabinet→chat relay knobs at CALL TIME (Rule 1).

    The relay (``.claude/chat/cabinet_relay.py``) posts each completed cabinet
    persona turn back into the originating chat channel (Discord/Telegram/…)
    instead of leaving the conversation dashboard-only. Knobs:

        CABINET_CHAT_RELAY_ENABLED ("true") — master switch. When false, the
            cabinet slash commands behave exactly as before (dashboard-only;
            the chat reply points at the browser URL).
        CABINET_CHAT_RELAY_MAX_TURNS ("0") — per-meeting cap on relayed persona
            turns (0 == unlimited). Guards against a ``/standup`` firehose when
            the full roster answers; prefer @mention audiences for tight turns.

    None-sentinel pattern: explicit values pass through; ``None`` resolves the
    matching env var inside the body so ``monkeypatch.setenv`` takes effect on
    the next call with no module reload.
    """
    if enabled is None:
        enabled = os.getenv("CABINET_CHAT_RELAY_ENABLED", "true").lower() == "true"
    if max_turns is None:
        max_turns = int(os.getenv("CABINET_CHAT_RELAY_MAX_TURNS", "0"))
    return CabinetRelaySettings(enabled=enabled, max_turns=max_turns)


class PostizSettings(NamedTuple):
    """Effective Postiz publishing-transport knobs (call-time resolved)."""

    api_url: str
    api_key: str
    timeout_s: float

    @property
    def configured(self) -> bool:
        return bool(self.api_url and self.api_key)


def get_postiz_settings(
    api_url: str | None = None,
    api_key: str | None = None,
    timeout_s: float | None = None,
) -> PostizSettings:
    """Resolve Postiz transport knobs at CALL TIME (Rule 1).

    Postiz is an OPTIONAL self-hosted multi-platform publisher the social
    slice can dispatch through (``execution_method: postiz`` in
    ``social/channels.yaml``). The framework talks to an UNMODIFIED Postiz
    over its Public API — no Postiz (AGPL-3.0) code is embedded. Knobs:

        POSTIZ_API_URL ("") — backend API origin of the Postiz instance,
            e.g. ``http://localhost:5000/api``. Empty == not configured;
            every Postiz surface degrades gracefully (no network I/O).
        POSTIZ_API_KEY ("") — the instance's Public API key. Sent RAW in
            the ``Authorization`` header (Postiz does not use ``Bearer``).
        POSTIZ_TIMEOUT_S ("15") — total request timeout seconds.
    """
    if api_url is None:
        api_url = os.getenv("POSTIZ_API_URL", "").strip()
    if api_key is None:
        api_key = os.getenv("POSTIZ_API_KEY", "").strip()
    if timeout_s is None:
        timeout_s = float(os.getenv("POSTIZ_TIMEOUT_S", "15"))
    return PostizSettings(api_url=api_url, api_key=api_key, timeout_s=timeout_s)


class ContentFactorySettings(NamedTuple):
    """Effective social content-factory knobs (call-time resolved)."""

    unattended: bool
    video_duration_s: int


def get_content_factory_settings(
    unattended: bool | None = None,
    video_duration_s: int | None = None,
) -> ContentFactorySettings:
    """Resolve social content-factory knobs at CALL TIME (Rule 1).

    The content factory (``social/content_factory.py``) generates media +
    copy and queues drafts. DEFAULT-DENY: it only auto-posts (approve +
    dispatch) when unattended mode is explicitly enabled; otherwise it queues
    for operator approval. Knobs:

        HOMIE_SOCIAL_UNATTENDED ("false") — the autopilot switch. When false
            (default), produce() QUEUES drafts only; the operator approves and
            the Homie dispatches. When true, produce() also approves+dispatches
            each draft (still per-post audited). Ships OFF — no accidental
            unattended posting to real brand accounts.
        CONTENT_FACTORY_VIDEO_DURATION_S ("18") — target seconds for a rendered
            vertical video.
    """
    if unattended is None:
        unattended = os.getenv("HOMIE_SOCIAL_UNATTENDED", "false").lower() == "true"
    if video_duration_s is None:
        video_duration_s = int(os.getenv("CONTENT_FACTORY_VIDEO_DURATION_S", "18"))
    return ContentFactorySettings(
        unattended=unattended, video_duration_s=video_duration_s
    )


class CofounderSettings(NamedTuple):
    """Effective autonomous co-founder orchestrator knobs (call-time resolved)."""

    enabled: bool
    projects_dir: Path
    max_iterations: int
    max_wall_clock_hours: float
    max_concurrent: int
    notify_levels: tuple[str, ...]
    zombie_stale_minutes: int
    archon_db: Path
    workflow_provider: str
    workflow_model: str


def get_cofounder_settings(
    enabled: bool | None = None,
    projects_dir: Path | str | None = None,
    max_iterations: int | None = None,
    max_wall_clock_hours: float | None = None,
    max_concurrent: int | None = None,
    notify_levels: str | tuple[str, ...] | list[str] | None = None,
    zombie_stale_minutes: int | None = None,
    archon_db: Path | str | None = None,
    workflow_provider: str | None = None,
    workflow_model: str | None = None,
) -> CofounderSettings:
    """Resolve autonomous co-founder knobs at CALL TIME (Rule 1).

    The co-founder orchestrator (``cofounder/run_pass.py``) advances vault-spec
    projects on the heartbeat cadence: dispatching detached Archon runs,
    polling the run-state DB, running executable completion checks, and
    notifying Telegram only on terminal flips. Every arg uses the
    None-sentinel pattern: explicit values pass through; ``None`` resolves the
    matching ``COFOUNDER_*`` env var inside the body. None of these values
    become import-time globals — env overrides (and ``monkeypatch.setenv`` in
    tests) take effect on the next call with no module reload.

    Knobs:
        COFOUNDER_ENABLED ("false") — master enable; ships OFF until the
            operator's Phase 9 flip. The ``cofounder`` kill switch
            (``HOMIE_KILLSWITCH_COFOUNDER``) is the refusal-counted gate on
            top of this.
        COFOUNDER_PROJECTS_DIR (MEMORY_DIR/cofounder) — watched vault folder
            holding one markdown file per project (sanitizer-denied).
        COFOUNDER_MAX_ITERATIONS ("50") — per-project dispatch cap before the
            status flips to awaiting-human.
        COFOUNDER_MAX_WALL_CLOCK_HOURS ("72") — per-project wall-clock cap
            from first dispatch before the status flips to awaiting-human.
        COFOUNDER_MAX_CONCURRENT ("2") — in-flight build cap across projects;
            excess projects wait in new/queued order.
        COFOUNDER_NOTIFY_LEVELS ("done,blocked,awaiting-human") —
            comma-separated levels that may send a Telegram ping; parsed to an
            order-preserving lowercased tuple (empties dropped; empty string
            disables all notifications).
        COFOUNDER_ZOMBIE_STALE_MINUTES ("60") — minutes without
            ``last_activity_at`` movement (two heartbeat cycles) before a
            running Archon row is a zombie CANDIDATE; the second signal
            (no working_path mtime growth across a full pass) must also hold.
        COFOUNDER_ARCHON_DB (~/.archon/archon.db) — Archon run-state SQLite
            the engine adapter polls READ-ONLY (Rule 2: physical DB rows are
            the only truth about in-flight builds; the adapter can never
            write it).
        COFOUNDER_WORKFLOW_PROVIDER ("claude") — the backend knob stamped by
            CODE into every authored workflow YAML at BOTH the workflow level
            and every loop-node level (loop nodes ignore per-node provider),
            then re-stamped after each pass so an LLM edit can never drift it.
        COFOUNDER_WORKFLOW_MODEL ("sonnet") — the model half of the same
            backend knob; stamped and re-stamped alongside the provider.
    """
    if enabled is None:
        enabled = os.getenv("COFOUNDER_ENABLED", "false").strip().lower() == "true"
    if projects_dir is None:
        raw_dir = os.getenv("COFOUNDER_PROJECTS_DIR", "").strip()
        projects_dir = Path(raw_dir) if raw_dir else MEMORY_DIR / "cofounder"
    else:
        projects_dir = Path(projects_dir)
    if max_iterations is None:
        max_iterations = int(os.getenv("COFOUNDER_MAX_ITERATIONS", "50"))
    if max_wall_clock_hours is None:
        max_wall_clock_hours = float(os.getenv("COFOUNDER_MAX_WALL_CLOCK_HOURS", "72"))
    if max_concurrent is None:
        max_concurrent = int(os.getenv("COFOUNDER_MAX_CONCURRENT", "2"))
    if notify_levels is None:
        notify_levels = os.getenv(
            "COFOUNDER_NOTIFY_LEVELS", "done,blocked,awaiting-human"
        )
    if isinstance(notify_levels, str):
        parsed_levels = tuple(
            level.strip().lower() for level in notify_levels.split(",") if level.strip()
        )
    else:
        parsed_levels = tuple(
            str(level).strip().lower() for level in notify_levels if str(level).strip()
        )
    if zombie_stale_minutes is None:
        zombie_stale_minutes = int(os.getenv("COFOUNDER_ZOMBIE_STALE_MINUTES", "60"))
    if archon_db is None:
        raw_db = os.getenv("COFOUNDER_ARCHON_DB", "").strip()
        archon_db = Path(raw_db) if raw_db else Path.home() / ".archon" / "archon.db"
    else:
        archon_db = Path(archon_db)
    if workflow_provider is None:
        workflow_provider = os.getenv("COFOUNDER_WORKFLOW_PROVIDER", "").strip() or "claude"
    if workflow_model is None:
        workflow_model = os.getenv("COFOUNDER_WORKFLOW_MODEL", "").strip() or "sonnet"
    return CofounderSettings(
        enabled=enabled,
        projects_dir=projects_dir,
        max_iterations=max_iterations,
        max_wall_clock_hours=max_wall_clock_hours,
        max_concurrent=max_concurrent,
        notify_levels=parsed_levels,
        zombie_stale_minutes=zombie_stale_minutes,
        archon_db=archon_db,
        workflow_provider=workflow_provider,
        workflow_model=workflow_model,
    )


class CofounderAgendaSettings(NamedTuple):
    """Effective co-founder morning-agenda knobs (call-time resolved)."""

    enabled: bool
    agenda_hour: int
    max_items: int
    max_attempts: int
    notify: bool


def get_cofounder_agenda_settings(
    enabled: bool | None = None,
    agenda_hour: int | None = None,
    max_items: int | None = None,
    max_attempts: int | None = None,
    notify: bool | None = None,
) -> CofounderAgendaSettings:
    """Resolve co-founder v2 agenda knobs at CALL TIME (Rule 1).

    The agenda pass (``cofounder/agenda.py``) is the WS2 propose-don't-act
    surface: a once-daily portfolio scan that PROPOSES persona->repo
    assignments as a vault artifact + Telegram card and never executes
    anything. It is gated separately from ``COFOUNDER_ENABLED`` so the v2.0
    agenda can bake while the v1 project pipeline stays dormant (and vice
    versa); the shared ``cofounder`` kill switch sits on top of both.

    Knobs:
        COFOUNDER_AGENDA_ENABLED ("false") — master enable for the agenda
            pass. Ships OFF (dormant-by-default, same family as v1).
        COFOUNDER_AGENDA_HOUR ("7") — earliest LOCAL hour the daily scan may
            run; the first heartbeat pass on/after this hour produces the day's
            agenda.
        COFOUNDER_AGENDA_MAX_ITEMS ("5") — cap on proposed agenda lines; the
            validator truncates anything past it.
        COFOUNDER_AGENDA_MAX_ATTEMPTS ("3") — per-day cap on failed proposal
            attempts (LLM error/garbage); once reached the pass stays quiet
            until tomorrow instead of retrying every heartbeat.
        COFOUNDER_AGENDA_NOTIFY ("true") — send the agenda Telegram card
            through the gated ``cofounder.notify`` sender (kill switch +
            capability gate + audit row all still apply).
    """
    if enabled is None:
        enabled = os.getenv("COFOUNDER_AGENDA_ENABLED", "false").strip().lower() == "true"
    if agenda_hour is None:
        agenda_hour = int(os.getenv("COFOUNDER_AGENDA_HOUR", "7"))
    if max_items is None:
        max_items = int(os.getenv("COFOUNDER_AGENDA_MAX_ITEMS", "5"))
    if max_attempts is None:
        max_attempts = int(os.getenv("COFOUNDER_AGENDA_MAX_ATTEMPTS", "3"))
    if notify is None:
        notify = os.getenv("COFOUNDER_AGENDA_NOTIFY", "true").strip().lower() == "true"
    return CofounderAgendaSettings(
        enabled=enabled,
        agenda_hour=agenda_hour,
        max_items=max_items,
        max_attempts=max_attempts,
        notify=notify,
    )


class CofounderDelegationSettings(NamedTuple):
    """Effective co-founder delegation-transport knobs (call-time resolved)."""

    enabled: bool
    max_assignments_per_day: int
    max_inflight_per_persona: int


def get_cofounder_delegation_settings(
    enabled: bool | None = None,
    max_assignments_per_day: int | None = None,
    max_inflight_per_persona: int | None = None,
) -> CofounderDelegationSettings:
    """Resolve co-founder v2 WS3 delegation knobs at CALL TIME (Rule 1).

    The delegation transport (``cofounder/delegate.py``) turns an APPROVED
    agenda line into a convoy + typed mailbox assignment for a persona.
    The operator's per-line approval ("run it" / ``/cofounder run <n>``)
    ALWAYS works — ``COFOUNDER_DELEGATION_ENABLED`` gates only AUTONOMOUS
    (unapproved) delegation: the autopilot pass (``cofounder/autopilot.py``,
    epic #408 T5). The
    ``cofounder_delegation`` kill switch
    (``HOMIE_KILLSWITCH_COFOUNDER_DELEGATION``) sits on top of BOTH paths —
    it is the emergency stop for the whole delegation surface.

    Knobs:
        COFOUNDER_DELEGATION_ENABLED ("true") — autonomous-delegation flag.
            Ships ARMED as of v1.4.0 (operator decision 2026-08-12: autonomy
            means autonomy); set "false" to fall back to propose-only.
            Approved lines do not need it either way.
        COFOUNDER_MAX_ASSIGNMENTS_PER_DAY ("5") — cap on delegations per
            local day across all personas (approved + autonomous combined).
        COFOUNDER_MAX_INFLIGHT_PER_PERSONA ("1") — cap on un-acked
            ``cofounder_assignment`` mailbox deliveries per persona
            (physical mailbox state is the in-flight truth — Rule 2).
    """
    if enabled is None:
        enabled = (
            os.getenv("COFOUNDER_DELEGATION_ENABLED", "true").strip().lower()
            == "true"
        )
    if max_assignments_per_day is None:
        max_assignments_per_day = int(
            os.getenv("COFOUNDER_MAX_ASSIGNMENTS_PER_DAY", "5")
        )
    if max_inflight_per_persona is None:
        max_inflight_per_persona = int(
            os.getenv("COFOUNDER_MAX_INFLIGHT_PER_PERSONA", "1")
        )
    return CofounderDelegationSettings(
        enabled=enabled,
        max_assignments_per_day=max_assignments_per_day,
        max_inflight_per_persona=max_inflight_per_persona,
    )


class CofounderAutopilotSettings(NamedTuple):
    """Effective co-founder autopilot budgets (call-time resolved)."""

    max_priority: int | None
    max_per_day: int
    code_max_per_day: int | None


def get_cofounder_autopilot_settings(
    max_priority: int | None = None,
    max_per_day: int | None = None,
    code_max_per_day: int | None = None,
) -> CofounderAutopilotSettings:
    """Resolve the autonomous-delegation budgets at CALL TIME (Rule 1).

    The autopilot pass (``cofounder/autopilot.py``) is the code path
    ``COFOUNDER_DELEGATION_ENABLED`` has always gated: it selects today's
    still-proposed agenda lines and sends each through the SAME transport
    ``/cofounder run <n>`` uses. These knobs are RETREAT LEVERS — they ship
    wide open so autonomy means autonomy, and the real containment stays
    where it already is (Rule-4 scope, the daily/in-flight caps, the kill
    switch, worktick's per-tick cap).

    Knobs:
        COFOUNDER_AUTO_MAX_PRIORITY (unset) — highest priority NUMBER the
            autopilot may delegate (1 = most urgent). Unset means no
            ceiling: P3 lines auto-delegate too.
        COFOUNDER_AUTO_MAX_PER_DAY (unset) — autopilot-sent lines per local
            day. Unset FOLLOWS ``COFOUNDER_MAX_ASSIGNMENTS_PER_DAY`` (the
            global cap the transport enforces anyway), so raising the
            global cap raises autonomy with it.
        COFOUNDER_AUTO_CODE_MAX_PER_DAY (unset) — autopilot-sent ``code``
            mode (Archon dispatch) lines per local day. Unset means
            unlimited WITHIN the global cap.
    """
    if max_priority is None:
        raw_priority = os.getenv("COFOUNDER_AUTO_MAX_PRIORITY", "").strip()
        max_priority = int(raw_priority) if raw_priority else None
    if max_per_day is None:
        raw_per_day = os.getenv("COFOUNDER_AUTO_MAX_PER_DAY", "").strip()
        max_per_day = (
            int(raw_per_day)
            if raw_per_day
            else get_cofounder_delegation_settings().max_assignments_per_day
        )
    if code_max_per_day is None:
        raw_code = os.getenv("COFOUNDER_AUTO_CODE_MAX_PER_DAY", "").strip()
        code_max_per_day = int(raw_code) if raw_code else None
    return CofounderAutopilotSettings(
        max_priority=max_priority,
        max_per_day=max_per_day,
        code_max_per_day=code_max_per_day,
    )


class CofounderCallbackSettings(NamedTuple):
    """Effective co-founder conversational-callback knobs (call-time)."""

    enabled: bool


def get_cofounder_callback_settings(
    enabled: bool | None = None,
) -> CofounderCallbackSettings:
    """Resolve the co-founder callback knob at CALL TIME (Rule 1).

    The callback (``cognition/cofounder_callback.py``) is how autonomous work
    reaches the operator's own conversation: on an interactive turn it renders
    the outcomes a durable watermark says he has not seen yet. It is a READ of
    physical ledger/agenda state plus a prompt suffix — it never delegates,
    never mutates a project, and never maps conversation onto an action.

    Ships ON, because a co-founder that works silently and never reports is
    the starvation this whole slice exists to fix. Turning it OFF leaves
    autonomy running and mutes only the conversational report (the Telegram
    cards and ``/cofounder brief`` still surface it).

    Knobs:
        COFOUNDER_CALLBACK_ENABLED ("true") — set false to mute the
            conversational callback. ``HOMIE_KILLSWITCH_COFOUNDER`` mutes it
            too, along with the rest of the slice.
    """
    if enabled is None:
        enabled = (
            os.getenv("COFOUNDER_CALLBACK_ENABLED", "true").strip().lower() == "true"
        )
    return CofounderCallbackSettings(enabled=enabled)


class CofounderWorktickSettings(NamedTuple):
    """Effective co-founder work-loop knobs (call-time resolved)."""

    enabled: bool
    max_per_tick: int
    code_workflow: str


def get_cofounder_worktick_settings(
    enabled: bool | None = None,
    max_per_tick: int | None = None,
    code_workflow: str | None = None,
) -> CofounderWorktickSettings:
    """Resolve co-founder v2 WS4 work-loop knobs at CALL TIME (Rule 1).

    The work loop (``cofounder/worktick.py``) rides the heartbeat: it claims
    ``cofounder_assignment`` mailbox deliveries for delegable personas,
    re-checks the delegation scope at claim (Rule 4's second half), executes
    per the OPERATOR-APPROVED mode, and reports a typed ``cofounder_result``.
    Shares the ``cofounder_delegation`` kill switch with the send side — one
    emergency stop for the whole delegation surface.

    Knobs:
        COFOUNDER_WORKLOOP_ENABLED ("false") — master enable for the work
            loop. Ships OFF (dormant-by-default family).
        COFOUNDER_WORKLOOP_MAX_PER_TICK ("2") — assignments executed per
            heartbeat tick across ALL personas (a tick is ~30 min; drafts
            run on the background QUALITY tier).
        COFOUNDER_WORKLOOP_CODE_WORKFLOW ("archon-ralph-dag") — the Archon
            workflow used for ``mode: code`` assignments (detached worktree
            dispatch, PR-for-review merge policy).
    """
    if enabled is None:
        enabled = (
            os.getenv("COFOUNDER_WORKLOOP_ENABLED", "false").strip().lower()
            == "true"
        )
    if max_per_tick is None:
        max_per_tick = int(os.getenv("COFOUNDER_WORKLOOP_MAX_PER_TICK", "2"))
    if code_workflow is None:
        code_workflow = (
            os.getenv("COFOUNDER_WORKLOOP_CODE_WORKFLOW", "").strip()
            or "archon-ralph-dag"
        )
    return CofounderWorktickSettings(
        enabled=enabled,
        max_per_tick=max_per_tick,
        code_workflow=code_workflow,
    )


class CofounderReportSettings(NamedTuple):
    """Effective co-founder reporting-loop knobs (call-time resolved)."""

    enabled: bool
    notify: bool
    checkout_hour: int
    poll_days: int


def get_cofounder_report_settings(
    enabled: bool | None = None,
    notify: bool | None = None,
    checkout_hour: int | None = None,
    poll_days: int | None = None,
) -> CofounderReportSettings:
    """Resolve co-founder v2 WS5 reporting knobs at CALL TIME (Rule 1).

    The reporting pass (``cofounder/report.py``) closes the delegation
    circle: it ingests the personas' typed ``cofounder_result`` messages
    (flipping agenda-line statuses), polls archon.db for dispatched
    code-mode runs, sends an intraday batch card when results land, and
    sends the once-daily end-of-day checkout card (operator resolution #3 —
    morning agenda + intraday awareness + EOD checkout). Deterministic —
    ZERO LLM calls. Shares the ``cofounder_delegation`` kill switch.

    Knobs:
        COFOUNDER_REPORT_ENABLED ("false") — master enable (dormant family).
        COFOUNDER_REPORT_NOTIFY ("true") — send the intraday/checkout cards
            (kill switch + capability gate + audit still apply; an emptied
            COFOUNDER_NOTIFY_LEVELS mutes everything as always).
        COFOUNDER_CHECKOUT_HOUR ("18") — earliest LOCAL hour the daily
            checkout card may send.
        COFOUNDER_REPORT_POLL_DAYS ("7") — how many recent agenda days to
            scan for still-dispatched code runs.
    """
    if enabled is None:
        enabled = (
            os.getenv("COFOUNDER_REPORT_ENABLED", "false").strip().lower() == "true"
        )
    if notify is None:
        notify = (
            os.getenv("COFOUNDER_REPORT_NOTIFY", "true").strip().lower() == "true"
        )
    if checkout_hour is None:
        checkout_hour = int(os.getenv("COFOUNDER_CHECKOUT_HOUR", "18"))
    if poll_days is None:
        poll_days = int(os.getenv("COFOUNDER_REPORT_POLL_DAYS", "7"))
    return CofounderReportSettings(
        enabled=enabled,
        notify=notify,
        checkout_hour=checkout_hour,
        poll_days=poll_days,
    )


class ArchonEventsSettings(NamedTuple):
    """Effective Archon live-telemetry ingest knobs (call-time resolved)."""

    db_path: Path
    poll_interval_s: float
    drain_limit: int
    buffer_size: int
    snapshot_limit: int
    max_data_chars: int
    connect_timeout_s: float


def get_archon_events_settings(
    db_path: Path | str | None = None,
    poll_interval_s: float | None = None,
    drain_limit: int | None = None,
    buffer_size: int | None = None,
    snapshot_limit: int | None = None,
    max_data_chars: int | None = None,
    connect_timeout_s: float | None = None,
) -> ArchonEventsSettings:
    """Resolve Archon events-ingest knobs at CALL TIME (Rule 1).

    The ingest (``integrations/archon_events.py``) cursor-tails Archon's
    ``remote_agent_workflow_events`` table READ-ONLY and fans the rows out to
    the dashboard as REST + SSE. Every arg uses the None-sentinel pattern so
    tests can point ``ARCHON_EVENTS_DB`` at a fixture db and shrink the
    cadence without a module reload.

    Knobs:
        ARCHON_EVENTS_DB (~/.archon/archon.db) — Archon's run ledger. Opened
            with a ``mode=ro`` URI ONLY; the framework never writes it.
        ARCHON_EVENTS_POLL_INTERVAL_S ("1.5") — tail cadence. Matches the
            ~1-2s band Archon's own DashboardEventPoller already places on
            this DB; the poller skips the query entirely when nobody is
            subscribed.
        ARCHON_EVENTS_DRAIN_LIMIT ("500") — max rows per drain. Rows past the
            limit inside one boundary second are NOT lost: the cursor uses
            ``created_at >= cursor`` so the next drain re-queries that second
            and the id-set suppresses only what was already emitted.
        ARCHON_EVENTS_BUFFER_SIZE ("1000") — SSE replay ring-buffer depth. A
            reconnect whose Last-Event-ID predates the ring gets 410 +
            ``X-Refetch-Hint`` pointing at the REST snapshot.
        ARCHON_EVENTS_SNAPSHOT_LIMIT ("200") — max rows the REST snapshot (and
            the SSE subscribe-time snapshot frame) returns per query.
        ARCHON_EVENTS_MAX_DATA_CHARS ("2000") — per-event budget for the
            serialized ``data`` blob. Archon's ``data`` carries LLM-authored
            ``tool_input`` and node output, so it is treated as hostile: each
            value is redacted and capped, then the whole object is trimmed to
            this budget.
        ARCHON_EVENTS_CONNECT_TIMEOUT_S ("2.0") — bounded busy-wait on the
            WAL-active db; expiry degrades to an empty read, never a hang.
    """
    if db_path is None:
        raw_db = os.getenv("ARCHON_EVENTS_DB", "").strip()
        db_path = Path(raw_db) if raw_db else Path.home() / ".archon" / "archon.db"
    else:
        db_path = Path(db_path)
    if poll_interval_s is None:
        poll_interval_s = float(os.getenv("ARCHON_EVENTS_POLL_INTERVAL_S", "1.5"))
    if drain_limit is None:
        drain_limit = int(os.getenv("ARCHON_EVENTS_DRAIN_LIMIT", "500"))
    if buffer_size is None:
        buffer_size = int(os.getenv("ARCHON_EVENTS_BUFFER_SIZE", "1000"))
    if snapshot_limit is None:
        snapshot_limit = int(os.getenv("ARCHON_EVENTS_SNAPSHOT_LIMIT", "200"))
    if max_data_chars is None:
        max_data_chars = int(os.getenv("ARCHON_EVENTS_MAX_DATA_CHARS", "2000"))
    if connect_timeout_s is None:
        connect_timeout_s = float(os.getenv("ARCHON_EVENTS_CONNECT_TIMEOUT_S", "2.0"))
    return ArchonEventsSettings(
        db_path=db_path,
        poll_interval_s=poll_interval_s,
        drain_limit=drain_limit,
        buffer_size=buffer_size,
        snapshot_limit=snapshot_limit,
        max_data_chars=max_data_chars,
        connect_timeout_s=connect_timeout_s,
    )


# Sentinel secret value that disables signature validation on a webhook route.
# Loopback-only escape hatch for local testing (hermes-v18 Phase 4 port).
WEBHOOK_INSECURE_NO_AUTH = "INSECURE_NO_AUTH"

# Hostnames/IP literals that only serve connections originating on the same
# machine (mirrors orchestration/api.py + Hermes _LOOPBACK_HOSTS).
_WEBHOOK_LOOPBACK_HOSTS = frozenset({
    "127.0.0.1",
    "localhost",
    "::1",
    "ip6-localhost",
    "ip6-loopback",
})


def webhook_host_is_loopback(host: str) -> bool:
    """True when ``host`` binds only to the local machine.

    Falsy values (empty string, None) are conservatively treated as
    NON-loopback because an unset host usually means a public default bind.
    """
    if not host:
        return False
    return str(host).strip().lower() in _WEBHOOK_LOOPBACK_HOSTS


class WebhookRoute(NamedTuple):
    """One operator-configured webhook route (hermes-v18 Phase 4)."""

    name: str
    secret: str                     # resolved (inline or via secret_env); never ""
    events: tuple[str, ...]        # allowed event types (() = accept all)
    prompt: str                     # template ("" = default JSON dump)
    deliver: str                    # "log" | platform value | "github_comment"
    deliver_extra: dict             # operator-fixed target config
    deliver_only: bool              # True = skip engine, push rendered template
    deliver_extra_templated: bool   # opt-in payload templating (default False)
    enabled: bool                   # explicit False rejects events (403)


class WebhookSettings(NamedTuple):
    """Effective webhook-adapter knobs (call-time resolved)."""

    host: str
    port: int
    allow_non_loopback: bool
    rate_limit: int
    max_body_bytes: int
    idempotency_ttl: int
    routes: dict[str, WebhookRoute]  # EMPTY by default -> adapter dormant


def _parse_webhook_route(name: str, raw: object, *, host: str) -> WebhookRoute | None:
    """Validate one WEBHOOK_ROUTES entry; return None (and log) when invalid.

    Mirrors Hermes' dynamic-route rejection: an empty effective secret or an
    INSECURE_NO_AUTH secret on a non-loopback host drops the route instead of
    raising — a misconfigured route must never take the whole bot down.
    """
    if not isinstance(raw, dict):
        print(f"[config] webhook route '{name}' skipped: not an object")
        return None
    secret = str(raw.get("secret", "") or "")
    secret_env = str(raw.get("secret_env", "") or "")
    if not secret and secret_env:
        secret = os.getenv(secret_env, "") or ""
    if not secret:
        print(
            f"[config] webhook route '{name}' skipped: no HMAC secret "
            f"(set 'secret' or 'secret_env'; '{WEBHOOK_INSECURE_NO_AUTH}' "
            f"disables auth for loopback testing only)"
        )
        return None
    if secret == WEBHOOK_INSECURE_NO_AUTH and not webhook_host_is_loopback(host):
        print(
            f"[config] webhook route '{name}' skipped: {WEBHOOK_INSECURE_NO_AUTH} "
            f"is only allowed on loopback hosts (host={host!r})"
        )
        return None
    deliver = str(raw.get("deliver", "log") or "log")
    deliver_only = bool(raw.get("deliver_only", False))
    if deliver_only and deliver in ("", "log"):
        print(
            f"[config] webhook route '{name}' skipped: deliver_only=true "
            f"requires a real deliver target (got {deliver!r})"
        )
        return None
    events_raw = raw.get("events", [])
    events = tuple(str(e) for e in events_raw) if isinstance(events_raw, list) else ()
    deliver_extra = raw.get("deliver_extra", {})
    if not isinstance(deliver_extra, dict):
        deliver_extra = {}
    return WebhookRoute(
        name=name,
        secret=secret,
        events=events,
        prompt=str(raw.get("prompt", "") or ""),
        deliver=deliver,
        deliver_extra=deliver_extra,
        deliver_only=deliver_only,
        deliver_extra_templated=bool(raw.get("deliver_extra_templated", False)),
        enabled=raw.get("enabled", True) is not False,
    )


def get_webhook_settings(
    host: str | None = None,
    port: int | None = None,
    allow_non_loopback: bool | None = None,
    rate_limit: int | None = None,
    max_body_bytes: int | None = None,
    idempotency_ttl: int | None = None,
    routes_json: str | None = None,
) -> WebhookSettings:
    """Resolve webhook-adapter knobs at CALL TIME (Rule 1) — hermes-v18 Phase 4.

    Every arg uses the None-sentinel pattern: explicit values pass through;
    ``None`` resolves the matching ``WEBHOOK_*`` env var inside the body so
    ``monkeypatch.setenv`` takes effect on the next call with no module reload.

    Knobs:
        WEBHOOK_HOST ("127.0.0.1") — bind host (loopback by default).
        WEBHOOK_PORT ("8622") — bind port.
        WEBHOOK_ALLOW_NON_LOOPBACK ("false") — explicit opt-in for a
            non-loopback bind (mirrors ORCHESTRATION_API_ALLOW_NON_LOOPBACK).
        WEBHOOK_RATE_LIMIT ("30") — per-route fixed-window hits/minute.
        WEBHOOK_MAX_BODY_BYTES ("1048576") — request body cap (1 MB).
        WEBHOOK_IDEMPOTENCY_TTL_SECONDS ("3600") — delivery-id replay window.
        WEBHOOK_ROUTES (JSON object) — the route table. UNSET/empty/malformed
            -> ``routes == {}`` (the adapter stays fully dormant; malformed
            JSON logs and NEVER raises). Per-route secrets resolve inline
            (``secret``) or via an env-var name (``secret_env``); routes with
            an empty effective secret are DROPPED, as are INSECURE_NO_AUTH
            routes on a non-loopback host and deliver_only routes without a
            real deliver target.
    """
    if host is None:
        host = os.getenv("WEBHOOK_HOST", "127.0.0.1")
    if port is None:
        port = int(os.getenv("WEBHOOK_PORT", "8622"))
    if allow_non_loopback is None:
        allow_non_loopback = (
            os.getenv("WEBHOOK_ALLOW_NON_LOOPBACK", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
    if rate_limit is None:
        rate_limit = int(os.getenv("WEBHOOK_RATE_LIMIT", "30"))
    if max_body_bytes is None:
        max_body_bytes = int(os.getenv("WEBHOOK_MAX_BODY_BYTES", "1048576"))
    if idempotency_ttl is None:
        idempotency_ttl = int(os.getenv("WEBHOOK_IDEMPOTENCY_TTL_SECONDS", "3600"))
    if routes_json is None:
        routes_json = os.getenv("WEBHOOK_ROUTES", "")

    routes: dict[str, WebhookRoute] = {}
    if routes_json and routes_json.strip():
        try:
            parsed = json.loads(routes_json)
        except (ValueError, TypeError) as exc:
            print(f"[config] WEBHOOK_ROUTES is not valid JSON ({exc}) — webhook dormant")
            parsed = None
        if isinstance(parsed, dict):
            for name, raw in parsed.items():
                route = _parse_webhook_route(str(name), raw, host=host)
                if route is not None:
                    routes[str(name)] = route
        elif parsed is not None:
            print("[config] WEBHOOK_ROUTES must be a JSON object — webhook dormant")

    return WebhookSettings(
        host=host,
        port=port,
        allow_non_loopback=allow_non_loopback,
        rate_limit=rate_limit,
        max_body_bytes=max_body_bytes,
        idempotency_ttl=idempotency_ttl,
        routes=routes,
    )


# Canonical interactive-homie toolset — the full set the main chat engine grants
# its 1:1 homie (chat/engine.py). Single source of truth so the cabinet
# full-parity path and the engine never drift apart.
DEFAULT_AGENT_TOOLSET: tuple[str, ...] = (
    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    "WebSearch", "WebFetch", "NotebookEdit", "Skill",
    # MCP tools
    "mcp__exa__web_search_exa",
    "mcp__exa__get_code_context_exa",
    "mcp__crawl4ai__crawl",
    "mcp__crawl4ai__md",
    "mcp__crawl4ai__ask",
    "mcp__crawl4ai__html",
    "mcp__crawl4ai__pdf",
    "mcp__crawl4ai__screenshot",
    "mcp__crawl4ai__execute_js",
)


def cabinet_persona_full_tools_enabled(enabled: bool | None = None) -> bool:
    """Opt-in: give cabinet personas the SAME toolset + capability as the main
    1:1 homie (full parity) instead of the M1 default-deny no-tools floor.

    Resolved at CALL TIME (Rule 1). Default **false** keeps the shipped framework
    secure-by-default (cabinet rooms stay tool-less unless an operator opts in);
    set ``CABINET_PERSONA_FULL_TOOLS=true`` in .env to arm them.

    SECURITY: this is a TRUSTED-OPERATOR escape hatch, not "the same gates plus
    more tools". With ``bypassPermissions`` + Bash/Write/Edit + unfiltered MCP, a
    cabinet persona can take filesystem/shell/MCP actions that do NOT pass through
    the named direct-integration mutation gates (those only protect the wrapped
    integration entrypoints — social posts, sends, etc.). Leave OFF unless every
    cabinet persona is trusted at the operator's own level.
    """
    if enabled is None:
        enabled = os.getenv("CABINET_PERSONA_FULL_TOOLS", "false").lower() == "true"
    return enabled


def cabinet_persona_max_tool_turns(max_turns: int | None = None) -> int:
    """Per-persona turn budget when full tools are armed (Rule 1, call-time).

    Bounds a tool-using cabinet turn so a full-roster standup doesn't run 13 long
    agentic loops. ``CABINET_PERSONA_MAX_TOOL_TURNS`` (default 8), clamped to
    [1, 50] so a bad/empty/negative/huge value can't disable execution, spin an
    unbounded loop, or crash request construction.
    """
    if max_turns is None:
        try:
            max_turns = int(os.getenv("CABINET_PERSONA_MAX_TOOL_TURNS", "8"))
        except (TypeError, ValueError):
            max_turns = 8
    return max(1, min(int(max_turns), 50))


def ensure_directories() -> None:
    """Ensure all required directories exist."""
    for directory in [MEMORY_DIR, DAILY_DIR, WEEKLY_DIR, STATE_DIR, DATA_DIR,
                       INTEGRATIONS_DIR, DRAFTS_ACTIVE_DIR, DRAFTS_SENT_DIR,
                       DRAFTS_EXPIRED_DIR, CONTINUITY_DIR, COMPACTION_RECOVERY_DIR,
                       SKILL_GENERATION_DIR]:
        directory.mkdir(parents=True, exist_ok=True)


def reload_config() -> dict[str, tuple[str, str]]:
    """Re-read .env and update module globals. Returns {name: (old, new)} for changed values.

    Only reloads values that can safely change at runtime.
    Token changes (TELEGRAM_BOT_TOKEN, SLACK_*) require full restart.
    """
    reloadable_keys = [
        "OPENAI_API_KEY", "VOICE_STT_MODEL", "VOICE_STT_PROVIDERS",
        "VOICE_STT_ENABLE_OPENAI", "VOICE_TTS_ENGINE", "VOICE_TTS_VOICE_EDGE",
        "VOICE_TTS_VOICE_OPENAI",
        "CHAT_MAX_TURNS", "CHAT_MAX_BUDGET_USD", "CHAT_ENGINE_TIMEOUT_SECONDS",
        "SESSION_TURN_THRESHOLD", "RECENT_CONVERSATION_COUNT",
        "RECENT_CONVERSATION_MESSAGE_MAX_CHARS",
        "REGION_BUDGET_RECENT_CONVERSATION",
        "CHAT_ATTACHMENT_MAX_BYTES", "CHAT_ATTACHMENT_MAX_CHARS",
        "CHAT_ATTACHMENT_TOTAL_MAX_CHARS", "CHAT_ENGINE_ATTACHMENT_TIMEOUT_SECONDS",
        "GOOGLE_CALENDAR_ID", "HEARTBEAT_INTERVAL_MINUTES",
        "HEARTBEAT_ACTIVE_START", "HEARTBEAT_ACTIVE_END",
    ]

    module = sys.modules[__name__]
    old_values = {k: getattr(module, k, None) for k in reloadable_keys}

    # Re-read .env from the persona-resolved path. Routing through ENV_FILE
    # (rather than recomputing ``Path(__file__).parent / ".env"``) keeps the
    # reload path aligned with the active profile (PRP-7a Workstream 2).
    load_dotenv(ENV_FILE, override=True)

    # Re-evaluate from env
    changes: dict[str, tuple[str, str]] = {}
    new_map: dict[str, str | int | float] = {
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", ""),
        "VOICE_STT_MODEL": os.getenv("VOICE_STT_MODEL", "whisper-1"),
        "VOICE_STT_PROVIDERS": os.getenv("VOICE_STT_PROVIDERS", ""),
        "VOICE_STT_ENABLE_OPENAI": os.getenv("VOICE_STT_ENABLE_OPENAI", ""),
        "VOICE_TTS_ENGINE": os.getenv("VOICE_TTS_ENGINE", "edge"),
        "VOICE_TTS_VOICE_EDGE": os.getenv("VOICE_TTS_VOICE_EDGE", "en-US-AndrewMultilingualNeural|+14%"),
        "VOICE_TTS_VOICE_OPENAI": os.getenv("VOICE_TTS_VOICE_OPENAI", "alloy"),
        "CHAT_MAX_TURNS": int(os.getenv("CHAT_MAX_TURNS", "25")),
        "CHAT_MAX_BUDGET_USD": float(os.getenv("CHAT_MAX_BUDGET_USD", "2.0")),
        "CHAT_ENGINE_TIMEOUT_SECONDS": float(os.getenv("CHAT_ENGINE_TIMEOUT_SECONDS", "900")),
        "SESSION_TURN_THRESHOLD": int(os.getenv("SESSION_TURN_THRESHOLD", "0")),
        "RECENT_CONVERSATION_COUNT": int(os.getenv("RECENT_CONVERSATION_COUNT", "80")),
        "RECENT_CONVERSATION_MESSAGE_MAX_CHARS": int(
            os.getenv("RECENT_CONVERSATION_MESSAGE_MAX_CHARS", "2000")
        ),
        "REGION_BUDGET_RECENT_CONVERSATION": int(
            os.getenv("REGION_BUDGET_RECENT_CONVERSATION", "24000")
        ),
        "CHAT_ATTACHMENT_MAX_BYTES": int(
            os.getenv("CHAT_ATTACHMENT_MAX_BYTES", str(8 * 1024 * 1024))
        ),
        "CHAT_ATTACHMENT_MAX_CHARS": int(os.getenv("CHAT_ATTACHMENT_MAX_CHARS", "100000")),
        "CHAT_ATTACHMENT_TOTAL_MAX_CHARS": int(
            os.getenv("CHAT_ATTACHMENT_TOTAL_MAX_CHARS", "120000")
        ),
        "CHAT_ENGINE_ATTACHMENT_TIMEOUT_SECONDS": float(
            os.getenv("CHAT_ENGINE_ATTACHMENT_TIMEOUT_SECONDS", "300")
        ),
        "GOOGLE_CALENDAR_ID": os.getenv("GOOGLE_CALENDAR_ID", ""),
        "HEARTBEAT_INTERVAL_MINUTES": int(os.getenv("HEARTBEAT_INTERVAL_MINUTES", "30")),
        "HEARTBEAT_ACTIVE_START": os.getenv("HEARTBEAT_ACTIVE_HOURS_START", "08:00"),
        "HEARTBEAT_ACTIVE_END": os.getenv("HEARTBEAT_ACTIVE_HOURS_END", "22:00"),
    }

    for key, new_val in new_map.items():
        if key == "REGION_BUDGET_RECENT_CONVERSATION":
            old_val = REGION_BUDGETS.get("recent_conversation")
        else:
            old_val = old_values.get(key)
        if old_val != new_val:
            if key == "REGION_BUDGET_RECENT_CONVERSATION":
                module.REGION_BUDGETS["recent_conversation"] = int(new_val)
            else:
                setattr(module, key, new_val)
            # Mask sensitive values in the change report
            if "KEY" in key or "TOKEN" in key:
                changes[key] = ("***", "***" if new_val else "(empty)")
            else:
                changes[key] = (str(old_val), str(new_val))

    return changes


# === PRP-7c Phase 3 — profile-aware delegated attributes ===
#
# ``BOT_PID_FILE``, ``BOT_LOCK_FILE``, ``HEALTH_CHECK_PORT``, and
# ``WHATSAPP_WEBHOOK_PORT`` are resolved on every attribute access via
# ``personas.services``. Resolution is intentionally lazy:
#
#   * Avoids the circular-import trap. ``personas.services`` imports from
#     ``personas.core`` (stdlib-only); it does NOT need ``config``. So
#     ``import config`` then ``import personas.services`` works, and the
#     reverse order works too.
#   * Mid-process profile swaps (tests, ``HOMIE_HOME`` rebinding) take
#     effect immediately because resolution happens at attribute access
#     time, not at module import time.
#   * Existing ``from config import HEALTH_CHECK_PORT`` consumers still
#     work because PEP 562 ``__getattr__`` handles the lookup transparently.
#
# Local ``Any`` import for the ``__getattr__`` annotation (kept near the
# bottom so the rest of the module's import-time behavior stays unchanged).
from typing import Any  # noqa: E402, I001

# Anti-pattern Rule 1: no def-time bind to ``personas.services`` — the
# import lives inside the helper so a test can monkey-patch the resolver
# and the next access sees the patched value.
def __getattr__(name: str) -> Any:
    """Delegate profile-aware constants to ``personas.services``."""
    if name == "BOT_PID_FILE":
        from personas.services import get_bot_pid_path
        return get_bot_pid_path()
    if name == "BOT_LOCK_FILE":
        from personas.services import get_bot_lock_path
        return get_bot_lock_path()
    if name == "HEALTH_CHECK_PORT":
        from personas.services import get_health_check_port
        return get_health_check_port()
    if name == "WHATSAPP_WEBHOOK_PORT":
        from personas.services import get_whatsapp_webhook_port
        return get_whatsapp_webhook_port()
    # Skill-from-experience loop knobs (WS4). Resolved on every attribute
    # access (Rule 1) so an env override / ``monkeypatch.setenv`` takes effect
    # on the NEXT ``from config import SKILL_*`` read with no module reload —
    # deliberately NOT bound as module-level ints the way the older
    # ``SKILL_TRIGGER_TOOL_CALLS`` (line ~378) is. The upstream consumers
    # (cognition.skill_usage, cognition.skill_promotion) read these via PEP 562.
    if name == "SKILL_PROMOTE_REUSE_THRESHOLD":
        return int(os.getenv("SKILL_PROMOTE_REUSE_THRESHOLD", "3"))
    if name == "SKILL_STALE_DAYS":
        return int(os.getenv("SKILL_STALE_DAYS", "30"))
    if name == "SKILL_SCAN_BLOCK_VERDICT":
        return os.getenv("SKILL_SCAN_BLOCK_VERDICT", "dangerous").strip() or "dangerous"
    raise AttributeError(f"module 'config' has no attribute {name!r}")
