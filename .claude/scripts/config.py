"""
Configuration for The Homie heartbeat system.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Load environment variables from .env in scripts directory
load_dotenv(Path(__file__).parent / ".env", override=True)

# === Paths ===
SCRIPTS_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPTS_DIR.parent.parent  # thehomie
CLAUDE_DIR = PROJECT_ROOT / ".claude"
MEMORY_DIR = PROJECT_ROOT / "TheHomie" / "Memory"

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
DATA_DIR = CLAUDE_DIR / "data"
DATABASE_PATH = DATA_DIR / "memory.db"
DATABASE_URL = os.getenv("DATABASE_URL", "")

# State files — per-machine operational data, NOT synced via Obsidian
STATE_DIR = DATA_DIR / "state"
HEARTBEAT_STATE_FILE = STATE_DIR / "heartbeat-state.json"

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

# === Embedding Configuration ===
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIMENSIONS = 384
EMBEDDING_CACHE_DIR = DATA_DIR / "models"

# === Integration Configuration (Phase 5) ===
INTEGRATIONS_DIR = SCRIPTS_DIR / "integrations"

# Google OAuth
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
CHAT_MAX_TURNS = int(os.getenv("CHAT_MAX_TURNS", "25"))
CHAT_MAX_BUDGET_USD = float(os.getenv("CHAT_MAX_BUDGET_USD", "2.0"))
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
VOICE_TTS_ENGINE = os.getenv("VOICE_TTS_ENGINE", "edge")  # "edge" or "openai"
VOICE_TTS_VOICE_EDGE = os.getenv("VOICE_TTS_VOICE_EDGE", "en-US-GuyNeural")
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
    "self_model": int(os.getenv("REGION_BUDGET_SELF_MODEL", "200")),
    "user_model": int(os.getenv("REGION_BUDGET_USER_MODEL", "1000")),
    "durable_memory": int(os.getenv("REGION_BUDGET_MEMORY", "2000")),
    "continuity": int(os.getenv("REGION_BUDGET_CONTINUITY", "500")),
    "recalled_memory": int(os.getenv("REGION_BUDGET_RECALLED", "750")),
    "procedural_memory": int(os.getenv("REGION_BUDGET_PROCEDURAL", "500")),
    "prefetched_context": int(os.getenv("REGION_BUDGET_PREFETCHED", "3000")),
}

# Staging store
STAGING_STORE_PATH = STATE_DIR / "memory-candidates.jsonl"
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
WHATSAPP_WEBHOOK_PORT = int(os.getenv("WHATSAPP_WEBHOOK_PORT", "8443"))

# Health check
HEALTH_CHECK_PORT = int(os.getenv("HEALTH_CHECK_PORT", "8787"))

# Self-model inference tracking
INFERENCE_STATE_FILE = STATE_DIR / "self-model-inferences.json"
INFERENCE_DECAY_DAYS = int(os.getenv("INFERENCE_DECAY_DAYS", "14"))
INFERENCE_CONFIRM_BOOST = float(os.getenv("INFERENCE_CONFIRM_BOOST", "0.1"))
INFERENCE_DECAY_RATE = float(os.getenv("INFERENCE_DECAY_RATE", "0.05"))
INFERENCE_MIN_CONFIDENCE = float(os.getenv("INFERENCE_MIN_CONFIDENCE", "0.3"))

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
        "OPENAI_API_KEY", "VOICE_STT_MODEL", "VOICE_TTS_ENGINE",
        "VOICE_TTS_VOICE_EDGE", "VOICE_TTS_VOICE_OPENAI",
        "CHAT_MAX_TURNS", "CHAT_MAX_BUDGET_USD",
        "GOOGLE_CALENDAR_ID", "HEARTBEAT_INTERVAL_MINUTES",
        "HEARTBEAT_ACTIVE_START", "HEARTBEAT_ACTIVE_END",
    ]

    module = sys.modules[__name__]
    old_values = {k: getattr(module, k, None) for k in reloadable_keys}

    # Re-read .env
    load_dotenv(Path(__file__).parent / ".env", override=True)

    # Re-evaluate from env
    changes: dict[str, tuple[str, str]] = {}
    new_map: dict[str, str | int | float] = {
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", ""),
        "VOICE_STT_MODEL": os.getenv("VOICE_STT_MODEL", "whisper-1"),
        "VOICE_TTS_ENGINE": os.getenv("VOICE_TTS_ENGINE", "edge"),
        "VOICE_TTS_VOICE_EDGE": os.getenv("VOICE_TTS_VOICE_EDGE", "en-US-GuyNeural"),
        "VOICE_TTS_VOICE_OPENAI": os.getenv("VOICE_TTS_VOICE_OPENAI", "alloy"),
        "CHAT_MAX_TURNS": int(os.getenv("CHAT_MAX_TURNS", "25")),
        "CHAT_MAX_BUDGET_USD": float(os.getenv("CHAT_MAX_BUDGET_USD", "2.0")),
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
