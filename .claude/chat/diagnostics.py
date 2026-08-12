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
    cognitive_loop: dict[str, object] = field(default_factory=dict)

    # Called-shots ledger (epic #186 T1) — on/off must be PROVABLE, not guessed
    called_shots: dict[str, object] = field(default_factory=dict)

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
    runtime_selected_model: str | None = None
    runtime_configured_models: dict[str, str] = field(default_factory=dict)
    runtime_model_warnings: list[str] = field(default_factory=list)
    runtime_generic_text_route: list[str] = field(default_factory=list)
    runtime_generic_tool_route: list[str] = field(default_factory=list)
    runtime_provider_details: dict[str, str] = field(default_factory=dict)
    runtime_auth_issues: dict[str, str] = field(default_factory=dict)

    # Live agent/factory execution safety
    live_execution: dict[str, object] = field(default_factory=dict)

    # Browser readiness
    browser: dict[str, object] = field(default_factory=dict)

    # Ghost Phone (the Homie's own background Android — P4.1 A2)
    ghost: dict[str, object] = field(default_factory=dict)

    # Persona curriculum (config-only; never creates ledgers or calls models)
    curriculum: dict[str, object] = field(default_factory=dict)

    # Crypto Homie scheduled round (sanitized profile + ledger health only)
    crypto_round: dict[str, object] = field(default_factory=dict)

    # Compiled persona readiness (six independent physical-state axes)
    persona_readiness: dict[str, dict[str, object]] = field(default_factory=dict)

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
    buzz: dict[str, object] = field(default_factory=dict)

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
    _check_cognitive_loop(report)
    _check_called_shots(report)
    _check_recall(report)
    _check_memory_db(report)
    _check_runtime(report)
    _check_live_execution(report)
    _check_browser(report)
    _check_ghost(report)
    _check_curriculum(report)
    _check_crypto_round(report)
    _check_persona_readiness(report)
    _check_sessions(report)
    _check_clear_lifecycle(report)
    _check_buzz(report)
    _check_capabilities(report)

    return report


def _check_buzz(report: DiagnosticsReport) -> None:
    """Read the profile-owned, secret-free Buzz runtime snapshot."""
    try:
        from buzz_status import read_buzz_status

        report.buzz = read_buzz_status()
    except Exception as exc:
        report.buzz = {
            "enabled": False,
            "state": "failed",
            "active_transport": "none",
            "last_error": _short_detail(str(exc)),
        }


def _check_curriculum(report: DiagnosticsReport) -> None:
    """Collect curriculum readiness without touching profile runtime state."""
    try:
        from curriculum.config import get_curriculum_settings
        from personas.lifecycle import list_profiles
        from security import kill_switches

        configured = 0
        enabled = 0
        errors: list[str] = []
        for profile in list_profiles():
            if profile.is_default:
                continue
            try:
                settings = get_curriculum_settings(profile.name)
            except Exception as exc:
                errors.append(f"{profile.name}: {exc}")
                continue
            if settings.sources or settings.domain != "general":
                configured += 1
            if settings.enabled:
                enabled += 1
        report.curriculum = {
            "available": True,
            "configured_personas": configured,
            "enabled_personas": enabled,
            "kill_switch_disabled": kill_switches.is_disabled(
                "persona_curriculum"
            ),
            "config_errors": errors[:10],
        }
    except Exception as exc:
        report.curriculum = {
            "available": False,
            "configured_personas": 0,
            "enabled_personas": 0,
            "kill_switch_disabled": False,
            "config_errors": [str(exc)],
        }


def _check_crypto_round(report: DiagnosticsReport) -> None:
    """Collect fail-open, source-ID-free scheduled round health."""
    try:
        from crypto_round.config import load_market_round_settings
        from crypto_round.db import CryptoRoundDB
        from crypto_round.provenance import status as provenance_status

        settings = load_market_round_settings()
        db = CryptoRoundDB(initialize=False)
        ledger = db.status()
        tape = db.tape_status()
        from lib import x_rate
        latest = ledger.get("latest")
        if isinstance(latest, dict):
            latest = {
                key: value
                for key, value in latest.items()
                if key not in {"output_json", "error"}
            }
        report.crypto_round = {
            "available": True,
            "enabled": settings.enabled,
            "cadence_hours": settings.every_hours,
            "scheduled_tool_max_turns": settings.max_turns,
            "source_tier_counts": settings.as_public_dict()["discord_channel_counts"],
            "latest": latest,
            "state_counts": ledger.get("state_counts", {}),
            "open_paper_calls": ledger.get("open_paper_calls", 0),
            "conversation_tape": tape,
            "source_receipt_count": len(db.source_receipts()),
            "source_receipt_history_count": db.source_receipt_history_count(),
            "x_rate": x_rate.status(),
            "last_failure": bool(
                isinstance(ledger.get("latest"), dict)
                and ledger["latest"].get("state") == "failed"
            ),
            "dependency_provenance": provenance_status(),
        }
    except Exception as exc:
        report.crypto_round = {
            "available": False,
            "enabled": False,
            "error": _short_detail(str(exc)),
        }


def _check_persona_readiness(report: DiagnosticsReport) -> None:
    """Collect compiled profiles without trusting provisioner receipts."""

    try:
        from personas import readiness
    except Exception as exc:
        report.persona_readiness = {
            "_collector": {
                "persona_id": "_collector",
                "status": "ERROR",
                "reasons": [_short_detail(str(exc))],
            }
        }
        return

    try:
        report.persona_readiness = (
            readiness.collect_persona_readiness_inventory()
        )
    except Exception as exc:
        report.persona_readiness = {
            "_collector": readiness.build_persona_readiness_error_snapshot(
                "_collector",
                str(exc),
            ).as_dict()
        }


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


def _check_cognitive_loop(report: DiagnosticsReport) -> None:
    """Populate code-backed cognitive-loop subsystem status."""
    try:
        from cognition.status import collect_cognitive_loop_status

        report.cognitive_loop = collect_cognitive_loop_status()
    except Exception as exc:
        report.cognitive_loop = {
            "overall": "unknown",
            "state_counts": {"unknown": 1},
            "subsystems": {
                "collector": {
                    "state": "unknown",
                    "evidence": (
                        "cognitive-loop status collector failed: "
                        f"{_short_detail(str(exc))}"
                    ),
                    "details": {},
                }
            },
            "next_actions": ["Fix cognitive-loop status collector import/runtime failure."],
        }


def _check_called_shots(report: DiagnosticsReport) -> None:
    """Physical on/off + ledger state for the called-shots feature (T1 #187).

    ``enabled`` is the EFFECTIVE state: the CALLED_SHOTS_ENABLED soft-toggle
    AND the operator kill-switch, both resolved live (Rule 2 — never cached).
    """
    try:
        from security import kill_switches  # Rule 3: module-attribute lookup

        from config import get_called_shots_settings

        settings = get_called_shots_settings()
        ks_disabled = kill_switches.is_disabled("called_shots")
        db_path = Path(settings.db_path)
        info: dict[str, object] = {
            "enabled": bool(settings.enabled) and not ks_disabled,
            "kill_switch_disabled": ks_disabled,
            "db_present": db_path.exists(),
            "open_count": 0,
        }
        if db_path.exists():
            try:
                # Read-only URI connect: mode=ro can never CREATE a DB (the
                # plain-connect TOCTOU would materialize an empty file if the
                # DB vanished between exists() and connect), and the short
                # busy_timeout keeps a locked DB from stalling diagnostics.
                conn = sqlite3.connect(
                    f"file:{db_path.as_posix()}?mode=ro", uri=True
                )
                try:
                    conn.execute("PRAGMA busy_timeout=250")
                    row = conn.execute(
                        "SELECT COUNT(*) FROM called_shots WHERE status = 'open'"
                    ).fetchone()
                    info["open_count"] = int(row[0])
                finally:
                    conn.close()
            except sqlite3.Error:
                pass
        report.called_shots = info
    except Exception as exc:
        report.called_shots = {
            "enabled": None,
            "error": _short_detail(str(exc)),
        }


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
        from runtime.model_control import (
            configured_runtime_models,
            runtime_model_warnings,
            selected_runtime_model,
        )
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
        report.runtime_selected_model = selected_runtime_model(selection)
        report.runtime_configured_models = configured_runtime_models()
        report.runtime_model_warnings = runtime_model_warnings(selection)
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


def _check_live_execution(report: DiagnosticsReport) -> None:
    """Expose the framework-wide live agent/factory opt-in state."""
    try:
        from orchestration.live_safety import live_execution_status

        report.live_execution = live_execution_status().to_dict()
    except Exception as exc:
        report.live_execution = {
            "mode": "unknown",
            "live_agent_run_allowed": False,
            "default_contract": "dry-run/read-only",
            "refusal_message": _short_detail(str(exc)),
            "lower_level_gates": [
                "browserops_workflow_policy",
                "direct_integration_capability_policy",
                "cabinet_tool_policy",
            ],
        }


def _check_browser(report: DiagnosticsReport) -> None:
    """Populate URL-free visible-browser readiness for operator surfaces."""

    try:
        from browser_control import browser_readiness

        report.browser = browser_readiness()
    except Exception as exc:
        report.browser = {
            "enabled": False,
            "status": "attention",
            "cdp_port": None,
            "cdp_reachable": False,
            "browser": "unknown",
            "visible_guard": "unknown",
            "tab_count": 0,
            "agent_browser_command_source": "unknown",
            "reason": _short_detail(str(exc)),
        }


def _check_ghost(report: DiagnosticsReport) -> None:
    """Populate the ghost snapshot (config-gated; disabled path never touches
    adb). Reuses browser_ops.build_ghost_state so diagnostics and the engine
    ghost-awareness pack share one fail-open source of truth (Rule 2)."""

    try:
        from browser_ops import build_ghost_state

        report.ghost = build_ghost_state()
    except Exception as exc:
        report.ghost = {
            "enabled": False,
            "running": False,
            "booted": False,
            "serial": None,
            "avd": None,
            "cdp_port": None,
            "cdp_reachable": False,
            "readiness_status": "unknown",
            "detail": _short_detail(str(exc)),
        }


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
            "BUZZ_RELAY_URL",
        ]
    )
    if not has_adapter:
        issues.append(("warn", "No chat adapter configured", "Set TELEGRAM_BOT_TOKEN in .env"))

    buzz_values = {
        key: str(env.get(key) or "").strip()
        for key in ("BUZZ_RELAY_URL", "BUZZ_PRIVATE_KEY", "BUZZ_ALLOWED_PUBKEYS")
    }
    if any(buzz_values.values()) and not all(buzz_values.values()):
        missing = ", ".join(key for key, value in buzz_values.items() if not value)
        issues.append(("error", f"Buzz configuration incomplete: {missing}", "Complete the active profile .env"))
    if all(buzz_values.values()):
        import re

        cli = str(env.get("BUZZ_CLI_PATH") or "buzz")
        resolved = cli if Path(cli).is_file() else shutil.which(cli)
        if not resolved:
            issues.append(("error", "Buzz CLI not found", "Install Buzz 0.5.x or set BUZZ_CLI_PATH"))
        else:
            try:
                import subprocess

                version = subprocess.run(
                    [resolved, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                match = re.search(r"(?<!\d)(\d+\.\d+(?:\.\d+)?)", version.stdout)
                if version.returncode or not match:
                    issues.append(("warn", "Buzz CLI version unknown", "Pilot contract is Buzz 0.5.x"))
                elif not match.group(1).startswith("0.5."):
                    issues.append(("warn", f"Buzz CLI {match.group(1)} is unverified", "Use Buzz 0.5.x for the pilot"))
            except (OSError, subprocess.SubprocessError):
                issues.append(("warn", "Buzz CLI compatibility check failed", "Pilot contract is Buzz 0.5.x"))

    # Runtime provider available
    has_runtime = (
        any(env.get(k) for k in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "KIMI_API_KEY"])
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

    # Persona profile inventory (issue #109) — a profile missing its
    # memory/ dir takes every turn with ZERO knowledge context, silently.
    # Rule 2: inspect reads physical disk state, so a failed boot-guard
    # repair is still visible here (no event log needed). Whole block is
    # fail-open: doctor never crashes on a personas import/inspect failure.
    try:
        from personas.lifecycle import inspect_profile_inventory, list_profiles

        for info in list_profiles():
            if info.is_default:
                continue  # install-dir layout — inventory contract N/A
            try:
                rep = inspect_profile_inventory(info.name)
            except Exception:  # noqa: BLE001 — one bad profile never hides the rest
                continue
            if not (info.path / "memory").is_dir():
                issues.append((
                    "error",
                    f"Profile '{info.name}' has NO memory/ dir — personas "
                    "run with empty context, silently",
                    f"Run: thehomie profile repair {info.name}",
                ))
            elif not rep.healthy:
                issues.append((
                    "warn",
                    f"Profile '{info.name}' memory inventory incomplete "
                    f"({rep.missing_count} missing)",
                    f"Run: thehomie profile repair {info.name}",
                ))
            if rep.orphaned_root_identity_files:
                issues.append((
                    "warn",
                    f"Profile '{info.name}' has orphaned root identity "
                    "file(s) the loader never reads: "
                    f"{', '.join(rep.orphaned_root_identity_files)}",
                    "Move them into memory/ manually; repair never auto-moves",
                ))
    except Exception:  # noqa: BLE001
        pass

    return issues
