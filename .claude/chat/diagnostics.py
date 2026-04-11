"""System diagnostics collector for The Homie framework."""

from __future__ import annotations

import json as json_mod
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Ensure scripts dir is importable
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config import CHAT_DB_PATH, DATABASE_PATH, STATE_DIR  # noqa: E402
from runtime.base import RUNTIME_LANE_CLAUDE_NATIVE, RUNTIME_LANE_GENERIC  # noqa: E402

_START_TIME = time.monotonic()


@dataclass
class DiagnosticsReport:
    timestamp: str
    uptime_seconds: float

    # Cognition
    cognition_available: bool = False
    cognition_moves: dict[str, bool] = field(default_factory=dict)

    # Recall
    recall_last_query: str | None = None
    recall_last_tier: str | None = None
    recall_last_count: int = 0
    recall_last_latency_ms: float | None = None

    # Memory DB
    memory_doc_count: int = 0
    memory_last_indexed: str | None = None
    memory_embedding_status: str = "unknown"

    # Runtime
    runtime_lanes: dict[str, str] = field(default_factory=dict)
    runtime_providers: dict[str, str] = field(default_factory=dict)
    runtime_selected_lane: str = "auto"
    runtime_selected_generic_provider: str | None = None
    runtime_generic_text_route: list[str] = field(default_factory=list)
    runtime_generic_tool_route: list[str] = field(default_factory=list)

    # Sessions
    sessions_active: int = 0
    sessions_total_messages: int = 0
    sessions_total_cost_usd: float = 0.0

    # Adapters (only populated when called from inside the bot)
    adapters_connected: dict[str, bool] = field(default_factory=dict)


def collect_diagnostics() -> DiagnosticsReport:
    """Collect full system diagnostics."""
    from datetime import datetime

    report = DiagnosticsReport(
        timestamp=datetime.now().isoformat(),
        uptime_seconds=round(time.monotonic() - _START_TIME, 1),
    )

    _check_cognition(report)
    _check_recall(report)
    _check_memory_db(report)
    _check_runtime(report)
    _check_sessions(report)

    return report


def _check_cognition(report: DiagnosticsReport) -> None:
    """Check which cognition modules are importable."""
    moves: dict[str, bool] = {}

    try:
        from cognition.recall import run_recall_pipeline  # noqa: F401

        moves["move1_recall"] = True
    except ImportError:
        moves["move1_recall"] = False

    try:
        from cognition.promotion import run_promotion_pipeline  # noqa: F401

        moves["move2_promotion"] = True
    except ImportError:
        moves["move2_promotion"] = False

    try:
        from cognition.continuity import load_continuity  # noqa: F401

        moves["move2_continuity"] = True
    except ImportError:
        moves["move2_continuity"] = False

    try:
        from cognition.processes import detect_process  # noqa: F401

        moves["move3_processes"] = True
    except ImportError:
        moves["move3_processes"] = False

    try:
        from cognition.skills import build_skill_index  # noqa: F401

        moves["move3_skills"] = True
    except ImportError:
        moves["move3_skills"] = False

    try:
        from cognition.self_model import InferenceTracker  # noqa: F401

        moves["move3_self_model"] = True
    except ImportError:
        moves["move3_self_model"] = False

    report.cognition_moves = moves
    report.cognition_available = any(moves.values())


def _check_recall(report: DiagnosticsReport) -> None:
    """Read last recall event from recall-log.json."""
    log_path = STATE_DIR / "recall-log.json"
    if not log_path.exists():
        return
    try:
        data = json_mod.loads(log_path.read_text())
        if data:
            last = data[-1] if isinstance(data, list) else data
            report.recall_last_query = last.get("query", "")
            report.recall_last_tier = last.get("tier", "")
            report.recall_last_count = last.get("results", 0)
            report.recall_last_latency_ms = last.get("latency_ms")
    except Exception:
        pass


def _check_memory_db(report: DiagnosticsReport) -> None:
    """Query memory.db for document count and last indexed time."""
    db_path = DATABASE_PATH
    if not db_path.exists():
        report.memory_embedding_status = "no_database"
        return
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
            report.memory_doc_count = row[0] if row else 0
            report.memory_embedding_status = "ready"
        except Exception:
            report.memory_embedding_status = "unavailable"
        finally:
            conn.close()
    except Exception:
        report.memory_embedding_status = "error"


def _check_runtime(report: DiagnosticsReport) -> None:
    """Check runtime provider health and availability."""
    try:
        from runtime.health import is_profile_available
        from runtime.profiles import build_profile_for_provider, normalize_provider
        from runtime.routing import GENERIC_TEXT_ROUTE, GENERIC_TOOL_ROUTE
        from runtime.selection import resolve_runtime_selection

        selection = resolve_runtime_selection()
        report.runtime_lanes = {
            RUNTIME_LANE_CLAUDE_NATIVE: "ON" if build_profile_for_provider("claude", key_prefix="diagnostics") else "OFF",
            RUNTIME_LANE_GENERIC: "ON",
        }
        report.runtime_selected_lane = selection.lane or "auto"
        report.runtime_selected_generic_provider = selection.generic_provider
        report.runtime_generic_text_route = [
            normalize_provider(provider)
            for provider in GENERIC_TEXT_ROUTE
        ]
        report.runtime_generic_tool_route = [
            normalize_provider(provider)
            for provider in GENERIC_TOOL_ROUTE
        ]

        providers_to_check: list[str] = []
        for provider in ("claude", *GENERIC_TEXT_ROUTE, *GENERIC_TOOL_ROUTE):
            normalized = normalize_provider(provider)
            if normalized not in providers_to_check:
                providers_to_check.append(normalized)

        for provider in providers_to_check:
            try:
                profile = build_profile_for_provider(
                    provider, key_prefix="diagnostics"
                )
                if profile and is_profile_available(profile):
                    report.runtime_providers[provider] = "ON"
                else:
                    report.runtime_providers[provider] = "OFF"
            except Exception:
                report.runtime_providers[provider] = "OFF"
    except ImportError:
        report.runtime_providers = {"error": "runtime not importable"}


def _check_sessions(report: DiagnosticsReport) -> None:
    """Aggregate session statistics."""
    try:
        from session import get_session_store

        store = get_session_store(CHAT_DB_PATH)
        sessions = store.list_active()
        report.sessions_active = len(sessions)
        report.sessions_total_messages = sum(s.message_count for s in sessions)
        report.sessions_total_cost_usd = sum(s.total_cost_usd for s in sessions)
    except Exception:
        pass


def check_environment() -> list[tuple[str, str, str]]:
    """Verify prerequisites. Returns list of (level, message, hint)."""
    issues: list[tuple[str, str, str]] = []

    # Python version
    if sys.version_info < (3, 12):  # noqa: UP036 — intentional runtime check for users
        issues.append(("error", f"Python {sys.version} — need 3.12+", "Install Python 3.12+"))

    # uv installed
    import shutil

    if not shutil.which("uv"):
        issues.append((
            "warn",
            "uv not found on PATH",
            "Install: curl -LsSf https://astral.sh/uv/install.sh | sh",
        ))

    # .env file exists
    env_path = _SCRIPTS_DIR / ".env"
    if not env_path.exists():
        issues.append(("error", "No .env file found", f"Copy .env.example to {env_path}"))

    # At least one adapter configured
    try:
        from dotenv import dotenv_values

        env = dotenv_values(env_path) if env_path.exists() else {}
    except ImportError:
        env = {}

    has_adapter = any(
        env.get(k)
        for k in [
            "TELEGRAM_BOT_TOKEN",
            "SLACK_BOT_TOKEN",
            "DISCORD_BOT_TOKEN",
            "WHATSAPP_ACCESS_TOKEN",
            "RELAY_AUTH_TOKEN",
        ]
    )
    if not has_adapter:
        issues.append(("warn", "No chat adapter configured", "Set TELEGRAM_BOT_TOKEN in .env"))

    # Runtime provider available
    has_runtime = (
        any(env.get(k) for k in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"])
        or shutil.which("claude")
        or shutil.which("codex")
    )
    if not has_runtime:
        issues.append((
            "error",
            "No runtime provider available",
            "Install Claude Code CLI or set OPENROUTER_API_KEY",
        ))

    # Vault exists
    from config import MEMORY_DIR

    if not MEMORY_DIR.exists():
        issues.append((
            "warn",
            f"Memory vault not found at {MEMORY_DIR}",
            "Run `thehomie setup` to create it",
        ))

    return issues
