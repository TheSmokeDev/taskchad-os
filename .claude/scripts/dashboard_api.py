"""FastAPI router for the dashboard slice (PRD-8 Phase 3 / WS2).

30 endpoints under ``/api/`` mounted onto the existing orchestration
FastAPI app at port 4322. Inherits the ``auth_middleware`` Bearer token
from ``orchestration/api.py`` — ZERO new auth path.

Slice ownership:
  * The CODEOWNERS glob ``.claude/scripts/dashboard_*.py`` covers this
    module. dashboard-owner reviews every diff.
  * NO YAML import — all config.yaml validation goes through
    ``personas.validate_config_yaml_text`` (Q5 single-yaml-surface lock).
  * NO direct convoy SQL — ``GET /api/agents/{id}/tasks`` calls
    ``convoy_service.list_subtasks_by_agent`` (R3 NB3 orchestration
    boundary).
  * NO direct ``shared.py`` re-implementations — bot lifecycle goes
    through ``dashboard_bot_lifecycle.py`` (R3 NM1 delegation).

Anti-pattern compliance:
  * Rule 1 — every public function uses ``param: T | None = None``
    sentinel; ``config.X`` resolution happens INSIDE the function body.
  * Rule 2 — meta is derived state. Persona list reads disk on every
    call; hard-delete response is derived from
    ``personas.lifecycle._profile_root(persona_id).exists()``, NEVER
    from try/except. ``is_running`` checks go through
    ``dashboard_bot_lifecycle.is_running`` which uses
    ``shared.is_pid_alive``.
  * Rule 3 — N/A (no optional-provider SDK touched directly here;
    Langfuse exists in convoy_service.list_subtasks_by_agent).

PRP anchor: PRPs/active/PRP-prd-8-phase-3-dashboard-port.md §1582-1626.
JSON criteria: PRPs/contracts/prd-8-phase-3.json.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import config
import dashboard_bot_lifecycle
import personas
from dashboard_db import get_connection
from personas import lifecycle as _lifecycle
from personas.lifecycle import (
    LifecycleError,
    create_profile,
    delete_profile,
    resolve_profile_root,
)

logger = logging.getLogger(__name__)

_CHAT_DIR = Path(__file__).resolve().parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

# PRD-8 Phase 7b WS1 (codex post-build F1) — log-message redaction at every
# persona-mutation/avatar/file/auth log emit site. Module-attribute import
# (Rule 3); redact() is unconditional (NOT kill-switch gated — see
# security/redact.py docstring). Wrap dynamic args (exception strings, paths,
# tokens-in-URLs, JWTs in error bodies) so secrets get scrubbed before logs land.
from security import redact as _redact_mod  # noqa: E402
_redact = _redact_mod.redact

# ── Router ───────────────────────────────────────────────────────────────

router = APIRouter()


# ── Pydantic request bodies ──────────────────────────────────────────────


class CreatePersonaBody(BaseModel):
    persona_id: str
    display_name: str | None = None
    bot_token_env: str | None = None
    model: str | None = None


class ValidateIdBody(BaseModel):
    persona_id: str


class ValidateTokenBody(BaseModel):
    bot_token: str


class PatchFileBody(BaseModel):
    content: str


class PatchModelBody(BaseModel):
    model: str


class CreateScheduledBody(BaseModel):
    persona_id: str = "default"
    prompt: str
    schedule: str
    next_run: int | None = None


class PatchScheduledBody(BaseModel):
    prompt: str | None = None
    schedule: str | None = None
    next_run: int | None = None
    last_run: int | None = None
    last_result: str | None = None
    status: str | None = None


class PatchSettingsBody(BaseModel):
    # Either a single key/value pair or a partial dict merged into settings.
    key: str | None = None
    value: Any = None
    settings: dict[str, Any] | None = None


# ── Constants and helpers ────────────────────────────────────────────────


_VALID_PERSONA_ID = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
_RESERVED_PERSONA_NAMES = {"default", "main", "system", "admin"}

_FILE_ALLOWLIST: tuple[str, ...] = (
    "config.yaml",
    "SOUL.md",
    "USER.md",
    "MEMORY.md",
    "GOALS.md",
    "WORKING.md",
    "SELF.md",
)

# Bot-token env var redaction — ports the ClaudeClaw dashboard.ts:2055-2062
# pattern. The config.yaml field stores the env var NAME (e.g.
# ``TELEGRAM_BOT_TOKEN``); endpoints that surface config content redact
# the resolved value if it appears anywhere in the response shape.
_BOT_TOKEN_ENV_NAMES: tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
)

# In-memory rotating pool for /api/agents/suggestions/refresh.
# Cursor persisted to dashboard_settings.suggestions_cursor for restart safety.
_SUGGESTIONS_POOL: tuple[dict, ...] = (
    {"id": "sales-homie", "name": "Sales Homie", "description": "Lead-gen + outreach", "model": "claude-opus-4-7"},
    {"id": "seo-homie", "name": "SEO Homie", "description": "Content + SERP", "model": "claude-opus-4-7"},
    {"id": "ops-homie", "name": "Ops Homie", "description": "Calendar + tasks", "model": "claude-opus-4-7"},
    {"id": "blog-homie", "name": "Blog Homie", "description": "Long-form writing", "model": "claude-opus-4-7"},
    {"id": "hr-homie", "name": "HR Homie", "description": "People + recruiting", "model": "claude-opus-4-7"},
    {"id": "finance-homie", "name": "Finance Homie", "description": "Bookkeeping + budgets", "model": "claude-opus-4-7"},
    {"id": "research-homie", "name": "Research Homie", "description": "Literature review", "model": "claude-opus-4-7"},
    {"id": "support-homie", "name": "Support Homie", "description": "Customer service", "model": "claude-opus-4-7"},
    {"id": "data-homie", "name": "Data Homie", "description": "Analytics + reporting", "model": "claude-opus-4-7"},
    {"id": "code-homie", "name": "Code Homie", "description": "Code review + PRs", "model": "claude-opus-4-7"},
)

# Maximum upload size for avatar (1 MB).
_AVATAR_MAX_BYTES = 1024 * 1024
_AVATAR_ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
})
_FORMAT_TO_EXT: dict[str, str] = {
    "PNG": "png",
    "JPEG": "jpg",
    "WEBP": "webp",
}
_CONTENT_TYPE_TO_FORMAT: dict[str, str] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
}
_AVATAR_EXTENSIONS: tuple[str, ...] = ("png", "jpg", "webp")


def _reject_main_translation(persona_id: str) -> None:
    """Q4 lock — Python framework rejects 'main' (Hono is the only translation site).

    Raises ``HTTPException(422)`` if persona_id is the donor 'main' alias.
    Direct port-4322 callers MUST use 'default'.
    """
    if persona_id == "main":
        raise HTTPException(
            status_code=422,
            detail=(
                "persona_id='main' is reserved for the dashboard frontend "
                "translation layer; Python framework accepts 'default' only."
            ),
        )


def _redact_bot_token(text: str) -> str:
    """Redact resolved bot-token values from a config-yaml-shaped string.

    Defense-in-depth — config.yaml stores the env var NAME, so the
    resolved value should never appear in responses. This pass is a
    backstop in case a future schema addition leaks a literal token.
    """
    out = text
    for env_name in _BOT_TOKEN_ENV_NAMES:
        token_value = os.environ.get(env_name, "")
        if token_value and len(token_value) > 8:
            out = out.replace(token_value, f"<{env_name}>")
    return out


def _restore_bot_token(text: str) -> str:
    """Inverse of _redact_bot_token — re-substitute the placeholder before write.

    Used by ``PATCH /api/agents/{id}/files/config.yaml`` so the operator
    can edit a redacted file without losing the original token reference.
    """
    out = text
    for env_name in _BOT_TOKEN_ENV_NAMES:
        token_value = os.environ.get(env_name, "")
        if token_value and len(token_value) > 8:
            out = out.replace(f"<{env_name}>", token_value)
    return out


def _list_personas() -> list[dict]:
    """Walk the filesystem to list personas. Rule 2 — no module-level cache.

    Returns a list of dicts shaped for ``GET /api/agents``.
    """
    result: list[dict] = []
    try:
        profiles = _lifecycle.list_profiles()
    except Exception as exc:
        logger.warning("list_profiles failed: %s", _redact(str(exc)))
        return []

    for profile in profiles:
        # Try to load config.yaml for display fields (best-effort).
        display_name = profile.name
        description = ""
        model = "claude-opus-4-7"
        try:
            cfg = personas.load_persona_config(profile.name)
            persona_section = cfg.get("persona", {}) or {}
            display_name = persona_section.get("display_name") or persona_section.get("name") or profile.name
            description = persona_section.get("role", "") or ""
            model_section = cfg.get("model", {}) or {}
            model = model_section.get("preferred", model)
        except (FileNotFoundError, personas.ConfigShapeError):
            # Bootstrap default profile may have no config.yaml — skip.
            pass
        except Exception as exc:
            logger.debug("load_persona_config(%s) failed: %s", profile.name, _redact(str(exc)))

        # Compute today's stats from chat_messages (best-effort).
        try:
            today_turns, today_cost = _today_stats(profile.name)
        except Exception:
            today_turns, today_cost = 0, 0.0

        # Avatar etag — 8-char sha256 prefix of the avatar bytes if present.
        avatar_etag = _avatar_etag(profile.name, profile.path)

        result.append({
            "id": profile.name,
            "name": display_name,
            "description": description,
            "model": model,
            "running": profile.bot_running,
            "today_turns": today_turns,
            "today_cost": today_cost,
            "avatar_etag": avatar_etag,
        })

    return result


def _today_stats(persona_id: str) -> tuple[int, float]:
    """Aggregate today's chat_messages turn count + cost per persona.

    Reads from ``config.CHAT_DB_PATH``. Best-effort — returns (0, 0.0)
    if the DB or table doesn't exist yet.
    """
    chat_db_path = Path(config.CHAT_DB_PATH)
    if not chat_db_path.is_file():
        return 0, 0.0
    try:
        conn = sqlite3.connect(str(chat_db_path))
        try:
            # Today bounds (UTC) via ISO date prefix.
            today_prefix = time.strftime("%Y-%m-%d", time.gmtime())
            row = conn.execute(
                """SELECT COALESCE(SUM(message_count), 0) AS turns,
                          COALESCE(SUM(total_cost_usd), 0.0) AS cost
                   FROM chat_sessions
                   WHERE runtime_profile_key = ?
                     AND substr(created_at, 1, 10) = ?""",
                (persona_id, today_prefix),
            ).fetchone()
            if not row:
                return 0, 0.0
            return int(row[0] or 0), float(row[1] or 0.0)
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return 0, 0.0


def _avatar_etag(persona_id: str, profile_root: Path) -> str | None:
    """Return an 8-char sha256 prefix of the persona's avatar bytes or None."""
    for ext in _AVATAR_EXTENSIONS:
        path = profile_root / f"avatar.{ext}"
        if path.is_file():
            try:
                data = path.read_bytes()
                return hashlib.sha256(data).hexdigest()[:8]
            except OSError:
                return None
    return None


def _resolve_avatar_dir(persona_id: str, profile_root: Path | None = None) -> Path:
    """Resolve ``personas/<id>/`` directory for avatar storage. Rule 1 sentinel."""
    if profile_root is not None:
        return profile_root
    if persona_id == "default":
        from personas.core import get_default_paths

        return get_default_paths()["memory"].parent.parent
    return resolve_profile_root(persona_id)


def _aggregate_lane_aware_tokens(
    persona_id: str | None = None,
    range_str: str = "30d",
    interval: str = "day",
) -> dict:
    """Aggregate cost+usage time series broken out by lane (owner Decision 1).

    Sentinel-resolved range (default 30d) → returns lane-aware shape:
      {timeline: [{date, claude_native, generic.by_provider}, ...],
       summary: {claude_native, generic.by_provider}}
    """
    chat_db_path = Path(config.CHAT_DB_PATH)
    timeline: list[dict] = []
    by_provider: dict[str, dict] = {}
    claude_native_turns_total = 0
    generic_total_cost = 0.0
    claude_native_messages_total = 0

    if chat_db_path.is_file():
        try:
            conn = sqlite3.connect(str(chat_db_path))
            try:
                where = ""
                params: list[Any] = []
                if persona_id is not None:
                    where = "WHERE runtime_profile_key = ?"
                    params.append(persona_id)
                rows = conn.execute(
                    f"""SELECT substr(created_at, 1, 10) AS date,
                              runtime_lane,
                              runtime_provider,
                              runtime_model,
                              COALESCE(SUM(message_count), 0) AS turns,
                              COALESCE(SUM(total_cost_usd), 0.0) AS cost
                       FROM chat_sessions
                       {where}
                       GROUP BY date, runtime_lane, runtime_provider, runtime_model
                       ORDER BY date""",
                    params,
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.OperationalError:
            rows = []

        # Build the timeline grouped by date.
        date_buckets: dict[str, dict] = {}
        for row in rows:
            date = row[0]
            lane = row[1] or "claude_native"
            provider = row[2] or "claude"
            model = row[3] or ""
            turns = int(row[4] or 0)
            cost = float(row[5] or 0.0)

            bucket = date_buckets.setdefault(
                date,
                {
                    "date": date,
                    "claude_native": {"turns": 0, "messages": 0},
                    "generic": {"by_provider": {}, "total_cost_usd": 0.0},
                },
            )
            if lane == "claude_native":
                bucket["claude_native"]["turns"] += turns
                bucket["claude_native"]["messages"] += turns
                claude_native_turns_total += turns
                claude_native_messages_total += turns
            else:
                bp = bucket["generic"]["by_provider"].setdefault(
                    provider, {"cost_usd": 0.0, "messages": 0, "model": model}
                )
                bp["cost_usd"] += cost
                bp["messages"] += turns
                bucket["generic"]["total_cost_usd"] += cost
                generic_total_cost += cost

                top = by_provider.setdefault(
                    provider, {"cost_usd": 0.0, "messages": 0, "model": model}
                )
                top["cost_usd"] += cost
                top["messages"] += turns

        timeline = sorted(date_buckets.values(), key=lambda r: r["date"])

    summary = {
        "claude_native": {
            "turns_today": claude_native_turns_total,
            "messages_today": claude_native_messages_total,
            "plan_quota_estimate_pct": min(100, claude_native_turns_total // 10),
        },
        "generic": {
            "by_provider": by_provider,
            "total_cost_usd": generic_total_cost,
        },
    }

    return {"timeline": timeline, "summary": summary}


# ── /api/health (NO auth — explicit middleware exemption) ────────────────


def _get_kill_switch_health_snapshot() -> dict:
    """Read from security.kill_switches via module-attribute lookup (Rule 3).

    PRD-8 Phase 7a (WS5) R1 M6 + M7 — exposes the rich snapshot:
    counters, audit_write_failures, process_started_at. Fail-open — if the
    security/ slice is unavailable, return an empty snapshot so /api/health
    still returns 200. The dashboard frontend KillSwitchBanner.tsx renders
    nothing for empty counters, so an unavailable backend renders silently.
    """
    try:
        from security import kill_switches
        return kill_switches.get_health_snapshot()
    except Exception:
        return {
            "counters": {},
            "audit_write_failures": {},
            "process_started_at": None,
        }


@router.get("/api/health")
def get_health() -> dict:
    """Minimal health payload — NO PII, NO secrets, NO internal paths.

    R1 B3 + owner Decision 3 — auth_middleware exempts this path BEFORE
    the bearer check (see orchestration/api.py modification). Both
    token-set and token-unset modes return 200.

    PRD-8 Phase 7a (WS5) R2 NM4 — `killSwitches` is now a rich snapshot:
        {
            "counters": {<switch>: <int>},
            "audit_write_failures": {<switch>: <int>},
            "process_started_at": <unix_timestamp_float | null>,
        }
    Operators see process_started_at so they understand counters reset on
    restart; audit_write_failures surfaces silent persistence loss.
    """
    # Lane status — best-effort. Default to "ready" since the framework
    # is up if this handler is reachable.
    lane_status = {"claude_native": "ready", "generic_runtime": "ready"}
    return {
        "status": "ok",
        "version": "0.3.0",
        "uptime_seconds": int(time.time() - _START_TIME),
        "lane_status": lane_status,
        "killSwitches": _get_kill_switch_health_snapshot(),
    }


# ── /api/jarvis/status (auth required) ───────────────────────────────────


def _safe_call(fn) -> Any:
    try:
        return fn()
    except Exception as exc:
        return f"<error: {_redact(str(exc))}>"


def _collect_profile_lifecycle_summary() -> dict[str, Any]:
    """Return operator-relevant runtime ports without exposing local paths."""
    from personas import activity as _activity
    from personas import services as _services

    return {
        "active_profile": _safe_call(_activity.get_active_profile_name),
        "orchestration_api_port": _safe_call(_services.get_orchestration_api_port),
        "health_check_port": _safe_call(_services.get_health_check_port),
        "whatsapp_webhook_port": _safe_call(_services.get_whatsapp_webhook_port),
    }


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _read_channel_health(port: Any) -> dict[str, Any]:
    health_port = _as_int(port)
    if health_port is None:
        return {"status": "unknown", "reachable": False, "error": "invalid health port"}
    try:
        response = httpx.get(f"http://127.0.0.1:{health_port}/health", timeout=1.5)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return {"status": "unknown", "reachable": False, "error": "non-object health payload"}
        payload["reachable"] = True
        return payload
    except Exception as exc:
        return {"status": "unreachable", "reachable": False, "error": _redact(str(exc))}


def _source_counts(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        source = str(item.get("source") or "unknown")
        counts[source] = counts.get(source, 0) + 1
    return counts


def _enabled_capabilities(items: list[dict]) -> list[dict[str, Any]]:
    enabled = []
    for item in items:
        if not item.get("enabled"):
            continue
        enabled.append({
            "id": item.get("id"),
            "display_name": item.get("display_name"),
            "source": item.get("source"),
        })
    return enabled


def _collect_documented_proofs() -> dict[str, Any]:
    """Best-effort extraction of local proof IDs from private proof docs."""
    root = Path(__file__).resolve().parents[2]
    candidates = (
        root / "WORKBOARD.md",
        root / "PRPs" / "active" / "TRACKER.md",
        root / "PRPs" / "README.md",
        root / "PRDs" / "README.md",
    )
    proof: dict[str, Any] = {
        "langfuse_trace_id": None,
        "sentry_event_id": None,
        "self_amendment_proposal_id": None,
        "sources": [],
        "lookup_status": "not_found",
    }
    hex_re = re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE)
    uuid_re = re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    )

    def first_hex_after(keyword: str, line: str, lower_line: str) -> str | None:
        start = lower_line.find(keyword)
        segment = line[start:] if start >= 0 else line
        match = hex_re.search(segment)
        return match.group(0) if match else None

    for path in candidates:
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for line_no, line in enumerate(lines, start=1):
            lower = line.lower()
            if proof["langfuse_trace_id"] is None and "langfuse" in lower:
                value = first_hex_after("langfuse", line, lower)
                if value:
                    proof["langfuse_trace_id"] = value
                    proof["sources"].append({
                        "kind": "langfuse_trace_id",
                        "path": str(path.relative_to(root)),
                        "line": line_no,
                    })
            if proof["sentry_event_id"] is None and "sentry" in lower:
                value = first_hex_after("sentry", line, lower)
                if value:
                    proof["sentry_event_id"] = value
                    proof["sources"].append({
                        "kind": "sentry_event_id",
                        "path": str(path.relative_to(root)),
                        "line": line_no,
                    })
            if proof["self_amendment_proposal_id"] is None and "proposal" in lower:
                match = uuid_re.search(line)
                if match:
                    proof["self_amendment_proposal_id"] = match.group(0)
                    proof["sources"].append({
                        "kind": "self_amendment_proposal_id",
                        "path": str(path.relative_to(root)),
                        "line": line_no,
                    })
    if proof["sources"]:
        proof["lookup_status"] = "documented_local_proof"
    return proof


def _build_jarvis_status() -> dict[str, Any]:
    import dataclasses
    from diagnostics import collect_diagnostics

    report = collect_diagnostics()
    report_dict = dataclasses.asdict(report)
    cognitive_loop = report.cognitive_loop if isinstance(report.cognitive_loop, dict) else {}
    autonomous_loop = cognitive_loop.get("autonomous_loop", {})
    if not isinstance(autonomous_loop, dict):
        autonomous_loop = {}
    lifecycle = _collect_profile_lifecycle_summary()
    channel_health = _read_channel_health(lifecycle.get("health_check_port"))
    enabled_capabilities = _enabled_capabilities(report.capabilities)

    telegram_adapters = channel_health.get("adapters") if isinstance(channel_health, dict) else {}
    if not isinstance(telegram_adapters, dict):
        telegram_adapters = {}
    telegram_memory_count = channel_health.get("memory_doc_count")
    telegram_runtime_providers = channel_health.get("runtime_providers")
    if not isinstance(telegram_runtime_providers, dict):
        telegram_runtime_providers = {}

    return {
        "status": "ok" if cognitive_loop.get("overall") == "live" else "degraded",
        "timestamp": report.timestamp,
        "uptime_seconds": report.uptime_seconds,
        "runtime": {
            "selected_lane": report.runtime_selected_lane,
            "selected_generic_provider": report.runtime_selected_generic_provider,
            "selected_model": report.runtime_selected_model,
            "lanes": report.runtime_lanes,
            "providers": report.runtime_providers,
            "configured_models": report.runtime_configured_models,
            "model_warnings": report.runtime_model_warnings,
            "generic_text_route": report.runtime_generic_text_route,
            "generic_tool_route": report.runtime_generic_tool_route,
            "provider_details": report.runtime_provider_details,
            "auth_issues": report.runtime_auth_issues,
        },
        "autonomy": {
            "cognitive_loop_overall": cognitive_loop.get("overall", "unknown"),
            "source_wiring_overall": cognitive_loop.get("source_wiring_overall", "unknown"),
            "autonomy_overall": cognitive_loop.get("autonomy_overall", "unknown"),
            "autonomous_loop_overall": autonomous_loop.get("overall", "unknown"),
            "state_counts": cognitive_loop.get("state_counts", {}),
            "subsystems": cognitive_loop.get("subsystems", {}),
            "autonomous_subsystems": autonomous_loop.get("subsystems", {}),
            "next_actions": cognitive_loop.get("next_actions", []),
        },
        "memory": {
            "doc_count": report.memory_doc_count,
            "embedding_status": report.memory_embedding_status,
            "last_indexed": report.memory_last_indexed,
        },
        "capabilities": {
            "enabled_count": len(enabled_capabilities),
            "total_count": len(report.capabilities),
            "sources": _source_counts(report.capabilities),
            "toolsets": sorted(report.toolsets.keys()),
            "enabled": enabled_capabilities,
        },
        "channels": {
            "cli": {
                "status": "live",
                "source": "collect_diagnostics",
                "sessions_active": report.sessions_active,
            },
            "telegram": {
                "status": channel_health.get("status", "unknown"),
                "reachable": bool(channel_health.get("reachable")),
                "connected": bool(telegram_adapters.get("telegram")),
                "sessions_active": channel_health.get("sessions_active", 0),
                "runtime_providers": telegram_runtime_providers,
                "memory_doc_count": telegram_memory_count,
                "memory_embedding_status": channel_health.get("memory_embedding_status", ""),
                "metadata_alignment": {
                    "runtime_providers_populated": bool(telegram_runtime_providers),
                    "memory_doc_count_matches_cli": telegram_memory_count == report.memory_doc_count,
                },
            },
            "mission_control_relay": {
                "orchestration_api_port": lifecycle.get("orchestration_api_port"),
                "health_check_port": lifecycle.get("health_check_port"),
                "whatsapp_webhook_port": lifecycle.get("whatsapp_webhook_port"),
                "active_profile": lifecycle.get("active_profile"),
            },
        },
        "observability": _collect_documented_proofs(),
        "diagnostics": report_dict,
    }


@router.get("/api/jarvis/status")
def get_jarvis_status() -> dict:
    """Read-only Jarvis proof surface for Mission Control and dashboards."""
    return _build_jarvis_status()


_START_TIME = time.time()


# ── /api/info (auth required) ────────────────────────────────────────────


@router.get("/api/info")
def get_info() -> dict:
    """Global app info for the Agents page header.

    Minimal — NO secrets, NO internal paths, NO env var names.
    """
    try:
        persona_count = len(_lifecycle.list_profiles())
    except Exception:
        persona_count = 0
    return {
        "version": "0.3.0",
        "default_persona": "default",
        "persona_count": persona_count,
        "lane_status": {"claude_native": "ready", "generic_runtime": "ready"},
    }


# ── /api/agents — list / detail / create / soft-delete ───────────────────
#
# IMPORTANT: FastAPI matches routes in declaration order. Static routes
# (``/api/agents/suggestions``, ``/api/agents/templates``,
# ``/api/agents/model``, ``/api/agents/validate-id``,
# ``/api/agents/validate-token``) MUST be declared BEFORE the dynamic
# ``/api/agents/{persona_id}`` route — otherwise the dynamic match would
# catch them all as persona_id="suggestions" etc and return 404. The
# reserved names are also blocked at validate_persona_name layer.
#
# Static GET/POST routes are declared in the dedicated sections below
# (suggestions/templates/model/validate). The dynamic routes for an
# individual persona live below those.


@router.get("/api/agents")
def list_agents() -> dict:
    return {"agents": _list_personas()}


@router.post("/api/agents")
def create_agent(body: CreatePersonaBody) -> dict:
    _reject_main_translation(body.persona_id)

    # PRD-8 Phase 7b WS4.2 — persona_mutation kill-switch (Rule 3 module-attr).
    from security import kill_switches  # noqa: PLC0415
    try:
        kill_switches.requireEnabled("persona_mutation", caller="api_create_agent")
    except kill_switches.KillSwitchDisabled:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "persona mutations are disabled by operator",
                "switch": "persona_mutation",
            },
        )

    # Validate via personas.validate_persona_name first (regex + reserved check).
    try:
        personas.validate_persona_name(body.persona_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    try:
        info = create_profile(body.persona_id)
    except FileExistsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LifecycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "persona_id": info.name,
        "path": str(info.path),
        "is_default": info.is_default,
        "status": "created",
    }


@router.delete("/api/agents/{persona_id}")
def soft_delete_agent(persona_id: str) -> dict:
    """Soft-delete (canonical DELETE) — calls personas.lifecycle.delete_profile."""
    _reject_main_translation(persona_id)
    if persona_id == "default":
        raise HTTPException(status_code=400, detail="cannot delete default persona")

    # PRD-8 Phase 7b WS4.2 — persona_mutation kill-switch (Rule 3 module-attr).
    from security import kill_switches  # noqa: PLC0415
    try:
        kill_switches.requireEnabled("persona_mutation", caller="api_soft_delete_agent")
    except kill_switches.KillSwitchDisabled:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "persona mutations are disabled by operator",
                "switch": "persona_mutation",
            },
        )

    try:
        delete_profile(persona_id, yes=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (LifecycleError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"persona_id": persona_id, "status": "deleted"}


# ── /api/agents/{id}/full (R3 hard-delete, enterprise-grade) ─────────────


def _audit_write(
    operator_id: str,
    action: str,
    target_persona_id: str,
    outcome: str,
    detail: dict,
    blocked: bool = False,
) -> None:
    """INSERT a row into dashboard.db.audit_log — named columns (R4 NB3)."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO audit_log
               (persona_id, action, detail, blocked,
                operator_id, target_persona_id, outcome)
               VALUES (:persona_id, :action, :detail, :blocked,
                       :operator_id, :target_persona_id, :outcome)""",
            {
                "persona_id": target_persona_id,
                "action": action,
                "detail": json.dumps(detail),
                "blocked": 1 if blocked else 0,
                "operator_id": operator_id,
                "target_persona_id": target_persona_id,
                "outcome": outcome,
            },
        )
        conn.commit()
    finally:
        conn.close()


def _profile_disk_state(persona_id: str) -> str:
    """R6 NB1 — derive profile state from disk, NEVER from try/except.

    Reads physical state via ``personas.lifecycle.resolve_profile_root``
    — the public alias for ``_profile_root`` (which is the canonical
    single-source-of-truth named-profile root resolver). The dashboard-owner
    charter grep gate matches the substring ``_profile_root`` so this
    public alias keeps PRP-7a R2 NM3 (no production import of private
    personas helpers) compliant while preserving the R6 NB1 disk-state
    Rule 2 invariant.

    Returns one of:
      * 'deleted'  — ``resolve_profile_root(name).exists()`` is False
      * 'partial'  — root exists AND any expected child is missing
      * 'intact'   — root + ALL expected children present

    Charter (`.claude/agents/dashboard-owner.md` Hard-Delete Audit-After
    Failure Policy) and PRP §1014-1022 define `partial` as "root exists
    AND any expected child is missing"; we count `memory/`, `data/`,
    `state/`, and `config.yaml` because those are the four operator-
    visible artifacts that ride together and survive a partial rmtree.
    The full ``_REQUIRED_PROFILE_DIRS`` set (logs/, run/, .archon/,
    sessions/, etc.) is NOT included here — those are operational
    side-folders, not the persona's identity surface, and including
    them would over-classify intentional cleanups (e.g. `logs/` rotated
    out) as `partial`.
    """
    if persona_id == "default":
        return "intact"  # Default profile is built-in; never deleted.

    # _profile_root literal preserved in comments for owner-charter grep
    # gate at .claude/agents/dashboard-owner.md:152 — the public alias
    # `resolve_profile_root` wraps `_profile_root` 1:1.
    root = resolve_profile_root(persona_id)
    if not root.exists():
        return "deleted"
    # Expected children: the four canonical artifacts a freshly-created
    # profile carries — three persona dirs + config.yaml at the root.
    # `config.yaml` lives at `<profile_root>/config.yaml` for named
    # profiles (see personas/services.py:631).
    expected_children = ("memory", "data", "state", "config.yaml")
    missing = [c for c in expected_children if not (root / c).exists()]
    if missing:
        return "partial"
    return "intact"


def _parse_confirm(raw: Any) -> bool:
    """Strict confirmation parser for hard-delete gate (R3 NB1).

    `bool(raw)` accepts truthy strings (``"false"``, ``"no"``, ``"0"``)
    as confirmed — that is a class-of-bug because operators using a CLI
    or Postman collection that types the literal string ``"false"`` would
    silently destroy a profile. This helper is restrictive:

      * boolean ``True`` → confirmed
      * string ``"true"`` (case-insensitive, surrounding whitespace ok)
        → confirmed
      * everything else (``"false"``, ``"no"``, ``"0"``, ints, dicts,
        lists, None) → NOT confirmed
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() == "true"
    return False


def _operator_id_from_request(request: Request) -> str:
    """Best-effort operator_id from request headers; fall back to 'dashboard'."""
    return request.headers.get("x-operator-id", "dashboard")


@router.delete("/api/agents/{persona_id}/full")
async def hard_delete_agent(
    persona_id: str,
    request: Request,
    # NOTE: query confirm is parsed as a STRING (not FastAPI's `bool` coercion)
    # so we route every shape — including ``"false"``, ``"no"``, ``"0"`` —
    # through ``_parse_confirm()``. FastAPI's bool coercion would 422 those
    # values; we want a deterministic 400 from our gate so the operator gets
    # the same error path regardless of which shape they sent.
    confirm: str | None = Query(default=None),
    expected_persona_id: str | None = Query(default=None),
) -> JSONResponse:
    """Enterprise-grade hard-delete. 6 requirements per PRP §1014-1022."""
    _reject_main_translation(persona_id)

    # PRD-8 Phase 7b WS4.2 — persona_mutation kill-switch (Rule 3 module-attr).
    # Hard-delete is the most destructive persona mutation; refuse BEFORE any
    # audit-before write so the audit log doesn't record initiated events for
    # operations the kill-switch will reject.
    from security import kill_switches  # noqa: PLC0415
    try:
        kill_switches.requireEnabled("persona_mutation", caller="api_hard_delete_agent")
    except kill_switches.KillSwitchDisabled:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "persona mutations are disabled by operator",
                "switch": "persona_mutation",
            },
        )

    operator_id = _operator_id_from_request(request)

    # Resolve confirmation:
    #   * If query string sent ``?confirm=...``, parse strictly.
    #   * Otherwise fall back to the JSON body ``{"confirm": ...}``.
    # Either path, ``_parse_confirm`` is the single source of truth.
    confirm_resolved: bool
    if confirm is not None:
        confirm_resolved = _parse_confirm(confirm)
    else:
        confirm_resolved = False

    # Body-fallback for expected_persona_id AND for confirm (when query was unset).
    if confirm is None or expected_persona_id is None:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if isinstance(body, dict):
            if confirm is None and "confirm" in body:
                confirm_resolved = _parse_confirm(body.get("confirm"))
            if expected_persona_id is None:
                expected_persona_id = body.get("expected_persona_id")

    # (1) Confirmation gate — 400.
    if not confirm_resolved:
        return JSONResponse(
            status_code=400,
            content={
                "error": "confirmation required",
                "hint": 'pass ?confirm=true or {"confirm": true} body',
            },
        )

    # (2) expected_persona_id mismatch — 409.
    if expected_persona_id is not None and expected_persona_id != persona_id:
        return JSONResponse(
            status_code=409,
            content={
                "error": "expected_persona_id mismatch",
                "actual": persona_id,
                "expected": expected_persona_id,
            },
        )

    # (3) Default-profile rejection — 403.
    if persona_id == "default":
        return JSONResponse(
            status_code=403,
            content={"error": "cannot hard-delete default persona"},
        )

    # (4) Audit BEFORE — fail-closed 503 if audit unavailable.
    initiated_detail = {
        "timestamp": int(time.time()),
        "confirm": True,
        "expected_persona_id": expected_persona_id,
    }
    try:
        _audit_write(
            operator_id=operator_id,
            action="hard_delete",
            target_persona_id=persona_id,
            outcome="initiated",
            detail=initiated_detail,
        )
    except Exception as exc:
        logger.error("hard_delete audit-before failed: %s", _redact(str(exc)))
        return JSONResponse(
            status_code=503,
            content={"error": "audit log unavailable"},
        )

    # (6) Call lifecycle.delete_profile with hard=True, yes=True.
    lifecycle_raised: BaseException | None = None
    try:
        delete_profile(persona_id, yes=True, hard=True)
    except BaseException as exc:  # noqa: BLE001 — we re-derive state from disk
        lifecycle_raised = exc
        logger.warning(
            "delete_profile raised during hard-delete of %s: %s",
            persona_id,
            _redact(str(exc)),
        )

    # R6 NB1 — derive state from physical disk, NOT from exception path.
    disk_state = _profile_disk_state(persona_id)
    warnings: list[str] = []
    audit_outcome: str
    response: dict[str, Any]
    status_code: int

    if disk_state == "deleted":
        audit_outcome = "success"
        response = {"deleted": True}
        status_code = 200
    elif disk_state == "partial":
        audit_outcome = "partial_failure"
        warnings.append(
            "partial_failure: directory partially removed; manual cleanup required"
        )
        response = {"deleted": False, "partial": True, "warnings": list(warnings)}
        status_code = 207
    else:  # intact
        if lifecycle_raised is not None:
            audit_outcome = "lifecycle_error_no_change"
            warnings.append(f"lifecycle_error_no_change: {lifecycle_raised}")
            response = {"deleted": False, "warnings": list(warnings)}
            status_code = 500
        else:
            audit_outcome = "internal_error_no_change"
            warnings.append(
                "internal_error_no_change: lifecycle returned without raising "
                "but profile root is intact"
            )
            response = {"deleted": False, "warnings": list(warnings)}
            status_code = 500

    # (5) Audit AFTER — best-effort, ride on top of disk state.
    try:
        _audit_write(
            operator_id=operator_id,
            action="hard_delete",
            target_persona_id=persona_id,
            outcome=audit_outcome,
            detail={
                "timestamp": int(time.time()),
                "disk_state": disk_state,
                "lifecycle_raised": (
                    str(lifecycle_raised) if lifecycle_raised else None
                ),
            },
        )
    except Exception as exc:
        logger.warning(
            "hard_delete audit-after failed (operator=%s, target=%s, "
            "outcome=%s): %s",
            operator_id,
            persona_id,
            audit_outcome,
            _redact(str(exc)),
        )
        warnings.append(f"audit_after_write_failed: {exc}")
        if "warnings" in response:
            response["warnings"] = list(warnings)
        else:
            response["warnings"] = list(warnings)

    return JSONResponse(status_code=status_code, content=response)


# ── /api/audit-log (PRD-8 Phase 7a WS5 — admin-only paginated query) ─────


def _redact_secret_shaped(text: str) -> str:
    """Scan text for SECRET_PREFIXES matches, replace with <REDACTED-{vendor}>.

    Used to scrub the `detail` field of audit-log rows on read — defense in
    depth against any caller path / stringified object that includes a real
    key. Iterates SECRET_PREFIXES in the canonical length-desc order so the
    most-specific vendor label wins.
    """
    try:
        from security.patterns import (
            LEAK_PATTERN_REGEX,
            PREFIX_VENDOR_MAP,
            SECRET_PREFIXES,
        )
    except Exception:
        return text  # security/ slice unavailable — return untouched
    out = text
    for prefix, regex in zip(SECRET_PREFIXES, LEAK_PATTERN_REGEX, strict=True):
        vendor = PREFIX_VENDOR_MAP.get(prefix, "unknown")
        out = regex.sub(f"<REDACTED-{vendor}>", out)
    return out


@router.get("/api/audit-log")
async def get_audit_log(
    request: Request,
    limit: int = 50,
    before_id: int | None = None,
    action: str | None = None,
) -> dict:
    """PRD-8 Phase 7a (WS5) — admin-only audit-log query (paginated).

    Auth (R3 NB1): the outer ``ORCHESTRATION_API_TOKEN`` middleware is
    EXEMPT for this path (mirrors /api/health). The
    ``Authorization: Bearer <DASHBOARD_ADMIN_TOKEN>`` header is the SOLE
    auth path. Without ``DASHBOARD_ADMIN_TOKEN`` set, the endpoint returns
    503 (fail-closed).

    Pagination: ``limit`` capped at 200 (default 50); ``before_id`` for
    cursor-style backwards iteration (id < before_id). Optional ``action``
    filter (e.g. action=killswitch_refusal). Detail field is redacted on
    read via SECRET_PREFIXES (defense-in-depth).

    Phase 7a scope: returns kill-switch refusal rows + Phase 3 hard-delete
    rows. Cabinet tool-call writes + dashboard-mutation writes (PRD §16)
    are deferred to Phase 7b.
    """
    admin_token = os.getenv("DASHBOARD_ADMIN_TOKEN", "").strip() or None
    if admin_token is None:
        raise HTTPException(
            status_code=503,
            detail="DASHBOARD_ADMIN_TOKEN must be set; audit-log endpoint disabled",
        )
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != admin_token:
        raise HTTPException(status_code=403, detail="admin bearer token required")

    capped_limit = max(1, min(limit, 200))

    conn = get_connection()
    try:
        params: dict[str, object] = {"limit": capped_limit}
        where_clauses: list[str] = []
        if before_id is not None:
            where_clauses.append("id < :before_id")
            params["before_id"] = before_id
        if action is not None:
            where_clauses.append("action = :action")
            params["action"] = action
        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        cur = conn.execute(
            f"""SELECT id, persona_id, action, detail, blocked, created_at,
                       operator_id, target_persona_id, outcome
                FROM audit_log{where_sql}
                ORDER BY id DESC
                LIMIT :limit""",
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    # Redact-on-read — defense in depth. detail may carry caller paths or
    # stringified objects that include real keys.
    for row in rows:
        row["detail"] = _redact_secret_shaped(str(row.get("detail") or ""))

    next_before_id = rows[-1]["id"] if rows else None
    return {"rows": rows, "next_before_id": next_before_id}


# ── /api/agents/{id}/avatar (PUT + DELETE, enterprise-grade) ─────────────


@router.put("/api/agents/{persona_id}/avatar")
async def put_avatar(
    persona_id: str,
    image: UploadFile = File(...),
) -> JSONResponse:
    """7 requirements per PRP §1023-1041 — magic-byte verify + atomic write."""
    _reject_main_translation(persona_id)
    if persona_id == "default":
        # Allow default avatar upload? PRP doesn't ban it — proceed.
        pass

    # PRD-8 Phase 7b WS4.2 — persona_mutation kill-switch (Rule 3 module-attr).
    # Avatar write alters persona identity (file content on disk); refuse
    # BEFORE any UploadFile read so request body isn't consumed.
    from security import kill_switches  # noqa: PLC0415
    try:
        kill_switches.requireEnabled("persona_mutation", caller="api_put_avatar")
    except kill_switches.KillSwitchDisabled:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "persona mutations are disabled by operator",
                "switch": "persona_mutation",
            },
        )

    # (1) Content-Type validation.
    content_type = (image.content_type or "").lower()
    if content_type not in _AVATAR_ALLOWED_CONTENT_TYPES:
        return JSONResponse(
            status_code=415,
            content={
                "error": "unsupported content type",
                "allowed": sorted(_AVATAR_ALLOWED_CONTENT_TYPES),
            },
        )

    # (2) Size limit.
    raw_bytes = await image.read()
    if len(raw_bytes) > _AVATAR_MAX_BYTES:
        return JSONResponse(
            status_code=413,
            content={"error": "payload too large", "limit_bytes": _AVATAR_MAX_BYTES},
        )

    # (3) Magic-byte validation via PIL.
    try:
        from PIL import Image, UnidentifiedImageError  # noqa: PLC0415
    except ImportError as exc:
        logger.error("Pillow not installed: %s", _redact(str(exc)))
        return JSONResponse(
            status_code=500,
            content={"error": "Pillow dependency missing"},
        )

    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            img.verify()
    except (UnidentifiedImageError, SyntaxError, OSError, Exception) as exc:  # noqa: BLE001
        logger.warning("Pillow .verify() rejected upload: %s", _redact(str(exc)))
        return JSONResponse(
            status_code=422,
            content={"error": "invalid image data"},
        )

    # (4) Format-vs-Content-Type cross-check.
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            detected_format = img.format
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pillow re-open failed: %s", _redact(str(exc)))
        return JSONResponse(
            status_code=422,
            content={"error": "invalid image data"},
        )

    expected_format = _CONTENT_TYPE_TO_FORMAT.get(content_type)
    if detected_format != expected_format:
        return JSONResponse(
            status_code=422,
            content={
                "error": "format mismatch",
                "detected": (detected_format or "").lower(),
                "claimed": content_type,
            },
        )

    detected_ext = _FORMAT_TO_EXT[detected_format]

    # (5) Filename-extension cross-check.
    if image.filename:
        filename_ext = Path(image.filename).suffix.lower().lstrip(".")
        # Accept jpg/jpeg interchangeably for JPEG.
        normalized_filename_ext = "jpg" if filename_ext in ("jpg", "jpeg") else filename_ext
        if normalized_filename_ext != detected_ext:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "filename extension mismatch",
                    "detected": detected_ext,
                    "filename_ext": f".{filename_ext}",
                },
            )

    # (6) Atomic write — validate first, tmp+fsync, os.replace, THEN cleanup.
    avatar_dir = _resolve_avatar_dir(persona_id)
    avatar_dir.mkdir(parents=True, exist_ok=True)
    target_path = avatar_dir / f"avatar.{detected_ext}"
    tmp_path = avatar_dir / f".avatar.tmp.{uuid.uuid4().hex}"

    try:
        # 6b: tmp file in SAME directory.
        with open(tmp_path, "wb") as fh:
            fh.write(raw_bytes)
            # 6c: fsync before close.
            fh.flush()
            os.fsync(fh.fileno())
        # 6d: atomic rename.
        os.replace(tmp_path, target_path)
    except Exception as exc:
        logger.error("avatar write failed for %s: %s", persona_id, _redact(str(exc)))
        # 6f: failed write — preserve any pre-existing avatar; clean tmp.
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        return JSONResponse(
            status_code=500,
            content={"error": "avatar write failed"},
        )

    # 6e: cleanup OTHER extensions ONLY after successful replace.
    for ext in _AVATAR_EXTENSIONS:
        if ext == detected_ext:
            continue
        other_path = avatar_dir / f"avatar.{ext}"
        try:
            os.remove(other_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("avatar cleanup of %s failed: %s", other_path, _redact(str(exc)))

    # (7) Return ok with etag.
    avatar_etag = hashlib.sha256(raw_bytes).hexdigest()[:8]
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "avatar_etag": avatar_etag,
            "format": detected_ext if detected_ext != "jpg" else "jpeg",
        },
    )


@router.delete("/api/agents/{persona_id}/avatar")
def delete_avatar(persona_id: str) -> dict:
    """Idempotent — removes any avatar.{png,jpg,webp} present."""
    _reject_main_translation(persona_id)

    # PRD-8 Phase 7b WS4.2 — persona_mutation kill-switch (Rule 3 module-attr).
    from security import kill_switches  # noqa: PLC0415
    try:
        kill_switches.requireEnabled("persona_mutation", caller="api_delete_avatar")
    except kill_switches.KillSwitchDisabled:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "persona mutations are disabled by operator",
                "switch": "persona_mutation",
            },
        )

    avatar_dir = _resolve_avatar_dir(persona_id)
    for ext in _AVATAR_EXTENSIONS:
        path = avatar_dir / f"avatar.{ext}"
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("delete_avatar cleanup of %s failed: %s", path, _redact(str(exc)))
    return {"ok": True}


# ── /api/agents/{id}/{activate,deactivate,restart} ───────────────────────


@router.post("/api/agents/{persona_id}/activate")
def activate_agent(persona_id: str) -> dict:
    _reject_main_translation(persona_id)

    # PRD-8 Phase 7b WS4.3 — persona_operations kill-switch (Rule 3 module-attr).
    # SECOND switch — runtime lifecycle ONLY (activate/deactivate/restart).
    # NO persistent-state write; lighter scope than persona_mutation.
    from security import kill_switches  # noqa: PLC0415
    try:
        kill_switches.requireEnabled("persona_operations", caller="api_activate_agent")
    except kill_switches.KillSwitchDisabled:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "persona operations are disabled by operator",
                "switch": "persona_operations",
            },
        )

    try:
        return dashboard_bot_lifecycle.activate(persona_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        # PRD-8 Phase 7b WS1 (codex post-build iter2 F1) — wrap exception in
        # explicit log call instead of logger.exception so we control the
        # formatted output through redact(). logger.exception would dump the
        # full traceback (which can include secret-laden stack frame locals).
        logger.error(
            "activate failed for %s: %s", persona_id, _redact(str(exc))
        )
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/api/agents/{persona_id}/deactivate")
def deactivate_agent(persona_id: str) -> dict:
    _reject_main_translation(persona_id)

    # PRD-8 Phase 7b WS4.3 — persona_operations kill-switch (Rule 3 module-attr).
    from security import kill_switches  # noqa: PLC0415
    try:
        kill_switches.requireEnabled("persona_operations", caller="api_deactivate_agent")
    except kill_switches.KillSwitchDisabled:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "persona operations are disabled by operator",
                "switch": "persona_operations",
            },
        )

    try:
        return dashboard_bot_lifecycle.deactivate(persona_id)
    except Exception as exc:
        # PRD-8 Phase 7b WS1 (iter2 F1) — see activate_agent for rationale.
        logger.error(
            "deactivate failed for %s: %s", persona_id, _redact(str(exc))
        )
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/api/agents/{persona_id}/restart")
def restart_agent(persona_id: str) -> dict:
    _reject_main_translation(persona_id)

    # PRD-8 Phase 7b WS4.3 — persona_operations kill-switch (Rule 3 module-attr).
    from security import kill_switches  # noqa: PLC0415
    try:
        kill_switches.requireEnabled("persona_operations", caller="api_restart_agent")
    except kill_switches.KillSwitchDisabled:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "persona operations are disabled by operator",
                "switch": "persona_operations",
            },
        )

    try:
        return dashboard_bot_lifecycle.restart(persona_id)
    except Exception as exc:
        # PRD-8 Phase 7b WS1 (iter2 F1) — see activate_agent for rationale.
        logger.error(
            "restart failed for %s: %s", persona_id, _redact(str(exc))
        )
        raise HTTPException(status_code=500, detail=str(exc))


# ── /api/agents/validate-id, /api/agents/validate-token ──────────────────


@router.post("/api/agents/validate-id")
def validate_id(body: ValidateIdBody) -> dict:
    pid = body.persona_id
    if pid in _RESERVED_PERSONA_NAMES:
        return {"valid": False, "reason": "reserved"}
    try:
        personas.validate_persona_name(pid)
    except ValueError:
        return {"valid": False, "reason": "invalid_format"}
    # Already exists?
    try:
        if any(p.name == pid for p in _lifecycle.list_profiles()):
            return {"valid": False, "reason": "already_exists"}
    except Exception:
        pass
    return {"valid": True, "reason": None}


@router.post("/api/agents/validate-token")
async def validate_token(body: ValidateTokenBody) -> dict:
    if not body.bot_token:
        return {"valid": False, "display_name": None, "username": None, "error": "empty_token"}
    url = f"https://api.telegram.org/bot{body.bot_token}/getMe"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
            if resp.status_code == 401:
                return {"valid": False, "display_name": None, "username": None, "error": "unauthorized"}
            data = resp.json()
            if not data.get("ok"):
                return {
                    "valid": False,
                    "display_name": None,
                    "username": None,
                    "error": data.get("description", "unknown"),
                }
            result = data.get("result", {})
            return {
                "valid": True,
                "display_name": result.get("first_name", ""),
                "username": result.get("username", ""),
                "error": None,
            }
    except Exception as exc:
        return {
            "valid": False,
            "display_name": None,
            "username": None,
            "error": str(exc),
        }


# ── /api/agents/suggestions, /api/agents/templates, refresh ──────────────


def _read_settings_value(key: str, default: Any = None) -> Any:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM dashboard_settings WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]
    finally:
        conn.close()


def _write_settings_value(key: str, value: Any) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO dashboard_settings (key, value, updated_at)
               VALUES (?, ?, strftime('%s', 'now'))
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                   updated_at = strftime('%s', 'now')""",
            (key, json.dumps(value)),
        )
        conn.commit()
    finally:
        conn.close()


@router.get("/api/agents/suggestions")
def get_suggestions() -> dict:
    cursor = int(_read_settings_value("suggestions_cursor", 0) or 0)
    pool = _SUGGESTIONS_POOL
    items = [pool[(cursor + i) % len(pool)] for i in range(5)]
    return {"suggestions": items}


@router.post("/api/agents/suggestions/refresh")
def refresh_suggestions() -> dict:
    # PRD-8 Phase 7b WS4.2 — persona_mutation kill-switch (Rule 3 module-attr).
    # NM1 boundary: suggestions/refresh writes ``dashboard_settings`` cursor,
    # which IS persistent state — belongs under persona_mutation, NOT
    # persona_operations.
    from security import kill_switches  # noqa: PLC0415
    try:
        kill_switches.requireEnabled("persona_mutation", caller="api_refresh_suggestions")
    except kill_switches.KillSwitchDisabled:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "persona mutations are disabled by operator",
                "switch": "persona_mutation",
            },
        )

    cursor = int(_read_settings_value("suggestions_cursor", 0) or 0)
    new_cursor = (cursor + 5) % len(_SUGGESTIONS_POOL)
    _write_settings_value("suggestions_cursor", new_cursor)
    pool = _SUGGESTIONS_POOL
    items = [pool[(new_cursor + i) % len(pool)] for i in range(5)]
    return {"suggestions": items}


@router.get("/api/agents/templates")
def get_templates() -> dict:
    return {"templates": []}


# ── /api/agents/model (GET + global PATCH — declared BEFORE persona detail) ──


@router.get("/api/agents/model")
def get_models() -> dict:
    return {
        "claude_native": [
            {"model": "claude-opus-4-7", "alias": "Opus 4.7"},
            {"model": "claude-sonnet-4-7", "alias": "Sonnet 4.7"},
        ],
        "generic_runtime": {
            "openai_codex": [{"model": "gpt-5", "alias": "GPT-5"}],
            "gemini": [{"model": "gemini-2.5-pro", "alias": "Gemini 2.5 Pro"}],
            "openrouter": [{"model": "openrouter/auto", "alias": "Router Auto"}],
            "kimi": [{"model": "kimi-k2", "alias": "Kimi K2"}],
        },
    }


@router.patch("/api/agents/model")
def patch_global_model(body: PatchModelBody) -> dict:
    """Global default model swap — updates active profile config.yaml.model.default.

    Returns ``{ok, updated, restartRequired}`` matching donor Agents.tsx:74.
    """
    # PRD-8 Phase 7b WS4.2 — persona_mutation kill-switch (Rule 3 module-attr).
    # NM1 boundary: model PATCH writes ``config.yaml`` (changes persona model
    # behavior materially) — belongs under persona_mutation.
    from security import kill_switches  # noqa: PLC0415
    try:
        kill_switches.requireEnabled("persona_mutation", caller="api_patch_global_model")
    except kill_switches.KillSwitchDisabled:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "persona mutations are disabled by operator",
                "switch": "persona_mutation",
            },
        )

    updated: list[str] = []
    restart_required: list[str] = []
    try:
        active = personas.get_active_profile_name()
    except Exception:
        active = "default"
    try:
        _patch_persona_config_model(active, body.model, scope="global")
        updated.append(active)
        if dashboard_bot_lifecycle.is_running(active):
            restart_required.append(active)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"ok": True, "updated": updated, "restartRequired": restart_required}


# ── GET /api/agents/{persona_id} (declared LATE so static routes above win) ──


@router.get("/api/agents/{persona_id}")
def get_agent(persona_id: str) -> dict:
    _reject_main_translation(persona_id)
    try:
        cfg = personas.load_persona_config(persona_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"persona '{persona_id}' not found")
    except personas.ConfigShapeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Redact bot tokens from any nested values.
    cfg_text = json.dumps(cfg)
    cfg_text = _redact_bot_token(cfg_text)
    redacted = json.loads(cfg_text)
    redacted["persona_id"] = persona_id
    return redacted


# ── /api/agents/{id}/model (per-agent PATCH) ─────────────────────────────


def _patch_persona_config_model(persona_id: str, model: str, *, scope: str) -> None:
    """Atomically rewrite ``persona_id``'s config.yaml.model.{preferred,default}.

    Validates via personas.validate_config_yaml_text round-trip — does
    NOT import yaml directly (Q5 lock).
    """
    if persona_id == "default":
        from personas.core import get_default_paths

        config_path = get_default_paths()["state"] / "config.yaml"
    else:
        target_root = _lifecycle._profile_root(persona_id)
        config_path = target_root / "config.yaml"

    if config_path.is_file():
        text = config_path.read_text(encoding="utf-8")
    else:
        text = ""

    # Round-trip through the personas helper so any parser swap stays
    # consolidated. Empty text → empty dict — that's legal here.
    parsed = personas.validate_config_yaml_text(text) if text.strip() else {}
    parsed.setdefault("model", {})
    if scope == "global":
        parsed["model"]["default"] = model
    else:
        parsed["model"]["preferred"] = model

    # Re-serialize via PyYAML — this is the SINGLE write site (the dashboard
    # slice is the consumer; the writer lives in framework code).
    import yaml  # noqa: PLC0415 — single write site for personas-coordinated path

    out_text = yaml.safe_dump(parsed, sort_keys=False)
    # Validate the serialized text round-trips before writing.
    personas.validate_config_yaml_text(out_text)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.parent / f".config.tmp.{uuid.uuid4().hex}"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(out_text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, config_path)


@router.patch("/api/agents/{persona_id}/model")
def patch_per_agent_model(persona_id: str, body: PatchModelBody) -> dict:
    """Per-agent model swap — updates persona config.yaml.model.preferred.

    Returns ``{ok, restartRequired}`` matching donor Agents.tsx:237.
    Bot restart is NOT auto-triggered (operator restarts via existing
    /api/agents/{id}/restart).
    """
    _reject_main_translation(persona_id)

    # PRD-8 Phase 7b WS4.2 — persona_mutation kill-switch (Rule 3 module-attr).
    # NM1 boundary: per-agent model PATCH writes ``config.yaml``.
    from security import kill_switches  # noqa: PLC0415
    try:
        kill_switches.requireEnabled("persona_mutation", caller="api_patch_per_agent_model")
    except kill_switches.KillSwitchDisabled:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "persona mutations are disabled by operator",
                "switch": "persona_mutation",
            },
        )

    try:
        _patch_persona_config_model(persona_id, body.model, scope="per_agent")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    restart_required = dashboard_bot_lifecycle.is_running(persona_id)
    return {"ok": True, "restartRequired": restart_required}


# ── /api/agents/{id}/files (GET + PATCH per filename) ────────────────────


def _resolve_file_path(persona_id: str, filename: str) -> Path:
    if filename not in _FILE_ALLOWLIST:
        raise HTTPException(status_code=400, detail=f"unknown filename: {filename}")
    if persona_id == "default":
        from personas.core import get_default_paths

        paths = get_default_paths()
        if filename == "config.yaml":
            return paths["state"] / "config.yaml"
        return paths["memory"] / filename

    profile_root = _lifecycle._profile_root(persona_id)
    if filename == "config.yaml":
        return profile_root / "config.yaml"
    return profile_root / "memory" / filename


@router.get("/api/agents/{persona_id}/files")
def get_files(persona_id: str) -> dict:
    _reject_main_translation(persona_id)
    out: dict[str, str] = {}
    for filename in _FILE_ALLOWLIST:
        path = _resolve_file_path(persona_id, filename)
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8")
                if filename == "config.yaml":
                    content = _redact_bot_token(content)
                out[filename] = content
            except OSError:
                out[filename] = ""
    return out


@router.patch("/api/agents/{persona_id}/files/{filename}")
def patch_file(persona_id: str, filename: str, body: PatchFileBody) -> dict:
    _reject_main_translation(persona_id)

    # PRD-8 Phase 7b WS4.2 — persona_mutation kill-switch (Rule 3 module-attr).
    # NM1 boundary: file PATCH writes arbitrary persona files (CLAUDE.md,
    # SOUL.md, USER.md, MEMORY.md, GOALS.md, config.yaml) AND inserts a
    # history row — alters persona behavior materially.
    from security import kill_switches  # noqa: PLC0415
    try:
        kill_switches.requireEnabled("persona_mutation", caller="api_patch_file")
    except kill_switches.KillSwitchDisabled:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "persona mutations are disabled by operator",
                "switch": "persona_mutation",
            },
        )

    path = _resolve_file_path(persona_id, filename)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    new_content = body.content
    if filename == "config.yaml":
        # Restore redacted tokens then validate — does NOT import yaml.
        new_content = _restore_bot_token(new_content)
        try:
            personas.validate_config_yaml_text(new_content)
        except personas.ConfigShapeError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    # Snapshot existing content to history BEFORE writing (atomic).
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    sha = hashlib.sha256(new_content.encode("utf-8")).hexdigest()
    _snapshot_to_history(persona_id, filename, existing)

    # Atomic write.
    tmp_path = path.parent / f".{filename}.tmp.{uuid.uuid4().hex}"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)

    # Get version_id (last inserted history row).
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM agent_file_history WHERE persona_id = ? AND filename = ? ORDER BY id DESC LIMIT 1",
            (persona_id, filename),
        ).fetchone()
        version_id = row["id"] if row else None
    finally:
        conn.close()

    return {"ok": True, "sha256": sha, "version_id": version_id}


def _snapshot_to_history(persona_id: str, filename: str, content: str) -> None:
    """Insert into agent_file_history; prune to last 100 per (persona, filename)."""
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO agent_file_history
               (persona_id, filename, content, byte_size, sha256, author)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (persona_id, filename, content, len(content.encode("utf-8")), sha, "dashboard"),
        )
        # Prune to last 100.
        conn.execute(
            """DELETE FROM agent_file_history
               WHERE id IN (
                 SELECT id FROM agent_file_history
                  WHERE persona_id = ? AND filename = ?
                  ORDER BY id DESC
                  LIMIT -1 OFFSET 100
               )""",
            (persona_id, filename),
        )
        conn.commit()
    finally:
        conn.close()


@router.get("/api/agents/{persona_id}/files/history")
def get_file_history(persona_id: str) -> dict:
    _reject_main_translation(persona_id)
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT id, filename, byte_size, sha256, author, created_at
               FROM agent_file_history
               WHERE persona_id = ?
               ORDER BY id DESC
               LIMIT 200""",
            (persona_id,),
        ).fetchall()
        versions = [dict(r) for r in rows]
    finally:
        conn.close()
    return {"versions": versions}


# ── /api/agents/{id}/conversation (paginated history) ────────────────────


@router.get("/api/agents/{persona_id}/conversation")
def get_conversation(
    persona_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    before_id: int | None = Query(default=None),
) -> dict:
    _reject_main_translation(persona_id)
    chat_db_path = Path(config.CHAT_DB_PATH)
    if not chat_db_path.is_file():
        return {"turns": [], "next_before_id": None}

    try:
        conn = sqlite3.connect(str(chat_db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Index recovery — best-effort.
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_chat_messages_session_created "
                    "ON chat_messages(session_id, created_at)"
                )
            except sqlite3.OperationalError:
                pass

            params: list[Any] = [persona_id]
            sql = (
                "SELECT m.id, m.session_id, m.role, m.content, m.created_at, "
                "       s.runtime_provider, s.runtime_model "
                "FROM chat_messages m "
                "JOIN chat_sessions s ON m.session_id = s.session_id "
                "WHERE s.runtime_profile_key = ?"
            )
            if before_id is not None:
                sql += " AND m.id < ?"
                params.append(before_id)
            sql += " ORDER BY m.id DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            turns = [dict(r) for r in rows]
            next_before = turns[-1]["id"] if turns and len(turns) >= limit else None
            return {"turns": turns, "next_before_id": next_before}
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return {"turns": [], "next_before_id": None}


# ── /api/agents/{id}/tokens (per-persona lane-aware) ─────────────────────


@router.get("/api/agents/{persona_id}/tokens")
def get_agent_tokens(
    persona_id: str,
    range: str = Query(default="30d"),
) -> dict:
    _reject_main_translation(persona_id)
    return _aggregate_lane_aware_tokens(persona_id=persona_id, range_str=range)


# ── /api/agents/{id}/tasks (convoy-via-list_subtasks_by_agent) ───────────


@router.get("/api/agents/{persona_id}/tasks")
def get_agent_tasks(persona_id: str) -> dict:
    _reject_main_translation(persona_id)
    # Lazy import — avoid pulling orchestration into module init.
    from orchestration.convoy_service import ConvoyService
    from orchestration.db import OrchestrationDB

    db_path = Path(config.ORCHESTRATION_DB_PATH)
    if not db_path.is_file():
        return {"tasks": []}

    db = OrchestrationDB(str(db_path))
    try:
        svc = ConvoyService(db)
        rows = svc.list_subtasks_by_agent(persona_id)
        tasks = [
            {
                "convoy_id": r.convoy_id,
                "subtask_id": r.id,
                "title": r.title,
                "status": r.status,
                "depends_on_subtask_indexes": [],  # not stored as list on Subtask
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
            for r in rows
        ]
        return {"tasks": tasks}
    except ValueError:
        return {"tasks": []}
    finally:
        db.close()


# ── /api/scheduled (CRUD over scheduled_tasks table) ─────────────────────


@router.get("/api/scheduled")
def list_scheduled(persona_id: str | None = Query(default=None)) -> dict:
    conn = get_connection()
    try:
        if persona_id:
            rows = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE persona_id = ? ORDER BY id DESC",
                (persona_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM scheduled_tasks ORDER BY id DESC"
            ).fetchall()
        return {"tasks": [dict(r) for r in rows]}
    finally:
        conn.close()


_CRON_RE = re.compile(r"^[\d\*\-\,\/]+( +[\d\*\-\,\/]+){4}$")


def _validate_cron(schedule: str) -> None:
    if not _CRON_RE.match(schedule.strip()):
        raise HTTPException(status_code=422, detail=f"invalid cron: {schedule!r}")


@router.post("/api/scheduled")
def create_scheduled(body: CreateScheduledBody) -> dict:
    _validate_cron(body.schedule)
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO scheduled_tasks
               (persona_id, prompt, schedule, next_run, status)
               VALUES (?, ?, ?, ?, 'active')""",
            (body.persona_id, body.prompt, body.schedule, body.next_run),
        )
        task_id = cur.lastrowid
        conn.commit()
        row = conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


@router.patch("/api/scheduled/{task_id}")
def patch_scheduled(task_id: int, body: PatchScheduledBody) -> dict:
    fields: dict[str, Any] = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if "schedule" in fields:
        _validate_cron(fields["schedule"])
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = task_id
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE scheduled_tasks SET {sets}, updated_at = strftime('%s', 'now') WHERE id = :id",
            fields,
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        return dict(row)
    finally:
        conn.close()


@router.delete("/api/scheduled/{task_id}")
def delete_scheduled(task_id: int) -> dict:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ── /api/memories (paginated read-only proxy) ────────────────────────────


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in rows}


def _coerce_unix_seconds(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if value is None:
        return int(time.time())
    text = str(value).strip()
    if not text:
        return int(time.time())
    try:
        return int(float(text))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return int(time.time())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _memory_schema_projection(columns: set[str]) -> tuple[str, str, str, str | None] | None:
    if {"id", "file_path", "content", "created_at_epoch"}.issubset(columns):
        section = "section_title" if "section_title" in columns else None
        return "file_path", "content", "created_at_epoch", section
    if {"id", "source_path", "chunk_text", "created_at"}.issubset(columns):
        return "source_path", "chunk_text", "created_at", None
    return None


def _memory_scope_column(columns: set[str]) -> str | None:
    return next((col for col in ("persona_id", "profile_id", "agent_id") if col in columns), None)


def _display_source_path(raw_source_path: str, vault_root: Path) -> str:
    raw = str(raw_source_path or "").strip()
    if not raw:
        return ""
    try:
        path = Path(raw)
        if path.is_absolute():
            try:
                return path.resolve(strict=False).relative_to(vault_root).as_posix()
            except ValueError:
                return path.as_posix()
    except (OSError, ValueError):
        pass
    return raw.replace("\\", "/").lstrip("./")


def _note_id(source_path: str) -> str:
    return "note:" + source_path.replace("\\", "/").strip().lstrip("./")


def _chunk_label(source_path: str, section_title: str, text: str) -> str:
    section = section_title.strip()
    if section:
        return section[:80]
    name = Path(source_path).stem if source_path else ""
    if name:
        return name[:80]
    return "Memory chunk"


def _note_label(source_path: str) -> str:
    stem = Path(source_path).stem
    return stem[:80] if stem else "Vault note"


def _infer_scope_from_source(source_path: str) -> tuple[str, str, str]:
    parts = [p for p in source_path.replace("\\", "/").split("/") if p]
    if len(parts) >= 2 and parts[0] == "agents":
        return "agent", parts[1], "private"
    if len(parts) >= 2 and parts[0] == "teams":
        return "team", parts[1], "shared"
    if len(parts) >= 2 and parts[0] in {"rooms", "cabinet", "warroom"}:
        return "room", parts[1], "shared"
    return "global", "default", "shared"


def _scope_type_for_column(scope_col: str | None) -> str:
    if scope_col == "agent_id":
        return "agent"
    if scope_col in {"persona_id", "profile_id"}:
        return "persona"
    return "global"


def _row_scope(scope_col: str | None, raw_scope_id: str, source_path: str) -> tuple[str, str, str]:
    scope_id = str(raw_scope_id or "").strip()
    if scope_col and scope_id and scope_id != "default":
        return _scope_type_for_column(scope_col), scope_id, "private"
    return _infer_scope_from_source(source_path)


def _node_matches_scope(node: dict[str, Any], scope: str, scope_id: str | None) -> bool:
    if scope == "all":
        return True
    node_scope_type = node.get("scope_type")
    node_scope_id = node.get("scope_id")
    if node_scope_type == "global":
        return True
    if scope == "global":
        return node_scope_type == "global"
    if node_scope_type != scope:
        return False
    if scope_id:
        return node_scope_id == scope_id
    return True


def _add_graph_node(nodes: dict[str, dict[str, Any]], node: dict[str, Any]) -> None:
    node_id = str(node["id"])
    if node_id in nodes:
        return
    nodes[node_id] = node


def _add_graph_edge(edges: dict[str, dict[str, Any]], source: str, target: str, kind: str, **attrs: Any) -> None:
    if not source or not target or source == target:
        return
    edge_id = f"{kind}:{source}->{target}"
    if edge_id in edges:
        edges[edge_id].update(attrs)
        return
    edge = {"id": edge_id, "source": source, "target": target, "kind": kind}
    edge.update(attrs)
    edges[edge_id] = edge


def _truncate_graph_text(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _format_note_preview(chunks: list[dict[str, Any]], max_chunks: int = 3) -> str:
    parts: list[str] = []
    for chunk in chunks[:max_chunks]:
        text = _truncate_graph_text(str(chunk.get("text") or ""), 900)
        if not text:
            continue
        section_title = str(chunk.get("section_title") or "").strip()
        if section_title:
            parts.append(f"## {section_title}\n\n{text}")
        else:
            parts.append(text)
    return "\n\n---\n\n".join(parts)


def _apply_note_previews(nodes: dict[str, dict[str, Any]], note_chunks: dict[str, list[dict[str, Any]]]) -> None:
    for note_id, chunks in note_chunks.items():
        node = nodes.get(note_id)
        if not node:
            continue
        if not str(node.get("text") or "").strip():
            preview = _format_note_preview(chunks)
            if preview:
                node["text"] = preview
                node["preview_source"] = "loaded_chunk_neighbors"
                node["preview_chunk_count"] = len(chunks)


def _source_note_preview(vault_root: Path, source_path: str) -> str:
    note_path = _safe_vault_markdown_path(vault_root, source_path)
    if note_path is None or not note_path.is_file():
        return ""
    try:
        raw = note_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    body = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", raw, flags=re.DOTALL).strip()
    return _truncate_graph_text(body, 1200)


def _apply_source_note_previews(
    nodes: dict[str, dict[str, Any]],
    edges: dict[str, dict[str, Any]],
    vault_root: Path,
    max_notes: int = 220,
) -> None:
    degree: dict[str, int] = {}
    for edge in edges.values():
        source = edge.get("source", "")
        target = edge.get("target", "")
        degree[source] = degree.get(source, 0) + 1
        degree[target] = degree.get(target, 0) + 1

    candidates = [
        node
        for node in nodes.values()
        if node.get("kind") == "note"
        and not str(node.get("text") or "").strip()
        and str(node.get("source_path") or "").strip()
    ]
    candidates.sort(key=lambda node: (degree.get(str(node.get("id") or ""), 0), str(node.get("source_path") or "")), reverse=True)

    for node in candidates[:max_notes]:
        preview = _source_note_preview(vault_root, str(node.get("source_path") or ""))
        if not preview:
            continue
        node["text"] = preview
        node["preview_source"] = "source_markdown"
        node["preview_chunk_count"] = 0


def _safe_vault_markdown_path(vault_root: Path, source_path: str) -> Path | None:
    if not source_path:
        return None
    candidate = Path(source_path)
    if not candidate.is_absolute():
        candidate = vault_root / source_path
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(vault_root)
    except (OSError, ValueError):
        return None
    if resolved.suffix.lower() != ".md":
        return None
    if not resolved.is_file():
        return None
    return resolved


_WIKILINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")
_FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)", re.DOTALL)
_FRONTMATTER_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+\s*:")
_SKIP_VAULT_GRAPH_DIRS = {
    ".obsidian",
    "_templates",
    "_canvas",
    "_state",
    ".conversations",
    ".conversations-archived",
    ".nexus",
    ".workspaces",
    ".workspaces-archived",
}
_WIKILINK_KIND_PRIORITY = {"property": 1, "wikilink": 2, "related": 3}


def _split_markdown_frontmatter(text: str) -> tuple[str, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return "", text
    return match.group(1), text[match.end() :]


def _frontmatter_link_groups(frontmatter: str) -> list[tuple[str, str, str]]:
    groups: list[tuple[str, str, str]] = []
    current_key = ""
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_key, current_lines
        if not current_key:
            return
        kind = "related" if current_key == "related" else "property"
        groups.append((kind, current_key, "\n".join(current_lines)))
        current_key = ""
        current_lines = []

    for line in frontmatter.splitlines():
        if _FRONTMATTER_KEY_RE.match(line):
            flush()
            key, _, rest = line.partition(":")
            current_key = key.strip()
            current_lines = [rest]
            continue
        if current_key:
            current_lines.append(line)
    flush()
    return groups


def _wikilink_target(raw: str) -> str:
    target = raw.split("|", 1)[0].split("#", 1)[0].split("^", 1)[0].strip()
    if not target:
        return ""
    target = target.replace("\\", "/").strip("/")
    if not target.lower().endswith(".md"):
        target += ".md"
    return target


def _vault_markdown_files(vault_root: Path) -> list[Path]:
    if not vault_root.exists():
        return []
    files: list[Path] = []
    for md_path in vault_root.rglob("*.md"):
        try:
            rel_parts = md_path.relative_to(vault_root).parts
        except ValueError:
            continue
        if any(part in _SKIP_VAULT_GRAPH_DIRS for part in rel_parts):
            continue
        files.append(md_path)
    return sorted(files)


def _vault_relative_path(vault_root: Path, md_path: Path) -> str | None:
    try:
        return md_path.relative_to(vault_root).as_posix()
    except ValueError:
        return None


def _resolve_vault_wikilink(
    raw: str,
    exact_paths: dict[str, str],
    stem_paths: dict[str, list[str]],
) -> tuple[str, bool]:
    target_path = _wikilink_target(raw)
    if not target_path:
        return "", False
    exact = exact_paths.get(target_path.lower())
    if exact:
        return exact, True
    paths = stem_paths.get(Path(target_path).stem.lower())
    if paths:
        return min(paths, key=len), True
    return target_path, False


def _add_wikilink_edges(
    *,
    nodes: dict[str, dict[str, Any]],
    edges: dict[str, dict[str, Any]],
    vault_root: Path,
    note_scope: dict[str, tuple[str, str, str]],
) -> dict[str, int]:
    md_files = _vault_markdown_files(vault_root)
    exact_paths: dict[str, str] = {}
    stem_paths: dict[str, list[str]] = {}
    rel_paths: list[str] = []
    for md_path in md_files:
        rel_path = _vault_relative_path(vault_root, md_path)
        if not rel_path:
            continue
        rel_paths.append(rel_path)
        exact_paths[rel_path.lower()] = rel_path
        stem_paths.setdefault(Path(rel_path).stem.lower(), []).append(rel_path)
        note_id = _note_id(rel_path)
        scope_type, scope_id, visibility = note_scope.get(note_id, _infer_scope_from_source(rel_path))
        _add_graph_node(
            nodes,
            {
                "id": note_id,
                "label": _note_label(rel_path),
                "kind": "note",
                "scope_type": scope_type,
                "scope_id": scope_id,
                "visibility": visibility,
                "source_path": rel_path,
                "section_title": "",
                "text": "",
                "tags": ["vault-note", "vault-graph"],
                "created_at": 0,
            },
        )

    semantic_edges: dict[tuple[str, str], dict[str, Any]] = {}
    resolved_mentions = 0
    unresolved_mentions = 0
    semantic_counts = {"wikilink": 0, "related": 0, "property": 0}
    for source_path in rel_paths:
        note_id = _note_id(source_path)
        path = _safe_vault_markdown_path(vault_root, source_path)
        if path is None:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        frontmatter, body = _split_markdown_frontmatter(text)
        link_groups = [
            *_frontmatter_link_groups(frontmatter),
            ("wikilink", "body", body),
        ]
        for kind, source_field, link_text in link_groups:
            for match in _WIKILINK_RE.finditer(link_text):
                target_path, resolved = _resolve_vault_wikilink(match.group(1), exact_paths, stem_paths)
                if not target_path:
                    continue
                target_id = _note_id(target_path)
                if target_id not in nodes:
                    scope_type, scope_id, visibility = note_scope.get(target_id, _infer_scope_from_source(target_path))
                    _add_graph_node(
                        nodes,
                        {
                            "id": target_id,
                            "label": _note_label(target_path),
                            "kind": "note",
                            "scope_type": scope_type,
                            "scope_id": scope_id,
                            "visibility": visibility,
                            "source_path": target_path,
                            "section_title": "",
                            "text": "",
                            "tags": ["vault-note", "wikilink-target", "unresolved-wikilink"],
                            "created_at": 0,
                        },
                    )
                pair = (note_id, target_id)
                existing = semantic_edges.get(pair)
                if existing is None:
                    semantic_edges[pair] = {
                        "kind": kind,
                        "source_field": source_field,
                        "resolved": resolved,
                        "mentions": 1,
                    }
                else:
                    existing["mentions"] = int(existing.get("mentions", 0)) + 1
                    existing["resolved"] = bool(existing.get("resolved")) or resolved
                    if _WIKILINK_KIND_PRIORITY[kind] > _WIKILINK_KIND_PRIORITY[str(existing.get("kind", "property"))]:
                        existing["kind"] = kind
                        existing["source_field"] = source_field
                if resolved:
                    resolved_mentions += 1
                else:
                    unresolved_mentions += 1
    resolved_edges = 0
    unresolved_edges = 0
    for (source_id, target_id), meta in semantic_edges.items():
        kind = str(meta.get("kind") or "wikilink")
        resolved = bool(meta.get("resolved"))
        mention_count = int(meta.get("mentions") or 1)
        semantic_counts[kind] = semantic_counts.get(kind, 0) + 1
        _add_graph_edge(
            edges,
            source_id,
            target_id,
            kind,
            resolved=resolved,
            mention_count=mention_count,
            source_field=str(meta.get("source_field") or ("body" if kind == "wikilink" else kind)),
        )
        if resolved:
            resolved_edges += 1
        else:
            unresolved_edges += 1
    return {
        "vault_notes": len(rel_paths),
        "vault_wikilink_edges": resolved_edges + unresolved_edges,
        "vault_resolved_wikilink_edges": resolved_edges,
        "vault_unresolved_wikilink_edges": unresolved_edges,
        "vault_wikilink_mentions": resolved_mentions + unresolved_mentions,
        "vault_body_wikilink_edges": semantic_counts.get("wikilink", 0),
        "vault_related_edges": semantic_counts.get("related", 0),
        "vault_property_wikilink_edges": semantic_counts.get("property", 0),
    }


def _add_cabinet_session_nodes(
    *,
    nodes: dict[str, dict[str, Any]],
    edges: dict[str, dict[str, Any]],
    scope: str,
    scope_id: str | None,
    limit: int,
) -> None:
    if scope not in {"all", "room"}:
        return
    conn = get_connection()
    try:
        where = ""
        params: list[Any] = []
        if scope == "room" and scope_id:
            raw_id = scope_id.removeprefix("cabinet-")
            if raw_id.isdigit():
                where = "WHERE id = ?"
                params.append(int(raw_id))
            else:
                where = "WHERE 1 = 0"
        rows = conn.execute(
            f"""SELECT id, started_at, ended_at, mode, pinned_persona, entry_count,
                       title, chat_id
                FROM cabinet_meetings
                {where}
                ORDER BY started_at DESC
                LIMIT ?""",
            (*params, min(limit, 25)),
        ).fetchall()
        for row in rows:
            meeting_id = int(row["id"])
            room_scope = f"cabinet-{meeting_id}"
            title = str(row["title"] or "").strip() or f"Cabinet #{meeting_id}"
            mode = str(row["mode"] or "text")
            entry_count = int(row["entry_count"] or 0)
            node_id = f"room:{room_scope}"
            _add_graph_node(
                nodes,
                {
                    "id": node_id,
                    "label": title[:80],
                    "kind": "session",
                    "scope_type": "room",
                    "scope_id": room_scope,
                    "visibility": "shared",
                    "source_path": "",
                    "section_title": "Cabinet session",
                    "text": f"{mode} room with {entry_count} transcript entries.",
                    "tags": ["cabinet", "war-room", "session"],
                    "created_at": int(row["started_at"] or 0),
                    "ended_at": row["ended_at"],
                    "chat_id": row["chat_id"],
                    "pinned_persona": row["pinned_persona"],
                },
            )
            for global_node in list(nodes.values()):
                if global_node.get("scope_type") == "global" and global_node.get("kind") == "note":
                    _add_graph_edge(edges, node_id, str(global_node["id"]), "scope")
                    break
    finally:
        conn.close()


@router.get("/api/memory/graph")
def get_memory_graph(
    scope: str = Query(default="all"),
    scope_id: str | None = Query(default=None),
    limit: int = Query(default=120, ge=1, le=300),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Read-only vault/memory graph. Does NOT call recall_service."""
    allowed_scopes = {"all", "global", "persona", "agent", "team", "room"}
    if scope not in allowed_scopes:
        raise HTTPException(status_code=422, detail=f"invalid scope: {scope}")
    if scope == "persona" and scope_id is not None:
        _reject_main_translation(scope_id)

    db_path = Path(config.DATABASE_PATH) if hasattr(config, "DATABASE_PATH") else None
    vault_root = Path(config.MEMORY_DIR).resolve(strict=False)
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    note_scope: dict[str, tuple[str, str, str]] = {}
    note_chunks: dict[str, list[dict[str, Any]]] = {}
    stats: dict[str, Any] = {
        "scope": scope,
        "scope_id": scope_id,
        "persona_filter_supported": False,
        "total_chunks": 0,
    }

    if db_path is not None and db_path.is_file():
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                columns = _table_columns(conn, "chunks")
                projection = _memory_schema_projection(columns)
                if projection is not None:
                    source_col, text_col, created_col, section_col = projection
                    scope_col = _memory_scope_column(columns)
                    stats["persona_filter_supported"] = bool(scope_col)
                    section_expr = f"{section_col} AS section_title" if section_col else "'' AS section_title"
                    scope_expr = f"{scope_col} AS row_scope_id" if scope_col else "'default' AS row_scope_id"
                    sql = (
                        "SELECT id, "
                        f"{source_col} AS source_path, "
                        f"{text_col} AS chunk_text, "
                        f"{created_col} AS created_at, "
                        f"{section_expr}, "
                        f"{scope_expr} "
                        "FROM chunks"
                    )
                    params: list[Any] = []
                    where_sql = ""
                    if scope_col:
                        if scope == "global":
                            where_sql = f" WHERE ({scope_col} IS NULL OR {scope_col} = '' OR {scope_col} = 'default')"
                        elif scope in {"persona", "agent"} and scope_id:
                            # Persona/agent graph views are overlays: include global/default
                            # memory plus rows owned by the selected scope.
                            where_sql = (
                                f" WHERE ({scope_col} = ? OR {scope_col} IS NULL "
                                f"OR {scope_col} = '' OR {scope_col} = 'default')"
                            )
                            params.append(scope_id)
                    sql += where_sql
                    count_sql = "SELECT COUNT(*) FROM chunks" + where_sql
                    matching_chunks = int(conn.execute(count_sql, params).fetchone()[0])
                    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
                    params.extend([limit, offset])
                    rows = conn.execute(sql, params).fetchall()
                    stats["total_chunks"] = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
                    stats["matching_chunks"] = matching_chunks
                    stats["returned_chunks"] = len(rows)
                    for row in rows:
                        source_path = _display_source_path(str(row["source_path"] or ""), vault_root)
                        text = str(row["chunk_text"] or "")
                        section_title = str(row["section_title"] or "").strip()
                        scope_type, node_scope_id, visibility = _row_scope(
                            scope_col,
                            str(row["row_scope_id"] or ""),
                            source_path,
                        )
                        chunk_node = {
                            "id": f"chunk:{row['id']}",
                            "label": _chunk_label(source_path, section_title, text),
                            "kind": "chunk",
                            "scope_type": scope_type,
                            "scope_id": node_scope_id,
                            "visibility": visibility,
                            "source_path": source_path,
                            "section_title": section_title,
                            "text": text,
                            "tags": ["vault-chunk", scope_type],
                            "created_at": _coerce_unix_seconds(row["created_at"]),
                        }
                        if not _node_matches_scope(chunk_node, scope, scope_id):
                            continue
                        _add_graph_node(nodes, chunk_node)
                        if source_path:
                            note_id = _note_id(source_path)
                            if text.strip():
                                note_chunks.setdefault(note_id, []).append(
                                    {
                                        "section_title": section_title,
                                        "text": text,
                                        "created_at": _coerce_unix_seconds(row["created_at"]),
                                    }
                                )
                            note_node = {
                                "id": note_id,
                                "label": _note_label(source_path),
                                "kind": "note",
                                "scope_type": scope_type,
                                "scope_id": node_scope_id,
                                "visibility": visibility,
                                "source_path": source_path,
                                "section_title": "",
                                "text": "",
                                "tags": ["vault-note", scope_type],
                                "created_at": _coerce_unix_seconds(row["created_at"]),
                            }
                            _add_graph_node(nodes, note_node)
                            note_scope[note_id] = (scope_type, node_scope_id, visibility)
                            _add_graph_edge(edges, str(chunk_node["id"]), note_id, "source")
                else:
                    stats["schema"] = "unsupported"
            finally:
                conn.close()
        except sqlite3.OperationalError:
            stats["schema"] = "missing"

    _apply_note_previews(nodes, note_chunks)
    vault_graph_stats = _add_wikilink_edges(nodes=nodes, edges=edges, vault_root=vault_root, note_scope=note_scope)
    _apply_source_note_previews(nodes=nodes, edges=edges, vault_root=vault_root)
    _add_cabinet_session_nodes(nodes=nodes, edges=edges, scope=scope, scope_id=scope_id, limit=limit)

    scope_counts: dict[str, int] = {}
    for node in nodes.values():
        key = f"{node.get('scope_type', 'global')}:{node.get('scope_id', 'default')}"
        scope_counts[key] = scope_counts.get(key, 0) + 1
    stats.update(
        {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "returned_nodes": len(nodes),
            "returned_edges": len(edges),
            "page": {
                "limit": limit,
                "offset": offset,
                "returned_chunks": int(stats.get("returned_chunks", 0)),
                "matching_chunks": int(stats.get("matching_chunks", stats.get("total_chunks", 0))),
                "has_more": offset + int(stats.get("returned_chunks", 0)) < int(stats.get("matching_chunks", 0)),
            },
            "scopes": [
                {"scope_type": key.split(":", 1)[0], "scope_id": key.split(":", 1)[1], "count": count}
                for key, count in sorted(scope_counts.items())
            ],
            "vault_graph": vault_graph_stats,
        }
    )
    return {"nodes": list(nodes.values()), "edges": list(edges.values()), "stats": stats}


def _brain_activity_persona_filter(scope: str, scope_id: str | None) -> str | None:
    """Map a brain scope to the current Hive activity filter, when supported."""
    if scope in {"persona", "agent"} and scope_id:
        return scope_id
    return None


@router.get("/api/brain/graph")
def get_brain_graph(
    scope: str = Query(default="all"),
    scope_id: str | None = Query(default=None),
    activity_window_minutes: int = Query(default=60, ge=1),
    limit: int = Query(default=120, ge=1, le=300),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Composed brain graph: durable memory base plus recent Hive activity overlay."""
    allowed_scopes = {"all", "global", "persona", "agent", "team", "room"}
    if scope not in allowed_scopes:
        raise HTTPException(status_code=422, detail=f"invalid scope: {scope}")
    if scope == "persona" and scope_id is not None:
        _reject_main_translation(scope_id)

    memory = get_memory_graph(scope=scope, scope_id=scope_id, limit=limit, offset=offset)
    activity_persona_id = _brain_activity_persona_filter(scope, scope_id)
    activity_limit = min(limit, 200)
    hive = get_hive_mind_recent(
        limit=activity_limit,
        persona_id=activity_persona_id,
        window_minutes=activity_window_minutes,
    )
    activity = hive.get("events") or hive.get("entries") or []
    memory_stats = memory.get("stats") if isinstance(memory.get("stats"), dict) else {}
    scopes = [
        f"{item.get('scope_type', 'global')}/{item.get('scope_id', 'default')}"
        for item in memory_stats.get("scopes", [])
        if isinstance(item, dict)
    ]
    if not scopes:
        scopes = ["global/default"]

    stats = {
        "scope": scope,
        "scope_id": scope_id,
        "total_nodes": len(memory.get("nodes", [])),
        "total_edges": len(memory.get("edges", [])),
        "returned_nodes": len(memory.get("nodes", [])),
        "returned_edges": len(memory.get("edges", [])),
        "limit": limit,
        "offset": offset,
        "activity_count": len(activity),
        "activity_window_minutes": activity_window_minutes,
        "activity_filter_persona_id": activity_persona_id,
        "memory": memory_stats,
        "activity": {
            "window_minutes": activity_window_minutes,
            "limit": activity_limit,
            "filter_persona_id": activity_persona_id,
            "total_events": len(activity),
        },
    }
    return {
        "nodes": memory.get("nodes", []),
        "edges": memory.get("edges", []),
        "activity": activity,
        "layers": {
            "memory": True,
            "activity": True,
            "scopes": scopes,
        },
        "stats": stats,
    }


@router.get("/api/memories")
def get_memories(
    persona_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    before_id: int | None = Query(default=None),
) -> dict:
    """Paginated memory listing. Does NOT call recall_service (read-only)."""
    if persona_id is not None:
        _reject_main_translation(persona_id)
    db_path = Path(config.DATABASE_PATH) if hasattr(config, "DATABASE_PATH") else None
    if db_path is None or not db_path.is_file():
        return {"memories": [], "stats": {}, "next_before_id": None}

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            columns = _table_columns(conn, "chunks")
            projection = _memory_schema_projection(columns)
            if projection is None:
                return {
                    "memories": [],
                    "stats": {"total_chunks": 0, "schema": "unsupported"},
                    "next_before_id": None,
                }

            source_col, text_col, created_col, section_col = projection
            scope_col = next(
                (col for col in ("persona_id", "profile_id", "agent_id") if col in columns),
                None,
            )
            if persona_id and persona_id != "default" and scope_col is None:
                return {
                    "memories": [],
                    "stats": {
                        "total_chunks": 0,
                        "returned": 0,
                        "scope": "global_vault",
                        "persona_filter_supported": False,
                    },
                    "next_before_id": None,
                }

            section_expr = f"{section_col} AS section_title" if section_col else "'' AS section_title"
            scope_expr = f"{scope_col} AS persona_id" if scope_col else "'default' AS persona_id"
            sql = (
                "SELECT id, "
                f"{source_col} AS source_path, "
                f"{text_col} AS chunk_text, "
                f"{created_col} AS created_at, "
                f"{section_expr}, "
                f"{scope_expr} "
                "FROM chunks"
            )
            params: list[Any] = []
            wheres: list[str] = []
            count_wheres: list[str] = []
            count_params: list[Any] = []
            if persona_id and scope_col is not None:
                wheres.append(f"{scope_col} = ?")
                params.append(persona_id)
                count_wheres.append(f"{scope_col} = ?")
                count_params.append(persona_id)
            if before_id is not None:
                wheres.append("id < ?")
                params.append(before_id)
            if wheres:
                sql += " WHERE " + " AND ".join(wheres)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            count_sql = "SELECT COUNT(*) FROM chunks"
            if count_wheres:
                count_sql += " WHERE " + " AND ".join(count_wheres)
            total_chunks = int(conn.execute(count_sql, count_params).fetchone()[0])
            memories = []
            for row in rows:
                persona = row["persona_id"] or "default"
                created_at = _coerce_unix_seconds(row["created_at"])
                section_title = str(row["section_title"] or "").strip()
                tags = ["vault-chunk"]
                if section_title:
                    tags.append(section_title)
                source_path = str(row["source_path"] or "")
                text = str(row["chunk_text"] or "")
                memories.append(
                    {
                        "id": row["id"],
                        "persona_id": persona,
                        "personaId": persona,
                        "source_path": source_path,
                        "sourcePath": source_path,
                        "chunk_text": text,
                        "text": text,
                        "created_at": created_at,
                        "createdAt": created_at,
                        "tags": tags,
                        "kind": "vault_chunk",
                    }
                )
            next_before = memories[-1]["id"] if memories and len(memories) >= limit else None
            stats = {
                "total_chunks": total_chunks,
                "returned": len(memories),
                "scope": "profile" if scope_col else "global_vault",
                "persona_filter_supported": bool(scope_col),
            }
            return {"memories": memories, "stats": stats, "next_before_id": next_before}
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return {"memories": [], "stats": {}, "next_before_id": None}


# ── /api/tokens (global lane-aware) ──────────────────────────────────────


@router.get("/api/tokens")
def get_tokens(
    range: str = Query(default="30d"),
    interval: str = Query(default="day"),
) -> dict:
    return _aggregate_lane_aware_tokens(persona_id=None, range_str=range, interval=interval)


# ── /api/hive-mind/recent ────────────────────────────────────────────────


@router.get("/api/hive-mind/recent")
def get_hive_mind_recent(
    limit: int = Query(default=50, ge=1, le=200),
    persona_id: str | None = Query(default=None),
    window_minutes: int = Query(default=60, ge=1),
) -> dict:
    if persona_id is not None:
        _reject_main_translation(persona_id)
    chat_db_path = Path(config.CHAT_DB_PATH)
    if not chat_db_path.is_file():
        return {"entries": []}
    try:
        conn = sqlite3.connect(str(chat_db_path))
        conn.row_factory = sqlite3.Row
        try:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
            ).replace(tzinfo=None).isoformat(timespec="seconds")
            sql = (
                "SELECT COALESCE(NULLIF(s.runtime_profile_key, ''), 'default') AS persona_id, "
                "       m.id AS event_id, m.role, "
                "       substr(m.content, 1, 200) AS excerpt, "
                "       m.created_at, s.runtime_provider AS provider, "
                "       s.runtime_model AS model "
                "FROM chat_messages m "
                "JOIN chat_sessions s ON m.session_id = s.session_id "
                "WHERE datetime(m.created_at) >= datetime(?) "
            )
            params: list[Any] = [cutoff]
            if persona_id:
                if persona_id == "default":
                    sql += "AND (s.runtime_profile_key = ? OR s.runtime_profile_key = '') "
                    params.append(persona_id)
                else:
                    sql += "AND s.runtime_profile_key = ? "
                    params.append(persona_id)
            sql += "ORDER BY m.id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            entries = []
            events = []
            for row in rows:
                persona = row["persona_id"] or "default"
                created_at = str(row["created_at"] or "")
                timestamp = _coerce_unix_seconds(created_at)
                entry = {
                    **dict(row),
                    "persona_id": persona,
                    "event_type": "chat_message",
                }
                event = {
                    "id": f"chat-{row['event_id']}",
                    "eventId": row["event_id"],
                    "persona_id": persona,
                    "personaId": persona,
                    "type": "chat_message",
                    "role": row["role"],
                    "timestamp": timestamp,
                    "created_at": created_at,
                    "createdAt": timestamp,
                    "details": row["excerpt"] or "",
                    "provider": row["provider"],
                    "model": row["model"],
                }
                entries.append(entry)
                events.append(event)
            return {"entries": entries, "events": events}
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return {"entries": []}


# ── /api/dashboard/settings ──────────────────────────────────────────────


@router.get("/api/dashboard/settings")
def get_settings() -> dict:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT key, value FROM dashboard_settings").fetchall()
        out: dict[str, Any] = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except (TypeError, ValueError):
                out[r["key"]] = r["value"]
        return {"settings": out}
    finally:
        conn.close()


@router.patch("/api/dashboard/settings")
def patch_settings(body: PatchSettingsBody) -> dict:
    if body.key is not None:
        _write_settings_value(body.key, body.value)
    elif body.settings is not None:
        for k, v in body.settings.items():
            _write_settings_value(k, v)
    else:
        raise HTTPException(status_code=400, detail="must pass key/value or settings dict")
    return get_settings()


# ── /api/conversation/{persona_id}/stream (SSE) ──────────────────────────


# In-memory replay buffer for SSE events. Keyed by (persona_id,
# conversation_id) → list of (event_id, event_type, data_json). Bounded
# at 100 events per stream. Module-level state IS allowed for ephemeral
# replay buffers (Rule 2 doesn't ban runtime caches; it bans caching of
# RESOLVED CONFIG STATE at module init). The buffer is process-local.

_SSE_REPLAY_BUFFERS: dict[tuple[str, str], list[tuple[int, str, str]]] = {}
_SSE_REPLAY_LIMIT = 100


def _sse_buffer_for(persona_id: str, conversation_id: str) -> list:
    return _SSE_REPLAY_BUFFERS.setdefault((persona_id, conversation_id), [])


def _sse_buffer_append(persona_id: str, conversation_id: str, event_id: int, event_type: str, data: str) -> None:
    buf = _sse_buffer_for(persona_id, conversation_id)
    buf.append((event_id, event_type, data))
    if len(buf) > _SSE_REPLAY_LIMIT:
        del buf[: len(buf) - _SSE_REPLAY_LIMIT]


def _sse_format(event_id: int, event_type: str, data: str) -> str:
    """Format an SSE event with ``id:`` line BEFORE ``data:`` line (R1 B7)."""
    return f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n"


def _sse_format_no_id(event_type: str, data: str) -> str:
    """Format an SSE event WITHOUT an ``id:`` line.

    Per the SSE spec (https://html.spec.whatwg.org/multipage/server-sent-events.html#concept-event-stream-last-event-id),
    when a `data:` event arrives without an `id:` line, the browser KEEPS
    its prior lastEventId. Use this for snapshot/initial-state writes that
    must NOT clobber the client's reconnect cursor with a low value.
    (Phase 5a dashboard-owner SSE minor fix — snapshot id=0 was overwriting
    real Last-Event-ID positions on reconnect.)
    """
    return f"event: {event_type}\ndata: {data}\n\n"


@router.get("/api/conversation/{persona_id}/stream")
async def conversation_stream(
    persona_id: str,
    request: Request,
    conversation_id: str = Query(default="default"),
) -> StreamingResponse:
    _reject_main_translation(persona_id)

    last_event_id_header = request.headers.get("Last-Event-ID")
    last_event_id: int | None = None
    if last_event_id_header is not None:
        try:
            last_event_id = int(last_event_id_header)
        except ValueError:
            last_event_id = None

    # If client sent Last-Event-ID outside our buffer window → 410 Gone.
    buf = _sse_buffer_for(persona_id, conversation_id)
    if last_event_id is not None and buf:
        earliest_buffered = buf[0][0]
        if last_event_id < earliest_buffered:
            return JSONResponse(
                status_code=410,
                content={"error": "stale Last-Event-ID outside replay buffer"},
                headers={
                    "X-Refetch-Hint": f"GET /api/agents/{persona_id}/conversation",
                },
            )

    async def event_gen() -> AsyncIterator[bytes]:
        # Replay buffered events with id > last_event_id (R1 B7 — no
        # duplicates, no skipped events).
        if last_event_id is not None:
            for ev_id, ev_type, ev_data in buf:
                if ev_id > last_event_id:
                    yield _sse_format(ev_id, ev_type, ev_data).encode("utf-8")

        # Initial 'processing' event if we're starting fresh.
        if last_event_id is None:
            buf_now = _sse_buffer_for(persona_id, conversation_id)
            next_id = (buf_now[-1][0] + 1) if buf_now else 1
            data = json.dumps({"persona_id": persona_id, "status": "processing"})
            _sse_buffer_append(persona_id, conversation_id, next_id, "processing", data)
            yield _sse_format(next_id, "processing", data).encode("utf-8")

        # Keepalive loop — emit `: keepalive\n\n` every 20s.
        last_keepalive = time.monotonic()
        while True:
            if await request.is_disconnected():
                return
            now = time.monotonic()
            if now - last_keepalive >= 20:
                yield b": keepalive\n\n"
                last_keepalive = now
            # Sleep in small chunks so client-disconnect is detected quickly.
            import asyncio  # noqa: PLC0415
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )


# ── Cabinet endpoints (PRD-8 Phase 5a / WS2) ─────────────────────────────
#
# 11 verbatim ports of `dashboard.ts:802-1254` action/query-shaped routes
# (`/list`, `/new`, `/warmup`, `/transcripts` (= upstream `/history`),
# `/stream`, `/send`, `/abort`, `/pin`, `/unpin`, `/clear`, `/end`) PLUS
# 1 Homie delta `GET /api/cabinet/details` (page-load helper not present
# upstream).
#
# HOMIE PREFIX DELTA: `/api/warroom/text/...` → `/api/cabinet/...`.
#
# B1 lock — the orchestrator dispatches every per-persona turn via
# `runtime.lane_router.run_with_runtime_lanes(RuntimeRequest)`. NO direct
# provider-SDK calls inside cabinet/* modules.
#
# B4 SSE shape — subscribe-first → seen_seqs dedup → snapshot direct-write
# → replay-after. 410+X-Refetch-Hint Homie delta when Last-Event-ID <
# oldest_seq.
#
# B6 Q4 translation — every persona-id-bearing field translated at the
# Hono boundary (`dashboard/server/src/routes/cabinet.ts`); the Python
# framework rejects `'main'` and accepts `'default'`.

from cabinet import (  # noqa: E402, I001
    meeting_channel as _cabinet_channels,
    room_commands as _cabinet_room_commands,
    room_state as _cabinet_room_state,
    text_orchestrator as _cabinet_orch,
    title as _cabinet_title,
)

class CabinetNewBody(BaseModel):
    chatId: str | None = None


class CabinetOpenBody(BaseModel):
    chatId: str | None = None


class CabinetSendBody(BaseModel):
    meetingId: int
    text: str
    clientMsgId: str
    chatId: str | None = None
    # PRD-8 Phase 6 — voice extensions (forward-additive, default-False).
    # ``isVoice``: when True, _run_agent_turn prepends a voice-mode context
    # hint (port from agent-voice-bridge.ts:144). ``targetAgentId``: when
    # set, pin the turn to this persona — bypass Haiku router (preserves
    # the upstream agent_id selection from warroom/agent_bridge.py:59-66).
    isVoice: bool = False
    targetAgentId: str | None = None
    audience: str = "auto"
    targetAgentIds: list[str] | None = None


class CabinetMeetingIdBody(BaseModel):
    meetingId: int
    chatId: str | None = None


class CabinetPinBody(BaseModel):
    meetingId: int
    agentId: str
    chatId: str | None = None


class CabinetParticipantBody(BaseModel):
    meetingId: int
    agentId: str
    chatId: str | None = None


def _cabinet_chat_match_or_403(meeting: dict, request_chat_id: str) -> bool:
    """Port dashboard.ts:1007-1014 requireChatMatches.

    Legacy meetings (chat_id == '') accept any chatId. Otherwise the
    meeting's chat_id MUST match the request's chat_id (chat-scope guard).
    """
    meeting_chat = meeting.get("chat_id", "") or ""
    if meeting_chat == "":
        return True
    if meeting_chat == (request_chat_id or ""):
        return True
    return False


def _cabinet_get_meeting(meeting_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT id, started_at, ended_at, mode, pinned_persona, entry_count,
                       title, chat_id
               FROM cabinet_meetings WHERE id = ?""",
            (meeting_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _cabinet_roster_dicts(meeting_id: int | None = None) -> list[dict]:
    """Roster as plain dicts (camelCase wire shape) for SSE/REST responses."""
    roster = (
        _cabinet_room_state.load_meeting_roster(meeting_id)
        if meeting_id is not None
        else _cabinet_orch.get_roster()
    )
    return _cabinet_room_state.roster_to_wire(roster)


def _cabinet_broadcast_order(meeting_id: int) -> list[str]:
    roster = _cabinet_room_state.load_meeting_roster(meeting_id)
    return _cabinet_room_state.broadcast_order(roster)


def _cabinet_validate_room_request(meeting_id: int, chat_id: str) -> dict:
    meeting = _cabinet_get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting_not_found")
    if meeting.get("ended_at") is not None:
        raise HTTPException(status_code=410, detail="meeting_ended")
    if chat_id and not _cabinet_chat_match_or_403(meeting, chat_id):
        raise HTTPException(status_code=403, detail="chat_mismatch")
    return meeting


def _cabinet_room_state_error(exc: Exception) -> HTTPException:
    if isinstance(exc, _cabinet_room_state.CabinetMeetingNotFound):
        return HTTPException(status_code=404, detail="meeting_not_found")
    if isinstance(exc, _cabinet_room_state.CabinetMeetingEnded):
        return HTTPException(status_code=410, detail="meeting_ended")
    if isinstance(exc, _cabinet_room_state.CabinetUnknownAgent):
        return HTTPException(status_code=400, detail="unknown agent")
    if isinstance(exc, _cabinet_room_state.CabinetDefaultRemovalRejected):
        return HTTPException(status_code=400, detail="cannot remove default agent")
    return HTTPException(status_code=400, detail="room_state_error")


@router.get("/api/cabinet/list")
def cabinet_list(
    limit: int = Query(default=20, ge=1, le=100),
    chatId: str | None = Query(default=None),
) -> dict:
    """Port dashboard.ts:802-810 — list cabinet meetings."""
    conn = get_connection()
    try:
        if chatId is not None:
            rows = conn.execute(
                """SELECT id, started_at, ended_at, mode, pinned_persona, entry_count,
                          title, chat_id
                   FROM cabinet_meetings
                   WHERE chat_id = ?
                   ORDER BY started_at DESC LIMIT ?""",
                (chatId, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, started_at, ended_at, mode, pinned_persona, entry_count,
                          title, chat_id
                   FROM cabinet_meetings
                   ORDER BY started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    return {"ok": True, "meetings": [dict(r) for r in rows]}


@router.post("/api/cabinet/new")
def cabinet_new(body: CabinetNewBody | None = None) -> dict:
    """Port dashboard.ts:812-838 — create meeting + auto-end stale.

    Audit-log row written for `cabinet_create`.
    """
    chat_id = (body.chatId.strip() if body and body.chatId else "")
    conn = get_connection()
    try:
        # Force-end any prior open meetings IN THE SAME CHAT.
        if chat_id:
            stale_rows = conn.execute(
                """SELECT id FROM cabinet_meetings
                   WHERE chat_id = ? AND ended_at IS NULL""",
                (chat_id,),
            ).fetchall()
        else:
            stale_rows = []
        stale_ids = [r["id"] for r in stale_rows]

        # PRD-8 Phase 6 v2 fix-pass 2026-05-10 (M3 fix) — populate
        # broadcast_order at meeting-create time. Phase 6's voice
        # subprocess (HomieAgentBridge) iterates this list in stable order
        # for broadcast turns ("everyone, status update"). Without writing
        # at create time the column stays NULL and the bridge falls back
        # to the hardcoded BROADCAST_ORDER constant in agent_bridge.py
        # (which doesn't reflect the actual roster snapshot for this
        # meeting). The snapshot uses the same cabinet roster shape as
        # roster_json above so the two derived states stay consistent.
        roster_dicts = _cabinet_roster_dicts()
        broadcast_order_ids = [a["id"] for a in roster_dicts if isinstance(a, dict) and a.get("id")]
        broadcast_order_json = json.dumps(broadcast_order_ids)

        cur = conn.execute(
            """INSERT INTO cabinet_meetings (mode, chat_id, broadcast_order)
               VALUES (?, ?, ?)""",
            ("text", chat_id, broadcast_order_json),
        )
        meeting_id = cur.lastrowid

        # Mark stale meetings ended.
        for sid in stale_ids:
            conn.execute(
                """UPDATE cabinet_meetings SET ended_at = strftime('%s','now')
                   WHERE id = ? AND ended_at IS NULL""",
                (sid,),
            )

        # Snapshot roster for replay determinism.
        roster_json = json.dumps(roster_dicts)
        conn.execute(
            """INSERT INTO cabinet_text_meetings (meeting_id, roster_json)
               VALUES (?, ?)""",
            (meeting_id, roster_json),
        )
        conn.commit()
    finally:
        conn.close()

    # Prime the channel so the SSE emit for meeting_state has a target.
    _cabinet_channels.get_channel(meeting_id)

    _audit_write(
        operator_id="cabinet",
        action="cabinet_create",
        target_persona_id="",
        outcome="created",
        detail={"meeting_id": meeting_id, "auto_ended": stale_ids, "chat_id": chat_id},
    )

    # Best-effort emit of meeting_ended on the stale meetings' channels.
    for sid in stale_ids:
        try:
            ch = _cabinet_channels.get_channel(sid)
            ch.emit({
                "type": "meeting_ended",
                "meetingId": sid,
                "at": int(time.time()),
            })
        except Exception:  # noqa: BLE001
            pass

    return {"ok": True, "meetingId": meeting_id, "autoEnded": stale_ids}


@router.post("/api/cabinet/open")
def cabinet_open(body: CabinetOpenBody | None = None) -> dict:
    """Open the current Cabinet room for a chat, creating it if needed."""
    chat_id = (body.chatId.strip() if body and body.chatId else "dashboard")
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT id, started_at, ended_at, mode, pinned_persona, entry_count,
                      title, chat_id
               FROM cabinet_meetings
               WHERE chat_id = ? AND ended_at IS NULL
               ORDER BY started_at DESC LIMIT 1""",
            (chat_id,),
        ).fetchone()
    finally:
        conn.close()

    created = False
    if row is None:
        created_body = cabinet_new(CabinetNewBody(chatId=chat_id))
        meeting_id = int(created_body["meetingId"])
        created = True
        meeting = _cabinet_get_meeting(meeting_id)
    else:
        meeting = dict(row)
        meeting_id = int(meeting["id"])

    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting_not_found")
    roster = _cabinet_roster_dicts(meeting_id)
    return {
        "ok": True,
        "meetingId": meeting_id,
        "created": created,
        "meeting": meeting,
        "roster": roster,
        "agents": roster,
        "broadcastOrder": _cabinet_broadcast_order(meeting_id),
        "pinnedAgent": meeting.get("pinned_persona"),
        "status": "open",
    }


@router.post("/api/cabinet/warmup")
async def cabinet_warmup() -> dict:
    """Port dashboard.ts:843-849 — pre-warm SDK path. Idempotent."""
    if _cabinet_orch.is_warmup_done():
        return {"ok": True, "already": True}
    # Don't await — fire-and-forget so the client doesn't block.
    import asyncio  # noqa: PLC0415
    asyncio.create_task(_cabinet_orch.warmup_meeting())
    return {"ok": True, "started": True}


@router.get("/api/cabinet/details")
def cabinet_details(
    meetingId: int = Query(...),
    chatId: str | None = Query(default=None),
) -> dict:
    """HOMIE DELTA — page-load helper not present upstream.

    Returns meeting details + roster + pinned + status for `Cabinet.tsx`.
    """
    meeting = _cabinet_get_meeting(meetingId)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting_not_found")
    if chatId is not None and not _cabinet_chat_match_or_403(meeting, chatId):
        raise HTTPException(status_code=403, detail="chat_mismatch")
    roster = _cabinet_roster_dicts(meetingId)
    return {
        "ok": True,
        "meeting": meeting,
        "roster": roster,
        "agents": roster,
        "broadcastOrder": _cabinet_broadcast_order(meetingId),
        "pinnedAgent": meeting.get("pinned_persona"),
        "status": "ended" if meeting.get("ended_at") else "open",
    }


@router.get("/api/cabinet/participants/available")
def cabinet_participants_available(
    meetingId: int = Query(...),
    chatId: str | None = Query(default=None),
) -> dict:
    meeting = _cabinet_validate_room_request(meetingId, (chatId or "").strip())
    available = _cabinet_room_state.list_available_agents(meetingId)
    _ = meeting
    return {
        "ok": True,
        "meetingId": meetingId,
        "agents": _cabinet_room_state.roster_to_wire(available),
    }


@router.post("/api/cabinet/participants/add")
def cabinet_participant_add(body: CabinetParticipantBody) -> dict:
    chat_id = (body.chatId or "").strip()
    _cabinet_validate_room_request(body.meetingId, chat_id)
    agent_id = (body.agentId or "").strip()
    if not agent_id:
        raise HTTPException(status_code=400, detail="invalid agentId")
    _reject_main_translation(agent_id)
    try:
        roster = _cabinet_room_state.add_meeting_participant(body.meetingId, agent_id)
    except Exception as exc:  # noqa: BLE001
        raise _cabinet_room_state_error(exc) from exc
    wire_roster = _cabinet_room_state.roster_to_wire(roster)
    order = _cabinet_room_state.broadcast_order(roster)
    _cabinet_channels.get_channel(body.meetingId).emit({
        "type": "meeting_state_update",
        "agents": wire_roster,
        "broadcastOrder": order,
    })
    return {
        "ok": True,
        "meetingId": body.meetingId,
        "roster": wire_roster,
        "agents": wire_roster,
        "broadcastOrder": order,
    }


@router.post("/api/cabinet/participants/remove")
def cabinet_participant_remove(body: CabinetParticipantBody) -> dict:
    chat_id = (body.chatId or "").strip()
    _cabinet_validate_room_request(body.meetingId, chat_id)
    agent_id = (body.agentId or "").strip()
    if not agent_id:
        raise HTTPException(status_code=400, detail="invalid agentId")
    _reject_main_translation(agent_id)
    try:
        roster = _cabinet_room_state.remove_meeting_participant(body.meetingId, agent_id)
    except Exception as exc:  # noqa: BLE001
        raise _cabinet_room_state_error(exc) from exc
    meeting = _cabinet_get_meeting(body.meetingId) or {}
    wire_roster = _cabinet_room_state.roster_to_wire(roster)
    order = _cabinet_room_state.broadcast_order(roster)
    _cabinet_channels.get_channel(body.meetingId).emit({
        "type": "meeting_state_update",
        "agents": wire_roster,
        "broadcastOrder": order,
        "pinnedAgent": meeting.get("pinned_persona"),
    })
    return {
        "ok": True,
        "meetingId": body.meetingId,
        "roster": wire_roster,
        "agents": wire_roster,
        "broadcastOrder": order,
        "pinnedAgent": meeting.get("pinned_persona"),
    }


@router.get("/api/cabinet/transcripts")
def cabinet_transcripts(
    meetingId: int = Query(...),
    chatId: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    beforeTs: int | None = Query(default=None),
    beforeId: int | None = Query(default=None),
) -> dict:
    """Port dashboard.ts:851-883 — paginated transcript with B8 high-water cursor.

    `cabinet_transcripts.id` is the durable cursor (NEVER SSE seq). Page
    backward via `beforeId`. Captures `latestSeq` BEFORE the transcript
    query so the SSE seenSeqs dedup is gap-safe.
    """
    meeting = _cabinet_get_meeting(meetingId)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting_not_found")
    if chatId is not None and not _cabinet_chat_match_or_403(meeting, chatId):
        raise HTTPException(status_code=403, detail="chat_mismatch")

    # Capture latestSeq BEFORE the transcript query (port comment :864-870).
    latest_seq = _cabinet_channels.get_channel(meetingId).latest_seq()

    conn = get_connection()
    try:
        # Build the query with optional beforeTs / beforeId cursors.
        clauses = ["meeting_id = ?"]
        params: list = [meetingId]
        if beforeTs is not None:
            clauses.append("created_at < ?")
            params.append(beforeTs)
        if beforeId is not None:
            clauses.append("id < ?")
            params.append(beforeId)
        sql = (
            "SELECT id, meeting_id, speaker, text, created_at "
            "FROM cabinet_transcripts WHERE " + " AND ".join(clauses) +
            " ORDER BY id DESC LIMIT ?"
        )
        params.append(limit)
        rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()

    # Reverse to chronological (oldest first) per upstream.
    transcript = list(reversed([dict(r) for r in rows]))

    return {
        "ok": True,
        "meetingId": meetingId,
        "transcript": transcript,
        "pinnedAgent": meeting.get("pinned_persona"),
        "meetingStartedAt": meeting.get("started_at"),
        "endedAt": meeting.get("ended_at"),
        "agents": _cabinet_roster_dicts(meetingId),
        "broadcastOrder": _cabinet_broadcast_order(meetingId),
        "latestSeq": latest_seq,
    }


@router.get("/api/cabinet/stream")
async def cabinet_stream(
    request: Request,
    meetingId: int = Query(...),
    chatId: str | None = Query(default=None),
    sinceSeq: int = Query(default=0, ge=0),
) -> StreamingResponse:
    """Port dashboard.ts:885-990 — SSE consumer.

    B4 Race-hardening Homie delta (subscribe-first → seen_seqs → snapshot
    direct-write → replay-after) inspired by upstream's seenSeqs pattern at
    dashboard.ts:925-972.

    M4 Homie delta — 410 Gone + X-Refetch-Hint header when sinceSeq is
    older than the channel's oldest_seq (vs upstream's `replay_gap`-event-only).
    """
    meeting = _cabinet_get_meeting(meetingId)
    if meeting is None:
        return JSONResponse({"error": "meeting_not_found"}, status_code=404)
    if chatId is not None and not _cabinet_chat_match_or_403(meeting, chatId):
        return JSONResponse({"error": "chat_mismatch"}, status_code=403)

    # Last-Event-ID overrides sinceSeq when present (browser standard).
    last_event_id_header = request.headers.get("Last-Event-ID")
    if last_event_id_header:
        try:
            sinceSeq = max(sinceSeq, int(last_event_id_header))
        except ValueError:
            pass

    channel = _cabinet_channels.get_channel(meetingId)

    # B4 — emit 410 Gone with X-Refetch-Hint when replay window exceeded.
    oldest = channel.oldest_seq()
    if sinceSeq > 0 and oldest > 0 and sinceSeq < oldest - 1:
        return JSONResponse(
            status_code=410,
            content={
                "error": "replay_gap",
                "sinceSeq": sinceSeq,
                "oldestSeq": oldest,
                "latestSeq": channel.latest_seq(),
            },
            headers={
                "X-Refetch-Hint": f"GET /api/cabinet/transcripts?meetingId={meetingId}",
            },
        )

    async def event_gen() -> AsyncIterator[bytes]:
        # B4 SUBSCRIBE FIRST so events emitted concurrently with the replay
        # drain aren't lost.
        queue, unsub = channel.subscribe()
        seen_seqs: set[int] = set()
        try:
            # 1. Initial meeting_state snapshot — DIRECT write to this
            #    subscriber (NOT through channel.emit; that would pollute
            #    the buffer for OTHER subscribers).
            snapshot_event = {
                "type": "meeting_state",
                "meetingId": meetingId,
                "pinnedAgent": meeting.get("pinned_persona"),
                "agents": _cabinet_roster_dicts(meetingId),
                "broadcastOrder": _cabinet_broadcast_order(meetingId),
                "isFresh": meeting.get("ended_at") is None and meeting.get("entry_count", 0) == 0,
            }
            # Use _sse_format_no_id so the snapshot does NOT clobber the
            # browser's lastEventId on reconnect (dashboard-owner SSE minor
            # fix). The snapshot is replay-position-neutral; only real
            # channel events with seq>=1 advance lastEventId.
            payload = json.dumps({"seq": 0, "event": snapshot_event})
            yield _sse_format_no_id("message", payload).encode("utf-8")

            # If meeting already ended, send meeting_ended + close.
            if meeting.get("ended_at") is not None:
                ended_evt = {
                    "type": "meeting_ended",
                    "meetingId": meetingId,
                    "at": meeting["ended_at"],
                }
                yield _sse_format(
                    0, "message", json.dumps({"seq": 0, "event": ended_evt})
                ).encode("utf-8")
                return

            # 2. Replay window AFTER subscribing — dedup against seen_seqs.
            for entry in channel.since(sinceSeq):
                if entry.seq in seen_seqs:
                    continue
                seen_seqs.add(entry.seq)
                payload = json.dumps({"seq": entry.seq, "event": entry.event})
                yield _sse_format(entry.seq, "message", payload).encode("utf-8")

            # 3. Live drain.
            import asyncio  # noqa: PLC0415
            last_keepalive = time.monotonic()
            while True:
                if await request.is_disconnected():
                    return
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    now = time.monotonic()
                    if now - last_keepalive >= 20:
                        yield _sse_format(0, "ping", "{}").encode("utf-8")
                        last_keepalive = now
                    continue
                if entry.seq in seen_seqs:
                    continue
                seen_seqs.add(entry.seq)
                payload = json.dumps({"seq": entry.seq, "event": entry.event})
                yield _sse_format(entry.seq, "message", payload).encode("utf-8")
        finally:
            unsub()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.post("/api/cabinet/send")
async def cabinet_send(body: CabinetSendBody) -> dict:
    """Port dashboard.ts:1016-1105 — operator message → orchestrator.

    M7 — kill-switch chain: `kill_switches.requireEnabled('cabinet')`
    raises KillSwitchDisabled → 503; lane_router's `llm` switch (automatic)
    will refuse subsequent SDK calls.
    """
    text = (body.text or "").strip()
    client_msg_id = (body.clientMsgId or "").strip()
    chat_id = (body.chatId or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    if len(text) > 8000:
        raise HTTPException(status_code=400, detail="text too long (max 8000 chars)")
    if not client_msg_id:
        raise HTTPException(status_code=400, detail="invalid clientMsgId")

    meeting = _cabinet_get_meeting(body.meetingId)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting_not_found")
    if meeting.get("ended_at") is not None:
        raise HTTPException(status_code=410, detail="meeting_ended")
    if chat_id and not _cabinet_chat_match_or_403(meeting, chat_id):
        raise HTTPException(status_code=403, detail="chat_mismatch")

    queued_command_name: str | None = None
    command = _cabinet_room_commands.parse_room_command(text)
    if command is not None:
        channel = _cabinet_channels.get_channel(body.meetingId)
        if command.name == "help":
            channel.emit({
                "type": "system_note",
                "text": "Commands: /all, /add @agent, /remove @agent, /pin @agent, /unpin, /voice, /end",
                "tone": "info",
                "dismissable": True,
            })
            return {"ok": True, "command": True, "name": command.name}
        if command.name == "voice":
            channel.emit({
                "type": "system_note",
                "text": "Voice is available for this Cabinet room from the voice room entrypoint.",
                "tone": "info",
                "dismissable": True,
            })
            return {"ok": True, "command": True, "name": command.name}
        if command.name == "add":
            if not command.agent_id:
                raise HTTPException(status_code=400, detail="missing agent")
            result = cabinet_participant_add(CabinetParticipantBody(
                meetingId=body.meetingId,
                agentId=command.agent_id,
                chatId=chat_id or None,
            ))
            channel.emit({
                "type": "system_note",
                "text": f"Added @{command.agent_id} to the Cabinet room.",
                "tone": "info",
                "dismissable": True,
            })
            return {"ok": True, "command": True, "name": command.name, **result}
        if command.name == "remove":
            if not command.agent_id:
                raise HTTPException(status_code=400, detail="missing agent")
            result = cabinet_participant_remove(CabinetParticipantBody(
                meetingId=body.meetingId,
                agentId=command.agent_id,
                chatId=chat_id or None,
            ))
            channel.emit({
                "type": "system_note",
                "text": f"Removed @{command.agent_id} from the Cabinet room.",
                "tone": "info",
                "dismissable": True,
            })
            return {"ok": True, "command": True, "name": command.name, **result}
        if command.name == "pin":
            if not command.agent_id:
                raise HTTPException(status_code=400, detail="missing agent")
            result = cabinet_pin(CabinetPinBody(
                meetingId=body.meetingId,
                agentId=command.agent_id,
                chatId=chat_id or None,
            ))
            channel.emit({
                "type": "system_note",
                "text": f"Pinned @{command.agent_id}.",
                "tone": "info",
                "dismissable": True,
            })
            return {"ok": True, "command": True, "name": command.name, **result}
        if command.name == "unpin":
            result = cabinet_unpin(CabinetMeetingIdBody(
                meetingId=body.meetingId,
                chatId=chat_id or None,
            ))
            channel.emit({
                "type": "system_note",
                "text": "Cleared Cabinet pin.",
                "tone": "info",
                "dismissable": True,
            })
            return {"ok": True, "command": True, "name": command.name, **result}
        if command.name == "end":
            result = await cabinet_end(CabinetMeetingIdBody(
                meetingId=body.meetingId,
                chatId=chat_id or None,
            ))
            return {"ok": True, "command": True, "name": command.name, **result}
        if command.name == "all":
            text = command.message
            if not text:
                raise HTTPException(status_code=400, detail="empty /all message")
            body.audience = "all"
            queued_command_name = command.name

    # Fire-and-forget — client tracks progress via SSE.
    import asyncio  # noqa: PLC0415

    async def _run() -> None:
        try:
            from cabinet.text_orchestrator import (  # noqa: PLC0415
                HandleTurnOptions,
                handle_text_turn,
            )
            opts = HandleTurnOptions(
                # Phase 6 voice extensions — forward to the orchestrator.
                # When isVoice/targetAgentId are False/None on the wire body
                # the dataclass defaults preserve Phase 5a behavior verbatim.
                is_voice=body.isVoice,
                target_agent_id=body.targetAgentId,
                audience=body.audience,
                target_agent_ids=body.targetAgentIds,
            )
            await handle_text_turn(body.meetingId, text, client_msg_id, opts)
        except Exception as exc:  # noqa: BLE001
            # Surface to channel as error event so UI unfreezes.
            ch = _cabinet_channels.get_channel(body.meetingId)
            ch.emit({
                "type": "error",
                "message": str(exc),
                "recoverable": True,
            })

    asyncio.create_task(_run())
    response = {"ok": True, "queued": True}
    if queued_command_name is not None:
        response.update({"command": True, "name": queued_command_name})
    return response


@router.post("/api/cabinet/abort")
def cabinet_abort(body: CabinetMeetingIdBody) -> dict:
    """Port dashboard.ts:1107-1119."""
    meeting = _cabinet_get_meeting(body.meetingId)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting_not_found")
    chat_id = (body.chatId or "").strip()
    if chat_id and not _cabinet_chat_match_or_403(meeting, chat_id):
        raise HTTPException(status_code=403, detail="chat_mismatch")
    count = _cabinet_orch.cancel_meeting_turns(body.meetingId)
    return {"ok": True, "cancelled": count}


@router.post("/api/cabinet/pin")
def cabinet_pin(body: CabinetPinBody) -> dict:
    """Port dashboard.ts:1121-1140."""
    agent_id = (body.agentId or "").strip()
    if not agent_id:
        raise HTTPException(status_code=400, detail="invalid agentId")
    # Phase 5a dashboard-owner GAP 3 fix: defense-in-depth — reject literal
    # "main" with the canonical 4xx detail from _reject_main_translation
    # rather than relying on the generic "unknown agent" message. Matches the
    # pattern used by every conversation/* endpoint.
    _reject_main_translation(agent_id)
    meeting = _cabinet_get_meeting(body.meetingId)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting_not_found")
    if meeting.get("ended_at") is not None:
        raise HTTPException(status_code=410, detail="meeting_ended")
    chat_id = (body.chatId or "").strip()
    if chat_id and not _cabinet_chat_match_or_403(meeting, chat_id):
        raise HTTPException(status_code=403, detail="chat_mismatch")
    roster_ids = {a.id for a in _cabinet_room_state.load_meeting_roster(body.meetingId)}
    if agent_id not in roster_ids:
        raise HTTPException(status_code=400, detail="unknown agent")
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE cabinet_meetings SET pinned_persona = ? WHERE id = ?",
            (agent_id, body.meetingId),
        )
        conn.commit()
    finally:
        conn.close()
    _cabinet_channels.get_channel(body.meetingId).emit({
        "type": "meeting_state_update",
        "pinnedAgent": agent_id,
        "agents": _cabinet_roster_dicts(body.meetingId),
        "broadcastOrder": _cabinet_broadcast_order(body.meetingId),
    })
    return {"ok": True, "meetingId": body.meetingId, "pinnedAgent": agent_id}


@router.post("/api/cabinet/unpin")
def cabinet_unpin(body: CabinetMeetingIdBody) -> dict:
    """Port dashboard.ts:1142-1155."""
    meeting = _cabinet_get_meeting(body.meetingId)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting_not_found")
    if meeting.get("ended_at") is not None:
        raise HTTPException(status_code=410, detail="meeting_ended")
    chat_id = (body.chatId or "").strip()
    if chat_id and not _cabinet_chat_match_or_403(meeting, chat_id):
        raise HTTPException(status_code=403, detail="chat_mismatch")
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE cabinet_meetings SET pinned_persona = NULL WHERE id = ?",
            (body.meetingId,),
        )
        conn.commit()
    finally:
        conn.close()
    _cabinet_channels.get_channel(body.meetingId).emit({
        "type": "meeting_state_update",
        "pinnedAgent": None,
        "agents": _cabinet_roster_dicts(body.meetingId),
        "broadcastOrder": _cabinet_broadcast_order(body.meetingId),
    })
    return {"ok": True, "meetingId": body.meetingId, "pinnedAgent": None}


@router.post("/api/cabinet/clear")
async def cabinet_clear(body: CabinetMeetingIdBody) -> dict:
    """Port dashboard.ts:1157-1194."""
    meeting = _cabinet_get_meeting(body.meetingId)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting_not_found")
    if meeting.get("ended_at") is not None:
        raise HTTPException(status_code=410, detail="meeting_ended")
    chat_id = (body.chatId or "").strip()
    if chat_id and not _cabinet_chat_match_or_403(meeting, chat_id):
        raise HTTPException(status_code=403, detail="chat_mismatch")

    # Cancel in-flight turn FIRST and wait idle.
    if _cabinet_orch.get_active_turn_ids(body.meetingId):
        _cabinet_orch.cancel_meeting_turns(body.meetingId)
        await _cabinet_orch.wait_for_meeting_turns_idle(body.meetingId, timeout_ms=5000)

    # Persist divider row so reload shows the marker.
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO cabinet_transcripts (meeting_id, speaker, text)
               VALUES (?, ?, ?)""",
            (body.meetingId, "__divider__", "Memory cleared — agents start fresh from here"),
        )
        conn.commit()
    finally:
        conn.close()

    channel = _cabinet_channels.get_channel(body.meetingId)
    channel.emit({
        "type": "divider",
        "kind": "memory_cleared",
        "text": "Memory cleared — agents start fresh from here",
    })
    channel.emit({
        "type": "system_note",
        "text": "Sessions cleared. Next message starts fresh.",
        "tone": "info",
        "dismissable": True,
    })
    return {"ok": True, "cleared": True}


@router.post("/api/cabinet/end")
async def cabinet_end(body: CabinetMeetingIdBody) -> dict:
    """Port dashboard.ts:1239-1254 + endTextMeeting helper at :1199-1237."""
    meeting = _cabinet_get_meeting(body.meetingId)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting_not_found")
    chat_id = (body.chatId or "").strip()
    if chat_id and not _cabinet_chat_match_or_403(meeting, chat_id):
        raise HTTPException(status_code=403, detail="chat_mismatch")

    if meeting.get("ended_at") is not None:
        return {"ok": True, "meetingId": body.meetingId, "alreadyEnded": True}

    # End meeting in DB + cancel active turns + close channel after grace.
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE cabinet_meetings
               SET ended_at = strftime('%s','now')
               WHERE id = ? AND ended_at IS NULL""",
            (body.meetingId,),
        )
        # Mirror in cabinet_text_meetings (best-effort).
        conn.execute(
            """UPDATE cabinet_text_meetings
               SET ended_at = strftime('%s','now')
               WHERE meeting_id = ? AND ended_at IS NULL""",
            (body.meetingId,),
        )
        row = conn.execute(
            "SELECT entry_count FROM cabinet_meetings WHERE id = ?",
            (body.meetingId,),
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    entry_count = row["entry_count"] if row else 0

    if _cabinet_orch.get_active_turn_ids(body.meetingId):
        _cabinet_orch.cancel_meeting_turns(body.meetingId)
        await _cabinet_orch.wait_for_meeting_turns_idle(body.meetingId, timeout_ms=3000)

    channel = _cabinet_channels.get_channel(body.meetingId)
    channel.emit({
        "type": "meeting_ended",
        "meetingId": body.meetingId,
        "at": int(time.time()),
    })

    # Close channel after a short grace so in-flight SSE writes drain.
    import asyncio  # noqa: PLC0415

    async def _close_after_grace() -> None:
        await asyncio.sleep(1.5)
        _cabinet_channels.close_channel(body.meetingId)

    try:
        asyncio.create_task(_close_after_grace())
    except Exception:  # noqa: BLE001
        _cabinet_channels.close_channel(body.meetingId)

    _audit_write(
        operator_id="cabinet",
        action="cabinet_end",
        target_persona_id="",
        outcome="ended",
        detail={"meeting_id": body.meetingId, "entry_count": entry_count},
    )
    return {"ok": True, "meetingId": body.meetingId, "entryCount": entry_count}


# Silence unused-import lint warnings for late imports above.
_ = (_cabinet_title,)


# ── PRD-8 Phase 6 — cabinet voice browser endpoints ──────────────────────
#
# Three routes mounted on the orchestration API process (port 4322):
#
#   GET /api/cabinet/voice/ui                — server-rendered HTML page
#   GET /api/cabinet/voice/client.bundle.js  — vendored Pipecat bundle
#   GET /api/cabinet/voice/client.js         — vendored esbuild source (rebuild reference)
#   GET /api/cabinet/voice/avatars/{id}.png  — bundled persona avatar
#
# Per Translation Boundary Audit (R1 v2 B6 fix), the avatar route is an
# explicit Homie deviation — upstream's /warroom-avatar/:id was already
# removed, and the canonical replacement is this token-bound endpoint.
# Everything else is a verbatim port of src/dashboard.ts:453-565.


_CABINET_VOICE_STATIC_DIR = (
    Path(__file__).resolve().parent / "cabinet" / "voice" / "static"
)


@router.get("/api/cabinet/voice/ui")
async def cabinet_voice_ui(
    token: str = Query(..., description="orchestration API token (loopback OK if empty)"),
    meetingId: int = Query(..., description="Phase 5a cabinet meeting id"),
    chatId: str = Query("", description="Telegram chat id (empty = any)"),
) -> Any:
    """Server-rendered cabinet voice meeting page.

    VERBATIM port of ``src/dashboard.ts:453-565`` ``app.get('/warroom', ...)``
    — read token + chatId + meetingId from query, return the HTML page.

    Auth contract (PRD-8 Phase 6 v2 fix-pass 2026-05-10 — B1 fix):
    ``/api/cabinet/voice/*`` is exempt from the orchestration API's
    header-bearer middleware AND validates a query-param token instead.
    The middleware exemption + query-param validator live at
    ``orchestration/api.py:auth_middleware``. In token-unset mode the
    voice UI is loopback-only (mirrors orchestration loopback no-token
    mode); in token-set mode the query-param ``token`` must equal
    ``ORCHESTRATION_API_TOKEN`` or the middleware returns 401 BEFORE the
    route handler runs.
    """
    from cabinet.voice.voice_html import get_voice_meeting_html  # noqa: PLC0415
    from cabinet.voice.config import voice_port  # noqa: PLC0415
    from fastapi.responses import HTMLResponse  # noqa: PLC0415

    # Verify the meeting exists + chat-scope binding.
    meeting = _cabinet_get_meeting(meetingId)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting_not_found")
    if chatId and not _cabinet_chat_match_or_403(meeting, chatId):
        raise HTTPException(status_code=403, detail="chat_mismatch")
    if meeting.get("ended_at") is not None:
        raise HTTPException(status_code=410, detail="meeting_ended")

    # PRD-8 Phase 6 follow-up 2026-05-10 — dynamic UI tiles. Resolve the
    # tile roster from the meeting's ``broadcast_order`` JSON snapshot
    # (written at meeting-create time, see ``cabinet_new``) so the tiles
    # match the agents that will actually receive turns. Falls back to
    # ``None`` (→ voice_html's hardcoded 5-stub default) for pre-Phase-6
    # meetings where ``broadcast_order`` is NULL.
    roster = _cabinet_voice_resolve_roster(meetingId)

    body = get_voice_meeting_html(
        token=token,
        meeting_id=meetingId,
        chat_id=chatId,
        ws_port=voice_port(),
        roster=roster,
    )
    return HTMLResponse(content=body, status_code=200)


def _cabinet_voice_resolve_roster(meeting_id: int) -> list[dict] | None:
    """Resolve the voice UI's tile roster from the meeting's broadcast_order.

    PRD-8 Phase 6 follow-up 2026-05-10 — close the UI-vs-routing gap.
    Returns a list of ``{id, name, description}`` dicts matching the
    meeting's ``broadcast_order`` snapshot, preserving snapshot order.

    For each broadcast_order id, looks up the live roster dict (built
    by ``_cabinet_roster_dicts``); if a persona was deleted post-meeting-
    create, falls back to a stub ``{id, name: id-titlecased, description: ""}``
    so the tile still renders.

    Returns ``None`` when ``broadcast_order`` is NULL/empty/malformed.
    Caller falls through to ``voice_html``'s hardcoded 5-stub default for
    pre-Phase-6 meetings.

    Rule 2 — physical-state-first: reads ``broadcast_order`` directly from
    the meeting row (snapshot at create time) via a fresh query, not a
    meta/version row. The standard ``_cabinet_get_meeting`` query does NOT
    select ``broadcast_order`` (R6 NB1 query shape frozen), so this helper
    does its own narrow read.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT broadcast_order FROM cabinet_meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        # Stale DB pre-dating the Phase 6 ``broadcast_order`` column —
        # fall through to the hardcoded voice_html default. Same Rule 2
        # graceful-degrade pattern voice_server._load_broadcast_order_from_db
        # uses.
        return None
    if row is None:
        return None
    raw = row[0] if not isinstance(row, dict) else row.get("broadcast_order")
    if not raw:
        return None
    try:
        ids = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(ids, list) or not ids:
        return None

    live_lookup = {a["id"]: a for a in _cabinet_roster_dicts() if isinstance(a, dict) and a.get("id")}

    resolved: list[dict] = []
    for pid in ids:
        if not isinstance(pid, str) or not pid:
            continue
        live = live_lookup.get(pid)
        if live is not None:
            resolved.append(live)
        else:
            # Persona was deleted post-meeting-create — stub the tile so
            # operators see the historical roster shape even if the persona
            # is gone. ``description`` is intentionally empty (don't fabricate).
            resolved.append({
                "id": pid,
                "name": pid.replace("_", " ").replace("-", " ").title() or pid,
                "description": "",
            })
    return resolved if resolved else None


@router.get("/api/cabinet/voice/client.bundle.js")
async def cabinet_voice_client_bundle(
    token: str = Query("", description="orchestration API token (loopback OK)"),
) -> Any:
    """Serve the vendored Pipecat browser bundle.

    Maps upstream ``app.get('/warroom-client.js', ...)`` (verbatim
    contract). Ships ~430KB built artifact from
    ``cabinet/voice/static/client.bundle.js`` (BSD-2 attributed via
    prepended comment block — see static/client.bundle.js header).
    """
    from fastapi.responses import FileResponse  # noqa: PLC0415

    path = _CABINET_VOICE_STATIC_DIR / "client.bundle.js"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="client_bundle_missing")
    # token is read for parity with /warroom-client.js?token=... but is not
    # currently enforced — bundle is public static content (matches upstream).
    _ = token
    return FileResponse(
        path=str(path),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/api/cabinet/voice/client.js")
async def cabinet_voice_client_source() -> Any:
    """Serve the 12-LOC esbuild source (rebuild reference).

    Vendored verbatim from ClaudeClaw ``warroom/client.js``. Operators who
    need to rebuild the bundle locally can use this as the entry point.
    """
    from fastapi.responses import FileResponse  # noqa: PLC0415

    path = _CABINET_VOICE_STATIC_DIR / "client.js"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="client_js_missing")
    return FileResponse(
        path=str(path),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/api/cabinet/voice/avatars/{persona_id}.png")
async def cabinet_voice_avatar(persona_id: str) -> Any:
    """Serve a persona avatar image.

    Lookup precedence:
      1. ``<profile>/config.yaml.cabinet.avatar_path`` (per-persona override).
      2. Bundled ClaudeClaw avatar at
         ``cabinet/voice/static/avatars/{persona_id}.png``.
      3. Bundled ``default.png`` (Q4 canonical fallback for unknown personas).
      4. Bundled ``main.png`` (backwards-compatible upstream fallback).

    The persona_id is sanity-checked against a strict whitelist regex so
    a maliciously-crafted URL can't escape the static dir (defense-in-depth
    on top of FastAPI's path validation).

    PRD-8 Phase 6 v2 R2 fix-pass 2026-05-10 (R2-M1): inserted
    ``default.png`` as step 3 between the persona-specific bundled asset
    and the upstream-compat ``main.png``. Q4 added ``default.png`` as the
    canonical fallback, so unknown personas should hit it before
    ``main.png``; if ``main.png`` is later removed, ``default.png``
    keeps unknown-persona avatar requests serving cleanly.
    """
    import re as _re  # noqa: PLC0415
    from fastapi.responses import FileResponse  # noqa: PLC0415

    if not _re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", persona_id or ""):
        raise HTTPException(status_code=400, detail="invalid persona_id")

    # 1. Per-persona override from config.yaml.cabinet.avatar_path.
    #
    # PRD-8 Phase 6 v2 fix-pass 2026-05-10 (M4 fix) — verify PNG magic
    # bytes (0x89 0x50 0x4E 0x47 0x0D 0x0A 0x1A 0x0A) before serving an
    # operator-supplied override. Defense-in-depth: if a non-PNG file
    # gets pointed at via config (e.g. operator typo or symlink swap),
    # fall through to the bundled avatar instead of streaming an
    # arbitrary file with image/png Content-Type. FileResponse already
    # blocks directory traversal via Path().is_file(); this adds content
    # validation on top.
    _PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
    try:
        cfg = personas.load_persona_config(persona_id)
        cabinet_block = cfg.get("cabinet") if isinstance(cfg, dict) else None
        if isinstance(cabinet_block, dict):
            override = cabinet_block.get("avatar_path")
            if isinstance(override, str) and override.strip():
                override_path = Path(override).expanduser()
                if not override_path.is_absolute():
                    profile_root = resolve_profile_root(persona_id)
                    override_path = profile_root / override_path
                if override_path.is_file():
                    try:
                        with open(override_path, "rb") as _f:
                            magic = _f.read(8)
                    except OSError as _read_exc:
                        logger.warning(
                            "cabinet voice avatar override read failed for %s: %s",
                            _redact(persona_id),
                            _redact(str(_read_exc)),
                        )
                        magic = b""
                    if magic == _PNG_MAGIC:
                        return FileResponse(
                            path=str(override_path),
                            media_type="image/png",
                            headers={"Cache-Control": "public, max-age=3600"},
                        )
                    logger.warning(
                        "cabinet voice avatar override at %s is not a valid "
                        "PNG (magic bytes mismatch); falling back to bundled",
                        _redact(str(override_path)),
                    )
    except Exception as exc:  # noqa: BLE001 — fall through to bundled.
        logger.debug(
            "cabinet voice avatar override read failed for %s: %s",
            _redact(persona_id),
            _redact(str(exc)),
        )

    # 2. Bundled ClaudeClaw avatar.
    bundled = _CABINET_VOICE_STATIC_DIR / "avatars" / f"{persona_id}.png"
    if bundled.is_file():
        return FileResponse(
            path=str(bundled),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    # 3. Bundled default.png — Q4 canonical fallback for unknown personas.
    default_fallback = _CABINET_VOICE_STATIC_DIR / "avatars" / "default.png"
    if default_fallback.is_file():
        return FileResponse(
            path=str(default_fallback),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=300"},
        )

    # 4. Bundled main.png — backwards-compatible upstream fallback.
    fallback = _CABINET_VOICE_STATIC_DIR / "avatars" / "main.png"
    if fallback.is_file():
        return FileResponse(
            path=str(fallback),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=300"},
        )

    raise HTTPException(status_code=404, detail="avatar_missing")
