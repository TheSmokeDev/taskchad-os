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
    # Living Self Act 3: the gated cognitive-pass monologue renders here as a
    # role="system", region="internal" memory. 500 tokens (~2000 chars) caps a
    # runaway monologue; assemble_regions truncates per the budget. Without this
    # row the cap would fall back to DEFAULT_REGION_BUDGETS.get == 1000.
    "internal": int(os.getenv("REGION_BUDGET_INTERNAL_MONOLOGUE", "500")),
    "recent_conversation": int(os.getenv("REGION_BUDGET_RECENT_CONVERSATION", "600")),
}

RECENT_CONVERSATION_COUNT = int(os.getenv("RECENT_CONVERSATION_COUNT", "6"))

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
SESSION_TURN_THRESHOLD = int(os.getenv("SESSION_TURN_THRESHOLD", "30"))

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


def get_inference_extraction_settings(
    dedup_threshold: float | None = None,
    extraction_enabled: bool | None = None,
    max_claims: int | None = None,
    min_chars: int | None = None,
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
    return InferenceExtractionSettings(
        dedup_threshold=dedup_threshold,
        extraction_enabled=extraction_enabled,
        max_claims=max_claims,
        min_chars=min_chars,
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
        old_val = old_values.get(key)
        if old_val != new_val:
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
