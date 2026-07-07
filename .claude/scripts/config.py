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
load_dotenv(ENV_FILE, override=True)

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
# vault. These env-resolved paths let recall + indexing address coding-vault and
# unified-vault too, each with its own SQLite DB under DATA_DIR. Framework code
# must NOT hardcode personal vault paths — they come from env (scrubbed on the
# public export). Unset env => None => that vault is simply unavailable.
HOMIE_CODING_VAULT_DIR = (
    Path(os.getenv("HOMIE_CODING_VAULT_DIR")) if os.getenv("HOMIE_CODING_VAULT_DIR") else None
)
HOMIE_UNIFIED_VAULT_DIR = (
    Path(os.getenv("HOMIE_UNIFIED_VAULT_DIR")) if os.getenv("HOMIE_UNIFIED_VAULT_DIR") else None
)

# thehomie keeps memory.db (back-compat); the others get suffixed DBs.
_VAULT_MEMORY_DIRS: dict[str, "Path | None"] = {
    "thehomie": MEMORY_DIR,
    "coding-vault": HOMIE_CODING_VAULT_DIR,
    "unified-vault": HOMIE_UNIFIED_VAULT_DIR,
}
_VAULT_DB_PATHS: dict[str, Path] = {
    "thehomie": DATABASE_PATH,
    "coding-vault": DATA_DIR / "memory.coding-vault.db",
    "unified-vault": DATA_DIR / "memory.unified-vault.db",
}
VAULT_NAMES = tuple(_VAULT_MEMORY_DIRS.keys())


def resolve_vault(name: str) -> "tuple[Path | None, Path]":
    """Vault name -> (memory_dir, db_path).

    ``memory_dir`` is None when the vault's env path is unset (vault not
    configured on this machine). ``db_path`` is always defined.
    """
    return _VAULT_MEMORY_DIRS.get(name), _VAULT_DB_PATHS.get(name, DATABASE_PATH)


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
example_scout_ENABLED = os.getenv("example_scout_ENABLED", "true").lower() == "true"
example_scout_REPO = os.getenv("example_scout_REPO", "NousResearch/hermes-agent")
example_scout_STATE_FILE = STATE_DIR / "example_scout-state.json"

# === Memory Recall Configuration ===
RECALL_ENABLED = os.getenv("RECALL_ENABLED", "true").lower() == "true"
RECALL_MIN_SCORE = float(os.getenv("RECALL_MIN_SCORE", "0.3"))
RECALL_MAX_RESULTS = int(os.getenv("RECALL_MAX_RESULTS", "3"))
RECALL_MIN_MSG_LEN = int(os.getenv("RECALL_MIN_MSG_LEN", "20"))

# Background job recall limits (heartbeat, reflection, weekly)
RECALL_BACKGROUND_MAX_RESULTS = int(os.getenv("RECALL_BACKGROUND_MAX", "3"))
RECALL_BACKGROUND_MAX_CHARS = int(os.getenv("RECALL_BACKGROUND_CHARS", "2000"))

# LLM re-ranking (Tier 1 queries only)
RECALL_RERANK_ENABLED = os.getenv("RECALL_RERANK_ENABLED", "true").lower() == "true"
RECALL_RERANK_TOP_N = int(os.getenv("RECALL_RERANK_TOP_N", "10"))
RECALL_RERANK_TIMEOUT_S = float(os.getenv("RECALL_RERANK_TIMEOUT_S", "3.0"))

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

# Google OAuth (AI account — your-calendar@gmail.com)
GOOGLE_CREDENTIALS_FILE = INTEGRATIONS_DIR / "google_credentials.json"
GOOGLE_TOKEN_FILE = INTEGRATIONS_DIR / "google_token.json"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
]

# Personal Gmail (owner6392lastname@gmail.com — read-only, separate token)
PERSONAL_GMAIL_ACCOUNT = os.getenv("PERSONAL_GMAIL_ACCOUNT", "owner6392lastname@gmail.com")
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
    # Living Self Act 1 (M4): SELF_MODEL 400->700, USER_INFERENCES 300->500,
    # PREFETCHED 3000->2500 — net-zero BASE-budget reallocation (-500 +300 +200
    # == 0) so the now-clean SELF.md + operator-belief regions get room while the
    # final 27K win32 clamp guarantees no new overflow. DEFAULTS only; the env
    # override path is unchanged.
    "self_model": int(os.getenv("REGION_BUDGET_SELF_MODEL", "700")),
    "user_model": int(os.getenv("REGION_BUDGET_USER_MODEL", "1000")),
    "durable_memory": int(os.getenv("REGION_BUDGET_MEMORY", "2000")),
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
    # Living Self Act 3: the gated cognitive-pass monologue renders here as a
    # role="system", region="internal" memory. 500 tokens (~2000 chars) caps a
    # runaway monologue; assemble_regions truncates per the budget. Without this
    # row the cap would fall back to DEFAULT_REGION_BUDGETS.get == 1000.
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
    """Check if current time is within active hours (local timezone)."""
    current_time = now_local().strftime("%H:%M")
    return HEARTBEAT_ACTIVE_START <= current_time <= HEARTBEAT_ACTIVE_END


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


def get_belief_evolve_settings(
    enabled: bool | None = None,
    min_supporting_paths: int | None = None,
    min_overlap: float | None = None,
    max_bytes: int | None = None,
    min_correctness: float | None = None,
    min_fidelity: float | None = None,
    corpus_path: str | None = None,
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
    return BeliefEvolveSettings(
        enabled=enabled,
        min_supporting_paths=min_supporting_paths,
        min_overlap=min_overlap,
        max_bytes=max_bytes,
        min_correctness=min_correctness,
        min_fidelity=min_fidelity,
        corpus_path=corpus_path,
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
    if tick_interval_hours is None:
        tick_interval_hours = float(
            os.getenv("PERSONA_LEARNING_TICK_INTERVAL", "12")
        )
    if silent_skip_window_hours is None:
        silent_skip_window_hours = float(
            os.getenv("PERSONA_LEARNING_SILENT_SKIP_WINDOW", "24")
        )
    return PersonaLearningSettings(
        enabled=enabled,
        tick_interval_hours=tick_interval_hours,
        silent_skip_window_hours=silent_skip_window_hours,
    )


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


class TurnProgressSettings(NamedTuple):
    """Effective turn-progress streaming knobs (call-time resolved)."""

    activity_enabled: bool
    edit_min_interval_s: float
    stop_button_enabled: bool


def get_turn_progress_settings(
    activity_enabled: bool | None = None,
    edit_min_interval_s: float | None = None,
    stop_button_enabled: bool | None = None,
) -> TurnProgressSettings:
    """Resolve turn-progress streaming knobs at CALL TIME (Rule 1) — Phase 5.

    The chat router's placeholder message ("Thinking...") progressively edits
    into a live status while a long turn runs: elapsed time, tool-call count,
    and the CURRENT tool activity ("Reading budget.md") derived from runtime
    tool events, plus a Stop button wired to the same cancel primitive as the
    dashboard's /stop endpoint. Knobs:

        TURN_PROGRESS_ACTIVITY_ENABLED ("true") — master switch for the live
            tool-activity label. When false the ticker keeps the legacy
            "Working... (Ns) | N tool calls" text only.
        TURN_PROGRESS_EDIT_MIN_INTERVAL_S ("1.5") — minimum seconds between
            placeholder edits. Telegram allows ~1 edit/sec/chat and Discord
            ~5 edits/5s/channel; 1.5s sits safely under both. Trailing state
            always flushes after the interval.
        TURN_PROGRESS_STOP_BUTTON_ENABLED ("true") — attach a Stop button to
            the progress placeholder (Telegram inline keyboard / Discord
            component). Stopping your own in-flight turn is not an external
            mutation — no default-deny gate; scope is the tapper's own
            conversation only.

    None-sentinel pattern: explicit values pass through; ``None`` resolves the
    matching env var inside the body so ``monkeypatch.setenv`` takes effect on
    the next call with no module reload. All consumers are fail-open — a
    progress-edit failure never fails the turn.
    """
    if activity_enabled is None:
        activity_enabled = (
            os.getenv("TURN_PROGRESS_ACTIVITY_ENABLED", "true").lower() == "true"
        )
    if edit_min_interval_s is None:
        try:
            edit_min_interval_s = float(
                os.getenv("TURN_PROGRESS_EDIT_MIN_INTERVAL_S", "1.5")
            )
        except (TypeError, ValueError):
            edit_min_interval_s = 1.5
    if stop_button_enabled is None:
        stop_button_enabled = (
            os.getenv("TURN_PROGRESS_STOP_BUTTON_ENABLED", "true").lower() == "true"
        )
    return TurnProgressSettings(
        activity_enabled=activity_enabled,
        edit_min_interval_s=max(0.0, edit_min_interval_s),
        stop_button_enabled=stop_button_enabled,
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
    (unapproved) delegation, which no shipped code path exercises yet
    (operator resolution #4, 2026-07-05). The
    ``cofounder_delegation`` kill switch
    (``HOMIE_KILLSWITCH_COFOUNDER_DELEGATION``) sits on top of BOTH paths —
    it is the emergency stop for the whole delegation surface.

    Knobs:
        COFOUNDER_DELEGATION_ENABLED ("false") — autonomous-delegation flag.
            Ships OFF; flipping it is the operator's end-state call after
            propose-only has earned trust. Approved lines do not need it.
        COFOUNDER_MAX_ASSIGNMENTS_PER_DAY ("5") — cap on delegations per
            local day across all personas (approved + autonomous combined).
        COFOUNDER_MAX_INFLIGHT_PER_PERSONA ("1") — cap on un-acked
            ``cofounder_assignment`` mailbox deliveries per persona
            (physical mailbox state is the in-flight truth — Rule 2).
    """
    if enabled is None:
        enabled = (
            os.getenv("COFOUNDER_DELEGATION_ENABLED", "false").strip().lower()
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
