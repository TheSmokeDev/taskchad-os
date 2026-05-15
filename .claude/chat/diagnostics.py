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

from config import CHAT_DB_PATH, DATABASE_PATH, ENV_FILE, STATE_DIR  # noqa: E402
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
    runtime_provider_details: dict[str, str] = field(default_factory=dict)
    runtime_auth_issues: dict[str, str] = field(default_factory=dict)

    # Sessions
    sessions_active: int = 0
    sessions_total_messages: int = 0
    sessions_total_cost_usd: float = 0.0

    # Clear/session lifecycle
    clear_lifecycle_recent_failures: int = 0
    clear_lifecycle_last_failure: str | None = None
    clear_lifecycle_last_failure_at: str | None = None

    # Adapters (only populated when called from inside the bot)
    adapters_connected: dict[str, bool] = field(default_factory=dict)

    # Capability/toolset registry (PRP-1b)
    capabilities: list[dict] = field(default_factory=list)
    toolsets: dict[str, list[str]] = field(default_factory=dict)


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
    _check_clear_lifecycle(report)
    _check_capabilities(report)

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
        from runtime.auth_profiles import codex_auth_status, resolve_codex_auth_profile
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
                if provider == "openai-codex":
                    codex_status = codex_auth_status(resolve_codex_auth_profile())
                    if codex_status.detail:
                        report.runtime_provider_details[provider] = codex_status.detail
                    issue = _runtime_auth_issue(provider, codex_status.detail)
                    if issue:
                        report.runtime_auth_issues[provider] = issue
                    if not codex_status.available:
                        report.runtime_providers[provider] = "OFF"
                        continue

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


def _runtime_auth_issue(provider: str, detail: str) -> str | None:
    """Return an operator-facing auth issue when a provider has stale credentials."""
    if provider != "openai-codex":
        return None
    detail_lower = detail.lower()
    stale_markers = ("refresh_token_reused", "token_expired")
    if not any(marker in detail_lower for marker in stale_markers):
        return None
    return (
        "Codex CLI auth is stale. Run `codex login`, then rerun "
        "`uv run thehomie doctor`. Detail: "
        + _short_detail(detail)
    )


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


def _check_clear_lifecycle(report: DiagnosticsReport) -> None:
    """Summarize recent /clear lifecycle hook failures from the state log."""
    log_path = STATE_DIR / "clear-lifecycle-events.jsonl"
    if not log_path.exists():
        return

    try:
        rows = [
            json_mod.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()[-50:]
            if line.strip()
        ]
    except Exception:
        return

    failure_count = 0
    last_failure: tuple[str, str, str] | None = None
    for row in rows:
        timestamp = str(row.get("timestamp", ""))
        for event in row.get("events", []) or []:
            if not isinstance(event, dict):
                continue
            status = str(event.get("status", ""))
            if status not in {"error", "warn"}:
                continue
            failure_count += 1
            step = str(event.get("step", "unknown"))
            detail = str(event.get("detail", status))
            last_failure = (timestamp, step, detail)

    report.clear_lifecycle_recent_failures = failure_count
    if last_failure:
        timestamp, step, detail = last_failure
        report.clear_lifecycle_last_failure_at = timestamp
        report.clear_lifecycle_last_failure = f"{step}: {_short_detail(detail)}"


def _check_capabilities(report: DiagnosticsReport) -> None:
    """Populate capabilities and toolsets from the capability registry.

    Atomic: either both fields are populated or both stay at defaults.
    Partial state would mislead diagnostic consumers.
    """
    try:
        # M6 fix: ensure integrations source is registered before list_capabilities runs.
        # Importing the module fires register_aggregator("integrations", ...) at module bottom.
        import integrations.registry  # noqa: F401
        # PRP-1c: same pattern for runtime overlays -- fires register_aggregator("runtime_overlays", ...)
        import runtime.overlays  # noqa: F401
        from runtime.capabilities import list_capabilities, resolve_toolset
        from runtime.toolsets import TOOLSETS

        caps = list_capabilities(sources=["chat_extensions", "integrations", "runtime_overlays"])
        _caps_local = [
            {
                "id": c.id,
                "display_name": c.display_name,
                "enabled": c.enabled,
                "source": c.source,
            }
            for c in caps
        ]
        _toolsets_local = {
            name: resolve_toolset(name, registry=TOOLSETS)
            for name in TOOLSETS
        }
        # Atomic assignment: only mutate the report after both locals built successfully.
        report.capabilities = _caps_local
        report.toolsets = _toolsets_local
    except Exception:
        # Fail-open: leave capabilities=[] and toolsets={} at defaults.
        # No partial state — both fields move together.
        pass


def _short_detail(value: str, *, max_chars: int = 220) -> str:
    text = " ".join(value.strip().split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


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
    env_path = ENV_FILE
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
